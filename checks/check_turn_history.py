"""预设模型与SQL验证历史传递；不调用真实模型或数据库。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import patch

import agent
from agent_state import create_initial_state
from graph_agent import build_agent_graph
from query_store import QueryStore


question_9 = "2026年9月已支付销售额是多少？"
question_10 = "那10月份呢？"
captured = []


def finish(status, answer, ids):
    return {
        "role": "assistant",
        "content": json.dumps({
            "status": status,
            "answer": answer,
            "evidence_query_ids": ids,
        }, ensure_ascii=False),
    }


def query(month):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_history",
            "type": "function",
            "function": {
                "name": "run_sql",
                "arguments": json.dumps({
                    "sql": "SELECT ? AS sales_yuan",
                    "params": [month],
                }),
            },
        }],
    }


def fake_sql(sql, params):
    return {
        "columns": ["sales_yuan"],
        "rows": [[{"2026-09": 1400.0, "2026-10": 1700.0}[params[0]]]],
        "truncated": False,
    }


def fake_model(messages):
    captured.append(deepcopy(messages))
    feedbacks = [message for message in messages if message["role"] == "tool"]
    if not feedbacks:
        current_question = messages[-1]["content"]
        if current_question == question_9:
            return query("2026-09")
        assert current_question == question_10
        assert messages[1]["content"] == question_9
        assert messages[2]["role"] == "assistant"
        assert "1400" in messages[2]["content"]
        return query("2026-10")

    feedback = json.loads(feedbacks[-1]["content"])
    assert feedback["ok"] is True, feedback
    data = feedback["data"]
    amount = data["rows"][0][0]
    return finish(
        "completed", f"已支付销售额为{amount:.2f}元。", [data["query_id"]]
    )


def invoke(state, model=fake_model):
    graph = build_agent_graph(model, agent.call_tool)
    with patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": fake_sql}):
        with redirect_stdout(StringIO()):
            return graph.invoke(
                state, config={"recursion_limit": 2 * state["max_rounds"] + 3}
            )


first = invoke(create_initial_state(question_9))
assert first["final_result"]["status"] == "completed"
assert first["history"] == [
    {"role": "user", "content": question_9},
    {"role": "assistant", "content": first["final_result"]["answer"]},
]
print("第一轮保存用户问题和经过检查的最终回答")

second_input = create_initial_state(question_10, history=first["history"])
assert [message["role"] for message in second_input["messages"]] == [
    "system", "user", "assistant", "user"
]
assert second_input["rounds"] == 0
assert second_input["steps"] == second_input["charts"] == []
assert second_input["query_results"] == {}
assert second_input["model_message"] is None
assert second_input["final_result"] is None
second = invoke(second_input)
assert second["final_result"]["status"] == "completed"
assert second["final_result"]["answer"] == "已支付销售额为1700.00元。"
assert [message["role"] for message in second["history"]] == [
    "user", "assistant", "user", "assistant"
]
assert len(second["history"]) == 4
assert all(set(message) == {"role", "content"} for message in second["history"])
assert not any(message["role"] == "tool" for message in second["history"])
assert second["rounds"] == 2 and len(second["steps"]) == 1
print("第二轮模型输入包含旧问答与新问题，历史只追加一次")
print("第二轮模型输入角色：", [m["role"] for m in captured[2]])

old_id = first["final_result"]["evidence_query_ids"][0]
new_id = second["final_result"]["evidence_query_ids"][0]
assert old_id != new_id
assert set(second["query_results"]) == {new_id}
try:
    QueryStore(second["query_results"]).get(old_id)
except ValueError:
    pass
else:
    raise AssertionError("上一轮查询编号不应作为本轮缓存使用")
print("新一轮计数与产物重置，旧查询编号无法用于本轮取数")

baseline = deepcopy(first["history"])
separate = create_initial_state("另一个问题", history=baseline)
separate["messages"][2]["content"] = "仅修改工作消息"
assert separate["history"] == baseline
separate["history"][1]["content"] = "仅修改新状态历史"
assert baseline == first["history"]
print("调用者历史、工作消息与新状态历史相互隔离")

calls = []


def repair_model(messages):
    calls.append(deepcopy(messages))
    if len(calls) == 1:
        return {"role": "assistant", "content": "未校验的临时回答"}
    return finish("needs_clarification", "请补充统计年份。", [])


clarification = invoke(
    create_initial_state("帮我统计销售额。"), model=repair_model
)
assert clarification["final_result"]["status"] == "needs_clarification"
assert clarification["history"] == [
    {"role": "user", "content": "帮我统计销售额。"},
    {"role": "assistant", "content": "请补充统计年份。"},
]
assert any(
    message["role"] == "user" and "程序校验" in message["content"]
    for message in calls[-1]
)
print("临时错误回答和程序修正反馈不会进入已完成问答历史")

failed = invoke(create_initial_state(question_9, max_rounds=1))
assert failed["final_result"]["status"] == "failed"
assert failed["history"] == [
    {"role": "user", "content": question_9},
    {"role": "assistant", "content": failed["final_result"]["answer"]},
]
assert failed["rounds"] == 1
print("预算耗尽也保存本轮问题与程序生成的失败说明")
print("业务问答历史检查通过")
