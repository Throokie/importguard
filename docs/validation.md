# Validation scope

This is an early-stage, locally operated tool. Tests use synthetic data. Passing checks do not establish real-bank compatibility, customer acceptance, or an independent security audit.

Publication candidate, 2026-10-23: **73/73 local tests passed** on Linux / Python 3.14.8. This includes the existing 50 logic/HTTP tests 12 account-export checks, and 11 language/catalog/thread-isolation checks. The bilingual browser scenario passed 23 checks including in-place switching, saved and blocked preferences, both ZIP downloads, nested error translation and 390px layouts.

## Local application

- The packaged Python logic and HTTP suite is runnable with `python3 -m unittest discover -s tests -v`.
- Account-separated exports were checked against a fixed 11-row input, five account/currency groups, selected-line conservation, per-currency totals, path safety and single-account compatibility.
- A real Chromium browser loaded the sample CSVs, ran the checks, changed row decisions and downloaded an actual ZIP. The download was opened and checked; the demo images are from that UI.
- Version 0.3 screenshots show both language interfaces. The retained v0.2 README GIF is an edited sequence of real screenshots, held long enough to read. It does not demonstrate real-time processing speed.
- A narrow 390px viewport was inspected. Wide results tables support horizontal scrolling.

The built-in scenario has 6 historical and 11 incoming rows: 5 new, 1 duplicate, 4 review, 1 invalid. Exclude source line 3 and include lines 6/7 to reach 7 included, 2 excluded, 2 pending, split across three account/currency groups.

## Target ledger

Actual 26.10.0 was tested only through its official parser/API and real isolated SQLite databases, with explicit test-adapter mapping. Direct checks met 15/16 assertions; the ImportGuard-export path met 11/12. Both reproduced the same ID-less second-import limitation. [Full scope](actual-budget.md).

## Unverified

- Actual's full GUI and CSV mapping dialog, Sure, QBO, other target applications or versions.
- Real bank formats, user rules, bank sync, non-USD target tests and real customer data.
- Windows/macOS launchers by manual operation; local validation was Linux.
- Independent security review, network capture, memory forensics and customer/payment validation.

GitHub Actions is configured to run the Python suite on push and pull requests. A configured workflow is not a claim that its remote runs have passed; consult the actual run for its commit and result.
