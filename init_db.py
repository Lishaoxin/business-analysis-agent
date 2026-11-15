import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "business.db"

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")

conn.execute("""
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    order_date TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('paid', 'cancelled'))
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS order_items (
    item_id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
)
""")

rows = conn.execute("PRAGMA table_info(order_items)").fetchall()

print("order_items 表结构：")
for row in rows:
    print(row[1], row[2])

print("外键检查：", conn.execute("PRAGMA foreign_keys").fetchone()[0])

conn.close()