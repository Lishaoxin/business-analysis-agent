"""用临时项目验证统一数据路径、实际文件读写和跨进程会话恢复。"""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import patch


def worker(mode):
    import app_paths as paths
    import tools

    code = Path(__file__).resolve().parent
    if mode == "missing":
        assert not paths.DB_PATH.exists()
        try:
            tools.get_schema()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("没有业务库时不应成功读取表结构")
        assert not paths.DB_PATH.exists()
        return

    import agent
    import main as api
    import session_runner
    from fastapi.testclient import TestClient

    assert tools.DB_PATH == paths.DB_PATH
    assert tools.OUTPUT_DIR == api.OUTPUT_DIR == paths.OUTPUT_DIR
    assert session_runner.SESSION_DB_PATH == paths.SESSION_DB_PATH
    assert api.BASE_DIR == code
    with TestClient(api.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.content == (code / "index.html").read_bytes()

        if mode == "default":
            assert paths.DATA_DIR == code
            assert not paths.DB_PATH.exists()
            assert not paths.SESSION_DB_PATH.exists()
            return

        assert paths.DATA_DIR != code
        assert tools.run_sql("SELECT COUNT(*) FROM orders WHERE status = ?", ["paid"])["rows"] == [[4]]
        assert set(tools.get_schema()) == {"orders", "products", "order_items"}

        model_inputs = []
        def fake_model(messages):
            if messages[-1]["role"] != "tool":
                model_inputs.append([item["role"] for item in messages])
                return {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "path_call", "type": "function", "function": {
                        "name": "run_sql",
                        "arguments": json.dumps({"sql": "SELECT COUNT(*) AS paid_orders FROM orders WHERE status = ?", "params": ["paid"]}),
                    }}],
                }
            feedback = json.loads(messages[-1]["content"])
            assert feedback["ok"] is True
            assert feedback["data"]["rows"] == [[4]]
            return {"role": "assistant", "content": json.dumps({
                "status": "completed", "answer": "已支付订单4笔。",
                "evidence_query_ids": [feedback["data"]["query_id"]],
            }, ensure_ascii=False)}

        with (
            patch.object(agent, "request_model", side_effect=fake_model),
            patch.object(agent, "load_dotenv", side_effect=AssertionError("不得读取真实.env")),
            patch.object(agent.requests, "post", side_effect=AssertionError("不得请求模型API")),
            redirect_stdout(StringIO()),
        ):
            response = client.post("/analyze", json={"question": mode, "thread_id": "data-path-session"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert paths.SESSION_DB_PATH.is_file()
        expected = ["system", "user"] if mode == "first" else ["system", "user", "assistant", "user"]
        assert model_inputs == [expected], model_inputs

        if mode == "first":
            result = tools.run_sql("SELECT p.category, SUM(i.quantity * i.unit_price_cents)/100.0 AS sales_yuan FROM orders o JOIN order_items i ON i.order_id=o.order_id JOIN products p ON p.product_id=i.product_id WHERE o.status='paid' GROUP BY p.category ORDER BY sales_yuan DESC")
            chart = Path(tools.draw_chart(result, title="自定义数据目录图表")["chart_path"])
            assert chart.parent == paths.OUTPUT_DIR and chart.is_file()
            image = client.get("/outputs/" + chart.name)
            assert image.status_code == 200 and image.content == chart.read_bytes()
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
            explicit = paths.DATA_DIR.parent / "explicit-session" / "nested" / "sessions.db"
            def clarify(messages):
                return {"role": "assistant", "content": json.dumps({"status": "needs_clarification", "answer": "请补充月份。", "evidence_query_ids": []}, ensure_ascii=False)}
            with redirect_stdout(StringIO()):
                result = session_runner.run_session_agent("需要月份", "explicit", clarify, agent.call_tool, db_path=explicit)
            assert result["status"] == "needs_clarification" and explicit.is_file()
        else:
            images = list(paths.OUTPUT_DIR.glob("*.png"))
            assert len(images) == 1
            assert client.get("/outputs/" + images[0].name).content == images[0].read_bytes()


def main():
    source = Path(__file__).resolve().parent
    with TemporaryDirectory(prefix="agent_app_paths_check_") as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(gettempdir()).resolve()
        assert root.name.startswith("agent_app_paths_check_")
        code = root / "project"
        code.mkdir()
        for path in source.glob("*.py"):
            shutil.copy2(path, code / path.name)
        shutil.copy2(source / "index.html", code / "index.html")
        excluded = {"DEEPSEEK_API_KEY", "AGENT_DATA_DIR", "PYTHONPATH", "PYTHONOPTIMIZE"}
        env = {name: os.environ[name] for name in os.environ if name not in excluded}
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", MPLCONFIGDIR=str(root / "mplconfig"))

        def run(arguments, environment):
            result = subprocess.run(
                [sys.executable, "-X", "utf8", *arguments],
                cwd=code, env=environment, capture_output=True,
                text=True, encoding="utf-8", timeout=30,
            )
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)

        def run_worker(mode, environment):
            run([str(code / "check_app_paths.py"), "--worker", mode], environment)

        run_worker("default", env)
        print("默认路径兼容原目录，首页从代码目录读取")
        data = root / "nested" / "runtime"
        custom = {**env, "AGENT_DATA_DIR": str(data)}
        run_worker("missing", custom)
        assert not data.exists()
        print("只读查询在业务库不存在时明确失败，不创建空数据库")
        run([str(code / "init_db.py")], custom)
        run([str(code / "seed_data.py")], custom)
        run_worker("first", custom)
        print("初始化、实际只读查询、会话与绘图使用同一自定义数据目录")
        print("图片由网页静态路由读取，显式会话库路径及缺失父目录仍可使用")
        run_worker("second", custom)
        print("新进程恢复同会话问答，已有图表仍可读取")
        assert not (code / "business.db").exists()
        assert not (code / "sessions.db").exists()
        assert list((code / "outputs").iterdir()) == []
        print("自定义数据没有混入源码目录，全部检查使用临时项目与预设模型")
    print("统一数据路径接入检查通过（未请求真实API）")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    else:
        main()
