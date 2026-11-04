# Notes for AI assistants working on Stocky

## Hiding is not fixing: report everything

Never make a problem disappear by renaming it, lowering its severity, filtering
it out, leaving it out of a count or not displaying it. A count goes down only
because the data is now read correctly, and a "fix" is shown with the numbers
from before and after (which issues went away, which moved to which kind).

This happened once: commit 91e7946 "fixed" 505 `missing_metric` issues mostly by
moving ~400 into a new `not_reported` kind (a greyed note, with no column in the
Market overview) that said "the filing's statements have no such line". The
filings showed that was false for ~180 of them (AAUAF's net loss was in the
facts; AACO's lines were there with an empty column). The test looked at what
our extraction managed to read, not at the filing.

- A claim about a filing ("not in the filing", "the filer's error") is checked
  against the stored filing (`filing_cache` dump: facts, statements,
  presentation), never against our own extraction. See `data_issues.py`.
- Every issue kind is counted in the UI. The Market tab has an "Other" column so
  a new kind cannot drop out of sight. A chart built on data with an open issue
  shows a warning under it (`charts.attach_warnings`).
- If something can't be fixed, leave it reported as open and say so.

## Also

- No change may force regenerating everything: stage 1 is only `dump.py`;
  everything that picks or computes runs on the dump (stage 2). Editing
  `dump.py` creates a new reader that must be classified in `readers.json`.
