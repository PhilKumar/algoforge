#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  AlgoForge Staging Deploy — feature/multi-tenant
#  Pulls latest branch code into the dedicated staging worktree,
#  migrates SQLite, restarts the staging service, and health-checks.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

STAGING_DIR="/home/ec2-user/algoforge-staging"
BRANCH="${BRANCH:-feature/multi-tenant}"
SERVICE_NAME="algoforge-staging"
STAGING_PORT=8002

log() { echo "[STAGING-DEPLOY] $(date '+%H:%M:%S') $*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -d "$STAGING_DIR/.git" ]] || die "Staging worktree not found at $STAGING_DIR"

log "Updating staging worktree..."
git -C "$STAGING_DIR" fetch origin
git -C "$STAGING_DIR" checkout "$BRANCH"
git -C "$STAGING_DIR" pull --ff-only origin "$BRANCH"

log "Installing dependencies..."
source "$STAGING_DIR/venv/bin/activate"
pip install -q --disable-pip-version-check -r "$STAGING_DIR/requirements.txt"

log "Running migration..."
set -a
source "$STAGING_DIR/.env.staging"
set +a
python3 "$STAGING_DIR/scripts/migrate_to_sqlite.py"

log "Restarting $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"
sleep 3

log "Health checking..."
curl -sf "http://127.0.0.1:${STAGING_PORT}/api/health" >/dev/null || die "Health check failed on port ${STAGING_PORT}"
log "Staging deploy complete."
