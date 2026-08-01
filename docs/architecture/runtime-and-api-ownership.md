# PhilForge Runtime And API Ownership

This register records the control-plane boundaries established during the
Phase 1 architecture remediation. It is intentionally narrower than the full
OpenAPI document: it calls out stateful trading families, their owners, and
the legacy surfaces that must not be mistaken for current product flows.

## Runtime ownership

| Family | Runtime owner | Durable state | Status semantics | Stop owner |
|---|---|---|---|---|
| Main paper/live strategy | authenticated user + active worker | SQLite/user engine state | read-only observation | user stop and admin emergency stop |
| Scalp | authenticated user + active worker | SQLite trades + per-user engine state | read-only observation | user stop/kill-all and admin emergency stop |
| Options Cascade (standard) | authenticated user + active worker | per-user engine state | read-only; no restore on GET | user stop/kill and admin emergency stop |
| Candle Entry Cascade | authenticated user + active worker | full campaign snapshot | read-only; no restore on GET | user kill and admin emergency stop |
| Fib Boundary Cascade | authenticated user + active worker | full campaign snapshot + SQLite backtest runs | read-only; no restore on GET | user kill and admin emergency stop |
| Terminal Cascade | authenticated user + active worker | per-user engine state + closed archive | read-only; no restore on GET | user stop/kill and admin emergency stop |

Only the active blue-green worker may restore or start background monitoring.
`GET /api/engine-control/status` is the canonical non-mutating summary used by
the header kill-switch. `POST /api/save-state`, startup recovery, explicit
start/restore actions, and shutdown hooks own persistence transitions.

## Current product routes

- `/api/scalp/*`: Scalp entry, management, exits, stop and kill-all.
- `/api/candle-entry/paper/*`: Candle Entry paper campaign.
- `/api/fib-boundary/paper/*`: Fib Boundary paper campaign.
- `/api/fib-boundary/backtest` and `/api/fib-boundary/backtests/*`: historical
  replays, durable run history, and JSON/CSV downloads.
- `/api/options/archive` and `/api/options/archive/export.csv`: inventory and
  exact-contract raw minute-bar export from the canonical option archive.
- `/api/terminal/*`: manual order/GTT/Forever/scanner surfaces.
- `/api/terminal/cascade/*`: Terminal Cascade runtime and archived runs.
- `/api/admin/engines` and `/api/emergency-stop`: complete administrator
  visibility and stop coverage across every runtime family.

## Compatibility and retirement register

| Surface | Classification | Current consumer | Treatment |
|---|---|---|---|
| `POST /api/cascade/backtest` | deprecated replay route | no current UI | retain temporarily for compatibility; remove only after request telemetry confirms zero use |
| `/api/cascade/paper/*` | dormant standard Cascade runtime | startup/recovery compatibility and tests | keep explicit, side-effect-free status; do not advertise as a current UI workflow |
| CPR chart-type controls formerly in Strategy Builder | orphaned UI | none | removed; there was no calculation or backend implementation to honor the selection |

## Data ownership and classification

- SQLite: user accounts, sessions, encrypted broker credentials, strategies,
  runs, journals, trade history, and Fib backtest payloads. Sensitive/private.
- Per-user root: charts, uploads, engine snapshots, and recovery state.
  Sensitive/private and user-owned.
- Option archive: shared market OHLC keyed by provider, underlying, expiry,
  strike and option type. Market data; never contains credentials or orders.
- Browser cache: versioned public static assets only. Authenticated HTML and
  API responses are never cached by the service worker.
- Backups: database + per-user data + option archive, checksum sidecar, local
  restore verification, and optional encrypted S3 replication.

Runtime data belongs under `/home/ec2-user/.local/share/philforge`, outside the
Git checkout. Source paths are configured only through `.env`; credentials and
secret values must never be written to logs or audit reports.

## Remaining architectural debt

`app.py` remains a large integration module. New durable option storage and
backup/migration behavior were extracted into bounded modules/scripts, but a
full controller/service split is intentionally deferred: performing that
rewrite together with live-trading reliability fixes would create unnecessary
regression risk. Future extraction should proceed one route family at a time
behind the existing API contracts and tests.
