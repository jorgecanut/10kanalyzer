"""What kind of company a filer is, and whether Stocky can analyse it (one rule for the app and the scan).

From SEC's submissions data: the industry code (SIC), the entity type and the
forms the company files. A company is **not analysable** (``warn``) when it
files no 10-K, 20-F or 40-F (a fund's holdings reports, or none), or when its
industry is one whose statements Stocky's ratios do not fit: banks and credit,
brokers and asset managers, insurers, investment and holding offices, REITs and
blank check companies. The run bar shows those in red; the market scan skips
them before downloading anything, and lists them as skipped with the reason.
"""

from __future__ import annotations

import re

ANNUAL_REPORT_FORMS = {"10-K", "20-F", "40-F", "10-K405", "10-KT"}
FUND_FORMS = re.compile(r"^(N-|NPORT|485|497)")
# SIC ranges whose statements the ratios do not fit, most specific first.
NOT_ANALYSABLE_SIC = (
    ((6798, 6798), "REIT"),
    ((6770, 6770), "Blank check (SPAC)"),
    ((6000, 6199), "Bank"),
    ((6200, 6299), "Broker / asset manager"),
    ((6300, 6499), "Insurance"),
    ((6700, 6799), "Investment / holding office"),
)
# Bumped whenever the rule changes: answers kept under another rule are asked again.
RULE = 2
# Forms looked at: the newest filings are enough to see whether annual reports are filed.
RECENT_FORMS = 1000


def classify(submissions: dict) -> dict:
    """``{"category", "warn", "sic"}`` from a submissions record (SEC's JSON shape).

    The industry decides first. "No annual report" is said only when the forms
    listed are all the company's: SEC's recent page holds the newest 1,000 or
    so, and a filer of many prospectuses (JPM) has its 10-Ks on older pages
    (``filings.files``); an incomplete list proves nothing.
    """
    sic_text = str(submissions.get("sic") or "").strip()
    description = str(submissions.get("sicDescription") or "").strip()
    filings = submissions.get("filings") or {}
    forms = [str(form) for form in (filings.get("recent") or {}).get("form") or []]
    complete = not filings.get("files") and len(forms) < RECENT_FORMS
    sic = int(sic_text) if sic_text.isdigit() else None
    for (low, high), label in NOT_ANALYSABLE_SIC:
        if sic is not None and low <= sic <= high:
            return {"category": label, "warn": True, "sic": sic, "rule": RULE}
    if complete and not any(form in ANNUAL_REPORT_FORMS for form in forms):
        fund = any(FUND_FORMS.match(form) for form in forms) or \
            str(submissions.get("entityType") or "").lower() not in ("", "operating")
        return {"category": "Fund: no company accounts" if fund else "No 10-K, 20-F or 40-F", "warn": True,
                "sic": sic, "rule": RULE}
    return {"category": description or "Industry unknown", "warn": False, "sic": sic, "rule": RULE}


def from_company(company) -> dict:
    """``classify`` for an edgartools ``Company`` (its data is loaded already: no further request).

    Whether it files annual reports is asked of its whole filing history
    (``get_filings(form=...)``), so the list is complete; when that cannot be
    read, the company is judged on its industry alone.
    """
    data = getattr(company, "data", None)
    record = {"sic": getattr(data, "sic", None), "sicDescription": getattr(data, "sic_description", None),
              "entityType": getattr(data, "entity_type", None), "filings": {"recent": {"form": []}}}
    try:
        annual = [form for form in sorted(ANNUAL_REPORT_FORMS) if len(company.get_filings(form=form)) > 0]
        forms = annual
        if not annual:
            history = company.get_filings()
            table = getattr(history, "data", None)
            if table is not None and "form" in getattr(table, "column_names", []):
                forms = [str(form) for form in table.column("form").to_pylist()[:RECENT_FORMS - 1]]
            else:
                forms = [str(filing.form) for _, filing in zip(range(RECENT_FORMS - 1), history)]
        record["filings"]["recent"]["form"] = forms
    except Exception:  # noqa: BLE001 - the history could not be read: the industry alone decides
        record["filings"]["files"] = [{"name": "unread"}]
    return classify(record)
