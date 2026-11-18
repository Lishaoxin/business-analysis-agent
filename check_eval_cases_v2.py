"""核对开发题集及参考答案；不会调用Agent或模型服务。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from eval_data import BASE_DIR, content_hash, restore_database


def check_suite(snapshot, suite):
    fingerprint = content_hash(snapshot["dataset"])
    if fingerprint != snapshot["dataset_sha256"]:
        raise ValueError("快照指纹校验失败")
    if fingerprint != suite["dataset_sha256"]:
        raise ValueError("题集未绑定当前快照")
    if snapshot["dataset"]["version"] != 2 or suite["version"] != 2:
        raise ValueError("本检查用于第二版开发题集")
    if suite["split"] != "development":
        raise ValueError("开发题集的split必须是development")

    cases = suite["cases"]
    if len(cases) != 20 or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("开发题集应有20题且ID不重复")
    if {case["id"] for case in cases} != {f"D{n:02d}" for n in range(1, 21)}:
        raise ValueError("开发题目编号应为D01至D20")
    if len({case["question"] for case in cases}) != len(cases):
        raise ValueError("题目不能重复")

    with closing(sqlite3.connect(":memory:")) as conn:
        restore_database(conn, snapshot["dataset"])
        for case in cases:
            if not isinstance(case["question"], str) or not case["question"].strip():
                raise ValueError("题目不能为空白")
            if case["expected_status"] != "completed":
                raise ValueError("本批开发题目均要求完成分析")
            if type(case.get("requires_chart", False)) is not bool:
                raise ValueError("requires_chart必须是布尔值")
            if not case["reference_sql"].lstrip().upper().startswith("SELECT"):
                raise ValueError("本批参考查询均应为SELECT")
            cursor = conn.execute(case["reference_sql"], case["reference_params"])
            rows = [list(row) for row in cursor.fetchall()]
            if set(case["expected"]) == {"rows"}:
                actual = {"rows": rows}
            else:
                if len(rows) != 1:
                    raise ValueError(f"题目{case['id']}应返回一行标准结果")
                actual = dict(zip([column[0] for column in cursor.description], rows[0]))
            if actual != case["expected"]:
                raise ValueError(f"题目{case['id']}的标准答案与参考查询不一致")
    return len(cases)


def main():
    snapshot_path = BASE_DIR / "eval_fixture_v2.json"
    suite_path = BASE_DIR / "eval_cases_v2_dev.json"
    before = {path: path.read_bytes() for path in (snapshot_path, suite_path)}
    snapshot = json.loads(before[snapshot_path])
    suite = json.loads(before[suite_path])
    count = check_suite(snapshot, suite)
    if any(path.read_bytes() != content for path, content in before.items()):
        raise AssertionError("检查期间输入文件被修改")
    print("开发题目数量：", count)
    print("数据指纹：", suite["dataset_sha256"][:16])
    print("题集指纹：", content_hash(suite)[:16])
    print("20题标准答案均与内存数据库参考查询一致")
    print("题集和快照保持不变；未调用Agent、未请求模型API")
    print("第二版开发题集检查通过")


if __name__ == "__main__":
    main()
