"""用可控时钟检查真实图中的工具计时，不请求模型服务或数据库。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import patch

from agent_state import create_initial_state
import graph_agent


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run_scenario(durations, *, first_fails=False, collect=True):
    clock = Clock()
    received = []
    model_inputs = []
    calls = 0
    tool_index = 0

    def model(messages):
        nonlocal calls
        calls += 1
        model_inputs.append(deepcopy(messages))
        # 模拟模型等待，不能混入某次工具的执行时间。
        clock.advance(3.0)
        if calls == 1:
            return {
                "role": "assistant", "content": "", "tool_calls": [
                    {
                        "id": f"call_timing_{index}",
                        "type": "function",
                        "function": {
                            "name": "run_sql",
                            "arguments": json.dumps({
                                "sql": "SELECT ? AS value", "params": [index]
                            }),
                        },
                    }
                    for index in range(len(durations))
                ],
            }
        feedbacks = [json.loads(item["content"]) for item in messages
                     if item["role"] == "tool"]
        evidence = [item["data"]["query_id"] for item in feedbacks if item["ok"]]
        return {
            "role": "assistant",
            "content": json.dumps({
                "status": "completed",
                "answer": "离线预设结果：查询完成。",
                "evidence_query_ids": evidence,
            }, ensure_ascii=False),
        }

    def tool(name, raw_arguments, query_store=None):
        nonlocal tool_index
        index = tool_index
        tool_index += 1
        assert name == "run_sql"
        clock.advance(durations[index])
        if first_fails and index == 0:
            return {"ok": False, "error_type": "OperationalError",
                    "error": "离线预设字段错误"}
        data = {"columns": ["value"], "rows": [[index]], "truncated": False}
        return {"ok": True, "data": {**data, "query_id": query_store.save(data)}}

    def observe(event):
        # 模拟进度接收端等待2秒，计时不能把这段时间算进工具。
        clock.advance(2.0)
        received.append(deepcopy(event))
        if event["event"] == "tool_result":
            event["data"]["duration_ms"] = 999999.0

    graph = graph_agent.build_agent_graph(
        model, tool, on_event=observe if collect else None
    )
    with patch.object(graph_agent, "perf_counter", side_effect=clock):
        with redirect_stdout(StringIO()):
            state = graph.invoke(create_initial_state("离线工具计时检查"))
    assert calls == 2
    assert state["final_result"]["status"] == "completed"
    return state, received, model_inputs


def main():
    state, events, inputs = run_scenario([0.025, 0.075], first_fails=True)
    steps = state["final_result"]["steps"]
    assert [step["duration_ms"] for step in steps] == [25.0, 75.0]
    assert steps[0]["result"]["ok"] is False
    assert steps[1]["result"]["ok"] is True
    print("同轮两次工具分别记录25/75毫秒，失败反馈也保留耗时")
    print("模型等待与进度回调等待未计入工具执行耗时")

    assert [event["event"] for event in events] == ["tool_start", "tool_result"] * 2
    starts = [event["data"] for event in events if event["event"] == "tool_start"]
    results = [event["data"] for event in events if event["event"] == "tool_result"]
    assert all("duration_ms" not in start for start in starts)
    assert results == steps
    assert [item["tool_call_id"] for item in starts] == [item["tool_call_id"] for item in results]
    print("开始事件不伪造耗时，结果事件与最终工具记录的耗时一致")
    assert all(step["duration_ms"] != 999999.0 for step in steps)
    print("修改收到的耗时事件不会污染内部工具记录")

    for messages in inputs:
        for message in messages:
            if message["role"] == "tool":
                assert "duration_ms" not in json.loads(message["content"])
    assert all("duration_ms" not in result for result in state["query_results"].values())
    assert json.loads(json.dumps(state, ensure_ascii=False)) == state
    assert state["final_result"]["evidence_query_ids"] == list(state["query_results"])
    print("耗时进入可序列化步骤，查询结果、证据编号和模型工具反馈保持原约定")

    plain, no_events, _ = run_scenario([0.0055], collect=False)
    assert no_events == []
    assert plain["steps"][0]["duration_ms"] == 5.5
    zero, _, _ = run_scenario([0.0], collect=False)
    assert zero["steps"][0]["duration_ms"] == 0.0
    print("不传进度回调仍记录耗时，零耗时不被误判为缺失")

    clock = Clock()
    received = []

    def exceptional_model(messages):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_unexpected", "type": "function",
            "function": {"name": "run_sql", "arguments": "{}"},
        }]}

    def exceptional_tool(*args, **kwargs):
        clock.advance(0.05)
        raise RuntimeError("离线预设未处理异常")

    graph = graph_agent.build_agent_graph(
        exceptional_model, exceptional_tool, on_event=received.append
    )
    with patch.object(graph_agent, "perf_counter", side_effect=clock):
        with redirect_stdout(StringIO()):
            try:
                graph.invoke(create_initial_state("离线异常检查"))
            except RuntimeError as exc:
                assert str(exc) == "离线预设未处理异常"
            else:
                raise AssertionError("未处理的工具异常被计时逻辑吞掉")
    assert [event["event"] for event in received] == ["tool_start"]
    print("未处理的异常继续向外抛出，不伪造工具返回或完成事件")
    print("工具执行耗时检查通过（预设工具与时钟，未请求真实API）")


if __name__ == "__main__":
    main()
