"""Stage 2: the numbers chosen from one annual filing's dump.

``extract_filing`` turns one 10-K / 20-F / 40-F into the ratio row (which
tagged number is Revenue, Net income, CapEx...), the statement lines, the
facts of the filing's own period and the calculation checks. It runs on
``filing_view.FilingView``, the filing as ``dump.py`` stored it, so a fix
here (a missing tag, a better fallback) reaches every filing already dumped
on the next run, with no SEC request.
"""

from __future__ import annotations

import re
from typing import Iterable

import numpy as np
import pandas as pd

import calc_check
import xbrl_extract

# Minimum span (in days) for a "duration" fact to count as the annual figure.
# This reliably filters out the quarterly facts that appear in the notes and
# the quarterly columns of comparative disclosures.
ANNUAL_MIN_DAYS = 300

# A selected fact's period end must sit this close to the filing's report date.
# Without it, a concept the filer did not tag in the current year falls back to
# the prior-year comparative in the same filing (QCOM FY2020 goodwill
# impairment was FY2019's 146M, not FY2020's 0).
PERIOD_MATCH_DAYS = 20

# Post-merger filers tag the consolidated statements under a successor/
# predecessor reporting basis using this axis. It marks a period's basis, not
# a line-item breakdown, so a scenario-only fact is a valid consolidated value
# and is used as a fallback when no dimension-free fact exists.
SCENARIO_AXIS = "us-gaap:StatementScenarioAxis"

# XBRL concept resolution order per metric. The first concept that yields an
# annual fact for the fiscal year wins. Netflix reports plain `Revenues`,
# `CostOfRevenue` and `LongTermDebtNoncurrent`, so those fallbacks matter.
METRIC_CONCEPTS: dict[str, tuple[list[str], str]] = {
    "Revenues": (
        [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            # KHC and others tag the "including assessed tax" variant (post-
            # ASC 606); omitting it silently drops revenue for those years.
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            # Older filings (pre-ASC 606) often split goods/services or use this.
            "SalesRevenueGoodsNet",
            # IFRS filers (e.g. Celestica 20-F); plain Revenue is IFRS's total.
            "RevenueFromContractsWithCustomers",
            "Revenue",
        ],
        "duration",
    ),
    "NetIncomeLoss": (
        [
            "NetIncomeLoss",
            # Used by companies with non-controlling interests / preferred
            # stock that stop tagging plain NetIncomeLoss (e.g. Estee Lauder
            # from FY2021 on).
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            # IFRS filers.
            "ProfitLossAttributableToOwnersOfParent",
            # Last resort: total net income including non-controlling interests.
            "ProfitLoss",
        ],
        "duration",
    ),
    "GrossProfit": (["GrossProfit"], "duration"),
    "CostOfRevenue": (
        [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
            "CostOfGoodsSold",
            "CostOfServices",
            # IFRS filers.
            "CostOfSales",
        ],
        "duration",
    ),
    "SellingGeneralAndAdministrativeExpense": (
        [
            "SellingGeneralAndAdministrativeExpense",
            "SellingGeneralAndAdministrative",
            # IFRS filers (e.g. Celestica).
            "SellingGeneralAndAdminExpenses",
        ],
        "duration",
    ),
    "ResearchAndDevelopmentExpense": (
        [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
            # IFRS filers.
            "ResearchAndDevelopmentExpenses",
        ],
        "duration",
    ),
    "GoodwillImpairmentLoss": (
        [
            "GoodwillImpairmentLoss",
            # Combined goodwill + intangibles impairment, or the broader
            # catch-all some filers use for the annual writedown.
            "GoodwillAndIntangibleAssetImpairment",
            "AssetImpairmentCharges",
            # IFRS filers.
            "ImpairmentLossRecognisedInProfitOrLossGoodwill",
        ],
        "duration",
    ),
    "OperatingIncomeLoss": (
        [
            "OperatingIncomeLoss",
            # IFRS filers.
            "ProfitLossFromOperatingActivities",
        ],
        "duration",
    ),
    "OperatingCashFlow": (
        [
            "NetCashProvidedByUsedInOperatingActivities",
            # Older filings (pre-~2017) use the "continuing operations" variant.
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            # IFRS filers.
            "CashFlowsFromUsedInOperatingActivities",
        ],
        "duration",
    ),
    "CapEx": (
        [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            # IFRS filers (e.g. Celestica, TSMC).
            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "PurchaseOfPropertyPlantAndEquipment",
        ],
        "duration",
    ),
    "AssetsCurrent": (
        [
            "AssetsCurrent",
            # IFRS filers.
            "CurrentAssets",
        ],
        "instant",
    ),
    "LiabilitiesCurrent": (
        [
            "LiabilitiesCurrent",
            # IFRS filers.
            "CurrentLiabilities",
        ],
        "instant",
    ),
    "LongTermDebt": (
        [
            "LongTermDebt",
            "LongTermDebtNoncurrent",
            # Common balance-sheet line for companies that fold leases in,
            # and the tag Estee Lauder actually uses (no LongTermDebt tag).
            "LongTermDebtAndCapitalLeaseObligations",
            # IFRS filers: total borrowings due after a year (parts below).
            "NoncurrentPortionOfNoncurrentBorrowings",
            "NoncurrentBorrowings",
        ],
        "instant",
    ),
    "StockholdersEquity": (
        [
            "StockholdersEquity",
            # Many filers only tag the total-equity variant (e.g. Estee
            # Lauder from FY2023 on, once it has non-controlling interests).
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            # IFRS filers.
            "EquityAttributableToOwnersOfParent",
            "Equity",
        ],
        "instant",
    ),
}

# A metric no single total covers: the sum of whichever parts the filer tags.
# IFRS income statements by function split SG&A into distribution (selling)
# and administrative costs; some US filers split it the same way.
COMPONENT_METRICS: dict[str, list[list[str]]] = {
    "SellingGeneralAndAdministrativeExpense": [
        ["SellingAndMarketingExpense", "SellingExpense", "DistributionCosts", "SellingAndDistributionExpense"],
        ["GeneralAndAdministrativeExpense", "AdministrativeExpense"],
    ],
    # Filers without a total: bonds or notes, plus loans, plus secured debt
    # (TSMC: bonds and bank loans; Realty Income: notes, term loans, mortgages).
    "LongTermDebt": [
        ["NoncurrentPortionOfNoncurrentBondsIssued", "BondsIssued", "SeniorNotes", "SeniorLongTermNotes",
         "NotesPayable", "UnsecuredDebt"],
        ["LongtermBorrowings", "NoncurrentPortionOfNoncurrentLoansReceived",
         "NoncurrentPortionOfNoncurrentBankLoansReceived", "LongTermLoansFromBank", "LoansPayable"],
        ["SecuredDebt"],
        ["ConvertibleDebtNoncurrent", "ConvertibleNotesPayable"],
    ],
}

# Foreign filers list American Depositary Shares, which can stand for several
# ordinary shares (TSMC: 5) or a fraction of one; prices are per ADS.
ADS_TEXT_RE = re.compile(
    r"each\s+(?:ADS\s+)?represent\w*\s+(?:the\s+right\s+to\s+receive\s+)?(?P<n>[\w./-]+)\s+(?:[\w-]+\s+){0,3}?shares?",
    re.IGNORECASE,
)
ADS_MEMBER_RE = re.compile(r"AmericanDeposit[ao]r|AdrMember|\bADS", re.IGNORECASE)
# American Depositary Shares/Receipts, not the depositary shares of a preferred
# stock that many US banks list (JPM: "each representing a one-four hundredth
# interest in a share of preferred stock").
ADR_TITLE_RE = re.compile(r"american\s+deposit[ao]ry", re.IGNORECASE)
# Ordinary shares per ADS seen in practice; an inferred ratio snaps to one.
NICE_RATIOS = (0.01, 0.02, 0.025, 0.04, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10,
               12, 15, 20, 25, 30, 40, 50, 100)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30, "forty": 40,
    "fifty": 50, "hundred": 100, "one-half": 0.5, "half": 0.5, "one-third": 1 / 3, "one-fourth": 0.25,
    "one-quarter": 0.25, "one-fifth": 0.2, "one-tenth": 0.1,
}
EPS_CONCEPTS = [
    "ifrs-full:BasicEarningsLossPerShare", "us-gaap:EarningsPerShareBasic",
    "ifrs-full:DilutedEarningsLossPerShare", "us-gaap:EarningsPerShareDiluted",
]


def _concept_name(concept: str) -> str:
    """``us-gaap:Foo`` or ``us-gaap_Foo`` -> ``Foo`` (facts vs statement rows)."""
    for separator in (":", "_"):
        if separator in concept:
            return concept.split(separator, 1)[1]
    return concept


def _statement_values(statement_rows: list[dict]) -> dict[str, float]:
    """Concept name -> value for the filing's own dimension-free statement lines.

    Some filers never tag a consolidated total: Duke Energy, for example, tags
    "Cost of sales" only per operating segment and the consolidated line exists
    in the presentation tree alone. Falling back to that line keeps the metric
    present without summing dimensioned facts, which would double count.
    """
    values: dict[str, float] = {}
    for row in statement_rows:
        value = row.get("value")
        if value is None or pd.isna(value):
            continue
        values.setdefault(_concept_name(str(row.get("concept") or "")), float(value))
    return values


def extract_metric(
    facts: pd.DataFrame,
    candidates: Iterable[str],
    period_of_report: str,
    period_type: str,
    statement_values: dict[str, float] | None = None,
    currency: str | None = None,
) -> float:
    """Return the value for the first matching concept, else nan.

    ``facts`` is the dataframe produced by ``xbrl.facts.to_dataframe()``.

    The governing period is located by matching each fact's end date against
    the filing's ``period_of_report`` date. We deliberately do *not* use the
    dataframe's ``fiscal_year`` column: for companies with non-calendar fiscal
    years it mislabels prior-year comparatives (e.g. Apple's FY2017 annual
    revenue is tagged ``fiscal_year=2018``), which silently duplicates years.

    Only dimension-free facts are considered. For duration concepts the fact
    must span an entire year (>= 300 days) so quarterly facts are ignored; the
    remaining candidate closest to the report date wins. For instant concepts
    the value on the balance-sheet date closest to the report date is used.
    When a concept has no dimension-free fact, a scenario-only fact (successor/
    predecessor reporting basis) is accepted as the consolidated value.
    Facts in another currency than ``currency`` are ignored: foreign filers
    such as TSMC add a USD convenience translation of the current year.
    """
    target = pd.Timestamp(period_of_report)
    is_dimensional = facts["is_dimensioned"].astype(bool)
    dimension = facts["dimension"] if "dimension" in facts.columns else pd.Series(pd.NA, index=facts.index)
    scenario_only = dimension == SCENARIO_AXIS
    concept_name = facts["concept"].str.split(":").str[-1]
    in_currency = _in_currency(facts, currency)

    def pick(mask: pd.Series):
        sub = facts[mask & in_currency & (concept_name == concept)].copy()
        if sub.empty:
            return np.nan
        sub["period_start"] = pd.to_datetime(sub["period_start"], errors="coerce")
        # Instant (balance-sheet) facts carry their date in ``period_instant``
        # while duration facts use ``period_end``; merge the two.
        sub["period_end"] = pd.to_datetime(sub["period_end"], errors="coerce")
        sub["period_end"] = sub["period_end"].fillna(
            pd.to_datetime(sub["period_instant"], errors="coerce")
        )
        sub = sub.dropna(subset=["period_end"])
        if period_type == "duration":
            span = (sub["period_end"] - sub["period_start"]).dt.days
            sub = sub[span >= ANNUAL_MIN_DAYS]
        if sub.empty:
            return np.nan
        # Closest period end to the filing's report date is the current year.
        sub["_dist"] = (sub["period_end"] - target).abs()
        sub = sub[sub["_dist"] <= pd.Timedelta(days=PERIOD_MATCH_DAYS)]
        if sub.empty:
            return np.nan
        # A filing often carries the same fact twice: a rounded copy
        # (decimals=-5) and a precise one (decimals=-3). Prefer the precise one.
        if "decimals" in sub.columns:
            sub["_decimals"] = pd.to_numeric(sub["decimals"], errors="coerce").fillna(-99)
        else:
            sub["_decimals"] = -99
        sub = sub.sort_values(["_dist", "_decimals"], ascending=[True, False])
        return pd.to_numeric(sub.iloc[0]["numeric_value"], errors="coerce")

    for concept in candidates:
        # Prefer dimension-free facts; fall back to a scenario-only fact (a
        # successor/predecessor reporting basis) when the filing has none.
        for mask in ((~is_dimensional), scenario_only):
            value = pick(mask)
            if pd.notna(value):
                return float(value)
        # No fact at all for this concept: use the filer's own statement line,
        # which also covers computed totals never tagged as a single fact.
        if statement_values:
            stated = statement_values.get(concept)
            if stated is not None and pd.notna(stated):
                return float(stated)

    return np.nan


def _in_currency(facts: pd.DataFrame, currency: str | None) -> pd.Series:
    """True for facts without a currency (shares, ratios) or in ``currency``."""
    if not currency or "currency" not in facts.columns:
        return pd.Series(True, index=facts.index)
    return facts["currency"].isna() | (facts["currency"].astype(str).str.upper() == currency)


def reporting_currency(facts: pd.DataFrame) -> str | None:
    """The currency most of a filing's money facts are in (per-share units aside)."""
    if "currency" not in facts.columns:
        return None
    unit = facts["unit_ref"].astype(str).str.lower() if "unit_ref" in facts.columns else pd.Series("", index=facts.index)
    money = facts["currency"].notna() & ~unit.str.contains("per", regex=False)
    counts = facts.loc[money, "currency"].astype(str).str.upper().value_counts()
    return str(counts.index[0]) if len(counts) else None


def _ratio_number(text: str) -> float | None:
    text = text.lower().strip(".,")
    if text in NUMBER_WORDS:
        return float(NUMBER_WORDS[text])
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    fraction = re.fullmatch(r"(\d+)/(\d+)", text)
    if fraction and int(fraction.group(2)):
        return int(fraction.group(1)) / int(fraction.group(2))
    return None


def ads_ratio(facts: pd.DataFrame, currency: str | None) -> tuple[float | None, str, bool]:
    """Ordinary shares per ADS, where it came from, and whether ADSs are listed.

    The cover page often says it ("American Depositary Shares, each
    representing five ordinary shares"); else earnings per ADS over earnings
    per ordinary share in the same currency and year (TSMC tags both). Neither:
    ``(None, "", is_adr)``, and valuation tries Yahoo.
    """

    titles = facts.loc[facts["concept"] == "dei:Security12bTitle", "value"].dropna().astype(str)
    titles = titles[titles.str.contains(ADR_TITLE_RE)]
    members = facts["member"].dropna().astype(str) if "member" in facts.columns else pd.Series(dtype=str)
    # Shell spells its member "AmericanDepositorySharesMember".
    is_adr = bool(len(titles) or members.str.contains(r"AdrMember|AmericanDeposit[ao]ry").any())
    for title in titles:
        match = ADS_TEXT_RE.search(title)
        if match:
            number = _ratio_number(match.group("n"))
            ratio = snap_ratio(number) if number else None
            if ratio:
                return ratio, "cover", True
    numbers = pd.to_numeric(facts["numeric_value"], errors="coerce")
    in_currency = _in_currency(facts, currency)
    member = facts["member"].astype(str) if "member" in facts.columns else pd.Series("", index=facts.index)
    for concept in EPS_CONCEPTS:
        rows = facts[(facts["concept"] == concept) & in_currency & numbers.notna()]
        if rows.empty:
            continue
        plain = rows[~rows["is_dimensioned"].astype(bool)]
        ads = rows[rows["is_dimensioned"].astype(bool) & member.loc[rows.index].str.contains(ADS_MEMBER_RE)]
        for _, row in ads.iterrows():
            same = plain[plain["period_end"] == row["period_end"]]
            if same.empty:
                continue
            base = float(pd.to_numeric(same.iloc[0]["numeric_value"]))
            if base:
                ratio = snap_ratio(float(row["numeric_value"]) / base)
                if ratio:
                    return ratio, "eps", True
    return None, "", is_adr


def snap_ratio(value: float | None, tolerance: float = 0.03, ratios: tuple = NICE_RATIOS) -> float | None:
    """The ADS ratio ``value`` is close to (within ``tolerance``), else None.

    Tight by default: the filing's own figures give the ratio almost exactly.
    The Yahoo estimate (trailing EPS against the last annual EPS) passes more.
    """
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    best = min(ratios, key=lambda nice: abs(np.log(value / nice)))
    return float(best) if abs(value / best - 1) <= tolerance else None


def _component_metric(
    facts: pd.DataFrame,
    groups: list[list[str]],
    period_of_report: str,
    period_type: str,
    statement_values: dict[str, float],
    currency: str | None,
) -> float:
    """Sum of the first concept with a value in each group; nan when none has one."""
    parts = [extract_metric(facts, group, period_of_report, period_type, statement_values, currency)
             for group in groups]
    found = [part for part in parts if pd.notna(part)]
    return float(sum(found)) if found else np.nan


# A company-specific capex line: its tag names PP&E (or capital expenditure)
# and a purchase (Toyota's "PurchaseOfPropertyPlantAndEquipmentExcluding...",
# Shell's "CashOutflowForTotalCashCapitalExpenditure").
CAPEX_ASSET_RE = re.compile(r"PropertyPlant|PropertyAndEquipment|FixedAssets|CapitalExpenditure", re.IGNORECASE)
CAPEX_ACTION_RE = re.compile(r"Purchase|Payment|Acquire|Acquisition|Addition|Expenditure|Outflow", re.IGNORECASE)


# Words that say nothing about which revenue stream a line belongs to.
STREAM_STOPWORDS = {"and", "of", "the", "total", "revenue", "revenues", "net", "sales", "other", "cost", "costs",
                    "expense", "expenses", "from", "to", "for", "in", "on", "a", "an"}
# Parents that hold the costs of an income statement by function.
COST_PARENT_RE = re.compile(r"CostsAndExpenses|OperatingExpenses|CostOf", re.IGNORECASE)


def _stream_words(label: str) -> set[str]:
    return {word for word in re.findall(r"[a-z]+", str(label).lower()) if word not in STREAM_STOPWORDS}


def _cost_lines(statement_rows: list[dict]) -> dict[str, float]:
    """The income statement's cost-of-revenue lines: element -> value.

    A line counts when edgartools files it as cost of goods and services
    sold (and it says "cost"), its tag starts with ``CostOf``, or it sits among the costs and is
    named after revenue streams: Oracle has no cost-of-revenue total, only
    "Cloud and software", "Hardware" and "Services" expenses beside the
    revenues of the same names. A line that is the total of others counts
    alone.
    """
    income = [row for row in statement_rows if row.get("statement") == "income"]
    revenue = [row for row in income
               if row.get("standard_concept") == "Revenue"
               or ("Revenue" in _concept_name(str(row.get("concept") or ""))
                   and not _concept_name(str(row.get("concept") or "")).startswith("CostOf"))]
    revenue_concepts = {row["concept"] for row in revenue}
    revenue_words = set().union(*(_stream_words(row.get("label")) for row in revenue)) if revenue else set()
    lines: dict[str, tuple[float, str]] = {}
    for row in income:
        concept = str(row.get("concept") or "")
        name = _concept_name(concept)
        parent = str(row.get("parent_concept") or "")
        value = row.get("value")
        if concept in revenue_concepts or value is None or not value > 0:
            continue
        words = _stream_words(row.get("label"))
        stream = bool(words) and words <= revenue_words and COST_PARENT_RE.search(parent)
        # edgartools also files a bank's "Compensation expense" as cost of goods
        # sold: its classification counts only for a line that says "cost".
        filed_as_cost = row.get("standard_concept") == "CostOfGoodsAndServicesSold" and (
            "cost" in name.lower() or "cost" in str(row.get("label") or "").lower())
        if filed_as_cost or name.startswith("CostOf") or stream:
            lines.setdefault(concept, (float(value), parent))
    totals = [concept for concept in lines if any(parent == concept for _, parent in lines.values())]
    if totals:
        return {totals[0]: lines[totals[0]][0]}
    return {concept: value for concept, (value, _) in lines.items()}


def _cost_of_revenue(row: dict, statement_rows: list[dict]) -> None:
    """Cost of revenue from its lines when no total was found or the one found is a single line.

    Oracle tags its hardware cost alone with ``CostOfRevenue`` (FY2010) and
    has no total later on. Gross profit is then derived (revenue minus cost),
    unless the income statement itself shows a gross profit line: a tagged
    gross profit elsewhere (a segment note) is not the company's.
    """
    lines = _cost_lines(statement_rows)
    if not lines:
        return
    current = row.get("CostOfRevenue")
    single = pd.notna(current) and len(lines) > 1 and any(
        abs(current - value) <= max(1.0, abs(value) * 1e-6) for value in lines.values())
    if pd.notna(current) and not single:
        return
    row["CostOfRevenue"] = float(sum(lines.values()))
    on_statement = {_concept_name(str(r.get("concept") or "")) for r in statement_rows if r.get("statement") == "income"}
    if "GrossProfit" not in on_statement:
        row["GrossProfit"] = np.nan


def _capex_line(statement_rows: list[dict]) -> float:
    """Capex from the one investing line that is clearly it, else nan.

    Only direct parts of the investing-activities total count, and only when
    exactly one matches: NextEra lists capex per subsidiary, and taking one of
    them would understate the total.
    """
    found: dict[str, float] = {}
    for row in statement_rows:
        name = _concept_name(str(row.get("concept") or ""))
        parent = str(row.get("parent_concept") or "")
        if (row.get("statement") == "cashflow" and "InvestingActivities" in parent
                and CAPEX_ASSET_RE.search(name) and CAPEX_ACTION_RE.search(name)
                and not re.search(r"Proceeds|Sale|Disposal", name)):
            found.setdefault(name, abs(float(row["value"])))
    return next(iter(found.values())) if len(found) == 1 else np.nan


def extract_filing(filing, period_key: str) -> dict:
    """Everything a run takes from one annual filing (a ``FilingView`` or a live filing).

    ``{"xbrl": False}`` when the filing has no XBRL (10-Ks before 2009-2011);
    otherwise the ratio ``row`` and the filing's ``statements``, ``facts`` and
    ``checks`` records. Raises when the filing cannot be read.
    """
    xbrl = filing.xbrl()
    if xbrl is None:
        return {"xbrl": False}

    facts = xbrl.facts.to_dataframe()

    # The filer's own fiscal-year label (authoritative, unlike the
    # calendar year of the period end).
    fy_facts = facts.loc[facts["concept"] == "dei:DocumentFiscalYearFocus", "value"]
    if len(fy_facts):
        try:
            fiscal_year = int(float(fy_facts.iloc[0]))
        except (ValueError, TypeError):
            fiscal_year = int(period_key[:4])
    else:
        fiscal_year = int(period_key[:4])

    currency = reporting_currency(facts)
    ratio, ratio_source, is_adr = ads_ratio(facts, currency)
    row: dict = {"period_of_report": period_key, "fiscal_year": fiscal_year,
                 "form": filing.form, "currency": currency or "USD",
                 "ads": is_adr, "ads_ratio": ratio, "ads_ratio_source": ratio_source}
    # Statement rows are also the fallback source for metrics a filer
    # renders but never tags as a consolidated fact.
    statement_rows = xbrl_extract.extract_statements(
        xbrl, period_key, calc_check.period_values(facts, period_key, currency),
        calc_check.convenience_values(facts, period_key, currency))
    statement_values = _statement_values(statement_rows)
    for metric, (concepts, period_type) in METRIC_CONCEPTS.items():
        row[metric] = extract_metric(
            facts, concepts, period_key, period_type, statement_values, currency
        )
    for metric, groups in COMPONENT_METRICS.items():
        if pd.isna(row[metric]):
            row[metric] = _component_metric(facts, groups, period_key,
                                            METRIC_CONCEPTS[metric][1], statement_values, currency)
    if pd.isna(row["CapEx"]):
        row["CapEx"] = _capex_line(statement_rows)
    _cost_of_revenue(row, statement_rows)
    filing_facts = xbrl_extract.extract_facts(xbrl, period_key)
    try:
        filing_checks = calc_check.check_filing(xbrl, facts, period_key, currency)
    except Exception as exc:  # noqa: BLE001 - the check never costs the filing
        print(f"    [!] {period_key}: calculation check skipped ({exc.__class__.__name__}: {exc})")
        filing_checks = []
    return {"xbrl": True, "row": row, "statements": statement_rows,
            "facts": filing_facts, "checks": filing_checks}
