from flask import Flask, request, jsonify
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

ALERT_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=2b99041269aa1070355657d9060ae29ef6c064cfeb3b2565f0c2b0ac6877d6c1'
DEPT_CONTACTS = {
    '综合部': ['19874467403'],
    '工程部': ['18666082342'],
    '秩序部': ['19874467403'],
    '客服部': ['18666082342'],
}

def get_db():
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0] if MYSQL_ADDRESS else "127.0.0.1",
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10, read_timeout=30, write_timeout=30
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
                    unit VARCHAR(50) DEFAULT '个',
                    initial_stock INT DEFAULT 0,
                    low_stock_threshold INT DEFAULT 5,
                    department VARCHAR(50) DEFAULT NULL,
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
                    quantity INT NOT NULL,
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
                "ALTER TABLE items ADD COLUMN asset_code VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN qr_code VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE items ADD COLUMN is_deleted TINYINT DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN operator VARCHAR(50) DEFAULT NULL"
            ]:
                try: cursor.execute(sql)
                except Exception: pass
            for sql in [
                'CREATE INDEX idx_item_id ON transactions(item_id)',
                'CREATE INDEX idx_name ON items(name)',
                'CREATE INDEX idx_department ON transactions(department)',
                'CREATE INDEX idx_asset_code ON items(asset_code)',
                'CREATE INDEX idx_qr_code ON items(qr_code)'
            ]:
                try: cursor.execute(sql)
                except Exception: pass
        logging.info("数据库初始化完成")
    except Exception as e:
        logging.error(f"数据库初始化失败: {e}")
    finally:
        conn.close()

def generate_item_id(name):
    return hashlib.md5(name.encode()).hexdigest()[:8]

init_db()

def log_audit(cursor, action, item_id, item_name, detail, operator='管理员'):
    cursor.execute('INSERT INTO audit_logs (action, item_id, item_name, detail, operator) VALUES (%s, %s, %s, %s, %s)',
                   (action, item_id, item_name, detail, operator))

def get_all_items_with_stock(include_deleted=False):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            where = "" if include_deleted else "WHERE i.is_deleted = 0"
            cursor.execute(f'''
                SELECT i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department, 
                       i.asset_code, i.qr_code, i.is_deleted,
                       COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                       COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i LEFT JOIN transactions t ON i.id = t.item_id
                {where}
                GROUP BY i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department, i.asset_code, i.qr_code, i.is_deleted
            ''')
            rows = cursor.fetchall()
            result = []
            for row in rows:
                real_stock = row['initial_stock'] + row['total_in'] - row['total_out']
                result.append({
                    'id': row['id'], 'name': row['name'], 'unit': row['unit'],
                    'real_stock': real_stock, 'total_in': row['total_in'], 'total_out': row['total_out'],
                    'threshold': row['low_stock_threshold'],
                    'department': normalize_department(row.get('department')) or '未分配',
                    'asset_code': row.get('asset_code') or '',
                    'qr_code': row.get('qr_code') or '',
                    'is_low': real_stock <= row['low_stock_threshold'],
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
    .scroll-card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); max-height: 340px; overflow-y: auto; }
    .low-item { background: #fff5f5; border-left: 4px solid #e74c3c; padding: 12px; margin-bottom: 8px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; }
    .low-item .name { font-weight: 600; color: #333; }
    .low-item .meta { color: #888; font-size: 12px; margin-left: 8px; }
    .low-item .stock { color: #e74c3c; font-weight: 700; font-size: 18px; }
    .ok-msg { text-align: center; color: #2ecc71; padding: 20px; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th { color: #888; text-align: left; padding-bottom: 8px; font-weight: 500; }
    td { padding: 8px 0; border-bottom: 1px solid #eee; }
    td.num { text-align: right; color: #e74c3c; font-weight: 600; }
    .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }
    .quick-links { text-align: center; margin: 15px 0; }
    .quick-links a { display: inline-block; padding: 6px 14px; background: white; border-radius: 20px; color: #333; text-decoration: none; font-size: 13px; margin: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
    .quick-links a.active { background: #333; color: white; }
    .search-bar { display: flex; gap: 8px; margin: 15px 0; }
    .search-bar input { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 10px; font-size: 15px; outline: none; }
    .search-bar button { padding: 12px 24px; background: #333; color: white; border: none; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer; }
'''

# ================= 页面 =================

@app.route('/')
def home():
    return "🎉 环球梦大厦库存看板系统运行中！请扫码访问。"

@app.route('/item/<item_id>')
def item_page(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''
                SELECT i.*, 
                    COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                    COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i LEFT JOIN transactions t ON i.id = t.item_id
                WHERE i.id = %s AND i.is_deleted = 0
                GROUP BY i.id
            ''', (item_id,))
            row = cursor.fetchone()
            if not row:
                return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            real_stock = row['initial_stock'] + row['total_in'] - row['total_out']
            data = {'name': row['name'], 'unit': row['unit'], 'real_stock': real_stock,
                    'total_in': row['total_in'], 'total_out': row['total_out'],
                    'is_low': real_stock <= row['low_stock_threshold']}
    finally:
        conn.close()
    c = '#e74c3c' if data['is_low'] else '#2ecc71'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>{data['name']}</title><style>{BASE_STYLE}
    .item-card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; text-align: center; }}
    .item-name {{ font-size: 22px; font-weight: 700; color: #333; margin-bottom: 8px; }}
    .label {{ font-size: 14px; color: #888; margin-bottom: 8px; }}
    .stock-number {{ font-size: 64px; font-weight: 800; color: {c}; line-height: 1; margin: 10px 0; }}
    .unit {{ font-size: 20px; color: #999; }}
    .warning {{ background: #fff3cd; border: 1px solid #ffc107; color: #856404; padding: 10px 16px; border-radius: 10px; margin-top: 15px; font-size: 14px; font-weight: 600; display: inline-block; }}
    .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
    .stat-box {{ background: white; border-radius: 16px; padding: 20px; text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
    .stat-value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
    </style></head><body>
    {render_header()}
    <div class="item-card"><div class="item-name">{data['name']}</div><div class="label">实时库存</div>
    <div class="stock-number">{data['real_stock']}<span class="unit"> {data['unit']}</span></div>
    {f'<div class="warning">⚠️ 库存不足，请及时采购！</div>' if data['is_low'] else ''}</div>
    <div class="stats-grid">
      <div class="stat-box"><div class="label">累计入库</div><div class="stat-value" style="color:#2ecc71;">+{data['total_in']}</div></div>
      <div class="stat-box"><div class="label">累计出库</div><div class="stat-value" style="color:#e74c3c;">-{data['total_out']}</div></div>
    </div>
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/item/<item_id>/history')
def item_history(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items WHERE id = %s', (item_id,))
            item = cursor.fetchone()
            if not item:
                return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            cursor.execute('SELECT * FROM transactions WHERE item_id = %s ORDER BY created_at DESC LIMIT 500', (item_id,))
            trans = cursor.fetchall()
    finally:
        conn.close()
    rows = ''
    for t in trans:
        tl = '入库' if t['type'] == 'in' else '出库'
        tc = '#2ecc71' if t['type'] == 'in' else '#e74c3c'
        rows += f'''<tr><td style="font-size:12px;color:#666;">{t['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
        <td style="color:{tc};font-weight:600;">{tl}</td>
        <td class="num" style="color:{tc};font-weight:600;">{'+' if t['type']=='in' else '-'}{t['quantity']}</td>
        <td style="font-size:12px;">{t.get('department') or ''}</td>
        <td style="font-size:12px;color:#888;">{t.get('operator') or ''}</td>
        <td style="font-size:12px;color:#999;">{t.get('note') or ''}</td></tr>'''
    if not rows: rows = '<tr><td colspan="6" style="text-align:center;color:#888;padding:30px;">暂无记录</td></tr>'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{item['name']} - 流水</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}
    <div class="card" style="margin-bottom:16px;"><div style="font-size:18px;font-weight:700;">📋 {item['name']} · 出入库流水</div>
    <div style="color:#888;font-size:13px;margin-top:6px;">共 {len(trans)} 条（最近500条）</div></div>
    <div style="background:white;border-radius:12px;padding:12px;box-shadow:0 2px 8px rgba(0,0,0,0.05);overflow-x:auto;">
    <table><thead><tr><th>时间</th><th>类型</th><th style="text-align:right;">数量</th><th>领用部门</th><th>操作人</th><th>备注</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

@app.route('/dashboard')
def dashboard():
    dept = normalize_department(request.args.get('dept', '').strip())
    query = request.args.get('q', '').strip()
    all_items = get_all_items_with_stock()
    positive = [d for d in all_items if d['real_stock'] > 0]
    if query:
        positive = [d for d in positive if query.lower() in d['name'].lower()]
    
    dept_items = {}
    for item in positive:
        dept_items.setdefault(item['department'], []).append(item)
    for d in dept_items: dept_items[d].sort(key=lambda x: x['name'])
    
    if dept:
        my_items = dept_items.get(dept, [])
    
    quick = '<div class="quick-links">'
    for d in ['综合部', '工程部', '秩序部', '客服部']:
        quick += f'<a href="/dashboard?dept={d}" class="{"active" if d==dept else ""}">{d}</a>'
    quick += f'<a href="/dashboard" class="{"active" if not dept else ""}">🌐 全局</a></div>'
    
    search = f'<form method="get" action="/dashboard" class="search-bar"><input type="hidden" name="dept" value="{dept}"><input type="text" name="q" value="{query}" placeholder="🔍 搜索物资"><button type="submit">搜索</button></form>'
    
    if dept:
        rows = ''
        for item in my_items:
            lc = '#e74c3c' if item['is_low'] else '#333'
            badge = ' <span style="color:#e74c3c;font-size:11px;">⚠️</span>' if item['is_low'] else ''
            rows += f'<tr><td><a href="/item/{item["id"]}/history" style="color:#333;text-decoration:none;">{item["name"]}</a>{badge}<span style="color:#ccc;font-size:11px;margin-left:6px;">{item["asset_code"]}</span></td><td class="num" style="color:{lc};">{item["real_stock"]} {item["unit"]}</td></tr>'
        if not rows: rows = '<tr><td colspan="2" style="text-align:center;color:#888;padding:15px;">暂无</td></tr>'
        body = f'''<div class="section-title">📦 {dept}物资库存清单<span>{len(my_items)} 项</span></div>
        <div class="scroll-card"><table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    else:
        body = ''
        if dept_items:
            body = f'<div class="section-title">📦 各部门物资库存清单<span>{len(positive)} 项</span></div>'
            for d, items in dept_items.items():
                rows = ''
                for item in items:
                    lc = '#e74c3c' if item['is_low'] else '#333'
                    badge = ' <span style="color:#e74c3c;font-size:11px;">⚠️</span>' if item['is_low'] else ''
                    rows += f'<tr><td><a href="/item/{item["id"]}/history" style="color:#333;text-decoration:none;">{item["name"]}</a>{badge}<span style="color:#ccc;font-size:11px;margin-left:6px;">{item["asset_code"]}</span></td><td class="num" style="color:{lc};">{item["real_stock"]} {item["unit"]}</td></tr>'
                body += f'<div class="scroll-card" style="margin-bottom:12px;"><div style="font-weight:700;font-size:15px;margin-bottom:10px;">📁 {d}（{len(items)} 项）</div><table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{rows}</tbody></table></div>'
        else:
            body = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无有库存的物资</div>'
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>环球梦大厦 · 物资看板</title><style>{BASE_STYLE}</style></head><body>
    {render_header()}{quick}{search}{body}
    <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div></body></html>'''

# ================= 管理后台 =================

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
            if dept_filter: sql += " AND i.department = %s"; params.append(dept_filter)
            sql += " GROUP BY i.id ORDER BY i.department, i.name"
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    finally:
        conn.close()
    
    rows_html = ''
    for r in rows:
        rs = r['initial_stock'] + r['total_in'] - r['total_out']
        opts = ''.join([f'<option value="{d}" {"selected" if d==r["department"] else ""}>{d}</option>' for d in ['综合部','工程部','秩序部','客服部','未分配']])
        sc = '#e74c3c' if rs <= r['low_stock_threshold'] else '#333'
        rows_html += f'''<tr data-id="{r['id']}">
            <td style="font-size:11px;color:#999;font-family:monospace;">{r['asset_code'] or '-'}</td>
            <td style="font-size:11px;color:#bbb;font-family:monospace;">{r['qr_code'] or '-'}</td>
            <td><input class="name" type="text" value="{r['name']}" style="width:150px;padding:4px;"></td>
            <td><input class="unit" type="text" value="{r['unit']}" style="width:40px;padding:4px;"></td>
            <td><select class="department" style="padding:4px;">{opts}</select></td>
            <td><input class="initial_stock" type="number" value="{r['initial_stock']}" style="width:70px;padding:4px;"></td>
            <td><input class="low_stock_threshold" type="number" value="{r['low_stock_threshold']}" style="width:60px;padding:4px;"></td>
            <td style="font-weight:600;color:{sc};">{rs}</td>
            <td style="white-space:nowrap;">
                <button onclick="saveItem(this, '{r['id']}')" style="padding:4px 8px;background:#2ecc71;color:#fff;border:none;border-radius:4px;cursor:pointer;">保存</button>
                <button onclick="adjustStock('{r['id']}', 'in')" style="padding:4px 8px;background:#3498db;color:#fff;border:none;border-radius:4px;cursor:pointer;">入库</button>
                <button onclick="adjustStock('{r['id']}', 'out')" style="padding:4px 8px;background:#f39c12;color:#fff;border:none;border-radius:4px;cursor:pointer;">出库</button>
                <a href="/item/{r['id']}/history" target="_blank" style="padding:4px 8px;background:#9b59b6;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;">流水</a>
                <button onclick="softDelete('{r['id']}')" style="padding:4px 8px;background:#e74c3c;color:#fff;border:none;border-radius:4px;cursor:pointer;">删除</button>
            </td></tr>'''
    
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>物资管理</title><style>
    body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }}
    .top-links {{ margin-bottom: 15px; }}
    .top-links a {{ display: inline-block; padding: 8px 16px; background: white; border-radius: 8px; color: #333; text-decoration: none; margin-right: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); font-size: 14px; }}
    .add-box, .toolbar {{ background: white; padding: 12px; border-radius: 12px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
    .toolbar input, .toolbar select, .add-box input, .add-box select {{ padding: 8px; border: 1px solid #ddd; border-radius: 6px; margin-right: 6px; }}
    .toolbar button, .add-box button {{ padding: 8px 16px; background: #333; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }}
    table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
    th {{ background: #f8f9fa; padding: 10px 6px; text-align: left; font-size: 12px; color: #666; border-bottom: 2px solid #eee; }}
    td {{ padding: 6px; border-bottom: 1px solid #eee; font-size: 13px; }}
    </style><script>
    function saveItem(btn, id) {{ const r = btn.closest('tr');
      fetch('/api/admin/update', {{method:'POST',headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{id, name: r.querySelector('.name').value, unit: r.querySelector('.unit').value,
          department: r.querySelector('.department').value, initial_stock: parseInt(r.querySelector('.initial_stock').value||'0'),
          low_stock_threshold: parseInt(r.querySelector('.low_stock_threshold').value||'5')}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function softDelete(id) {{ if (!confirm('确认删除？')) return;
      fetch('/api/admin/soft_delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function adjustStock(id, type) {{ const q = prompt('数量：'); if(!q) return;
      fetch('/api/admin/adjust_stock',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id, type, quantity: parseInt(q)}})}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    function addItem() {{ const d = {{name: document.getElementById('n_name').value, unit: document.getElementById('n_unit').value||'个',
        department: document.getElementById('n_dept').value, initial_stock: parseInt(document.getElementById('n_stock').value||'0'),
        low_stock_threshold: parseInt(document.getElementById('n_thr').value||'5')}};
      if(!d.name){{alert('填名称');return;}}
      fetch('/api/admin/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}})
        .then(x=>x.json()).then(res=>{{alert(res.message); if(res.success) location.reload();}}); }}
    </script></head><body>
    <h1 style="font-size:20px;">🛠️ 物资管理后台</h1>
    <div class="top-links"><a href="/dashboard">📊 看板</a><a href="/admin">📦 物资</a><a href="/admin/upload_qrcode">📤 上传二维码表</a><a href="/admin/logs">📜 日志</a></div>
    <div class="add-box"><b>➕ 新增</b>
      <input id="n_name" placeholder="名称" style="width:180px;">
      <input id="n_unit" placeholder="单位" style="width:60px;">
      <select id="n_dept"><option>综合部</option><option>工程部</option><option>秩序部</option><option>客服部</option></select>
      <input id="n_stock" type="number" placeholder="库存" style="width:70px;">
      <input id="n_thr" type="number" placeholder="预警值" style="width:70px;">
      <button onclick="addItem()">新增</button></div>
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
    <table><thead><tr><th>资产编号</th><th>二维码</th><th>名称</th><th>单位</th><th>部门</th><th>初始库存</th><th>预警值</th><th>当前库存</th><th>操作</th></tr></thead>
    <tbody>{rows_html if rows_html else '<tr><td colspan="9" style="text-align:center;padding:40px;color:#888;">暂无</td></tr>'}</tbody></table>
    </body></html>'''

@app.route('/admin/upload_qrcode')
def admin_upload_page():
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>上传草料二维码表</title>
    <style>body{font-family:-apple-system,sans-serif;background:#f0f2f5;margin:0;padding:15px;}
    .card{background:white;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,0.05);max-width:600px;margin:0 auto;}
    h1{font-size:20px;}.hint{color:#888;font-size:13px;margin:10px 0;line-height:1.6;}
    input[type=file]{padding:10px;border:2px dashed #ddd;border-radius:8px;width:100%;box-sizing:border-box;}
    button{padding:12px 24px;background:#333;color:white;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-top:16px;}
    .top-links a{display:inline-block;padding:8px 16px;background:white;border-radius:8px;color:#333;text-decoration:none;margin-right:8px;box-shadow:0 2px 6px rgba(0,0,0,0.05);font-size:14px;}
    </style></head><body>
    <h1>📤 上传草料二维码导出表</h1>
    <div class="top-links" style="margin-bottom:15px;"><a href="/admin">📦 物资</a><a href="/dashboard">📊 看板</a></div>
    <div class="card">
    <p class="hint">📌 操作说明：<br>
    1. 草料后台导出子码列表，保存为 CSV 格式（UTF-8）<br>
    2. 确保表头包含"资产编号"、"二维码地址"两列<br>
    3. 点击下面选择文件上传<br>
    4. 系统会自动按"资产编号"匹配数据库物资，写入二维码唯一编号</p>
    <form method="post" action="/api/admin/upload_qrcode" enctype="multipart/form-data">
      <input type="file" name="file" accept=".csv,.txt" required>
      <button type="submit">开始上传并匹配</button>
    </form></div></body></html>'''

@app.route('/api/admin/upload_qrcode', methods=['POST'])
def upload_qrcode():
    f = request.files.get('file')
    if not f:
        return "<h3>未选择文件</h3><a href='/admin/upload_qrcode'>返回</a>"
    try:
        content = f.read().decode('utf-8-sig')
    except:
        content = f.read().decode('gbk', errors='ignore')
    
    reader = csv.DictReader(io.StringIO(content), delimiter='\t')
    if not reader.fieldnames or len(reader.fieldnames) < 3:
        reader = csv.DictReader(io.StringIO(content))
    
    asset_col = None
    url_col = None
    for col in reader.fieldnames or []:
        if '资产编号' in col or '物资编码' in col: asset_col = col
        if '二维码地址' in col or 'url' in col.lower(): url_col = col
    
    if not asset_col or not url_col:
        return f"<h3>未找到必要的列</h3><p>需要'资产编号'和'二维码地址'列，实际列：{reader.fieldnames}</p><a href='/admin/upload_qrcode'>返回</a>"
    
    conn = get_db()
    success, fail = 0, 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            for row in reader:
                asset = (row.get(asset_col) or '').strip()
                url = (row.get(url_col) or '').strip()
                if not asset or not url: continue
                m = re.search(r'/([^/]+)$', url)
                qr_code = m.group(1) if m else ''
                if not qr_code: continue
                cursor.execute('UPDATE items SET qr_code = %s WHERE asset_code = %s', (qr_code, asset))
                if cursor.rowcount > 0: success += 1
                else: fail += 1
        conn.commit()
    finally:
        conn.close()
    
    return f'''<html><head><meta charset="UTF-8"><title>完成</title>
    <style>body{{font-family:-apple-system,sans-serif;background:#f0f2f5;padding:40px;text-align:center;}}
    .card{{background:white;border-radius:16px;padding:40px;box-shadow:0 4px 16px rgba(0,0,0,0.06);max-width:500px;margin:0 auto;}}
    h2{{color:#2ecc71;}} .fail{{color:#e74c3c;}}
    a{{display:inline-block;margin-top:20px;padding:12px 24px;background:#333;color:white;border-radius:8px;text-decoration:none;}}
    </style></head><body><div class="card">
    <h2>✅ 匹配完成</h2>
    <p>成功匹配：<b>{success}</b> 条</p>
    <p class="fail">未匹配：<b>{fail}</b> 条（数据库里没有对应资产编号的物资）</p>
    <a href="/admin">返回物资管理</a></div></body></html>'''

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
        rows += f'<tr><td style="font-size:12px;">{l["created_at"].strftime("%Y-%m-%d %H:%M")}</td><td style="font-weight:600;">{l["action"]}</td><td>{l.get("item_name") or "-"}</td><td style="font-size:12px;">{l.get("detail") or ""}</td></tr>'
    if not rows: rows = '<tr><td colspan="4" style="text-align:center;padding:30px;color:#888;">暂无</td></tr>'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>操作日志</title><style>
    body{{font-family:-apple-system,sans-serif;background:#f0f2f5;padding:15px;}}
    table{{width:100%;background:white;border-radius:12px;border-collapse:collapse;box-shadow:0 2px 8px rgba(0,0,0,0.05);}}
    th{{background:#f8f9fa;padding:10px;text-align:left;font-size:13px;color:#666;}}
    td{{padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;}}
    .top-links a{{display:inline-block;padding:8px 16px;background:white;border-radius:8px;color:#333;text-decoration:none;margin-right:8px;box-shadow:0 2px 6px rgba(0,0,0,0.05);font-size:14px;}}
    </style></head><body>
    <div class="top-links"><a href="/admin">📦 物资</a><a href="/dashboard">📊 看板</a></div>
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
            nn = d.get('name', old['name'])
            nu = d.get('unit', old['unit'])
            nd = normalize_department(d.get('department', old['department']))
            ni = int(d.get('initial_stock', old['initial_stock']))
            nt = int(d.get('low_stock_threshold', old['low_stock_threshold']))
            cursor.execute('UPDATE items SET name=%s, unit=%s, department=%s, initial_stock=%s, low_stock_threshold=%s WHERE id=%s',
                           (nn, nu, nd, ni, nt, iid))
            log_audit(cursor, '更新物资', iid, nn, f'{old["name"]}→{nn}, 库存{old["initial_stock"]}→{ni}')
        conn.commit()
        return jsonify({'success': True, 'message': f'已更新 {nn}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/soft_delete', methods=['POST'])
def admin_soft_delete():
    d = request.json or {}
    iid = d.get('id')
    if not iid: return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT name FROM items WHERE id=%s', (iid,))
            it = cursor.fetchone()
            if not it: return jsonify({'success': False, 'message': '不存在'})
            cursor.execute('UPDATE items SET is_deleted=1 WHERE id=%s', (iid,))
            log_audit(cursor, '删除物资', iid, it['name'], '软删除')
        conn.commit()
        return jsonify({'success': True, 'message': f'已删除 {it["name"]}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/add', methods=['POST'])
def admin_add():
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name: return jsonify({'success': False, 'message': '名称不能为空'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            bid = generate_item_id(name + str(datetime.now()))
            iid = bid
            suffix = 1
            while True:
                cursor.execute('SELECT id FROM items WHERE id=%s', (iid,))
                if not cursor.fetchone(): break
                iid = f"{bid[:6]}{suffix}"; suffix += 1
            dept = normalize_department(d.get('department', '综合部'))
            cursor.execute('INSERT INTO items (id,name,unit,department,initial_stock,low_stock_threshold) VALUES (%s,%s,%s,%s,%s,%s)',
                           (iid, name, d.get('unit', '个'), dept, int(d.get('initial_stock', 0)), int(d.get('low_stock_threshold', 5))))
            log_audit(cursor, '新增物资', iid, name, f'归属{dept}')
        conn.commit()
        return jsonify({'success': True, 'message': f'已新增 {name}（ID: {iid}）'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/adjust_stock', methods=['POST'])
def admin_adjust_stock():
    d = request.json or {}
    iid = d.get('id'); typ = d.get('type'); qty = d.get('quantity')
    if not iid or typ not in ['in', 'out'] or not qty:
        return jsonify({'success': False, 'message': '参数错误'})
    try:
        qty = int(qty)
        if qty <= 0: raise ValueError()
    except: return jsonify({'success': False, 'message': '数量错误'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT name FROM items WHERE id=%s', (iid,))
            it = cursor.fetchone()
            if not it: return jsonify({'success': False, 'message': '不存在'})
            cursor.execute('INSERT INTO transactions (item_id,type,quantity,note,operator) VALUES (%s,%s,%s,%s,%s)',
                           (iid, typ, qty, '后台调整', '管理员'))
            lb = '入库' if typ == 'in' else '出库'
            log_audit(cursor, f'{lb}调整', iid, it['name'], f'{lb}{qty}')
        conn.commit()
        return jsonify({'success': True, 'message': f'已{lb} {qty}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

# ================= Webhook：用 qr_code 匹配 =================

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
        
        qr_code = ref_qr.get('code') or ''    # 🌟 二维码唯一编号
        item_name = ref_qr.get('fields', {}).get('资产名称') or ref_qr.get('name')
        
        if not item_name: return jsonify({'status': 'error', 'message': '未找到物资名称'}), 400
        department = fields.get('所属部门') or fields.get('领用部门') or fields.get('部门')
        department = normalize_department(department)
        
        if '出库' in form_name:
            trans_type = 'out'; quantity = fields.get('出库数量')
        elif '入库' in form_name:
            trans_type = 'in'; quantity = fields.get('入库数量')
        else:
            return jsonify({'status': 'error', 'message': f'未知表单'}), 400
        if not quantity: return jsonify({'status': 'error', 'message': '未找到数量'}), 400
        
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"USE {MYSQL_DATABASE}")
                # 🌟 第一优先：按 qr_code 精确匹配
                item_id = None
                if qr_code:
                    cursor.execute('SELECT id FROM items WHERE qr_code=%s AND is_deleted=0', (qr_code,))
                    row = cursor.fetchone()
                    if row: item_id = row['id']
                # 第二优先：按名字匹配
                if not item_id:
                    cursor.execute('SELECT id FROM items WHERE name=%s AND is_deleted=0', (item_name,))
                    row = cursor.fetchone()
                    if row: item_id = row['id']
                # 兜底：自动创建
                if not item_id:
                    item_id = generate_item_id(item_name)
                    unit = fields.get('单选项', '个')
                    cursor.execute('INSERT INTO items (id,name,unit,initial_stock,low_stock_threshold,qr_code) VALUES (%s,%s,%s,0,5,%s)',
                                   (item_id, item_name, unit, qr_code))
                    log_audit(cursor, '新增物资', item_id, item_name, f'扫码自动创建 qr_code={qr_code}')
                
                cursor.execute('INSERT INTO transactions (item_id,type,quantity,department,note) VALUES (%s,%s,%s,%s,%s)',
                               (item_id, trans_type, int(quantity), department, f'草料同步-{form_name}'))
            conn.commit()
        finally:
            conn.close()
        return jsonify({'status': 'success', 'message': f'{item_name} 已同步'}), 200
    except Exception as e:
        logging.error(f"处理失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ================= 重新导入（按物资编码，不再加括号） =================

@app.route('/api/init_data')
def init_data():
    import pandas as pd
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('DELETE FROM items')
            cursor.execute('DELETE FROM transactions')
            df = pd.read_excel('full_items.xlsx', sheet_name='物资编码')
            df.columns = [str(c).replace('<br>', '').replace('\n', '').strip() for c in df.columns]
            count = 0
            for idx, row in df.iterrows():
                name = str(row['物资名称']).replace("\n", "").strip()
                if not name or name == 'nan': continue
                asset_code = str(row['物资编码']).strip() if pd.notna(row.get('物资编码')) else ''
                if not asset_code: continue
                dept = str(row['申采部门']).replace('<br>', '').replace('\n', '').strip() if pd.notna(row.get('申采部门')) else '综合部'
                if dept == 'nan' or not dept: dept = '综合部'
                dept = normalize_department(dept)
                unit = str(row['计量单位']).replace("\n", "").strip() if pd.notna(row.get('计量单位')) else '个'
                try: stock = int(float(row['数量'])) if pd.notna(row.get('数量')) else 0
                except: stock = 0
                # 用物资编码生成唯一ID，同编码只保留一条
                item_id = hashlib.md5(asset_code.encode()).hexdigest()[:10]
                cursor.execute('''REPLACE INTO items 
                    (id,name,unit,initial_stock,low_stock_threshold,department,asset_code)
                    VALUES (%s,%s,%s,%s,5,%s,%s)''',
                    (item_id, name, unit, stock, dept, asset_code))
                count += 1
            log_audit(cursor, '批量导入', None, None, f'按物资编码导入{count}条')
        conn.commit()
        return f"🎉 成功导入 {count} 条物资！每条都有唯一的物资编码，重名不再冲突。"
    except Exception as e:
        return f"导入失败: {str(e)}"
    finally:
        conn.close()

@app.route('/api/export_links')
def export_links():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT name, asset_code, id FROM items WHERE is_deleted=0 ORDER BY name')
            items = cursor.fetchall()
            base = f"https://{request.host}/item/"
            lines = ["物资名称,资产编号,专属链接"]
            for it in items:
                lines.append(f"{it['name']},{it['asset_code']},{base}{it['id']}")
            return "\n".join(lines)
    finally:
        conn.close()

@app.route('/api/check_alerts')
def check_alerts():
    all_items = get_all_items_with_stock()
    low = [d for d in all_items if d['is_low']]
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
            lines.append(f"> {it['name']}（{it['asset_code']}）：剩余 **{it['real_stock']}** {it['unit']}（阈值 {it['low_stock_threshold']}）")
        lines.append("")
    msg = "\n".join(lines)
    ok = send_alert_to_group(msg, list(at_set))
    return f"✅ 已推送，共@ {len(at_set)}人" if ok else "❌ 推送失败"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)