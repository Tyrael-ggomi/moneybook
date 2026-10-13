import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import restore_backup


class BackupPayloadTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "backup.json.gz.fernet"
        self.payload = {
            "format": "moneybook-backup-v1",
            "created_at": "2026-10-13T00:00:00Z",
            "tables": {table: [] for table in restore_backup.INSERT_ORDER},
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def write_payload(self, payload, key=None):
        key = key or self.key
        encrypted = Fernet(key).encrypt(
            gzip.compress(json.dumps(payload).encode("utf-8"))
        )
        self.path.write_bytes(encrypted)

    def test_loads_valid_encrypted_payload(self):
        self.write_payload(self.payload)
        with patch.dict(os.environ, {"BACKUP_ENCRYPTION_KEY": self.key.decode()}):
            result = restore_backup.load_payload(self.path)
        self.assertEqual(result["format"], "moneybook-backup-v1")
        self.assertEqual(set(result["tables"]), set(restore_backup.INSERT_ORDER))

    def test_rejects_wrong_encryption_key(self):
        self.write_payload(self.payload)
        with patch.dict(os.environ, {"BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(SystemExit):
                restore_backup.load_payload(self.path)

    def test_rejects_unknown_backup_format(self):
        self.write_payload({"format": "unknown", "tables": {}})
        with patch.dict(os.environ, {"BACKUP_ENCRYPTION_KEY": self.key.decode()}):
            with self.assertRaises(SystemExit):
                restore_backup.load_payload(self.path)

    def test_rejects_invalid_column_names(self):
        payload = {
            "format": "moneybook-backup-v1",
            "tables": {table: [] for table in restore_backup.INSERT_ORDER},
        }
        payload["tables"]["cards"] = [{"id; DROP TABLE cards": 1}]
        self.write_payload(payload)
        with patch.dict(os.environ, {"BACKUP_ENCRYPTION_KEY": self.key.decode()}):
            with self.assertRaises(SystemExit):
                restore_backup.load_payload(self.path)


class ImplicitCommitBackupTests(unittest.TestCase):
    def test_context_manager_commit_schedules_backup(self):
        import app

        class FakeConnection:
            def __init__(self):
                self.commits = 0
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append((sql, params))

            def commit(self):
                self.commits += 1

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        raw = FakeConnection()
        wrapped = app.DBConn(raw, postgres=False)
        with patch("app._schedule_backup_timer") as schedule:
            with wrapped as conn:
                conn.execute("UPDATE cards SET name=?", ("test",))
            schedule.assert_called_once_with(600)
        self.assertEqual(raw.commits, 2)
        self.assertTrue(any("backup_state" in sql for sql, _ in raw.statements))


if __name__ == "__main__":
    unittest.main()
