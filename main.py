from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import run_agent
from session_runner import SessionBusyError
from stream_runner import AgentEventStream

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

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


@app.post("/analyze")
def analyze(body: AnalyzeRequest, request: Request):
    question, thread_id = prepare_analysis(body)
    try:
        run_result = run_agent(question, thread_id=thread_id)
        return with_chart_urls(run_result, request)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except requests.Timeout as exc:
        raise HTTPException(
            status_code=504, detail="模型请求超时，请稍后重试"
        ) from exc
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="模型服务请求失败，请检查服务配置或稍后重试",
        ) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def prepare_stream(body: AnalyzeRequest):
    # 在流开始前校验，保证空问题可以正常返回HTTP 400。
    question, thread_id = prepare_analysis(body)
    stream = AgentEventStream(run_agent, question, thread_id)
    try:
        yield stream
    finally:
        # 请求结束或客户端断开后，停止继续积压发送事件。
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
