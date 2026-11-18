# Search companies by name in the run bar

Date: 2026-11-18. Status: approved by the owner ("yep, that is great, i like suggestions. go on").

## Why
The run bar takes tickers only; the owner wants to find a company by its name too.

## Design (small change)
- **Suggestions while typing**: from 2 characters, up to 8 matches under the bar,
  `TGT · Target Corp · NYSE`. Order: exact ticker, ticker prefix, name starting
  with the text, a word of the name starting with it, name containing it. Case,
  punctuation and legal suffixes (Inc, Corp, …) are ignored. Arrows/Enter or a
  click put the ticker in the bar; with comma-separated tickers only the part
  being typed is searched and replaced.
- **Enter without picking** runs what was typed, as before: a name never starts a run.
- **Source**: SEC's `company_tickers_exchange.json` (every SEC filer with a
  ticker), downloaded by the server at most once a day with the signed-in
  account as the SEC contact (as runs do), kept in `data/company_tickers.json`
  (shared, atomic write). SEC unreachable: the last copy is used; no copy: no
  suggestions, and the list says names cannot be searched (never a silent empty list).
- `GET /api/companies?q=` → `{"items": [{ticker, name, exchange}], "available": bool}`.

## Approaches
1. SEC's list (chosen): exactly the companies a run can analyse.
2. Yahoo's search: also lists funds and foreign listings a run cannot analyse, needs a request per keystroke. Dropped.

## Not
No fuzzy/typo matching; no search in other inputs.
