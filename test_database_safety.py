import sqlite3
import tempfile
import unittest
from pathlib import Path

from database_safety import migrate_legacy_database, backup_sqlite_database


class DatabaseSafetyTests(unittest.TestCase):
    def _make_db(self, path: Path, value: str):
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO sample(value) VALUES (?)", (value,))

    def test_migration_copies_and_preserves_legacy_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "old" / "shop.db"
            target = root / "persistent" / "shop.db"
            legacy.parent.mkdir()
            self._make_db(legacy, "kept")
            self.assertTrue(migrate_legacy_database(target, legacy))
            self.assertTrue(legacy.exists())
            with sqlite3.connect(target) as con:
                self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], "kept")

    def test_migration_never_overwrites_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "old.db"
            target = root / "new.db"
            self._make_db(legacy, "old")
            self._make_db(target, "new")
            self.assertFalse(migrate_legacy_database(target, legacy))
            with sqlite3.connect(target) as con:
                self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], "new")
            self.assertTrue(legacy.exists())

    def test_backup_is_valid_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "shop.db"
            self._make_db(source, "snapshot")
            out = backup_sqlite_database(source, root / "backups", keep=3)
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            self.assertTrue(source.exists())
            with sqlite3.connect(out) as con:
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], "snapshot")


if __name__ == "__main__":
    unittest.main()
