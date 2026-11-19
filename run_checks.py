"""在临时项目副本中统一运行自动回归检查；不运行真实模型评测。"""

import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory, gettempdir
from time import perf_counter

from eval_data import FIXTURE_DIR, restore_database

BASE_DIR = Path(__file__).resolve().parent
CHECK_DIR = BASE_DIR / "checks"

PYTHON_CHECKS = (
    "check_app_paths.py",
    "check_container_start.py",
    "check_tool_arguments.py",
    "check_query_id.py",
    "check_draw_query_id.py",
    "check_completion_evidence.py",
    "check_query_snapshot.py",
    "check_agent_completion.py",
    "check_graph_agent.py",
    "check_turn_history.py",
    "check_session_memory.py",
    "check_agent_events.py",
    "check_tool_timing.py",
    "check_model_timing.py",
    "check_model_usage.py",
    "check_usage_summary.py",
    "check_graph_budget.py",
    "check_entry_budget.py",
    "check_session_api.py",
    "check_sse_api.py",
    "check_trace_api.py",
    "check_eval_fixture_v2.py",
    "check_eval_cases_v2.py",
    "check_eval_manifest.py",
    "check_eval_report_metadata.py",
    "check_eval_review.py",
    "check_eval_suite_entry.py",
)
WEB_CHECK = "check_session_page.cjs"
PROJECT_FILES = ("requirements.txt", "requirements-dev.txt", "index.html")
FIXTURE_FILES = (
    "eval_fixture_v1.json", "eval_cases_v1.json", "eval_fixture_v2.json",
    "eval_cases_v2_dev.json", "eval_cases_v2_test.json",
)


def prepare_workspace(root):
    # 复制代码与固定输入；不复制.env、数据库、会话或已有运行产物。
    for source in BASE_DIR.glob("*.py"):
        shutil.copy2(source, root / source.name)
    for name in PROJECT_FILES:
        shutil.copy2(BASE_DIR / name, root / name)
    # 检查之间有辅助函数依赖，在临时副本中放到一层，保留原有导入。
    for source in CHECK_DIR.iterdir():
        if source.is_file() and source.suffix in {".py", ".cjs"}:
            shutil.copy2(source, root / source.name)

    fixtures = root / FIXTURE_DIR.relative_to(BASE_DIR)
    fixtures.mkdir(parents=True)
    for name in FIXTURE_FILES:
        shutil.copy2(FIXTURE_DIR / name, fixtures / name)

    # 依赖业务表结构的旧检查使用这份临时数据库。
    snapshot = json.loads((fixtures / "eval_fixture_v1.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(root / "business.db")) as conn:
        restore_database(conn, snapshot["dataset"])


def stop_process(process):
    # 只终止本入口启动的检查进程及其子进程。
    if os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_one(command, root, env, timeout):
    options = {"start_new_session": True}
    if os.name == "nt":
        options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        }
    started = perf_counter()
    process = subprocess.Popen(
        command, cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **options,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process(process)
        output, _ = process.communicate()
        return "TIMEOUT", perf_counter() - started, output
    except BaseException:
        stop_process(process)
        process.communicate()
        raise
    status = "PASS" if process.returncode == 0 else "FAIL"
    return status, perf_counter() - started, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="只列出检查，不执行")
    parser.add_argument("--include-web", action="store_true", help="默认清单额外包含Node网页检查")
    parser.add_argument("--only", action="append", choices=(*PYTHON_CHECKS, WEB_CHECK), help="只运行指定检查，可重复")
    parser.add_argument("--timeout", type=float, default=60.0, help="单个检查的超时秒数，默认60")
    parser.add_argument("--verbose", action="store_true", help="通过时也显示脚本完整输出")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout必须是有限正数")
    selected = list(dict.fromkeys(args.only)) if args.only else list(PYTHON_CHECKS)
    if args.include_web and not args.only:
        selected.append(WEB_CHECK)
    if args.list:
        for number, name in enumerate(selected, 1):
            print(f"{number:02d}. {name}")
        print(f"共{len(selected)}项；这是自动回归清单，不包含真实模型评测。")
        return 0

    required = [
        *(BASE_DIR / name for name in PROJECT_FILES),
        *(FIXTURE_DIR / name for name in FIXTURE_FILES),
        *(CHECK_DIR / name for name in (*selected, "checkpoint_demo.py")),
    ]
    missing = [str(path.relative_to(BASE_DIR)) for path in required if not path.is_file()]
    if missing:
        parser.error("缺少文件：" + ", ".join(missing))
    node = shutil.which("node") if WEB_CHECK in selected else None
    if WEB_CHECK in selected and node is None:
        parser.error("网页检查需要Node.js，请安装后再运行")

    failures = []
    started = perf_counter()
    with TemporaryDirectory(prefix="agent_offline_checks_") as directory:
        root = Path(directory).resolve()
        if root.parent != Path(gettempdir()).resolve() or not root.name.startswith("agent_offline_checks_"):
            raise RuntimeError("临时工作目录不符合预期")
        prepare_workspace(root)
        excluded = {"DEEPSEEK_API_KEY", "PYTHONPATH", "PYTHONOPTIMIZE", "AGENT_DATA_DIR"}
        env = {name: os.environ[name] for name in os.environ if name not in excluded}
        env.update({
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(root / ".mplconfig"),
        })
        print(f"开始自动回归：{len(selected)}项，使用临时项目和数据库。", flush=True)
        for index, name in enumerate(selected, 1):
            command = [node, str(root / name)] if name == WEB_CHECK else [
                sys.executable, "-X", "utf8", str(root / name)
            ]
            try:
                status, seconds, output = run_one(command, root, env, args.timeout)
            except OSError as exc:
                status, seconds, output = "FAIL", 0.0, str(exc)
            print(f"[{index}/{len(selected)}] {status} {name} ({seconds:.2f}s)", flush=True)
            if status != "PASS":
                failures.append(name)
            if args.verbose or status != "PASS":
                print(output.rstrip() or "（没有输出）", flush=True)

    print(f"自动回归：{len(selected) - len(failures)}/{len(selected)}通过，总耗时{perf_counter() - started:.2f}秒。")
    if failures:
        print("未通过：" + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
