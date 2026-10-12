import os
import pymysql
import hashlib

MYSQL_ADDRESS = os.environ.get("MYSQL_ADDRESS")
MYSQL_USERNAME = os.environ.get("MYSQL_USERNAME")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "inventory_db")

def get_db():
    conn = pymysql.connect(
        host=MYSQL_ADDRESS.split(":")[0],
        port=int(MYSQL_ADDRESS.split(":")[1]) if ":" in MYSQL_ADDRESS else 3306,
        user=MYSQL_USERNAME,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor
    )
    return conn

def init_db():
    conn = get_db()
    with conn.cursor() as cursor:
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
    conn.commit()
    conn.close()

def generate_item_id(name):
    return hashlib.md5(name.encode()).hexdigest()[:8]