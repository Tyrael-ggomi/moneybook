from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import base64
import hashlib
import hmac
import secrets
import time
import threading
import gzip
import urllib.parse
import urllib.request
import urllib.error

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
try:
    from webauthn import generate_registration_options, verify_registration_response, generate_authentication_options, verify_authentication_response, options_to_json, base64url_to_bytes
    from webauthn.helpers.structs import AuthenticatorSelectionCriteria, AuthenticatorAttachment, ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor, AuthenticatorTransport
    WEBAUTHN_AVAILABLE=True
except ImportError:
    WEBAUTHN_AVAILABLE=False

APP_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
DB_PATH = Path(os.environ.get('MONEYBOOK_DB', APP_DIR / 'moneybook.db')).resolve()
HOST = os.environ.get('MONEYBOOK_HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', os.environ.get('MONEYBOOK_PORT', '8765')))
USING_POSTGRES = bool(DATABASE_URL)
INTEGRITY_ERROR = (sqlite3.IntegrityError,) if not USING_POSTGRES else (psycopg.errors.UniqueViolation, psycopg.errors.ForeignKeyViolation) if psycopg else (Exception,)


class DBConn:
    def __init__(self, raw, postgres=False):
        self.raw = raw
        self.postgres = postgres
        self._backup_dirty = False

    def _track_backup_change(self, sql):
        if re.match(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+["\x60]?(cards|categories|transactions|settings|auto_rules|notes)\b', str(sql), re.I):
            self._backup_dirty = True

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.raw.__exit__(exc_type, exc, tb)

    def _sql(self, sql):
        return sql.replace('?', '%s') if self.postgres else sql

    def execute(self, sql, params=()):
        self._track_backup_change(sql)
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql, params):
        self._track_backup_change(sql)
        sql = self._sql(sql)
        if self.postgres:
            with self.raw.cursor() as cur:
                return cur.executemany(sql, params)
        return self.raw.executemany(sql, params)

    def commit(self):
        self.raw.commit()
        if self._backup_dirty:
            self._backup_dirty = False
            now = int(time.time())
            sql = "INSERT INTO backup_state(id,last_change_at,status) VALUES(1,?,'pending') ON CONFLICT(id) DO UPDATE SET last_change_at=excluded.last_change_at,status='pending',last_error=NULL"
            self.raw.execute(self._sql(sql), (now,))
            self.raw.commit()
            _schedule_backup_timer(600)

    def close(self):
        return self.raw.close()


def db():
    """Render/Supabase uses PostgreSQL; local development continues to use SQLite."""
    if USING_POSTGRES:
        if psycopg is None:
            raise RuntimeError('PostgreSQL 연결 모듈이 없습니다. requirements.txt를 확인하세요.')
        return DBConn(psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None), postgres=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=3000')
    return DBConn(conn, postgres=False)


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def json_response(handler, payload, status=HTTPStatus.OK):
    raw = json.dumps(payload, ensure_ascii=False, default=_json_default).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(raw)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler, raw, content_type='text/html; charset=utf-8', status=HTTPStatus.OK):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(raw)))
    handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
    handler.send_header('Pragma', 'no-cache')
    handler.send_header('Expires', '0')
    handler.end_headers()
    handler.wfile.write(raw)


def parse_body(handler):
    length = int(handler.headers.get('Content-Length', '0'))
    if length > 1_000_000:
        raise ValueError('요청 데이터가 너무 큽니다.')
    raw = handler.rfile.read(length)
    return json.loads(raw.decode('utf-8') or '{}')


def normalize_amount(value):
    if isinstance(value, bool):
        raise ValueError('금액이 올바르지 않습니다.')
    s = str(value).strip().replace(',', '').replace('원', '').replace(' ', '')
    if not re.fullmatch(r'-?\d+', s):
        raise ValueError('금액은 정수로 입력하세요. 예: 12,300 또는 -5,000')
    return int(s)


def normalize_date(value):
    s = str(value or '').strip()
    if not s:
        return date.today().isoformat()
    s = s.replace('.', '-').replace('/', '-')
    m = re.fullmatch(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', s)
    if m:
        s = f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    # Basic validation and canonical YYYY-MM-DD output.
    parts = s.split('-')
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        y, mo, d = map(int, parts)
        return date(y, mo, d).isoformat()
    raise ValueError('날짜 형식이 올바르지 않습니다.')


def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r['key']: r['value'] for r in rows}


def ensure_schema():
    """Keep the local SQLite schema compatible; Supabase schema is created by migration SQL."""
    _ensure_backup_table()
    if USING_POSTGRES:
        with db() as conn:
            required = conn.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('cards','categories','transactions','settings','auto_rules','notes')").fetchone()
            if not required or int(required['n']) != 6:
                raise RuntimeError('Supabase DB에 Moneybook 테이블이 아직 모두 만들어지지 않았습니다.')
        _ensure_auth_tables()
        return
    with db() as conn:
        rule_cols={r[1] for r in conn.execute("PRAGMA table_info(auto_rules)").fetchall()}
        if 'sort_order' not in rule_cols:
            conn.execute("ALTER TABLE auto_rules ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE auto_rules SET sort_order=id WHERE sort_order=0")
        tx_cols={r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        if 'tx_time' not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN tx_time TEXT NOT NULL DEFAULT ''")
        card_cols={r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()}
        if 'payment_day' not in card_cols:
            conn.execute("ALTER TABLE cards ADD COLUMN payment_day INTEGER")
        if 'period_start_day' not in card_cols:
            conn.execute("ALTER TABLE cards ADD COLUMN period_start_day INTEGER")
        if 'period_end_day' not in card_cols:
            conn.execute("ALTER TABLE cards ADD COLUMN period_end_day INTEGER")
        # Known card-company rules supplied for the existing sample cards.
        # Other cards remain unset until the user configures them.
        conn.execute("UPDATE cards SET payment_day=20, period_start_day=9, period_end_day=8 WHERE name LIKE '현대카드%' AND (period_start_day IS NULL OR period_end_day IS NULL)")
        conn.execute("UPDATE cards SET payment_day=20, period_start_day=7, period_end_day=6 WHERE name LIKE '신한%' AND (period_start_day IS NULL OR period_end_day IS NULL)")
        # Import fallback: keep unclassified rows editable instead of blocking import.
        minjeong = conn.execute("SELECT id,active FROM categories WHERE name='미정' LIMIT 1").fetchone()
        if minjeong:
            conn.execute("UPDATE categories SET active=1 WHERE id=?", (minjeong['id'],))
        else:
            max_order_row = conn.execute("SELECT COALESCE(MAX(sort_order),-1) AS max_order FROM categories").fetchone()
            max_order = max_order_row['max_order'] if USING_POSTGRES else max_order_row[0]
            conn.execute("INSERT INTO categories(name,active,sort_order) VALUES('미정',1,?)", (max_order+1,))
        conn.commit()
    _ensure_auth_tables()




# Dropbox automatic backup ----------------------------------------------------
BACKUP_TABLES = ("cards", "categories", "transactions", "settings", "auto_rules", "notes")
BACKUP_QUIET_SECONDS = 10 * 60
_backup_timer = None
_backup_timer_lock = threading.Lock()
_backup_run_lock = threading.Lock()


def _ensure_backup_table():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS backup_state (
            id INTEGER PRIMARY KEY,
            last_change_at BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'idle',
            last_attempt_at BIGINT,
            last_success_at BIGINT,
            last_file TEXT,
            last_error TEXT
        )""")
        conn.execute("INSERT INTO backup_state(id,last_change_at,status) VALUES(1,0,'idle') ON CONFLICT(id) DO NOTHING")
        conn.commit()


def _dropbox_configured():
    return all(os.environ.get(k, "").strip() for k in (
        "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN", "BACKUP_ENCRYPTION_KEY"
    )) and Fernet is not None


def _backup_state(conn=None):
    own = conn is None
    if own:
        conn = db()
    try:
        row = conn.execute("SELECT * FROM backup_state WHERE id=1").fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def _set_backup_state(**fields):
    allowed = {"last_change_at", "status", "last_attempt_at", "last_success_at", "last_file", "last_error"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE backup_state SET {assignments} WHERE id=1", tuple(fields.values()))
        conn.commit()


def _dropbox_json_request(url, payload, access_token=None):
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _dropbox_access_token():
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"].strip(),
        "client_id": os.environ["DROPBOX_APP_KEY"].strip(),
        "client_secret": os.environ["DROPBOX_APP_SECRET"].strip(),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.dropboxapi.com/oauth2/token", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    token = str(result.get("access_token") or "")
    if not token:
        raise RuntimeError("Dropbox 인증에 실패했습니다.")
    return token


def _upload_dropbox_backup(filename, encrypted_bytes):
    access_token = _dropbox_access_token()
    prefix = os.environ.get("DROPBOX_BACKUP_PATH", "").strip().rstrip("/")
    path = (prefix + "/" if prefix else "/") + filename
    args = json.dumps({
        "path": path,
        "mode": "add",
        "autorename": True,
        "mute": True,
        "strict_conflict": False
    }, separators=(",", ":"))
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload",
        data=encrypted_bytes,
        headers={
            "Authorization": "Bearer " + access_token,
            "Dropbox-API-Arg": args,
            "Content-Type": "application/octet-stream"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    return str(result.get("path_display") or path)


def _run_due_backup():
    if not _backup_run_lock.acquire(blocking=False):
        return
    try:
        state = _backup_state()
        if not state:
            return
        now = int(time.time())
        changed_at = int(state.get("last_change_at") or 0)
        status = str(state.get("status") or "")
        if not changed_at or now < changed_at + BACKUP_QUIET_SECONDS:
            return
        if status == "success" and int(state.get("last_success_at") or 0) >= changed_at:
            return
        last_attempt = int(state.get("last_attempt_at") or 0)
        if status == "failed" and now < last_attempt + BACKUP_QUIET_SECONDS:
            return
        if not _dropbox_configured():
            _set_backup_state(status="waiting_config", last_error="Dropbox 환경 변수 설정이 필요합니다.")
            return
        if Fernet is None:
            _set_backup_state(status="failed", last_attempt_at=now, last_error="암호화 모듈이 설치되지 않았습니다.")
            return

        _set_backup_state(status="uploading", last_attempt_at=now, last_error=None)
        try:
            key = os.environ["BACKUP_ENCRYPTION_KEY"].strip().encode("ascii")
            cipher = Fernet(key)
            payload = {"format": "moneybook-backup-v1", "created_at": datetime.utcnow().isoformat() + "Z",
                       "tables": {}}
            with db() as conn:
                for table in BACKUP_TABLES:
                    payload["tables"][table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            raw = json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":")).encode("utf-8")
            compressed = gzip.compress(raw, compresslevel=6)
            encrypted = cipher.encrypt(compressed)
            stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            filename = f"moneybook-backup-{stamp}.json.gz.fernet"
            uploaded_path = _upload_dropbox_backup(filename, encrypted)
            latest = _backup_state()
            latest_change = int((latest or {}).get("last_change_at") or 0)
            if latest_change > changed_at:
                _set_backup_state(status="pending", last_success_at=now, last_file=uploaded_path, last_error=None)
                _schedule_backup_timer(max(1, latest_change + BACKUP_QUIET_SECONDS - int(time.time())))
            else:
                _set_backup_state(status="success", last_success_at=now, last_file=uploaded_path, last_error=None)
        except Exception as exc:
            # Do not expose OAuth credentials, tokens, or provider response bodies.
            reason = "Dropbox 백업에 실패했습니다. 인증·권한·암호화 키 설정을 확인하세요."
            if isinstance(exc, (ValueError, TypeError)) and "fernet" in str(exc).lower():
                reason = "암호화 키 형식이 올바르지 않습니다. Render 환경 변수를 확인하세요."
            _set_backup_state(status="failed", last_attempt_at=now, last_error=reason)
    finally:
        _backup_run_lock.release()


def _schedule_backup_timer(delay):
    global _backup_timer
    with _backup_timer_lock:
        if _backup_timer is not None:
            _backup_timer.cancel()
        _backup_timer = threading.Timer(max(0, float(delay)), _run_due_backup)
        _backup_timer.daemon = True
        _backup_timer.start()


def _maybe_schedule_due_backup():
    state = _backup_state()
    if not state:
        return
    now = int(time.time())
    changed_at = int(state.get("last_change_at") or 0)
    if not changed_at:
        return
    status = str(state.get("status") or "")
    if status == "success" and int(state.get("last_success_at") or 0) >= changed_at:
        return
    if not _dropbox_configured():
        return
    if status == "failed" and now < int(state.get("last_attempt_at") or 0) + BACKUP_QUIET_SECONDS:
        return
    _schedule_backup_timer(max(0, changed_at + BACKUP_QUIET_SECONDS - now))


def api_backup_status(handler):
    state = _backup_state() or {}
    json_response(handler, {
        "configured": _dropbox_configured(),
        "status": state.get("status", "idle"),
        "last_change_at": state.get("last_change_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_success_at": state.get("last_success_at"),
        "last_file": state.get("last_file"),
        "last_error": state.get("last_error"),
        "quiet_seconds": BACKUP_QUIET_SECONDS
    })


def api_dropbox_exchange(handler):
    data = parse_body(handler)
    code = str(data.get("code") or "").strip()
    if not code:
        raise ValueError("Dropbox 인증코드를 입력하세요.")
    app_key = os.environ.get("DROPBOX_APP_KEY", "").strip()
    app_secret = os.environ.get("DROPBOX_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        raise ValueError("Render에 DROPBOX_APP_KEY와 DROPBOX_APP_SECRET을 먼저 설정하세요.")
    form = urllib.parse.urlencode({
        "code": code, "grant_type": "authorization_code",
        "client_id": app_key, "client_secret": app_secret
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.dropboxapi.com/oauth2/token", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not return Dropbox response body: it can contain diagnostic details.
        raise ValueError("인증코드가 만료되었거나 이미 사용되었습니다. Dropbox에서 새 코드를 받아 다시 시도하세요.") from exc
    refresh_token = str(result.get("refresh_token") or "")
    if not refresh_token:
        raise ValueError("갱신 토큰을 받지 못했습니다. Dropbox에서 offline access 인증을 다시 진행하세요.")
    # This is returned once to the authenticated user for manual entry in Render.
    # It is never written to the database or application logs.
    json_response(handler, {"ok": True, "refresh_token": refresh_token})


def dropbox_setup_page():
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>머니북 Dropbox 연결</title><style>body{font-family:system-ui,sans-serif;max-width:680px;margin:24px auto;padding:0 16px;line-height:1.6;color:#172033}input,button,textarea{box-sizing:border-box;width:100%;padding:12px;margin:6px 0 12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}button{background:#172033;color:white;cursor:pointer}pre,textarea{overflow-wrap:anywhere;white-space:pre-wrap}small{color:#475569}.warn{background:#fff7ed;padding:12px;border-radius:8px}.hidden{display:none}</style></head><body><h1>머니북 Dropbox 연결 준비</h1><p>이 페이지는 로그인한 머니북 사용자만 이용할 수 있습니다. 인증코드와 갱신 토큰을 채팅에 보내지 마세요.</p><h2>1. 인증코드 교환</h2><label for="code">Dropbox 인증코드</label><input id="code" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="인증코드를 여기에 붙여넣기"><button id="exchange">갱신 토큰 받기</button><p id="message" role="status"></p><div id="tokenBox" class="hidden"><label for="token">갱신 토큰 (Render에만 입력)</label><textarea id="token" rows="4" readonly></textarea><button id="copyToken">토큰 복사</button><p class="warn">토큰은 이 화면에서만 표시됩니다. Render 환경 변수 <b>DROPBOX_REFRESH_TOKEN</b>에 붙여 넣고 저장하세요. 채팅이나 GitHub에는 올리지 마세요.</p></div><hr><h2>2. 백업 암호화 키 생성</h2><p>이 키는 백업 파일을 암호화합니다. 잃어버리면 백업을 복호화할 수 없으므로 비밀번호 관리자 등 안전한 곳에도 보관하세요.</p><button id="makeKey">암호화 키 생성</button><textarea id="key" rows="3" readonly placeholder="버튼을 눌러 키를 생성하세요"></textarea><button id="copyKey">암호화 키 복사</button><p class="warn">Render 환경 변수 <b>BACKUP_ENCRYPTION_KEY</b>에 입력하세요. 이 키도 채팅이나 GitHub에 올리지 마세요.</p><p><small>필요한 Render 환경 변수: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, BACKUP_ENCRYPTION_KEY. DROPBOX_BACKUP_PATH는 선택사항이며 비우면 Dropbox 앱 폴더 최상위에 저장합니다.</small></p><script>const $=id=>document.getElementById(id);$("exchange").onclick=async()=>{const code=$("code").value.trim();if(!code){$("message").textContent="인증코드를 입력하세요.";return} $("exchange").disabled=true;$("message").textContent="인증 중…";$("tokenBox").classList.add("hidden");try{const r=await fetch("/api/dropbox/exchange",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code})});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||"인증에 실패했습니다.");$("token").value=d.refresh_token;$("tokenBox").classList.remove("hidden");$("code").value="";$("message").textContent="완료! 토큰을 Render에 입력하세요."}catch(e){$("message").textContent=e.message||"인증에 실패했습니다."}finally{$("exchange").disabled=false}};$("copyToken").onclick=async()=>{try{await navigator.clipboard.writeText($("token").value);$("message").textContent="토큰을 복사했어요. Render에만 붙여 넣으세요."}catch(e){$("token").focus();$("token").select();$("message").textContent="토큰을 선택했어요. 복사해서 Render에 붙여 넣으세요."}};$("makeKey").onclick=()=>{const bytes=new Uint8Array(32);crypto.getRandomValues(bytes);let s="";for(const b of bytes)s+=String.fromCharCode(b);$("key").value=btoa(s).replace(/\+/g,"-").replace(/\//g,"_")};$("copyKey").onclick=async()=>{if(!$("key").value){$("message").textContent="먼저 암호화 키를 생성하세요.";return}try{await navigator.clipboard.writeText($("key").value);$("message").textContent="암호화 키를 복사했어요. Render와 안전한 보관 장소에 저장하세요."}catch(e){$("key").focus();$("key").select();$("message").textContent="키를 선택했어요. 복사해서 안전하게 보관하세요."}};</script></body></html>"""


AUTH_COOKIE='moneybook_session'
AUTH_TTL=30*24*60*60
CHALLENGE_TTL=5*60
def _now_ts(): return int(time.time())
def _b64(data): return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')
def _unb64(value): return base64.urlsafe_b64decode(str(value)+'='*(-len(str(value))%4))
def _password_hash(password,salt=None):
    salt=salt or secrets.token_bytes(16)
    digest=hashlib.scrypt(str(password).encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
    return 'scrypt$'+_b64(salt)+'$'+_b64(digest)
def _password_check(password,encoded):
    try:
        alg,salt,digest=str(encoded).split('$',2)
        if alg!='scrypt': return False
        candidate=hashlib.scrypt(str(password).encode(),salt=_unb64(salt),n=2**14,r=8,p=1,dklen=32)
        return hmac.compare_digest(candidate,_unb64(digest))
    except Exception: return False
def _auth_secret(conn):
    row=conn.execute("SELECT value FROM auth_meta WHERE key='session_secret'").fetchone()
    if row: return str(row['value'])
    secret=_b64(secrets.token_bytes(32)); conn.execute("INSERT INTO auth_meta(key,value) VALUES(?,?)",('session_secret',secret)); conn.commit(); return secret
def _make_session(conn):
    expires=_now_ts()+AUTH_TTL; payload='1.'+str(expires)
    sig=_b64(hmac.new(_auth_secret(conn).encode(),payload.encode(),hashlib.sha256).digest())
    return payload+'.'+sig
def _valid_session(handler):
    cookies={}
    for part in handler.headers.get('Cookie','').split(';'):
        if '=' in part:
            k,v=part.strip().split('=',1); cookies[k]=v
    parts=cookies.get(AUTH_COOKIE,'').split('.')
    if len(parts)!=3 or parts[0]!='1': return False
    try: expires=int(parts[1])
    except ValueError: return False
    if expires<_now_ts(): return False
    with db() as conn: secret=_auth_secret(conn).encode()
    expected=_b64(hmac.new(secret,('1.'+str(expires)).encode(),hashlib.sha256).digest())
    return hmac.compare_digest(expected,parts[2])
def _set_session_cookie(handler,token):
    secure=' Secure;' if str(handler.headers.get('X-Forwarded-Proto','')).lower()=='https' or handler.server.server_port==443 else ''
    handler.send_header('Set-Cookie',AUTH_COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict;'+secure+' Max-Age='+str(AUTH_TTL))
def _clear_session_cookie(handler):
    secure=' Secure;' if str(handler.headers.get('X-Forwarded-Proto','')).lower()=='https' or handler.server.server_port==443 else ''
    handler.send_header('Set-Cookie',AUTH_COOKIE+'=; Path=/; HttpOnly; SameSite=Strict;'+secure+' Max-Age=0')
def _public_origin(handler):
    origin=os.environ.get('MONEYBOOK_ORIGIN','').strip(); rp_id=os.environ.get('MONEYBOOK_RP_ID','').strip()
    forwarded=str(handler.headers.get('X-Forwarded-Proto','')).split(',')[0].strip()
    proto=forwarded or ('https' if handler.server.server_port==443 else 'http')
    host=str(handler.headers.get('Host','')).split(',')[0].strip()
    if not origin: origin=proto+'://'+host
    if not rp_id: rp_id=urlparse(origin).hostname or host.split(':')[0]
    return rp_id,origin
def _store_challenge(conn,kind,challenge):
    conn.execute("DELETE FROM auth_challenges WHERE kind=?",(kind,)); conn.execute("INSERT INTO auth_challenges(kind,challenge,expires_at) VALUES(?,?,?)",(kind,_b64(challenge),_now_ts()+CHALLENGE_TTL)); conn.commit()
def _take_challenge(conn,kind):
    row=conn.execute("SELECT id,challenge,expires_at FROM auth_challenges WHERE kind=? ORDER BY id DESC LIMIT 1",(kind,)).fetchone()
    if not row or int(row['expires_at'])<_now_ts(): return None
    conn.execute("DELETE FROM auth_challenges WHERE id=?",(row['id'],)); conn.commit(); return _unb64(row['challenge'])
def _ensure_auth_tables():
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS auth_users (id INTEGER PRIMARY KEY,password_hash TEXT NOT NULL,webauthn_user_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS auth_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        passkey_id_type='SERIAL' if USING_POSTGRES else 'INTEGER'
        conn.execute(f"CREATE TABLE IF NOT EXISTS auth_passkeys (id {passkey_id_type} PRIMARY KEY,credential_id TEXT NOT NULL UNIQUE,public_key TEXT NOT NULL,sign_count INTEGER NOT NULL DEFAULT 0,transports TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS auth_challenges (id {passkey_id_type} PRIMARY KEY,kind TEXT NOT NULL,challenge TEXT NOT NULL,expires_at INTEGER NOT NULL)")
        configured=os.environ.get('MONEYBOOK_AUTH_PASSWORD','').strip()
        existing=conn.execute("SELECT id,password_hash FROM auth_users WHERE id=1").fetchone()
        if not existing:
            initial=configured or secrets.token_urlsafe(12)
            conn.execute("INSERT INTO auth_users(id,password_hash,webauthn_user_id,created_at) VALUES(1,?,?,?)",(_password_hash(initial),_b64(secrets.token_bytes(32)),datetime.utcnow().isoformat())); conn.commit()
            print('============================================================'); print('머니북 초기 로그인 비밀번호:',initial); print('============================================================')
        elif configured and not _password_check(configured,existing['password_hash']):
            conn.execute("UPDATE auth_users SET password_hash=? WHERE id=1",(_password_hash(configured),)); conn.commit()
            print('머니북 로그인 비밀번호를 MONEYBOOK_AUTH_PASSWORD 설정값으로 갱신했습니다.')
def _auth_user(conn): return conn.execute("SELECT id,password_hash,webauthn_user_id FROM auth_users WHERE id=1").fetchone()
def api_auth_status(handler):
    with db() as conn:
        row=conn.execute("SELECT COUNT(*) AS n FROM auth_passkeys").fetchone(); has=int(row['n'] if USING_POSTGRES else row[0])>0
    json_response(handler,{'authenticated':_valid_session(handler),'passkey':has,'webauthn':WEBAUTHN_AVAILABLE})
def api_auth_login(handler):
    data=parse_body(handler)
    with db() as conn:
        user=_auth_user(conn)
        if not user or not _password_check(data.get('password',''),user['password_hash']): raise PermissionError('비밀번호가 올바르지 않습니다.')
        token=_make_session(conn)
    raw=b'{"ok":true}'; handler.send_response(HTTPStatus.OK); handler.send_header('Content-Type','application/json; charset=utf-8'); handler.send_header('Content-Length',str(len(raw))); handler.send_header('Cache-Control','no-store'); _set_session_cookie(handler,token); handler.end_headers(); handler.wfile.write(raw)
def api_auth_logout(handler):
    raw=b'{"ok":true}'; handler.send_response(HTTPStatus.OK); handler.send_header('Content-Type','application/json; charset=utf-8'); handler.send_header('Content-Length',str(len(raw))); handler.send_header('Cache-Control','no-store'); _clear_session_cookie(handler); handler.end_headers(); handler.wfile.write(raw)
def _require_webauthn(handler):
    if not WEBAUTHN_AVAILABLE: raise RuntimeError('WebAuthn 모듈이 설치되지 않았습니다. Render 재배포 후 다시 시도하세요.')
def api_passkey_register_options(handler):
    _require_webauthn(handler)
    if not _valid_session(handler): raise PermissionError('로그인 후 Face ID를 등록할 수 있습니다.')
    rp_id,_=_public_origin(handler)
    with db() as conn:
        user=_auth_user(conn); rows=conn.execute("SELECT credential_id FROM auth_passkeys").fetchall()
        exclude=[PublicKeyCredentialDescriptor(id=_unb64(r['credential_id'])) for r in rows]
        options=generate_registration_options(rp_id=rp_id,rp_name='머니북',user_id=_unb64(user['webauthn_user_id']),user_name='moneybook',user_display_name='머니북',authenticator_selection=AuthenticatorSelectionCriteria(authenticator_attachment=AuthenticatorAttachment.PLATFORM,resident_key=ResidentKeyRequirement.REQUIRED,user_verification=UserVerificationRequirement.REQUIRED),exclude_credentials=exclude)
        _store_challenge(conn,'register',options.challenge)
    json_response(handler,json.loads(options_to_json(options)))
def api_passkey_register_verify(handler):
    _require_webauthn(handler)
    if not _valid_session(handler): raise PermissionError('로그인이 필요합니다.')
    data=parse_body(handler); rp_id,origin=_public_origin(handler)
    with db() as conn:
        challenge=_take_challenge(conn,'register')
        if not challenge: raise ValueError('Face ID 등록 시간이 만료되었습니다. 다시 시도하세요.')
        verification=verify_registration_response(credential=data,expected_challenge=challenge,expected_rp_id=rp_id,expected_origin=origin,require_user_verification=True)
        try:
            conn.execute("INSERT INTO auth_passkeys(credential_id,public_key,sign_count,transports,created_at) VALUES(?,?,?,?,?)",(_b64(verification.credential_id),_b64(verification.credential_public_key),int(verification.sign_count),json.dumps(data.get('response',{}).get('transports') or ['internal']),datetime.utcnow().isoformat()))
        except INTEGRITY_ERROR: raise ValueError('이미 등록된 Face ID입니다.')
        conn.commit()
    json_response(handler,{'ok':True})
def api_passkey_auth_options(handler):
    _require_webauthn(handler)
    with db() as conn:
        rows=conn.execute("SELECT credential_id,transports FROM auth_passkeys ORDER BY id").fetchall()
        if not rows: raise ValueError('등록된 Face ID가 없습니다. 먼저 비밀번호로 로그인한 뒤 Face ID를 등록하세요.')
        allow=[PublicKeyCredentialDescriptor(id=_unb64(r['credential_id']),transports=[AuthenticatorTransport.INTERNAL]) for r in rows]
        options=generate_authentication_options(rp_id=_public_origin(handler)[0],allow_credentials=allow,user_verification=UserVerificationRequirement.REQUIRED); _store_challenge(conn,'authenticate',options.challenge)
    json_response(handler,json.loads(options_to_json(options)))
def api_passkey_auth_verify(handler):
    _require_webauthn(handler)
    data=parse_body(handler); rp_id,origin=_public_origin(handler); credential_id=str(data.get('rawId') or data.get('id') or '')
    with db() as conn:
        row=conn.execute("SELECT id,public_key,sign_count FROM auth_passkeys WHERE credential_id=?",(credential_id,)).fetchone()
        if not row: raise PermissionError('등록된 Face ID를 찾을 수 없습니다.')
        challenge=_take_challenge(conn,'authenticate')
        if not challenge: raise ValueError('Face ID 로그인 시간이 만료되었습니다. 다시 시도하세요.')
        verification=verify_authentication_response(credential=data,expected_challenge=challenge,expected_rp_id=rp_id,expected_origin=origin,credential_public_key=_unb64(row['public_key']),credential_current_sign_count=int(row['sign_count']),require_user_verification=True)
        conn.execute("UPDATE auth_passkeys SET sign_count=? WHERE id=?",(int(verification.new_sign_count),row['id'])); token=_make_session(conn); conn.commit()
    raw=b'{"ok":true}'; handler.send_response(HTTPStatus.OK); handler.send_header('Content-Type','application/json; charset=utf-8'); handler.send_header('Content-Length',str(len(raw))); handler.send_header('Cache-Control','no-store'); _set_session_cookie(handler,token); handler.end_headers(); handler.wfile.write(raw)

def normalize_time(value):
    s=str(value or '').strip()
    if not s:
        return ''
    s=s.replace('시', ':').replace('분','').replace('：', ':').strip()
    m=re.fullmatch(r'(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?',s)
    if not m:
        m=re.fullmatch(r'(\d{1,2})\s*(?:시)?\s*(\d{1,2})?\s*(?:분)?',s)
    if not m:
        raise ValueError('시간 형식이 올바르지 않습니다. 예: 9:19')
    hh=int(m.group(1)); mm=int(m.group(2) or 0); ss=int(m.group(3) or 0) if len(m.groups())>=3 else 0
    if not (0<=hh<=23 and 0<=mm<=59 and 0<=ss<=59):
        raise ValueError('시간 형식이 올바르지 않습니다. 예: 9:19')
    return f'{hh:02d}:{mm:02d}'

def api_bootstrap(handler):
    with db() as conn:
        cards = [dict(r) for r in conn.execute("SELECT id,name,active,sort_order,payment_day,period_start_day,period_end_day FROM cards WHERE active=1 ORDER BY sort_order,id")]
        categories = [dict(r) for r in conn.execute("SELECT id,name,active,sort_order FROM categories WHERE active=1 ORDER BY sort_order,id")]
        rules = [dict(r) for r in conn.execute("""
            SELECT ar.keyword, ar.category_id, c.name AS category
            FROM auto_rules ar JOIN categories c ON c.id=ar.category_id
            WHERE ar.active=1 AND c.active=1 ORDER BY ar.sort_order, ar.id
        """).fetchall()]
        settings = get_settings(conn)
    json_response(handler, {'cards': cards, 'categories': categories, 'rules': rules, 'settings': settings})


def apply_auto_category(conn, content):
    if not content:
        return None
    rules = conn.execute("""
        SELECT ar.keyword, ar.category_id
        FROM auto_rules ar JOIN categories c ON c.id=ar.category_id
        WHERE ar.active=1 AND c.active=1 ORDER BY ar.sort_order, ar.id
    """).fetchall()
    upper = content.upper()
    for r in rules:
        if str(r['keyword']).upper() in upper:
            return r['category_id']
    return None


def api_transactions(handler):
    q = parse_qs(urlparse(handler.path).query)
    limit = min(max(int(q.get("limit", ["100"])[0]), 1), 500)
    offset = max(int(q.get("offset", ["0"])[0]), 0)
    search = str(q.get("search", [""])[0]).strip()
    date_from = str(q.get("date_from", [""])[0]).strip()
    date_to = str(q.get("date_to", [""])[0]).strip()
    card_id = str(q.get("card_id", [""])[0]).strip()
    category_id = str(q.get("category_id", [""])[0]).strip()
    where, args = [], []
    if search:
        where.append("(t.content LIKE ? OR c.name LIKE ? OR g.name LIKE ?)")
        x=f"%{search}%"; args += [x,x,x]
    if date_from:
        normalize_date(date_from); where.append("t.tx_date >= ?"); args.append(date_from)
    if date_to:
        normalize_date(date_to); where.append("t.tx_date <= ?"); args.append(date_to)
    if card_id: where.append("t.card_id = ?"); args.append(int(card_id))
    if category_id: where.append("t.category_id = ?"); args.append(int(category_id))
    clause=(" WHERE "+" AND ".join(where)) if where else ""
    with db() as conn:
        sql=("SELECT t.id,t.tx_date,t.tx_time,t.amount,t.card_id,t.category_id,t.content,t.user_percent,t.wife_percent,t.installment_months,"
             "COALESCE(c.name,'(카드 없음)') card_name,COALESCE(g.name,'미정') category_name "
             "FROM transactions t LEFT JOIN cards c ON c.id=t.card_id LEFT JOIN categories g ON g.id=t.category_id "
             +clause+" ORDER BY t.tx_date DESC, CASE WHEN t.tx_time=\'\' THEN 1 ELSE 0 END ASC, t.tx_time DESC,t.id DESC LIMIT ? OFFSET ?")
        rows=conn.execute(sql,(*args,limit,offset)).fetchall()
        count_row=conn.execute("SELECT COUNT(*) AS n FROM transactions t LEFT JOIN cards c ON c.id=t.card_id LEFT JOIN categories g ON g.id=t.category_id"+clause,args).fetchone()
        total=count_row['n'] if USING_POSTGRES else count_row[0]
    json_response(handler, {'transactions':[dict(r) for r in rows], 'total':total})

def api_transaction_by_id(handler, tx_id):
    with db() as conn:
        row=conn.execute("""SELECT t.id,t.tx_date,t.tx_time,t.amount,t.card_id,t.category_id,t.content,t.user_percent,t.wife_percent,t.installment_months,
            COALESCE(c.name,'(카드 없음)') card_name,COALESCE(g.name,'미정') category_name
            FROM transactions t LEFT JOIN cards c ON c.id=t.card_id LEFT JOIN categories g ON g.id=t.category_id WHERE t.id=?""",(tx_id,)).fetchone()
    if not row:
        raise ValueError('지출 내역을 찾을 수 없습니다.')
    json_response(handler, dict(row))


def validate_transaction_payload(conn, data):
    tx_date=normalize_date(data.get("date")); tx_time=normalize_time(data.get("time")); amount=normalize_amount(data.get("amount")); card_id=int(data.get("card_id")); category_id=data.get("category_id"); content=str(data.get("content") or "").strip(); user_percent=int(data.get("user_percent",50)); installment_months=int(data.get("installment_months",1))
    if not 0 <= user_percent <= 100: raise ValueError("내 비율은 0~100% 사이여야 합니다.")
    if not 1 <= installment_months <= 60: raise ValueError("할부 개월은 1~60개월입니다.")
    if not conn.execute("SELECT id FROM cards WHERE id=? AND active=1",(card_id,)).fetchone(): raise ValueError("사용할 수 없는 카드입니다.")
    if category_id in (None,"",0,"0"): category_id=apply_auto_category(conn,content)
    else: category_id=int(category_id)
    if category_id is None: raise ValueError("카테고리를 선택하세요. 자동 분류 대상도 없습니다.")
    if not conn.execute("SELECT id FROM categories WHERE id=? AND active=1",(category_id,)).fetchone(): raise ValueError("사용할 수 없는 카테고리입니다.")
    return tx_date,tx_time,amount,card_id,category_id,content,user_percent,100-user_percent,installment_months

def api_update_transaction(handler, tx_id):
    data=parse_body(handler)
    with db() as conn:
        values=validate_transaction_payload(conn,data)
        cur=conn.execute("UPDATE transactions SET tx_date=?,tx_time=?,amount=?,card_id=?,category_id=?,content=?,user_percent=?,wife_percent=?,installment_months=? WHERE id=?",(*values,tx_id))
        if cur.rowcount==0: raise ValueError("지출 내역을 찾을 수 없습니다.")
        conn.commit()
    json_response(handler,{"ok":True,"id":tx_id})

def api_delete_transaction(handler, tx_id):
    with db() as conn:
        cur=conn.execute("DELETE FROM transactions WHERE id=?",(tx_id,))
        if cur.rowcount==0: raise ValueError("삭제할 지출 내역을 찾을 수 없습니다.")
        conn.commit()
    json_response(handler,{"ok":True,"id":tx_id})

def api_add_transaction(handler):
    data=parse_body(handler)
    with db() as conn:
        values=validate_transaction_payload(conn,data)
        if USING_POSTGRES:
            row=conn.execute("INSERT INTO transactions (tx_date,tx_time,amount,card_id,category_id,content,user_percent,wife_percent,installment_months) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",values).fetchone()
            tx_id=row['id']
        else:
            cur=conn.execute("INSERT INTO transactions (tx_date,tx_time,amount,card_id,category_id,content,user_percent,wife_percent,installment_months) VALUES (?,?,?,?,?,?,?,?,?)",values)
            tx_id=cur.lastrowid
        conn.commit()
    json_response(handler,{"ok":True,"id":tx_id})

def api_check_duplicates(handler):
    data=parse_body(handler)
    rows=data.get('rows')
    if not isinstance(rows,list): raise ValueError('확인할 내역이 없습니다.')
    with db() as conn:
        out=[]
        seen_exact=set(); seen_similar=set()
        for row in rows[:500]:
            d=normalize_date(row.get('date')); tm=normalize_time(row.get('time')); amount=normalize_amount(row.get('amount')); card_id=int(row.get('card_id') or data.get('card_id')); content=str(row.get('content') or '').strip()
            key=(d,tm,amount,card_id,content); simkey=(d,amount,card_id,content)
            exact=conn.execute("SELECT id FROM transactions WHERE tx_date=? AND tx_time=? AND amount=? AND card_id=? AND content=? LIMIT 1",(d,tm,amount,card_id,content)).fetchone()
            similar=conn.execute("SELECT id FROM transactions WHERE tx_date=? AND amount=? AND card_id=? AND content=? LIMIT 1",(d,amount,card_id,content)).fetchone()
            internal_exact=key in seen_exact; internal_similar=simkey in seen_similar
            seen_exact.add(key); seen_similar.add(simkey)
            out.append({'exact':bool(exact) or internal_exact,'similar':bool(similar) or internal_similar,'id': exact['id'] if exact else (similar['id'] if similar else None),'internal':internal_exact or internal_similar})
    json_response(handler,{'duplicates':out})

def api_import_transactions(handler):
    data=parse_body(handler)
    rows=data.get('rows')
    if not isinstance(rows,list) or not rows:
        raise ValueError('가져올 지출 내역이 없습니다.')
    if len(rows)>500:
        raise ValueError('한 번에 최대 500건까지 가져올 수 있습니다.')
    with db() as conn:
        prepared=[]
        for i,row in enumerate(rows,1):
            if not isinstance(row,dict):
                raise ValueError(f'{i}번째 행의 형식이 올바르지 않습니다.')
            try:
                prepared.append(validate_transaction_payload(conn,row))
            except Exception as e:
                raise ValueError(f'{i}번째 행: {e}')
        conn.executemany("INSERT INTO transactions (tx_date,tx_time,amount,card_id,category_id,content,user_percent,wife_percent,installment_months) VALUES (?,?,?,?,?,?,?,?,?)",prepared)
        conn.commit()
    json_response(handler,{'ok':True,'count':len(prepared)})


def api_bulk_update_transactions(handler):
    data=parse_body(handler)
    ids=data.get('ids')
    category_id=data.get('category_id')
    if not isinstance(ids,list) or not ids or len(ids)>500:
        raise ValueError('선택한 지출 내역이 올바르지 않습니다.')
    try: ids=[int(x) for x in ids]
    except Exception: raise ValueError('선택한 지출 내역이 올바르지 않습니다.')
    if len(set(ids))!=len(ids): raise ValueError('중복된 지출 내역이 있습니다.')
    if category_id in (None,'',0,'0'): raise ValueError('카테고리를 선택하세요.')
    category_id=int(category_id)
    with db() as conn:
        if not conn.execute("SELECT id FROM categories WHERE id=? AND active=1",(category_id,)).fetchone(): raise ValueError('사용할 수 없는 카테고리입니다.')
        placeholders=','.join('?' for _ in ids)
        cur=conn.execute(f"UPDATE transactions SET category_id=? WHERE id IN ({placeholders})",(category_id,*ids))
        conn.commit()
    json_response(handler,{'ok':True,'count':cur.rowcount})


def api_bulk_delete_transactions(handler):
    data=parse_body(handler)
    ids=data.get('ids')
    if not isinstance(ids,list) or not ids or len(ids)>500: raise ValueError('선택한 지출 내역이 올바르지 않습니다.')
    try: ids=[int(x) for x in ids]
    except Exception: raise ValueError('선택한 지출 내역이 올바르지 않습니다.')
    if len(set(ids))!=len(ids): raise ValueError('중복된 지출 내역이 있습니다.')
    placeholders=','.join('?' for _ in ids)
    with db() as conn:
        cur=conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})",tuple(ids))
        conn.commit()
    json_response(handler,{'ok':True,'count':cur.rowcount})

def api_notes(handler, note_id=None):
    method = handler.command
    if method == 'GET':
        with db() as conn:
            rows = conn.execute("SELECT id,content,done,created_at,updated_at FROM notes ORDER BY done ASC, id DESC").fetchall()
        json_response(handler, {'notes':[dict(r) for r in rows]})
        return
    data=parse_body(handler)
    content=str(data.get('content') or '').strip()
    if not content:
        raise ValueError('메모 내용을 입력하세요.')
    with db() as conn:
        if method == 'POST' and note_id is None:
            if USING_POSTGRES:
                row=conn.execute("INSERT INTO notes(content,done) VALUES(?,0) RETURNING id",(content,)).fetchone()
                note_id=row['id']
            else:
                cur=conn.execute("INSERT INTO notes(content,done) VALUES(?,0)",(content,))
                note_id=cur.lastrowid
            conn.commit(); json_response(handler,{'ok':True,'id':note_id}); return
        if method == 'POST' and note_id is not None:
            done=1 if data.get('done') else 0
            cur=conn.execute("UPDATE notes SET content=?,done=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(content,done,note_id))
            if not cur.rowcount: raise ValueError('메모를 찾을 수 없습니다.')
            conn.commit(); json_response(handler,{'ok':True,'id':note_id}); return
        raise ValueError('지원하지 않는 메모 요청입니다.')


def api_note_toggle(handler, note_id):
    with db() as conn:
        row=conn.execute("SELECT done FROM notes WHERE id=?",(note_id,)).fetchone()
        if not row: raise ValueError('메모를 찾을 수 없습니다.')
        new_done=0 if row['done'] else 1
        conn.execute("UPDATE notes SET done=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_done,note_id))
        conn.commit()
    json_response(handler,{'ok':True,'id':note_id,'done':new_done})


def api_note_delete(handler, note_id):
    with db() as conn:
        cur=conn.execute("DELETE FROM notes WHERE id=?",(note_id,))
        if not cur.rowcount: raise ValueError('메모를 찾을 수 없습니다.')
        conn.commit()
    json_response(handler,{'ok':True,'id':note_id})


def api_settings(handler, kind=None, item_id=None):
    method = handler.command
    if kind not in ('cards', 'categories', 'rules', 'names'):
        raise ValueError('지원하지 않는 설정입니다.')
    with db() as conn:
        if method == 'GET':
            if kind == 'names':
                settings = get_settings(conn)
                json_response(handler, {'user_name': settings.get('user_name', '나'), 'wife_name': settings.get('wife_name', '배우자')})
                return
            if kind == 'cards':
                rows = conn.execute("SELECT id,name,active,sort_order,payment_day,period_start_day,period_end_day FROM cards ORDER BY active DESC, sort_order,id").fetchall()
            elif kind == 'categories':
                rows = conn.execute("SELECT id,name,active,sort_order FROM categories ORDER BY active DESC, sort_order,id").fetchall()
            else:
                rows = conn.execute("""SELECT ar.id,ar.keyword,ar.category_id,ar.active,ar.sort_order,
                                      COALESCE(c.name,'미정') AS category_name
                                      FROM auto_rules ar LEFT JOIN categories c ON c.id=ar.category_id
                                      ORDER BY ar.active DESC, ar.sort_order, ar.id""").fetchall()
            json_response(handler, {'items':[dict(r) for r in rows]})
            return

        data = parse_body(handler)
        if kind == 'names':
            user_name = str(data.get('user_name') or '').strip()
            wife_name = str(data.get('wife_name') or '').strip()
            if not user_name or not wife_name: raise ValueError('이름을 모두 입력하세요.')
            conn.execute("INSERT INTO settings(key,value) VALUES('user_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (user_name,))
            conn.execute("INSERT INTO settings(key,value) VALUES('wife_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (wife_name,))
            conn.commit(); json_response(handler, {'ok':True}); return

        if kind in ('cards','categories'):
            table = kind
            name = str(data.get('name') or '').strip()
            if not name: raise ValueError('이름을 입력하세요.')
            if method == 'POST' and item_id is None:
                try:
                    max_order_row = conn.execute(f"SELECT COALESCE(MAX(sort_order),-1) AS max_order FROM {table}").fetchone()
                    max_order = max_order_row['max_order'] if USING_POSTGRES else max_order_row[0]
                    if kind=='cards':
                        pd=data.get('payment_day'); sd=data.get('period_start_day'); ed=data.get('period_end_day')
                        pd=int(pd) if pd not in (None,'') else None; sd=int(sd) if sd not in (None,'') else None; ed=int(ed) if ed not in (None,'') else None
                        if pd is not None and not 1<=pd<=31: raise ValueError('결제일은 1~31일입니다.')
                        if sd is not None and not 1<=sd<=31: raise ValueError('이용기간 시작일은 1~31일입니다.')
                        if ed is not None and not 1<=ed<=31: raise ValueError('이용기간 종료일은 1~31일입니다.')
                        cur = conn.execute(f"INSERT INTO {table}(name,active,sort_order,payment_day,period_start_day,period_end_day) VALUES(?,1,?,?,?,?) RETURNING id" if USING_POSTGRES else f"INSERT INTO {table}(name,active,sort_order,payment_day,period_start_day,period_end_day) VALUES(?,1,?,?,?,?)", (name,max_order+1,pd,sd,ed))
                        new_row = cur.fetchone() if USING_POSTGRES else None
                    else:
                        cur = conn.execute(f"INSERT INTO {table}(name,active,sort_order) VALUES(?,1,?) RETURNING id" if USING_POSTGRES else f"INSERT INTO {table}(name,active,sort_order) VALUES(?,1,?)", (name,max_order+1))
                        new_row = cur.fetchone() if USING_POSTGRES else None
                except INTEGRITY_ERROR: raise ValueError('이미 같은 이름이 있습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':new_row['id'] if USING_POSTGRES else cur.lastrowid}); return
            if method == 'POST' and item_id is not None:
                try:
                    if kind=='cards':
                        pd=data.get('payment_day'); sd=data.get('period_start_day'); ed=data.get('period_end_day')
                        pd=int(pd) if pd not in (None,'') else None; sd=int(sd) if sd not in (None,'') else None; ed=int(ed) if ed not in (None,'') else None
                        if pd is None or sd is None or ed is None: raise ValueError('결제일과 이용기간의 시작일·종료일을 모두 입력하세요.')
                        if not 1<=pd<=31 or not 1<=sd<=31 or not 1<=ed<=31: raise ValueError('결제일과 이용기간은 1~31일 사이로 입력하세요.')
                        cur=conn.execute(f"UPDATE {table} SET name=?, active=?, payment_day=?, period_start_day=?, period_end_day=? WHERE id=?", (name,1 if data.get('active',True) else 0,pd,sd,ed,item_id))
                    else:
                        cur=conn.execute(f"UPDATE {table} SET name=?, active=? WHERE id=?", (name,1 if data.get('active',True) else 0,item_id))
                except INTEGRITY_ERROR: raise ValueError('이미 같은 이름이 있습니다.')
                if not cur.rowcount: raise ValueError('항목을 찾을 수 없습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':item_id}); return

        if kind == 'rules':
            keyword=str(data.get('keyword') or '').strip()
            category_id=data.get('category_id')
            if not keyword: raise ValueError('자동분류 키워드를 입력하세요.')
            if category_id in (None,'','0',0): raise ValueError('카테고리를 선택하세요.')
            category_id=int(category_id)
            if not conn.execute("SELECT id FROM categories WHERE id=? AND active=1",(category_id,)).fetchone(): raise ValueError('사용할 수 없는 카테고리입니다.')
            if method == 'POST' and item_id is None:
                try:
                    cur=conn.execute("INSERT INTO auto_rules(keyword,category_id,active) VALUES(?,?,1) RETURNING id" if USING_POSTGRES else "INSERT INTO auto_rules(keyword,category_id,active) VALUES(?,?,1)",(keyword,category_id))
                    new_row=cur.fetchone() if USING_POSTGRES else None
                except INTEGRITY_ERROR: raise ValueError('이미 같은 키워드가 있습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':new_row['id'] if USING_POSTGRES else cur.lastrowid}); return
            if method == 'POST' and item_id is not None:
                try:
                    cur=conn.execute("UPDATE auto_rules SET keyword=?,category_id=?,active=? WHERE id=?",(keyword,category_id,1 if data.get('active',True) else 0,item_id))
                except INTEGRITY_ERROR: raise ValueError('이미 같은 키워드가 있습니다.')
                if not cur.rowcount: raise ValueError('자동분류 규칙을 찾을 수 없습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':item_id}); return
        raise ValueError('지원하지 않는 설정 요청입니다.')


def api_settings_order(handler):
    data=parse_body(handler)
    kind=str(data.get('kind') or '')
    ids=data.get('ids')
    if kind not in ('cards','categories','rules') or not isinstance(ids,list) or not ids:
        raise ValueError('순서 정보가 올바르지 않습니다.')
    table={'cards':'cards','categories':'categories','rules':'auto_rules'}[kind]
    ids=[int(x) for x in ids]
    with db() as conn:
        rows=conn.execute(f"SELECT id FROM {table}").fetchall()
        existing={int(r['id']) for r in rows}
        if set(ids)!=existing or len(ids)!=len(existing):
            raise ValueError('순서 정보가 현재 설정과 일치하지 않습니다.')
        for order,item_id in enumerate(ids):
            conn.execute(f"UPDATE {table} SET sort_order=? WHERE id=?",(order,item_id))
        conn.commit()
    json_response(handler,{'ok':True})


def api_settings_delete(handler, kind, item_id):
    if kind not in ('cards','categories','rules'): raise ValueError('지원하지 않는 설정입니다.')
    table={'cards':'cards','categories':'categories','rules':'auto_rules'}[kind]
    with db() as conn:
        if kind == 'cards':
            # Existing transactions keep their historical card reference; hide the card from new input.
            cur=conn.execute("UPDATE cards SET active=0 WHERE id=?",(item_id,))
        elif kind == 'categories':
            cur=conn.execute("UPDATE categories SET active=0 WHERE id=?",(item_id,))
            conn.execute("UPDATE auto_rules SET active=0 WHERE category_id=?",(item_id,))
        else:
            cur=conn.execute("UPDATE auto_rules SET active=0 WHERE id=?",(item_id,))
        if not cur.rowcount: raise ValueError('항목을 찾을 수 없습니다.')
        conn.commit()
    json_response(handler, {'ok':True,'id':item_id})

def api_settlement(handler):
    q = parse_qs(urlparse(handler.path).query)
    month_value=q.get('month',[date.today().strftime('%Y-%m')])[0]
    try:
        center_dt=date.fromisoformat(month_value+'-01') if re.fullmatch(r'\d{4}-\d{2}',month_value) else date.fromisoformat(normalize_date(month_value)).replace(day=1)
    except Exception:
        raise ValueError('결산 기준 월이 올바르지 않습니다.')
    def shift_month(y,m,offset):
        total=y*12+(m-1)+offset; return total//12,total%12+1
    months=[]
    for off in (-1,0,1):
        yy,mm=shift_month(center_dt.year,center_dt.month,off); months.append(f'{yy:04d}-{mm:02d}')
    def add_month(ymv,n):
        y,m=map(int,ymv.split('-')); yy,mm=shift_month(y,m,n); return f'{yy:04d}-{mm:02d}'
    def safe_date(y,m,d):
        import calendar
        return date(y,m,min(d,calendar.monthrange(y,m)[1]))
    def period_for(target_ym,card):
        y,m=map(int,target_ym.split('-')); sd=card['period_start_day']; ed=card['period_end_day']
        if sd is None or ed is None:
            return date(y,m,1), safe_date(y,m,31)
        py,pm=shift_month(y,m,-1)
        return safe_date(py,pm,int(sd)), safe_date(y,m,int(ed))
    def settlement_month(txdate,card):
        if card['period_start_day'] is None or card['period_end_day'] is None:
            return txdate.strftime('%Y-%m')
        # Test current month and adjacent month; choose the period containing the purchase date.
        for off in (-1,0,1,2):
            yy,mm=shift_month(txdate.year,txdate.month,off); target=f'{yy:04d}-{mm:02d}'
            a,b=period_for(target,card)
            if a<=txdate<=b: return target
        return txdate.strftime('%Y-%m')
    with db() as conn:
        settings={r['key']:r['value'] for r in conn.execute('SELECT key,value FROM settings').fetchall()}
        cards=[dict(r) for r in conn.execute("SELECT id,name,sort_order,payment_day,period_start_day,period_end_day FROM cards WHERE active=1 ORDER BY sort_order,id").fetchall()]
        categories=[dict(r) for r in conn.execute("SELECT id,name,sort_order FROM categories WHERE active=1 ORDER BY sort_order,id").fetchall()]
        rows=conn.execute("SELECT tx_date,tx_time,amount,card_id,category_id,user_percent,wife_percent,installment_months FROM transactions").fetchall()
    card_map={int(c['id']):c for c in cards}
    month_map={m:{'total':0,'user':0,'wife':0,'cards':{str(c['id']):0 for c in cards},'categories':{str(c['id']):0 for c in categories}} for m in months}
    for r in rows:
        card=card_map.get(int(r['card_id']))
        if not card: continue
        purchase=r['tx_date'] if isinstance(r['tx_date'], date) else date.fromisoformat(str(r['tx_date']))
        first=settlement_month(purchase,card)
        n=max(1,int(r['installment_months'] or 1)); per=float(r['amount'])/n
        for i in range(n):
            target=add_month(first,i)
            if target not in month_map: continue
            d=month_map[target]; d['total']+=per; d['user']+=per*int(r['user_percent'])/100; d['wife']+=per*int(r['wife_percent'])/100
            d['cards'][str(r['card_id'])]+=per
            if r['category_id'] is not None and str(r['category_id']) in d['categories']: d['categories'][str(r['category_id'])]+=per
    result=[]
    for m in months:
        d=month_map[m]
        result.append({'month':m,'total':round(d['total']),'user':round(d['user']),'wife':round(d['wife']),
                       'cards':[{'id':c['id'],'name':c['name'],'amount':round(d['cards'][str(c['id'])]),'period_start_day':c['period_start_day'],'period_end_day':c['period_end_day']} for c in cards],
                       'categories':[{'id':c['id'],'name':c['name'],'amount':round(d['categories'][str(c['id'])])} for c in categories]})
    json_response(handler,{'center_month':center_dt.strftime('%Y-%m'),'user_name':settings.get('user_name','나'),'wife_name':settings.get('wife_name','배우자'),'months':result})


def api_settlement_details(handler):
    q=parse_qs(urlparse(handler.path).query)
    month=q.get('month',[''])[0]
    kind=q.get('kind',['total'])[0]
    item_id=q.get('id',[''])[0]
    if not re.fullmatch(r'\d{4}-\d{2}',month): raise ValueError('결산 월이 올바르지 않습니다.')
    allowed={'total','user','wife','card','category'}
    if kind not in allowed: raise ValueError('결산 항목이 올바르지 않습니다.')
    if kind in ('card','category') and not item_id: raise ValueError('결산 항목 ID가 없습니다.')
    center_dt=date.fromisoformat(month+'-01')
    def shift_month(y,m,offset):
        total=y*12+(m-1)+offset; return total//12,total%12+1
    def add_month(ymv,n):
        y,m=map(int,ymv.split('-')); yy,mm=shift_month(y,m,n); return f'{yy:04d}-{mm:02d}'
    def safe_date(y,m,d):
        import calendar
        return date(y,m,min(d,calendar.monthrange(y,m)[1]))
    def period_for(target_ym,card):
        y,m=map(int,target_ym.split('-')); sd=card['period_start_day']; ed=card['period_end_day']
        if sd is None or ed is None: return date(y,m,1),safe_date(y,m,31)
        py,pm=shift_month(y,m,-1); return safe_date(py,pm,int(sd)),safe_date(y,m,int(ed))
    def settlement_month(txdate,card):
        if card['period_start_day'] is None or card['period_end_day'] is None: return txdate.strftime('%Y-%m')
        for off in (-1,0,1,2):
            yy,mm=shift_month(txdate.year,txdate.month,off); target=f'{yy:04d}-{mm:02d}'
            x,y=period_for(target,card)
            if x<=txdate<=y: return target
        return txdate.strftime('%Y-%m')
    with db() as conn:
        cards={int(r['id']):dict(r) for r in conn.execute("SELECT id,name,payment_day,period_start_day,period_end_day FROM cards").fetchall()}
        cats={int(r['id']):dict(r) for r in conn.execute("SELECT id,name FROM categories").fetchall()}
        rows=conn.execute("SELECT id,tx_date,tx_time,amount,card_id,category_id,content,user_percent,wife_percent,installment_months FROM transactions ORDER BY tx_date DESC, tx_time DESC, id DESC").fetchall()
    out=[]
    for r in rows:
        card=cards.get(int(r['card_id']))
        if not card: continue
        purchase=r['tx_date'] if isinstance(r['tx_date'],date) else date.fromisoformat(str(r['tx_date']))
        first=settlement_month(purchase,card); n=max(1,int(r['installment_months'] or 1))
        if month < first or month > add_month(first,n-1): continue
        if kind=='card' and int(item_id)!=int(r['card_id']): continue
        if kind=='category' and int(item_id)!=(int(r['category_id']) if r['category_id'] is not None else -1): continue
        allocation=float(r['amount'])/n
        if kind=='user': value=allocation*int(r['user_percent'])/100
        elif kind=='wife': value=allocation*int(r['wife_percent'])/100
        else: value=allocation
        out.append({'id':r['id'],'date':purchase.isoformat(),'time':r['tx_time'] or '','content':r['content'] or '(내용 없음)','card_id':int(r['card_id']),'category_id':(int(r['category_id']) if r['category_id'] is not None else None),'card_name':card['name'],'category_name':cats.get(int(r['category_id']),{}).get('name','미정') if r['category_id'] is not None else '미정','amount':round(float(r['amount'])),'installment_months':n,'allocation':round(value),'user_percent':int(r['user_percent']),'wife_percent':int(r['wife_percent'])})
    total=sum(x['allocation'] for x in out)
    json_response(handler,{'month':month,'kind':kind,'total':total,'transactions':out})


class Handler(BaseHTTPRequestHandler):
    def _auth_required(self):
        path=urlparse(self.path).path
        return path.startswith('/api/') and not path.startswith('/api/auth/') and path!='/api/auth/status'

    def log_message(self, fmt, *args):
        print('[HTTP]', fmt % args)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/auth/status': api_auth_status(self); return
            if self._auth_required() and not _valid_session(self): json_response(self, {'ok':False,'error':'로그인이 필요합니다.','authenticated':False}, HTTPStatus.UNAUTHORIZED); return
            if path == '/dropbox-setup':
                if not _valid_session(self):
                    text_response(self, '로그인 후 이용하세요.', status=HTTPStatus.UNAUTHORIZED)
                else:
                    text_response(self, dropbox_setup_page())
            elif path == '/api/backup/status':
                api_backup_status(self)
            elif path == '/api/bootstrap':
                api_bootstrap(self)
            elif path == '/api/transactions':
                api_transactions(self)
            elif re.fullmatch(r'/api/transactions/\\d+', path):
                api_transaction_by_id(self, int(path.rsplit('/',1)[1]))
            elif path == '/api/notes':
                api_notes(self)
            elif re.fullmatch(r'/api/notes/toggle/\d+', path):
                api_note_toggle(self, int(path.rsplit('/',1)[1]))
            elif re.fullmatch(r'/api/settings/(cards|categories|rules|names)', path):
                api_settings(self, path.split('/')[3])
            elif path == '/api/settlement':
                api_settlement(self)
            elif path == '/api/settlement/details':
                api_settlement_details(self)
            elif path in ('/', '/index.html'):
                text_response(self, (APP_DIR / 'static' / 'index.html').read_bytes())
            elif path == '/app.js':
                text_response(self, (APP_DIR / 'static' / 'app.js').read_bytes(), 'application/javascript; charset=utf-8')
            elif path == '/style.css':
                text_response(self, (APP_DIR / 'static' / 'style.css').read_bytes(), 'text/css; charset=utf-8')
            elif path == '/manifest.webmanifest':
                text_response(self, (APP_DIR / 'static' / 'manifest.webmanifest').read_bytes(), 'application/manifest+json; charset=utf-8')
            else:
                text_response(self, 'Not found', status=HTTPStatus.NOT_FOUND)
        except Exception as e:
            json_response(self, {'ok': False, 'error': str(e)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path.startswith('/api/auth/'):
                if path == '/api/auth/login': api_auth_login(self)
                elif path == '/api/auth/logout': api_auth_logout(self)
                elif path == '/api/auth/passkey/register/options': api_passkey_register_options(self)
                elif path == '/api/auth/passkey/register/verify': api_passkey_register_verify(self)
                elif path == '/api/auth/passkey/authenticate/options': api_passkey_auth_options(self)
                elif path == '/api/auth/passkey/authenticate/verify': api_passkey_auth_verify(self)
                else: json_response(self, {'ok':False,'error':'Not found'}, HTTPStatus.NOT_FOUND)
                return
            if self._auth_required() and not _valid_session(self): json_response(self, {'ok':False,'error':'로그인이 필요합니다.','authenticated':False}, HTTPStatus.UNAUTHORIZED); return
            if path == '/api/dropbox/exchange':
                api_dropbox_exchange(self)
            elif path == '/api/transactions':
                api_add_transaction(self)
            elif path == '/api/transactions/import':
                api_import_transactions(self)
            elif path == '/api/transactions/check_duplicates':
                api_check_duplicates(self)
            elif path == '/api/notes':
                api_notes(self)
            elif re.fullmatch(r'/api/notes/toggle/\d+', path):
                api_note_toggle(self, int(path.rsplit('/',1)[1]))
            elif re.fullmatch(r'/api/settings/(cards|categories|rules)', path):
                api_settings(self, path.split('/')[3])
            elif re.fullmatch(r'/api/notes/\d+', path):
                api_notes(self, int(path.rsplit('/',1)[1]))
            elif re.fullmatch(r'/api/settings/(cards|categories|rules)/\d+', path):
                parts=path.split('/'); api_settings(self, parts[3], int(parts[4]))
            elif path == '/api/settings/names':
                api_settings(self, 'names')
            elif path == '/api/settings/order':
                api_settings_order(self)
            elif path == '/api/transactions/bulk_update':
                api_bulk_update_transactions(self)
            elif path == '/api/transactions/bulk_delete':
                api_bulk_delete_transactions(self)
            elif re.fullmatch(r'/api/transactions/\d+', path):
                api_update_transaction(self, int(path.rsplit('/', 1)[1]))
            else:
                json_response(self, {'ok': False, 'error': 'Not found'}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            json_response(self, {'ok': False, 'error': str(e)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self):
        try:
            path = urlparse(self.path).path
            if re.fullmatch(r'/api/notes/\d+', path):
                api_note_delete(self, int(path.rsplit('/', 1)[1]))
            elif re.fullmatch(r'/api/transactions/\d+', path):
                api_delete_transaction(self, int(path.rsplit('/', 1)[1]))
            else:
                json_response(self, {'ok': False, 'error': 'Not found'}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            json_response(self, {'ok': False, 'error': str(e)}, HTTPStatus.BAD_REQUEST)


def main():
    ensure_schema()
    _maybe_schedule_due_backup()
    print('머니북 모바일 v0.6.5 (Supabase-ready)')
    print(f'DB: {DB_PATH}')
    print(f'Open: http://127.0.0.1:{PORT}')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.')
    finally:
        server.server_close()

if __name__ == '__main__':
    main()