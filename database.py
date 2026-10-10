import sqlite3
import os

DB_PATH = 'inventory.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库，创建物资表和流水表"""
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            unit TEXT DEFAULT '个',
            initial_stock INTEGER DEFAULT 0,
            low_stock_threshold INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('in', 'out')),
            quantity INTEGER NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
    ''')
    conn.commit()
    conn.close()

def generate_item_id(name):
    """根据物资名称生成唯一ID，用作URL中的参数"""
    import hashlib
    return hashlib.md5(name.encode()).hexdigest()[:8]

if __name__ == '__main__':
    init_db()
    print("数据库初始化完成！")