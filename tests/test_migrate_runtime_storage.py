import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_runtime_storage.py"


class RuntimeStorageMigrationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        app_root = root / "checkout"
        state_root = root / "state"
        app_root.mkdir()
        db_path = app_root / "philforge.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO sample (value) VALUES ('preserved')")
        user_root = app_root / "data" / "users"
        archive_root = app_root / "data" / "option_archive"
        backup_root = app_root / "backups"
        (user_root / "7").mkdir(parents=True)
        (user_root / "7" / "engine.json").write_text('{"state":"idle"}', encoding="utf-8")
        archive_root.mkdir(parents=True)
        (archive_root / "contract.json").write_text('{"bars":[]}', encoding="utf-8")
        backup_root.mkdir(parents=True)
        (backup_root / "old.sha256").write_text("abc", encoding="utf-8")
        env_path = app_root / ".env"
        env_path.write_text(
            "SECRET_VALUE=do-not-print\n"
            f"PHILFORGE_DB={db_path}\n"
            f"PHILFORGE_USER_DATA_ROOT={user_root}\n"
            f"PHILFORGE_BACKUP_ROOT={backup_root}\n"
            f"PHILFORGE_OPTION_ARCHIVE_ROOT={archive_root}\n",
            encoding="utf-8",
        )
        return app_root, state_root, db_path, user_root, env_path

    def _run(self, app_root: Path, state_root: Path, env_path: Path, *args: str):
        env = os.environ.copy()
        env["PHILFORGE_DB"] = str(app_root / "philforge.db")
        env["PHILFORGE_USER_DATA_ROOT"] = str(app_root / "data" / "users")
        env["PHILFORGE_BACKUP_ROOT"] = str(app_root / "backups")
        env["PHILFORGE_OPTION_ARCHIVE_ROOT"] = str(app_root / "data" / "option_archive")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--app-root",
                str(app_root),
                "--state-root",
                str(state_root),
                "--env-file",
                str(env_path),
                *args,
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_default_is_read_only_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root, state_root, db_path, _, env_path = self._fixture(Path(tmp))
            before = env_path.read_bytes()
            proc = self._run(app_root, state_root, env_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "planned")
            self.assertFalse(state_root.exists())
            self.assertTrue(db_path.exists())
            self.assertEqual(env_path.read_bytes(), before)
            self.assertNotIn("do-not-print", proc.stdout)

    def test_apply_requires_maintenance_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root, state_root, _, _, env_path = self._fixture(Path(tmp))
            proc = self._run(app_root, state_root, env_path, "--apply")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--maintenance-confirmed", proc.stderr)

    def test_apply_copies_verifies_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root, state_root, db_path, user_root, env_path = self._fixture(Path(tmp))
            proc = self._run(app_root, state_root, env_path, "--apply", "--maintenance-confirmed")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "migrated")
            self.assertEqual(payload["db_quick_check"], "ok")
            self.assertNotIn("do-not-print", proc.stdout)

            with sqlite3.connect(state_root / "philforge.db") as conn:
                value = conn.execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(value, "preserved")
            self.assertTrue((state_root / "users" / "7" / "engine.json").exists())
            self.assertTrue((state_root / "option-archive" / "contract.json").exists())
            self.assertTrue((state_root / "backups" / "old.sha256").exists())
            self.assertTrue(db_path.exists())
            self.assertTrue((user_root / "7" / "engine.json").exists())

            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("SECRET_VALUE=do-not-print", env_text)
            self.assertIn(f"PHILFORGE_DB={(state_root / 'philforge.db').resolve()}", env_text)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
