#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  One-time server setup for Blue-Green CI/CD
#  Run this ONCE on the EC2 server to migrate from the old
#  single-service model to template-based blue-green deploys.
#
#  Usage: bash deploy/setup-cicd.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

echo "╔══════════════════════════════════════════════╗"
echo "║   CI/CD Blue-Green Setup — PhilForge         ║"
echo "╚══════════════════════════════════════════════╝"

APP_DIR="/home/ec2-user/philforge"
BLUE_PORT=8000
SYNC_SITE_CONFIG="${SYNC_SITE_CONFIG:-0}"  # set to 1 only when intentionally replacing the server vhost
SITE_CONF="/etc/nginx/conf.d/philforge.conf"

# ── 1. Install systemd template service ──────────────────────
echo "==> Installing philforge@.service template..."
sudo cp "$APP_DIR/deploy/philforge.service" /etc/systemd/system/philforge@.service
echo "==> Installing backup service + timer..."
sudo cp "$APP_DIR/deploy/philforge-backup.service" /etc/systemd/system/philforge-backup.service
sudo cp "$APP_DIR/deploy/philforge-backup.timer" /etc/systemd/system/philforge-backup.timer
sudo systemctl daemon-reload
mkdir -p "$APP_DIR/backups"
sudo systemctl enable --now philforge-backup.timer

# ── 2. Stop old monolithic service (if running) ──────────────
if systemctl is-active --quiet philforge 2>/dev/null; then
    echo "==> Stopping old philforge.service..."
    sudo systemctl stop philforge
    sudo systemctl disable philforge 2>/dev/null || true
fi

# ── 3. Create initial upstream config ────────────────────────
echo "==> Creating nginx upstream config..."
echo "upstream philforge_backend { server 127.0.0.1:${BLUE_PORT}; }" \
    | sudo tee /etc/nginx/conf.d/philforge-upstream.conf >/dev/null

# ── 4. Install new nginx site config ─────────────────────────
if [[ ! -f "$SITE_CONF" ]]; then
    echo "==> Installing nginx site config (first-time setup)..."
    sudo cp "$APP_DIR/deploy/nginx.conf" "$SITE_CONF"
elif [[ "$SYNC_SITE_CONFIG" == "1" ]]; then
    echo "==> Syncing nginx site config from deploy/nginx.conf..."
    sudo cp "$APP_DIR/deploy/nginx.conf" "$SITE_CONF"
else
    echo "==> Preserving existing nginx site config (set SYNC_SITE_CONFIG=1 to overwrite)"
fi
sudo nginx -t && sudo nginx -s reload
echo "    Nginx config OK and reloaded."

# ── 5. Start blue instance ───────────────────────────────────
echo "==> Starting philforge@${BLUE_PORT}..."
sudo systemctl start "philforge@${BLUE_PORT}"

# ── 6. Initialize port state file ────────────────────────────
echo "$BLUE_PORT" > "$HOME/.philforge-active-port"

# ── 7. Make deploy script executable ─────────────────────────
chmod +x "$APP_DIR/deploy/cd-deploy.sh"

echo ""
echo "==> DONE! PhilForge blue-green is ready."
echo "    Active: port $BLUE_PORT"
echo "    State:  ~/.philforge-active-port"
echo "    Test:   curl http://127.0.0.1:${BLUE_PORT}/api/health"
echo "    Backup: systemctl list-timers philforge-backup.timer"
