from flask import Flask, request, jsonify
import logging
import pymysql
import os
import hashlib
import re
from datetime import datetime, timedelta

# ================= 配置日志 =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# ================= 数据库配置 =================
MYSQL_ADDRESS = os.environ.get("MYSQL_ADDRESS")
MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "inventory_db")

def get_db():
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0] if MYSQL_ADDRESS else "127.0.0.1",
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor
    )
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
                    low_stock_threshold INT DEFAULT 5
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
            # 自动升级：如果 transactions 表里没有 department 字段，自动加上
            try:
                cursor.execute("ALTER TABLE transactions ADD COLUMN department VARCHAR(50) DEFAULT NULL")
                logging.info("已自动添加 department 字段")
            except Exception:
                pass # 如果已经存在，忽略错误

            try:
                cursor.execute('CREATE INDEX idx_item_id ON transactions(item_id)')
                cursor.execute('CREATE INDEX idx_name ON items(name)')
                cursor.execute('CREATE INDEX idx_department ON transactions(department)')
            except Exception:
                pass
        conn.commit()
        logging.info("数据库初始化完成")
    except Exception as e:
        logging.error(f"数据库初始化失败: {e}")
    finally:
        conn.close()

def generate_item_id(name):
    return hashlib.md5(name.encode()).hexdigest()[:8]

# 启动时自动初始化
init_db()

# ================= 工具函数 =================
def get_item_with_stock(item_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            cursor.execute('SELECT * FROM items WHERE id = %s', (item_id,))
            item = cursor.fetchone()
            if not item:
                return None

            cursor.execute('''
                SELECT 
                    COALESCE(SUM(CASE WHEN type = 'in' THEN quantity ELSE 0 END), 0) AS total_in,
                    COALESCE(SUM(CASE WHEN type = 'out' THEN quantity ELSE 0 END), 0) AS total_out
                FROM transactions
                WHERE item_id = %s
            ''', (item_id,))
            stats = cursor.fetchone()

            real_stock = item['initial_stock'] + stats['total_in'] - stats['total_out']
            display_name = re.sub(r'\(\d+\)$', '', item['name'])
            
            return {
                'id': item['id'],
                'name': display_name,
                'unit': item['unit'],
                'real_stock': real_stock,
                'total_in': stats['total_in'],
                'total_out': stats['total_out'],
                'low_stock_threshold': item['low_stock_threshold']
            }
    finally:
        conn.close()

# ================= 页面路由 =================
@app.route('/')
def home():
    return "🎉 环球梦大厦库存看板系统运行中！请扫码访问。"

@app.route('/item/<item_id>')
def item_page(item_id):
    item = get_item_with_stock(item_id)
    if not item:
        return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404

    is_low = item['real_stock'] <= item['low_stock_threshold']
    stock_color = '#e74c3c' if is_low else '#2ecc71'

    html = f'''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>{item['name']} - 库存看板</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 15px; }}
            .header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; background: white; padding: 12px 16px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
            .logo {{ height: 32px; object-fit: contain; }}
            .project-title {{ font-size: 14px; font-weight: 600; color: #333; white-space: nowrap; flex: 1; text-align: right; margin-left: 10px; }}
            .card {{ background: white; border-radius: 16px; padding: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); margin-bottom: 16px; text-align: center; }}
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
            .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <img src="/static/logo.png" class="logo" alt="越秀服务" onerror="this.style.display='none'">
            <div class="project-title">环球梦大厦 · 物资库存看板</div>
        </div>
        <div class="card">
            <div class="item-name">{item['name']}</div>
            <div class="label">实时库存</div>
            <div class="stock-number">{item['real_stock']}<span class="unit"> {item['unit']}</span></div>
            {f'<div class="warning">⚠️ 库存不足，请及时采购！</div>' if is_low else ''}
        </div>
        <div class="stats-grid">
            <div class="stat-box stat-in">
                <div class="label">累计入库</div>
                <div class="stat-value">+{item['total_in']}</div>
            </div>
            <div class="stat-box stat-out">
                <div class="label">累计出库</div>
                <div class="stat-value">-{item['total_out']}</div>
            </div>
        </div>
        <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
    </body>
    </html>
    '''
    return html

@app.route('/dashboard')
def dashboard():
    """全局数据面板：常用物资预警 + 部门领用统计"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {MYSQL_DATABASE}")
            
            # 1. 获取所有物资及其实时库存
            cursor.execute('SELECT * FROM items')
            items = cursor.fetchall()
            
            # 2. 计算实时库存并筛选出预警物资（低于阈值）
            low_stock_items = []
            all_items_with_stock = []
            for item in items:
                cursor.execute('''
                    SELECT 
                        COALESCE(SUM(CASE WHEN type = 'in' THEN quantity ELSE 0 END), 0) AS total_in,
                        COALESCE(SUM(CASE WHEN type = 'out' THEN quantity ELSE 0 END), 0) AS total_out
                    FROM transactions WHERE item_id = %s
                ''', (item['id'],))
                stats = cursor.fetchone()
                real_stock = item['initial_stock'] + stats['total_in'] - stats['total_out']
                display_name = re.sub(r'\(\d+\)$', '', item['name'])
                
                item_data = {
                    'id': item['id'],
                    'name': display_name,
                    'unit': item['unit'],
                    'real_stock': real_stock,
                    'threshold': item['low_stock_threshold']
                }
                all_items_with_stock.append(item_data)
                
                if real_stock <= item['low_stock_threshold']:
                    low_stock_items.append(item_data)
            
            # 按实时库存升序排列，最紧急的排在最前面
            low_stock_items.sort(key=lambda x: x['real_stock'])
            
            # 3. 统计各部门最近30天的领用情况
            thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                SELECT t.department, i.name, i.unit, SUM(t.quantity) as total_out_qty
                FROM transactions t
                JOIN items i ON t.item_id = i.id
                WHERE t.type = 'out' AND t.department IS NOT NULL AND t.created_at >= %s
                GROUP BY t.department, i.name, i.unit
                ORDER BY t.department, total_out_qty DESC
            ''', (thirty_days_ago,))
            dept_usage = cursor.fetchall()
            
            # 整理部门数据
            dept_dict = {}
            for row in dept_usage:
                dept = row['department']
                if dept not in dept_dict:
                    dept_dict[dept] = []
                dept_dict[dept].append({
                    'name': re.sub(r'\(\d+\)$', '', row['name']),
                    'unit': row['unit'],
                    'qty': row['total_out_qty']
                })

    finally:
        conn.close()

    # 构造看板HTML
    low_stock_html = "".join([f'''
        <div style="background: #fff5f5; border-left: 4px solid #e74c3c; padding: 12px; margin-bottom: 8px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span style="font-weight: 600; color: #333;">{item['name']}</span>
                <span style="color: #888; font-size: 12px; margin-left: 8px;">预警值: {item['threshold']}</span>
            </div>
            <div style="color: #e74c3c; font-weight: 700; font-size: 18px;">{item['real_stock']} {item['unit']}</div>
        </div>
    ''' for item in low_stock_items])

    dept_html = ""
    for dept, usages in dept_dict.items():
        usage_rows = "".join([f'''
            <tr>
                <td style="padding: 8px 0; border-bottom: 1px solid #eee;">{u['name']}</td>
                <td style="padding: 8px 0; border-bottom: 1px solid #eee; text-align: right; font-weight: 600; color: #e74c3c;">{u['qty']} {u['unit']}</td>
            </tr>
        ''' for u in usages])
        dept_html += f'''
        <div style="background: white; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
            <div style="font-weight: 700; font-size: 16px; margin-bottom: 12px; color: #333;">📁 {dept}</div>
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                <thead>
                    <tr style="color: #888; text-align: left;">
                        <th style="padding-bottom: 8px;">物资名称</th>
                        <th style="text-align: right; padding-bottom: 8px;">近30天领用</th>
                    </tr>
                </thead>
                <tbody>
                    {usage_rows}
                </tbody>
            </table>
        </div>
        '''

    if not dept_html:
        dept_html = "<div style='color: #888; text-align: center; padding: 20px;'>暂无部门领用记录（请确保草料表单中有“所属部门”字段且已填写）</div>"

    html = f'''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>环球梦大厦 · 物资预警看板</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f0f2f5; margin: 0; padding: 15px; }}
            .header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; background: white; padding: 12px 16px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
            .logo {{ height: 32px; object-fit: contain; }}
            .project-title {{ font-size: 14px; font-weight: 600; color: #333; white-space: nowrap; flex: 1; text-align: right; margin-left: 10px; }}
            .section-title {{ font-size: 18px; font-weight: 700; color: #333; margin: 20px 0 12px 0; display: flex; align-items: center; }}
            .section-title span {{ background: #e74c3c; color: white; font-size: 12px; padding: 2px 8px; border-radius: 10px; margin-left: 10px; font-weight: normal; }}
            .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <img src="/static/logo.png" class="logo" alt="越秀服务" onerror="this.style.display='none'">
            <div class="project-title">环球梦大厦 · 物资预警看板</div>
        </div>

        <!-- 库存预警区 -->
        <div class="section-title">🚨 库存预警 <span>{len(low_stock_items)} 项</span></div>
        <div style="background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
            {low_stock_html if low_stock_html else '<div style="text-align:center; color:#2ecc71; padding: 20px;">✅ 所有物资库存充足</div>'}
        </div>

        <!-- 部门领用统计区 -->
        <div class="section-title" style="margin-top: 30px;">📊 各部门近30天领用统计</div>
        <div>
            {dept_html}
        </div>

        <div class="footer">数据实时同步 · 环球梦大厦物业服务中心</div>
    </body>
    </html>
    '''
    return html

# ================= API 接口 =================
@app.route('/api/item/<item_id>')
def api_item(item_id):
    item = get_item_with_stock(item_id)
    if not item:
        return jsonify({'error': '物资不存在'}), 404
    return jsonify(item)

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

        # 提取领用部门（兼容不同的字段名）
        department = fields.get('所属部门') or fields.get('领用部门') or fields.get('部门')

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
                
                unit = str(row['计量单位']).replace("\n", "").strip() if pd.notna(row['计量单位']) else '个'
                try:
                    initial_stock = int(float(row['数量'])) if pd.notna(row['数量']) else 0
                except:
                    initial_stock = 0
                
                item_id = generate_item_id(name)
                cursor.execute('''
                    INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold)
                    VALUES (%s, %s, %s, %s, 5)
                ''', (item_id, name, unit, initial_stock))
                count += 1
        conn.commit()
        return f"🎉 成功导入 {count} 种物资！（重名物资已自动加序号区分）"
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

# ================= 启动应用 =================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)