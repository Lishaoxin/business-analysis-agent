"""实际执行图，用可控时钟验证任务预算；不调用真实模型或数据库。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import patch

import requests

from agent_state import create_initial_state
from check_tool_timing import Clock
import graph_agent
from run_budget import TaskBudget


def execute(*, model_delay=0.25, tool_delay=0.25, batch=False, repair=False,
            wait_before_start=0, wait_after_first_model=0,
            wait_before_validate=0, start_event_delay=0,
            result_event_delay=0, validation_delay=0, invalid_final_once=False):
    clock = Clock()
    budget = TaskBudget(max_seconds=2, clock=clock)
    clock.advance(wait_before_start)
    stats = {"models": 0, "tools": 0, "validations": 0}
    events = []

    def model(messages):
        stats["models"] += 1
        clock.advance(model_delay)
        feedbacks = [json.loads(item["content"]) for item in messages
                     if item["role"] == "tool"]
        if not feedbacks or (repair and feedbacks[-1]["ok"] is False):
            count = 2 if batch and not feedbacks else 1
            message = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_budget_{stats['models']}_{index}",
                 "type": "function", "function": {
                     "name": "run_sql",
                     "arguments": json.dumps({"sql": "SELECT ? AS value", "params": [index + 4]}),
                 }} for index in range(count)
            ]}
        else:
            content = {} if invalid_final_once and stats["models"] == 2 else {
                "status": "completed", "answer": "离线预设查询完成。",
                "evidence_query_ids": [item["data"]["query_id"] for item in feedbacks if item["ok"]],
            }
            message = {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}
        prompt = 100 * stats["models"]
        return {"message": message, "usage": {
            "prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10
        }}

    def tool(name, raw_arguments, query_store=None):
        stats["tools"] += 1
        clock.advance(tool_delay)
        if repair and stats["tools"] == 1:
            return {"ok": False, "error_type": "OperationalError", "error": "离线预设字段错误"}
        value = json.loads(raw_arguments)["params"][0]
        data = {"columns": ["value"], "rows": [[value]], "truncated": False}
        return {"ok": True, "data": {**data, "query_id": query_store.save(data)}}

    def observe(event):
        events.append(deepcopy(event))
        clock.advance(start_event_delay if event["event"] == "tool_start" else result_event_delay)

    real_validate = graph_agent.validate_conclusion
    def validate(*args, **kwargs):
        stats["validations"] += 1
        try:
            return real_validate(*args, **kwargs)
        finally:
            clock.advance(validation_delay)

    graph = graph_agent.build_agent_graph(model, tool, on_event=observe, budget=budget)
    visited = []
    state = None
    with (
        patch.object(graph_agent, "perf_counter", side_effect=clock),
        patch.object(graph_agent, "validate_conclusion", side_effect=validate),
        redirect_stdout(StringIO()),
    ):
        for mode, item in graph.stream(
            create_initial_state("离线预算检查"), stream_mode=["updates", "values"],
            config={"recursion_limit": 15},
        ):
            if mode == "values":
                state = item
            else:
                visited.extend(item)
                if "model" in item and stats["models"] == 1:
                    clock.advance(wait_after_first_model)
                if "model" in item and stats["models"] == 2:
                    clock.advance(wait_before_validate)
    assert state is not None
    assert json.loads(json.dumps(state, ensure_ascii=False)) == state
    assert "budget" not in state and "clock" not in state
    assert len(state["history"]) == 2, "本轮结束问答应只追加一次"
    return state, stats, events, visited


def failed(state):
    assert state["final_result"]["status"] == "failed"
    assert "时间预算" in state["final_result"]["answer"]


def main():
    state, stats, _, visited = execute()
    assert state["final_result"]["status"] == "completed"
    assert visited == ["model", "tools", "model", "validate"]
    assert stats == {"models": 2, "tools": 1, "validations": 1}
    assert state["final_result"]["usage_summary"]["total_tokens"] == 320
    print("预算内正常完成，原有查询、证据、耗时和用量汇总保持可用")

    state, stats, events, visited = execute(wait_before_start=2)
    failed(state)
    assert stats["models"] == stats["tools"] == 0
    assert state["model_calls"] == state["steps"] == events == []
    assert visited == ["model"]
    print("首次调用前预算已尽：不请求模型、不执行工具")

    state, stats, events, _ = execute(model_delay=2)
    failed(state)
    assert stats["models"] == 1 and stats["tools"] == 0
    assert len(state["model_calls"]) == 1 and events == []
    assert state["model_calls"][0]["usage"]["total_tokens"] == 110
    assert state["final_result"]["usage_summary"]["total_tokens"] == 110
    print("模型返回时超时：保留该次真实用量和耗时，不再执行其工具")

    state, stats, events, _ = execute(wait_after_first_model=1.75)
    failed(state)
    assert stats["models"] == 1 and stats["tools"] == 0
    assert events == [] and state["steps"] == []
    print("模型后到工具节点之间预算耗尽：工具不被启动")

    state, stats, events, visited = execute(tool_delay=1.75, batch=True)
    failed(state)
    assert stats["tools"] == stats["models"] == 1
    assert len(state["steps"]) == len(state["query_results"]) == 1
    assert state["steps"][0]["result"]["ok"] is True
    assert state["steps"][0]["duration_ms"] == 1750.0
    assert [event["event"] for event in events] == ["tool_start", "tool_result"]
    assert visited == ["model", "tools"]
    print("首个工具返回后超时：保留结果与查询快照，跳过同轮剩余工具并直接结束")

    state, stats, _, _ = execute(result_event_delay=1.5, batch=True)
    failed(state)
    assert stats["tools"] == 1 and len(state["steps"]) == 1
    print("发送工具结果期间预算耗尽：不继续启动下一工具")

    state, stats, events, _ = execute(start_event_delay=1.75)
    failed(state)
    assert stats["tools"] == 0 and state["query_results"] == {}
    step = state["steps"][0]
    assert step["skipped"] is True and step["duration_ms"] == 0.0
    assert step["result"]["error_type"] == "TaskBudgetExceededError"
    assert "未执行工具" in step["result"]["error"]
    assert [event["event"] for event in events] == ["tool_start", "tool_result"]
    print("开始进度发送后过期：工具未执行，明确标记skipped并返回预算反馈")

    state, stats, _, _ = execute(wait_before_validate=1.25)
    failed(state)
    assert stats["models"] == 2 and stats["validations"] == 0
    assert state["final_result"]["usage_summary"]["total_tokens"] == 320
    state, stats, _, _ = execute(validation_delay=1.25)
    failed(state)
    assert stats["validations"] == 1
    state, stats, _, _ = execute(validation_delay=1.25, invalid_final_once=True)
    failed(state)
    assert stats["models"] == 2
    print("结束校验前或校验中超时：均返回failed，不接受迟到结果或追加模型请求")

    state, stats, _, _ = execute(repair=True)
    assert state["final_result"]["status"] == "completed"
    assert stats["models"] == 3 and stats["tools"] == 2
    assert state["final_result"]["usage_summary"]["total_tokens"] == 630
    print("预算内的工具错误仍可修复，所有已返回调用均被保留")
    print("所有终态只追加一次历史；预算对象和时钟没有进入可序列化State")

    def must_not_call(*args, **kwargs):
        raise AssertionError("不应启动调用")
    try:
        graph_agent.build_agent_graph(must_not_call, must_not_call, budget=object())
    except ValueError:
        pass
    else:
        raise AssertionError("错误budget类型没有被拒绝")

    def timeout(messages):
        raise requests.Timeout("离线预设模型超时")
    graph = graph_agent.build_agent_graph(timeout, must_not_call, budget=TaskBudget())
    with redirect_stdout(StringIO()):
        try:
            graph.invoke(create_initial_state("离线异常检查"))
        except requests.Timeout:
            pass
        else:
            raise AssertionError("HTTP超时被预算逻辑吞掉")
    print("错误预算类型被拒绝，模型请求异常仍交给调用者处理")
    print("图执行时间预算检查通过（预设模型、工具与时钟，未请求真实API）")


if __name__ == "__main__":
    main()
