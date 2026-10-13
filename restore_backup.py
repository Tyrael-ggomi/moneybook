"""Restore a Moneybook encrypted backup into an existing Moneybook database.

Run only after setting DATABASE_URL (or MONEYBOOK_DB for SQLite) and
BACKUP_ENCRYPTION_KEY. This is destructive and requires --replace.
"""
import argparse
import gzip
import json
import os
import re
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import app


INSERT_ORDER = (
    "cards",
    "categories",
    "settings",
    "auto_rules",
    "transactions",
    "notes",
    "auth_users",
    "auth_passkeys",
)
DELETE_ORDER = tuple(reversed(INSERT_ORDER))
FORMAT_NAME = "moneybook-backup-v1"


def load_payload(path: Path):
    key = os.environ.get("BACKUP_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SystemExit("BACKUP_ENCRYPTION_KEY 환경 변수를 먼저 설정하세요.")
    try:
        encrypted = path.read_bytes()
        compressed = Fernet(key.encode("ascii")).decrypt(encrypted)
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, InvalidToken, json.JSONDecodeError, gzip.BadGzipFile) as exc:
        raise SystemExit("백업을 읽을 수 없습니다. 파일과 BACKUP_ENCRYPTION_KEY를 확인하세요.") from exc

    if not isinstance(payload, dict) or payload.get("format") != FORMAT_NAME:
        raise SystemExit("지원하지 않는 머니북 백업 형식입니다.")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise SystemExit("백업에 테이블 데이터가 없습니다.")
    for table in INSERT_ORDER:
        rows = tables.get(table)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SystemExit(f"백업 데이터 형식이 올바르지 않습니다: {table}")
        for row in rows:
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(column)) for column in row):
                raise SystemExit(f"백업에 잘못된 열 이름이 있습니다: {table}")
    return payload


def restore(path: Path):
    payload = load_payload(path)
    with app.db() as conn:
        # Verify every destination table exists before deleting any data.
        for table in INSERT_ORDER:
            conn.execute(f"SELECT * FROM {table} LIMIT 0")
        for table in DELETE_ORDER:
            conn.execute(f"DELETE FROM {table}")
        for table in INSERT_ORDER:
            rows = payload["tables"][table]
            if not rows:
                continue
            columns = list(rows[0].keys())
            if any(list(row.keys()) != columns for row in rows):
                raise SystemExit(f"백업 행의 열 구성이 서로 다릅니다: {table}")
            column_sql = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
            conn.executemany(sql, [tuple(row[column] for column in columns) for row in rows])
        conn.commit()

        # Keep PostgreSQL generated IDs above restored IDs.
        if conn.postgres:
            for table in ("cards", "categories", "transactions", "auto_rules", "notes", "auth_passkeys"):
                conn.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                    f"EXISTS(SELECT 1 FROM {table}))",
                    (table,),
                )
            conn.commit()
    return {table: len(payload["tables"][table]) for table in INSERT_ORDER}


def main():
    parser = argparse.ArgumentParser(description="Restore an encrypted Moneybook backup")
    parser.add_argument("backup_file", type=Path, help="encrypted .fernet backup file")
    parser.add_argument("--replace", action="store_true", help="confirm replacing current Moneybook data")
    args = parser.parse_args()
    if not args.replace:
        parser.error("기존 데이터 전체를 교체하므로 --replace 옵션이 반드시 필요합니다.")
    if not args.backup_file.is_file():
        parser.error("백업 파일을 찾을 수 없습니다.")
    print("주의: 현재 머니북의 카드·분류·거래·설정·메모·로그인 정보를 백업 내용으로 교체합니다.")
    answer = input("정말 복원할까요? 계속하려면 RESTORE 를 입력하세요: ").strip()
    if answer != "RESTORE":
        print("복원을 취소했습니다.")
        return 2
    counts = restore(args.backup_file)
    print("복원 완료:")
    for table, count in counts.items():
        print(f"- {table}: {count}개")
    print("복원 후 로그인 비밀번호/패스키와 데이터가 정상인지 확인하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
