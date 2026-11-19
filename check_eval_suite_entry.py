"""检查v1与v2开发题集的入口选择，使用预设Agent和临时数据库。"""

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import patch

import agent
import eval_agent
import eval_data
from eval_manifest import EVAL_SOURCE_FILES, build_eval_manifest
import tools


def check():
    project = Path(agent.__file__).resolve().parent
    input_names = ("eval_fixture_v1.json", "eval_cases_v1.json", "eval_fixture_v2.json", "eval_cases_v2_dev.json")
    original_inputs = {name: (project / name).read_bytes() for name in input_names}
    old_db, old_base, old_output = tools.DB_PATH, tools.BASE_DIR, tools.OUTPUT_DIR

    with TemporaryDirectory(prefix="agent_eval_suite_check_") as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(gettempdir()).resolve()
        assert root.name.startswith("agent_eval_suite_check_")
        candidate_dir = Path(eval_agent.__file__).resolve().parent
        for name in EVAL_SOURCE_FILES:
            source = candidate_dir / name if (candidate_dir / name).is_file() else project / name
            (root / name).write_bytes(source.read_bytes())
        for name, content in original_inputs.items():
            (root / name).write_bytes(content)

        with (
            patch.object(eval_data, "BASE_DIR", root),
            patch.object(eval_agent, "BASE_DIR", root),
            patch.object(agent, "load_dotenv", side_effect=AssertionError("不得读取真实.env")),
            patch.object(agent, "request_model", side_effect=AssertionError("不得请求真实模型")),
        ):
            legacy = eval_data.load_eval_inputs()
            assert legacy == eval_data.load_eval_inputs("v1")
            assert legacy["suite_name"] == "v1" and legacy["split"] == "legacy"
            assert len(legacy["cases"]) == 10
            assert legacy["cases"][0]["id"] == "E01"
            assert len(legacy["dataset"]["tables"]["orders"]["rows"]) == 6
            development = eval_data.load_eval_inputs("v2-dev")
            untouched = deepcopy(development)
            assert development["suite_name"] == "v2-dev"
            assert development["split"] == "development"
            assert development["dataset_version"] == development["cases_version"] == 2
            assert development["dataset_file"] == "eval_fixture_v2.json"
            assert development["cases_file"] == "eval_cases_v2_dev.json"
            assert len(development["cases"]) == 20
            assert development["cases"][0]["id"] == "D01"
            assert len(development["dataset"]["tables"]["orders"]["rows"]) == 1000
            print("默认入口仍读取第一版10题；v2-dev成对选择1000订单快照与20道开发题")

            report = eval_agent.build_report(development, development["cases"])
            for name in ("suite_name", "split", "dataset_file", "cases_file", "dataset_version", "cases_version", "dataset_sha256", "cases_sha256"):
                assert report[name] == development[name]
            assert report["code_manifest"] == build_eval_manifest(root)
            assert report["max_rounds"] == 6 and report["max_seconds"] == 30.0
            print("报告记录题集名称、用途、输入文件、版本与指纹，原有代码清单和执行上限保留")

            for argv, count in (
                (["eval_agent.py", "--check-only"], 1),
                (["eval_agent.py", "--suite", "v2-dev", "--check-only"], 1),
                (["eval_agent.py", "--case", "all", "--check-only"], 10),
                (["eval_agent.py", "--suite", "v2-dev", "--case", "all", "--check-only"], 20),
                (["eval_agent.py", "--suite", "v2-dev", "--case", "D18", "--check-only"], 1),
            ):
                with patch.object(agent, "run_agent") as run_agent, patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as output:
                    eval_agent.main()
                run_agent.assert_not_called()
                assert f"固定题目数量： {count}" in output.getvalue()
            assert not (root / "eval_runs").exists()
            with patch.object(sys, "argv", ["eval_data.py", "--suite", "v2-dev"]), redirect_stdout(StringIO()) as output:
                eval_data.main()
            assert "orders 行数： 1000" in output.getvalue()
            print("默认首题、指定单题及全部题目的check-only均不启动Agent或创建批次")

            def reject_change(filename, mutate):
                path = root / filename
                value = json.loads(path.read_bytes())
                mutate(value)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                try:
                    eval_data.load_eval_inputs("v2-dev")
                except ValueError:
                    pass
                else:
                    raise AssertionError("错误输入未被拒绝")
                finally:
                    path.write_bytes(original_inputs[filename])

            reject_change("eval_cases_v2_dev.json", lambda value: value.update(dataset_sha256=legacy["dataset_sha256"]))
            reject_change("eval_cases_v2_dev.json", lambda value: value.update(split="holdout"))
            reject_change("eval_cases_v2_dev.json", lambda value: value.update(version=1))
            reject_change("eval_cases_v2_dev.json", lambda value: value.update(version=True))
            reject_change("eval_cases_v2_dev.json", lambda value: value["cases"][1].update(id=value["cases"][0]["id"]))
            reject_change("eval_fixture_v2.json", lambda value: value.update(dataset_sha256="0"*64))
            try:
                eval_data.load_eval_inputs("unknown")
            except ValueError:
                pass
            else:
                raise AssertionError("未知题集未被拒绝")
            print("错误数据绑定、用途、版本、重复编号、快照指纹及未知题集均被拒绝")

            for argv in (
                ["eval_agent.py", "--suite", "unknown"],
                ["eval_agent.py", "--suite", "v2-dev", "--case", "E01"],
            ):
                with patch.object(agent, "run_agent") as run_agent, patch.object(sys, "argv", argv), redirect_stderr(StringIO()):
                    try:
                        eval_agent.main()
                    except SystemExit as exc:
                        assert exc.code == 2
                    else:
                        raise AssertionError("错误命令仍开始采集")
                run_agent.assert_not_called()
            assert not (root / "eval_runs").exists()
            print("错误题集或跨题集题号在采集前被拒绝")

            calls = []
            selected = []

            def fake_run_agent(question, **kwargs):
                case = selected[len(calls)]
                assert question == case["question"]
                assert kwargs == {"max_rounds": 6, "max_seconds": 30.0}
                assert tools.DB_PATH.parent.parent == root / "eval_runs"
                assert tools.BASE_DIR == tools.DB_PATH.parent
                assert tools.OUTPUT_DIR == tools.DB_PATH.parent / "outputs"
                with closing(sqlite3.connect(tools.DB_PATH.as_uri()+"?mode=ro", uri=True)) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1000
                initial = json.loads((tools.BASE_DIR / "report.json").read_text(encoding="utf-8"))
                assert initial["suite_name"] == "v2-dev" and initial["split"] == "development"
                calls.append(question)
                return {"status": "completed", "answer": "离线预设回答。", "evidence_query_ids": [], "steps": [], "charts": []}

            for argv, ids in (
                (["eval_agent.py", "--suite", "v2-dev"], ["D01"]),
                (["eval_agent.py", "--suite", "v2-dev", "--case", "all"], [case["id"] for case in development["cases"]]),
            ):
                calls.clear()
                selected[:] = [case for case in development["cases"] if case["id"] in ids]
                before = set((root / "eval_runs").iterdir()) if (root / "eval_runs").exists() else set()
                with patch.object(agent, "run_agent", side_effect=fake_run_agent), patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                    eval_agent.main()
                created = set((root / "eval_runs").iterdir()) - before
                assert len(created) == 1
                finished = json.loads((created.pop() / "report.json").read_text(encoding="utf-8"))
                assert finished["finished_at"]
                assert finished["requested_cases"] == ids
                assert [row["id"] for row in finished["rows"]] == ids
                assert len(calls) == len(ids)
                assert all(row["review"]["passed"] is None for row in finished["rows"])
                assert finished["suite_name"] == "v2-dev" and finished["split"] == "development"
                assert finished["code_manifest"] == report["code_manifest"]
                assert (tools.DB_PATH, tools.BASE_DIR, tools.OUTPUT_DIR) == (old_db, old_base, old_output)
            print("预设采集使用独立1000订单数据库，默认首题为D01，全部模式保存20题")
            print("Agent仅收到问题与明确上限，标准答案和参考SQL不传给模型，也未自动判题")
            assert development == untouched
            assert all((root / name).read_bytes() == content for name, content in original_inputs.items())

    assert all((project / name).read_bytes() == content for name, content in original_inputs.items())
    assert (tools.DB_PATH, tools.BASE_DIR, tools.OUTPUT_DIR) == (old_db, old_base, old_output)
    print("第一版和第二版输入均保持不变，临时采集结束后工具路径恢复")
    print("评测题集入口检查通过（预设Agent与临时数据库，未请求真实API）")


if __name__ == "__main__":
    check()
