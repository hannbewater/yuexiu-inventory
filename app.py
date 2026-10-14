from flask import Flask, request, jsonify
import logging
import pymysql
import os
import hashlib
import re
import requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

# ================= 数据库配置 =================
MYSQL_ADDRESS = os.environ.get("MYSQL_ADDRESS")
MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "inventory_db")

# ================= 部门名称归一化 =================
DEPT_ALIAS = {'安管部': '秩序部'}
def normalize_department(dept):
    if not dept: return dept
    return DEPT_ALIAS.get(str(dept).strip(), str(dept).strip())

# ================= 钉钉统一预警群配置 =================
ALERT_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=2b99041269aa1070355657d9060ae29ef6c064cfeb3b2565f0c2b0ac6877d6c1'
DEPT_CONTACTS = {
    '综合部': ['19874467403'],
    '工程部': ['18666082342'],
    '秩序部': ['19874467403'],
    '客服部': ['18666082342'],
}

def get_db():
    """获取数据库连接，带自动重连和超时设置"""
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0] if MYSQL_ADDRESS else "127.0.0.1",
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30
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
                    department VARCHAR(50) DEFAULT NULL
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            for sql in [
                "ALTER TABLE items ADD COLUMN department VARCHAR(50) DEFAULT NULL",
                "ALTER TABLE transactions ADD COLUMN department VARCHAR(50) DEFAULT NULL"
            ]:
                try: cursor.execute(sql)
                except Exception: pass
            for sql in [
                'CREATE INDEX idx_item_id ON transactions(item_id)',
                'CREATE INDEX idx_name ON items(name)',
                'CREATE INDEX idx_department ON transactions(department)'
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

# ================= 核心：一次查询取回所有物资库存 =================
def get_all_items_with_stock():
    """一次性 JOIN 查询，避免对391个物资逐个查询导致连接断开"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''
                SELECT 
                    i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department,
                    COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                    COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i
                LEFT JOIN transactions t ON i.id = t.item_id
                GROUP BY i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department
            ''')
            rows = cursor.fetchall()
            result = []
            for row in rows:
                real_stock = row['initial_stock'] + row['total_in'] - row['total_out']
                result.append({
                    'id': row['id'],
                    'name': re.sub(r'\(\d+\)$', '', row['name']),
                    'unit': row['unit'],
                    'real_stock': real_stock,
                    'total_in': row['total_in'],
                    'total_out': row['total_out'],
                    'threshold': row['low_stock_threshold'],
                    'department': normalize_department(row.get('department')) or '未分配',
                    'is_low': real_stock <= row['low_stock_threshold']
                })
            return result
    finally:
        conn.close()

def send_alert_to_group(message, at_mobiles):
    if not ALERT_WEBHOOK:
        return False
    at_text = " ".join([f"@{m}" for m in at_mobiles]) if at_mobiles else ""
    full_content = f"{message}\n\n{at_text}" if at_text else message
    data = {
        "msgtype": "markdown",
        "markdown": {"title": "库存预警", "text": full_content},
        "at": {"atMobiles": at_mobiles, "isAtAll": False}
    }
    headers = {'Content-Type': 'application/json;charset=utf-8'}
    try:
        resp = requests.post(ALERT_WEBHOOK, json=data, headers=headers, timeout=5)
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
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }
    .main-header { display: flex; align-items: center; background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%); padding: 18px 22px; border-radius: 14px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; }
    .main-header .logo { height: 46px; object-fit: contain; margin-right: 16px; }
    .main-header .title-block { flex: 1; }
    .main-header .project-name { font-size: 24px; font-weight: 800; color: #1a1a1a; letter-spacing: 2px; line-height: 1.2; }
    .main-header .sub-title { font-size: 12px; color: #999; margin-top: 4px; letter-spacing: 4px; }
    .section-title { font-size: 16px; font-weight: 700; color: #333; margin: 24px 0 12px 0; }
    .section-title span { background: #e74c3c; color: white; font-size: 12px; padding: 2px 8px; border-radius: 10px; margin-left: 8px; font-weight: normal; }
    .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    .scroll-card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); max-height: 340px; overflow-y: auto; }
    .scroll-card::-webkit-scrollbar { width: 6px; }
    .scroll-card::-webkit-scrollbar-thumb { background: #ccc; border-radius: 3px; }
    .scroll-card::-webkit-scrollbar-track { background: #f1f1f1; border-radius: 3px; }
    .low-item { background: #fff5f5; border-left: 4px solid #e74c3c; padding: 12px; margin-bottom: 8px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; }
    .low-item:last-child { margin-bottom: 0; }
    .low-item .name { font-weight: 600; color: #333; }
    .low-item .meta { color: #888; font-size: 12px; margin-left: 8px; }
    .low-item .stock { color: #e74c3c; font-weight: 700; font-size: 18px; }
    .ok-msg { text-align: center; color: #2ecc71; padding: 20px; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th { color: #888; text-align: left; padding-bottom: 8px; font-weight: 500; position: sticky; top: 0; background: white; }
    td { padding: 8px 0; border-bottom: 1px solid #eee; }
    td.num { text-align: right; color: #e74c3c; font-weight: 600; }
    .footer { text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }
    .quick-links { text-align: center; margin: 15px 0; }
    .quick-links a { display: inline-block; padding: 6px 14px; background: white; border-radius: 20px; color: #333; text-decoration: none; font-size: 13px; margin: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
    .quick-links a.active { background: #333; color: white; }
    .view-tabs { display: flex; gap: 6px; margin-bottom: 16px; background: white; padding: 6px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    .view-tabs a { flex: 1; text-align: center; padding: 12px; border-radius: 8px; text-decoration: none; color: #666; font-size: 14px; font-weight: 600; transition: all 0.2s; }
    .view-tabs a.active { background: #333; color: white; }
    .view-tabs a .hint { display: block; font-size: 11px; font-weight: 400; opacity: 0.7; margin-top: 2px; letter-spacing: 1px; }
    .view-tabs a.active .hint { opacity: 0.85; }
    .search-bar { display: flex; gap: 8px; margin: 15px 0; }
    .search-bar input[type="text"] { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 10px; font-size: 15px; outline: none; }
    .search-bar input[type="text"]:focus { border-color: #333; }
    .search-bar button { padding: 12px 24px; background: #333; color: white; border: none; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer; }
'''

def render_low_stock_html(items):
    if not items:
        return '<div class="ok-msg">✅ 所有物资库存充足</div>'
    html = ''
    for i in items:
        dept_tag = f'<span class="meta">[{i["department"]}]</span>' if i.get('department') else ''
        html += f'''
        <div class="low-item">
            <div><span class="name">{i['name']}</span>{dept_tag}<span class="meta">预警值 {i['threshold']}</span></div>
            <div class="stock">{i['real_stock']} {i['unit']}</div>
        </div>
        '''
    return html

def render_view_tabs(current_view, dept, query):
    def build_url(view):
        params = [f"view={view}"]
        if dept: params.append(f"dept={dept}")
        if query: params.append(f"q={query}")
        return "/dashboard?" + "&".join(params)
    common_active = 'active' if current_view == 'common' else ''
    all_active = 'active' if current_view == 'all' else ''
    return f'''
    <div class="view-tabs">
        <a href="{build_url('common')}" class="{common_active}">常用消耗物资看板<span class="hint">重点关注 · 待配置</span></a>
        <a href="{build_url('all')}" class="{all_active}">全物资看板<span class="hint">全部物资</span></a>
    </div>
    '''

# ================= 页面路由 =================

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
                SELECT 
                    i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department,
                    COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                    COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i
                LEFT JOIN transactions t ON i.id = t.item_id
                WHERE i.id = %s
                GROUP BY i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department
            ''', (item_id,))
            row = cursor.fetchone()
            if not row:
                return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            real_stock = row['initial_stock'] + row['total_in'] - row['total_out']
            data = {
                'id': row['id'],
                'name': re.sub(r'\(\d+\)$', '', row['name']),
                'unit': row['unit'],
                'real_stock': real_stock,
                'total_in': row['total_in'],
                'total_out': row['total_out'],
                'threshold': row['low_stock_threshold'],
                'department': normalize_department(row.get('department')) or '未分配',
                'is_low': real_stock <= row['low_stock_threshold']
            }
    finally:
        conn.close()

    stock_color = '#e74c3c' if data['is_low'] else '#2ecc71'
    html = f'''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>{data['name']} - 库存看板</title>
        <style>
            {BASE_STYLE}
            .item-card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; text-align: center; }}
            .item-name {{ font-size: 22px; font-weight: 700; color: #333; margin-bottom: 8px; }}
            .label {{ font-size: 14px; color: #888; margin-bottom: 8px; }}
            .stock-number {{ font-size: 64px; font-weight: 800; color: {stock_color}; line-height: 1; margin: 10px 0; }}
            .unit {{ font-size: 20px; color: #999; font-weight: 500; }}
            .warning {{ background: #fff3cd; border: 1px solid #ffc107; color: #856404; padding: 10px 16px; border-radius: 10px; margin-top: 15px; font-size: 14px; font-weight: 600; display: inline-block; }}
            .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
            .stat-box {{ background: white; border-radius: 16px; padding: 20px; text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
            .stat-value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
            .stat-in .stat-value {{ color: #2ecc71; }}
            .stat-out .stat-value {{ color: #e74c3c; }}
        </style>
    </head>
    <body>
        {render_header()}
        <div class="item-card">
            <div class="item-name">{data['name']}</div>
            <div class="label">实时库存</div>
            <div class="stock-number">{data['real_stock']}<span class="unit"> {data['unit']}</span></div>
            {f'<div class="warning">⚠️ 库存不足，请及时采购！</div>' if data['is_low'] else ''}
        </div>
        <div class="stats-grid">
            <div class="stat-box stat-in"><div class="label">累计入库</div><div class="stat-value">+{data['total_in']}</div></div>
            <div class="stat-box stat-out"><div class="label">累计出库</div><div class="stat-value">-{data['total_out']}</div></div>
        </div>
        <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
    </body>
    </html>
    '''
    return html

@app.route('/dashboard')
def dashboard():
    dept = normalize_department(request.args.get('dept', '').strip())
    query = request.args.get('q', '').strip()
    view = request.args.get('view', 'all').strip()
    if view not in ['common', 'all']: view = 'all'
    
    # 🌟 一次性取出所有物资库存（避免N次查询断开连接）
    all_items_data = get_all_items_with_stock()
    all_low = [d for d in all_items_data if d['is_low']]
    all_low.sort(key=lambda x: x['real_stock'])
    
    search_results = None
    if query:
        search_results = [d for d in all_items_data if query.lower() in d['name'].lower()]
        search_results.sort(key=lambda x: x['name'])
    
    # 部门相关数据
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            if dept:
                my_low = [d for d in all_low if d['department'] == dept]
                cursor.execute('''
                    SELECT i.name, i.unit, SUM(t.quantity) as total_qty
                    FROM transactions t
                    JOIN items i ON t.item_id = i.id
                    WHERE t.type = 'out' AND t.department = %s AND t.created_at >= %s
                    GROUP BY i.name, i.unit
                    ORDER BY total_qty DESC
                ''', (dept, thirty_days_ago))
                usages = cursor.fetchall()
                dept_dict = None
            else:
                cursor.execute('''
                    SELECT t.department, i.name, i.unit, SUM(t.quantity) as total_out_qty
                    FROM transactions t
                    JOIN items i ON t.item_id = i.id
                    WHERE t.type = 'out' AND t.department IS NOT NULL AND t.created_at >= %s
                    GROUP BY t.department, i.name, i.unit
                    ORDER BY t.department, total_out_qty DESC
                ''', (thirty_days_ago,))
                dept_usage = cursor.fetchall()
                dept_dict = {}
                for row in dept_usage:
                    d = normalize_department(row['department'])
                    dept_dict.setdefault(d, []).append({
                        'name': re.sub(r'\(\d+\)$', '', row['name']),
                        'unit': row['unit'],
                        'qty': row['total_out_qty']
                    })
    finally:
        conn.close()

    quick_links = '<div class="quick-links">'
    for d in ['综合部', '工程部', '秩序部', '客服部']:
        active = 'active' if d == dept else ''
        quick_links += f'<a href="/dashboard?view={view}&dept={d}" class="{active}">{d}</a>'
    active_global = 'active' if not dept else ''
    quick_links += f'<a href="/dashboard?view={view}" class="{active_global}">🌐 全局看板</a></div>'

    view_tabs = render_view_tabs(view, dept, query)

    search_bar = f'''
    <form method="get" action="/dashboard" class="search-bar">
        <input type="hidden" name="view" value="{view}">
        <input type="hidden" name="dept" value="{dept}">
        <input type="text" name="q" value="{query}" placeholder="🔍 搜索物资名称...">
        <button type="submit">搜索</button>
    </form>
    '''

    search_html = ''
    if query and search_results is not None:
        if search_results:
            rows = ''
            for item in search_results:
                status_color = '#e74c3c' if item['is_low'] else '#2ecc71'
                status_text = '⚠️ 需采购' if item['is_low'] else '✅ 充足'
                bg_color = '#fff5f5' if item['is_low'] else '#f0fdf4'
                rows += f'''
                <div class="low-item" style="border-left-color: {status_color}; background: {bg_color};">
                    <div>
                        <span class="name">{item['name']}</span>
                        <span class="meta">[{item['department']}]</span>
                        <span class="meta" style="color:{status_color}; font-weight:600;">{status_text}</span>
                    </div>
                    <div class="stock" style="color:{status_color};">{item['real_stock']} {item['unit']}</div>
                </div>
                '''
            search_html = f'''<div class="section-title">🔍 搜索结果："{query}"<span>{len(search_results)} 项</span></div><div class="scroll-card">{rows}</div>'''
        else:
            search_html = f'''<div class="section-title">🔍 搜索结果："{query}"<span>0 项</span></div><div class="card" style="text-align:center;color:#888;padding:20px;">没有找到匹配的物资</div>'''

    view_label = "常用消耗物资看板" if view == 'common' else "全物资看板"

    if dept:
        usage_rows = ''.join([f'<tr><td>{u["name"]}</td><td class="num">{u["total_qty"]} {u["unit"]}</td></tr>' for u in usages])
        if not usage_rows:
            usage_rows = '<tr><td colspan="2" style="text-align:center;color:#888;padding:15px;">暂无领用记录</td></tr>'
        html = f'''
        <!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"><title>{dept} · {view_label}</title><style>{BASE_STYLE}</style></head><body>
            {render_header()}{view_tabs}{quick_links}{search_bar}{search_html}
            <div class="section-title">🚨 本部门物资预警<span>{len(my_low)} 项</span></div><div class="scroll-card">{render_low_stock_html(my_low)}</div>
            <div class="section-title" style="margin-top:24px;">📦 全部物资预警（含其他部门）<span>{len(all_low)} 项</span></div><div class="scroll-card">{render_low_stock_html(all_low)}</div>
            <div class="section-title" style="margin-top:24px;">📊 {dept}近30天领用记录</div>
            <div class="scroll-card"><table><thead><tr><th>物资</th><th style="text-align:right;">领用量</th></tr></thead><tbody>{usage_rows}</tbody></table></div>
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body></html>'''
    else:
        dept_html = ''
        if dept_dict:
            for d, usages in dept_dict.items():
                rows = ''.join([f'<tr><td>{u["name"]}</td><td class="num">{u["qty"]} {u["unit"]}</td></tr>' for u in usages])
                dept_html += f'''<div class="scroll-card" style="margin-bottom: 12px;"><div style="font-weight:700; font-size:15px; margin-bottom:10px;">📁 {d}</div><table><thead><tr><th>物资</th><th style="text-align:right;">近30天领用</th></tr></thead><tbody>{rows}</tbody></table></div>'''
        if not dept_html:
            dept_html = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无部门领用记录</div>'
        html = f'''
        <!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"><title>环球梦大厦 · {view_label}</title><style>{BASE_STYLE}</style></head><body>
            {render_header()}{view_tabs}{quick_links}{search_bar}{search_html}
            <div class="section-title">🚨 全部库存预警<span>{len(all_low)} 项</span></div><div class="scroll-card">{render_low_stock_html(all_low)}</div>
            <div class="section-title" style="margin-top:30px;">📊 各部门近30天领用统计</div>{dept_html}
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body></html>'''
    return html

# ================= API 接口 =================

@app.route('/api/webhook/grass', methods=['POST'])
def webhook_grass():
    data = request.json
    logging.info(f"收到草料推送数据: {data}")
    if not data:
        return jsonify({'status': 'error', 'message': '无数据'}), 400
    try:
        ref_data = data.get('data', {}).get('ref_data', {})
        form_name = ref_data.get('form', {}).get('name', '')
        fields = ref_data.get('fields', {})
        ref_qrcode = data.get('data', {}).get('ref_qrcode', {})
        item_name = ref_qrcode.get('fields', {}).get('资产名称') or ref_qrcode.get('name')
        if not item_name:
            return jsonify({'status': 'error', 'message': '未找到物资名称'}), 400
        department = fields.get('所属部门') or fields.get('领用部门') or fields.get('部门')
        department = normalize_department(department)
        if '出库' in form_name:
            trans_type = 'out'; quantity = fields.get('出库数量')
        elif '入库' in form_name:
            trans_type = 'in'; quantity = fields.get('入库数量')
        else:
            return jsonify({'status': 'error', 'message': f'未知的表单类型: {form_name}'}), 400
        if not quantity:
            return jsonify({'status': 'error', 'message': '未在表单中找到数量'}), 400
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"USE {MYSQL_DATABASE}")
                cursor.execute('SELECT id FROM items WHERE name = %s', (item_name,))
                item = cursor.fetchone()
                if not item:
                    item_id = generate_item_id(item_name)
                    unit = fields.get('单选项', '个')
                    cursor.execute('INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold) VALUES (%s, %s, %s, 0, 5)', (item_id, item_name, unit))
                else:
                    item_id = item['id']
                cursor.execute('INSERT INTO transactions (item_id, type, quantity, department, note) VALUES (%s, %s, %s, %s, %s)', (item_id, trans_type, int(quantity), department, f'草料同步-{form_name}'))
            conn.commit()
        finally:
            conn.close()
        return jsonify({'status': 'success', 'message': f'{item_name} 数据已成功同步'}), 200
    except Exception as e:
        logging.error(f"处理草料数据失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
            imported_names = set()
            count = 0
            for index, row in df.iterrows():
                name = str(row['物资名称']).replace("\n", "").strip()
                if not name or name == 'nan': continue
                original_name = name
                suffix = 1
                while name in imported_names:
                    name = f"{original_name}({suffix})"; suffix += 1
                imported_names.add(name)
                dept = str(row['申采部门']).replace('<br>', '').replace('\n', '').strip() if pd.notna(row['申采部门']) else '综合部'
                if dept == 'nan' or not dept: dept = '综合部'
                dept = normalize_department(dept)
                unit = str(row['计量单位']).replace("\n", "").strip() if pd.notna(row['计量单位']) else '个'
                try: initial_stock = int(float(row['数量'])) if pd.notna(row['数量']) else 0
                except: initial_stock = 0
                item_id = generate_item_id(name)
                cursor.execute('INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold, department) VALUES (%s, %s, %s, %s, 5, %s)', (item_id, name, unit, initial_stock, dept))
                count += 1
        conn.commit()
        return f"🎉 成功导入 {count} 种物资！"
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
            cursor.execute('SELECT name, id FROM items ORDER BY name')
            items = cursor.fetchall()
            base_url = f"https://{request.host}/item/"
            lines = ["物资名称,专属链接"]
            for item in items:
                lines.append(f"{item['name']},{base_url}{item['id']}")
            return "\n".join(lines)
    except Exception as e:
        return f"导出失败: {str(e)}"
    finally:
        conn.close()

@app.route('/api/check_alerts')
def check_alerts():
    """一次性取出所有物资，扫描缺货，汇总推送"""
    all_items_data = get_all_items_with_stock()
    all_low = [d for d in all_items_data if d['is_low']]
    
    if not all_low:
        return "✅ 所有部门物资库存充足，无需推送"
    
    # 按部门分组
    all_depts_low = {}
    for item in all_low:
        dept = item['department']
        all_depts_low.setdefault(dept, []).append(item)
    
    at_mobiles_set = set()
    for dept in all_depts_low.keys():
        for phone in DEPT_CONTACTS.get(dept, []):
            at_mobiles_set.add(phone)
    
    lines = ["#### 🚨 环球梦大厦 · 库存预警", ""]
    lines.append(f"**共 {len(all_depts_low)} 个部门有物资缺货，请及时采购：**")
    lines.append("")
    for dept, items in all_depts_low.items():
        lines.append(f"**【{dept}】**")
        for item in items:
            lines.append(f"> {item['name']}：剩余 **{item['real_stock']}** {item['unit']}（阈值 {item['threshold']}）")
        lines.append("")
    
    message = "\n".join(lines)
    at_mobiles = list(at_mobiles_set)
    success = send_alert_to_group(message, at_mobiles)
    
    if success:
        dept_summary = "、".join([f"{d}({len(items)}项)" for d, items in all_depts_low.items()])
        return f"✅ 已推送：{dept_summary}，共 @{len(at_mobiles)} 人"
    else:
        return "❌ 推送失败，请检查钉钉机器人配置"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)