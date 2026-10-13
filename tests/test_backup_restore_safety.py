import io
import json
import unittest
from unittest.mock import patch

import app


class FakeHandler:
    def __init__(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass

    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class BackupRestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.selected = {
            "name": "moneybook-backup-20261013T000000Z.json.gz.fernet",
            "path": "/moneybook-backup-20261013T000000Z.json.gz.fernet",
            "modified": "2026-10-13T00:00:00Z",
            "size": 123,
            "safety": False,
        }

    def test_restore_requires_exact_confirmation_before_any_backup_access(self):
        handler = FakeHandler({"path": self.selected["path"], "confirmation": "restore"})
        with patch("app._list_dropbox_backups") as list_backups:
            app.api_backup_restore(handler)
        self.assertEqual(handler.status, 400)
        list_backups.assert_not_called()
        self.assertFalse(handler.response_json()["ok"])

    def test_restore_rejects_path_not_in_fresh_backup_listing(self):
        handler = FakeHandler({"path": "/unrelated-file.txt", "confirmation": "RESTORE"})
        with patch("app._dropbox_configured", return_value=True), \
             patch("app._list_dropbox_backups", return_value=[]), \
             patch("app._download_dropbox_file") as download, \
             patch("restore_backup.restore") as restore:
            app.api_backup_restore(handler)
        self.assertEqual(handler.status, 404)
        download.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(handler.response_json()["ok"])

    def test_restore_aborts_if_current_data_safety_backup_fails(self):
        handler = FakeHandler({"path": self.selected["path"], "confirmation": "RESTORE"})
        with patch("app._dropbox_configured", return_value=True), \
             patch("app._list_dropbox_backups", return_value=[self.selected]), \
             patch("app._download_dropbox_file", return_value=b"encrypted-test-content"), \
             patch("restore_backup.load_payload", return_value={"format": "moneybook-backup-v1"}), \
             patch("app._create_encrypted_backup", side_effect=RuntimeError("simulated Dropbox upload failure")), \
             patch("restore_backup.restore") as restore:
            app.api_backup_restore(handler)
        self.assertEqual(handler.status, 400)
        restore.assert_not_called()
        self.assertFalse(handler.response_json()["ok"])
        self.assertTrue(app._backup_run_lock.acquire(blocking=False))
        app._backup_run_lock.release()

    def test_successful_restore_creates_safety_copy_before_restoring(self):
        handler = FakeHandler({"path": self.selected["path"], "confirmation": "RESTORE"})
        counts = {table: 0 for table in (
            "cards", "categories", "settings", "auto_rules",
            "transactions", "notes", "auth_users", "auth_passkeys"
        )}
        with patch("app._dropbox_configured", return_value=True), \
             patch("app._list_dropbox_backups", return_value=[self.selected]), \
             patch("app._download_dropbox_file", return_value=b"encrypted-test-content"), \
             patch("restore_backup.load_payload", return_value={"format": "moneybook-backup-v1"}), \
             patch("app._create_encrypted_backup", return_value=("moneybook-backup-current.json.gz.fernet", b"safety-bytes")), \
             patch("app._upload_dropbox_backup", return_value="/moneybook-pre-restore-current.json.gz.fernet") as upload, \
             patch("restore_backup.restore", return_value=counts) as restore, \
             patch("app._set_backup_state") as set_state:
            app.api_backup_restore(handler)
        self.assertEqual(handler.status, 200)
        upload.assert_called_once_with("moneybook-pre-restore-current.json.gz.fernet", b"safety-bytes")
        restore.assert_called_once()
        set_state.assert_called_once()
        response = handler.response_json()
        self.assertTrue(response["ok"])
        self.assertEqual(response["safety_backup"], "/moneybook-pre-restore-current.json.gz.fernet")
        self.assertEqual(response["counts"], counts)


if __name__ == "__main__":
    unittest.main()
