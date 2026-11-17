"""检查队列、SSE响应和断线清理；使用预设模型/工具，不请求真实API。"""

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
import requests

import agent
import main as api
import session_runner
from session_runner import SessionBusyError
from stream_runner import AgentEventStream


def conclusion(thread_id, status="completed"):
    return {
        "thread_id": thread_id,
        "status": status,
        "answer": "离线预设回答。",
        "evidence_query_ids": ["query_example"] if status == "completed" else [],
        "steps": [],
        "charts": [],
    }


def tool_info():
    return {"round": 1, "tool_call_id": "call_example", "tool_name": "run_sql", "raw_arguments": "{}"}


async def collect_stream(stream):
    try:
        return [event async for event in stream.events()]
    finally:
        stream.close()
        if stream.worker is not None:
            await asyncio.to_thread(stream.worker.join, 3)
            assert not stream.worker.is_alive(), "后台线程未结束"


async def check_incremental_and_close():
    release, returned = Event(), Event()
    calls = []

    def fake_run(question, thread_id=None, on_event=None):
        calls.append(question)
        on_event({"event": "tool_start", "data": tool_info()})
        assert release.wait(3), "未收到测试放行信号"
        on_event({"event": "tool_result", "data": {**tool_info(), "result": {"ok": False, "error": "可修复错误"}}})
        on_event({"event": "tool_start", "data": {**tool_info(), "tool_call_id": "call_fixed"}})
        on_event({"event": "tool_result", "data": {**tool_info(), "tool_call_id": "call_fixed", "result": {"ok": True, "data": {}}}})
        returned.set()
        return conclusion(thread_id)

    stream = AgentEventStream(fake_run, "增量检查", "incremental-A", queue_size=1)
    iterator = stream.events()
    try:
        first = await asyncio.wait_for(anext(iterator), timeout=2)
        assert first["event"] == "tool_start"
        assert not returned.is_set(), "首个进度不应等任务全部完成后才出现"
        release.set()
        rest = await asyncio.wait_for(_read_rest(iterator), timeout=3)
        events = [first] + rest
        assert [e["event"] for e in events] == ["tool_start", "tool_result", "tool_start", "tool_result", "final"]
        assert events[1]["data"]["result"]["ok"] is False
        assert returned.is_set()
        assert events[-1]["data"]["status"] == "completed"
        assert len(calls) == 1
    finally:
        release.set()
        await iterator.aclose()
        await asyncio.to_thread(stream.worker.join, 3)
        assert not stream.worker.is_alive()
    print("任务未结束时已能收到进度；工具失败可以继续，最终只发一次final")

    second_queued, finished = Event(), Event()

    def many_events(question, thread_id=None, on_event=None):
        for index in range(100):
            on_event({"event": "tool_start", "data": {"index": index}})
            if index == 1:
                second_queued.set()
        finished.set()
        return conclusion(thread_id)

    stream = AgentEventStream(many_events, "队列满时断开", "close-A", queue_size=1)
    iterator = stream.events()
    try:
        await asyncio.wait_for(anext(iterator), 2)
        assert await asyncio.to_thread(second_queued.wait, 2)
    finally:
        await iterator.aclose()
        stream.close()
        await asyncio.to_thread(stream.worker.join, 3)
    assert finished.is_set() and not stream.worker.is_alive()
    print("有界队列满时关闭消费者，后台不会永久卡在发送进度上")

    for status in ["needs_clarification", "failed"]:
        events = await collect_stream(AgentEventStream(
            lambda question, thread_id=None, on_event=None: conclusion(thread_id, status),
            "状态检查", "status-A",
        ))
        assert len(events) == 1 and events[0]["event"] == "final"
        assert events[0]["data"]["status"] == status
    print("needs_clarification和failed是正式结果，均通过final传递")


async def _read_rest(iterator):
    return [event async for event in iterator]


async def check_disconnect_keeps_session_busy(directory):
    release, waiting = Event(), Event()
    model_round = 0

    def model(messages):
        nonlocal model_round
        model_round += 1
        if model_round == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_schema", "type": "function",
                "function": {"name": "get_schema", "arguments": "{}"},
            }]}
        waiting.set()
        assert release.wait(3)
        return {"role": "assistant", "content": json.dumps({
            "status": "needs_clarification", "answer": "请补充月份。", "evidence_query_ids": [],
        }, ensure_ascii=False)}

    with patch.object(session_runner, "SESSION_DB_PATH", directory / "sessions.db"):
        with patch.object(agent, "request_model", side_effect=model):
            with patch.object(agent, "call_tool", return_value={"ok": True, "data": {}}):
                with redirect_stdout(io.StringIO()):
                    stream = AgentEventStream(agent.run_agent, "澄清检查", "busy-A", queue_size=1)
                    iterator = stream.events()
                    try:
                        first = await asyncio.wait_for(anext(iterator), 2)
                        assert first["event"] == "tool_start"
                        assert await asyncio.to_thread(waiting.wait, 2)
                        await iterator.aclose()
                        try:
                            agent.run_agent("重叠请求", thread_id="busy-A")
                        except SessionBusyError:
                            pass
                        else:
                            raise AssertionError("连接断开时错误地提前释放了会话占用")
                    finally:
                        release.set()
                        stream.close()
                        await iterator.aclose()
                        await asyncio.to_thread(stream.worker.join, 3)
                        assert not stream.worker.is_alive()
                    with patch.object(agent, "request_model", return_value={
                        "role": "assistant", "content": json.dumps({
                            "status": "needs_clarification", "answer": "请补充月份。", "evidence_query_ids": []
                        }, ensure_ascii=False)
                    }):
                        assert agent.run_agent("任务结束后重试", thread_id="busy-A")["status"] == "needs_clarification"
    print("连接断开不提前释放会话占用，实际任务结束后可再次使用")


def parse_sse(text):
    events = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        event_name, data_lines = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].lstrip(" ")
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if data_lines:
            events.append({"event": event_name, "data": json.loads("\n".join(data_lines))})
    return events


def check_http():
    calls = []

    def fake_run(question, thread_id=None, on_event=None):
        calls.append((question, thread_id))
        on_event({"event": "tool_start", "data": tool_info()})
        on_event({"event": "tool_result", "data": {**tool_info(), "result": {"ok": True}}})
        result = conclusion(thread_id)
        result["charts"] = [str(api.OUTPUT_DIR / "example.png")]
        return result

    with TestClient(api.app) as client:
        with patch.object(api, "run_agent", side_effect=fake_run):
            response = client.post("/analyze/stream", json={"question": "  查询订单  ", "thread_id": "demo-A"})
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            events = parse_sse(response.text)
            assert [e["event"] for e in events] == ["tool_start", "tool_result", "final"]
            assert calls == [("查询订单", "demo-A")]
            assert events[-1]["data"]["charts"] == ["http://testserver/outputs/example.png"]
            assert events[-1]["data"]["thread_id"] == "demo-A"
            response = client.post("/analyze/stream", json={"question": "新会话"})
            assert parse_sse(response.text)[-1]["data"]["thread_id"]
            before = len(calls)
            for payload, code in [
                ({}, 422), ({"question": "   "}, 400),
                ({"question": 123}, 422),
                ({"question": "检查", "thread_id": ""}, 422),
                ({"question": "检查", "thread_id": 123}, 422),
                ({"question": "检查", "thread_id": "x" * 129}, 422),
                ({"question": "检查", "session_id": "wrong"}, 422),
            ]:
                response = client.post("/analyze/stream", json=payload)
                assert response.status_code == code, response.text
                assert response.headers["content-type"].startswith("application/json")
            assert len(calls) == before
        print("POST SSE帧顺序、响应头、会话编号与最终图表URL正确")
        print("错误请求在流开始前返回400/422，不启动Agent")

        for exc, code in [
            (SessionBusyError("该会话正在处理另一个问题，请等待完成"), 409),
            (requests.Timeout("offline timeout"), 504),
            (requests.ConnectionError("offline connection error"), 502),
            (RuntimeError("internal example"), 500),
        ]:
            with patch.object(api, "run_agent", side_effect=exc):
                response = client.post("/analyze/stream", json={"question": "异常检查", "thread_id": "errors-A"})
                assert response.status_code == 200
                events = parse_sse(response.text)
                assert len(events) == 1 and events[0]["event"] == "error"
                assert events[0]["data"]["status_code"] == code
                assert events[0]["data"]["thread_id"] == "errors-A"
        print("流开始后的冲突、超时和异常发送一次error，不伪装为final")


async def check_asgi_disconnect():
    release, finished = Event(), Event()
    created = []
    disconnected = asyncio.Event()
    body = json.dumps({"question": "断线检查", "thread_id": "disconnect-A"}).encode()
    body_sent = False
    sent = []

    def fake_run(question, thread_id=None, on_event=None):
        try:
            on_event({"event": "tool_start", "data": tool_info()})
            assert release.wait(3)
            on_event({"event": "tool_result", "data": {"result": {"ok": True}}})
            return conclusion(thread_id)
        finally:
            finished.set()

    def factory(*args, **kwargs):
        stream = AgentEventStream(*args, **kwargs)
        created.append(stream)
        return stream

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            disconnected.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/analyze/stream", "raw_path": b"/analyze/stream",
        "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    with patch.object(api, "run_agent", side_effect=fake_run):
        with patch.object(api, "AgentEventStream", side_effect=factory):
            try:
                await asyncio.wait_for(api.app(scope, receive, send), timeout=3)
                assert disconnected.is_set() and len(created) == 1
                assert created[0]._closed.is_set(), "HTTP断线后没有关闭发送端"
                assert not finished.is_set(), "本例后台应仍在等待，不能假装已取消执行"
                frames = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
                assert parse_sse(frames.decode())[0]["event"] == "tool_start"
            finally:
                release.set()
                for stream in created:
                    stream.close()
                    if stream.worker is not None:
                        await asyncio.to_thread(stream.worker.join, 3)
                        assert not stream.worker.is_alive()
    assert finished.is_set()
    print("模拟HTTP客户端断线后依赖清理生效，后台可正常结束")


async def check_async_parts():
    await check_incremental_and_close()
    with tempfile.TemporaryDirectory(prefix="agent_sse_check_") as name:
        directory = Path(name).resolve()
        assert directory.parent == Path(tempfile.gettempdir()).resolve()
        assert directory.name.startswith("agent_sse_check_")
        await check_disconnect_keeps_session_busy(directory)
    await check_asgi_disconnect()


if __name__ == "__main__":
    asyncio.run(check_async_parts())
    check_http()
    print("SSE接口检查通过（预设模型和工具，未请求真实API）")
