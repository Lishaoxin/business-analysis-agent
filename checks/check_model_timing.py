"""检查每轮模型耗时、纠错与预算语义；不请求真实API。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import patch

import requests

from agent_state import create_initial_state
import graph_agent
from check_tool_timing import Clock


def execute(*, history=None, repair=False, max_rounds=6, terminal="completed"):
    clock = Clock()
    model_index = 0
    events = []
    seen_inputs = []

    def model(messages):
        nonlocal model_index
        model_index += 1
        seen_inputs.append(deepcopy(messages))
        clock.advance([0.1, 0.25, 0.05][model_index - 1])
        if model_index == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_model_timing", "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT 4 AS order_count", "params": []}',
                },
            }]}
        if repair and model_index == 2:
            return {"role": "assistant", "content": "离线预设不合格格式"}
        feedback = json.loads(next(
            item["content"] for item in reversed(messages) if item["role"] == "tool"
        ))
        return {"role": "assistant", "content": json.dumps({
            "status": terminal,
            "answer": "离线预设结束信息。",
            "evidence_query_ids": [feedback["data"]["query_id"]],
        }, ensure_ascii=False)}

    def tool(name, raw_arguments, query_store=None):
        clock.advance(0.04)
        data = {"columns": ["order_count"], "rows": [[4]], "truncated": False}
        return {"ok": True, "data": {**data, "query_id": query_store.save(data)}}

    def observe(event):
        clock.advance(2.0)
        events.append(deepcopy(event))

    initial = create_initial_state(
        "离线模型计时检查", max_rounds=max_rounds, history=history
    )
    graph = graph_agent.build_agent_graph(model, tool, on_event=observe)
    with patch.object(graph_agent, "perf_counter", side_effect=clock):
        with redirect_stdout(StringIO()):
            final = graph.invoke(initial, {"recursion_limit": 2 * max_rounds + 3})
    return initial, final, seen_inputs, events, model_index


def assert_durations(state, durations):
    expected = [{"round": index + 1, "duration_ms": value, "usage": None}
                for index, value in enumerate(durations)]
    assert state["model_calls"] == expected
    assert state["final_result"]["model_calls"] == expected
    assert state["rounds"] == len(expected)


def main():
    initial, first, inputs, events, count = execute()
    assert initial["model_calls"] == [], "节点不应直接修改输入列表"
    assert count == 2
    assert_durations(first, [100.0, 250.0])
    assert first["steps"][0]["duration_ms"] == 40.0
    assert first["final_result"]["status"] == "completed"
    print("两轮模型分别记录100/250毫秒，工具40毫秒独立记录")
    print("进度回调等待未混入模型或工具执行耗时")

    _, repaired, _, _, count = execute(repair=True)
    assert count == 3
    assert_durations(repaired, [100.0, 250.0, 50.0])
    assert repaired["final_result"]["status"] == "completed"
    assert len(repaired["history"]) == 2
    assert repaired["history"][-1]["content"] == "离线预设结束信息。"
    print("结束格式错误后修正，三次模型调用的耗时均保留")

    _, exhausted, _, _, count = execute(max_rounds=1)
    assert count == 1
    assert_durations(exhausted, [100.0])
    assert exhausted["final_result"]["status"] == "failed"
    assert len(exhausted["steps"]) == 1
    _, last_allowed, _, _, count = execute(max_rounds=2)
    assert count == 2
    assert_durations(last_allowed, [100.0, 250.0])
    assert last_allowed["final_result"]["status"] == "completed"
    print("预算耗尽不伪造额外模型记录，最后允许的一轮仍能正常完成")

    initial, second, second_inputs, _, _ = execute(history=first["history"])
    assert initial["model_calls"] == []
    assert_durations(second, [100.0, 250.0])
    assert len(second["history"]) == 4
    assert [item["role"] for item in second_inputs[0]] == ["system", "user", "assistant", "user"]
    assert first["final_result"]["evidence_query_ids"] != second["final_result"]["evidence_query_ids"]
    print("带历史的新问题保留问答，模型耗时列表从第1轮重新记录")

    for state in [first, repaired, exhausted, second]:
        assert json.loads(json.dumps(state, ensure_ascii=False)) == state
        assert all(set(item) == {"role", "content"} for item in state["history"])
    for messages in inputs:
        for message in messages:
            assert "model_calls" not in message
            if message["role"] == "tool":
                assert "duration_ms" not in json.loads(message["content"])
    assert all(set(item) == {"columns", "rows", "truncated"}
               for item in first["query_results"].values())
    assert [event["event"] for event in events] == ["tool_start", "tool_result"]
    print("State可序列化，模型消息、问答历史、查询证据与工具事件协议保持兼容")

    for status in ["needs_clarification", "failed"]:
        graph = graph_agent.build_agent_graph(
            lambda messages, status=status: {
                "role": "assistant", "content": json.dumps({
                    "status": status, "answer": "离线预设说明。", "evidence_query_ids": []
                }, ensure_ascii=False)
            },
            lambda *args, **kwargs: None,
        )
        clock = Clock()
        with patch.object(graph_agent, "perf_counter", side_effect=clock):
            with redirect_stdout(StringIO()):
                final = graph.invoke(create_initial_state("离线状态检查"))
        assert_durations(final, [0.0])
        assert final["final_result"]["status"] == status
        assert final["final_result"]["steps"] == []
    print("需要澄清和任务失败也返回实际调用记录，零耗时有效")

    received = []
    def timeout(messages):
        raise requests.Timeout("离线模拟模型超时")
    graph = graph_agent.build_agent_graph(
        timeout, lambda *args, **kwargs: None, on_event=received.append
    )
    with redirect_stdout(StringIO()):
        try:
            graph.invoke(create_initial_state("离线超时检查"))
        except requests.Timeout:
            pass
        else:
            raise AssertionError("模型超时被计时逻辑意外吞掉")
    assert received == []
    print("模型请求超时继续交给调用者处理，不伪造正常返回或完成事件")
    print("模型调用耗时检查通过（预设模型、工具与时钟，未请求真实API）")


if __name__ == "__main__":
    main()
