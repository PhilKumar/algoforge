# AlgoForge Multi-Tenant Rollout Guide

This guide is for the `feature/multi-tenant` branch only. It does not apply to the current single-user `main` branch.

## 1. What This Branch Changes

- Username + password auth with per-user sessions
- SQLite-backed users, strategies, runs, journals, trade history, and scalp trades
- Per-user chart and engine-state storage under `data/users/`
- Per-user broker context for live routes and scalp
- Admin console + account settings UI
- Daily backup tooling and WebSocket isolation/load-test tooling

## 2. Required Environment Variables

At minimum, set these in `.env` before first real use:

```bash
APP_HOST=127.0.0.1
APP_PORT=8000
DEBUG=false

DHAN_CLIENT_ID=...
DHAN_ACCESS_TOKEN=...
DHAN_PIN=...
DHAN_TOTP_SECRET=...

ADMIN_USERNAME=admin
ALGOFORGE_PIN=your_first_admin_password
ALGOFORGE_DB=/home/ec2-user/algoforge/algoforge.db
ALGOFORGE_USER_DATA_ROOT=/home/ec2-user/algoforge/data/users
ALGOFORGE_BACKUP_ROOT=/home/ec2-user/algoforge/backups
ALGOFORGE_BACKUP_RETENTION_DAYS=14
ALGOFORGE_BACKUP_MIN_FREE_MB=1024
SESSION_TTL_HOURS=24
MAX_LOGIN_ATTEMPTS=5
LOGIN_LOCKOUT_MINUTES=5
ENCRYPTION_KEY=generated_fernet_key_here
DHAN_REFERRAL_URL=https://your_dhan_referral_link_here
```

Generate `ENCRYPTION_KEY` with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Notes:

- `ALGOFORGE_PIN` or `ALGOFORGE_PASSWORD` is only used for first-run admin bootstrap when no admin user exists yet.
- Once the admin user exists in SQLite, changing `ALGOFORGE_PIN` does not reset that account.
- Per-user broker credential storage is blocked unless `ENCRYPTION_KEY` is set.
- `DHAN_REFERRAL_URL` is optional. When set, the login page shows an external CTA to open a new Dhan account.

## 3. Local Or Staging Bring-Up

```bash
git checkout feature/multi-tenant
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`, then run:

```bash
python3 scripts/migrate_to_sqlite.py
python3 scripts/test_auth.py
python3 scripts/test_engine_isolation.py
python3 scripts/test_broker_isolation.py
python3 -m unittest tests.test_timeframes
uvicorn app:app --host 127.0.0.1 --port 8000
```

If you want a safe local test DB instead of your real data:

```bash
export ALGOFORGE_DB=/tmp/algoforge-multi-tenant-test.db
export ALGOFORGE_USER_DATA_ROOT=/tmp/algoforge-user-data
export ALGOFORGE_BACKUP_ROOT=/tmp/algoforge-backups
export ALGOFORGE_PIN=123456
export ENCRYPTION_KEY="$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
export ALGOFORGE_SKIP_STARTUP_JOBS=1
uvicorn app:app --host 127.0.0.1 --port 8000
```

## 4. Staging UAT Checklist

Run this with at least:

- `admin` user
- one normal user with broker credentials
- one normal user without broker credentials

### Auth + Admin

1. Log in as `admin`.
2. Open the topbar account/admin controls.
3. Create two users from the Admin Console.
4. Disable one user and confirm that their existing session stops working.
5. Reset that user’s password and verify old sessions are revoked.

### Data Isolation

1. As `admin`, save one strategy and one run.
2. As user A, confirm those do not appear in Saved Strategies or Results.
3. As user A, create strategy, backtest/run, journal entry, and chart uploads.
4. Log back in as `admin` and confirm user A’s data is invisible.
5. Repeat the same check for trade history and scalp trades.

### Broker Isolation

1. As user A, save broker credentials in Account Settings.
2. Check broker connectivity from user A.
3. As user B with no broker creds, verify broker-backed routes fail cleanly.
4. As `admin`, verify the admin account can still use the global `.env` broker fallback if intended.

### Engine + WebSocket Isolation

1. Start one paper engine as `admin`.
2. Start one paper engine as user A.
3. Open both users in separate browsers.
4. Verify each browser only sees its own engines and events.
5. Run the WebSocket probe:

```bash
python3 scripts/load_test_ws.py \
  --base-url http://127.0.0.1:8000 \
  --credential admin:123456 \
  --credential usera:654321 \
  --credential userb:abcdef \
  --start-paper \
  --duration 12
```

Expected result: every probe line should report `PASS` with `isolated=True`.

## 5. Production Cutover On Lightsail

These steps assume the current production server is the AWS Lightsail host and the app directory is `/home/ec2-user/algoforge`.

### Pre-Cutover Backup

```bash
cd /home/ec2-user/algoforge
python3 scripts/backup_algoforge.py --output-dir backups/manual --include-legacy
```

This backup path streams large folders directly into the archive instead of staging a full duplicate copy.
If the instance does not have enough free disk to create a safe local archive, it aborts early instead of filling the box and destabilizing SSH/Nginx.

### Update `.env`

Set these explicitly on the server:

- `ADMIN_USERNAME`
- `ALGOFORGE_DB`
- `ALGOFORGE_USER_DATA_ROOT`
- `ALGOFORGE_BACKUP_ROOT`
- `ALGOFORGE_BACKUP_MIN_FREE_MB`
- `ENCRYPTION_KEY`
- existing Dhan/global broker values if admin fallback is still needed

### Migrate Existing Data

```bash
cd /home/ec2-user/algoforge
source venv/bin/activate
python3 scripts/migrate_to_sqlite.py
```

This is idempotent and preserves the old JSON/files for rollback.

### Install Or Refresh Services

If blue-green systemd is already installed, do not rerun setup blindly. Use deploy only.

If this is the first blue-green setup on a box:

```bash
cd /home/ec2-user/algoforge
SYNC_SITE_CONFIG=0 bash deploy/setup-cicd.sh
```

If blue-green is already present:

```bash
cd /home/ec2-user/algoforge
bash deploy/cd-deploy.sh
```

`SYNC_SITE_CONFIG=0` is intentional. It preserves the existing server-local Nginx vhost unless you explicitly want the repo config to overwrite it.

### Safe One-Command Production Rollout

On a production box that already has the repo and venv in place, prefer:

```bash
cd /home/ec2-user/algoforge
bash deploy/rollout-main.sh
```

That script:

- updates the checkout to latest `main`
- ensures the required multi-tenant env keys exist
- creates a pre-cutover backup with legacy data included
- runs migration before and after blue-green deploy
- installs/enables the backup timer
- verifies health and active port at the end

### Post-Deploy Verification

```bash
curl -s http://127.0.0.1:8000/api/health
systemctl list-timers algoforge-backup.timer
sudo journalctl -u algoforge@8000 -n 50 --no-pager
```

Then verify in the browser:

1. Admin login works with username + password
2. Account Settings loads
3. Admin Console lists users
4. Broker check works for admin
5. User-specific broker settings save only when `ENCRYPTION_KEY` is configured
6. Saved Strategies / Results / Charts / Journal / Scalp data remain isolated
7. Light and dark mode still render correctly on Builder, Live, Scalp, and Charts

## 7. Daily Ops

Check backups:

```bash
systemctl list-timers algoforge-backup.timer
ls -lh /home/ec2-user/algoforge/backups
```

Manual backup:

```bash
python3 scripts/backup_algoforge.py
```

WebSocket/user-isolation probe on the production-shaped environment:

```bash
python3 scripts/load_test_ws.py --base-url https://philforge.in --credential admin:... --credential usera:...
```

## 8. Rollback

If cutover fails:

1. Stop the new instance.
2. Restore the latest backup archive.
3. Check out the previous known-good commit or `main`.
4. Point Nginx back to the stable app instance.

Minimum rollback commands:

```bash
cd /home/ec2-user/algoforge
git checkout main
sudo systemctl restart algoforge@8000
sudo nginx -t && sudo nginx -s reload
```

Use the preserved JSON files and backup archive if you need to roll data back as well.

## 9. Merge Readiness

Do not merge to `main` until all of these are true:

- production UAT passes with at least 3 users
- `scripts/test_auth.py` passes
- `scripts/test_engine_isolation.py` passes
- `scripts/test_broker_isolation.py` passes
- `python3 -m unittest tests.test_timeframes` passes
- `scripts/load_test_ws.py` passes with concurrent users
- backup timer is installed and producing archives
- `ENCRYPTION_KEY` is configured on the target server
- final admin and non-admin browser pass is clean on desktop and mobile
