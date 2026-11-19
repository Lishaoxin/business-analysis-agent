"""生成第二版评测快照；不读取或修改业务数据库。"""

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from random import Random

from eval_data import FIXTURE_DIR, TABLE_ORDER, content_hash, restore_database

OUTPUT_PATH = FIXTURE_DIR / "eval_fixture_v2.json"
SEED = 20261118
ORDER_COUNT = 1000
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 6, 30)
REGIONS = ("华东", "华南", "华北", "西南")

# 第四项只是生成数据时使用的基准价，不会作为商品表的新字段。
PRODUCTS = (
    ("P001", "机械键盘", "电脑配件", 29900),
    ("P002", "无线鼠标", "电脑配件", 9900),
    ("P003", "扩展坞", "电脑配件", 15900),
    ("P004", "24英寸显示器", "显示设备", 89900),
    ("P005", "27英寸显示器", "显示设备", 149900),
    ("P006", "显示器支架", "显示设备", 19900),
    ("P007", "打印机", "办公设备", 129900),
    ("P008", "扫描仪", "办公设备", 69900),
    ("P009", "碎纸机", "办公设备", 39900),
    ("P010", "固态硬盘", "存储设备", 49900),
    ("P011", "U盘", "存储设备", 5900),
    ("P012", "移动硬盘", "存储设备", 69900),
)

# 日期、地区、状态，以及本单的(商品编号、数量、成交单价分)。
# 明确保留日期边界、同商品不同成交价、非整数元和零金额订单。
ANCHOR_ORDERS = (
    ("2026-01-01", "华东", "paid", (("P001", 1, 29900), ("P002", 2, 9900))),
    ("2026-01-31", "华南", "paid", (("P001", 2, 27900),)),
    ("2026-02-01", "华北", "paid", (("P010", 1, 47405),)),
    ("2026-06-30", "西南", "paid", (("P011", 1, 0),)),
    ("2026-03-15", "华东", "cancelled", (("P004", 1, 89900),)),
)


def build_dataset(template):
    # 只复用旧快照的表结构和字段，不复用旧订单。
    tables = {
        name: {
            "schema": template["tables"][name]["schema"],
            "columns": list(template["tables"][name]["columns"]),
            "rows": [],
        }
        for name in TABLE_ORDER
    }
    tables["products"]["rows"] = [list(product[:3]) for product in PRODUCTS]
    rng = Random(SEED)
    day_count = (END_DATE - START_DATE).days + 1
    item_id = 1

    for number in range(1, ORDER_COUNT + 1):
        order_id = f"V2O{number:04d}"
        if number <= len(ANCHOR_ORDERS):
            order_date, region, status, items = ANCHOR_ORDERS[number - 1]
        else:
            order_date = (START_DATE + timedelta(days=rng.randrange(day_count))).isoformat()
            region = rng.choice(REGIONS)
            status = "cancelled" if number % 5 == 0 else "paid"
            # P012保留为未售出商品；每笔订单购买1至4种不同商品。
            selected = rng.sample(PRODUCTS[:-1], rng.randint(1, 4))
            items = []
            for product_id, _, _, base_price in selected:
                quantity = rng.randint(1, 5)
                price_cents = base_price * rng.choice((90, 95, 100, 105)) // 100
                items.append((product_id, quantity, price_cents))

        tables["orders"]["rows"].append([order_id, order_date, region, status])
        for product_id, quantity, price_cents in items:
            tables["order_items"]["rows"].append(
                [item_id, order_id, product_id, quantity, price_cents]
            )
            item_id += 1

    return {"version": 2, "tables": tables}


def build_snapshot(source_path=None):
    if source_path is None:
        source_path = FIXTURE_DIR / "eval_fixture_v1.json"
    original = json.loads(Path(source_path).read_text(encoding="utf-8"))
    if original["dataset"]["version"] != 1:
        raise ValueError("生成模板必须是第一版数据快照")
    if content_hash(original["dataset"]) != original["dataset_sha256"]:
        raise ValueError("模板快照指纹校验失败")

    dataset = build_dataset(original["dataset"])
    return {
        "dataset_sha256": content_hash(dataset),
        "generator": {
            "seed": SEED,
            "order_count": ORDER_COUNT,
            "date_start": START_DATE.isoformat(),
            "date_end": END_DATE.isoformat(),
        },
        "dataset": dataset,
    }


def main():
    snapshot = build_snapshot()
    # 先在内存中恢复，检查主键、约束和外键，再保存新快照。
    with closing(sqlite3.connect(":memory:")) as conn:
        restore_database(conn, snapshot["dataset"])
        status_counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM orders GROUP BY status"
        ).fetchall())
        month_count = conn.execute(
            "SELECT COUNT(DISTINCT substr(order_date, 1, 7)) FROM orders"
        ).fetchone()[0]

    # x模式拒绝覆盖已有快照，防止误改已固定的评测数据。
    with OUTPUT_PATH.open("x", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print("第二版数据快照已保存：", OUTPUT_PATH.name)
    print("固定随机种子：", SEED)
    print("数据指纹：", snapshot["dataset_sha256"][:16])
    for name in TABLE_ORDER:
        print(name, "行数：", len(snapshot["dataset"]["tables"][name]["rows"]))
    print("月份数量：", month_count)
    print("已支付订单：", status_counts["paid"])
    print("已取消订单：", status_counts["cancelled"])


if __name__ == "__main__":
    main()
