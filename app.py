from flask import Flask, request, jsonify
import logging
import pymysql
from database import get_db, init_db, generate_item_id

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
init_db()

# ================= 工具函数 =================

def get_item_with_stock(item_id):
    """获取单个物资的实时库存、累计入库和累计出库 (MySQL 版本)"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 查询物资档案
            cursor.execute('SELECT * FROM items WHERE id = %s', (item_id,))
            item = cursor.fetchone()
            if not item:
                return None

            # 查询所有流水并计算
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

@app.route('/item/<item_id>')
def item_page(item_id):
    """扫码后展示的物资数据看板"""
    item = get_item_with_stock(item_id)
    if not item:
        return "<h2 style='text-align:center;margin-top:50px;'>物资不存在</h2>", 404

    # 判断是否低库存
    is_low = item['real_stock'] <= item['low_stock_threshold']
    stock_color = '#e74c3c' if is_low else '#2ecc71'

    # 数据看板 HTML 模板
    html = f'''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>{item['name']} - 库存看板</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 15px; }}
            /* 顶部 Logo 与标题的排版布局 */
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
        <!-- 顶部 Logo 与项目名称 -->
        <div class="header">
            <img src="/static/logo.png" class="logo" alt="越秀服务" onerror="this.style.display='none'">
            <div class="project-title">环球梦大厦 · 物资库存看板</div>
        </div>

        <!-- 核心库存展示 -->
        <div class="card">
            <div class="item-name">{item['name']}</div>
            <div class="label">实时库存</div>
            <div class="stock-number">{item['real_stock']}<span class="unit"> {item['unit']}</span></div>
            {f'<div class="warning">⚠️ 库存不足，请及时采购！</div>' if is_low else ''}
        </div>

        <!-- 累计出入库统计 -->
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

        <div class="footer">
            数据实时同步 · 环球梦大厦物业服务中心
        </div>
    </body>
    </html>
    '''
    return html

# ================= API 接口 =================

@app.route('/api/item/<item_id>')
def api_item(item_id):
    """返回单个物资库存数据的 JSON 接口"""
    item = get_item_with_stock(item_id)
    if not item:
        return jsonify({'error': '物资不存在'}), 404
    return jsonify(item)

@app.route('/api/webhook/grass', methods=['POST'])
def webhook_grass():
    """接收草料二维码推送的出入库数据 API 接口 (MySQL 版本)"""
    data = request.json
    logging.info(f"收到草料推送数据: {data}")
    
    if not data:
        return jsonify({'status': 'error', 'message': '无数据'}), 400

    try:
        # 1. 深入获取草料的嵌套数据
        ref_data = data.get('data', {}).get('ref_data', {})
        form_name = ref_data.get('form', {}).get('name', '')
        fields = ref_data.get('fields', {})
        
        # 2. 获取物资名称
        ref_qrcode = data.get('data', {}).get('ref_qrcode', {})
        item_name = ref_qrcode.get('fields', {}).get('资产名称') or ref_qrcode.get('name')
        
        if not item_name:
            return jsonify({'status': 'error', 'message': '未找到物资名称'}), 400

        # 3. 根据表单名称判断是入库还是出库
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
                # 4. 去数据库里找物资
                cursor.execute('SELECT id FROM items WHERE name = %s', (item_name,))
                item = cursor.fetchone()
                
                if not item:
                    # 自动创建物资
                    logging.warning(f"数据库中不存在物资 [{item_name}]，尝试自动创建...")
                    item_id = generate_item_id(item_name)
                    unit = fields.get('单选项', '个')
                    cursor.execute('''
                        INSERT INTO items (id, name, unit, initial_stock, low_stock_threshold) 
                        VALUES (%s, %s, %s, 0, 5)
                    ''', (item_id, item_name, unit))
                else:
                    item_id = item['id']

                # 5. 写入流水
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

# ================= 启动应用 =================

if __name__ == '__main__':
    # 微信云托管要求监听 80 端口
    app.run(host='0.0.0.0', port=80, debug=False)