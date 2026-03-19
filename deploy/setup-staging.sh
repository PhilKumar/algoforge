#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  AlgoForge Staging Setup — feature/multi-tenant
#  Creates a separate worktree + venv + systemd service + nginx vhost
#  for staging.philforge.in without touching live production.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

APP_REPO="/home/ec2-user/algoforge"
STAGING_DIR="/home/ec2-user/algoforge-staging"
BRANCH="${BRANCH:-feature/multi-tenant}"
STAGING_DOMAIN="${STAGING_DOMAIN:-staging.philforge.in}"
STAGING_PORT="${STAGING_PORT:-8002}"
SERVICE_NAME="algoforge-staging"
SITE_CONF="/etc/nginx/conf.d/${SERVICE_NAME}.conf"

ENABLE_TLS="${ENABLE_TLS:-0}"          # set to 1 after DNS is pointed
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"

log() { echo "[STAGING] $(date '+%H:%M:%S') $*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -d "$APP_REPO/.git" ]] || die "Base repo not found at $APP_REPO"
[[ "$STAGING_PORT" == "8002" ]] || die "This script and the shipped configs expect STAGING_PORT=8002"

log "Fetching latest refs from origin..."
git -C "$APP_REPO" fetch origin

if [[ ! -d "$STAGING_DIR/.git" ]]; then
    log "Creating staging worktree at $STAGING_DIR for branch $BRANCH..."
    git -C "$APP_REPO" worktree add "$STAGING_DIR" "$BRANCH" || \
      git -C "$APP_REPO" worktree add -b "$BRANCH" "$STAGING_DIR" "origin/$BRANCH"
else
    log "Refreshing existing staging worktree..."
    git -C "$STAGING_DIR" fetch origin
    git -C "$STAGING_DIR" checkout "$BRANCH"
    git -C "$STAGING_DIR" pull --ff-only origin "$BRANCH"
fi

if [[ ! -d "$STAGING_DIR/venv" ]]; then
    log "Creating staging virtualenv..."
    python3.11 -m venv "$STAGING_DIR/venv"
fi

log "Installing staging dependencies..."
source "$STAGING_DIR/venv/bin/activate"
pip install -q --disable-pip-version-check --upgrade pip
pip install -q --disable-pip-version-check -r "$STAGING_DIR/requirements.txt"

if [[ ! -f "$STAGING_DIR/.env.staging" ]]; then
    log "Creating .env.staging from template..."
    cp "$STAGING_DIR/.env.staging.example" "$STAGING_DIR/.env.staging"
    log "Edit $STAGING_DIR/.env.staging before real broker testing."
fi

mkdir -p "$STAGING_DIR/backups" "$STAGING_DIR/data/users"

log "Installing $SERVICE_NAME systemd unit..."
sudo cp "$STAGING_DIR/deploy/algoforge-staging.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

log "Installing bootstrap nginx config for $STAGING_DOMAIN..."
sudo cp "$STAGING_DIR/deploy/nginx.staging.bootstrap.conf" "$SITE_CONF"
sudo nginx -t
sudo nginx -s reload

log "Running SQLite migration in staging worktree..."
set -a
source "$STAGING_DIR/.env.staging"
set +a
python3 "$STAGING_DIR/scripts/migrate_to_sqlite.py"

log "Starting staging service..."
sudo systemctl restart "$SERVICE_NAME"
sleep 3
curl -sf "http://127.0.0.1:${STAGING_PORT}/api/health" >/dev/null || die "Staging health check failed on port ${STAGING_PORT}"

if [[ "$ENABLE_TLS" == "1" ]]; then
    [[ -n "$LETSENCRYPT_EMAIL" ]] || die "Set LETSENCRYPT_EMAIL before ENABLE_TLS=1"
    log "Requesting Let's Encrypt certificate for $STAGING_DOMAIN..."
    sudo certbot certonly --nginx -d "$STAGING_DOMAIN" -m "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email
    log "Installing HTTPS nginx config for $STAGING_DOMAIN..."
    sudo cp "$STAGING_DIR/deploy/nginx.staging.conf" "$SITE_CONF"
    sudo nginx -t
    sudo nginx -s reload
    log "Staging HTTPS ready: https://$STAGING_DOMAIN"
else
    log "Bootstrap staging ready over HTTP: http://$STAGING_DOMAIN"
    log "After DNS points correctly, rerun with ENABLE_TLS=1 LETSENCRYPT_EMAIL=you@example.com"
fi

log "Done."
