from pathlib import Path
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import run_agent
from session_runner import SessionBusyError

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


@app.post("/analyze")
def analyze(body: AnalyzeRequest, request: Request):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    try:
        # 首问可以不传编号；追问必须传回同一个编号。
        thread_id = body.thread_id or uuid4().hex
        run_result = run_agent(question, thread_id=thread_id)

        chart_urls = []
        for chart_path in run_result["charts"]:
            filename = Path(chart_path).name
            chart_url = request.url_for("outputs", path=filename)
            chart_urls.append(str(chart_url))

        run_result["charts"] = chart_urls
        return run_result
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
