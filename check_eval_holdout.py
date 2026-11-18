"""核对保留题定义与冻结代码，不运行Agent或请求模型。"""

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import monotonic

from eval_data import BASE_DIR, content_hash, restore_database
from eval_manifest import build_eval_manifest
from tools import authorize_sql

RUNTIME_FILES = (
    "agent.py", "graph_agent.py", "agent_state.py", "tools.py",
    "tool_models.py", "result_models.py", "completion_checks.py",
    "query_store.py", "usage_models.py", "run_budget.py",
    "session_runner.py", "event_models.py",
)


def check_suite(snapshot, suite, development, source_dir):
    fingerprint = content_hash(snapshot["dataset"])
    if fingerprint != snapshot["dataset_sha256"]:
        raise ValueError("数据快照指纹校验失败")
    if fingerprint != suite["dataset_sha256"] or fingerprint != development["dataset_sha256"]:
        raise ValueError("开发题和保留题必须绑定同一固定快照")
    if type(snapshot["dataset"]["version"]) is not int or snapshot["dataset"]["version"] != 2:
        raise ValueError("本检查用于第二版快照")
    if type(suite["version"]) is not int or suite["version"] != 2:
        raise ValueError("保留题集版本必须为2")
    if suite["split"] != "holdout" or development["split"] != "development":
        raise ValueError("开发与保留题集用途必须明确区分")
    frozen = suite["frozen_runtime_files"]
    if set(frozen) != set(RUNTIME_FILES):
        raise ValueError("冻结清单必须覆盖12个Agent核心文件")
    current = build_eval_manifest(source_dir)["files"]
    changed = [name for name in RUNTIME_FILES if current[name] != frozen[name]]
    if changed:
        raise ValueError("Agent核心代码与保留题冻结版本不同：" + ", ".join(changed))

    cases = suite["cases"]
    if len(cases) != 20 or {case["id"] for case in cases} != {f"T{n:02d}" for n in range(1, 21)}:
        raise ValueError("保留题应有20题，编号为不重复的T01至T20")
    if len({case["question"] for case in cases}) != len(cases):
        raise ValueError("保留题不能重复")
    if {case["id"] for case in cases} & {case["id"] for case in development["cases"]}:
        raise ValueError("开发与保留题编号不能重复")
    if {case["question"] for case in cases} & {case["question"] for case in development["cases"]}:
        raise ValueError("保留题不能直接重复开发题")
    statuses = Counter(case["expected_status"] for case in cases)
    if statuses != {"completed": 18, "needs_clarification": 2}:
        raise ValueError("本批保留题应包含18道查询题和2道澄清题")

    with closing(sqlite3.connect(":memory:")) as conn:
        restore_database(conn, snapshot["dataset"])
        conn.set_authorizer(authorize_sql)
        for case in cases:
            if not isinstance(case["question"], str) or not case["question"].strip():
                raise ValueError("题目不能为空白")
            if type(case.get("requires_chart", False)) is not bool:
                raise ValueError("requires_chart必须是布尔值")
            if case["expected_status"] == "needs_clarification":
                expected = case["expected"]
                if not isinstance(expected.get("ask_for"), list) or not expected["ask_for"]:
                    raise ValueError("澄清题必须列明需要询问的条件")
                if not all(isinstance(value, str) and value.strip() for value in expected["ask_for"]):
                    raise ValueError("澄清条件必须是非空文本")
                if expected.get("must_not_assume_scope") is not True:
                    raise ValueError("澄清题必须明确不能猜测范围")
                if not case.get("review_points") or "reference_sql" in case or case.get("requires_chart", False):
                    raise ValueError("澄清题应提供人工审核要点，不设置查询答案或图表")
                continue

            if not case["reference_sql"].lstrip().upper().startswith("SELECT"):
                raise ValueError("参考查询必须以SELECT开头")
            deadline = monotonic() + 2.0
            conn.set_progress_handler(lambda: monotonic() >= deadline, 1000)
            cursor = conn.execute(case["reference_sql"], case["reference_params"])
            columns = [column[0] for column in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
            if len(rows) > 100:
                raise ValueError("参考结果不能超过现有工具的100行预览范围")
            if set(case["expected"]) == {"rows"}:
                actual = {"rows": rows}
            else:
                if len(rows) != 1:
                    raise ValueError(f"题目{case['id']}应返回一行标准结果")
                actual = dict(zip(columns, rows[0]))
            if actual != case["expected"]:
                raise ValueError(f"题目{case['id']}标准答案与参考SQL不一致")
            if case.get("requires_chart") and (len(columns) != 2 or not rows):
                raise ValueError("绘图题的参考结果必须是非空的两列数据")
    return statuses


def main():
    paths = {name: BASE_DIR/name for name in ("eval_fixture_v2.json", "eval_cases_v2_dev.json", "eval_cases_v2_test.json")}
    before = {name: path.read_bytes() for name, path in paths.items()}
    snapshot = json.loads(before["eval_fixture_v2.json"])
    development = json.loads(before["eval_cases_v2_dev.json"])
    suite = json.loads(before["eval_cases_v2_test.json"])
    counts = check_suite(snapshot, suite, development, BASE_DIR)
    if any(path.read_bytes() != before[name] for name, path in paths.items()):
        raise AssertionError("检查期间输入文件被修改")
    print("冻结代码版本：", suite["frozen_source_revision"])
    print("Agent核心文件：12个指纹一致")
    print("保留题目数量：", sum(counts.values()))
    print("查询题：", counts["completed"], "澄清题：", counts["needs_clarification"])
    print("数据指纹：", suite["dataset_sha256"][:16])
    print("保留题集指纹：", content_hash(suite)[:16])
    print("18道查询题的标准答案与只读SQL结果一致，2道澄清题已定义人工审核要点")
    print("开发题与保留题无重复编号或相同题目，输入文件保持不变")
    print("保留题准备检查通过（未运行Agent、未请求模型API）")


if __name__ == "__main__":
    main()
