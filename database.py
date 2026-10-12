import os
import pymysql
import hashlib

MYSQL_ADDRESS = os.environ.get("MYSQL_ADDRESS")
MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "inventory_db")

def get_db():
    """获取 MySQL 连接（不指定数据库，用于建库）"""
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0],
        port=int(MYSQL_ADDRESS.split(":")[1]) if MYSQL_ADDRESS and ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor
    )
    return conn

def init_db():
    """初始化：先建库，再建表"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 1. 创建数据库（如果不存在）
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DATABASE} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            # 2. 切换到该数据库
            cursor.execute(f"USE {MYSQL_DATABASE}")
            
            # 3. 创建物资表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS items (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    unit VARCHAR(50) DEFAULT '个',
                    initial_stock INT DEFAULT 0,
                    low_stock_threshold INT DEFAULT 5
                )
            ''')
            # 4. 创建流水表
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
        conn.commit()
    finally:
        conn.close()

def generate_item_id(name):
    """根据物资名称生成唯一ID"""
    return hashlib.md5(name.encode()).hexdigest()[:8]