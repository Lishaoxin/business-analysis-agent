"""检查评测报告元数据、只检查模式与采集隔离，不请求模型。"""

from contextlib import closing, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import requests

import agent
import eval_agent
from eval_data import load_eval_inputs
from eval_manifest import EVAL_SOURCE_FILES, build_eval_manifest
import tools


def check():
    original_inputs = load_eval_inputs()
    inputs = {**original_inputs, "cases": original_inputs["cases"][:3]}
    untouched = deepcopy(inputs)
    project = Path(agent.__file__).resolve().parent

    with tempfile.TemporaryDirectory(prefix="agent_eval_report_check_") as name:
        root = Path(name).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("agent_eval_report_check_")
        for filename in EVAL_SOURCE_FILES:
            candidates = Path(eval_agent.__file__).resolve().parent
            source = candidates / filename if (candidates / filename).is_file() else project / filename
            (root / filename).write_bytes(source.read_bytes())

        original_db, original_base = tools.DB_PATH, tools.BASE_DIR
        with (
            patch.object(eval_agent, "BASE_DIR", root),
            patch.object(eval_agent, "load_eval_inputs", return_value=inputs),
            patch.object(agent, "load_dotenv", side_effect=AssertionError("不得读取真实.env")),
            patch.object(agent, "request_model", side_effect=AssertionError("不得请求真实模型")),
        ):
            report = eval_agent.build_report(inputs, inputs["cases"])
            assert report["report_version"] == 2
            assert report["code_manifest"] == build_eval_manifest(root)
            assert len(report["code_manifest"]["files"]) == 18
            assert report["max_rounds"] == 6 and report["max_seconds"] == 30.0
            assert report["python_version"] and report["sqlite_version"] and report["platform"]
            assert report["package_versions"]
            assert any(key.lower() == "requests" for key in report["package_versions"])
            assert report["agent_code_sha256"] and report["tools_code_sha256"]
            assert report["rows"] == [] and report["finished_at"] is None
            invalid_cases = [{**inputs["cases"][0], "expected_status": "wrong"}]
            try:
                eval_agent.build_report(inputs, invalid_cases)
            except ValueError:
                pass
            else:
                raise AssertionError("错误预期状态没有在采集前被拒绝")
            print("报告包含18文件清单、6轮/30秒上限、实际环境与依赖版本，旧字段保留")

            with (
                patch.object(agent, "run_agent") as run_agent,
                patch.object(sys, "argv", ["eval_agent.py", "--case", "all", "--check-only"]),
                redirect_stdout(StringIO()) as output,
            ):
                eval_agent.main()
            run_agent.assert_not_called()
            assert "评测准备检查通过" in output.getvalue()
            assert not (root / "eval_runs").exists()
            assert tools.DB_PATH == original_db and tools.BASE_DIR == original_base
            print("check-only验证快照及报告信息，不启动Agent或创建评测目录")

            calls = []
            def fake_run_agent(question, **kwargs):
                assert question == inputs["cases"][len(calls)]["question"]
                assert kwargs == {"max_rounds": 6, "max_seconds": 30.0}
                assert tools.DB_PATH.resolve().is_relative_to(root)
                assert tools.BASE_DIR == tools.DB_PATH.parent
                report_path = tools.BASE_DIR / "report.json"
                initial = json.loads(report_path.read_text(encoding="utf-8"))
                assert initial["code_manifest"] == report["code_manifest"]
                with closing(sqlite3.connect(tools.DB_PATH.as_uri() + "?mode=ro", uri=True)) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 6
                calls.append((question, kwargs))
                if len(calls) == 3:
                    raise requests.Timeout("离线预设服务超时")
                return {
                    "status": "completed" if len(calls) == 1 else "failed",
                    "answer": "离线预设回答。", "evidence_query_ids": [],
                    "steps": [], "charts": [],
                }

            with (
                patch.object(agent, "run_agent", side_effect=fake_run_agent),
                patch.object(sys, "argv", ["eval_agent.py", "--case", "all"]),
                redirect_stdout(StringIO()),
            ):
                eval_agent.main()
            directories = list((root / "eval_runs").iterdir())
            assert len(directories) == 1
            finished = json.loads((directories[0] / "report.json").read_text(encoding="utf-8"))
            assert finished["finished_at"]
            assert finished["code_manifest"] == report["code_manifest"]
            assert len(calls) == len(finished["rows"]) == 3
            assert finished["rows"][0]["execution_status"] == "completed"
            assert finished["rows"][1]["execution_status"] == "completed"
            assert finished["rows"][1]["result"]["status"] == "failed"
            assert finished["rows"][2]["execution_status"] == "failed"
            assert finished["rows"][2]["error_type"] == "Timeout"
            assert all(row["review"]["passed"] is None for row in finished["rows"])
            assert all(row["expected_status"] == "completed" for row in finished["rows"])
            assert all((directories[0] / row["log_file"]).is_file() for row in finished["rows"])
            assert tools.DB_PATH == original_db and tools.BASE_DIR == original_base
            print("真实采集流程使用独立快照库，只向Agent传问题与明确上限")
            print("首份及最终报告均保留代码清单，执行状态与业务状态分别记录")
            print("正常返回、业务失败和服务异常均保存日志，未自动判题通过")

            with (
                patch.object(eval_agent, "restore_database", side_effect=RuntimeError("离线恢复失败")),
                patch.object(agent, "run_agent") as run_agent,
                patch.object(sys, "argv", ["eval_agent.py", "--case", "E01"]),
                redirect_stdout(StringIO()),
            ):
                try:
                    eval_agent.main()
                except RuntimeError as exc:
                    assert str(exc) == "离线恢复失败"
                else:
                    raise AssertionError("数据库恢复失败被静默忽略")
            run_agent.assert_not_called()
            assert tools.DB_PATH == original_db and tools.BASE_DIR == original_base
            assert inputs == untouched
            print("数据库恢复异常仍恢复工具全局路径，原输入快照未被修改")

            missing = root / "graph_agent.py"
            saved = missing.read_bytes()
            missing.unlink()
            before = set((root / "eval_runs").iterdir())
            with (
                patch.object(agent, "run_agent") as run_agent,
                patch.object(sys, "argv", ["eval_agent.py", "--case", "all"]),
            ):
                try:
                    eval_agent.main()
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError("代码清单不完整时仍开始采集")
            run_agent.assert_not_called()
            assert set((root / "eval_runs").iterdir()) == before
            missing.write_bytes(saved)
            print("必需源码缺失时在启动Agent和创建批次之前报错")
    print("评测报告元数据检查通过（预设Agent与临时数据库，未请求真实API）")


if __name__ == "__main__":
    check()
