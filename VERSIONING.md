# Version isolation

- `main`: repository landing page until a release is reviewed and merged.
- `release/v0.4.0`: isolated first public source snapshot, local TOTP authentication edition.
- Future work: create `feature/<topic>` branches and submit pull requests into the chosen release/main branch.
- Do not force-push shared branches or mix private runtime data with source history.
- This release is a sanitized export of the private development workspace. Personal reports, statements, real financial test expectations and local databases are excluded. Fresh installs start with empty platform sources.
- Archived email code and migrations are retained for compatibility; do not mount legacy email routes without adapting them to current 2FA.
- Each deployment needs its own `.env`, authentication keys and databases. Branch isolation alone does not isolate running databases.
- Do not share `var/private` between deployments; do not run different release versions against one database without a backup and compatible migration plan.
