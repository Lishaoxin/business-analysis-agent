"""模拟模型和工具，检查 run_agent 结束流程；不调用模型服务、不访问数据库。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import sqlite3
from unittest.mock import patch

import agent


def finish(status, answer, ids):
    return {
        "role": "assistant",
        "content": json.dumps({
            "status": status,
            "answer": answer,
            "evidence_query_ids": ids,
        }, ensure_ascii=False),
    }


def tool(name, arguments, call_id):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }],
    }


def query(sql="SELECT 4 AS order_count", call_id="call_query"):
    return tool("run_sql", {"sql": sql, "params": []}, call_id)


def query_ids(messages):
    ids = []
    for message in messages:
        if message["role"] == "tool":
            feedback = json.loads(message["content"])
            if feedback["ok"] and "query_id" in feedback["data"]:
                ids.append(feedback["data"]["query_id"])
    return ids


def fake_sql(sql, params):
    if "missing_column" in sql:
        raise sqlite3.OperationalError("no such column: missing_column")
    return {
        "columns": ["order_count"],
        "rows": [] if "empty_result" in sql else [[4]],
        "truncated": "truncated_result" in sql,
    }


def fake_draw(result, title):
    assert result["rows"] == [[4]]
    return {"chart_path": "offline_chart.png", "row_count": 1}


def run(model, requires_query=True, max_rounds=6):
    histories = []

    def request(messages):
        histories.append(deepcopy(messages))
        return model(messages, len(histories))

    with patch.object(agent, "request_model", side_effect=request):
        with patch.dict(agent.TOOL_FUNCTIONS, {
            "run_sql": fake_sql,
            "get_schema": lambda: {"orders": "CREATE TABLE orders (order_id TEXT)"},
            "draw_chart": fake_draw,
        }):
            with redirect_stdout(StringIO()):
                result = agent.run_agent(
                    "离线测试问题",
                    max_rounds=max_rounds,
                    requires_query=requires_query,
                )
    return result, histories


def has_feedback(histories, text):
    return any(
        message["role"] == "user" and text in message["content"]
        for messages in histories
        for message in messages
    )


# 1. 正常查询后，结束信息必须引用本次工具返回的编号。
result, histories = run(
    lambda messages, turn: (
        query() if turn == 1
        else finish("completed", "订单共有4笔。", query_ids(messages))
    )
)
assert result["status"] == "completed"
assert len(histories) == 2 and len(result["steps"]) == 1
assert result["evidence_query_ids"] == [
    result["steps"][0]["result"]["data"]["query_id"]
]
assert result["charts"] == []
first_task_id = result["evidence_query_ids"][0]
print("正常查询与有效结束信息：通过")

# 2. 普通文本不能绕过结束格式检查。
result, histories = run(
    lambda messages, turn: (
        {"role": "assistant", "content": "直接输出一段普通回答"}
        if turn == 1 else finish("needs_clarification", "请补充月份。", [])
    )
)
assert result["status"] == "needs_clarification"
assert len(histories) == 2 and has_feedback(histories, "程序校验")
print("普通文本被拒绝并回喂，随后可以澄清：通过")

# 3. 假编号被拒绝，模型随后可以查询并修复。
result, histories = run(
    lambda messages, turn: (
        finish("completed", "订单共有4笔。", ["query_fake"])
        if turn == 1 else query() if turn == 2
        else finish("completed", "订单共有4笔。", query_ids(messages))
    )
)
assert result["status"] == "completed" and len(histories) == 3
assert result["evidence_query_ids"] != ["query_fake"]
assert has_feedback(histories, "当前任务中没有这个查询结果")
print("假编号被拒绝并通过新查询修复：通过")

# 4. 声称完成但没有证据时，必须修复后才能返回 completed。
result, histories = run(
    lambda messages, turn: (
        finish("completed", "订单共有4笔。", [])
        if turn == 1 else query() if turn == 2
        else finish("completed", "订单共有4笔。", query_ids(messages))
    )
)
assert result["status"] == "completed" and len(histories) == 3
assert has_feedback(histories, "必须引用至少一个成功查询编号")
print("缺证据被拒绝并修复：通过")

# 5. 工具失败可恢复，不能因为中间一次失败就否定整个任务。
result, histories = run(
    lambda messages, turn: (
        query("SELECT missing_column FROM orders", "call_bad")
        if turn == 1 else query(call_id="call_repair") if turn == 2
        else finish("completed", "订单共有4笔。", query_ids(messages))
    )
)
assert result["status"] == "completed" and len(result["steps"]) == 2
assert result["steps"][0]["result"]["ok"] is False
assert result["steps"][1]["result"]["ok"] is True
assert len(result["evidence_query_ids"]) == 1
print("查询失败后修复，不引用失败结果：通过")

# 6/7. 澄清与失败均可无查询依据地返回。
for status, answer in [
    ("needs_clarification", "请补充月份。"),
    ("failed", "当前无法完成查询。"),
]:
    result, histories = run(
        lambda messages, turn: finish(status, answer, [])
    )
    assert result["status"] == status and len(histories) == 1
    assert result["steps"] == [] and result["evidence_query_ids"] == []
    print(f"{status}允许无查询证据：通过")

# 8. 成功查询的空结果有效，和查询错误不同。
result, histories = run(
    lambda messages, turn: (
        query("SELECT empty_result AS order_count")
        if turn == 1
        else finish("completed", "没有符合条件的记录。", query_ids(messages))
    )
)
assert result["status"] == "completed"
assert result["steps"][0]["result"]["data"]["rows"] == []
print("成功查询但无匹配记录：通过")

# 9. 调用者明确声明非查询任务，才可完成时不提供 SQL 编号。
result, histories = run(
    lambda messages, turn: (
        tool("get_schema", {}, "call_schema") if turn == 1
        else finish("completed", "orders含order_id字段。", [])
    ),
    requires_query=False,
)
assert result["status"] == "completed"
assert [step["tool_name"] for step in result["steps"]] == ["get_schema"]
assert result["evidence_query_ids"] == []
print("程序明确允许非查询任务：通过")

# 10. 被截断的引用被拒绝；改查完整聚合结果后才可完成。
result, histories = run(
    lambda messages, turn: (
        query("SELECT truncated_result AS order_count", "call_truncated")
        if turn == 1
        else finish("completed", "订单共有4笔。", query_ids(messages))
        if turn == 2
        else query(call_id="call_aggregate") if turn == 3
        else finish("completed", "订单共有4笔。", query_ids(messages)[-1:])
    )
)
assert result["status"] == "completed" and len(histories) == 4
assert has_feedback(histories, "查询结果被截断")
assert result["evidence_query_ids"] == [
    result["steps"][-1]["result"]["data"]["query_id"]
]
print("截断证据被拒绝并修复：通过")

# 11. 修复仍使用同一个轮数预算，耗尽后保留记录并返回 failed。
result, histories = run(
    lambda messages, turn: (
        query() if turn == 1
        else finish("completed", "没有提供依据。", [])
    ),
    max_rounds=3,
)
assert result["status"] == "failed" and len(histories) == 3
assert len(result["steps"]) == 1 and result["evidence_query_ids"] == []
assert "3轮" in result["answer"]
print("用尽轮数返回failed并保留工具记录：通过")

# 12. 上一次任务的编号不能用于新任务。
result, histories = run(
    lambda messages, turn: (
        finish("completed", "借用上次的结果。", [first_task_id])
        if turn == 1 else finish("failed", "本任务没有该查询结果。", [])
    )
)
assert result["status"] == "failed" and len(histories) == 2
assert has_feedback(histories, "当前任务中没有这个查询结果")
assert result["steps"] == []
print("跨任务引用被拒绝：通过")

# 13. 原有图表列表仍然返回，绘图使用同一个查询编号。
result, histories = run(
    lambda messages, turn: (
        query() if turn == 1
        else tool("draw_chart", {
            "query_id": query_ids(messages)[0],
            "title": "离线示例",
        }, "call_chart") if turn == 2
        else finish("completed", "示例图已生成。", query_ids(messages)[:1])
    )
)
assert result["status"] == "completed" and len(result["steps"]) == 2
assert result["charts"] == ["offline_chart.png"]
assert result["steps"][1]["result"]["data"]["query_id"] == (
    result["evidence_query_ids"][0]
)
print("原有查询与图表字段保持可用：通过")

print("13 项 Agent 结束流程检查通过")
