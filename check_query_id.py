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

# 用预设回复替换 request_model，实际执行 run_agent 的上下文传递。
created_stores = []


def create_store():
    instance = QueryStore()
    created_stores.append(instance)
    return instance


def fake_request_model(messages):
    if any(message.get("role") == "tool" for message in messages):
        return {"role": "assistant", "content": "测试完成"}

    tool_calls = []
    for index, value in enumerate(["first", "second"]):
        tool_calls.append({
            "id": f"call_query_{index}",
            "type": "function",
            "function": {
                "name": "run_sql",
                "arguments": json.dumps({
                    "sql": "SELECT ? AS value",
                    "params": [value],
                }),
            },
        })
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


with patch.object(agent, "QueryStore", side_effect=create_store):
    with patch.object(agent, "request_model", side_effect=fake_request_model):
        with redirect_stdout(StringIO()):
            first_run = agent.run_agent("第一个测试任务")
            second_run = agent.run_agent("第二个测试任务")

assert len(created_stores) == 2
assert created_stores[0] is not created_stores[1]

first_ids = []
for run, task_store in zip([first_run, second_run], created_stores):
    assert len(run["steps"]) == 2
    for step, value in zip(run["steps"], ["first", "second"]):
        assert step["result"]["ok"] is True, step
        task_query_id = step["result"]["data"]["query_id"]
        assert task_store.get(task_query_id)["rows"] == [[value]]
        if run is first_run:
            first_ids.append(task_query_id)

try:
    created_stores[1].get(first_ids[0])
except ValueError:
    pass
else:
    raise AssertionError("不同 Agent 任务不应共享查询结果")

print("同一 Agent 任务的两次查询共用一个存储")
print("不同 Agent 任务的查询结果相互隔离")
print("query_id 接入检查通过")
