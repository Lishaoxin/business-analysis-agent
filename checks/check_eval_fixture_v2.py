"""仅检查数据生成与临时快照，不请求模型或操作业务数据库。"""

import copy
import io
import json
import random
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import patch

import build_eval_fixture_v2 as builder
from eval_data import FIXTURE_DIR, content_hash, restore_database


def main():
    template_path = FIXTURE_DIR / "eval_fixture_v1.json"
    template_bytes = template_path.read_bytes()
    random_state = random.getstate()
    first = builder.build_snapshot(template_path)
    second = builder.build_snapshot(template_path)
    assert first == second
    assert random.getstate() == random_state
    assert content_hash(first["dataset"]) == first["dataset_sha256"]
    assert first["dataset"]["version"] == 2
    assert first["generator"]["seed"] == builder.SEED
    print("相同种子和生成规则得到相同快照与指纹，不影响全局随机状态")

    template = json.loads(template_bytes)["dataset"]
    before = copy.deepcopy(template)
    builder.build_dataset(template)
    assert template == before
    with patch.object(builder, "SEED", builder.SEED + 1):
        changed = builder.build_snapshot(template_path)
    assert first["dataset_sha256"] != changed["dataset_sha256"]
    print("生成不修改模板；改变种子会改变数据指纹")

    with closing(sqlite3.connect(":memory:")) as conn:
        restore_database(conn, first["dataset"])
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1000
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 12
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status"))
        assert counts == {"paid": 800, "cancelled": 200}
        months = {row[0] for row in conn.execute("SELECT DISTINCT substr(order_date, 1, 7) FROM orders")}
        assert months == {f"2026-{month:02d}" for month in range(1, 7)}
        regions = {row[0] for row in conn.execute("SELECT DISTINCT region FROM orders")}
        assert regions == set(builder.REGIONS)
        assert conn.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone() == ("2026-01-01", "2026-06-30")
        assert conn.execute("SELECT COUNT(*) FROM orders o LEFT JOIN order_items i ON i.order_id=o.order_id WHERE i.item_id IS NULL").fetchone()[0] == 0
        item_count = conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
        assert 1000 <= item_count <= 4000
        print("1000笔订单、12种商品、6个月和4个地区可恢复，约束与外键检查通过")

        assert conn.execute("SELECT COUNT(*) FROM order_items WHERE unit_price_cents=0").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM order_items WHERE unit_price_cents%100!=0").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(DISTINCT unit_price_cents) FROM order_items WHERE product_id='P001'").fetchone()[0] > 1
        assert conn.execute("SELECT COUNT(*) FROM order_items WHERE product_id='P012'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE order_date>='2026-07-01'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM order_items WHERE quantity<=0 OR unit_price_cents<0").fetchone()[0] == 0
        print("不同成交价、非整数元、零金额明细、未售出商品和无订单月份均已保留")

    with TemporaryDirectory(prefix="agent_eval_fixture_check_") as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(gettempdir()).resolve()
        assert root.name.startswith("agent_eval_fixture_check_")
        output = root / "eval_fixture_v2.json"
        with patch.object(builder, "OUTPUT_PATH", output):
            with redirect_stdout(io.StringIO()):
                builder.main()
            saved_bytes = output.read_bytes()
            assert json.loads(saved_bytes) == first
            try:
                with redirect_stdout(io.StringIO()):
                    builder.main()
            except FileExistsError:
                pass
            else:
                raise AssertionError("不能覆盖已存在的快照")
            assert output.read_bytes() == saved_bytes
        print("命令入口写入有效快照，重复生成拒绝覆盖已有文件")

    assert template_path.read_bytes() == template_bytes
    print("第一版快照保持不变，所有检查仅使用内存或临时文件")
    print("第二版数据生成检查通过（未请求真实API）")


if __name__ == "__main__":
    main()
