import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup_manager import create_backup, latest_backup


class BackupManagerTests(unittest.TestCase):
    def test_create_backup_preserves_source_and_validates_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "shop.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE sample(value TEXT)")
                con.execute("INSERT INTO sample VALUES ('preserved')")
            backup = create_backup(db, base / "backups")
            self.assertIsNotNone(backup)
            self.assertTrue(db.exists())
            with sqlite3.connect(backup) as con:
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], "preserved")
            latest = latest_backup(base / "backups")
            self.assertEqual(latest["path"], str(backup))

    def test_missing_source_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertIsNone(create_backup(base / "missing.db", base / "backups"))

    def test_backup_dir_cannot_be_database_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "shop.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE sample(value TEXT)")
            with self.assertRaises(ValueError):
                create_backup(db, db)


if __name__ == "__main__":
    unittest.main()
