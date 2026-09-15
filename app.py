from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('MONEYBOOK_DB', APP_DIR / 'moneybook.db')).resolve()
HOST = os.environ.get('MONEYBOOK_HOST', '0.0.0.0')
PORT = int(os.environ.get('MONEYBOOK_PORT', '8765'))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=3000')
    return conn


def json_response(handler, payload, status=HTTPStatus.OK):
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
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
    """Add fields introduced after the original v0.4 schema without touching existing data."""
    with db() as conn:
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
            max_order = conn.execute("SELECT COALESCE(MAX(sort_order),-1) FROM categories").fetchone()[0]
            conn.execute("INSERT INTO categories(name,active,sort_order) VALUES('미정',1,?)", (max_order+1,))
        conn.commit()

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
            WHERE ar.active=1 AND c.active=1 ORDER BY c.sort_order, c.id, ar.id
        """).fetchall()]
        settings = get_settings(conn)
    json_response(handler, {'cards': cards, 'categories': categories, 'rules': rules, 'settings': settings})


def apply_auto_category(conn, content):
    if not content:
        return None
    rules = conn.execute("""
        SELECT ar.keyword, ar.category_id
        FROM auto_rules ar JOIN categories c ON c.id=ar.category_id
        WHERE ar.active=1 AND c.active=1 ORDER BY length(ar.keyword) DESC, ar.id
    """).fetchall()
    upper = content.upper()
    for r in rules:
        if str(r['keyword']).upper() in upper:
            return r['category_id']
    return None


def api_transactions(handler):
    q = parse_qs(urlparse(handler.path).query)
    limit = min(max(int(q.get("limit", ["30"])[0]), 1), 100)
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
             +clause+" ORDER BY t.tx_date DESC, CASE WHEN t.tx_time=\'\' THEN 1 ELSE 0 END ASC, t.tx_time DESC,t.id DESC LIMIT ?")
        rows=conn.execute(sql,(*args,limit)).fetchall()
        total=conn.execute("SELECT COUNT(*) FROM transactions t LEFT JOIN cards c ON c.id=t.card_id LEFT JOIN categories g ON g.id=t.category_id"+clause,args).fetchone()[0]
    json_response(handler, {'transactions':[dict(r) for r in rows], 'total':total})

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
        cur=conn.execute("INSERT INTO transactions (tx_date,tx_time,amount,card_id,category_id,content,user_percent,wife_percent,installment_months) VALUES (?,?,?,?,?,?,?,?,?)",values)
        conn.commit()
    json_response(handler,{"ok":True,"id":cur.lastrowid})

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
            cur=conn.execute("INSERT INTO notes(content,done) VALUES(?,0)",(content,))
            conn.commit(); json_response(handler,{'ok':True,'id':cur.lastrowid}); return
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
                rows = conn.execute("""SELECT ar.id,ar.keyword,ar.category_id,ar.active,
                                      COALESCE(c.name,'미정') AS category_name
                                      FROM auto_rules ar LEFT JOIN categories c ON c.id=ar.category_id
                                      ORDER BY ar.active DESC, c.sort_order, c.id, ar.id""").fetchall()
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
                    max_order = conn.execute(f"SELECT COALESCE(MAX(sort_order),-1) FROM {table}").fetchone()[0]
                    if kind=='cards':
                        pd=data.get('payment_day'); sd=data.get('period_start_day'); ed=data.get('period_end_day')
                        pd=int(pd) if pd not in (None,'') else None; sd=int(sd) if sd not in (None,'') else None; ed=int(ed) if ed not in (None,'') else None
                        if pd is not None and not 1<=pd<=31: raise ValueError('결제일은 1~31일입니다.')
                        if sd is not None and not 1<=sd<=31: raise ValueError('이용기간 시작일은 1~31일입니다.')
                        if ed is not None and not 1<=ed<=31: raise ValueError('이용기간 종료일은 1~31일입니다.')
                        cur = conn.execute(f"INSERT INTO {table}(name,active,sort_order,payment_day,period_start_day,period_end_day) VALUES(?,1,?,?,?,?)", (name,max_order+1,pd,sd,ed))
                    else:
                        cur = conn.execute(f"INSERT INTO {table}(name,active,sort_order) VALUES(?,1,?)", (name,max_order+1))
                except sqlite3.IntegrityError: raise ValueError('이미 같은 이름이 있습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':cur.lastrowid}); return
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
                except sqlite3.IntegrityError: raise ValueError('이미 같은 이름이 있습니다.')
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
                    cur=conn.execute("INSERT INTO auto_rules(keyword,category_id,active) VALUES(?,?,1)",(keyword,category_id))
                except sqlite3.IntegrityError: raise ValueError('이미 같은 키워드가 있습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':cur.lastrowid}); return
            if method == 'POST' and item_id is not None:
                try:
                    cur=conn.execute("UPDATE auto_rules SET keyword=?,category_id=?,active=? WHERE id=?",(keyword,category_id,1 if data.get('active',True) else 0,item_id))
                except sqlite3.IntegrityError: raise ValueError('이미 같은 키워드가 있습니다.')
                if not cur.rowcount: raise ValueError('자동분류 규칙을 찾을 수 없습니다.')
                conn.commit(); json_response(handler, {'ok':True,'id':item_id}); return
        raise ValueError('지원하지 않는 설정 요청입니다.')


def api_settings_order(handler):
    data=parse_body(handler)
    kind=str(data.get('kind') or '')
    ids=data.get('ids')
    if kind not in ('cards','categories') or not isinstance(ids,list) or not ids:
        raise ValueError('순서 정보가 올바르지 않습니다.')
    table=kind
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
        purchase=date.fromisoformat(r['tx_date'])
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('[HTTP]', fmt % args)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/bootstrap':
                api_bootstrap(self)
            elif path == '/api/transactions':
                api_transactions(self)
            elif path == '/api/notes':
                api_notes(self)
            elif re.fullmatch(r'/api/notes/toggle/\d+', path):
                api_note_toggle(self, int(path.rsplit('/',1)[1]))
            elif re.fullmatch(r'/api/settings/(cards|categories|rules|names)', path):
                api_settings(self, path.split('/')[3])
            elif path == '/api/settlement':
                api_settlement(self)
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
            if path == '/api/transactions':
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
    print('머니북 모바일 v0.6.3')
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
