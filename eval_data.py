import hashlib
import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TABLE_ORDER = ("orders", "products", "order_items")


def content_hash(value):
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_eval_inputs():
    snapshot = json.loads(
        (BASE_DIR / "eval_fixture_v1.json").read_text(encoding="utf-8")
    )
    suite = json.loads(
        (BASE_DIR / "eval_cases_v1.json").read_text(encoding="utf-8")
    )
    dataset = snapshot["dataset"]
    if dataset["version"] != 1 or suite["version"] != 1:
        raise ValueError("不支持的评测文件版本")

    fingerprint = content_hash(dataset)
    if fingerprint != snapshot["dataset_sha256"]:
        raise ValueError("数据快照指纹校验失败")
    if fingerprint != suite["dataset_sha256"]:
        raise ValueError("题目对应的数据版本与快照不一致")

    cases = suite["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("题目必须非空且ID不重复")

    return {
        "dataset": dataset,
        "cases": cases,
        "dataset_sha256": fingerprint,
        "cases_sha256": content_hash(suite),
    }


def restore_database(conn, dataset):
    conn.execute("PRAGMA foreign_keys = ON")
    tables = dataset["tables"]
    for table_name in TABLE_ORDER:
        conn.execute(tables[table_name]["schema"])

    for table_name in TABLE_ORDER:
        table = tables[table_name]
        columns = ", ".join(table["columns"])
        placeholders = ", ".join("?" for _ in table["columns"])
        conn.executemany(
            f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})",
            table["rows"],
        )

    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("快照数据的外键检查失败")
    conn.commit()


def main():
    inputs = load_eval_inputs()
    conn = sqlite3.connect(":memory:")
    try:
        restore_database(conn, inputs["dataset"])
        print("离线检查通过")
        print("数据指纹：", inputs["dataset_sha256"][:16])
        print("题集指纹：", inputs["cases_sha256"][:16])
        print("固定题目数量：", len(inputs["cases"]))
        for table_name in TABLE_ORDER:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
            print(table_name, "行数：", count)
    finally:
        conn.close()


if __name__ == "__main__":
    main()