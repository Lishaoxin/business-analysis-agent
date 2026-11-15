import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "business.db"

order_status = "paid"

conn = sqlite3.connect(DB_PATH)

row = conn.execute("""
SELECT COALESCE(
    SUM(order_items.quantity * order_items.unit_price_cents),
    0
)
FROM order_items
JOIN orders ON order_items.order_id = orders.order_id
WHERE orders.status = ?
""", (order_status,)).fetchone()

total_cents = row[0]

print("订单状态：", order_status)
print("销售额（分）：", total_cents)
print(f"销售额（元）：{total_cents / 100:.2f}")

product_rows = conn.execute("""
SELECT
    products.product_name,
    SUM(order_items.quantity) AS total_quantity,
    SUM(order_items.quantity * order_items.unit_price_cents) AS total_sales_cents
FROM order_items
JOIN orders ON order_items.order_id = orders.order_id
JOIN products ON order_items.product_id = products.product_id
WHERE orders.status = ?
GROUP BY products.product_id, products.product_name
ORDER BY products.product_id
""", (order_status,)).fetchall()

print("按商品统计：")
for product_name, total_quantity, total_sales_cents in product_rows:
    print(
        f"{product_name}：销量 {total_quantity} 件，"
        f"销售额 {total_sales_cents / 100:.2f} 元"
    )

conn.close()