# Contributing

Small, well-explained fixes and synthetic format examples are welcome.

1. Describe the user-visible problem and expected behavior.
2. Replace names, accounts, identifiers and transactions with synthetic data. Never attach real statements, passwords, cookies or API keys.
3. Keep runtime dependencies in Python's standard library unless there is a clear, discussed reason to add one.
4. Add a regression case for a parsing, matching or export behavior change. Keep ambiguous matches reviewable and account/currency boundaries explicit.
5. Run `python3 -m unittest discover -s tests -v` from the repository root. For UI changes, also walk through sample load, mapping, review, decisions and ZIP download in a browser.

Pull requests should explain what changes, why, what was tested, and any remaining limits. Do not claim a target application is supported from a mock receiver or a single passing import. Include version, actual import path, column mappings, reimport behavior and failures.

The documentation uses the same synthetic examples shipped in `examples/`. Demo assets are edited from the real UI; visual annotations must not invent product states or hide known limits.
