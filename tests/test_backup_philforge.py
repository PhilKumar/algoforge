import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backup_philforge.py"


class BackupPhilForgeTests(unittest.TestCase):
    def test_backup_archive_includes_db_user_data_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "philforge.db"
            user_data_root = root / "user-data"
            backup_root = root / "backups"

            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO sample (value) VALUES ('ok')")
            conn.commit()
            conn.close()

            (user_data_root / "1" / "charts").mkdir(parents=True, exist_ok=True)
            (user_data_root / "1" / "charts" / "shot.txt").write_text("chart", encoding="utf-8")

            (root / "journals").mkdir(parents=True, exist_ok=True)
            (root / "journals" / "2026-03-19.json").write_text('{"note":"hi"}', encoding="utf-8")
            (root / "strategies.json").write_text('{"Demo": {"name": "Demo"}}', encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "PHILFORGE_DB": str(db_path),
                    "PHILFORGE_USER_DATA_ROOT": str(user_data_root),
                    "PHILFORGE_BACKUP_ROOT": str(backup_root),
                    "PHILFORGE_BACKUP_MIN_FREE_MB": "0",
                }
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(backup_root),
                    "--include-legacy",
                    "--legacy-root",
                    str(root),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(proc.stdout)
            archive_path = Path(payload["archive"])
            self.assertTrue(archive_path.exists())
            self.assertTrue((backup_root / "latest.tar.gz").exists())
            self.assertGreaterEqual(payload["included_legacy"], 2)

            with tarfile.open(archive_path, "r:gz") as tf:
                names = set(tf.getnames())

            self.assertIn("philforge-backup/philforge.db", names)
            self.assertIn("philforge-backup/manifest.json", names)
            self.assertIn("philforge-backup/user-data/1/charts/shot.txt", names)
            self.assertIn("philforge-backup/legacy/strategies.json", names)
            self.assertIn("philforge-backup/legacy/journals/2026-03-19.json", names)

    def test_backup_archive_allows_legacy_only_when_db_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "missing.db"
            user_data_root = root / "user-data"
            backup_root = root / "backups"

            (root / "strategies.json").write_text('{"Demo": {"name": "Demo"}}', encoding="utf-8")
            user_data_root.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env.update(
                {
                    "PHILFORGE_DB": str(db_path),
                    "PHILFORGE_USER_DATA_ROOT": str(user_data_root),
                    "PHILFORGE_BACKUP_ROOT": str(backup_root),
                    "PHILFORGE_BACKUP_MIN_FREE_MB": "0",
                }
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(backup_root),
                    "--include-legacy",
                    "--legacy-root",
                    str(root),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

            payload = json.loads(proc.stdout)
            archive_path = Path(payload["archive"])
            self.assertTrue(archive_path.exists())

            with tarfile.open(archive_path, "r:gz") as tf:
                names = set(tf.getnames())
                manifest = json.loads(tf.extractfile("philforge-backup/manifest.json").read().decode("utf-8"))

            self.assertNotIn("philforge-backup/philforge.db", names)
            self.assertIn("philforge-backup/legacy/strategies.json", names)
            self.assertFalse(manifest["db_present"])


if __name__ == "__main__":
    unittest.main()
