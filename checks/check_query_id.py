"""只读 SQLite + 预设模型回复，验证 query_id 接入；不请求真实模型服务。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import patch

import agent
from query_store import QueryStore


arguments = {"sql": "SELECT ? AS order_status", "params": ["paid"]}
store = QueryStore()
feedback = agent.call_tool(
    "run_sql", json.dumps(arguments, ensure_ascii=False), query_store=store
)
assert feedback["ok"] is True, feedback
data = feedback["data"]
query_id = data["query_id"]
assert isinstance(query_id, str) and query_id.startswith("query_")
saved_result = store.get(query_id)
assert saved_result["columns"] == ["order_status"]
assert saved_result["rows"] == [["paid"]]
assert saved_result["truncated"] is False
print("带编号的工具反馈：", json.dumps(feedback, ensure_ascii=False))
print("取回查询结果：", saved_result)

# 反馈中的列表被外部修改时，存储里的原始查询结果仍保持不变。
data["rows"][0][0] = "changed"
assert store.get(query_id)["rows"] == [["paid"]]
print("修改工具反馈不会污染保存结果")

# SQL 失败时，不能返回一个声称成功保存的查询编号。
failed = agent.call_tool(
    "run_sql",
    json.dumps({"sql": "SELECT missing_column FROM orders", "params": []}),
    query_store=store,
)
assert failed["ok"] is False, failed
assert failed["error_type"] == "OperationalError", failed
assert "query_id" not in failed.get("data", {}), failed
assert store.get(query_id)["rows"] == [["paid"]]
print("失败查询返回错误反馈，不返回查询编号")

# 没传存储的旧诊断调用继续返回原有数据结构。
legacy = agent.call_tool("run_sql", json.dumps(arguments))
assert legacy["ok"] is True and legacy["data"]["rows"] == [["paid"]]
assert "query_id" not in legacy["data"]
print("旧的独立工具调用保持兼容")

# 实际运行图：两轮工具节点分别查询，缓存通过State衔接；不请求模型。
import graph_agent
from agent_state import create_initial_state


def fake_request_model(messages):
    tool_messages = [
        message for message in messages if message.get("role") == "tool"
    ]
    if len(tool_messages) == 2:
        query_ids = []
        for message in tool_messages:
            feedback = json.loads(message["content"])
            assert feedback["ok"] is True, feedback
            query_ids.append(feedback["data"]["query_id"])
        return {
            "role": "assistant",
            "content": json.dumps({
                "status": "completed",
                "answer": "测试完成",
                "evidence_query_ids": query_ids,
            }, ensure_ascii=False),
        }

    index = len(tool_messages)
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": f"call_query_{index}",
            "type": "function",
            "function": {
                "name": "run_sql",
                "arguments": json.dumps({
                    "sql": "SELECT ? AS value",
                    "params": [["first", "second"][index]],
                }),
            },
        }],
    }


graph = graph_agent.build_agent_graph(fake_request_model, agent.call_tool)
with redirect_stdout(StringIO()):
    first_state = graph.invoke(create_initial_state("第一个测试任务"))
    second_state = graph.invoke(create_initial_state("第二个测试任务"))

restored_stores = []
for state in [first_state, second_state]:
    run = state["final_result"]
    assert run["status"] == "completed", run
    assert len(run["steps"]) == 2
    assert [step["round"] for step in run["steps"]] == [1, 2]
    assert run["evidence_query_ids"] == [
        step["result"]["data"]["query_id"] for step in run["steps"]
    ]
    task_store = QueryStore(state["query_results"])
    restored_stores.append(task_store)
    for step, value in zip(run["steps"], ["first", "second"]):
        assert step["result"]["ok"] is True, step
        query_id = step["result"]["data"]["query_id"]
        assert task_store.get(query_id)["rows"] == [[value]]

assert first_state["query_results"] is not second_state["query_results"]
first_id = first_state["final_result"]["evidence_query_ids"][0]
try:
    restored_stores[1].get(first_id)
except ValueError:
    pass
else:
    raise AssertionError("不同任务不应共享查询结果")

print("同一任务两轮工具节点之间，查询结果与编号保留")
print("不同 Agent 任务的查询快照相互隔离")
print("query_id 接入检查通过")
