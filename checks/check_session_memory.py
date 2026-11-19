"""真实临时SQLite与预设模型；不调用真实模型或业务数据库。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
from unittest.mock import patch

import requests

import agent
from checkpoint_demo import build_demo
from graph_agent import build_agent_graph
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
import session_runner


QUESTION_9 = "2026年9月已支付销售额是多少？"
QUESTION_10 = "那10月份呢？"


def finish(status, answer, ids):
    return {
        "role": "assistant",
        "content": json.dumps({
            "status": status, "answer": answer, "evidence_query_ids": ids,
        }, ensure_ascii=False),
    }


def fake_sql(sql, params):
    return {
        "columns": ["sales_yuan"],
        "rows": [[{"2026-09": 1400.0, "2026-10": 1700.0}[params[0]]]],
        "truncated": False,
    }


def fake_model(messages):
    feedbacks = [m for m in messages if m["role"] == "tool"]
    if feedbacks:
        feedback = json.loads(feedbacks[-1]["content"])
        assert feedback["ok"] is True, feedback
        data = feedback["data"]
        return finish(
            "completed",
            f"已支付销售额为{data['rows'][0][0]:.2f}元。",
            [data["query_id"]],
        )

    questions = [m["content"] for m in messages if m["role"] == "user"]
    current = questions[-1]
    if current == QUESTION_9:
        month = "2026-09"
    elif current == QUESTION_10 and QUESTION_9 in questions[:-1]:
        assert any(
            m["role"] == "assistant" and "1400" in m["content"]
            for m in messages
        )
        month = "2026-10"
    else:
        return finish("needs_clarification", "请先说明统计年份和指标。", [])

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_session",
            "type": "function",
            "function": {
                "name": "run_sql",
                "arguments": json.dumps({
                    "sql": "SELECT ? AS sales_yuan", "params": [month],
                }),
            },
        }],
    }


def run_local(db_path, thread_id, question, model=fake_model):
    with patch.object(session_runner, "SESSION_DB_PATH", db_path):
        with patch.object(agent, "request_model", side_effect=model):
            with patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql}):
                with redirect_stdout(StringIO()):
                    return agent.run_agent(question, thread_id=thread_id)


def worker(db_path, thread_id, question):
    captured = []

    def observed(messages):
        captured.append(deepcopy(messages))
        return fake_model(messages)

    result = run_local(db_path, thread_id, question, observed)
    print(json.dumps({
        "result": result,
        "first_roles": [m["role"] for m in captured[0]],
        "model_calls": len(captured),
    }, ensure_ascii=False))


def child(db_path, thread_id, question):
    env = os.environ.copy()
    # 脚本所在目录优先；项目目录提供工具等其余模块。
    env["PYTHONPATH"] = str(Path(agent.__file__).resolve().parent)
    completed = subprocess.run(
        [
            sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
            "--worker", "--db", str(db_path), "--thread", thread_id,
            "--question", question,
        ],
        capture_output=True, text=True, encoding="utf-8",
        env=env, check=True,
    )
    assert not completed.stderr, completed.stderr
    return json.loads(completed.stdout)


def saved_state(db_path, thread_id, demo=False):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        saver = SqliteSaver(
            conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
        )
        if demo:
            graph = build_demo(saver)
            key = thread_id
        else:
            graph = build_agent_graph(
                fake_model, agent.call_tool, checkpointer=saver
            )
            key = f"business:{thread_id}"
        return graph.get_state(
            {"configurable": {"thread_id": key}}
        ).values
    finally:
        conn.close()


def check():
    with tempfile.TemporaryDirectory(prefix="agent_session_check_") as temp_name:
        directory = Path(temp_name).resolve()
        assert directory.parent == Path(tempfile.gettempdir()).resolve()
        assert directory.name.startswith("agent_session_check_")
        db_path = directory / "sessions.db"

        # 同一个库里，教学图与业务图按内部编号前缀区分。
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            saver = SqliteSaver(
                conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
            )
            demo = build_demo(saver)
            demo.invoke(
                {"text": "教学演示", "history": []},
                {"configurable": {"thread_id": "session-A"}},
            )
        finally:
            conn.close()

        # 两次调用分别启动新Python进程，历史只能从文件恢复。
        first = child(db_path, "session-A", QUESTION_9)
        second = child(db_path, "session-A", QUESTION_10)
        assert first["result"]["answer"] == "已支付销售额为1400.00元。"
        assert second["result"]["answer"] == "已支付销售额为1700.00元。"
        assert first["result"]["thread_id"] == second["result"]["thread_id"] == "session-A"
        assert first["first_roles"] == ["system", "user"]
        assert second["first_roles"] == ["system", "user", "assistant", "user"]
        assert first["model_calls"] == second["model_calls"] == 2
        print("两个独立进程使用同一会话，旧问答已自动恢复")
        print("第二轮模型输入角色：", second["first_roles"])

        state_a = saved_state(db_path, "session-A")
        assert len(state_a["history"]) == 4
        assert state_a["rounds"] == 2 and len(state_a["steps"]) == 1
        old_id = first["result"]["evidence_query_ids"][0]
        new_id = second["result"]["evidence_query_ids"][0]
        assert old_id != new_id and set(state_a["query_results"]) == {new_id}
        assert state_a["final_result"]["answer"] == second["result"]["answer"]
        print("历史保留，但轮数、步骤、查询缓存和最终结果按新问题重建")

        other = child(db_path, "session-B", QUESTION_10)
        assert other["result"]["status"] == "needs_clarification"
        assert other["first_roles"] == ["system", "user"]
        assert saved_state(db_path, "session-B")["query_results"] == {}
        assert len(saved_state(db_path, "session-A")["history"]) == 4
        assert saved_state(db_path, "session-A", demo=True)["history"] == ["教学演示"]
        print("不同会话相互隔离，教学记录与业务记录也分别保存")

        # 模型服务异常不写入已结束问答，连接与会话占用标记均释放。
        def offline_timeout(messages):
            raise requests.Timeout("离线模拟超时")

        try:
            run_local(db_path, "session-A", "用于模拟超时的输入", offline_timeout)
        except requests.Timeout:
            pass
        else:
            raise AssertionError("模型服务超时应抛给调用者")
        assert len(saved_state(db_path, "session-A")["history"]) == 4
        retried = run_local(db_path, "session-A", QUESTION_9)
        assert retried["status"] == "completed"
        assert len(saved_state(db_path, "session-A")["history"]) == 6
        assert all(
            m["content"] != "用于模拟超时的输入"
            for m in saved_state(db_path, "session-A")["history"]
        )
        print("异常后旧问答未受污染，同会话可以继续开始新问题")

        # 同一进程中，同会话重叠请求被拒绝。
        started = Event()
        release = Event()

        def blocking_model(messages):
            started.set()
            assert release.wait(10), "等待并发检查释放超时"
            return finish("needs_clarification", "请补充分析条件。", [])

        with patch.object(session_runner, "SESSION_DB_PATH", db_path):
            with patch.object(agent, "request_model", side_effect=blocking_model):
                with redirect_stdout(StringIO()):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            agent.run_agent, "并发检查", thread_id="busy-A"
                        )
                        try:
                            assert started.wait(10), "首个请求未进入模型"
                            try:
                                agent.run_agent("重叠请求", thread_id="busy-A")
                            except session_runner.SessionBusyError:
                                pass
                            else:
                                raise AssertionError("同会话重叠请求应被拒绝")
                        finally:
                            release.set()
                        assert future.result(timeout=10)["status"] == "needs_clarification"
        # 已结束时占用标记释放。
        assert run_local(db_path, "busy-A", QUESTION_9)["status"] == "completed"
        print("同会话重叠请求被拒绝，结束后可以再次使用")

        # 不传编号仍为独立调用，不创建会话库。
        standalone_path = directory / "standalone.db"
        standalone = run_local(standalone_path, None, QUESTION_9)
        assert standalone["status"] == "completed"
        assert "thread_id" not in standalone
        assert not standalone_path.exists()
        print("不传thread_id时保持独立调用，不创建会话数据库")

        for invalid in ["", "   ", 123]:
            try:
                run_local(db_path, invalid, QUESTION_9)
            except ValueError:
                continue
            raise AssertionError("错误会话编号应被拒绝")
        print("错误会话编号已拦截")

    print("业务会话持久化检查通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--thread")
    parser.add_argument("--question")
    args = parser.parse_args()
    if args.worker:
        worker(args.db, args.thread, args.question)
    else:
        check()
