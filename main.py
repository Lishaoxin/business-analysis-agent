from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import run_agent
from run_trace import RunTrace
from session_runner import SessionBusyError
from stream_runner import AgentEventStream
from trace_logging import log_trace_event
from app_paths import OUTPUT_DIR

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="业务数据分析 Agent")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


@app.get("/")
def homepage():
    return FileResponse(BASE_DIR / "index.html", media_type="text/html")


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: str
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("thread_id", mode="before")
    @classmethod
    def strip_thread_id(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value


def prepare_analysis(body: AnalyzeRequest):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    return question, body.thread_id or uuid4().hex


def with_chart_urls(run_result, request: Request):
    chart_urls = [
        str(request.url_for("outputs", path=Path(chart_path).name))
        for chart_path in run_result["charts"]
    ]
    return {**run_result, "charts": chart_urls}


def traced_http_error(trace, thread_id, exc, code, message):
    info = trace.finish()
    log_trace_event(
        "analysis_error", info, thread_id=thread_id,
        error_type=type(exc).__name__, status_code=code,
    )
    return HTTPException(
        status_code=code, detail=message,
        headers={"X-Trace-ID": info["trace_id"]},
    )


@app.post("/analyze")
def analyze(body: AnalyzeRequest, request: Request, response: Response):
    question, thread_id = prepare_analysis(body)
    trace = RunTrace()
    response.headers["X-Trace-ID"] = trace.trace_id
    log_trace_event("analysis_start", trace.snapshot(), thread_id=thread_id)
    try:
        run_result = run_agent(question, thread_id=thread_id)
        info = trace.finish()
        result = with_chart_urls({**run_result, **info}, request)
        log_trace_event("analysis_end", info, thread_id=thread_id, status=run_result["status"])
        return result
    except SessionBusyError as exc:
        raise traced_http_error(trace, thread_id, exc, 409, str(exc)) from exc
    except requests.Timeout as exc:
        raise traced_http_error(trace, thread_id, exc, 504, "模型请求超时，请稍后重试") from exc
    except requests.RequestException as exc:
        raise traced_http_error(
            trace, thread_id, exc, 502,
            "模型服务请求失败，请检查服务配置或稍后重试",
        ) from exc
    except (ValueError, RuntimeError) as exc:
        raise traced_http_error(trace, thread_id, exc, 500, str(exc)) from exc
    except Exception as exc:
        raise traced_http_error(trace, thread_id, exc, 500, "分析执行异常，请稍后重试") from exc


def prepare_stream(body: AnalyzeRequest, response: Response):
    # 校验失败时尚未开始分析，不创建本次分析的trace。
    question, thread_id = prepare_analysis(body)
    stream = AgentEventStream(run_agent, question, thread_id)
    response.headers["X-Trace-ID"] = stream.trace.trace_id
    try:
        yield stream
    finally:
        stream.close()


@app.post("/analyze/stream", response_class=EventSourceResponse)
async def analyze_stream(
    request: Request,
    stream: AgentEventStream = Depends(prepare_stream, scope="request"),
) -> AsyncIterator[ServerSentEvent]:
    try:
        async for event in stream.events():
            data = event["data"]
            if event["event"] == "final":
                data = with_chart_urls(data, request)
            yield ServerSentEvent(event=event["event"], data=data)
    finally:
        stream.close()
