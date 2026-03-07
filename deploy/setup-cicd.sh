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
echo "║   CI/CD Blue-Green Setup — AlgoForge         ║"
echo "╚══════════════════════════════════════════════╝"

APP_DIR="/home/ec2-user/algoforge"
BLUE_PORT=8000

# ── 1. Install systemd template service ──────────────────────
echo "==> Installing algoforge@.service template..."
sudo cp "$APP_DIR/deploy/algoforge.service" /etc/systemd/system/algoforge@.service
sudo systemctl daemon-reload

# ── 2. Stop old monolithic service (if running) ──────────────
if systemctl is-active --quiet algoforge 2>/dev/null; then
    echo "==> Stopping old algoforge.service..."
    sudo systemctl stop algoforge
    sudo systemctl disable algoforge 2>/dev/null || true
fi

# ── 3. Create initial upstream config ────────────────────────
echo "==> Creating nginx upstream config..."
echo "upstream algoforge_backend { server 127.0.0.1:${BLUE_PORT}; }" \
    | sudo tee /etc/nginx/conf.d/algoforge-upstream.conf >/dev/null

# ── 4. Install new nginx site config ─────────────────────────
echo "==> Installing nginx site config..."
sudo cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/conf.d/algoforge.conf
sudo nginx -t && sudo nginx -s reload
echo "    Nginx config OK and reloaded."

# ── 5. Start blue instance ───────────────────────────────────
echo "==> Starting algoforge@${BLUE_PORT}..."
sudo systemctl start "algoforge@${BLUE_PORT}"

# ── 6. Initialize port state file ────────────────────────────
echo "$BLUE_PORT" > "$HOME/.algoforge-active-port"

# ── 7. Make deploy script executable ─────────────────────────
chmod +x "$APP_DIR/deploy/cd-deploy.sh"

echo ""
echo "==> DONE! AlgoForge blue-green is ready."
echo "    Active: port $BLUE_PORT"
echo "    State:  ~/.algoforge-active-port"
echo "    Test:   curl http://127.0.0.1:${BLUE_PORT}/health"
