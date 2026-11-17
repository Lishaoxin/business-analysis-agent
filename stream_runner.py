import asyncio
from copy import deepcopy
from queue import Empty, Full, Queue
from threading import Event, Thread

import requests

from event_models import AgentEvent
from session_runner import SessionBusyError


class AgentEventStream:
    """后台执行一次Agent，消费者异步逐条读取事件。"""

    def __init__(self, run_agent, question, thread_id, queue_size=64):
        self.run_agent = run_agent
        self.question = question
        self.thread_id = thread_id
        self._queue = Queue(maxsize=queue_size)
        self._closed = Event()
        self._started = False
        self.worker = None

    def close(self):
        # 停止发送，不强行终止正在执行的模型请求。
        # 会话占用仍由run_session_agent在真正结束时释放。
        self._closed.set()

    def _send(self, event_name, data):
        if self._closed.is_set():
            return
        event = AgentEvent(event=event_name, data=deepcopy(data))
        message = event.model_dump(mode="json")
        while not self._closed.is_set():
            try:
                self._queue.put(message, timeout=0.1)
                return
            except Full:
                # 队列满时等待消费者；断线后close会让等待退出。
                continue

    def _on_event(self, event):
        if self._closed.is_set():
            return
        if event["event"] not in {"tool_start", "tool_result"}:
            raise ValueError("Agent回调只能发送工具进度事件")
        self._send(event["event"], event["data"])

    def _run(self):
        try:
            result = self.run_agent(
                self.question,
                thread_id=self.thread_id,
                on_event=self._on_event,
            )
            # 返回到这里时，结束信息校验和会话保存已经完成。
            self._send("final", {**result, "thread_id": self.thread_id})
        except Exception as exc:
            if isinstance(exc, SessionBusyError):
                code, message = 409, str(exc)
            elif isinstance(exc, requests.Timeout):
                code, message = 504, "模型请求超时，请稍后重试"
            elif isinstance(exc, requests.RequestException):
                code, message = 502, "模型服务请求失败，请检查服务配置或稍后重试"
            else:
                code, message = 500, "分析执行异常，请稍后重试"
            self._send("error", {
                "thread_id": self.thread_id,
                "error_type": type(exc).__name__,
                "message": message,
                "status_code": code,
            })

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
