import asyncio
from copy import deepcopy
from queue import Empty, Full, Queue
from threading import Event, Thread

import requests

from event_models import AgentEvent
from run_trace import RunTrace
from session_runner import SessionBusyError
from trace_logging import log_trace_event


class AgentEventStream:
    """后台执行一次Agent，消费者异步逐条读取事件。"""

    def __init__(self, run_agent, question, thread_id, queue_size=64, trace=None):
        self.run_agent = run_agent
        self.question = question
        self.thread_id = thread_id
        self.trace = trace if trace is not None else RunTrace()
        self._queue = Queue(maxsize=queue_size)
        self._closed = Event()
        self._started = False
        self.worker = None

    def close(self):
        # 停止发送，不强行终止正在执行的模型请求。
        # 会话占用仍由run_session_agent在真正结束时释放。
        self._closed.set()

    def _send(self, event_name, data, trace_info=None):
        if self._closed.is_set():
            return
        info = self.trace.snapshot() if trace_info is None else trace_info
        event = AgentEvent(event=event_name, data={**deepcopy(data), **info})
        message = event.model_dump(mode="json")
        while not self._closed.is_set():
            try:
                self._queue.put(message, timeout=0.1)
                return
            except Full:
                # 队列满时等待消费者；断线后close会让等待退出。
                continue

    def _on_event(self, event):
        if event["event"] not in {"tool_start", "tool_result"}:
            raise ValueError("Agent回调只能发送工具进度事件")
        data = event["data"]
        info = self.trace.snapshot()
        fields = {
            "thread_id": self.thread_id,
            "round": data.get("round"),
            "tool_call_id": data.get("tool_call_id"),
            "tool_name": data.get("tool_name"),
        }
        if event["event"] == "tool_result":
            fields["ok"] = data.get("result", {}).get("ok")
        # 断线只停止发送，后台实际执行的进度仍有日志可查。
        log_trace_event(event["event"], info, **fields)
        self._send(event["event"], data, trace_info=info)

    def _run(self):
        log_trace_event("analysis_start", self.trace.snapshot(), thread_id=self.thread_id)
        try:
            result = self.run_agent(
                self.question,
                thread_id=self.thread_id,
                on_event=self._on_event,
            )
            # 后台真正完成后结算，close()不能提前冻结耗时。
            info = self.trace.finish()
            log_trace_event(
                "analysis_end", info, thread_id=self.thread_id,
                status=result["status"],
            )
            self._send("final", {**result, "thread_id": self.thread_id}, trace_info=info)
        except Exception as exc:
            if isinstance(exc, SessionBusyError):
                code, message = 409, str(exc)
            elif isinstance(exc, requests.Timeout):
                code, message = 504, "模型请求超时，请稍后重试"
            elif isinstance(exc, requests.RequestException):
                code, message = 502, "模型服务请求失败，请检查服务配置或稍后重试"
            else:
                code, message = 500, "分析执行异常，请稍后重试"
            info = self.trace.finish()
            log_trace_event(
                "analysis_error", info, thread_id=self.thread_id,
                error_type=type(exc).__name__, status_code=code,
            )
            self._send("error", {
                "thread_id": self.thread_id,
                "error_type": type(exc).__name__,
                "message": message,
                "status_code": code,
            }, trace_info=info)

    async def events(self):
        if self._started:
            raise RuntimeError("同一个事件流只能读取一次")
        self._started = True
        if self._closed.is_set():
            return

        self.worker = Thread(
            target=self._run,
            name=f"agent-stream-{self.thread_id[:8]}",
            daemon=True,
        )
        self.worker.start()
        try:
            while not self._closed.is_set():
                try:
                    event = await asyncio.to_thread(self._queue.get, True, 0.1)
                except Empty:
                    continue
                if self._closed.is_set():
                    return
                yield event
                if event["event"] in {"final", "error"}:
                    return
        finally:
            self.close()
