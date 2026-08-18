#!/bin/zsh
# PhilForge, LOCAL and OFFLINE for the Fib Boundary page.
#
# Same app, same routes, same engine -- but the Fib Boundary routes read index
# candles from tools/.nifty_cache and premiums from the local Upstox option
# archive, so Backtest and past-mother paper Starts work with NO Dhan account.
# SAFETY: never mints a Dhan token (one per client; a local mint kills the live
# server's). PIN/TOTP are blanked and startup jobs skipped, exactly like
# tools/run_e2e_server.sh. Throwaway DB. Login: admin / 123456.
#
#     zsh tools/run_fib_offline_server.sh        # then open http://localhost:8769/app
export PHILFORGE_FIB_OFFLINE=1
export PHILFORGE_SKIP_STARTUP_JOBS=1
export PHILFORGE_STARTUP_TOKEN=0
export DHAN_PIN=
export DHAN_TOTP_SECRET=
export DHAN_ACCESS_TOKEN=offline-dummy
export PHILFORGE_DB="${PHILFORGE_DB:-/tmp/philforge-fib-offline/offline.db}"
export PHILFORGE_PIN=123456
export APP_PORT="${APP_PORT:-8769}"
mkdir -p "$(dirname "$PHILFORGE_DB")"
cd "$(dirname "$0")/.."
exec python3 app.py
