import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app

SCHEMA = """
CREATE TABLE cards (id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
  payment_day INTEGER, period_start_day INTEGER, period_end_day INTEGER);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE auto_rules (id INTEGER PRIMARY KEY, keyword TEXT NOT NULL, category_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE notes (id INTEGER PRIMARY KEY, content TEXT);
CREATE TABLE transactions (id INTEGER PRIMARY KEY, tx_date TEXT NOT NULL, tx_time TEXT NOT NULL DEFAULT '', amount INTEGER NOT NULL,
  card_id INTEGER NOT NULL, category_id INTEGER, content TEXT NOT NULL DEFAULT '', user_percent INTEGER NOT NULL DEFAULT 50,
  wife_percent INTEGER NOT NULL DEFAULT 50, installment_months INTEGER NOT NULL DEFAULT 1);
INSERT INTO cards(id,name) VALUES (1,'테스트카드');
INSERT INTO categories(id,name,sort_order) VALUES (1,'식비',0);
INSERT INTO auto_rules(keyword,category_id) VALUES ('스타벅스',1);
"""

TOKEN = "test-token-0123456789abcdef"


class ColdStartEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "test.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        cls.patches = [
            patch.object(app, "DB_PATH", db_path),
            patch.object(app, "_schedule_backup_timer", lambda delay: None),
            patch.object(app, "_init_done", False),
            patch.dict(os.environ, {"MONEYBOOK_API_TOKEN": TOKEN, "MONEYBOOK_AUTH_PASSWORD": "pw"}),
        ]
        for p in cls.patches:
            p.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def auth(self):
        return {"Authorization": "Bearer " + TOKEN}

    def test_health_needs_no_auth_or_db(self):
        with patch.object(app, "db", side_effect=AssertionError("health must not touch the DB")):
            status, body = self.request("GET", "/health")
            self.assertEqual((status, body), (200, b"ok"))
            status, body = self.request("HEAD", "/health")
            self.assertEqual((status, body), (200, b""))

    def test_api_requires_auth(self):
        status, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/bootstrap", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/bootstrap", headers=self.auth())
        self.assertEqual(status, 200)

    def test_token_cannot_reach_backup_endpoints(self):
        status, _ = self.request("GET", "/api/backup/status", headers=self.auth())
        self.assertEqual(status, 401)

    def test_batch_jsonl_is_idempotent(self):
        lines = "\n".join([
            json.dumps({"date": "2026-11-01", "time": "12:30", "amount": "4,500", "card": "테스트카드", "content": "스타벅스 강남"}),
            json.dumps({"date": "2026-11-01", "time": "12:31", "amount": 9000, "card_id": 1, "content": "알 수 없는 가게"}),
            json.dumps({"date": "2026-11-01", "time": "12:31", "amount": 9000, "card_id": 1, "content": "알 수 없는 가게"}),
            "",
            "not json",
            json.dumps({"time": "12:00", "amount": 1, "card_id": 1, "content": "날짜 없음"}),
        ])
        headers = {**self.auth(), "Content-Type": "text/plain"}
        status, body = self.request("POST", "/api/transactions/batch", lines, headers)
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual((result["inserted"], result["duplicates"], result["errors"]), (3, 0, 2))

        status, body = self.request("POST", "/api/transactions/batch", lines, headers)
        result = json.loads(body)
        self.assertEqual((result["inserted"], result["duplicates"], result["errors"]), (0, 3, 2))

        with app.db() as conn:
            rows = conn.execute("SELECT content, category_id FROM transactions WHERE tx_date='2026-11-01' ORDER BY id").fetchall()
            uncategorized = conn.execute("SELECT id FROM categories WHERE name='미정'").fetchone()["id"]
        self.assertEqual([(r["content"], r["category_id"]) for r in rows], [
            ("스타벅스 강남", 1), ("알 수 없는 가게", uncategorized), ("알 수 없는 가게", uncategorized)])

    def test_batch_accepts_json_rows(self):
        payload = json.dumps({"rows": [{"date": "2026-11-02", "amount": 100, "card_id": 1, "content": "스타벅스"}]})
        status, body = self.request("POST", "/api/transactions/batch", payload, {**self.auth(), "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["inserted"], 1)


class StartupTests(unittest.TestCase):
    def test_webauthn_not_imported_at_startup(self):
        import subprocess, sys
        code = "import sys, app; print('webauthn' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(app.__file__).parent, check=True)
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
