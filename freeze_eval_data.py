import hashlib
import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "business.db"
OUTPUT_PATH = BASE_DIR / "eval_fixture_v1.json"

TABLE_KEYS = {
    "orders": "order_id",
    "products": "product_id",
    "order_items": "item_id",
}

conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
try:
    conn.execute("BEGIN")
    tables = {}
    for table_name, primary_key in TABLE_KEYS.items():
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()[0]
        cursor = conn.execute(
            f"SELECT * FROM {table_name} ORDER BY {primary_key}"
        )
        tables[table_name] = {
            "schema": schema,
            "columns": [column[0] for column in cursor.description],
            "rows": [list(row) for row in cursor.fetchall()],
        }
finally:
    conn.close()

dataset = {"version": 1, "tables": tables}
canonical = json.dumps(
    dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")
)
fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
report = {"dataset_sha256": fingerprint, "dataset": dataset}

with OUTPUT_PATH.open("x", encoding="utf-8") as file:
    json.dump(report, file, ensure_ascii=False, indent=2)

print("固定数据已保存：", OUTPUT_PATH.name)
print("数据指纹：", fingerprint[:16])
for table_name, table in tables.items():
    print(table_name, "行数：", len(table["rows"]))