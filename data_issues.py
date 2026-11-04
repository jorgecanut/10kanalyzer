"""Stage 2: what is wrong or missing in a company's numbers, for the market scan and the charts.

Hiding is not fixing. Every problem found here is reported, and a chart drawn
from a number with an open issue says so. A metric the pipeline did not read
is called ``not_reported`` (the filer never gave it) only after the stored
filing itself was searched: its statement, every line of that statement
(also those with no value for the year) and its facts. Our own extraction
having no line is not proof: in batch 1 that test called AAUAF's net loss and
AACO's first-year figures "not in the filing" when both were there.

Issue kinds made here:

- ``check_mismatch``: a total in the filing's calculation linkbase does not add up.
- ``missing_metric``: the filing holds the metric (a fact or a statement line) but the pipeline did not read it.
- ``statement_missing``: no statement to read the metric from could be found in the filing.
- ``not_reported``: the statement is there, with no such line and no fact for the year.
- ``implausible_value``: metrics of one year contradict each other.
"""

from __future__ import annotations

import re

import pandas as pd

import calc_check

# Missing in a fiscal year: worth a look.
KEY_METRICS = ("Revenues", "NetIncomeLoss", "OperatingIncomeLoss", "OperatingCashFlow", "StockholdersEquity")

# Metric -> (statement, standard concepts of its line, label of its line, tags of its fact).
# The fact tags are wider than the pipeline's: a tag the pipeline does not
# know (AAUAF's ProfitLossFromContinuingOperations) is exactly what must not
# pass as "not reported".
EVIDENCE = {
    "Revenues": ("income", {"Revenue", "HealthCareRevenue"},
                 re.compile(r"^(total )?(net )?(operating )?(revenues?|sales)\b(?!.*(cost|marketing|expense))",
                            re.IGNORECASE),
                 re.compile(r"^(Revenues?|RevenueFrom\w+|SalesRevenue\w*|\w*OperatingRevenue|RevenuesNet\w*"
                            r"|HealthCareOrganizationRevenue\w*|ElectricUtilityRevenue|RevenueMineralSales"
                            r"|OilAndGasRevenue|RealEstateRevenueNet)$")),
    "NetIncomeLoss": ("income", {"NetIncome", "ProfitLoss"},
                      re.compile(r"^net (income|loss|earnings|profit)|^(profit|loss) for the", re.IGNORECASE),
                      re.compile(r"^(NetIncomeLoss(AvailableToCommonStockholdersBasic)?|ProfitLoss"
                                 r"|ProfitLossAttributableToOwnersOfParent|ProfitLossFromContinuingOperations)$")),
    "OperatingIncomeLoss": ("income", {"OperatingIncomeLoss"},
                            re.compile(r"^(total )?(operating \(?(income|profit|loss)\)?( \(?(income|profit|loss)\)?)?"
                                       r"|\(?(income|profit|loss)\)?( \(?(income|profit|loss)\)?)? from operations)\s*$",
                                       re.IGNORECASE),
                            re.compile(r"^(OperatingIncomeLoss|ProfitLossFromOperatingActivities)$")),
    "OperatingCashFlow": ("cashflow", {"NetCashFromOperatingActivities"},
                          re.compile(r"operating activities$", re.IGNORECASE),
                          re.compile(r"^(NetCashProvidedByUsedInOperatingActivities\w*"
                                     r"|CashFlowsFromUsedInOperatingActivities\w*)$")),
    "StockholdersEquity": ("balance", {"StockholdersEquity", "TotalEquity"},
                           re.compile(r"^total (?!liabilit).*\b(stockholders?|shareholders?|members?|partners?)"
                                      r"\W+(\w+\W+)*(equity|deficit|capital)\b", re.IGNORECASE),
                           re.compile(r"^(StockholdersEquity\w*|Equity|EquityAttributableToOwnersOfParent"
                                      r"|PartnersCapital\w*|MembersEquity)$")),
}
# Lines named like the metric that are something else: EPS, share counts.
NOT_THE_LINE = re.compile(r"per (share|unit)|PerShare|PerUnit|weighted|Weighted", re.IGNORECASE)
STATEMENT_NAMES = {"income": "income statement", "balance": "balance sheet", "cashflow": "cash flow statement"}
STATEMENT_METHODS = {"income": "income_statement", "balance": "balance_sheet", "cashflow": "cash_flow_statement"}
# A fact's period must end this close to the report date to count for the year.
PERIOD_MATCH_DAYS = 20
# Revenue minus cost may miss gross profit by this much of revenue (a filer's
# rounding, a line between them) before it counts as a contradiction.
GROSS_TOLERANCE = 0.02

TITLES = {
    "check_mismatch": "Total does not add up",
    "missing_metric": "Metric not read",
    "statement_missing": "Statement not found",
    "not_reported": "Not in the filing",
    "implausible_value": "Value looks wrong",
}


def _issue(kind: str, severity: str, **fields) -> dict:
    return {"kind": kind, "severity": severity, **fields}


def _tag(concept: str) -> str:
    """``us-gaap:Foo`` / ``us-gaap_Foo`` -> ``Foo``."""
    return re.split(r"[:_]", concept, maxsplit=1)[-1] if re.search(r"[:_]", concept) else concept


# --------------------------------------------------------------------------- #
# Evidence: what the stored filing holds for a metric the pipeline did not read
# --------------------------------------------------------------------------- #

def _statement_evidence(xbrl, statement: str, standards: set, label: re.Pattern, tags: re.Pattern) -> dict:
    """``state`` (present / missing / failed) and the first line that would hold the metric."""
    try:
        found = getattr(xbrl.statements, STATEMENT_METHODS[statement])()
        data = found.to_dataframe() if found is not None else None
    except Exception as exc:  # noqa: BLE001 - recorded as evidence, never raised
        return {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if data is None or data.empty or "concept" not in data.columns:
        return {"state": "missing"}
    periods = [column for column in data.columns if re.match(r"\d{4}-\d{2}-\d{2}", str(column))]
    for _, row in data.iterrows():
        if bool(row.get("abstract")) or bool(row.get("dimension")):
            continue
        concept = str(row.get("concept") or "")
        if NOT_THE_LINE.search(str(row.get("label") or "")) or NOT_THE_LINE.search(concept):
            continue
        if (row.get("standard_concept") in standards or label.search(str(row.get("label") or ""))
                or tags.search(_tag(concept))):
            values = {str(column): float(row[column]) for column in periods
                      if pd.notna(pd.to_numeric(row.get(column), errors="coerce"))}
            return {"state": "present", "line": {"concept": concept, "label": str(row.get("label") or ""),
                                                 "columns": values}}
    return {"state": "present", "line": None}


def _fact_evidence(facts: pd.DataFrame, period_key: str, tags: re.Pattern) -> dict | None:
    """The dimension-free fact of the metric whose period ends on the report date (longest span first)."""
    if facts is None or facts.empty or "concept" not in facts.columns:
        return None
    names = facts["concept"].astype(str).map(_tag)
    frame = facts[names.map(lambda name: bool(tags.search(name)))]
    if "is_dimensioned" in frame.columns:
        frame = frame[~frame["is_dimensioned"].fillna(False).astype(bool)]
    if frame.empty:
        return None
    target = pd.Timestamp(period_key)
    end = pd.to_datetime(frame.get("period_end"), errors="coerce")
    instant = pd.to_datetime(frame.get("period_instant"), errors="coerce")
    when = end.fillna(instant) if end is not None else instant
    frame = frame[(when - target).abs() <= pd.Timedelta(days=PERIOD_MATCH_DAYS)]
    value = pd.to_numeric(frame.get("numeric_value", frame.get("value")), errors="coerce")
    frame = frame[value.notna()]
    if frame.empty:
        return None
    start = pd.to_datetime(frame.get("period_start"), errors="coerce")
    stop = pd.to_datetime(frame.get("period_end"), errors="coerce")
    days = (stop - start).dt.days
    frame = frame.assign(_days=days.fillna(0)).sort_values("_days", ascending=False)
    best = frame.iloc[0]
    number = pd.to_numeric(best.get("numeric_value", best.get("value")), errors="coerce")
    return {"concept": str(best["concept"]), "value": float(number),
            "days": int(best["_days"]) if best["_days"] else None,
            "end": str((best.get("period_end") if pd.notna(best.get("period_end")) else best.get("period_instant"))),
            "currency": None if pd.isna(best.get("currency")) else str(best.get("currency"))}


def metric_evidence(xbrl, facts: pd.DataFrame, period_key: str, row: dict) -> dict:
    """For each key metric ``row`` lacks: what the filing holds for it. Runs in ``extract.extract_filing``."""
    out = {}
    for metric in KEY_METRICS:
        if pd.notna(row.get(metric)):
            continue
        statement, standards, label, tags = EVIDENCE[metric]
        out[metric] = {"statement": statement, **_statement_evidence(xbrl, statement, standards, label, tags),
                       "fact": _fact_evidence(facts, period_key, tags)}
    return out


def _money(value: float, currency: str | None) -> str:
    return f"{value:,.0f}" + (f" {currency}" if currency else "")


def classify_missing(metric: str, evidence: dict | None) -> tuple[str, str, str, str | None]:
    """(kind, severity, detail, label) for a key metric with no value after cleaning.

    No evidence (an older extraction) proves nothing, so it stays a
    ``missing_metric``: only a searched filing can be ``not_reported``.
    """
    if not evidence:
        return ("missing_metric", "warning",
                "no tag or statement line was read, and the filing was not searched for one", None)
    where = STATEMENT_NAMES[evidence["statement"]]
    fact, line = evidence.get("fact"), evidence.get("line")
    if fact:
        span = f" over {fact['days']} days" if fact.get("days") else ""
        return ("missing_metric", "warning",
                f"the filing reports {fact['concept']} = {_money(fact['value'], fact.get('currency'))}{span} "
                f"to {fact['end'][:10]}, but no rule reads it", None)
    if line:
        shown = ", ".join(f"{column}: {value:,.0f}" for column, value in line["columns"].items()) or "no values"
        return ("missing_metric", "warning",
                f"the {where} has the line {line['concept']} but no value was read for this year ({shown})",
                line["label"])
    if evidence.get("state") == "failed":
        return ("statement_missing", "warning",
                f"the {where} could not be read ({evidence.get('error')}); no fact for the year either", None)
    if evidence.get("state") == "missing":
        return ("statement_missing", "warning",
                f"no {where} was found in this filing and no fact for the year, so this could not be read", None)
    return ("not_reported", "info",
            f"the {where} has no such line and the filing has no fact for this year", None)


# --------------------------------------------------------------------------- #
# All the issues of one company's run
# --------------------------------------------------------------------------- #

def implausible(row: dict) -> list[tuple[str, str]]:
    """Metrics of one year that contradict each other: (metric, why).

    The calculation checks test the filer's totals, so they cannot see that the
    pipeline picked the wrong fact: Applied Industrial's $21.8M of a note beside
    net sales of $2,460M passed every one of them.
    """
    revenue, cost, gross, operating = (row.get(metric) for metric in (
        "Revenues", "CostOfRevenue", "GrossProfit", "OperatingIncomeLoss"))
    if not revenue or pd.isna(revenue) or revenue <= 0:
        return []
    found = []
    if pd.notna(gross) and gross > revenue * 1.001:
        found.append(("GrossProfit", f"gross profit {gross:,.0f} is above revenue {revenue:,.0f}"))
    if pd.notna(operating) and operating > revenue * 1.001:
        found.append(("OperatingIncomeLoss", f"operating income {operating:,.0f} is above revenue {revenue:,.0f}"))
    if (pd.notna(gross) and pd.notna(cost) and not row.get("gross_profit_derived")
            and abs(revenue - cost - gross) > revenue * GROSS_TOLERANCE):
        found.append(("Revenues", f"revenue {revenue:,.0f} minus cost {cost:,.0f} is not gross profit {gross:,.0f}"))
    return found


def company_issues(derived: pd.DataFrame, checks: pd.DataFrame, filed: dict[str, dict]) -> list[dict]:
    """Every data issue of one company's run.

    ``derived`` is ``clean_and_derive``'s output, ``checks`` ``assemble``'s and
    ``filed`` period -> {form, accession, evidence} from its ``on_filing``.
    """
    issues: list[dict] = []
    for check in checks.to_dict("records") if checks is not None and not checks.empty else []:
        if check["status"] != calc_check.STATUS_MISMATCH:
            continue
        where = filed.get(check["period_of_report"], {})
        issues.append(_issue(
            "check_mismatch", "warning" if check["kind"] == "statement" else "info",
            accession=where.get("accession"), form=where.get("form"),
            fiscal_year=check.get("fiscal_year"), period=check["period_of_report"], statement=check["statement"],
            concept=check["concept"], label=check["label"], reported=check["reported"],
            computed=check["computed"], difference=check["difference"],
            detail=check.get("hint") or f"{check['parts']} parts; tolerance {check['tolerance']:g}"))
    for row in derived.to_dict("records"):
        where = filed.get(row["period_of_report"], {})
        common = {"accession": where.get("accession"), "form": where.get("form"),
                  "fiscal_year": row.get("fiscal_year"), "period": row["period_of_report"]}
        for metric in KEY_METRICS:
            if pd.notna(row.get(metric)):
                continue
            kind, severity, detail, label = classify_missing(metric, (where.get("evidence") or {}).get(metric))
            issues.append(_issue(kind, severity, concept=metric, label=label, detail=detail, **common))
        for metric, why in implausible(row):
            issues.append(_issue("implausible_value", "warning", concept=metric, detail=why, **common))
    return issues


# --------------------------------------------------------------------------- #
# Charts: the issues behind each chart
# --------------------------------------------------------------------------- #

def chart_warnings(concepts: set[str], years: list[int], issues: list[dict]) -> list[dict]:
    """The issues touching a chart drawn from ``concepts`` over ``years``, one entry per kind and metric.

    A metric issue counts when its metric is drawn; a ``check_mismatch`` when
    the total that does not add up is one of the chart's lines.
    """
    shown = {int(year) for year in years}
    grouped: dict[tuple, dict] = {}

    def text(value) -> str | None:              # rows read back from data_issues.csv carry NaN
        return None if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)

    for issue in issues:
        year = issue.get("fiscal_year")
        if year is None or pd.isna(year) or int(year) not in shown:
            continue
        concept = _tag(str(issue.get("concept") or ""))
        if concept not in concepts:
            continue
        key = (issue["kind"], concept)
        entry = grouped.setdefault(key, {
            "kind": issue["kind"], "severity": issue["severity"], "title": TITLES.get(issue["kind"], issue["kind"]),
            "concept": concept, "label": text(issue.get("label")) if issue["kind"] == "check_mismatch" else None,
            "years": [], "details": []})
        if issue["severity"] == "warning":
            entry["severity"] = "warning"
        entry["years"].append(int(year))
        entry["details"].append({"year": int(year), "detail": text(issue.get("detail")) or ""})
    out = []
    for entry in grouped.values():
        order = sorted(range(len(entry["years"])), key=lambda i: entry["years"][i])
        entry["years"] = [entry["years"][i] for i in order]
        entry["details"] = [entry["details"][i] for i in order]
        out.append(entry)
    return sorted(out, key=lambda e: (e["severity"] != "warning", e["kind"], e["concept"]))
