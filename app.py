from flask import Flask, request, jsonify, redirect
import logging
import pymysql
import os
import hashlib
import re
import requests
import csv
import io
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
            for sql in [
                "ALTER TABLE items ADD COLUMN brand VARCHAR(255) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN spec VARCHAR(500) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN sub_unit VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN conversion DECIMAL(10,2) DEFAULT NULL",
                "ALTER TABLE items MODIFY initial_stock DECIMAL(10,2) DEFAULT 0",
                "ALTER TABLE items MODIFY low_stock_threshold DECIMAL(10,2) DEFAULT 5",
                "ALTER TABLE items ADD COLUMN asset_code VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN qr_code VARCHAR(50) DEFAULT NULL",
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
                'CREATE INDEX idx_qr_code ON items(qr_code)'
            ]:
                try: cursor.execute(sql)
                except Exception: pass
    finally:
        conn.close()

init_db()

def log_audit(cursor, action, item_id, item_name, detail, operator='管理员'):
    cursor.execute('INSERT INTO audit_logs (action, item_id, item_name, detail, operator) VALUES (%s, %s, %s, %s, %s)',
                   (action, item_id, item_name, detail, operator))

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

def get_all_items_with_stock(include_deleted=False):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            where = "" if include_deleted else "WHERE i.is_deleted = 0"
            cursor.execute(f'''
                SELECT i.id, i.name, i.brand, i.spec, i.unit, i.sub_unit, i.conversion,
                       i.initial_stock, i.low_stock_threshold, i.department, i.asset_code, i.qr_code, i.is_deleted,
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
        <img src="/static/logo.png" class="logo" alt="越秀服务" onerror="this.style.display='none'">
        <div class="title-block">
            <div class="project-name">环球梦大厦</div>
            <div class="sub-title">物资库存预警看板</div>
        </div>
    </div>
    '''

BASE_STYLE = '''
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }
    .main-header { display: flex; align-items: center; background: linear-gradient(135deg, #fff, #f8fafc); padding: 18px 22px; border-radius: 14px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; }
    .main-header .logo { height: 46px; margin-right: 16px; }
    .project-name { font-size: 24px; font-weight: 800; color: #1a1a1a; letter-spacing: 2px; }
    .sub-title { font-size: 12px; color: #999; margin-top: 4px; letter-spacing: 4px; }
    .section-title { font-size: 16px; font-weight: 700; color: #333; margin: 24px 0 12px; }
    .section-title span { background: #e74c3c; color: white; font-size: 12px; padding: 2px 8px; border-radius: 10px; margin-left: 8px; }
    .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    .scroll-card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); max-height: 400px; overflow-y: auto; }
    .low-item { background: #fff5f5; border-left: 4px solid #e74c3c; padding: 12px; margin-bottom: 8px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; }
    .low-item .name { font-weight: 600; color: #333; }
    .low-item .meta { color: #888; font-size: 12px; margin-left: 8px; }
    .low-item .stock { color: #e74c3c; font-weight: 700; font-size: 18px; }
    .ok-msg { text-align: center; color: #2ecc71; padding: 20px; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th { color: #888; text-align: left; padding-bottom: 8px; font-weight: 500; }
    td { padding: 8px 0; border-bottom: 1px solid #eee; }
    td.num { text-align: right; color: #333; font-weight: 600; }
    .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }
    .quick-links { text-align: center; margin: 15px 0; }
    .quick-links a { display: inline-block; padding: 6px 14px; background: white; border-radius: 20px; color: #333; text-decoration: none; font-size: 13px; margin: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
    .quick-links a.active { background: #333; color: white; }
    .search-bar { display: flex; gap: 8px; margin: 15px 0; }
    .search-bar input { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 10px; font-size: 15px; outline: none; }
    .search-bar button { padding: 12px 24px; background: #333; color: white; border: none; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer; }
    .item-sub { color: #999; font-size: 11px; margin-top: 2px; }
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
    all_items = get_all_items_with_stock()
    
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
                <td><a href="/item/{item["id"]}/history" style="color:#333;text-decoration:none;">{item["name"]}</a>{sub_html}</td>
                <td class="num">{format_qty(item["real_stock"], item["unit"], item["sub_unit"], item["conversion"])}</td>
            </tr>'''
        return f'<table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{rows}</tbody></table>'
    
    if dept:
        my_items = dept_dict.get(dept, [])
        if my_items:
            body = f'<div class="section-title">📦 {dept}库存清单<span>{len(my_items)} 项</span></div><div class="scroll-card">{render_table(my_items)}</div>'
        else:
            body = f'<div class="card" style="text-align:center;color:#888;padding:20px;">{dept}暂无有库存的物资</div>'
    else:
        if dept_dict:
            total = sum(len(v) for v in dept_dict.values())
            body = f'<div class="section-title">📦 各部门库存总览<span>{total} 项</span></div>'
            for d, items in dept_dict.items():
                body += f'<div class="scroll-card" style="margin-bottom:12px;"><div style="font-weight:700;font-size:15px;margin-bottom:10px;">📁 {d}（{len(items)} 项）</div>{render_table(items)}</div>'
        else:
            body = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无有库存的物资</div>'
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>库存看板 · 环球梦大厦</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}{quick}{search}{body}
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/alert')
def alert_dashboard():
    dept = normalize_department(request.args.get('dept', '').strip())
    all_items = get_all_items_with_stock()
    
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
                body += f'<div class="scroll-card" style="margin-bottom:12px;"><div style="font-weight:700;font-size:15px;margin-bottom:10px;color:#e74c3c;">📁 {d}（{len(items)} 项）</div>{render_low(items)}</div>'
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
    .item-card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; text-align: center; }}
    .item-name {{ font-size: 22px; font-weight: 700; color: #333; margin-bottom: 8px; }}
    .item-subtitle {{ color: #999; font-size: 12px; margin-bottom: 12px; }}
    .label {{ font-size: 14px; color: #888; margin-bottom: 8px; }}
    .stock-number {{ font-size: 56px; font-weight: 800; color: {c}; line-height: 1.1; margin: 10px 0; }}
    .warning {{ background: #fff3cd; border: 1px solid #ffc107; color: #856404; padding: 10px 16px; border-radius: 10px; margin-top: 15px; font-size: 14px; font-weight: 600; display: inline-block; }}
    .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
    .stat-box {{ background: white; border-radius: 16px; padding: 20px; text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
    .stat-value {{ font-size: 24px; font-weight: 700; margin-top: 8px; }}
    </style></head><body>
    {render_header()}
    <div class="item-card"><div class="item-name">{data['name']}</div>
    <div class="item-subtitle">{sub}</div>
    <div class="label">实时库存</div>
    <div class="stock-number">{format_qty(data['real_stock'], data['unit'])}</div>
    {f'<div style="color:#999;font-size:14px;margin-top:6px;">约 {data["real_stock"] * data["conversion"]:.0f} {data["sub_unit"]}</div>' if data['sub_unit'] and data['conversion'] else ''}
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
        rows += f'''<tr><td style="font-size:12px;color:#666;">{t['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
        <td style="color:{tc};font-weight:600;">{tl}</td>
        <td class="num" style="color:{tc};font-weight:600;">{'+' if t['type']=='in' else '-'}{format_qty(t['quantity'], item['unit'])}</td>
        <td style="font-size:12px;">{t.get('department') or ''}</td>
        <td style="font-size:12px;color:#999;">{t.get('note') or ''}</td></tr>'''
    if not rows: rows = '<tr><td colspan="5" style="text-align:center;color:#888;padding:30px;">暂无记录</td></tr>'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{item['name']} - 流水</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}
    <div class="card" style="margin-bottom:16px;"><div style="font-size:18px;font-weight:700;">📋 {item['name']} · 出入库流水</div>
    <div style="color:#888;font-size:13px;margin-top:6px;">共 {len(trans)} 条</div></div>
    <div style="background:white;border-radius:12px;padding:12px;box-shadow:0 2px 8px rgba(0,0,0,0.05);overflow-x:auto;">
    <table><thead><tr><th>时间</th><th>类型</th><th style="text-align:right;">数量</th><th>部门</th><th>备注</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/admin')
def admin_page():
    keyword = request.args.get('q', '').strip()
    dept_filter = request.args.get('dept', '').strip()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            sql = '''SELECT i.*, COALESCE(SUM(CASE WHEN t.type='in' THEN t.quantity ELSE 0 END),0) AS total_in,
                    COALESCE(SUM(CASE WHEN t.type='out' THEN t.quantity ELSE 0 END),0) AS total_out
                    FROM items i LEFT JOIN transactions t ON i.id=t.item_id WHERE i.is_deleted=0'''
            params = []
            if keyword: sql += " AND i.name LIKE %s"; params.append(f"%{keyword}%")
            if dept_filter: sql += " AND i.department LIKE %s"; params.append(f"%{dept_filter}%")
            sql += " GROUP BY i.id ORDER BY i.department, i.name LIMIT 300"
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    finally:
        conn.close()
    
    rows_html = ''
    for r in rows:
        rs = float(r['initial_stock'] or 0) + float(r['total_in'] or 0) - float(r['total_out'] or 0)
        opts = ''.join([f'<option value="{d}" {"selected" if d==r["department"] else ""}>{d}</option>' for d in ['综合部','工程部','秩序部','客服部','未分配']])
        sc = '#e74c3c' if rs <= float(r['low_stock_threshold'] or 5) else '#333'
        rows_html += f'''<tr data-id="{r['id']}">
            <td><input class="name" type="text" value="{r['name']}" style="width:120px;padding:4px;"></td>
            <td><input class="brand" type="text" value="{r.get('brand') or ''}" style="width:70px;padding:4px;"></td>
            <td><input class="unit" type="text" value="{r['unit']}" style="width:40px;padding:4px;"></td>
            <td><input class="department" type="text" value="{r['department'] or ''}" style="width:80px;padding:4px;"></td>
            <td><input class="initial_stock" type="number" step="0.01" value="{r['initial_stock']}" style="width:60px;padding:4px;"></td>
            <td><input class="low_stock_threshold" type="number" step="0.01" value="{r['low_stock_threshold']}" style="width:60px;padding:4px;"></td>
            <td style="font-weight:600;color:{sc};font-size:12px;">{format_qty(rs, r['unit'])}</td>
            <td style="white-space:nowrap;">
                <button onclick="saveItem(this, '{r['id']}')" style="padding:3px 7px;background:#2ecc71;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">保存</button>
                <button onclick="adjustStock('{r['id']}', 'in')" style="padding:3px 7px;background:#3498db;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">入</button>
                <button onclick="adjustStock('{r['id']}', 'out')" style="padding:3px 7px;background:#f39c12;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">出</button>
                <a href="/item/{r['id']}/history" target="_blank" style="padding:3px 7px;background:#9b59b6;color:#fff;border-radius:4px;text-decoration:none;font-size:12px;">流水</a>
                <button onclick="softDelete('{r['id']}')" style="padding:3px 7px;background:#e74c3c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">删</button>
            </td></tr>'''
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>物资管理</title><style>
    body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }}
    .top-links a {{ display: inline-block; padding: 8px 16px; background: white; border-radius: 8px; color: #333; text-decoration: none; margin-right: 8px; margin-bottom: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); font-size: 14px; }}
    .add-box, .toolbar {{ background: white; padding: 12px; border-radius: 12px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
    .toolbar input, .toolbar select, .add-box input, .add-box select {{ padding: 8px; border: 1px solid #ddd; border-radius: 6px; margin-right: 6px; }}
    .toolbar button, .add-box button {{ padding: 8px 16px; background: #333; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }}
    table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
    th {{ background: #f8f9fa; padding: 8px 4px; text-align: left; font-size: 12px; color: #666; border-bottom: 2px solid #eee; }}
    td {{ padding: 4px; border-bottom: 1px solid #eee; font-size: 12px; }}
    </style><script>
    function saveItem(btn, id) {{ const r = btn.closest('tr');
      const d = {{id, name: r.querySelector('.name').value, brand: r.querySelector('.brand').value,
        unit: r.querySelector('.unit').value, department: r.querySelector('.department').value,
        initial_stock: parseFloat(r.querySelector('.initial_stock').value||'0'),
        low_stock_threshold: parseFloat(r.querySelector('.low_stock_threshold').value||'5')}};
      fetch('/api/admin/update', {{method:'POST',headers:{{'Content-Type':'application/json'}},body: JSON.stringify(d)}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function softDelete(id) {{ if (!confirm('确认删除？')) return;
      fetch('/api/admin/soft_delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function adjustStock(id, type) {{ const q = prompt('数量（' + (type==='in'?'入库':'出库') + '）：'); if(!q) return;
      fetch('/api/admin/adjust_stock',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id, type, quantity: parseFloat(q)}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    </script></head><body>
    <h1 style="font-size:20px;">🛠️ 物资管理后台</h1>
    <div class="top-links"><a href="/portal">🏠 导航</a><a href="/inventory">📦 库存</a><a href="/alert">🚨 预警</a></div>
    <div class="toolbar">
      <form method="get" style="display:inline-flex;gap:6px;align-items:center;">
        <input type="text" name="q" value="{keyword}" placeholder="搜索名称">
        <select name="dept"><option value="">全部</option>
          <option {"selected" if dept_filter=="综合部" else ""}>综合部</option>
          <option {"selected" if dept_filter=="工程部" else ""}>工程部</option>
          <option {"selected" if dept_filter=="秩序部" else ""}>秩序部</option>
          <option {"selected" if dept_filter=="客服部" else ""}>客服部</option></select>
        <button type="submit">搜索</button>
        <a href="/admin" style="padding:8px 16px;background:#eee;color:#333;border-radius:6px;text-decoration:none;">重置</a>
      </form>
      <span style="color:#888;font-size:13px;margin-left:12px;">共 {len(rows)} 项</span></div>
    <table><thead><tr><th>名称</th><th>品牌</th><th>单位</th><th>部门</th><th>初始库存</th><th>预警值</th><th>当前</th><th>操作</th></tr></thead>
    <tbody>{rows_html if rows_html else '<tr><td colspan="8" style="text-align:center;padding:40px;color:#888;">暂无</td></tr>'}</tbody></table>
    </body></html>'''

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

@app.route('/api/admin/soft_delete', methods=['POST'])
def admin_soft_delete():
    d = request.json or {}
    iid = d.get('id')
    if not iid: return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('UPDATE items SET is_deleted=1 WHERE id=%s', (iid,))
            log_audit(cursor, '删除物资', iid, '', '软删除')
        conn.commit()
        return jsonify({'success': True, 'message': '已删除'})
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

@app.route('/api/webhook/grass', methods=['POST'])
def webhook_grass():
    data = request.json
    logging.info(f"收到草料推送: {data}")
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
                    cursor.execute('SELECT id FROM items WHERE name=%s AND is_deleted=0 LIMIT 1', (item_name,))
                    row = cursor.fetchone()
                    if row: item_id = row['id']
                if not item_id:
                    item_id = hashlib.md5((item_name + str(datetime.now())).encode()).hexdigest()[:10]
                    unit = fields.get('单选项', '个')
                    cursor.execute('INSERT INTO items (id,name,unit,initial_stock,low_stock_threshold,qr_code) VALUES (%s,%s,%s,0,5,%s)',
                                   (item_id, item_name, unit, qr_code))
                cursor.execute('INSERT INTO transactions (item_id,type,quantity,department,note) VALUES (%s,%s,%s,%s,%s)',
                               (item_id, trans_type, float(quantity), department, f'草料同步-{form_name}'))
            conn.commit()
        finally: conn.close()
        return jsonify({'status': 'success', 'message': f'{item_name} 已同步'}), 200
    except Exception as e:
        logging.error(f"处理失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/import_plan')
def import_plan():
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
        if not name_col: return f"导入失败：未找到'采购物品名称'列。实际列名：{list(df.columns)}"
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
                except Exception as e:
                    conn.ping(reconnect=True)
                    cursor = conn.cursor()
                    cursor.execute(f"USE {MYSQL_DATABASE}")
                    cursor.executemany('''INSERT IGNORE INTO items 
                        (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                        VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
                    conn.commit()
                batch = []
        if batch:
            try:
                cursor.executemany('''INSERT IGNORE INTO items 
                    (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                    VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
                conn.commit()
            except Exception as e:
                conn.ping(reconnect=True)
                cursor = conn.cursor()
                cursor.execute(f"USE {MYSQL_DATABASE}")
                cursor.executemany('''INSERT IGNORE INTO items 
                    (id,name,brand,spec,unit,initial_stock,low_stock_threshold,department)
                    VALUES (%s,%s,%s,%s,%s,0,5,%s)''', batch)
                conn.commit()
        log_audit(cursor, '批量导入', None, None, f'从采购计划表导入{count}条')
        conn.commit()
        return f"🎉 成功导入 {count} 条物资！"
    except Exception as e:
        import traceback
        return f"导入失败: {str(e)}<br><pre>{traceback.format_exc()}</pre>"
    finally:
        conn.close()

# ============ 新增：查看某部门物资清单 ============
@app.route('/api/list_dept_items')
def list_dept_items():
    dept = request.args.get('dept', '').strip()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''SELECT id, name, brand, spec, unit, department 
                FROM items WHERE is_deleted=0 AND (name LIKE %s OR department LIKE %s) ORDER BY name''',
                (f'%{dept}%', f'%{dept}%'))
            rows = cursor.fetchall()
            lines = ["ID,名称,品牌,规格,单位,部门"]
            for r in rows:
                lines.append(f"{r['id']},{r['name']},{r.get('brand') or ''},{(r.get('spec') or '')[:40]},{r['unit']},{r['department'] or ''}")
            return "\n".join(lines)
    finally:
        conn.close()

# ============ 新增：一键设置库存和预警值 ============
@app.route('/api/setup_stock')
def setup_stock():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            results = []
            
            # 客服部 - 擦手纸：680包，预警180
            cursor.execute('''UPDATE items SET initial_stock=680, low_stock_threshold=180, unit='包'
                WHERE is_deleted=0 AND name LIKE %s''', ('%擦手纸%',))
            results.append(f"擦手纸: {cursor.rowcount}条")
            
            # 客服部 - 大卷纸/卷纸：552卷，预警96
            cursor.execute('''UPDATE items SET initial_stock=552, low_stock_threshold=96, unit='卷'
                WHERE is_deleted=0 AND (name LIKE %s OR name LIKE %s)''', ('%大卷纸%', '%卷纸%'))
            results.append(f"大卷纸: {cursor.rowcount}条")
            
            # 客服部 - 坐垫纸：125盒，预警25
            cursor.execute('''UPDATE items SET initial_stock=125, low_stock_threshold=25, unit='盒'
                WHERE is_deleted=0 AND name LIKE %s''', ('%坐垫纸%',))
            results.append(f"坐垫纸: {cursor.rowcount}条")
            
            # 秩序部 - 邦固黄黑警示带：10卷，预警5
            cursor.execute('''UPDATE items SET initial_stock=10, low_stock_threshold=5, unit='卷'
                WHERE is_deleted=0 AND name LIKE %s AND brand LIKE %s''', ('%警示带%', '%邦固%'))
            results.append(f"邦固警示带: {cursor.rowcount}条")
            
            # 秩序部 - 友安红白警示带：30卷，预警10
            cursor.execute('''UPDATE items SET initial_stock=30, low_stock_threshold=10, unit='卷'
                WHERE is_deleted=0 AND name LIKE %s AND brand LIKE %s''', ('%警示带%', '%友安%'))
            results.append(f"友安警示带: {cursor.rowcount}条")
            
        conn.commit()
        return "✅ 设置完成：<br>" + "<br>".join(results)
    finally:
        conn.close()

@app.route('/api/export_links')
def export_links():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT name, brand, spec, asset_code, id FROM items WHERE is_deleted=0 ORDER BY name LIMIT 1000')
            items = cursor.fetchall()
            base = f"https://{request.host}/item/"
            lines = ["物资名称,品牌,规格,资产编号,专属链接"]
            for it in items:
                lines.append(f"{it['name']},{it.get('brand') or ''},{it.get('spec') or ''},{it.get('asset_code') or ''},{base}{it['id']}")
            return "\n".join(lines)
    finally: conn.close()

@app.route('/api/check_alerts')
def check_alerts():
    all_items = get_all_items_with_stock()
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

@app.route('/portal')
def portal():
    return '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>环球梦大厦 · 导航</title>
    <style>* { box-sizing: border-box; } body { font-family: -apple-system, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; }
    .header { display: flex; align-items: center; background: white; padding: 22px 26px; border-radius: 16px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 20px; }
    .header img { height: 52px; margin-right: 18px; }
    .project-name { font-size: 26px; font-weight: 800; color: #1a1a1a; letter-spacing: 2px; }
    .sub-title { font-size: 12px; color: #999; margin-top: 4px; letter-spacing: 4px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .section { background: white; border-radius: 16px; padding: 20px 22px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
    .section-title { font-size: 15px; font-weight: 700; color: #333; margin-bottom: 14px; padding-bottom: 10px; border-bottom: 2px solid #f0f2f5; }
    .link-item { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; margin-bottom: 8px; background: #f8fafc; border-radius: 10px; text-decoration: none; color: #333; }
    .link-item:hover { background: #eef2f7; }
    .link-item .label { font-size: 14px; font-weight: 600; }
    .link-item .desc { font-size: 11px; color: #999; margin-top: 2px; }
    .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; }
    </style></head><body>
    <div class="header"><img src="/static/logo.png" onerror="this.style.display='none'"><div><div class="project-name">环球梦大厦</div><div class="sub-title">物资库存系统 · 统一导航</div></div></div>
    <div class="grid">
    <div class="section"><div class="section-title">📦 库存看板</div>
    <a class="link-item" href="/inventory"><div><div class="label">全局库存</div><div class="desc">综合部+客服部+秩序部</div></div><span>›</span></a>
    <a class="link-item" href="/inventory?dept=综合部"><div><div class="label">综合部库存</div><div class="desc">全部物资</div></div><span>›</span></a>
    <a class="link-item" href="/inventory?dept=客服部"><div><div class="label">客服部库存</div><div class="desc">擦手纸/卷纸/坐垫纸</div></div><span>›</span></a>
    <a class="link-item" href="/inventory?dept=秩序部"><div><div class="label">秩序部库存</div><div class="desc">警示带</div></div><span>›</span></a>
    </div>
    <div class="section"><div class="section-title">🚨 预警看板</div>
    <a class="link-item" href="/alert"><div><div class="label">全部预警</div><div class="desc">所有缺货汇总</div></div><span>›</span></a>
    <a class="link-item" href="/alert?dept=综合部"><div><div class="label">综合部预警</div><div class="desc">综合部缺货</div></div><span>›</span></a>
    <a class="link-item" href="/alert?dept=客服部"><div><div class="label">客服部预警</div><div class="desc">客服部缺货</div></div><span>›</span></a>
    <a class="link-item" href="/alert?dept=秩序部"><div><div class="label">秩序部预警</div><div class="desc">警示带缺货</div></div><span>›</span></a>
    </div>
    <div class="section"><div class="section-title">🛠️ 管理后台</div>
    <a class="link-item" href="/admin"><div><div class="label">物资管理</div><div class="desc">改库存/预警值</div></div><span>›</span></a>
    </div>
    <div class="section"><div class="section-title">⚙️ 系统工具</div>
    <a class="link-item" href="/api/import_plan"><div><div class="label">导入采购计划表</div><div class="desc">重新导入全部物资</div></div><span>›</span></a>
    <a class="link-item" href="/api/setup_stock"><div><div class="label">一键设置客服/秩序库存</div><div class="desc">擦手纸680 卷纸552 坐垫纸125 警示带</div></div><span>›</span></a>
    <a class="link-item" href="/api/export_links"><div><div class="label">导出全部物资</div><div class="desc">CSV格式</div></div><span>›</span></a>
    <a class="link-item" href="/api/check_alerts"><div><div class="label">手动触发预警</div><div class="desc">推送到钉钉群</div></div><span>›</span></a>
    </div>
    </div><div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
    </body></html>'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)