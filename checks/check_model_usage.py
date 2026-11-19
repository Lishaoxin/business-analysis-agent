"""预设HTTP响应，检查实际请求函数、图节点和每轮Token记录；不访问真实服务。"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from unittest.mock import Mock, patch

from pydantic import ValidationError
import requests

import agent
from graph_agent import run_graph_agent


ABSENT = object()


def run_with_http(usages, *, repair=False, max_rounds=6):
    captured = {"inputs": [], "payloads": []}

    def post(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        assert kwargs["timeout"] == (10, 60)
        assert kwargs["json"]["model"] == agent.MODEL_NAME
        assert kwargs["json"]["thinking"] == {"type": "disabled"}
        assert kwargs["json"]["tools"] == agent.TOOL_DEFINITIONS
        messages = deepcopy(kwargs["json"]["messages"])
        index = len(captured["inputs"])
        captured["inputs"].append(messages)
        if index == 0:
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_usage_sql", "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT 4 AS order_count", "params": []}',
                },
            }]}
        elif repair and index == 1:
            message = {"role": "assistant", "content": "离线预设不合格结束格式"}
        else:
            feedback = json.loads(next(
                item["content"] for item in reversed(messages) if item["role"] == "tool"
            ))
            message = {"role": "assistant", "content": json.dumps({
                "status": "completed", "answer": "离线预设结果：4笔订单。",
                "evidence_query_ids": [feedback["data"]["query_id"]],
            }, ensure_ascii=False)}
        payload = {"choices": [{"message": message}]}
        raw_usage = usages[index]
        if raw_usage is not ABSENT:
            payload["usage"] = deepcopy(raw_usage)
        captured["payloads"].append(payload)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def sql(sql, params):
        return {"columns": ["order_count"], "rows": [[4]], "truncated": False}

    # 不读取真实.env、环境Key，也不会执行真实HTTP或业务数据库查询。
    with (
        patch.object(agent, "load_dotenv", return_value=False) as dotenv,
        patch.object(agent.os, "getenv", return_value="offline-test-key"),
        patch.object(agent.requests, "post", side_effect=post) as http,
        patch.dict(agent.TOOL_FUNCTIONS, {"run_sql": sql}),
        redirect_stdout(StringIO()),
    ):
        result = agent.run_agent("离线Token接入检查", max_rounds=max_rounds)
    assert http.call_count == len(captured["inputs"])
    assert dotenv.call_count == http.call_count
    return result, captured


def usage(prompt, completion):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def main():
    first = {**usage(100, 20), "prompt_cache_hit_tokens": 64,
             "prompt_cache_miss_tokens": 36}
    second = usage(180, 35)
    result, captured = run_with_http([first, second])
    assert result["status"] == "completed"
    assert [item["round"] for item in result["model_calls"]] == [1, 2]
    assert [item["usage"] for item in result["model_calls"]] == [usage(100, 20), second]
    assert all(item["duration_ms"] >= 0 for item in result["model_calls"])
    assert result["steps"][0]["result"]["data"]["rows"] == [[4]]
    assert result["evidence_query_ids"] == [result["steps"][0]["result"]["data"]["query_id"]]
    print("实际request_model保留服务用量，两轮记录分别为120和215个Token")
    print("服务缓存扩展字段不重复计数，原工具查询、引用和耗时字段正常")

    captured["payloads"][0]["usage"]["prompt_tokens"] = 999
    assert result["model_calls"][0]["usage"] == usage(100, 20)
    print("修改原始响应不会污染已保存的规范化用量")
    for messages in captured["inputs"]:
        for message in messages:
            assert "usage" not in message and "model_calls" not in message
            assert "message" not in message
            if message["role"] == "tool":
                assert "usage" not in json.loads(message["content"])
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    print("消息与用量分别处理，用量不混入后续模型消息或工具反馈")

    unknown, _ = run_with_http([ABSENT, None])
    assert unknown["status"] == "completed"
    assert [item["usage"] for item in unknown["model_calls"]] == [None, None]
    zero, _ = run_with_http([usage(0, 0), usage(0, 0)])
    assert [item["usage"] for item in zero["model_calls"]] == [usage(0, 0), usage(0, 0)]
    print("缺失或null用量保持None，服务明确返回的零计数仍被保留")

    third = usage(200, 40)
    repaired, _ = run_with_http([first, second, third], repair=True)
    assert repaired["status"] == "completed"
    assert [item["usage"] for item in repaired["model_calls"]] == [usage(100, 20), second, third]
    exhausted, calls = run_with_http([first], max_rounds=1)
    assert exhausted["status"] == "failed"
    assert len(calls["inputs"]) == len(exhausted["model_calls"]) == 1
    assert exhausted["model_calls"][0]["usage"] == usage(100, 20)
    print("格式纠错保留全部已返回用量，达到轮数上限不虚增调用")

    def old_model(messages):
        return {"role": "assistant", "content": json.dumps({
            "status": "needs_clarification", "answer": "离线预设：请补充月份。",
            "evidence_query_ids": [],
        }, ensure_ascii=False)}
    with redirect_stdout(StringIO()):
        old = run_graph_agent("离线兼容检查", old_model, agent.call_tool)
    assert old["status"] == "needs_clarification"
    assert old["model_calls"][0]["usage"] is None
    print("原有只返回消息的模型函数仍可使用，用量记为未知")

    try:
        run_with_http([{"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 999}])
    except ValidationError as exc:
        assert exc.errors(include_url=False)[0]["type"] == "value_error"
    else:
        raise AssertionError("不合法的服务用量被误当有效计数")
    print("不合法用量继续触发校验错误，不伪造或修正服务计数")

    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("offline HTTP failure")
    with (
        patch.object(agent, "load_dotenv", return_value=False),
        patch.object(agent.os, "getenv", return_value="offline-test-key"),
        patch.object(agent.requests, "post", return_value=response),
    ):
        try:
            agent.request_model([{"role": "user", "content": "offline"}])
        except requests.HTTPError:
            pass
        else:
            raise AssertionError("HTTP错误被用量逻辑吞掉")
    response.json.assert_not_called()
    with (
        patch.object(agent, "load_dotenv", return_value=False),
        patch.object(agent.os, "getenv", return_value="offline-test-key"),
        patch.object(agent.requests, "post", side_effect=requests.Timeout("offline timeout")),
    ):
        try:
            agent.request_model([{"role": "user", "content": "offline"}])
        except requests.Timeout:
            pass
        else:
            raise AssertionError("模型请求超时被用量逻辑吞掉")
    print("HTTP错误与模型超时仍交给调用者处理")
    print("每轮Token用量接入检查通过（预设HTTP响应，未请求真实API）")


if __name__ == "__main__":
    main()
