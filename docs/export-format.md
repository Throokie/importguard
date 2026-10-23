# Export format

The review ZIP is an import aid, not an instruction to bypass destination validation. Use the destination's preview, then verify final counts and totals. [Actual Budget's observed second-import limitation](actual-budget.md) also applies when a row was explicitly included here.

## Language

Version 0.3 uses the selected interface language for `README.txt`, review reasons and manifest/audit warnings. CLI exports default to English (`--language zh-CN` selects Chinese). Structured records include the chosen `language` plus stable message codes and parameters where appropriate. All import CSV headers, original transaction values and numerical amounts remain unchanged by language selection; `review.csv` reasons are presentation text and do change.

## Layout version 2

`import-manifest.json` is the entry point. A single account/currency group has root-level `ready_rows.csv` and `normalized_rows.csv`; multiple groups have `accounts/gNNN_<hash>_<currency>/` directories. Account names never directly form export paths. Case and whitespace normalize, so `Main` and ` main ` share an account key. Different punctuation remains distinct.

Each manifest group supplies its normalized account key, displayed account names/aliases, currency, row count, signed amount total, physical source lines, and the paths of the two alternative CSV formats. Import each group into its corresponding destination account.

| File | Purpose |
| --- | --- |
| `ready_rows.csv` | Incoming columns and original values, UTF-8 BOM, comma-separated, with formula-like text escaped |
| `normalized_rows.csv` | Alternative standard columns, ISO dates and signed dot-decimal amounts |
| `all_selected_rows_REVIEW_ONLY.csv` | Multi-group combined summary; never import into one account |
| `all_normalized_rows_REVIEW_ONLY.csv` | Normalized combined review summary |
| `review.csv` | Non-included rows, reason, decision, displayed candidate lines and original JSON |
| `audit.json` | All incoming rows, input SHA-256, settings, default/manual decisions and formula-escape counts |
| `README.txt` | Counts and per-export usage limits |

Choose either ready or normalized CSV for each group; importing both duplicates the selected data. Pending and invalid rows do not enter import files. Excluding all selected rows for a group removes that group's files. With no selected rows, root CSVs contain headers only.

## Source lines and escaping

Decision keys refer to physical incoming CSV line numbers. A quoted multiline record uses its ending physical line. All selected source lines should appear exactly once across the group manifest entries.

Potential spreadsheet formulas in text are prefixed with a single quote. Legal signed amount values remain numeric. Audits preserve source values, but the exported text may differ; check the destination preview. The review ZIP itself contains financial data and should be handled accordingly.

No matching between splits/combined transactions or currencies is performed. The manifest's totals describe selected source amounts, not successful transactions in an external ledger.
