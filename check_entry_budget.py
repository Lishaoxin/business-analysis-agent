"""检查独立、会话和HTTP入口的预算接线；不调用真实模型。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
import requests

import agent
from check_sse_api import parse_sse
from check_tool_timing import Clock
import graph_agent
import main as api
from run_budget import TaskBudget
import session_runner


DEFAULT = object()


def fake_model(clock, delay=0.25, seen=None):
    def model(messages):
        if seen is not None:
            seen.append(deepcopy(messages))
        clock.advance(delay)
        feedbacks = [item for item in messages if item["role"] == "tool"]
        if not feedbacks:
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_entry_budget", "type": "function",
                "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 4 AS order_count", "params": []}'},
            }]}
            prompt = 100
        else:
            feedback = json.loads(feedbacks[-1]["content"])
            message = {"role": "assistant", "content": json.dumps({
                "status": "completed", "answer": "离线预设结果：4笔。",
                "evidence_query_ids": [feedback["data"]["query_id"]],
            }, ensure_ascii=False)}
            prompt = 200
        return {"message": message, "usage": {
            "prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10,
        }}
    return model


def fake_sql(clock):
    def sql(sql, params):
        clock.advance(0.25)
        return {"columns": ["order_count"], "rows": [[4]], "truncated": False}
    return sql


def run(clock, question, *, thread_id=None, max_seconds=DEFAULT, delay=0.25, seen=None):
    kwargs = {"thread_id": thread_id}
    if max_seconds is not DEFAULT:
        kwargs["max_seconds"] = max_seconds
    with (
        patch.object(agent, "request_model", side_effect=fake_model(clock, delay, seen)),
        patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql(clock)}),
        redirect_stdout(StringIO()),
    ):
        return agent.run_agent(question, **kwargs)


def load_state(db_path, thread_id):
    with closing(sqlite3.connect(str(db_path), check_same_thread=False)) as conn:
        saver = SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        graph = graph_agent.build_agent_graph(lambda messages: None, lambda *args: None, checkpointer=saver)
        return graph.get_state({"configurable": {"thread_id": "business:" + thread_id}}).values


def check():
    clock = Clock()
    created = []
    class ControlledBudget(TaskBudget):
        def __init__(self, max_seconds=30.0):
            super().__init__(max_seconds=max_seconds, clock=clock)
            created.append(self)

    with tempfile.TemporaryDirectory(prefix="agent_entry_budget_check_") as name:
        directory = Path(name).resolve()
        assert directory.parent == Path(tempfile.gettempdir()).resolve()
        assert directory.name.startswith("agent_entry_budget_check_")
        db_path = directory / "sessions.db"
        with (
            patch.object(graph_agent, "TaskBudget", ControlledBudget),
            patch.object(session_runner, "TaskBudget", ControlledBudget),
            patch.object(session_runner, "SESSION_DB_PATH", db_path),
        ):
            result = run(clock, "独立默认预算")
            assert result["status"] == "completed"
            assert len(created) == 1 and created[-1].max_seconds == 30
            assert not db_path.exists()
            late = run(clock, "独立短预算", max_seconds=2, delay=2)
            assert late["status"] == "failed" and "时间预算" in late["answer"]
            assert len(created) == 2 and created[-1].max_seconds == 2
            assert len(late["model_calls"]) == 1 and late["steps"] == []
            assert late["usage_summary"]["total_tokens"] == 110
            print("独立调用默认30秒，可指定短预算；每次只创建一个对象且不创建会话库")

            with patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql(clock)}), redirect_stdout(StringIO()):
                direct = graph_agent.run_graph_agent(
                    "直接图入口", fake_model(clock), agent.call_tool, max_seconds=2
                )
            assert direct["status"] == "completed" and len(created) == 3
            first_budget_index = len(created)
            first = run(clock, "第一问", thread_id="budget-A", max_seconds=2, delay=2)
            seen = []
            second = run(clock, "第二问", thread_id="budget-A", max_seconds=2, seen=seen)
            assert first["status"] == "failed" and second["status"] == "completed"
            assert len(created) == first_budget_index + 2
            assert created[-1] is not created[-2]
            assert created[-2].remaining_seconds() == 0 and created[-1].remaining_seconds() > 0
            assert [item["role"] for item in seen[0]] == ["system", "user", "assistant", "user"]
            assert second["usage_summary"]["model_call_count"] == 2
            assert second["usage_summary"]["total_tokens"] == 320
            state = load_state(db_path, "budget-A")
            assert len(state["history"]) == 4
            assert "budget" not in state and "max_seconds" not in state and "clock" not in state
            assert json.loads(json.dumps(state, ensure_ascii=False)) == state
            print("同一会话首问超预算后，下一问获得新预算并保留已结束历史")
            print("耗时和Token只统计本轮，预算对象及起点没有进入SQLite检查点")

            other_seen = []
            other = run(clock, "其他会话", thread_id="budget-B", seen=other_seen)
            assert other["status"] == "completed" and created[-1].max_seconds == 30
            assert [item["role"] for item in other_seen[0]] == ["system", "user"]
            with patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql(clock)}), redirect_stdout(StringIO()):
                direct_session = session_runner.run_session_agent(
                    "直接会话入口", "direct-session", fake_model(clock), agent.call_tool,
                    db_path=db_path, max_seconds=2,
                )
            assert direct_session["status"] == "completed"
            print("不同会话相互隔离，直接图入口与直接会话入口也有预算")

            real_initial = session_runner.create_initial_state
            def slow_initial(*args, **kwargs):
                state = real_initial(*args, **kwargs)
                clock.advance(2)
                return state
            with patch.object(session_runner, "create_initial_state", side_effect=slow_initial):
                early = run(clock, "初始化耗时", thread_id="initialization", max_seconds=2)
            assert early["status"] == "failed" and early["model_calls"] == []
            print("会话初始化耗时计入预算，首个模型调用前过期也能正常结束")

            invalid_db = directory / "invalid.db"
            with patch.object(session_runner, "SESSION_DB_PATH", invalid_db):
                for thread_id in [None, "invalid"]:
                    for invalid in [0, -1, True, "2", None, float("inf"), float("nan")]:
                        with patch.object(agent, "request_model") as model:
                            try:
                                agent.run_agent("错误预算", thread_id=thread_id, max_seconds=invalid)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError("无效预算应被拒绝")
                            model.assert_not_called()
                assert not invalid_db.exists()
            print("错误时间上限在请求模型和创建会话库之前被拒绝")

            with patch.object(agent, "request_model", side_effect=requests.Timeout("offline timeout")):
                with redirect_stdout(StringIO()):
                    try:
                        agent.run_agent("网络异常", thread_id="retry-A", max_seconds=2)
                    except requests.Timeout:
                        pass
                    else:
                        raise AssertionError("网络异常被预算逻辑吞掉")
            retried = run(clock, "异常后新问题", thread_id="retry-A", max_seconds=2)
            assert retried["status"] == "completed"
            assert len(load_state(db_path, "retry-A")["history"]) == 2
            print("网络异常继续外抛，释放会话后可以用新预算重试")

            started, release = Event(), Event()
            def blocking_model(messages):
                clock.advance(2)
                started.set()
                assert release.wait(10)
                return {"message": {"role": "assistant", "content": json.dumps({
                    "status": "needs_clarification", "answer": "离线预设说明。", "evidence_query_ids": [],
                }, ensure_ascii=False)}, "usage": None}
            with patch.object(agent, "request_model", side_effect=blocking_model), redirect_stdout(StringIO()):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(agent.run_agent, "阻塞模型", thread_id="busy", max_seconds=2)
                    try:
                        assert started.wait(10)
                        try:
                            agent.run_agent("重叠请求", thread_id="busy", max_seconds=2)
                        except session_runner.SessionBusyError:
                            pass
                        else:
                            raise AssertionError("预算过期不能提前释放仍在运行的会话")
                    finally:
                        release.set()
                    assert future.result(timeout=10)["status"] == "failed"
            assert run(clock, "结束后再用", thread_id="busy", max_seconds=2)["status"] == "completed"
            print("预算已过但请求未返回时仍占用会话，实际结束后才能再次使用")

            assert api.run_agent is agent.run_agent
            streams = []
            real_stream = api.AgentEventStream
            def make_stream(*args, **kwargs):
                stream = real_stream(*args, **kwargs)
                streams.append(stream)
                return stream
            before = len(created)
            with (
                patch.object(agent, "request_model", side_effect=fake_model(clock, delay=30)),
                patch.object(api, "AgentEventStream", side_effect=make_stream),
                redirect_stdout(StringIO()),
                TestClient(api.app) as client,
            ):
                response = client.post("/analyze", json={"question": "JSON预算", "thread_id": "api-json"})
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "failed" and "时间预算" in data["answer"]
                assert data["usage_summary"]["total_tokens"] == 110
                response = client.post("/analyze/stream", json={"question": "SSE预算", "thread_id": "api-sse"})
                assert response.status_code == 200
                events = parse_sse(response.text)
                assert [event["event"] for event in events] == ["final"]
                assert events[0]["data"]["status"] == "failed"
                assert events[0]["data"]["usage_summary"]["total_tokens"] == 110
                assert len(created) == before + 2
                assert all(item.max_seconds == 30 for item in created[before:])
                before_invalid = len(created)
                response = client.post("/analyze", json={"question": "不开放客户端上限", "max_seconds": 1})
                assert response.status_code == 422 and len(created) == before_invalid
            for stream in streams:
                stream.worker.join(timeout=10)
                assert not stream.worker.is_alive()
            print("JSON与SSE入口默认启用30秒预算，超限分别返回业务failed和final")
            print("HTTP请求字段保持原约定；客户端不能通过额外字段改写时间上限")
            assert not session_runner._active_sessions
    print("任务入口时间预算检查通过（预设模型与时钟，未请求真实API）")


if __name__ == "__main__":
    check()
