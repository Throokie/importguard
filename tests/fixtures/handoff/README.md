# Synthetic export fixtures

The fixed 11-row incoming case covers five account/currency groups, an excluded row, an invalid row, two distinct same-day purchases, unusual account names and per-currency totals. No personal data is included.

Expected counts, totals and selected source lines were fixed before the account-partitioning repair was observed. The two single-account golden CSVs come from the frozen 0.1 exporter and check byte compatibility of that path. They do not assert compatibility with any external ledger. The fixture hashes protect accidental edits; changing expectations requires explaining the business behavior change.
