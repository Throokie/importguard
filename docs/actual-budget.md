# Actual Budget: a tested boundary, not a compatibility claim

Tested on 2026-10-23 using official `@actual-app/api@26.10.0`, core 26.10.0, Node 24.21.0 and better-sqlite3 13.0.3. Both budgets were new, isolated, synthetic and USD-only. No real account, token, bank connection or server URL was provided.

The path used the official `transactions-parse-file` handler, explicit column mapping in a test adapter, the public `importTransactions` API and real local SQLite storage. Transactions were read back, budgets exported, and the closed databases checked read-only. This was more than a simulated receiver, but **the full desktop/Web GUI and its mapping dialog were not tested**. ImportGuard does not include a production Actual adapter.

| Case | Observed result |
| --- | --- |
| Two same-date, same-payee, same-amount purchases with distinct stable IDs explicitly mapped to `imported_id` | Both retained |
| Same stable ID used in different accounts | Account separation retained |
| Reimport a stable-ID batch | No extra transactions |
| Two matching ID-less rows first imported in one batch | Both retained |
| Reimport that entire ID-less batch | No extra transactions |
| Two separate genuine ID-less purchases imported in separate batches | Second ignored; final count 1 instead of the expected 2 |
| Explicitly include the second purchase in frozen ImportGuard 0.2, then import its real exported CSV | Same second-purchase omission reproduced |

Direct path: **15 of 16 assertions met**, one unmet, no execution errors. ImportGuard-export path: **11 of 12 met**, one unmet, no execution errors. They reproduced the same matching limitation; they are not two independent bugs or customer samples. A zero exit code indicated that result collection completed, not that every business condition passed.

Notes differed between the two ID-less purchases; that difference did not preserve the second purchase. The installed source matched imported ID within account context, then considered existing nearby-date/same-amount records and payee candidates. These observations do not establish a universal Actual defect: the target cannot reliably infer retry versus a distinct purchase from identical matching fields alone.

**Do not assume a CSV “Import ID” column is mapped by Actual's GUI.** The passing case explicitly assigned API `imported_id`; GUI support for the equivalent operation is unverified. Creating random IDs merely to force insertion can undermine safe reimport behavior and was not presented as a fix.

Two `cloudStorage.upload … unauthorized` warnings occurred during budget creation and were retained in the local test evidence. In the fixed source, missing user-token was rejected before packaging/upload; local parsing and storage continued. No full-process network capture was performed, so this is not a network-audit conclusion.

Other versions, currencies, bank formats, user rules, synced budgets, hosted services and real-user data remain untested. The source package does not bundle the Actual runtime, synthetic test budgets or internal working records. Future target integrations should provide a reproducible isolated harness before claiming compatibility.

Official references: [API package 26.10.0](https://www.npmjs.com/package/@actual-app/api/v/26.10.0), [API reference](https://actualbudget.org/docs/api/reference/), [file importing](https://actualbudget.org/docs/transactions/importing/). Current online docs do not replace evidence from the fixed tested version.
