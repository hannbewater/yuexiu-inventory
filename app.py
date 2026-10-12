from flask import Flask, request, jsonify
import logging
import pymysql
import os
import hashlib

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
                    note VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            try:
                cursor.execute('CREATE INDEX idx_item_id ON transactions(item_id)')
                cursor.execute('CREATE INDEX idx_name ON items(name)')
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
            return {
                'id': item['id'],
                'name': item['name'],
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
                    INSERT INTO transactions (item_id, type, quantity, note) 
                    VALUES (%s, %s, %s, %s)
                ''', (item_id, trans_type, int(quantity), f'草料同步-{form_name}'))
            conn.commit()
        finally:
            conn.close()
        return jsonify({'status': 'success', 'message': f'{item_name} 数据已成功同步'}), 200
    except Exception as e:
        logging.error(f"处理草料数据失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/init_data')
def init_data():
    """一键导入，并自动处理重名物资"""
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
                
                # 🌟 核心修复：处理重名物资，自动添加 (1)、(2) 后缀
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

# ================= 启动 =================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)