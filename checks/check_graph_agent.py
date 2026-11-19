"""模拟模型和工具；实际运行LangGraph，不请求模型服务或数据库。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import patch

import requests

import agent
from agent_state import create_initial_state
from graph_agent import build_agent_graph, run_graph_agent


def fake_sql(sql, params):
    return {
        "columns": ["value"],
        "rows": [[params[0] if params else 4]],
        "truncated": False,
    }


def make_model(batch=False):
    stats = {"calls": 0}

    def model(messages):
        stats["calls"] += 1
        tool_messages = [
            message for message in messages if message["role"] == "tool"
        ]
        if not tool_messages:
            values = [4, 5] if batch else [4]
            calls = [{
                "index": index,
                "id": f"call_graph_{index}",
                "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": json.dumps({
                        "sql": "SELECT ? AS value", "params": [value],
                    }),
                },
            } for index, value in enumerate(values)]
            return {"role": "assistant", "content": "", "tool_calls": calls}

        assistant = next(
            message for message in reversed(messages)
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        assert all("index" not in call for call in assistant["tool_calls"])
        assert {call["id"] for call in assistant["tool_calls"]} == {
            message["tool_call_id"] for message in tool_messages
        }
        ids = [
            json.loads(message["content"])["data"]["query_id"]
            for message in tool_messages
        ]
        return {
            "role": "assistant",
            "content": json.dumps({
                "status": "completed",
                "answer": "离线查询已完成。",
                "evidence_query_ids": ids,
            }, ensure_ascii=False),
        }

    return model, stats


def trace(model, max_rounds=6):
    graph = build_agent_graph(model, agent.call_tool)
    state = create_initial_state("离线图检查", max_rounds=max_rounds)
    with patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql}):
        with redirect_stdout(StringIO()):
            events = list(graph.stream(
                state,
                stream_mode=["updates", "values"],
                config={"recursion_limit": 2 * max_rounds + 3},
            ))
    visited = [
        name for mode, event in events if mode == "updates"
        for name in event
    ]
    final_state = [
        event for mode, event in events if mode == "values"
    ][-1]
    return visited, final_state


model, stats = make_model()
visited, state = trace(model)
assert visited == ["model", "tools", "model", "validate"], visited
assert stats["calls"] == state["rounds"] == 2
assert state["final_result"]["status"] == "completed"
assert state["final_result"]["evidence_query_ids"] == list(state["query_results"])
assert json.loads(json.dumps(state, ensure_ascii=False)) == state
print("真实图执行路径：", " -> ".join(visited))
print("查询快照和结束依据已进入State，状态可序列化")

model, stats = make_model(batch=True)
visited, state = trace(model)
assert len(state["steps"]) == len(state["query_results"]) == 2
assert len(state["final_result"]["evidence_query_ids"]) == 2
assert [step["round"] for step in state["steps"]] == [1, 1]
print("同轮多个工具调用均完成，协议编号与查询结果均保留")

model, stats = make_model()
visited, state = trace(model, max_rounds=1)
assert visited == ["model", "tools", "model"]
assert stats["calls"] == state["rounds"] == 1
assert state["final_result"]["status"] == "failed"
assert len(state["final_result"]["steps"]) == 1
print("预算耗尽后未额外调用模型，并保留工具记录")

model, stats = make_model()
visited, state = trace(model, max_rounds=2)
assert stats["calls"] == state["rounds"] == 2
assert state["final_result"]["status"] == "completed"
print("最后一次允许的模型调用仍可正常完成校验")

for limit in [0, -1, True, "2"]:
    model, stats = make_model()
    try:
        run_graph_agent("离线检查", model, agent.call_tool, max_rounds=limit)
    except ValueError:
        assert stats["calls"] == 0
    else:
        raise AssertionError(f"无效模型调用上限应被拒绝：{limit!r}")
print("无效模型调用上限在请求模型前被拒绝")


def offline_timeout(messages):
    raise requests.Timeout("离线模拟模型超时")


try:
    run_graph_agent("离线检查", offline_timeout, agent.call_tool)
except requests.Timeout:
    print("模型请求异常继续交给调用者处理")
else:
    raise AssertionError("模型超时应继续抛给接口或CLI调用者")

print("LangGraph业务流程检查通过")
