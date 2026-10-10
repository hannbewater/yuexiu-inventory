import pandas as pd
from database import get_db, init_db, generate_item_id

def import_from_excel():
    # 1. 初始化数据库（如果删除了 inventory.db，这一步会自动重建）
    init_db()
    conn = get_db()

    # 2. 读取 Excel 文件
    print("正在读取入库记录...")
    df_in = pd.read_excel('in.xlsx')
    print("正在读取出库记录...")
    df_out = pd.read_excel('out.xlsx')

    # 3. 提取所有物资的名称和单位
    items_dict = {}
    
    # 遍历入库表 (E列是二维码名称，H列是物资单位)
    for index, row in df_in.iterrows():
        name = str(row['二维码名称']).strip()
        unit = str(row['物资单位']).strip() if pd.notna(row['物资单位']) else '个'
        if name and name != 'nan':
            items_dict[name] = unit

    # 遍历出库表 (E列是二维码名称，H列是单选项)
    for index, row in df_out.iterrows():
        name = str(row['二维码名称']).strip()
        unit = str(row['单选项']).strip() if pd.notna(row['单选项']) else '个'
        if name and name != 'nan':
            items_dict[name] = unit

    print(f"共发现 {len(items_dict)} 种物资，正在写入档案表...")

    # 4. 将物资写入 items 表，初始库存统一设为 0
    item_id_map = {}
    for name, unit in items_dict.items():
        item_id = generate_item_id(name)
        item_id_map[name] = item_id
        conn.execute('''
            INSERT OR REPLACE INTO items (id, name, unit, initial_stock, low_stock_threshold)
            VALUES (?, ?, ?, 0, 5)
        ''', (item_id, name, unit))

    # 5. 将历史入库记录写入流水表
    print("正在写入历史入库记录...")
    for index, row in df_in.iterrows():
        name = str(row['二维码名称']).strip()
        qty = row['入库数量']
        if name in item_id_map and pd.notna(qty):
            conn.execute(
                'INSERT INTO transactions (item_id, type, quantity, note) VALUES (?, ?, ?, ?)',
                (item_id_map[name], 'in', int(qty), '历史数据导入')
            )

    # 6. 将历史出库记录写入流水表
    print("正在写入历史出库记录...")
    for index, row in df_out.iterrows():
        name = str(row['二维码名称']).strip()
        qty = row['出库数量']
        if name in item_id_map and pd.notna(qty):
            conn.execute(
                'INSERT INTO transactions (item_id, type, quantity, note) VALUES (?, ?, ?, ?)',
                (item_id_map[name], 'out', int(qty), '历史数据导入')
            )

    conn.commit()
    conn.close()
    print("🎉 历史数据导入完成！所有库存已自动计算。")

if __name__ == '__main__':
    import_from_excel()