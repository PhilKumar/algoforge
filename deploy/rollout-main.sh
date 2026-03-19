#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/ec2-user/algoforge"
VENV="$APP_DIR/venv"
LOG_TAG="[ROLLOUT]"

log() { echo "$LOG_TAG $(date '+%H:%M:%S') $*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -d "$APP_DIR" ]] || die "App dir not found: $APP_DIR"
[[ -x "$VENV/bin/python" ]] || die "Virtualenv python not found: $VENV/bin/python"

cd "$APP_DIR"

log "Updating repo to latest main..."
git fetch origin
git checkout main
git pull --ff-only origin main
log "Repo now at $(git rev-parse --short HEAD)"

source "$VENV/bin/activate"

log "Installing dependencies..."
pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"

log "Ensuring required directories exist..."
mkdir -p "$APP_DIR/backups/manual" "$APP_DIR/data/users"

log "Ensuring multi-tenant env keys exist..."
python3 - <<'PY'
from pathlib import Path
from cryptography.fernet import Fernet

env_path = Path('/home/ec2-user/algoforge/.env')
text = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
lines = text.splitlines()
updates = {
    'ADMIN_USERNAME': 'admin',
    'ALGOFORGE_DB': '/home/ec2-user/algoforge/algoforge.db',
    'ALGOFORGE_USER_DATA_ROOT': '/home/ec2-user/algoforge/data/users',
    'ALGOFORGE_BACKUP_ROOT': '/home/ec2-user/algoforge/backups',
    'ALGOFORGE_BACKUP_RETENTION_DAYS': '14',
    'ALGOFORGE_BACKUP_MIN_FREE_MB': '1024',
}
existing = {}
for line in lines:
    if not line or line.lstrip().startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    existing[key.strip()] = value
if not existing.get('ENCRYPTION_KEY', '').strip():
    updates['ENCRYPTION_KEY'] = Fernet.generate_key().decode()

out = []
seen = set()
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key, _ = line.split('=', 1)
        key = key.strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
print('[ROLLOUT] .env updated with multi-tenant settings')
PY

log "Creating pre-cutover backup..."
python3 "$APP_DIR/scripts/backup_algoforge.py" --output-dir "$APP_DIR/backups/manual" --include-legacy

log "Running pre-deploy migration..."
python3 "$APP_DIR/scripts/migrate_to_sqlite.py"

log "Installing backup service + timer..."
sudo cp "$APP_DIR/deploy/algoforge-backup.service" /etc/systemd/system/algoforge-backup.service
sudo cp "$APP_DIR/deploy/algoforge-backup.timer" /etc/systemd/system/algoforge-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now algoforge-backup.timer

log "Running blue-green deploy..."
bash "$APP_DIR/deploy/cd-deploy.sh"

log "Running post-deploy migration..."
python3 "$APP_DIR/scripts/migrate_to_sqlite.py"

log "Triggering one backup job now..."
sudo systemctl start algoforge-backup.service || true

ACTIVE_PORT=$(cat "$HOME/.algoforge-active-port")
log "Active port is $ACTIVE_PORT"

log "Health check..."
curl -sf "http://127.0.0.1:${ACTIVE_PORT}/api/health"
echo

log "Service states..."
systemctl is-active "algoforge@${ACTIVE_PORT}"
systemctl is-active nginx
systemctl is-active algoforge-backup.timer
systemctl is-enabled algoforge-backup.timer
systemctl show algoforge-backup.service -p Result -p ExecMainStatus -p ActiveState --no-pager

log "Recent backups..."
ls -lt "$APP_DIR/backups" | head

log "Rollout completed successfully."
