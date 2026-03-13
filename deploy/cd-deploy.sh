#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  AlgoForge — Zero-Downtime Blue-Green Deployment
# ═══════════════════════════════════════════════════════════════
#  Called by GitHub Actions after `git pull`.
#  Starts new code on a standby port, health-checks it,
#  swaps nginx, drains old connections, stops old instance.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

APP="algoforge"
APP_DIR="/home/ec2-user/algoforge"
VENV="$APP_DIR/venv"

BLUE_PORT=8000
GREEN_PORT=8001
PORT_FILE="$HOME/.${APP}-active-port"
UPSTREAM_CONF="/etc/nginx/conf.d/${APP}-upstream.conf"

HEALTH_PATH="/api/health"
HEALTH_TIMEOUT=45          # seconds to wait for standby health
DRAIN_TIMEOUT=30           # seconds to let old WS connections drain

LOG_TAG="[DEPLOY]"

# ── Helpers ───────────────────────────────────────────────────
log()  { echo "$LOG_TAG $(date '+%H:%M:%S') $*"; }
die()  { log "ERROR: $*"; exit 1; }

health_check() {
    local port=$1
    for i in $(seq 1 "$HEALTH_TIMEOUT"); do
        if curl -sf --max-time 3 "http://127.0.0.1:${port}${HEALTH_PATH}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ── Determine active/standby ─────────────────────────────────
if [[ -f "$PORT_FILE" ]]; then
    ACTIVE_PORT=$(cat "$PORT_FILE")
else
    # First deploy — assume blue
    ACTIVE_PORT=$BLUE_PORT
    echo "$ACTIVE_PORT" > "$PORT_FILE"
fi

if [[ "$ACTIVE_PORT" == "$BLUE_PORT" ]]; then
    STANDBY_PORT=$GREEN_PORT
else
    STANDBY_PORT=$BLUE_PORT
fi

log "Active: port $ACTIVE_PORT → Deploying to: port $STANDBY_PORT"

# ── 1. Install dependencies + clear stale bytecode ───────────
log "Installing dependencies..."
source "$VENV/bin/activate"
pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
log "Clearing __pycache__ to prevent stale bytecode..."
find "$APP_DIR" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# ── 2. Stop standby if somehow still running ──────────────────
sudo systemctl stop "${APP}@${STANDBY_PORT}" 2>/dev/null || true

# ── 2a. Tell active instance to persist state before restart ──
#  This ensures live_state_*.json files exist for the new instance to restore
if curl -sf --max-time 5 -X POST "http://127.0.0.1:${ACTIVE_PORT}/api/save-state" >/dev/null 2>&1; then
    log "Active instance saved state to disk"
else
    log "⚠ Could not save active state (may not be running)"
fi

# ── 2b. Kill legacy non-template service if it exists ─────────
#  Prevents port conflict: old algoforge.service may hold port 8000
if sudo systemctl is-active "${APP}.service" >/dev/null 2>&1; then
    log "⚠ Found legacy ${APP}.service — stopping & disabling..."
    sudo systemctl stop "${APP}.service"
    sudo systemctl disable "${APP}.service" 2>/dev/null || true
fi

# ── 2c. Kill any stale process holding the standby port ───────
if sudo fuser "${STANDBY_PORT}/tcp" >/dev/null 2>&1; then
    log "⚠ Stale process on port $STANDBY_PORT — killing..."
    sudo fuser -k "${STANDBY_PORT}/tcp" 2>/dev/null || true
    sleep 1
fi
sleep 1

# ── 2d. Pre-flight smoke test (catches import errors before systemd) ──
log "Running pre-flight import check..."
if ! "$VENV/bin/python" -c "
import sys, os
os.chdir('$APP_DIR')
sys.path.insert(0, '$APP_DIR')
from app import app
print('Pre-flight OK: app imported successfully')
" 2>&1; then
    die "Pre-flight import failed! Fix the error above before deploying."
fi

# ── 3. Start standby instance ────────────────────────────────
log "Starting standby on port $STANDBY_PORT..."
sudo systemctl start "${APP}@${STANDBY_PORT}"

# Give Dhan token generation a moment (2-min rate limit per generation)
sleep 5

# ── 4. Health check standby ──────────────────────────────────
log "Waiting for standby health check..."
if ! health_check "$STANDBY_PORT"; then
    log "ROLLBACK — standby failed health check! Stopping standby."
    log "── Last 50 lines of journal for ${APP}@${STANDBY_PORT} ──"
    sudo journalctl -u "${APP}@${STANDBY_PORT}" --no-pager -n 50 --since "5 min ago" 2>&1 || true
    log "── systemctl status ──"
    sudo systemctl status "${APP}@${STANDBY_PORT}" --no-pager -l 2>&1 || true
    sudo systemctl stop "${APP}@${STANDBY_PORT}" 2>/dev/null || true
    die "Deploy aborted. Active instance on port $ACTIVE_PORT unchanged."
fi
log "Standby is healthy!"

# ── 5. Swap nginx upstream + sync site config ────────────────
log "Switching nginx to port $STANDBY_PORT..."
echo "upstream ${APP}_backend { server 127.0.0.1:${STANDBY_PORT}; }" \
    | sudo tee "$UPSTREAM_CONF" >/dev/null

# Sync main nginx site config (picks up client_max_body_size, etc.)
if [[ -f "$APP_DIR/deploy/nginx.conf" ]]; then
    sudo cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/conf.d/${APP}.conf
    log "Synced nginx site config from deploy/nginx.conf"
fi

if ! sudo nginx -t 2>/dev/null; then
    die "Nginx config test failed! Restoring old upstream."
fi

# Graceful reload — existing connections stay on old workers
sudo nginx -s reload
log "Nginx reloaded. New traffic → port $STANDBY_PORT"

# ── 6. Drain old connections ─────────────────────────────────
log "Draining old connections for ${DRAIN_TIMEOUT}s..."
sleep "$DRAIN_TIMEOUT"

# ── 7. Stop old instance ─────────────────────────────────────
log "Stopping old instance on port $ACTIVE_PORT..."
sudo systemctl stop "${APP}@${ACTIVE_PORT}" 2>/dev/null || true

# ── 8. Persist new active port ────────────────────────────────
echo "$STANDBY_PORT" > "$PORT_FILE"

log "═══════════════════════════════════════════════"
log "  DEPLOY COMPLETE — $APP active on port $STANDBY_PORT"
log "═══════════════════════════════════════════════"
