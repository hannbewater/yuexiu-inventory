from flask import Flask, request, jsonify
import logging
import pymysql
import os
import hashlib
import re
import requests
from datetime import datetime, timedelta

# ================= 配置日志 =================
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

# ================= 工具函数 =================
def get_all_items_with_stock():
    """一次性 JOIN 查询所有物资库存"""
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
                    'full_name': row['name'],
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
    if not ALERT_WEBHOOK: return False
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
                'id': row['id'], 'name': re.sub(r'\(\d+\)$', '', row['name']),
                'unit': row['unit'], 'real_stock': real_stock,
                'total_in': row['total_in'], 'total_out': row['total_out'],
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
    
    all_items_data = get_all_items_with_stock()
    # 只显示有库存的物资
    positive_items = [d for d in all_items_data if d['real_stock'] > 0]
    
    search_results = None
    if query:
        search_results = [d for d in positive_items if query.lower() in d['name'].lower()]
        search_results.sort(key=lambda x: x['name'])
    
    # 按部门分组
    dept_items = {}
    for item in positive_items:
        d = item['department']
        dept_items.setdefault(d, []).append(item)
    for d in dept_items:
        dept_items[d].sort(key=lambda x: x['name'])
    
    # 部门领用统计
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            if dept:
                my_items = [d for d in positive_items if d['department'] == dept]
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
                my_items = None
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
        
        my_items_rows = ''
        for item in my_items:
            low_color = '#e74c3c' if item['is_low'] else '#333'
            low_badge = ' <span style="color:#e74c3c;font-size:11px;">⚠️ 需采购</span>' if item['is_low'] else ''
            my_items_rows += f'''<tr><td>{item['name']}{low_badge}</td><td class="num" style="color:{low_color};">{item['real_stock']} {item['unit']}</td></tr>'''
        if not my_items_rows:
            my_items_rows = '<tr><td colspan="2" style="text-align:center;color:#888;padding:15px;">暂无库存物资</td></tr>'
        
        html = f'''
        <!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"><title>{dept} · {view_label}</title><style>{BASE_STYLE}</style></head><body>
            {render_header()}{view_tabs}{quick_links}{search_bar}{search_html}
            <div class="section-title">📦 {dept}物资库存清单<span>{len(my_items)} 项</span></div>
            <div class="scroll-card"><table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{my_items_rows}</tbody></table></div>
            <div class="section-title" style="margin-top:24px;">📊 {dept}近30天领用记录</div>
            <div class="scroll-card"><table><thead><tr><th>物资</th><th style="text-align:right;">领用量</th></tr></thead><tbody>{usage_rows}</tbody></table></div>
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body></html>'''
    else:
        dept_html = ''
        if dept_items:
            for d, items in dept_items.items():
                rows = ''
                for item in items:
                    low_color = '#e74c3c' if item['is_low'] else '#333'
                    low_badge = ' <span style="color:#e74c3c;font-size:11px;">⚠️ 需采购</span>' if item['is_low'] else ''
                    rows += f'''<tr><td>{item['name']}{low_badge}</td><td class="num" style="color:{low_color};">{item['real_stock']} {item['unit']}</td></tr>'''
                dept_html += f'''<div class="scroll-card" style="margin-bottom: 12px;"><div style="font-weight:700; font-size:15px; margin-bottom:10px;">📁 {d}（{len(items)} 项）</div><table><thead><tr><th>物资</th><th style="text-align:right;">当前库存</th></tr></thead><tbody>{rows}</tbody></table></div>'''
        else:
            dept_html = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无有库存的物资</div>'
        
        usage_dept_html = ''
        if dept_dict:
            for d, usages in dept_dict.items():
                rows = ''.join([f'<tr><td>{u["name"]}</td><td class="num">{u["qty"]} {u["unit"]}</td></tr>' for u in usages])
                usage_dept_html += f'''<div class="scroll-card" style="margin-bottom: 12px;"><div style="font-weight:700; font-size:15px; margin-bottom:10px;">📁 {d}</div><table><thead><tr><th>物资</th><th style="text-align:right;">近30天领用</th></tr></thead><tbody>{rows}</tbody></table></div>'''
        if not usage_dept_html:
            usage_dept_html = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无部门领用记录</div>'
        
        html = f'''
        <!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"><title>环球梦大厦 · {view_label}</title><style>{BASE_STYLE}</style></head><body>
            {render_header()}{view_tabs}{quick_links}{search_bar}{search_html}
            <div class="section-title">📦 各部门物资库存清单<span>{len(positive_items)} 项</span></div>{dept_html}
            <div class="section-title" style="margin-top:30px;">📊 各部门近30天领用统计</div>{usage_dept_html}
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body></html>'''
    return html

# ================= 🛠️ 管理后台 =================

@app.route('/admin')
def admin_page():
    keyword = request.args.get('q', '').strip()
    dept_filter = request.args.get('dept', '').strip()
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            sql = '''
                SELECT i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department,
                       COALESCE(SUM(CASE WHEN t.type = 'in' THEN t.quantity ELSE 0 END), 0) AS total_in,
                       COALESCE(SUM(CASE WHEN t.type = 'out' THEN t.quantity ELSE 0 END), 0) AS total_out
                FROM items i
                LEFT JOIN transactions t ON i.id = t.item_id
                WHERE 1=1
            '''
            params = []
            if keyword:
                sql += " AND i.name LIKE %s"
                params.append(f"%{keyword}%")
            if dept_filter:
                sql += " AND i.department = %s"
                params.append(dept_filter)
            sql += " GROUP BY i.id, i.name, i.unit, i.initial_stock, i.low_stock_threshold, i.department ORDER BY i.department, i.name"
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    finally:
        conn.close()
    
    rows_html = ''
    for r in rows:
        real_stock = r['initial_stock'] + r['total_in'] - r['total_out']
        current_dept = r['department'] or ''
        dept_options = ''.join([f'<option value="{d}" {"selected" if d == current_dept else ""}>{d}</option>' for d in ['综合部','工程部','秩序部','客服部','未分配']])
        stock_color = '#e74c3c' if real_stock <= r['low_stock_threshold'] else '#333'
        rows_html += f'''
        <tr data-id="{r['id']}">
            <td style="font-size:11px;color:#999;font-family:monospace;">{r['id']}</td>
            <td><input class="name" type="text" value="{r['name']}" style="width:130px;padding:4px;"></td>
            <td><input class="unit" type="text" value="{r['unit']}" style="width:40px;padding:4px;"></td>
            <td><select class="department" style="padding:4px;">{dept_options}</select></td>
            <td><input class="initial_stock" type="number" value="{r['initial_stock']}" style="width:70px;padding:4px;"></td>
            <td><input class="low_stock_threshold" type="number" value="{r['low_stock_threshold']}" style="width:60px;padding:4px;"></td>
            <td style="font-weight:600;color:{stock_color};">{real_stock}</td>
            <td style="white-space:nowrap;">
                <button onclick="saveItem(this, '{r['id']}')" style="padding:4px 8px;background:#2ecc71;color:#fff;border:none;border-radius:4px;cursor:pointer;margin-right:2px;">保存</button>
                <button onclick="adjustStock('{r['id']}', 'in')" style="padding:4px 8px;background:#3498db;color:#fff;border:none;border-radius:4px;cursor:pointer;margin-right:2px;">入库</button>
                <button onclick="adjustStock('{r['id']}', 'out')" style="padding:4px 8px;background:#f39c12;color:#fff;border:none;border-radius:4px;cursor:pointer;margin-right:2px;">出库</button>
                <button onclick="deleteItem('{r['id']}')" style="padding:4px 8px;background:#e74c3c;color:#fff;border:none;border-radius:4px;cursor:pointer;">删除</button>
            </td>
        </tr>
        '''
    
    html = f'''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>物资管理后台</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }}
            h1 {{ font-size: 20px; }}
            .toolbar {{ display: flex; gap: 10px; margin-bottom: 15px; align-items: center; flex-wrap: wrap; }}
            .toolbar input, .toolbar select {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; }}
            .toolbar button {{ padding: 8px 16px; background: #333; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }}
            .add-box {{ background: white; padding: 16px; border-radius: 12px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
            .add-box input, .add-box select {{ padding: 8px; border: 1px solid #ddd; border-radius: 6px; margin-right: 8px; }}
            .add-box button {{ padding: 8px 16px; background: #333; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }}
            table {{ width: 100%; background: white; border-radius: 12px; border-collapse: collapse; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
            th {{ background: #f8f9fa; padding: 12px 8px; text-align: left; font-size: 13px; color: #666; border-bottom: 2px solid #eee; }}
            td {{ padding: 8px; border-bottom: 1px solid #eee; font-size: 13px; }}
            tr:hover {{ background: #fafafa; }}
        </style>
        <script>
            function saveItem(btn, id) {{
                const row = btn.closest('tr');
                const data = {{
                    id: id,
                    name: row.querySelector('.name').value,
                    unit: row.querySelector('.unit').value,
                    department: row.querySelector('.department').value,
                    initial_stock: parseInt(row.querySelector('.initial_stock').value || '0'),
                    low_stock_threshold: parseInt(row.querySelector('.low_stock_threshold').value || '5')
                }};
                fetch('/api/admin/update', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(data)
                }}).then(r => r.json()).then(res => {{
                    alert(res.message);
                    if (res.success) location.reload();
                }});
            }}
            
            function deleteItem(id) {{
                if (!confirm('确认删除该物资？该操作会同时删除所有出入库记录，不可恢复！')) return;
                fetch('/api/admin/delete', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{id: id}})
                }}).then(r => r.json()).then(res => {{
                    alert(res.message);
                    if (res.success) location.reload();
                }});
            }}
            
            function adjustStock(id, type) {{
                const label = type === 'in' ? '入库' : '出库';
                const qty = prompt('请输入' + label + '数量：');
                if (!qty || isNaN(parseInt(qty))) return;
                fetch('/api/admin/adjust_stock', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{id: id, type: type, quantity: parseInt(qty)}})
                }}).then(r => r.json()).then(res => {{
                    alert(res.message);
                    if (res.success) location.reload();
                }});
            }}
            
            function addItem() {{
                const data = {{
                    name: document.getElementById('new_name').value.trim(),
                    unit: document.getElementById('new_unit').value.trim() || '个',
                    department: document.getElementById('new_department').value,
                    initial_stock: parseInt(document.getElementById('new_initial_stock').value || '0'),
                    low_stock_threshold: parseInt(document.getElementById('new_low_stock_threshold').value || '5')
                }};
                if (!data.name) {{ alert('请填写物资名称'); return; }}
                fetch('/api/admin/add', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(data)
                }}).then(r => r.json()).then(res => {{
                    alert(res.message);
                    if (res.success) location.reload();
                }});
            }}
        </script>
    </head>
    <body>
        <h1>🛠️ 环球梦大厦 · 物资管理后台</h1>
        
        <div class="add-box">
            <div style="font-weight:700; margin-bottom:10px;">➕ 新增物资</div>
            <input id="new_name" type="text" placeholder="物资名称" style="width:180px;">
            <input id="new_unit" type="text" placeholder="单位（默认个）" style="width:100px;">
            <select id="new_department">
                <option value="综合部">综合部</option>
                <option value="工程部">工程部</option>
                <option value="秩序部">秩序部</option>
                <option value="客服部">客服部</option>
            </select>
            <input id="new_initial_stock" type="number" placeholder="初始库存" style="width:90px;">
            <input id="new_low_stock_threshold" type="number" placeholder="预警值" style="width:80px;">
            <button onclick="addItem()">新增</button>
        </div>
        
        <div class="toolbar">
            <form method="get" action="/admin" style="display:flex; gap:8px; align-items:center;">
                <input type="text" name="q" value="{keyword}" placeholder="🔍 搜索物资名称...">
                <select name="dept">
                    <option value="">全部部门</option>
                    <option value="综合部" {"selected" if dept_filter == "综合部" else ""}>综合部</option>
                    <option value="工程部" {"selected" if dept_filter == "工程部" else ""}>工程部</option>
                    <option value="秩序部" {"selected" if dept_filter == "秩序部" else ""}>秩序部</option>
                    <option value="客服部" {"selected" if dept_filter == "客服部" else ""}>客服部</option>
                </select>
                <button type="submit">搜索</button>
                <a href="/admin" style="padding:8px 16px; background:#eee; color:#333; border-radius:6px; text-decoration:none;">重置</a>
            </form>
            <span style="color:#888; font-size:13px;">共 {len(rows)} 项</span>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>名称</th>
                    <th>单位</th>
                    <th>归属部门</th>
                    <th>初始库存</th>
                    <th>预警值</th>
                    <th>当前库存</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                {rows_html if rows_html else '<tr><td colspan="8" style="text-align:center;padding:40px;color:#888;">暂无物资</td></tr>'}
            </tbody>
        </table>
    </body>
    </html>
    '''
    return html

# ================= 管理后台 API =================

@app.route('/api/admin/update', methods=['POST'])
def admin_update():
    data = request.json or {}
    item_id = data.get('id')
    if not item_id:
        return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''
                UPDATE items SET name=%s, unit=%s, department=%s, initial_stock=%s, low_stock_threshold=%s
                WHERE id=%s
            ''', (
                data.get('name', ''),
                data.get('unit', '个'),
                normalize_department(data.get('department', '')),
                int(data.get('initial_stock', 0)),
                int(data.get('low_stock_threshold', 5)),
                item_id
            ))
        conn.commit()
        return jsonify({'success': True, 'message': f'已更新物资 {data.get("name")}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/delete', methods=['POST'])
def admin_delete():
    data = request.json or {}
    item_id = data.get('id')
    if not item_id:
        return jsonify({'success': False, 'message': '缺少ID'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('DELETE FROM transactions WHERE item_id = %s', (item_id,))
            cursor.execute('DELETE FROM items WHERE id = %s', (item_id,))
        conn.commit()
        return jsonify({'success': True, 'message': '已删除'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/add', methods=['POST'])
def admin_add():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '物资名称不能为空'})
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            base_id = generate_item_id(name)
            item_id = base_id
            suffix = 1
            while True:
                cursor.execute('SELECT id FROM items WHERE id = %s', (item_id,))
                if not cursor.fetchone():
                    break
                item_id = f"{base_id[:6]}{suffix}"
                suffix += 1
            
            cursor.execute('''
                INSERT INTO items (id, name, unit, department, initial_stock, low_stock_threshold)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (
                item_id, name,
                data.get('unit', '个'),
                normalize_department(data.get('department', '综合部')),
                int(data.get('initial_stock', 0)),
                int(data.get('low_stock_threshold', 5))
            ))
        conn.commit()
        return jsonify({'success': True, 'message': f'已新增 {name}（ID: {item_id}）'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/admin/adjust_stock', methods=['POST'])
def admin_adjust_stock():
    data = request.json or {}
    item_id = data.get('id')
    trans_type = data.get('type')
    quantity = data.get('quantity')
    if not item_id or trans_type not in ['in', 'out'] or not quantity:
        return jsonify({'success': False, 'message': '参数错误'})
    try:
        quantity = int(quantity)
        if quantity <= 0:
            return jsonify({'success': False, 'message': '数量必须大于0'})
    except:
        return jsonify({'success': False, 'message': '数量格式错误'})
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('''
                INSERT INTO transactions (item_id, type, quantity, department, note)
                VALUES (%s, %s, %s, %s, %s)
            ''', (item_id, trans_type, quantity, None, '后台手动调整'))
        conn.commit()
        label = '入库' if trans_type == 'in' else '出库'
        return jsonify({'success': True, 'message': f'已{label} {quantity}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

# ================= 通用 API =================

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
    all_items_data = get_all_items_with_stock()
    all_low = [d for d in all_items_data if d['is_low']]
    if not all_low:
        return "✅ 所有部门物资库存充足，无需推送"
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