"""The quarters since the latest annual report: TTM, three-month and quarter-end values (stage 2).

A run reads the 10-Qs whose period ends after the latest 10-K (0-3 of them,
more only when the 10-K is overdue). Each is read twice by the same rules as
an annual report (``extract.extract_filing``): at its own date over its year
to date (YTD), and at the same date a year earlier (the 10-Q's comparative
columns). From those:

- TTM = the latest fiscal year + YTD - the prior year's YTD, per flow metric;
- three months = YTD - the previous 10-Q's YTD of the same fiscal year
  (Q1: its YTD); cash flows are YTD only in a 10-Q, so this is the only way;
- balance-sheet values = the 10-Q's own, at its date.

Every gap is an issue (hiding is not fixing): a 10-Q not read, a metric the
year has but the quarter does not, a metric read from another tag than the
year's (TTM would mix two measures), prior-year-end balances that differ from
the 10-K's (restated), and a cover-page quarter that disagrees with the dates.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import numpy as np
import pandas as pd

import data_issues
import extract
import filing_view

USD_TO_MILLIONS = 1_000_000
QUARTER_DAYS = 91.31          # a quarter of a 365.25-day year
SPAN_TOLERANCE = 20           # days either side of the cover page's span
PRIOR_YEAR_DAYS = (358, 372)  # a year earlier, on 52/53-week calendars too
RESTATED_TOLERANCE = 0.005    # prior year-end balances differing by more than this
RESTATED_METRICS = ("AssetsCurrent", "LiabilitiesCurrent", "LongTermDebt", "StockholdersEquity")

DURATIONS = [metric for metric, (_, kind) in extract.METRIC_CONCEPTS.items() if kind == "duration"]
INSTANTS = [metric for metric, (_, kind) in extract.METRIC_CONCEPTS.items() if kind == "instant"]


def _issue(kind: str, severity: str, slot, period: str, accession, **fields) -> dict:
    return data_issues._issue(kind, severity, fiscal_year=slot, period=period, form="10-Q",
                              accession=accession, **fields)


def _millions(value) -> float:
    value = pd.to_numeric(value, errors="coerce")
    return float(value) / USD_TO_MILLIONS if pd.notna(value) else np.nan


def _cover_span(facts: pd.DataFrame, period_end: pd.Timestamp) -> int | None:
    """Days the 10-Q's cover page says its year to date spans; else its longest sub-year duration."""
    start = pd.to_datetime(facts.get("period_start"), errors="coerce")
    end = pd.to_datetime(facts.get("period_end"), errors="coerce")
    near = (end - period_end).abs() <= pd.Timedelta(days=extract.PERIOD_MATCH_DAYS)
    spans = (end - start).dt.days
    cover = facts["concept"].isin(["dei:DocumentPeriodEndDate", "dei:DocumentType"]) & near
    found = spans[cover].dropna()
    if found.empty:
        found = spans[near & ~facts["is_dimensioned"].astype(bool) & (spans < extract.ANNUAL_MIN_DAYS)].dropna()
    return int(found.max()) if not found.empty else None


def _prior_date(facts: pd.DataFrame, period_end: pd.Timestamp, min_days: int, max_days: int) -> str | None:
    """The end of the 10-Q's prior-year comparative: same span, about a year earlier."""
    start = pd.to_datetime(facts.get("period_start"), errors="coerce")
    end = pd.to_datetime(facts.get("period_end"), errors="coerce")
    spans = (end - start).dt.days
    gap = (period_end - end).dt.days
    match = (spans >= min_days) & (spans <= max_days) & (gap >= PRIOR_YEAR_DAYS[0]) & (gap <= PRIOR_YEAR_DAYS[1])
    ends = end[match].dropna()
    if ends.empty:
        return None
    return ends.value_counts().index[0].strftime("%Y-%m-%d")


def _read(view, period: str, min_days: int, max_days: int) -> dict:
    """``extract_filing`` at one date and span, quietly: its notes for the prior year are expected."""
    with redirect_stdout(io.StringIO()):
        return extract.extract_filing(view, period, min_days=min_days, max_days=max_days)


def _restated(facts: pd.DataFrame, fy: pd.Series, fy_end: str, currency, slot, period, accession) -> list[dict]:
    """Prior year-end balances in the 10-Q that differ from the 10-K's."""
    issues = []
    for metric in RESTATED_METRICS:
        before = pd.to_numeric(fy.get(metric), errors="coerce")
        if pd.isna(before):
            continue
        concepts = extract.METRIC_CONCEPTS[metric][0]
        now = _millions(extract.extract_metric(facts, concepts, fy_end, "instant", currency=currency))
        if np.isnan(now) or abs(now - before) <= abs(before) * RESTATED_TOLERANCE:
            continue
        issues.append(_issue("quarter_restated", "info", slot, period, accession, concept=metric,
                             reported=float(before), computed=float(now), difference=float(now - before),
                             detail=f"the 10-Q shows {now:,.1f} M at the fiscal year end {fy_end}, "
                                    f"the 10-K showed {before:,.1f} M"))
    return issues


def build(annual: pd.DataFrame, annual_sources: dict, entries: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    """One row per 10-Q after the latest fiscal year in ``annual`` (financial_data.csv, millions).

    ``annual_sources`` is the latest fiscal year's ``extract_filing(...)["sources"]``;
    ``entries`` the 10-Qs' dumps, oldest first. Returns the rows and their issues.
    """
    if annual is None or annual.empty or not entries:
        return pd.DataFrame(), []
    import edgar_dashboard     # its run imports this module

    fy = annual.sort_values("period_of_report").iloc[-1]
    fy_end = str(fy["period_of_report"])[:10]
    fy_date = pd.Timestamp(fy_end)
    fy_year = int(fy["fiscal_year"])
    rows: list[dict] = []
    issues: list[dict] = []
    previous: dict | None = None

    for entry in entries:
        meta = entry.get("filing") or {}
        accession = meta.get("accession")
        index_date = str(meta.get("report_date") or "")[:10]
        if not entry.get("xbrl") or not entry.get("facts"):
            issues.append(_issue("quarter_unread", "warning", None, index_date, accession,
                                 detail="the 10-Q has no XBRL in the dump, so nothing was read from it"))
            continue
        try:
            view = filing_view.FilingView(entry)
            facts = view.xbrl().facts.to_dataframe()
            period = edgar_dashboard.filing_period(entry, index_date)
            period_end = pd.Timestamp(period)
            span = _cover_span(facts, period_end)
            if span is None:
                raise ValueError("no year-to-date period on its cover page or in its facts")
            min_days, max_days = span - SPAN_TOLERANCE, span + SPAN_TOLERANCE
            ytd = _read(view, period, min_days, max_days)
            prior_date = _prior_date(facts, period_end, min_days, max_days)
            prior = _read(view, prior_date, min_days, max_days) if prior_date else None
        except Exception as exc:  # noqa: BLE001 - one 10-Q never sinks the run; it is reported
            issues.append(_issue("quarter_unread", "warning", None, index_date, accession,
                                 detail=f"the 10-Q could not be read ({exc.__class__.__name__}: {exc})"))
            continue

        # Which quarter: from the dates, checked against the cover page.
        steps = max(1, round((period_end - fy_date).days / QUARTER_DAYS))
        quarter = (steps - 1) % 4 + 1
        year = fy_year + 1 + (steps - 1) // 4
        slot = f"Q{quarter}'{year % 100:02d}"
        stated = str((entry.get("dei") or {}).get("dei:DocumentFiscalPeriodFocus") or "").upper()
        if stated and stated != f"Q{quarter}":
            issues.append(_issue("quarter_label_mismatch", "info", slot, period, accession,
                                 detail=f"the cover page says {stated}, the dates say Q{quarter} "
                                        f"({(period_end - fy_date).days} days after the fiscal year end {fy_end}); "
                                        f"Q{quarter} is used"))
        overdue = steps > 3

        currency = ytd["row"].get("currency")
        out = {"slot": slot, "label": f"TTM {slot}", "period_end": period, "fiscal_year": year,
               "quarter": quarter, "accession": accession, "period_of_report": period,
               "form": "10-Q", "currency": currency}
        same_year = previous is not None and previous["fiscal_year"] == year and previous["quarter"] == quarter - 1

        def missing(metric: str, detail: str, severity: str = "warning") -> None:
            issues.append(_issue("quarter_metric_missing", severity, slot, period, accession,
                                 concept=metric, detail=detail))

        for metric in DURATIONS:
            year_value = pd.to_numeric(fy.get(metric), errors="coerce")
            now = _millions(ytd["row"].get(metric))
            before = _millions(prior["row"].get(metric)) if prior else np.nan
            out[f"{metric}__ytd"] = now
            out[f"{metric}__ytd_prior"] = before
            out[f"{metric}__source"] = ytd["sources"].get(metric)
            if quarter == 1:
                out[f"{metric}__q"] = now
            elif same_year:
                out[f"{metric}__q"] = now - previous[f"{metric}__ytd"]
            else:
                out[f"{metric}__q"] = np.nan
            if overdue:
                out[metric] = np.nan
                if pd.notna(year_value):
                    missing(metric, f"no TTM: this 10-Q is {steps} quarters after the latest 10-K ({fy_end}), "
                                    "which is overdue, so its year to date does not follow that fiscal year")
                continue
            out[metric] = year_value + now - before if pd.notna(year_value) else np.nan
            if pd.isna(year_value):
                continue                   # the fiscal year's own issue covers it
            if np.isnan(now):
                evidence = (ytd.get("evidence") or {}).get(metric)
                why = data_issues.classify_missing(metric, evidence)[2] if evidence else \
                    "no tag or statement line was read for the year to date"
                missing(metric, f"the fiscal year has it but this 10-Q does not: {why}")
            elif np.isnan(before):
                missing(metric, "the 10-Q has the year to date but not the same months a year earlier"
                        + ("" if prior_date else " (no prior-year comparative period was found in it)"))
            if quarter > 1 and not same_year and pd.notna(now):
                missing(metric, "no three-month value: it needs the previous 10-Q of the year, which was not read",
                        "info")
            _compare_sources(issues, metric, annual_sources.get(metric), ytd["sources"].get(metric),
                             prior["sources"].get(metric) if prior else None, slot, period, accession)

        for metric in INSTANTS:
            value = _millions(ytd["row"].get(metric))
            out[metric] = value
            out[f"{metric}__source"] = ytd["sources"].get(metric)
            if np.isnan(value) and pd.notna(pd.to_numeric(fy.get(metric), errors="coerce")):
                missing(metric, f"the fiscal year has it but this 10-Q's balance sheet at {period} does not")

        issues.extend(_restated(facts, fy, fy_end, currency, slot, period, accession))
        rows.append(out)
        previous = out

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = edgar_dashboard.derive(frame)
    return frame, issues


def _compare_sources(issues: list, metric: str, year: str | None, now: str | None, before: str | None,
                     slot, period, accession) -> None:
    """A TTM made of values read from different tags measures two things: say so."""
    seen = {"fiscal year": year, "year to date": now, "year earlier": before}
    named = {where: source for where, source in seen.items() if source}
    if len(set(named.values())) > 1:
        issues.append(_issue("quarter_concept_differs", "warning", slot, period, accession, concept=metric,
                             detail="the TTM adds values read from different tags: "
                                    + ", ".join(f"{where} {source}" for where, source in named.items())))
