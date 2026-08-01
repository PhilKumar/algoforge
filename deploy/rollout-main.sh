#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/ec2-user/philforge"
STATE_ROOT="/home/ec2-user/.local/share/philforge"
VENV="$APP_DIR/venv"
LOG_TAG="[ROLLOUT]"
LOCK_FILE="$HOME/.philforge-deploy.lock"

log() { echo "$LOG_TAG $(date '+%H:%M:%S') $*"; }
die() { log "ERROR: $*"; exit 1; }

exec 9>"$LOCK_FILE"
flock 9

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
install -d -m 700 "$STATE_ROOT" "$STATE_ROOT/backups" "$STATE_ROOT/backups/manual" \
  "$STATE_ROOT/users" "$STATE_ROOT/option-archive"

log "Ensuring multi-tenant env keys exist..."
python3 - <<'PY'
from pathlib import Path
from cryptography.fernet import Fernet

env_path = Path('/home/ec2-user/philforge/.env')
text = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
lines = text.splitlines()
updates = {
    'ADMIN_USERNAME': 'admin',
    'PHILFORGE_DB': '/home/ec2-user/.local/share/philforge/philforge.db',
    'PHILFORGE_USER_DATA_ROOT': '/home/ec2-user/.local/share/philforge/users',
    'PHILFORGE_BACKUP_ROOT': '/home/ec2-user/.local/share/philforge/backups',
    'PHILFORGE_BACKUP_RETENTION_DAYS': '14',
    'PHILFORGE_BACKUP_MIN_FREE_MB': '1024',
    'PHILFORGE_OPTION_ARCHIVE_ROOT': '/home/ec2-user/.local/share/philforge/option-archive',
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
            # Existing installations keep their configured paths. Runtime
            # storage is moved only by migrate_runtime_storage.py during a
            # confirmed maintenance window.
            out.append(line if existing.get(key, '').strip() else f"{key}={updates[key]}")
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
python3 "$APP_DIR/scripts/backup_philforge.py" --output-dir "$STATE_ROOT/backups/manual" --include-legacy
python3 "$APP_DIR/scripts/verify_backup.py" --archive "$STATE_ROOT/backups/manual/latest.tar.gz"

log "Running pre-deploy migration..."
python3 "$APP_DIR/scripts/migrate_to_sqlite.py"

log "Installing backup service + timer..."
sudo cp "$APP_DIR/deploy/philforge-backup.service" /etc/systemd/system/philforge-backup.service
sudo cp "$APP_DIR/deploy/philforge-backup.timer" /etc/systemd/system/philforge-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now philforge-backup.timer

log "Running blue-green deploy..."
bash "$APP_DIR/deploy/cd-deploy.sh"

log "Running post-deploy migration..."
python3 "$APP_DIR/scripts/migrate_to_sqlite.py"

log "Triggering one backup job now..."
sudo systemctl start philforge-backup.service || true

ACTIVE_PORT=$(cat "$HOME/.philforge-active-port")
log "Active port is $ACTIVE_PORT"

log "Health check..."
curl -sf "http://127.0.0.1:${ACTIVE_PORT}/api/health"
echo

log "Service states..."
systemctl is-active "philforge@${ACTIVE_PORT}"
systemctl is-active nginx
systemctl is-active philforge-backup.timer
systemctl is-enabled philforge-backup.timer
systemctl show philforge-backup.service -p Result -p ExecMainStatus -p ActiveState --no-pager

log "Recent backups..."
ls -lt "$STATE_ROOT/backups" | head

log "Rollout completed successfully."
