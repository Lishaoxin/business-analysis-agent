import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "business.db"

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")

conn.execute("""
INSERT INTO orders (order_id, order_date, region, status)
VALUES (?, ?, ?, ?)
ON CONFLICT(order_id) DO NOTHING
""", ("O001", "2026-09-01", "华东", "paid"))

conn.execute("""
INSERT INTO orders (order_id, order_date, region, status)
VALUES (?, ?, ?, ?)
ON CONFLICT(order_id) DO NOTHING
""", ("O002", "2026-09-02", "华南", "cancelled"))

conn.executemany("""
INSERT INTO orders (order_id, order_date, region, status)
VALUES (?, ?, ?, ?)
ON CONFLICT(order_id) DO NOTHING
""", [
    ("O003", "2026-09-05", "华南", "paid"),
    ("O004", "2026-10-01", "华东", "paid"),
    ("O005", "2026-10-03", "华南", "paid"),
    ("O006", "2026-10-08", "华东", "cancelled"),
])

conn.execute("""
INSERT INTO products (product_id, product_name, category)
VALUES (?, ?, ?)
ON CONFLICT(product_id) DO NOTHING
""", ("P001", "键盘", "电脑配件"))

conn.execute("""
INSERT INTO products (product_id, product_name, category)
VALUES (?, ?, ?)
ON CONFLICT(product_id) DO NOTHING
""", ("P002", "鼠标", "电脑配件"))

conn.execute("""
INSERT INTO products (product_id, product_name, category)
VALUES (?, ?, ?)
ON CONFLICT(product_id) DO NOTHING
""", ("P003", "显示器", "显示设备"))

conn.execute("""
INSERT INTO order_items (
    item_id, order_id, product_id, quantity, unit_price_cents
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(item_id) DO NOTHING
""", (1, "O001", "P001", 3, 10000))

conn.execute("""
INSERT INTO order_items (
    item_id, order_id, product_id, quantity, unit_price_cents
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(item_id) DO NOTHING
""", (2, "O001", "P002", 5, 15000))

conn.execute("""
INSERT INTO order_items (
    item_id, order_id, product_id, quantity, unit_price_cents
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(item_id) DO NOTHING
""", (3, "O002", "P001", 3, 10000))

conn.executemany("""
INSERT INTO order_items (
    item_id, order_id, product_id, quantity, unit_price_cents
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(item_id) DO NOTHING
""", [
    (4, "O003", "P001", 2, 10000),
    (5, "O003", "P002", 1, 15000),
    (6, "O004", "P003", 1, 130000),
    (7, "O004", "P001", 1, 10000),
    (8, "O005", "P002", 2, 15000),
    (9, "O006", "P003", 1, 130000),
])

conn.commit()

order_rows = conn.execute(
    "SELECT order_id, order_date, region, status FROM orders ORDER BY order_id"
).fetchall()

print("订单：")
for row in order_rows:
    print(row)

product_rows = conn.execute(
    "SELECT product_id, product_name, category FROM products ORDER BY product_id"
).fetchall()

print("商品：")
for row in product_rows:
    print(row)

item_rows = conn.execute("""
SELECT item_id, order_id, product_id, quantity, unit_price_cents
FROM order_items
ORDER BY item_id
""").fetchall()

print("明细：")
for row in item_rows:
    print(row)

conn.close()