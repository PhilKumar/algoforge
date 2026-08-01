#!/bin/zsh
# Isolated PhilForge for local e2e (Playwright) runs.
#
# SAFETY: never let a local instance mint a Dhan token. Dhan keeps ONE active
# token per client id, so a local mint silently kills the token the LIVE
# server is trading with (learned the hard way, 2026-08-01). Startup jobs are
# skipped and the PIN/TOTP secrets are blanked so AUTO_TOKEN stays off.
export PHILFORGE_SKIP_STARTUP_JOBS=1
export PHILFORGE_STARTUP_TOKEN=0
export DHAN_PIN=
export DHAN_TOTP_SECRET=
export DHAN_ACCESS_TOKEN=e2e-dummy
export PHILFORGE_DB=/tmp/philforge-e2e/e2e.db
export PHILFORGE_PIN=123456
export APP_PORT="${APP_PORT:-8765}"
mkdir -p /tmp/philforge-e2e
cd "$(dirname "$0")/.."
exec python3 app.py
