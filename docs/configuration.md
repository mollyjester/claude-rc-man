# Configuration

rcman reads its settings from environment variables. The installer writes
them to the service's environment file; change them there and restart the
service.

Each variable's default and allowed values are defined in the settings model,
`backend/rcman/settings.py`, which is the reference. rcman refuses to start
when a value is invalid.

| Variable | What it sets |
|---|---|
| `RCMAN_PUBLIC_ORIGIN` | The address browsers use to reach rcman — scheme, host and port, with no path, such as `https://rcman.example.com`. Required. Requests that change something are accepted only when they come from this origin |
| `RCMAN_USERS_FILE` | The list of people who may use rcman, managed with `rcman-user` (see [Adding and removing people](users.md)) |
| `RCMAN_DATABASE` | The SQLite database holding the audit trail and each person's settings |
| `RCMAN_HELPERS_DIR` | Where `rc-helper` and `rc-admin` are installed |
| `RCMAN_DOCS_DIR` | Where the documentation pages shown in rcman are |
| `RCMAN_AUDIT_RETENTION_DAYS` | How long audit events are kept (see [Audit trail](audit.md)) |
| `RCMAN_AUDIT_MAX_ROWS` | The most audit events kept at once (see [Audit trail](audit.md)) |
