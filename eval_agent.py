import argparse
import hashlib
import json
import sqlite3
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import agent
import tools
from eval_data import load_eval_inputs, restore_database

BASE_DIR = Path(__file__).resolve().parent
MAX_ROUNDS = 6
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
        "requires_chart": case.get("requires_chart", False),
        "review": {"passed": None, "notes": ""},
    }
    log_path = run_dir / f"{case['id']}.log"
    started = perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                record["result"] = agent.run_agent(
                    case["question"], max_rounds=MAX_ROUNDS
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="E01", help="题目ID，填all运行全部")
    args = parser.parse_args()

    inputs = load_eval_inputs()
    cases = inputs["cases"]
    if args.case != "all":
        cases = [case for case in cases if case["id"] == args.case]
    if not cases:
        parser.error("找不到指定的题目ID")

    run_dir = BASE_DIR / "eval_runs" / f"run_{uuid4().hex[:12]}"
    run_dir.mkdir(parents=True)
    report_path = run_dir / "report.json"
    report = {
        "started_at": datetime.now(LOCAL_TZ).isoformat(),
        "finished_at": None,
        "requested_model": agent.MODEL_NAME,
        "max_rounds": MAX_ROUNDS,
        "python_version": sys.version.split()[0],
        "sqlite_version": sqlite3.sqlite_version,
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
                print("回答：", record["result"]["answer"])

        report["finished_at"] = datetime.now(LOCAL_TZ).isoformat()
        save_report(report_path, report)
    finally:
        tools.DB_PATH, tools.BASE_DIR = old_db, old_base

    print("报告：", report_path)


if __name__ == "__main__":
    main()