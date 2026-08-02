import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import db
from backend.backup import backup_database
from backend.config import settings
from backend.migrations import apply_migrations, current_version


class DatabaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._old_data_dir = settings.data_dir
        self._temp = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self._temp.name)
        db.init_db()

    def tearDown(self):
        settings.data_dir = self._old_data_dir
        self._temp.cleanup()

    def test_soft_delete_restore_and_permanent_purge(self):
        conv_id = db.create_conversation("recoverable")
        db.add_message(conv_id, "user", "question")
        self.assertTrue(db.delete_conversation(conv_id))
        self.assertIsNone(db.get_conversation(conv_id))
        self.assertEqual(db.list_deleted_conversations()[0]["id"], conv_id)

        self.assertTrue(db.restore_conversation(conv_id))
        self.assertIsNotNone(db.get_conversation(conv_id))

        self.assertTrue(db.delete_conversation(conv_id))
        self.assertTrue(db.purge_conversation(conv_id))
        self.assertIsNone(db.get_conversation(conv_id, include_deleted=True))

    def test_online_backup_is_readable(self):
        conv_id = db.create_conversation("backup")
        backup = backup_database(Path(self._temp.name) / "exports")
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(backup)) as conn:
            row = conn.execute(
                "SELECT title FROM conversations WHERE id=?", (conv_id,)
            ).fetchone()
            self.assertEqual(row[0], "backup")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_old_database_is_upgraded_once(self):
        legacy = Path(self._temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as raw:
            raw.row_factory = sqlite3.Row
            raw.executescript("""
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    kb_name TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
            first = apply_migrations(raw)
            second = apply_migrations(raw)
            self.assertEqual(first, [1, 2, 3, 4])
            self.assertEqual(second, [])
            self.assertEqual(current_version(raw), 4)
            message_columns = {
                row["name"] for row in raw.execute("PRAGMA table_info(messages)")
            }
            conversation_columns = {
                row["name"] for row in raw.execute("PRAGMA table_info(conversations)")
            }
            self.assertIn("status", message_columns)
            self.assertIn("deleted_at", conversation_columns)
            self.assertIsNotNone(raw.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='research_briefs'"
            ).fetchone())
            self.assertIsNotNone(raw.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='run_events'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
