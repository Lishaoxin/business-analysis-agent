from time import monotonic
import math
import sqlite3
from pathlib import Path
from uuid import uuid4
from app_paths import DB_PATH, OUTPUT_DIR
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "WenQuanYi Zen Hei"]
plt.rcParams["axes.unicode_minus"] = False

BASE_DIR = Path(__file__).resolve().parent
SQL_TIMEOUT_SECONDS = 2.0

ALLOWED_TABLES = {"orders", "products", "order_items"}

ALLOWED_FUNCTIONS = {
    "sum", "count", "avg", "min", "max",
    "coalesce", "round", "nullif", "date", "strftime",
}

def get_schema():
    conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

    try:
        rows = conn.execute("""
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN (?, ?, ?)
        ORDER BY name
        """, ("orders", "products", "order_items")).fetchall()

        schema = {}
        for table_name, create_sql in rows:
            schema[table_name] = create_sql

        return schema
    finally:
        conn.close()

def authorize_sql(action, arg1, arg2, db_name, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK

    if action == sqlite3.SQLITE_READ:
        # COUNT(*) 等查询的 db_name 可能为 None。
        if db_name in (None, "main") and (arg1 or "").lower() in ALLOWED_TABLES:
            return sqlite3.SQLITE_OK

    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK

    return sqlite3.SQLITE_DENY

def run_sql(sql, params=()):
    if not isinstance(sql, str) or not sql.strip().upper().startswith("SELECT"):
        raise ValueError("当前工具接受以 SELECT 开头的单条查询")

    conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

    try:
        conn.set_authorizer(authorize_sql)
        deadline = monotonic() + SQL_TIMEOUT_SECONDS

        def check_timeout():
            return monotonic() >= deadline

        conn.set_progress_handler(check_timeout, 1000)
        cursor = conn.execute(sql, params)

        columns = []
        for column in cursor.description:
            columns.append(column[0])

        fetched_rows = cursor.fetchmany(101)

        rows = []
        for row in fetched_rows[:100]:
            rows.append(list(row))

        return {
            "columns": columns,
            "rows": rows,
            "truncated": len(fetched_rows) > 100,
        }
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise TimeoutError("SQL 执行超时，请简化查询") from exc
        raise
    finally:
        conn.close()

def draw_chart(result, title="查询结果"):

    if result["truncated"]:
        raise ValueError("查询结果超过100行，请先缩小查询范围")
    if len(result["columns"]) != 2:
        raise ValueError("绘图查询需要两列：第一列类别，第二列数值")
    if not result["rows"]:
        raise ValueError("查询结果为空，无法绘图")

    labels = []
    values = []
    for row in result["rows"]:
        if not isinstance(row[1], (int, float)) or not math.isfinite(row[1]):
            raise ValueError("第二列必须是有效数值")
        labels.append(str(row[0]))
        values.append(row[1])

    output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    chart_path = output_dir / f"chart_{uuid4().hex}.png"

    fig, ax = plt.subplots(figsize=(8, 4.5), layout="constrained")
    try:
        positions = list(range(len(labels)))
        bars = ax.bar(positions, values)
        ax.set_xticks(positions, labels)
        ax.set_title(title)
        ax.set_xlabel(result["columns"][0])
        ax.set_ylabel(result["columns"][1])
        ax.bar_label(bars, fmt="%.2f", padding=3)
        ax.margins(y=0.15)
        fig.savefig(chart_path, dpi=150)
    finally:
        plt.close(fig)

    return {
        "chart_path": str(chart_path),
        "row_count": len(values),
    }

if __name__ == "__main__":
    sql = """
    SELECT
        products.product_name AS product_name,
        SUM(order_items.quantity * order_items.unit_price_cents) / 100.0 AS sales_yuan
    FROM order_items
    JOIN orders ON order_items.order_id = orders.order_id
    JOIN products ON order_items.product_id = products.product_id
    WHERE orders.status = ?
    GROUP BY products.product_id, products.product_name
    ORDER BY products.product_id
    """

    result = run_sql(sql, ("paid",))

    print("列名：", result["columns"])
    print("绘图数据：", result["rows"])
    print("是否截断：", result["truncated"])
    chart_result = draw_chart(
        result,
        title="已支付订单各商品销售额",
    )
    print("绘图结果：", chart_result)