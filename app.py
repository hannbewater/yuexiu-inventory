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
DEPT_ALIAS = {
    '安管部': '秩序部',
}
def normalize_department(dept):
    if not dept:
        return dept
    dept = str(dept).strip()
    return DEPT_ALIAS.get(dept, dept)

# ================= 钉钉机器人 =================
DEPT_WEBHOOKS = {
    '综合部': 'https://oapi.dingtalk.com/robot/send?access_token=2b99041269aa1070355657d9060ae29ef6c064cfeb3b2565f0c2b0ac6877d6c1',
    '工程部': '',
    '秩序部': '',
    '客服部': '',
}

def get_db():
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0] if MYSQL_ADDRESS else "127.0.0.1",
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
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
def calc_stock(cursor, item):
    cursor.execute('''
        SELECT 
            COALESCE(SUM(CASE WHEN type = 'in' THEN quantity ELSE 0 END), 0) AS total_in,
            COALESCE(SUM(CASE WHEN type = 'out' THEN quantity ELSE 0 END), 0) AS total_out
        FROM transactions WHERE item_id = %s
    ''', (item['id'],))
    stats = cursor.fetchone()
    real_stock = item['initial_stock'] + stats['total_in'] - stats['total_out']
    return {
        'id': item['id'],
        'name': re.sub(r'\(\d+\)$', '', item['name']),
        'unit': item['unit'],
        'real_stock': real_stock,
        'total_in': stats['total_in'],
        'total_out': stats['total_out'],
        'threshold': item['low_stock_threshold'],
        'department': normalize_department(item.get('department')) or '未分配',
        'is_low': real_stock <= item['low_stock_threshold']
    }

def send_dept_alert(department, message):
    dept = normalize_department(department)
    webhook = DEPT_WEBHOOKS.get(dept)
    if not webhook:
        logging.warning(f"未配置部门 [{dept}] 的钉钉 Webhook，跳过推送")
        return False
    data = {"msgtype": "text", "text": {"content": f"【库存预警】{message}"}}
    headers = {'Content-Type': 'application/json;charset=utf-8'}
    try:
        resp = requests.post(webhook, json=data, headers=headers, timeout=5)
        if resp.status_code == 200 and resp.json().get('errcode') == 0:
            logging.info(f"成功向 [{dept}] 钉钉群推送预警")
            return True
        else:
            logging.error(f"钉钉推送失败: {resp.text}")
            return False
    except Exception as e:
        logging.error(f"钉钉推送异常: {e}")
        return False

def render_header():
    """🌏 突出显示“环球梦大厦”"""
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
    /* 🌏 顶部主标题（突出环球梦大厦） */
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

    /* 🔄 常用消耗物资 / 全物资 切换 Tab */
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
    """渲染“常用消耗物资 / 全物资”切换 Tab，并保留 dept 和 q 参数"""
    def build_url(view):
        params = [f"view={view}"]
        if dept:
            params.append(f"dept={dept}")
        if query:
            params.append(f"q={query}")
        return "/dashboard?" + "&".join(params)
    
    common_active = 'active' if current_view == 'common' else ''
    all_active = 'active' if current_view == 'all' else ''
    
    return f'''
    <div class="view-tabs">
        <a href="{build_url('common')}" class="{common_active}">常用消耗物资看板<span class="hint">重点关注 · 待配置</span></a>
        <a href="{build_url('all')}" class="{all_active}">全物资看板<span class="hint">全部 {""}</span></a>
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
            cursor.execute('SELECT * FROM items WHERE id = %s', (item_id,))
            item = cursor.fetchone()
            if not item:
                return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404
            data = calc_stock(cursor, item)
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
    # 🌟 视图类型：common（常用消耗物资）/ all（全物资）
    # 目前两种视图都展示全部物资（常用消耗物资尚未统计）
    view = request.args.get('view', 'all').strip()
    if view not in ['common', 'all']:
        view = 'all'
    
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items')
            items = cursor.fetchall()
            all_items_data = [calc_stock(cursor, item) for item in items]
            all_low = [d for d in all_items_data if d['is_low']]
            all_low.sort(key=lambda x: x['real_stock'])
            
            search_results = None
            if query:
                search_results = [d for d in all_items_data if query.lower() in d['name'].lower()]
                search_results.sort(key=lambda x: x['name'])
            
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
            search_html = f'''
            <div class="section-title">🔍 搜索结果："{query}"<span>{len(search_results)} 项</span></div>
            <div class="scroll-card">{rows}</div>
            '''
        else:
            search_html = f'''
            <div class="section-title">🔍 搜索结果："{query}"<span>0 项</span></div>
            <div class="card" style="text-align:center;color:#888;padding:20px;">没有找到匹配的物资</div>
            '''

    view_label = "常用消耗物资看板" if view == 'common' else "全物资看板"

    if dept:
        usage_rows = ''.join([f'<tr><td>{u["name"]}</td><td class="num">{u["total_qty"]} {u["unit"]}</td></tr>' for u in usages])
        if not usage_rows:
            usage_rows = '<tr><td colspan="2" style="text-align:center;color:#888;padding:15px;">暂无领用记录</td></tr>'
        html = f'''
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
            <title>{dept} · {view_label}</title>
            <style>{BASE_STYLE}</style>
        </head>
        <body>
            {render_header()}
            {view_tabs}
            {quick_links}
            {search_bar}
            {search_html}
            <div class="section-title">🚨 本部门物资预警<span>{len(my_low)} 项</span></div>
            <div class="scroll-card">{render_low_stock_html(my_low)}</div>
            <div class="section-title" style="margin-top:24px;">📦 全部物资预警（含其他部门）<span>{len(all_low)} 项</span></div>
            <div class="scroll-card">{render_low_stock_html(all_low)}</div>
            <div class="section-title" style="margin-top:24px;">📊 {dept}近30天领用记录</div>
            <div class="scroll-card">
                <table><thead><tr><th>物资</th><th style="text-align:right;">领用量</th></tr></thead>
                <tbody>{usage_rows}</tbody></table>
            </div>
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body>
        </html>
        '''
    else:
        dept_html = ''
        if dept_dict:
            for d, usages in dept_dict.items():
                rows = ''.join([f'<tr><td>{u["name"]}</td><td class="num">{u["qty"]} {u["unit"]}</td></tr>' for u in usages])
                dept_html += f'''
                <div class="scroll-card" style="margin-bottom: 12px;">
                    <div style="font-weight:700; font-size:15px; margin-bottom:10px;">📁 {d}</div>
                    <table><thead><tr><th>物资</th><th style="text-align:right;">近30天领用</th></tr></thead>
                    <tbody>{rows}</tbody></table>
                </div>
                '''
        if not dept_html:
            dept_html = '<div class="card" style="text-align:center;color:#888;padding:20px;">暂无部门领用记录</div>'
        html = f'''
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
            <title>环球梦大厦 · {view_label}</title>
            <style>{BASE_STYLE}</style>
        </head>
        <body>
            {render_header()}
            {view_tabs}
            {quick_links}
            {search_bar}
            {search_html}
            <div class="section-title">🚨 全部库存预警<span>{len(all_low)} 项</span></div>
            <div class="scroll-card">{render_low_stock_html(all_low)}</div>
            <div class="section-title" style="margin-top:30px;">📊 各部门近30天领用统计</div>
            {dept_html}
            <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
        </body>
        </html>
        '''
    return html

# ================= API 接口 =================

@app.route('/api/item/<item_id>')
def api_item(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items WHERE id = %s', (item_id,))
            item = cursor.fetchone()
            if not item:
                return jsonify({'error': '物资不存在'}), 404
            return jsonify(calc_stock(cursor, item))
    finally:
        conn.close()

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
            trans_type = 'out'
            quantity = fields.get('出库数量')
        elif '入库' in form_name:
            trans_type = 'in'
            quantity = fields.get('入库数量')
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
                    logging.warning(f"数据库中不存在物资 [{item_name}]，尝试自动创建...")
                    item_id = generate_item_id(item_name)
                    unit = fields.get('单选项', '个')
                    cursor.execute('''
                        INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold) 
                        VALUES (%s, %s, %s, 0, 5)
                    ''', (item_id, item_name, unit))
                else:
                    item_id = item['id']
                cursor.execute('''
                    INSERT INTO transactions (item_id, type, quantity, department, note) 
                    VALUES (%s, %s, %s, %s, %s)
                ''', (item_id, trans_type, int(quantity), department, f'草料同步-{form_name}'))
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
                    name = f"{original_name}({suffix})"
                    suffix += 1
                imported_names.add(name)
                dept = str(row['申采部门']).replace('<br>', '').replace('\n', '').strip() if pd.notna(row['申采部门']) else '综合部'
                if dept == 'nan' or not dept:
                    dept = '综合部'
                dept = normalize_department(dept)
                unit = str(row['计量单位']).replace("\n", "").strip() if pd.notna(row['计量单位']) else '个'
                try:
                    initial_stock = int(float(row['数量'])) if pd.notna(row['数量']) else 0
                except:
                    initial_stock = 0
                item_id = generate_item_id(name)
                cursor.execute('''
                    INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold, department)
                    VALUES (%s, %s, %s, %s, 5, %s)
                ''', (item_id, name, unit, initial_stock, dept))
                count += 1
        conn.commit()
        return f"🎉 成功导入 {count} 种物资！已根据“申采部门”自动分配归属部门（安管部已统一为秩序部）。"
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
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT DISTINCT department FROM items WHERE department IS NOT NULL')
            depts = cursor.fetchall()
            results = []
            for dept_row in depts:
                dept = normalize_department(dept_row['department'])
                cursor.execute('SELECT * FROM items WHERE department = %s', (dept,))
                items = cursor.fetchall()
                low_items = []
                for item in items:
                    data = calc_stock(cursor, item)
                    if data['is_low']:
                        low_items.append(f"• {data['name']}：剩余 {data['real_stock']} {data['unit']}（阈值 {data['threshold']}）")
                if low_items:
                    msg = f"⚠️ {dept}物资预警\n以下物资库存不足，请及时采购：\n" + "\n".join(low_items)
                    send_dept_alert(dept, msg)
                    results.append(f"{dept}: {len(low_items)}项已推送")
                else:
                    results.append(f"{dept}: 库存充足")
        return " | ".join(results)
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)