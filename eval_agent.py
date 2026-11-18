import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import traceback
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from importlib.metadata import distributions
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import agent
import tools
from eval_data import EVAL_SUITES, load_eval_inputs, restore_database
from eval_manifest import build_eval_manifest

BASE_DIR = Path(__file__).resolve().parent
MAX_ROUNDS = 6
MAX_SECONDS = 30.0
LOCAL_TZ = timezone(timedelta(hours=8))


def save_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def run_case(case, run_dir):
    record = {
        "id": case["id"],
        "question": case["question"],
        "expected": case["expected"],
        "expected_status": case.get("expected_status", "completed"),
        "requires_chart": case.get("requires_chart", False),
        "review": {"passed": None, "notes": ""},
    }
    log_path = run_dir / f"{case['id']}.log"
    started = perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                record["result"] = agent.run_agent(
                    case["question"], max_rounds=MAX_ROUNDS,
                    max_seconds=MAX_SECONDS,
                )
                record["execution_status"] = "completed"
            except Exception as exc:
                record["execution_status"] = "failed"
                record["error_type"] = type(exc).__name__
                record["error"] = str(exc)
                traceback.print_exc(file=log)

    record["elapsed_seconds"] = round(perf_counter() - started, 3)
    record["log_file"] = log_path.name
    return record


def get_package_versions():
    packages = {}
    for distribution in distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[name] = distribution.version
    return dict(sorted(packages.items(), key=lambda item: item[0].lower()))


def build_report(inputs, cases):
    for case in cases:
        if case.get("expected_status", "completed") not in (
            "completed", "needs_clarification", "failed"
        ):
            raise ValueError(f"题目{case['id']}的expected_status不受支持")
    return {
        "report_version": 2,
        "suite_name": inputs["suite_name"],
        "split": inputs["split"],
        "dataset_file": inputs["dataset_file"],
        "cases_file": inputs["cases_file"],
        "dataset_version": inputs["dataset_version"],
        "cases_version": inputs["cases_version"],
        "started_at": datetime.now(LOCAL_TZ).isoformat(),
        "finished_at": None,
        "requested_model": agent.MODEL_NAME,
        "max_rounds": MAX_ROUNDS,
        "max_seconds": MAX_SECONDS,
        "python_version": sys.version.split()[0],
        "sqlite_version": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "package_versions": get_package_versions(),
        "code_manifest": build_eval_manifest(BASE_DIR),
        "dataset_sha256": inputs["dataset_sha256"],
        "cases_sha256": inputs["cases_sha256"],
        "agent_code_sha256": hashlib.sha256(
            Path(agent.__file__).read_bytes()
        ).hexdigest(),
        "tools_code_sha256": hashlib.sha256(
            Path(tools.__file__).read_bytes()
        ).hexdigest(),
        "requested_cases": [case["id"] for case in cases],
        "rows": [],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=EVAL_SUITES, default="v1", help="选择评测输入，默认v1")
    parser.add_argument("--case", help="题目ID，填all运行全部，默认所选题集首题")
    parser.add_argument("--check-only", action="store_true", help="只检查输入与报告信息，不请求模型")
    args = parser.parse_args()

    inputs = load_eval_inputs(args.suite)
    cases = inputs["cases"]
    selected_case = args.case if args.case is not None else cases[0]["id"]
    if selected_case != "all":
        cases = [case for case in cases if case["id"] == selected_case]
    if not cases:
        parser.error("找不到指定的题目ID")

    report = build_report(inputs, cases)
    if args.check_only:
        with closing(sqlite3.connect(":memory:")) as conn:
            restore_database(conn, inputs["dataset"])
        print("评测准备检查通过（未请求真实API）")
        print("评测题集：", report["suite_name"])
        print("题集用途：", report["split"])
        print("数据文件：", report["dataset_file"])
        print("题目文件：", report["cases_file"])
        print("数据指纹：", report["dataset_sha256"][:16])
        print("题集指纹：", report["cases_sha256"][:16])
        print("固定题目数量：", len(cases))
        for table_name, table in inputs["dataset"]["tables"].items():
            print(table_name, "行数：", len(table["rows"]))
        print("模型：", report["requested_model"])
        print("模型调用上限：", report["max_rounds"])
        print("任务时间预算（秒）：", report["max_seconds"])
        print("代码清单文件数：", len(report["code_manifest"]["files"]))
        print("代码总指纹：", report["code_manifest"]["code_sha256"])
        print("Python：", report["python_version"])
        print("SQLite：", report["sqlite_version"])
        print("记录依赖数量：", len(report["package_versions"]))
        return

    run_dir = BASE_DIR / "eval_runs" / f"run_{uuid4().hex[:12]}"
    run_dir.mkdir(parents=True)
    report_path = run_dir / "report.json"

    old_db, old_base = tools.DB_PATH, tools.BASE_DIR
    try:
        tools.DB_PATH = run_dir / "business.db"
        tools.BASE_DIR = run_dir
        conn = sqlite3.connect(tools.DB_PATH)
        try:
            restore_database(conn, inputs["dataset"])
        finally:
            conn.close()

        save_report(report_path, report)
        for case in cases:
            print("开始：", case["id"], case["question"], flush=True)
            record = run_case(case, run_dir)
            report["rows"].append(record)
            save_report(report_path, report)
            print(case["id"], record["execution_status"],
                  "耗时：", record["elapsed_seconds"], "秒")
            if record["execution_status"] == "completed":
                print("业务状态：", record["result"].get("status", "legacy"))
                print("回答：", record["result"]["answer"])

        report["finished_at"] = datetime.now(LOCAL_TZ).isoformat()
        save_report(report_path, report)
    finally:
        tools.DB_PATH, tools.BASE_DIR = old_db, old_base

    print("报告：", report_path)


if __name__ == "__main__":
    main()