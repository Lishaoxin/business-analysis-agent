"""检查Token汇总和实际Agent结束结果；只用预设HTTP响应与工具。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json

from pydantic import ValidationError

from agent_state import create_initial_state
from check_model_usage import ABSENT, run_with_http, usage
from graph_agent import build_agent_graph
from usage_models import summarize_usage


def expected(call_count, usage_count, prompt=None, completion=None):
    complete = call_count == usage_count
    return {
        "model_call_count": call_count,
        "usage_call_count": usage_count,
        "complete": complete,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion if complete else None,
    }


def main():
    calls = [{"usage": usage(100, 20)}, {"usage": usage(180, 35)}]
    saved = deepcopy(calls)
    complete = summarize_usage(calls)
    assert complete == expected(2, 2, 280, 55)
    assert calls == saved
    complete["total_tokens"] = 999
    assert summarize_usage(calls) == expected(2, 2, 280, 55)
    print("两轮用量完整：输入280、输出55、合计335个Token")
    print("汇总不修改原记录，修改汇总结果不会污染后续计算")

    assert summarize_usage([calls[0], {"usage": None}]) == expected(2, 1)
    assert summarize_usage([{}, {"usage": None}]) == expected(2, 0)
    assert summarize_usage([{"usage": usage(0, 0)}]) == expected(1, 1, 0, 0)
    assert summarize_usage([]) == expected(0, 0, 0, 0)
    print("缺一轮用量时总数为None，全部未知也不冒充零用量")
    print("明确零用量正常合计；没有调用记录时调用数和总数均为0")

    try:
        summarize_usage([{"usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 999
        }}])
    except ValidationError:
        pass
    else:
        raise AssertionError("不合法计数参与了汇总")
    print("不合法用量被拒绝，不将错误计数累加到总数")

    result, _ = run_with_http([usage(100, 20), usage(180, 35)])
    assert result["status"] == "completed"
    assert result["usage_summary"] == expected(2, 2, 280, 55)
    assert len(result["model_calls"]) == 2
    assert result["steps"][0]["result"]["data"]["rows"] == [[4]]
    json.dumps(result, ensure_ascii=False)
    print("实际Agent结果附带汇总，逐轮记录、查询结果和证据保持可用")

    unknown, _ = run_with_http([ABSENT, usage(180, 35)])
    assert unknown["status"] == "completed"
    assert unknown["usage_summary"] == expected(2, 1)
    assert unknown["model_calls"][1]["usage"] == usage(180, 35)
    zero, _ = run_with_http([usage(0, 0), usage(0, 0)])
    assert zero["usage_summary"] == expected(2, 2, 0, 0)
    print("业务可以完成而用量不齐全，已知的逐轮计数仍保留")

    repaired, _ = run_with_http(
        [usage(100, 20), usage(180, 35), usage(200, 40)], repair=True
    )
    assert repaired["usage_summary"] == expected(3, 3, 480, 95)
    exhausted, _ = run_with_http([usage(100, 20)], max_rounds=1)
    assert exhausted["status"] == "failed"
    assert exhausted["usage_summary"] == expected(1, 1, 100, 20)
    print("纠错的全部调用都计入；任务失败也可以具有完整用量")

    for status in ["needs_clarification", "failed"]:
        initial = create_initial_state("离线后续问题", history=[
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮回答"},
        ])
        def model(messages, status=status):
            return {
                "message": {"role": "assistant", "content": json.dumps({
                    "status": status, "answer": "离线预设说明。",
                    "evidence_query_ids": [],
                }, ensure_ascii=False)},
                "usage": usage(30, 10),
            }
        graph = build_agent_graph(model, lambda *args, **kwargs: None)
        with redirect_stdout(StringIO()):
            state = graph.invoke(initial)
        assert initial["model_calls"] == []
        assert state["final_result"]["usage_summary"] == expected(1, 1, 30, 10)
        assert state["final_result"]["status"] == status
        assert len(state["history"]) == 4
        assert json.loads(json.dumps(state, ensure_ascii=False)) == state
    print("澄清和失败结果都有汇总，带历史的新问题只统计本轮调用")
    print("Token用量汇总检查通过（预设响应，未请求真实API）")


if __name__ == "__main__":
    main()
