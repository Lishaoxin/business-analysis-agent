"""离线核对分析追踪、耗时和日志；所有时间推进与Agent返回均为预设。"""

import asyncio
import json
from threading import Event
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
import requests

from check_sse_api import parse_sse
import main as api
from run_trace import RunTrace
from session_runner import SessionBusyError
import stream_runner
from stream_runner import AgentEventStream


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def result(thread_id):
    return {
        "thread_id": thread_id,
        "status": "completed",
        "answer": "不写入追踪日志的回答",
        "evidence_query_ids": ["query_example"],
        "steps": [],
        "charts": [],
        # 接口必须使用程序分配的真实追踪元数据覆盖这些示例值。
        "trace_id": "forged", "elapsed_ms": 99999,
    }


def records(captured, trace_id):
    return [value for record in captured.records
            if (value := json.loads(record.getMessage())).get("trace_id") == trace_id]


def check_trace_id(value):
    assert value.startswith("trace_")
    assert UUID(value.removeprefix("trace_")).version == 4


def check_json(captured):
    clock = Clock()
    calls = []

    def fake_run(question, *, thread_id):
        calls.append((question, thread_id))
        clock.advance(0.125)
        return result(thread_id)

    with patch.object(api, "RunTrace", side_effect=lambda: RunTrace(clock=clock)):
        with patch.object(api, "run_agent", side_effect=fake_run):
            with TestClient(api.app) as client:
                first = client.post("/analyze", json={"question": "第一问", "thread_id": "same-A"})
                second = client.post("/analyze", json={"question": "第二问", "thread_id": "same-A"})
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    check_trace_id(a["trace_id"])
    check_trace_id(b["trace_id"])
    assert a["trace_id"] != b["trace_id"]
    assert a["thread_id"] == b["thread_id"] == "same-A"
    assert calls == [("第一问", "same-A"), ("第二问", "same-A")]
    for response in [first, second]:
        data = response.json()
        assert response.headers["x-trace-id"] == data["trace_id"]
        assert data["elapsed_ms"] == 125.0
        logs = records(captured, data["trace_id"])
        assert [log["event"] for log in logs] == ["analysis_start", "analysis_end"]
        assert logs[0]["elapsed_ms"] == 0.0
        assert logs[-1]["elapsed_ms"] == data["elapsed_ms"]
    print("JSON响应、X-Trace-ID与日志使用同一编号，模拟125毫秒计算正确")
    print("同一thread_id的连续两问生成不同trace_id")


def check_sse(captured):
    clock = Clock()

    def fake_run(question, thread_id=None, on_event=None):
        info = {"round": 1, "tool_name": "run_sql", "tool_call_id": "call_example", "raw_arguments": "不写入日志的SQL参数", "trace_id": "forged"}
        clock.advance(0.05)
        on_event({"event": "tool_start", "data": info})
        clock.advance(0.075)
        on_event({"event": "tool_result", "data": {**info, "result": {"ok": True, "data": {"rows": [[4]]}}}})
        clock.advance(0.025)
        return result(thread_id)

    with patch.object(stream_runner, "RunTrace", side_effect=lambda: RunTrace(clock=clock)):
        with patch.object(api, "run_agent", side_effect=fake_run):
            with TestClient(api.app) as client:
                response = client.post("/analyze/stream", json={"question": "不写入日志的问题", "thread_id": "stream-A"})
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [event["event"] for event in events] == ["tool_start", "tool_result", "final"]
    trace_id = response.headers["x-trace-id"]
    check_trace_id(trace_id)
    assert all(event["data"]["trace_id"] == trace_id for event in events)
    assert [event["data"]["elapsed_ms"] for event in events] == [50.0, 125.0, 150.0]
    logs = records(captured, trace_id)
    assert [log["event"] for log in logs] == ["analysis_start", "tool_start", "tool_result", "analysis_end"]
    assert logs[-1]["elapsed_ms"] == events[-1]["data"]["elapsed_ms"]
    assert logs[1]["tool_call_id"] == logs[2]["tool_call_id"] == "call_example"
    print("SSE响应头、各工具事件、final和日志共享同一个trace_id")
    print("SSE模拟累计耗时50/125/150毫秒，最终事件与结束日志一致")


def check_errors_and_validation(captured):
    with TestClient(api.app) as client:
        for path in ["/analyze", "/analyze/stream"]:
            for exception, expected in [
                (SessionBusyError("正在处理另一个问题"), 409),
                (requests.Timeout("offline timeout"), 504),
                (requests.ConnectionError("offline connection"), 502),
                (RuntimeError("offline runtime error"), 500),
            ]:
                with patch.object(api, "run_agent", side_effect=exception):
                    response = client.post(path, json={"question": "异常检查", "thread_id": "error-A"})
                trace_id = response.headers["x-trace-id"]
                check_trace_id(trace_id)
                if path.endswith("/stream"):
                    assert response.status_code == 200
                    events = parse_sse(response.text)
                    assert len(events) == 1 and events[0]["event"] == "error"
                    assert events[0]["data"]["trace_id"] == trace_id
                    assert events[0]["data"]["status_code"] == expected
                    assert events[0]["data"]["elapsed_ms"] >= 0
                else:
                    assert response.status_code == expected
                    assert isinstance(response.json()["detail"], str)
                logs = records(captured, trace_id)
                assert [log["event"] for log in logs] == ["analysis_start", "analysis_error"]
                assert logs[-1]["status_code"] == expected and logs[-1]["elapsed_ms"] >= 0
    print("JSON异常响应头和SSE error均能关联错误日志，原有错误码语义保留")

    before = len(captured.records)
    with TestClient(api.app) as client:
        with patch.object(api, "run_agent") as run:
            for path in ["/analyze", "/analyze/stream"]:
                assert client.post(path, json={"question": "   "}).status_code == 400
                assert client.post(path, json={}).status_code == 422
                assert client.post(path, json={"question": "检查", "trace_id": "client-value"}).status_code == 422
            run.assert_not_called()
    assert len(captured.records) == before
    print("输入校验失败不启动分析；trace_id不能由请求参数指定")


async def check_disconnect(captured):
    clock = Clock(10.0)
    trace = RunTrace(clock=clock)
    release, waiting = Event(), Event()

    def fake_run(question, thread_id=None, on_event=None):
        clock.value = 10.050
        on_event({"event": "tool_start", "data": {"tool_name": "run_sql", "tool_call_id": "call_close", "round": 1}})
        waiting.set()
        assert release.wait(3)
        clock.value = 10.750
        on_event({"event": "tool_result", "data": {"tool_name": "run_sql", "tool_call_id": "call_close", "round": 1, "result": {"ok": True}}})
        return result(thread_id)

    stream = AgentEventStream(fake_run, "断线检查", "close-A", queue_size=1, trace=trace)
    iterator = stream.events()
    try:
        first = await asyncio.wait_for(anext(iterator), 2)
        assert first["data"]["elapsed_ms"] == 50.0
        assert await asyncio.to_thread(waiting.wait, 2)
        await iterator.aclose()
        clock.value = 10.500
        assert trace.snapshot()["elapsed_ms"] == 500.0, "关闭连接不应提前冻结后台耗时"
    finally:
        release.set()
        stream.close()
        await iterator.aclose()
        if stream.worker is not None:
            await asyncio.to_thread(stream.worker.join, 3)
            assert not stream.worker.is_alive()
    assert trace.snapshot()["elapsed_ms"] == 750.0
    clock.value = 11.0
    assert trace.finish()["elapsed_ms"] == 750.0
    logs = records(captured, trace.trace_id)
    assert [log["event"] for log in logs] == ["analysis_start", "tool_start", "tool_result", "analysis_end"]
    assert logs[-1]["elapsed_ms"] == 750.0
    print("断线后后台仍记录完成日志，耗时在实际结束时结算并保持固定")


if __name__ == "__main__":
    with TestCase().assertLogs("uvicorn.error", level="INFO") as captured:
        check_json(captured)
        check_sse(captured)
        check_errors_and_validation(captured)
        asyncio.run(check_disconnect(captured))
    for item in captured.records:
        value = json.loads(item.getMessage())
        assert not ({"question", "sql", "raw_arguments", "rows", "answer"} & value.keys())
    print("追踪日志仅包含执行摘要，未混入问题、SQL参数、结果行或回答")
    print("接口追踪与耗时检查通过（预设Agent和时钟，未请求真实API）")
