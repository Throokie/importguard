# Possible next steps

These are candidate directions, without release dates or compatibility promises.

- Shipped in v0.3: English / Simplified Chinese switch, localized reasons/errors/exports, with preserved review state.
- More synthetic bank-format fixtures, contributed without personal information.
- Reproducible target-adapter harnesses that fix version, mappings, retry behavior and expectations.
- Explicit session import/export so a downloaded audit could restore review work.

An Actual adapter needs a stable-ID contract and a deliberate strategy for ambiguous ID-less reimports before being called supported. The current CSV preflight deliberately leaves those choices visible.
