from flask import Flask, request, jsonify, redirect
import logging
import pymysql
import os
import hashlib
import re
import requests
import csv
import io
import json
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

MYSQL_ADDRESS = os.environ.get("MYSQL_ADDRESS")
MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "inventory_db")

DEPT_ALIAS = {'安管部': '秩序部'}
def normalize_department(dept):
    if not dept: return dept
    return DEPT_ALIAS.get(str(dept).strip(), str(dept).strip())

DEPT_VISIBLE_ITEMS = {
    '综合部': None,
    '客服部': ['擦手纸', '坐垫纸', '大卷纸'],
    '工程部': [],
    '秩序部': ['警示带'],
}

def filter_items_for_dept(items, dept):
    if dept not in DEPT_VISIBLE_ITEMS: return []
    whitelist = DEPT_VISIBLE_ITEMS[dept]
    if whitelist is None: return [it for it in items if it['department'] == dept]
    if not whitelist: return []
    return [it for it in items if any(kw in it['name'] for kw in whitelist)]

def get_all_visible_items(all_items):
    visible = []
    seen = set()
    for dept in DEPT_VISIBLE_ITEMS.keys():
        for it in filter_items_for_dept(all_items, dept):
            if it['id'] not in seen:
                visible.append(it); seen.add(it['id'])
    return visible

ALERT_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=2b99041269aa1070355657d9060ae29ef6c064cfeb3b2565f0c2b0ac6877d6c1'
DEPT_CONTACTS = {
    '综合部': ['19874467403'], '工程部': ['18666082342'],
    '秩序部': ['19874467403'], '客服部': ['18666082342'],
}

def get_db():
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0] if MYSQL_ADDRESS else "127.0.0.1",
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME, password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=10, read_timeout=60, write_timeout=60
    )
    conn.ping(reconnect=True)
    return conn

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DATABASE} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS items (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    brand VARCHAR(255) DEFAULT NULL,
                    spec VARCHAR(500) DEFAULT NULL,
                    unit VARCHAR(50) DEFAULT '个',
                    sub_unit VARCHAR(50) DEFAULT NULL,
                    conversion DECIMAL(10,2) DEFAULT NULL,
                    initial_stock DECIMAL(10,2) DEFAULT 0,
                    low_stock_threshold DECIMAL(10,2) DEFAULT 5,
                    department VARCHAR(100) DEFAULT NULL,
                    asset_code VARCHAR(50) DEFAULT NULL,
                    qr_code VARCHAR(50) DEFAULT NULL,
                    is_active TINYINT DEFAULT 0,
                    is_deleted TINYINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    item_id VARCHAR(50) NOT NULL,
                    type VARCHAR(10) NOT NULL,
                    quantity DECIMAL(10,2) NOT NULL,
                    department VARCHAR(50) DEFAULT NULL,
                    note VARCHAR(255),
                    operator VARCHAR(50) DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    action VARCHAR(50) NOT NULL,
                    item_id VARCHAR(50) DEFAULT NULL,
                    item_name VARCHAR(255) DEFAULT NULL,
                    detail TEXT,
                    operator VARCHAR(50) DEFAULT '管理员',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS backups (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    items_data LONGTEXT,
                    transactions_data LONGTEXT,
                    note VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            for sql in [
                "ALTER TABLE items ADD COLUMN brand VARCHAR(255) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN spec VARCHAR(500) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN sub_unit VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN conversion DECIMAL(10,2) DEFAULT NULL",
                "ALTER TABLE items MODIFY initial_stock DECIMAL(10,2) DEFAULT 0",
                "ALTER TABLE items MODIFY low_stock_threshold DECIMAL(10,2) DEFAULT 5",
                "ALTER TABLE items ADD COLUMN asset_code VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN qr_code VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN is_active TINYINT DEFAULT 0",
                "ALTER TABLE items ADD COLUMN is_deleted TINYINT DEFAULT 0",
                "ALTER TABLE transactions MODIFY quantity DECIMAL(10,2)",
                "ALTER TABLE transactions ADD COLUMN operator VARCHAR(50) DEFAULT NULL"
            ]:
                try: cursor.execute(sql)
                except Exception: pass
            for sql in [
                'CREATE INDEX idx_item_id ON transactions(item_id)',
                'CREATE INDEX idx_name ON items(name)',
                'CREATE INDEX idx_department ON transactions(department)',
                'CREATE INDEX idx_is_active ON items(is_active)'
            ]:
                try: cursor.execute(sql)
                except Exception: pass
    finally:
        conn.close()

init_db()

def log_audit(cursor, action, item_id, item_name, detail, operator='管理员'):
    cursor.execute('INSERT INTO audit_logs (action, item_id, item_name, detail, operator) VALUES (%s, %s, %s, %s, %s)',
                   (action, item_id, item_name, detail, operator))

def auto_backup(note):
    """自动备份当前 items 和 transactions 数据"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items')
            items = cursor.fetchall()
            cursor.execute('SELECT * FROM transactions')
            trans = cursor.fetchall()
            items_json = json.dumps(items, default=str, ensure_ascii=False)
            trans_json = json.dumps(trans, default=str, ensure_ascii=False)
            cursor.execute('INSERT INTO backups (name, items_data, transactions_data, note) VALUES (%s, %s, %s, %s)',
                           (f'自动备份-{datetime.now().strftime("%Y%m%d_%H%M%S")}', items_json, trans_json, note))
            # 只保留最近10个备份
            cursor.execute('DELETE FROM backups WHERE id NOT IN (SELECT id FROM (SELECT id FROM backups ORDER BY id DESC LIMIT 10) AS t)')
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"自动备份失败: {e}")
        return False
    finally:
        conn.close()

def format_qty(qty, unit, sub_unit=None, conversion=None):
    qty = float(qty or 0)
    if qty == int(qty): qty_str = str(int(qty))
    else: qty_str = f"{qty:.2f}".rstrip('0').rstrip('.')
    if sub_unit and conversion and float(conversion) > 0:
        sub_qty = qty * float(conversion)
        if sub_qty == int(sub_qty): sub_str = str(int(sub_qty))
        else: sub_str = f"{sub_qty:.1f}".rstrip('0').rstrip('.')
        return f"{qty_str} {unit}（={sub_str} {sub_unit}）"
    return f"{qty_str} {unit}"

def get_all_items_with_stock(include_deleted=False, only_active=False):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            where_parts = []
            if not include_deleted: where_parts.append("i.is_deleted = 0")
            if only_active: where_parts.append("i.is_active = 1")
            where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
            cursor.execute(f'''
                SELECT i.id, i.name, i.brand, i.spec, i.unit, i.sub_unit, i.conversion,
                       i.initial_stock, i.low_stock_threshold, i.department, i.asset_code, i.qr_code, i.is_active, i.is_deleted,
                       COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                       COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i LEFT JOIN transactions t ON i.id = t.item_id
                {where}
                GROUP BY i.id
            ''')
            rows = cursor.fetchall()
            result = []
            for row in rows:
                real_stock = float(row['initial_stock'] or 0) + float(row['total_in'] or 0) - float(row['total_out'] or 0)
                result.append({
                    'id': row['id'], 'name': row['name'],
                    'brand': row.get('brand') or '', 'spec': row.get('spec') or '',
                    'unit': row['unit'], 'sub_unit': row.get('sub_unit') or '',
                    'conversion': float(row['conversion']) if row.get('conversion') else None,
                    'real_stock': real_stock,
                    'total_in': float(row['total_in'] or 0),
                    'total_out': float(row['total_out'] or 0),
                    'threshold': float(row['low_stock_threshold'] or 5),
                    'department': normalize_department(row.get('department')) or '未分配',
                    'asset_code': row.get('asset_code') or '', 'qr_code': row.get('qr_code') or '',
                    'is_active': row.get('is_active') or 0,
                    'is_low': real_stock <= float(row['low_stock_threshold'] or 5),
                    'is_deleted': row['is_deleted']
                })
            return result
    finally:
        conn.close()

def send_alert_to_group(message, at_mobiles):
    if not ALERT_WEBHOOK: return False
    at_text = " ".join([f"@{m}" for m in at_mobiles]) if at_mobiles else ""
    full_content = f"{message}\n\n{at_text}" if at_text else message
    data = {"msgtype": "markdown", "markdown": {"title": "库存预警", "text": full_content},
            "at": {"atMobiles": at_mobiles, "isAtAll": False}}
    try:
        resp = requests.post(ALERT_WEBHOOK, json=data, headers={'Content-Type': 'application/json;charset=utf-8'}, timeout=5)
        return resp.status_code == 200 and resp.json().get('errcode') == 0
    except Exception as e:
        logging.error(f"钉钉推送异常: {e}")
        return False

def render_header():
    return '''
    <div class="main-header">
        <div class="header-left">
            <img src="/static/logo.png" class="logo" alt="越秀服务" onerror="this.style.display='none'">
            <div class="title-block">
                <div class="project-name">环球梦大厦</div>
                <div class="sub-title">物资库存预警看板</div>
            </div>
        </div>
        <div class="header-right">
            <a href="/portal" class="header-btn">🏠 导航</a>
            <a href="/admin" class="header-btn">⚙️ 管理</a>
        </div>
    </div>
    '''

BASE_STYLE = '''
    :root {
        --brand: #FF6B1A; --brand-dark: #E85A0E;
        --ink: #1a1f2e; --ink-light: #5a6172; --ink-lighter: #8a91a0;
        --bg: #f5f7fa; --card: #ffffff; --border: #e8ecf1;
        --danger: #e74c3c; --success: #2ecc71;
    }
    * { box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC", "Segoe UI", Roboto, sans-serif;
        background: var(--bg); margin: 0; padding: 16px; color: var(--ink);
        -webkit-font-smoothing: antialiased;
    }
    .main-header { display: flex; align-items: center; justify-content: space-between;
        background: linear-gradient(135deg, #1a1f2e 0%, #252b3d 100%);
        padding: 18px 24px; border-radius: 18px;
        box-shadow: 0 8px 32px rgba(26, 31, 46, 0.18); margin-bottom: 20px;
        position: relative; overflow: hidden; }
    .main-header::after { content: ''; position: absolute; top: -50%; right: -10%;
        width: 280px; height: 280px;
        background: radial-gradient(circle, rgba(255, 107, 26, 0.15) 0%, transparent 70%); pointer-events: none; }
    .main-header .header-left { display: flex; align-items: center; z-index: 1; }
    .main-header .logo { height: 44px; margin-right: 16px; background: white;
        padding: 6px 10px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
    .main-header .project-name { font-size: 22px; font-weight: 800; color: #fff; letter-spacing: 3px; line-height: 1.2; }
    .main-header .sub-title { font-size: 11px; color: rgba(255,255,255,0.55); margin-top: 4px; letter-spacing: 4px; }
    .main-header .header-right { display: flex; gap: 8px; z-index: 1; }
    .main-header .header-btn { display: inline-flex; align-items: center; padding: 8px 14px;
        background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12);
        border-radius: 10px; color: rgba(255,255,255,0.9); text-decoration: none;
        font-size: 13px; font-weight: 500; transition: all 0.2s; }
    .main-header .header-btn:hover { background: rgba(255,107,26,0.9); border-color: transparent; }
    .section-title { font-size: 17px; font-weight: 700; color: var(--ink); margin: 26px 0 14px;
        display: flex; align-items: center; gap: 10px; }
    .section-title span { background: var(--brand); color: white; font-size: 12px;
        padding: 3px 10px; border-radius: 12px; font-weight: 500; }
    .card { background: var(--card); border-radius: 16px; padding: 20px;
        box-shadow: 0 2px 12px rgba(26, 31, 46, 0.05); border: 1px solid var(--border); }
    .scroll-card { background: var(--card); border-radius: 16px; padding: 8px 16px;
        box-shadow: 0 2px 12px rgba(26, 31, 46, 0.05); border: 1px solid var(--border);
        max-height: 420px; overflow-y: auto; }
    .scroll-card::-webkit-scrollbar { width: 6px; }
    .scroll-card::-webkit-scrollbar-thumb { background: #d0d6e0; border-radius: 3px; }
    .low-item { background: linear-gradient(90deg, #fff5f5 0%, #ffffff 100%);
        border-left: 3px solid var(--danger); padding: 14px 16px; margin: 8px 0;
        border-radius: 10px; display: flex; justify-content: space-between; align-items: center; transition: all 0.15s; }
    .low-item:hover { transform: translateX(2px); box-shadow: 0 2px 8px rgba(231, 76, 60, 0.1); }
    .low-item .name { font-weight: 600; color: var(--ink); font-size: 14px; }
    .low-item .meta { color: var(--ink-lighter); font-size: 12px; margin-left: 8px; }
    .low-item .stock { color: var(--danger); font-weight: 700; font-size: 20px; }
    .ok-msg { text-align: center; color: var(--success); padding: 32px 20px; font-weight: 600; font-size: 15px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th { color: var(--ink-lighter); text-align: left; padding: 12px 8px 10px; font-weight: 500;
        font-size: 12px; letter-spacing: 0.5px; text-transform: uppercase; border-bottom: 2px solid var(--border); }
    td { padding: 12px 8px; border-bottom: 1px solid var(--border); color: var(--ink); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fafbfc; }
    td.num { text-align: right; color: var(--ink); font-weight: 700; font-size: 15px; }
    .quick-links { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin: 16px 0; }
    .quick-links a { display: inline-flex; align-items: center; padding: 8px 16px; background: var(--card);
        border: 1px solid var(--border); border-radius: 22px; color: var(--ink-light);
        text-decoration: none; font-size: 13px; font-weight: 500; transition: all 0.2s; }
    .quick-links a:hover { border-color: var(--brand); color: var(--brand); transform: translateY(-1px); }
    .quick-links a.active { background: var(--brand); color: white; border-color: var(--brand); }
    .search-bar { display: flex; gap: 8px; margin: 16px 0; }
    .search-bar input { flex: 1; padding: 14px 18px; border: 1px solid var(--border);
        border-radius: 12px; font-size: 15px; outline: none; background: var(--card); color: var(--ink); }
    .search-bar input:focus { border-color: var(--brand); box-shadow: 0 0 0 3px rgba(255, 107, 26, 0.1); }
    .search-bar button { padding: 14px 28px; background: var(--brand); color: white; border: none;
        border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer; }
    .search-bar button:hover { background: var(--brand-dark); }
    .item-sub { color: var(--ink-lighter); font-size: 11px; margin-top: 3px; line-height: 1.3; }
    .footer { text-align: center; color: var(--ink-lighter); font-size: 12px; margin-top: 40px; padding-bottom: 24px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 600; margin-left: 6px; }
    .badge-active { background: #d5f5e3; color: #1e8449; }
    .badge-inactive { background: #f0f2f5; color: #8a91a0; }
'''

@app.route('/')
def home():
    return "🎉 环球梦大厦库存看板系统运行中！请扫码访问。"

@app.route('/dashboard')
def dashboard_redirect():
    return redirect('/inventory')

@app.route('/inventory')
def inventory():
    dept = normalize_department(request.args.get('dept', '').strip())
    query = request.args.get('q', '').strip()
    # 只显示 is_active=1 的物资
    all_items = get_all_items_with_stock(only_active=True)
    
    if dept:
        dept_items = filter_items_for_dept(all_items, dept)
        dept_items = [d for d in dept_items if d['real_stock'] > 0]
        if query: dept_items = [d for d in dept_items if query.lower() in d['name'].lower()]
        dept_items.sort(key=lambda x: x['name'])
        dept_dict = {dept: dept_items} if dept_items else {}
    else:
        visible = get_all_visible_items(all_items)
        visible = [d for d in visible if d['real_stock'] > 0]
        if query: visible = [d for d in visible if query.lower() in d['name'].lower()]
        dept_dict = {}
        for item in visible:
            dept_dict.setdefault(item['department'], []).append(item)
        for d in dept_dict: dept_dict[d].sort(key=lambda x: x['name'])
    
    quick = '<div class="quick-links">'
    for d in ['综合部', '工程部', '秩序部', '客服部']:
        quick += f'<a href="/inventory?dept={d}" class="{"active" if d==dept else ""}">{d}</a>'
    quick += f'<a href="/inventory" class="{"active" if not dept else ""}">🌐 全局</a><a href="/alert">🚨 预警</a></div>'
    
    search = f'<form method="get" action="/inventory" class="search-bar"><input type="hidden" name="dept" value="{dept}"><input type="text" name="q" value="{query}" placeholder="🔍 搜索物资名称"><button type="submit">搜索</button></form>'
    
    def render_table(items):
        rows = ''
        for item in items:
            sub = f'{item["brand"]} {item["spec"]}'.strip()
            sub_html = f'<div class="item-sub">{sub}</div>' if sub else ''
            rows += f'''<tr>
                <td><a href="/item/{item["id"]}/history" style="color:#1a1f2e;text-decoration:none;">{item["name"]}</a>{sub_html}</td>
                <td class="num">{format_qty(item["real_stock"], item["unit"], item["sub_unit"], item["conversion"])}</td>
            </tr>'''
        return f'<table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{rows}</tbody></table>'
    
    if dept:
        my_items = dept_dict.get(dept, [])
        if my_items:
            body = f'<div class="section-title">📦 {dept}库存清单<span>{len(my_items)} 项</span></div><div class="scroll-card">{render_table(my_items)}</div>'
        else:
            body = f'<div class="card" style="text-align:center;color:#8a91a0;padding:40px;">{dept}暂无有库存的物资</div>'
    else:
        if dept_dict:
            total = sum(len(v) for v in dept_dict.values())
            body = f'<div class="section-title">📦 各部门库存总览<span>{total} 项</span></div>'
            for d, items in dept_dict.items():
                body += f'<div class="scroll-card" style="margin-bottom:12px;"><div style="font-weight:700;font-size:15px;margin-bottom:10px;padding:8px 0;">📁 {d}（{len(items)} 项）</div>{render_table(items)}</div>'
        else:
            body = '<div class="card" style="text-align:center;color:#8a91a0;padding:40px;">暂无有库存的物资<br><br><a href="/admin" style="color:#FF6B1A;">去管理后台启用物资 →</a></div>'
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>库存看板 · 环球梦大厦</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}{quick}{search}{body}
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/alert')
def alert_dashboard():
    dept = normalize_department(request.args.get('dept', '').strip())
    all_items = get_all_items_with_stock(only_active=True)
    
    if dept:
        dept_items = filter_items_for_dept(all_items, dept)
        low_items = [d for d in dept_items if d['is_low']]
        low_items.sort(key=lambda x: x['real_stock'])
        dept_low = {dept: low_items} if low_items else {}
    else:
        visible = get_all_visible_items(all_items)
        low_items = [d for d in visible if d['is_low']]
        low_items.sort(key=lambda x: x['real_stock'])
        dept_low = {}
        for item in low_items: dept_low.setdefault(item['department'], []).append(item)
    
    quick = '<div class="quick-links">'
    for d in ['综合部', '工程部', '秩序部', '客服部']:
        quick += f'<a href="/alert?dept={d}" class="{"active" if d==dept else ""}">{d}</a>'
    quick += f'<a href="/alert" class="{"active" if not dept else ""}">🌐 全部预警</a><a href="/inventory">📦 库存</a></div>'
    
    def render_low(items):
        cards = ''
        for it in items:
            sub = f'{it["brand"]} {it["spec"]}'.strip()
            cards += f'''<div class="low-item">
                <div>
                    <span class="name">{it["name"]}</span>
                    <span class="meta">{sub}</span>
                    <span class="meta">预警值 {format_qty(it["threshold"], it["unit"])}</span>
                </div>
                <div class="stock">{format_qty(it["real_stock"], it["unit"], it["sub_unit"], it["conversion"])}</div>
            </div>'''
        return cards
    
    if dept:
        my_low = dept_low.get(dept, [])
        if my_low:
            body = f'<div class="section-title">🚨 {dept}预警物资<span>{len(my_low)} 项</span></div><div class="scroll-card">{render_low(my_low)}</div>'
        else:
            body = f'<div class="card ok-msg">✅ {dept}所有物资库存充足</div>'
    else:
        if dept_low:
            total = sum(len(v) for v in dept_low.values())
            body = f'<div class="section-title">🚨 全部预警物资<span>{total} 项</span></div>'
            for d, items in dept_low.items():
                body += f'<div class="scroll-card" style="margin-bottom:12px;"><div style="font-weight:700;font-size:15px;margin-bottom:10px;padding:8px 0;color:#e74c3c;">📁 {d}（{len(items)} 项）</div>{render_low(items)}</div>'
        else:
            body = '<div class="card ok-msg">✅ 所有物资库存充足，无预警</div>'
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>预警看板 · 环球梦大厦</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}{quick}{body}
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/item/<item_id>')
def item_page(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''SELECT i.*, 
                COALESCE(SUM(CASE WHEN t.type='in' THEN t.quantity ELSE 0 END),0) AS total_in,
                COALESCE(SUM(CASE WHEN t.type='out' THEN t.quantity ELSE 0 END),0) AS total_out
                FROM items i LEFT JOIN transactions t ON i.id=t.item_id WHERE i.id=%s AND i.is_deleted=0 GROUP BY i.id''', (item_id,))
            row = cursor.fetchone()
            if not row: return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            real_stock = float(row['initial_stock'] or 0) + float(row['total_in'] or 0) - float(row['total_out'] or 0)
            data = {'name': row['name'], 'brand': row.get('brand') or '', 'spec': row.get('spec') or '',
                    'unit': row['unit'], 'sub_unit': row.get('sub_unit') or '',
                    'conversion': float(row['conversion']) if row.get('conversion') else None,
                    'real_stock': real_stock, 'total_in': float(row['total_in'] or 0),
                    'total_out': float(row['total_out'] or 0), 'asset_code': row.get('asset_code') or '',
                    'is_low': real_stock <= float(row['low_stock_threshold'] or 5)}
    finally:
        conn.close()
    c = '#e74c3c' if data['is_low'] else '#2ecc71'
    sub = f'{data["brand"]} {data["spec"]}'.strip()
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>{data['name']}</title><style>{BASE_STYLE}
    .item-card {{ background: white; border-radius: 20px; padding: 32px 24px; box-shadow: 0 4px 24px rgba(26,31,46,0.06); border: 1px solid #e8ecf1; margin-bottom: 16px; text-align: center; }}
    .item-name {{ font-size: 24px; font-weight: 800; color: #1a1f2e; margin-bottom: 8px; }}
    .item-subtitle {{ color: #8a91a0; font-size: 13px; margin-bottom: 20px; }}
    .label {{ font-size: 13px; color: #8a91a0; margin-bottom: 12px; letter-spacing: 1px; }}
    .stock-number {{ font-size: 72px; font-weight: 800; color: {c}; line-height: 1; margin: 8px 0; letter-spacing: -3px; }}
    .warning {{ background: linear-gradient(90deg, #fff3cd 0%, #ffffff 100%); border-left: 4px solid #ffc107; color: #856404; padding: 12px 18px; border-radius: 10px; margin-top: 20px; font-size: 14px; font-weight: 600; display: inline-block; }}
    .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .stat-box {{ background: white; border-radius: 18px; padding: 22px; text-align: center; box-shadow: 0 2px 12px rgba(26,31,46,0.05); border: 1px solid #e8ecf1; }}
    .stat-value {{ font-size: 26px; font-weight: 800; margin-top: 10px; }}
    </style></head><body>
    {render_header()}
    <div class="item-card"><div class="item-name">{data['name']}</div>
    <div class="item-subtitle">{sub}</div>
    <div class="label">实时库存</div>
    <div class="stock-number">{format_qty(data['real_stock'], data['unit'])}</div>
    {f'<div style="color:#8a91a0;font-size:14px;margin-top:8px;">约 {data["real_stock"] * data["conversion"]:.0f} {data["sub_unit"]}</div>' if data['sub_unit'] and data['conversion'] else ''}
    {f'<div class="warning">⚠️ 库存不足，请及时采购！</div>' if data['is_low'] else ''}</div>
    <div class="stats-grid">
      <div class="stat-box"><div class="label">累计入库</div><div class="stat-value" style="color:#2ecc71;">+{format_qty(data['total_in'], data['unit'])}</div></div>
      <div class="stat-box"><div class="label">累计出库</div><div class="stat-value" style="color:#e74c3c;">-{format_qty(data['total_out'], data['unit'])}</div></div>
    </div>
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/item/<item_id>/history')
def item_history(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items WHERE id=%s', (item_id,))
            item = cursor.fetchone()
            if not item: return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            cursor.execute('SELECT * FROM transactions WHERE item_id=%s ORDER BY created_at DESC LIMIT 500', (item_id,))
            trans = cursor.fetchall()
    finally:
        conn.close()
    rows = ''
    for t in trans:
        tl = '入库' if t['type'] == 'in' else '出库'
        tc = '#2ecc71' if t['type'] == 'in' else '#e74c3c'
        rows += f'''<tr><td style="font-size:12px;color:#8a91a0;">{t['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
        <td style="color:{tc};font-weight:600;">{tl}</td>
        <td class="num" style="color:{tc};">{'+' if t['type']=='in' else '-'}{format_qty(t['quantity'], item['unit'])}</td>
        <td style="font-size:12px;">{t.get('department') or ''}</td>
        <td style="font-size:12px;color:#8a91a0;">{t.get('note') or ''}</td></tr>'''
    if not rows: rows = '<tr><td colspan="5" style="text-align:center;color:#8a91a0;padding:30px;">暂无记录</td></tr>'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{item['name']} - 流水</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}
    <div class="card" style="margin-bottom:16px;"><div style="font-size:18px;font-weight:700;">📋 {item['name']} · 出入库流水</div>
    <div style="color:#8a91a0;font-size:13px;margin-top:6px;">共 {len(trans)} 条</div></div>
    <div style="background:white;border-radius:16px;padding:8px 16px;box-shadow:0 2px 12px rgba(26,31,46,0.05);border:1px solid #e8ecf1;overflow-x:auto;">
    <table><thead><tr><th>时间</th><th>类型</th><th style="text-align:right;">数量</th><th>部门</th><th>备注</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/admin')
def admin_page():
    keyword = request.args.get('q', '').strip()
    dept_filter = request.args.get('dept', '').strip()
    show_all = request.args.get('show_all', '0') == '1'
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            sql = '''SELECT i.*, COALESCE(SUM(CASE WHEN t.type='in' THEN t.quantity ELSE 0 END),0) AS total_in,
                    COALESCE(SUM(CASE WHEN t.type='out' THEN t.quantity ELSE 0 END),0) AS total_out
                    FROM items i LEFT JOIN transactions t ON i.id=t.item_id WHERE i.is_deleted=0'''
            params = []
            if not show_all: sql += " AND i.is_active=1"
            if keyword: sql += " AND i.name LIKE %s"; params.append(f"%{keyword}%")
            if dept_filter: sql += " AND i.department LIKE %s"; params.append(f"%{dept_filter}%")
            sql += " GROUP BY i.id ORDER BY i.is_active DESC, i.department, i.name LIMIT 500"
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            
            cursor.execute('SELECT COUNT(*) AS cnt FROM items WHERE is_deleted=0 AND is_active=1')
            active_count = cursor.fetchone()['cnt']
            cursor.execute('SELECT COUNT(*) AS cnt FROM items WHERE is_deleted=0')
            total_count = cursor.fetchone()['cnt']
    finally:
        conn.close()
    
    rows_html = ''
    for r in rows:
        rs = float(r['initial_stock'] or 0) + float(r['total_in'] or 0) - float(r['total_out'] or 0)
        sc = '#e74c3c' if rs <= float(r['low_stock_threshold'] or 5) else '#1a1f2e'
        active_checked = 'checked' if r['is_active'] else ''
        badge = f'<span class="badge badge-active">已启用</span>' if r['is_active'] else f'<span class="badge badge-inactive">未启用</span>'
        rows_html += f'''<tr data-id="{r['id']}">
            <td><input type="checkbox" class="is_active" {active_checked} onchange="toggleActive('{r['id']}', this.checked)" style="width:18px;height:18px;"></td>
            <td><input class="name" type="text" value="{r['name']}" style="width:140px;padding:4px;">{badge}</td>
            <td><input class="brand" type="text" value="{r.get('brand') or ''}" style="width:70px;padding:4px;"></td>
            <td><input class="unit" type="text" value="{r['unit']}" style="width:40px;padding:4px;"></td>
            <td><input class="department" type="text" value="{r['department'] or ''}" style="width:80px;padding:4px;"></td>
            <td><input class="initial_stock" type="number" step="0.01" value="{r['initial_stock']}" style="width:60px;padding:4px;"></td>
            <td><input class="low_stock_threshold" type="number" step="0.01" value="{r['low_stock_threshold']}" style="width:60px;padding:4px;"></td>
            <td style="font-weight:600;color:{sc};font-size:12px;">{format_qty(rs, r['unit'])}</td>
            <td style="white-space:nowrap;">
                <button onclick="saveItem(this, '{r['id']}')" style="padding:3px 7px;background:#FF6B1A;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">保存</button>
                <button onclick="adjustStock('{r['id']}', 'in')" style="padding:3px 7px;background:#3498db;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">入</button>
                <button onclick="adjustStock('{r['id']}', 'out')" style="padding:3px 7px;background:#f39c12;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">出</button>
                <a href="/item/{r['id']}/history" target="_blank" style="padding:3px 7px;background:#9b59b6;color:#fff;border-radius:4px;text-decoration:none;font-size:12px;">流水</a>
            </td></tr>'''
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>物资管理</title><style>
    body {{ font-family: -apple-system, sans-serif; background: #f5f7fa; margin: 0; padding: 16px; color: #1a1f2e; }}
    h1 {{ font-size: 20px; }}
    .top-links a {{ display: inline-block; padding: 8px 16px; background: white; border-radius: 10px; color: #1a1f2e; text-decoration: none; margin-right: 8px; margin-bottom: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.04); font-size: 14px; border: 1px solid #e8ecf1; }}
    .top-links a.danger {{ color: #e74c3c; border-color: #f5c6c2; }}
    .toolbar {{ background: white; padding: 14px; border-radius: 12px; margin-bottom: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); border: 1px solid #e8ecf1; }}
    .toolbar input, .toolbar select {{ padding: 8px 12px; border: 1px solid #e8ecf1; border-radius: 8px; margin-right: 6px; font-size: 14px; }}
    .toolbar button {{ padding: 8px 18px; background: #FF6B1A; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 14px; }}
    table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04); border: 1px solid #e8ecf1; }}
    th {{ background: #f8f9fa; padding: 10px 6px; text-align: left; font-size: 12px; color: #8a91a0; border-bottom: 2px solid #e8ecf1; text-transform: uppercase; }}
    td {{ padding: 6px; border-bottom: 1px solid #f0f2f5; font-size: 13px; }}
    tr:hover td {{ background: #fafbfc; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 600; margin-left: 6px; }}
    .badge-active {{ background: #d5f5e3; color: #1e8449; }}
    .badge-inactive {{ background: #f0f2f5; color: #8a91a0; }}
    .info-box {{ background: #fff8f0; border-left: 4px solid #FF6B1A; padding: 12px 16px; border-radius: 8px; margin-bottom: 14px; font-size: 14px; }}
    </style><script>
    function saveItem(btn, id) {{ const r = btn.closest('tr');
      const d = {{id, name: r.querySelector('.name').value, brand: r.querySelector('.brand').value,
        unit: r.querySelector('.unit').value, department: r.querySelector('.department').value,
        initial_stock: parseFloat(r.querySelector('.initial_stock').value||'0'),
        low_stock_threshold: parseFloat(r.querySelector('.low_stock_threshold').value||'5')}};
      fetch('/api/admin/update', {{method:'POST',headers:{{'Content-Type':'application/json'}},body: JSON.stringify(d)}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function toggleActive(id, checked) {{
      fetch('/api/admin/toggle_active', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id, is_active: checked?1:0}})}})
        .then(x=>x.json()).then(res=>{{if(!res.success){{alert(res.message);location.reload();}}else{{location.reload();}}}}); }}
    function adjustStock(id, type) {{ const q = prompt('数量（' + (type==='in'?'入库':'出库') + '）：'); if(!q) return;
      fetch('/api/admin/adjust_stock',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id, type, quantity: parseFloat(q)}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    </script></head><body>
    <h1>🛠️ 物资管理后台</h1>
    <div class="top-links"><a href="/portal">🏠 导航</a><a href="/inventory">📦 库存</a><a href="/alert">🚨 预警</a><a href="/admin/backups">💾 备份恢复</a><a href="/admin/logs">📜 日志</a></div>
    <div class="info-box">
      <b>📌 说明</b>：勾选左侧 checkbox = 启用库存管理，只有勾选的物资才会出现在看板和二维码导出中。<br>
      目前启用 <b>{active_count}</b> 条 / 全部 {total_count} 条。下方表格默认只显示已启用的，想查看全部请点「显示全部」。
    </div>
    <div class="toolbar">
      <form method="get" style="display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;">
        <input type="hidden" name="show_all" value="{('1' if show_all else '0')}">
        <input type="text" name="q" value="{keyword}" placeholder="搜索名称">
        <select name="dept"><option value="">全部</option>
          <option {"selected" if dept_filter=="综合部" else ""}>综合部</option>
          <option {"selected" if dept_filter=="工程部" else ""}>工程部</option>
          <option {"selected" if dept_filter=="秩序部" else ""}>秩序部</option>
          <option {"selected" if dept_filter=="客服部" else ""}>客服部</option></select>
        <button type="submit">搜索</button>
        <a href="/admin" style="padding:8px 16px;background:#eee;color:#333;border-radius:8px;text-decoration:none;font-size:14px;">重置</a>
        <a href="/admin?show_all={('0' if show_all else '1')}&dept={dept_filter}&q={keyword}" style="padding:8px 16px;background:{'#FF6B1A' if show_all else '#eee'};color:{'white' if show_all else '#333'};border-radius:8px;text-decoration:none;font-size:14px;">{'只显示已启用' if show_all else '显示全部480条'}</a>
      </form>
      <span style="color:#8a91a0;font-size:13px;margin-left:12px;">当前显示 {len(rows)} 项</span>
    </div>
    <table><thead><tr><th></th><th>名称</th><th>品牌</th><th>单位</th><th>部门</th><th>初始库存</th><th>预警值</th><th>当前</th><th>操作</th></tr></thead>
    <tbody>{rows_html if rows_html else '<tr><td colspan="9" style="text-align:center;padding:40px;color:#8a91a0;">暂无数据</td></tr>'}</tbody></table>
    </body></html>'''

@app.route('/admin/backups')
def admin_backups():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT id, name, note, created_at, LENGTH(items_data) AS items_len FROM backups ORDER BY id DESC LIMIT 20')
            backups = cursor.fetchall()
    finally:
        conn.close()
    
    rows = ''
    for b in backups:
        rows += f'''<tr>
            <td>{b['id']}</td>
            <td>{b['name']}</td>
            <td>{b['note'] or ''}</td>
            <td style="font-size:12px;color:#8a91a0;">{b['created_at'].strftime('%Y-%m-%d %H:%M:%S') if b['created_at'] else ''}</td>
            <td style="font-size:12px;">{b['items_len'] or 0} 字节</td>
            <td>
                <button onclick="restore({b['id']})" style="padding:4px 10px;background:#e74c3c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">恢复此备份</button>
            </td>
        </tr>'''
    if not rows:
        rows = '<tr><td colspan="6" style="text-align:center;padding:40px;color:#8a91a0;">暂无备份</td></tr>'
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>备份恢复</title><style>
    body {{ font-family: -apple-system, sans-serif; background: #f5f7fa; padding: 16px; color: #1a1f2e; }}
    .top-links a {{ display: inline-block; padding: 8px 16px; background: white; border-radius: 10px; color: #1a1f2e; text-decoration: none; margin-right: 8px; margin-bottom: 12px; font-size: 14px; border: 1px solid #e8ecf1; }}
    table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; border: 1px solid #e8ecf1; }}
    th {{ background: #f8f9fa; padding: 10px 8px; text-align: left; font-size: 12px; color: #8a91a0; text-transform: uppercase; }}
    td {{ padding: 10px 8px; border-bottom: 1px solid #f0f2f5; font-size: 13px; }}
    .info {{ background: #fff8f0; border-left: 4px solid #FF6B1A; padding: 12px 16px; border-radius: 8px; margin-bottom: 14px; }}
    button.new {{ padding: 10px 20px; background: #FF6B1A; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; margin-bottom: 14px; }}
    </style><script>
    function restore(id) {{
      if(!confirm('⚠️ 恢复备份会覆盖当前所有数据！确定恢复？')) return;
      if(!confirm('再次确认：确定要恢复到备份 #' + id + ' 吗？此操作不可撤销！')) return;
      fetch('/api/admin/restore', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function doBackup() {{
      fetch('/api/admin/backup_now', {{method:'POST'}}).then(x=>x.json()).then(res=>{{alert(res.message); location.reload();}}); }}
    </script></head><body>
    <h1>💾 备份与恢复</h1>
    <div class="top-links"><a href="/admin">📦 物资管理</a><a href="/admin/backups">💾 备份恢复</a><a href="/admin/logs">📜 日志</a></div>
    <div class="info">系统在每次危险操作（导入采购计划、批量设置库存）前会自动备份一次。只保留最近10个备份。</div>
    <button class="new" onclick="doBackup()">➕ 手动创建一个备份</button>
    <table><thead><tr><th>ID</th><th>名称</th><th>备注</th><th>时间</th><th>大小</th><th>操作</th></tr></thead>
    <tbody>{rows}</tbody></table>
    </body></html>'''

@app.route('/admin/logs')
def admin_logs():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 200')
            logs = cursor.fetchall()
    finally:
        conn.close()
    rows = ''
    for l in logs:
        rows += f'<tr><td style="font-size:12px;color:#8a91a0;">{l["created_at"].strftime("%Y-%m-%d %H:%M")}</td><td style="font-weight:600;">{l["action"]}</td><td>{l.get("item_name") or "-"}</td><td style="font-size:12px;">{l.get("detail") or ""}</td></tr>'
    if not rows: rows = '<tr><td colspan="4" style="text-align:center;padding:40px;color:#8a91a0;">暂无</td></tr>'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>操作日志</title><style>
    body {{ font-family: -apple-system, sans-serif; background: #f5f7fa; padding: 16px; color: #1a1f2e; }}
    .top-links a {{ display: inline-block; padding: 8px 16px; background: white; border-radius: 10px; color: #1a1f2e; text-decoration: none; margin-right: 8px; margin-bottom: 12px; font-size: 14px; border: 1px solid #e8ecf1; }}
    table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; border: 1px solid #e8ecf1; }}
    th {{ background: #f8f9fa; padding: 10px; text-align: left; font-size: 12px; color: #8a91a0; }}
    td {{ padding: 8px 10px; border-bottom: 1px solid #f0f2f5; font-size: 13px; }}
    </style></head><body>
    <div class="top-links"><a href="/admin">📦 物资管理</a><a href="/admin/backups">💾 备份</a></div>
    <h1>📜 操作日志</h1>
    <table><thead><tr><th>时间</th><th>操作</th><th>物资</th><th>详情</th></tr></thead><tbody>{rows}</tbody></table>
    </body></html>'''

# ================= 管理 API =================

@app.route('/api/admin/update', methods=['POST'])
def admin_update():
    d = request.json or {}
    iid = d.get('id')
    if not iid: return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items WHERE id=%s', (iid,))
            old = cursor.fetchone()
            if not old: return jsonify({'success': False, 'message': '不存在'})
            cursor.execute('''UPDATE items SET name=%s, brand=%s, unit=%s, department=%s, initial_stock=%s, low_stock_threshold=%s WHERE id=%s''',
                (d.get('name', old['name']), d.get('brand', ''), d.get('unit', old['unit']),
                 d.get('department', old['department']),
                 float(d.get('initial_stock', old['initial_stock'] or 0)),
                 float(d.get('low_stock_threshold', old['low_stock_threshold'] or 5)), iid))
            log_audit(cursor, '更新物资', iid, d.get('name', ''), f"库存{old['initial_stock']}→{d.get('initial_stock')}")
        conn.commit()
        return jsonify({'success': True, 'message': '已更新'})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})
    finally: conn.close()

@app.route('/api/admin/toggle_active', methods=['POST'])
def admin_toggle_active():
    d = request.json or {}
    iid = d.get('id')
    is_active = 1 if d.get('is_active') else 0
    if not iid: return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('UPDATE items SET is_active=%s WHERE id=%s', (is_active, iid))
            log_audit(cursor, '启用物资' if is_active else '停用物资', iid, '', '')
        conn.commit()
        return jsonify({'success': True, 'message': '已更新'})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})
    finally: conn.close()

@app.route('/api/admin/adjust_stock', methods=['POST'])
def admin_adjust_stock():
    d = request.json or {}
    iid = d.get('id'); typ = d.get('type'); qty = d.get('quantity')
    if not iid or typ not in ['in', 'out'] or not qty: return jsonify({'success': False, 'message': '参数错误'})
    try:
        qty = float(qty)
        if qty <= 0: raise ValueError()
    except: return jsonify({'success': False, 'message': '数量错误'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('INSERT INTO transactions (item_id,type,quantity,note,operator) VALUES (%s,%s,%s,%s,%s)',
                           (iid, typ, qty, '后台调整', '管理员'))
            lb = '入库' if typ == 'in' else '出库'
            log_audit(cursor, f'{lb}调整', iid, '', f'{lb}{qty}')
        conn.commit()
        return jsonify({'success': True, 'message': f'已{lb} {qty}'})
    except Exception as e: return jsonify({'success': False, 'message': str(e)})
    finally: conn.close()

@app.route('/api/admin/backup_now', methods=['POST'])
def admin_backup_now():
    if auto_backup('手动备份'):
        return jsonify({'success': True, 'message': '已创建备份'})
    return jsonify({'success': False, 'message': '备份失败'})

@app.route('/api/admin/restore', methods=['POST'])
def admin_restore():
    d = request.json or {}
    bid = d.get('id')
    if not bid: return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM backups WHERE id=%s', (bid,))
            b = cursor.fetchone()
            if not b: return jsonify({'success': False, 'message': '备份不存在'})
            items = json.loads(b['items_data'])
            trans = json.loads(b['transactions_data'])
            cursor.execute('DELETE FROM transactions')
            cursor.execute('DELETE FROM items')
            # 恢复 items
            if items:
                cols = list(items[0].keys())
                placeholders = ','.join(['%s'] * len(cols))
                col_names = ','.join(cols)
                sql = f'INSERT INTO items ({col_names}) VALUES ({placeholders})'
                batch = []
                for it in items:
                    batch.append(tuple(it.get(c) for c in cols))
                    if len(batch) >= 50:
                        cursor.executemany(sql, batch)
                        batch = []
                if batch: cursor.executemany(sql, batch)
            if trans:
                cols = list(trans[0].keys())
                placeholders = ','.join(['%s'] * len(cols))
                col_names = ','.join(cols)
                sql = f'INSERT INTO transactions ({col_names}) VALUES ({placeholders})'
                batch = []
                for t in trans:
                    batch.append(tuple(t.get(c) for c in cols))
                    if len(batch) >= 50:
                        cursor.executemany(sql, batch)
                        batch = []
                if batch: cursor.executemany(sql, batch)
            log_audit(cursor, '恢复备份', None, None, f'从备份#{bid}恢复：{len(items)}物资，{len(trans)}流水')
        conn.commit()
        return jsonify({'success': True, 'message': f'已恢复：{len(items)}条物资，{len(trans)}条流水'})
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'message': str(e) + '\n' + traceback.format_exc()})
    finally: conn.close()

# ================= 危险操作（带自动备份） =================

@app.route('/api/import_plan')
def import_plan():
    auto_backup('导入采购计划表-自动备份')
    import pandas as pd
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('DELETE FROM transactions')
            cursor.execute('DELETE FROM items')
            conn.commit()
        df = pd.read_excel('plan.xlsx', header=1)
        df.columns = [str(c).replace('<br>', '').replace('\n', '').strip() for c in df.columns]
        name_col = next((c for c in df.columns if '物品名称' in c or '物资名称' in c), None)
        brand_col = next((c for c in df.columns if c == '品牌' or '品牌' in c), None)
        spec_col = next((c for c in df.columns if '规格' in c), None)
        unit_col = next((c for c in df.columns if c == '单位' or '计量单位' in c), None)
        dept_col = next((c for c in df.columns if c == '部门' or '申采部门' in c), None)
        if not name_col: return f"导入失败：未找到'采购物品名称'列"
        unique = {}
        for idx, row in df.iterrows():
            name = str(row.get(name_col) or '').replace('\n', '').strip()
            if not name or name == 'nan': continue
            brand = str(row.get(brand_col) or '').replace('\n', '').strip() if brand_col else ''
            if brand == 'nan': brand = ''
            spec = str(row.get(spec_col) or '').replace('\n', ' ').replace('<br>', ' ').strip() if spec_col else ''
            if spec == 'nan': spec = ''
            unit = str(row.get(unit_col) or '').replace('\n', '').strip() if unit_col else '个'
            if unit == 'nan' or not unit: unit = '个'
            dept = str(row.get(dept_col) or '').replace('\n', ' ').replace('<br>', ' ').strip() if dept_col else '综合部'
            key = (name, brand, spec)
            if key not in unique:
                unique[key] = {'name': name, 'brand': brand, 'spec': spec, 'unit': unit, 'depts': set()}
            if dept and dept != 'nan':
                for d in re.split(r'[\s、,，/]+', dept):
                    d = d.strip()
                    if d and d != 'nan':
                        unique[key]['depts'].add(normalize_department(d))
        count = 0
        batch = []
        for (name, brand, spec), info in unique.items():
            key_str = f"{name}|{brand}|{spec}"
            iid = hashlib.md5(key_str.encode()).hexdigest()[:10]
            dept_str = '、'.join(sorted(info['depts'])) if info['depts'] else '综合部'
            batch.append((iid, name, brand, spec, info['unit'], dept_str))
            count += 1
            if len(batch) >= 50:
                try:
                    cursor.executemany('''INSERT IGNORE INTO items 
                        (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                        VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
                    conn.commit()
                except:
                    conn.ping(reconnect=True)
                    cursor = conn.cursor()
                    cursor.execute(f"USE {MYSQL_DATABASE}")
                    cursor.executemany('''INSERT IGNORE INTO items 
                        (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                        VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
                    conn.commit()
                batch = []
        if batch:
            cursor.executemany('''INSERT IGNORE INTO items 
                (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
            conn.commit()
        return f"🎉 成功导入 {count} 条物资！<br><br>⚠️ 注意：新导入的物资默认<b>未启用</b>库存管理，需要去 <a href='/admin'>管理后台</a> 勾选要跟踪的物资。"
    except Exception as e:
        import traceback
        return f"导入失败: {str(e)}<br><pre>{traceback.format_exc()}</pre>"
    finally:
        conn.close()

@app.route('/api/setup_stock')
def setup_stock():
    auto_backup('设置库存-自动备份')
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            results = []
            # 客服部 - 擦手纸保留 7c0c2b7e1a
            cursor.execute('''UPDATE items SET is_deleted=1 WHERE is_deleted=0 AND name LIKE %s AND id != %s''', ('%擦手纸%', '7c0c2b7e1a'))
            results.append(f"擦手纸删除重复:{cursor.rowcount}")
            cursor.execute('''UPDATE items SET initial_stock=680, low_stock_threshold=180, unit='包', is_active=1, is_deleted=0 WHERE id=%s''', ('7c0c2b7e1a',))
            results.append(f"擦手纸设置:{cursor.rowcount}")
            # 大卷纸
            cursor.execute('''UPDATE items SET is_deleted=1 WHERE is_deleted=0 AND (name LIKE %s OR name LIKE %s) AND id != %s''', ('%大卷纸%', '%卷纸%', 'fc0ed139b3'))
            results.append(f"大卷纸删除重复:{cursor.rowcount}")
            cursor.execute('''UPDATE items SET initial_stock=552, low_stock_threshold=96, unit='卷', is_active=1, is_deleted=0 WHERE id=%s''', ('fc0ed139b3',))
            results.append(f"大卷纸设置:{cursor.rowcount}")
            # 坐垫纸
            cursor.execute('''UPDATE items SET is_deleted=1 WHERE is_deleted=0 AND name LIKE %s AND id != %s''', ('%坐垫纸%', 'af26543925'))
            results.append(f"坐垫纸删除重复:{cursor.rowcount}")
            cursor.execute('''UPDATE items SET initial_stock=125, low_stock_threshold=25, unit='盒', is_active=1, is_deleted=0 WHERE id=%s''', ('af26543925',))
            results.append(f"坐垫纸设置:{cursor.rowcount}")
            # 邦固警示带
            cursor.execute('''UPDATE items SET initial_stock=10, low_stock_threshold=5, unit='卷', is_active=1 WHERE id=%s''', ('13813139f4',))
            results.append(f"邦固警示带:{cursor.rowcount}")
            # 友安警示带
            cursor.execute('''UPDATE items SET initial_stock=30, low_stock_threshold=10, unit='卷', is_active=1 WHERE id=%s''', ('288fbd9f9f',))
            results.append(f"友安警示带:{cursor.rowcount}")
            # 浙安警示带软删
            cursor.execute('UPDATE items SET is_deleted=1 WHERE id=%s', ('81992a4a4b',))
            results.append(f"浙安警示带删除:{cursor.rowcount}")
        conn.commit()
        return "✅ 设置完成：<br>" + "<br>".join(results) + "<br><br>这些物资已自动<b>启用</b>库存管理。"
    finally:
        conn.close()

@app.route('/api/export_links')
def export_links():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT name, brand, spec, id FROM items WHERE is_deleted=0 AND is_active=1 ORDER BY name')
            items = cursor.fetchall()
            base = f"https://{request.host}/item/"
            lines = ["物资名称,品牌,规格,专属链接"]
            for it in items:
                lines.append(f"{it['name']},{it.get('brand') or ''},{it.get('spec') or ''},{base}{it['id']}")
            return "\n".join(lines)
    finally: conn.close()

@app.route('/api/check_alerts')
def check_alerts():
    all_items = get_all_items_with_stock(only_active=True)
    visible = get_all_visible_items(all_items)
    low = [d for d in visible if d['is_low']]
    if not low: return "✅ 库存充足"
    depts_low = {}
    for it in low: depts_low.setdefault(it['department'], []).append(it)
    at_set = set()
    for d in depts_low:
        for p in DEPT_CONTACTS.get(d, []): at_set.add(p)
    lines = ["#### 🚨 环球梦大厦 · 库存预警", ""]
    for d, items in depts_low.items():
        lines.append(f"**【{d}】**")
        for it in items:
            sub = f'{it["brand"]} {it["spec"]}'.strip()
            lines.append(f"> {it['name']} {sub}：剩余 **{format_qty(it['real_stock'], it['unit'])}**（阈值 {it['threshold']}）")
        lines.append("")
    msg = "\n".join(lines)
    ok = send_alert_to_group(msg, list(at_set))
    return f"✅ 已推送，共@ {len(at_set)}人" if ok else "❌ 推送失败"

@app.route('/api/webhook/grass', methods=['POST'])
def webhook_grass():
    data = request.json
    if not data: return jsonify({'status': 'error', 'message': '无数据'}), 400
    try:
        ref_data = data.get('data', {}).get('ref_data', {})
        form_name = ref_data.get('form', {}).get('name', '')
        fields = ref_data.get('fields', {})
        ref_qr = data.get('data', {}).get('ref_qrcode', {})
        qr_code = ref_qr.get('code') or ''
        item_name = ref_qr.get('fields', {}).get('资产名称') or ref_qr.get('name')
        if not item_name: return jsonify({'status': 'error', 'message': '未找到物资名称'}), 400
        department = fields.get('所属部门') or fields.get('领用部门') or fields.get('部门')
        department = normalize_department(department)
        if '出库' in form_name: trans_type = 'out'; quantity = fields.get('出库数量')
        elif '入库' in form_name: trans_type = 'in'; quantity = fields.get('入库数量')
        else: return jsonify({'status': 'error', 'message': '未知表单'}), 400
        if not quantity: return jsonify({'status': 'error', 'message': '未找到数量'}), 400
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"USE {MYSQL_DATABASE}")
                item_id = None
                if qr_code:
                    cursor.execute('SELECT id FROM items WHERE qr_code=%s AND is_deleted=0', (qr_code,))
                    row = cursor.fetchone()
                    if row: item_id = row['id']
                if not item_id:
                    cursor.execute('SELECT id FROM items WHERE name=%s AND is_deleted=0 AND is_active=1 LIMIT 1', (item_name,))
                    row = cursor.fetchone()
                    if row: item_id = row['id']
                if not item_id:
                    return jsonify({'status': 'error', 'message': f'物资【{item_name}】未启用库存管理，请先在管理后台启用'}), 400
                cursor.execute('INSERT INTO transactions (item_id,type,quantity,department,note) VALUES (%s,%s,%s,%s,%s)',
                               (item_id, trans_type, float(quantity), department, f'草料同步-{form_name}'))
            conn.commit()
        finally: conn.close()
        return jsonify({'status': 'success', 'message': f'{item_name} 已同步'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/portal')
def portal():
    return '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>环球梦大厦 · 导航</title>
    <style>* { box-sizing: border-box; } body { font-family: -apple-system, "PingFang SC", sans-serif; background: #f5f7fa; margin: 0; padding: 20px; color: #1a1f2e; }
    .header { display: flex; align-items: center; background: linear-gradient(135deg, #1a1f2e 0%, #252b3d 100%); padding: 22px 26px; border-radius: 18px; box-shadow: 0 8px 32px rgba(26,31,46,0.18); margin-bottom: 20px; position: relative; overflow: hidden; }
    .header::after { content: ''; position: absolute; top: -50%; right: -10%; width: 280px; height: 280px; background: radial-gradient(circle, rgba(255,107,26,0.15) 0%, transparent 70%); }
    .header img { height: 46px; margin-right: 16px; background: white; padding: 6px 10px; border-radius: 10px; z-index: 1; }
    .header .title { z-index: 1; }
    .project-name { font-size: 24px; font-weight: 800; color: #fff; letter-spacing: 3px; }
    .sub-title { font-size: 11px; color: rgba(255,255,255,0.55); margin-top: 4px; letter-spacing: 4px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .section { background: white; border-radius: 16px; padding: 20px 22px; box-shadow: 0 2px 12px rgba(26,31,46,0.05); border: 1px solid #e8ecf1; }
    .section-title { font-size: 15px; font-weight: 700; color: #1a1f2e; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 2px solid #f0f2f5; }
    .link-item { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; margin-bottom: 8px; background: #f8fafc; border-radius: 10px; text-decoration: none; color: #1a1f2e; transition: all 0.15s; border: 1px solid transparent; }
    .link-item:hover { background: #fff5ec; border-color: #FF6B1A; transform: translateX(3px); }
    .link-item.danger { background: #fff5f5; }
    .link-item.danger:hover { background: #ffe5e5; border-color: #e74c3c; }
    .link-item .label { font-size: 14px; font-weight: 600; }
    .link-item .desc { font-size: 11px; color: #8a91a0; margin-top: 2px; }
    .link-item .arrow { color: #FF6B1A; font-size: 16px; font-weight: bold; }
    .footer { text-align: center; color: #8a91a0; font-size: 12px; margin-top: 30px; letter-spacing: 1px; }
    </style><script>
    function confirmDanger(url, msg) { if(confirm('⚠️ ' + msg + '\\n\\n此操作会自动备份，但仍建议先在 /admin/backups 手动创建备份。\\n\\n确定继续？')) location.href=url; }
    </script></head><body>
    <div class="header"><img src="/static/logo.png" onerror="this.style.display='none'"><div class="title"><div class="project-name">环球梦大厦</div><div class="sub-title">物资库存系统 · 统一导航</div></div></div>
    <div class="grid">
    <div class="section"><div class="section-title">📦 库存看板</div>
    <a class="link-item" href="/inventory"><div><div class="label">全局库存</div><div class="desc">所有已启用的物资</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/inventory?dept=综合部"><div><div class="label">综合部库存</div><div class="desc">综合部现有物资</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/inventory?dept=客服部"><div><div class="label">客服部库存</div><div class="desc">擦手纸/卷纸/坐垫纸</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/inventory?dept=秩序部"><div><div class="label">秩序部库存</div><div class="desc">警示带</div></div><span class="arrow">›</span></a>
    </div>
    <div class="section"><div class="section-title">🚨 预警看板</div>
    <a class="link-item" href="/alert"><div><div class="label">全部预警</div><div class="desc">所有缺货汇总</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/alert?dept=综合部"><div><div class="label">综合部预警</div><div class="desc">综合部缺货</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/alert?dept=客服部"><div><div class="label">客服部预警</div><div class="desc">客服部缺货</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/alert?dept=秩序部"><div><div class="label">秩序部预警</div><div class="desc">警示带缺货</div></div><span class="arrow">›</span></a>
    </div>
    <div class="section"><div class="section-title">🛠️ 管理后台</div>
    <a class="link-item" href="/admin"><div><div class="label">物资管理</div><div class="desc">启用/停用、改库存、改预警值</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/admin/backups"><div><div class="label">💾 备份与恢复</div><div class="desc">数据丢失时可恢复</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/admin/logs"><div><div class="label">📜 操作日志</div><div class="desc">所有后台操作历史</div></div><span class="arrow">›</span></a>
    </div>
    <div class="section"><div class="section-title">⚙️ 系统工具</div>
    <a class="link-item" href="/api/export_links"><div><div class="label">导出已启用物资</div><div class="desc">CSV，用于生成草料二维码</div></div><span class="arrow">›</span></a>
    <a class="link-item" href="/api/check_alerts"><div><div class="label">手动触发预警</div><div class="desc">推送到钉钉群</div></div><span class="arrow">›</span></a>
    <a class="link-item danger" href="javascript:;" onclick="confirmDanger('/api/import_plan', '这会清空所有物资和流水，重新从 plan.xlsx 导入！')"><div><div class="label">⚠️ 重新导入采购计划表</div><div class="desc">会清空现有数据（自动备份）</div></div><span class="arrow">›</span></a>
    <a class="link-item danger" href="javascript:;" onclick="confirmDanger('/api/setup_stock', '这会重置客服部/秩序部库存并删除重复物资！')"><div><div class="label">⚠️ 一键设置库存</div><div class="desc">重置客服/秩序部库存（自动备份）</div></div><span class="arrow">›</span></a>
    </div>
    </div><div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
    </body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)