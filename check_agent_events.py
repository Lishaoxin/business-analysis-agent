"""用预设模型和工具，检查实际Agent执行时的进度事件；不请求外部服务。"""

from contextlib import closing, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
import requests

import agent
from graph_agent import build_agent_graph
import session_runner


def tool_request(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def make_model(trace, repair=False, expected_questions=None):
    round_index = 0

    def model(messages):
        nonlocal round_index
        round_index += 1
        trace.append(("model",))
        if round_index == 1:
            if expected_questions is not None:
                questions = [m["content"] for m in messages if m["role"] == "user"]
                assert questions == expected_questions, questions
            if repair:
                calls = [
                    tool_request("call_schema", "get_schema", {}),
                    tool_request("call_bad", "run_sql", {
                        "sql": "SELECT missing_column FROM orders", "params": []
                    }),
                ]
            else:
                calls = [tool_request("call_good", "run_sql", {
                    "sql": "SELECT COUNT(*) FROM orders", "params": []
                })]
            return {"role": "assistant", "content": "", "tool_calls": calls}
        if repair and round_index == 2:
            return {"role": "assistant", "content": "", "tool_calls": [
                tool_request("call_good", "run_sql", {
                    "sql": "SELECT COUNT(*) FROM orders", "params": []
                })
            ]}
        latest = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        assert latest["ok"] is True
        return {"role": "assistant", "content": json.dumps({
            "status": "completed",
            "answer": "离线预设结果：4笔订单。",
            "evidence_query_ids": [latest["data"]["query_id"]],
        }, ensure_ascii=False)}

    return model


def make_tool(trace):
    def tool(name, raw_arguments, query_store=None):
        trace.append(("execute", name))
        arguments = json.loads(raw_arguments)
        if name == "get_schema":
            return {"ok": True, "data": {"orders": "CREATE TABLE orders(order_id TEXT)"}}
        assert name == "run_sql"
        if "missing_column" in arguments["sql"]:
            return {"ok": False, "error_type": "OperationalError", "error": "no such column: missing_column"}
        data = {"columns": ["order_count"], "rows": [[4]], "truncated": False}
        return {"ok": True, "data": {**data, "query_id": query_store.save(data)}}

    return tool


def run_offline(question, trace, on_event=None, repair=False, thread_id=None, expected_questions=None):
    model = make_model(trace, repair=repair, expected_questions=expected_questions)
    with patch.object(agent, "request_model", side_effect=model):
        with patch.object(agent, "call_tool", side_effect=make_tool(trace)):
            with redirect_stdout(io.StringIO()):
                arguments = {"thread_id": thread_id}
                if on_event is not None:
                    arguments["on_event"] = on_event
                return agent.run_agent(question, **arguments)


def main():
    trace = []
    received = []

    def collect_event(event):
        received.append(deepcopy(event))
        trace.append((event["event"], event["data"]["tool_call_id"]))
        # 故意改观察方收到的数据，内部记录和模型反馈必须保持原值。
        event["data"]["tool_name"] = "changed_by_observer"
        data = event["data"].get("result", {}).get("data", {})
        if "rows" in data:
            data["rows"][0][0] = 999
            data["query_id"] = "query_fake"

    result = run_offline("离线检查", trace, collect_event, repair=True)
    assert trace == [
        ("model",),
        ("tool_start", "call_schema"), ("execute", "get_schema"), ("tool_result", "call_schema"),
        ("tool_start", "call_bad"), ("execute", "run_sql"), ("tool_result", "call_bad"),
        ("model",),
        ("tool_start", "call_good"), ("execute", "run_sql"), ("tool_result", "call_good"),
        ("model",),
    ], trace
    print("实际执行顺序：tool_start -> 执行工具 -> tool_result")
    print("同轮多个工具分别发送事件，tool_call_id前后匹配")

    results = [event for event in received if event["event"] == "tool_result"]
    assert [event["data"] for event in results] == result["steps"]
    assert results[1]["data"]["result"]["ok"] is False
    assert [event["event"] for event in received] == ["tool_start", "tool_result"] * 3
    assert result["status"] == "completed"
    assert result["steps"][-1]["result"]["data"]["rows"] == [[4]]
    assert result["evidence_query_ids"] != ["query_fake"]
    print("工具失败仍发送tool_result，模型修复后继续完成")
    print("修改收到的事件不会污染步骤、查询编号或模型反馈")

    plain = run_offline("旧方式调用", [])
    assert set(plain) == {"status", "answer", "evidence_query_ids", "steps", "charts"}
    assert plain["status"] == "completed"
    assert len(plain["steps"]) == 1
    print("不传on_event时，原有独立调用和返回字段保持可用")

    with tempfile.TemporaryDirectory(prefix="agent_event_check_") as temp_name:
        directory = Path(temp_name).resolve()
        assert directory.parent == Path(tempfile.gettempdir()).resolve()
        assert directory.name.startswith("agent_event_check_")
        db_path = directory / "sessions.db"
        first_events, second_events = [], []
        with patch.object(session_runner, "SESSION_DB_PATH", db_path):
            first = run_offline("第一问", [], first_events.append, thread_id="events-A")
            second = run_offline(
                "第二问", [], second_events.append, thread_id="events-A",
                expected_questions=["第一问", "第二问"],
            )
        assert [event["event"] for event in first_events] == ["tool_start", "tool_result"]
        assert [event["event"] for event in second_events] == ["tool_start", "tool_result"]
        assert first["thread_id"] == second["thread_id"] == "events-A"
        assert first["evidence_query_ids"] != second["evidence_query_ids"]

        with closing(sqlite3.connect(str(db_path), check_same_thread=False)) as conn:
            saver = SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
            graph = build_agent_graph(lambda messages: None, lambda *args: None, checkpointer=saver)
            saved = graph.get_state({"configurable": {"thread_id": "business:events-A"}}).values
        json.dumps(saved, ensure_ascii=False)
        assert "on_event" not in saved
        assert len(saved["history"]) == 4
        assert saved["rounds"] == 2
        assert list(saved["query_results"]) == second["evidence_query_ids"]
        print("会话入口也能收集事件，两次任务的接收列表相互隔离")
        print("历史和查询快照可恢复，回调函数没有进入State或检查点")

    with patch.object(agent, "request_model") as request:
        try:
            agent.run_agent("错误回调", on_event=123)
        except ValueError as exc:
            assert "on_event" in str(exc)
        else:
            raise AssertionError("无效on_event没有被拒绝")
        request.assert_not_called()
    print("错误的on_event类型在请求模型前被拒绝")

    received = []
    with patch.object(agent, "request_model", side_effect=requests.Timeout("offline timeout")):
        try:
            with redirect_stdout(io.StringIO()):
                agent.run_agent("异常检查", on_event=received.append)
        except requests.Timeout:
            pass
        else:
            raise AssertionError("模型请求异常被意外吞掉")
    assert received == []
    print("模型请求异常仍交给调用者处理，没有伪造工具结果或结束事件")
    print("Agent进度事件检查通过（预设模型和工具，未请求真实API）")


if __name__ == "__main__":
    main()
