from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event: Literal["tool_start", "tool_result", "final", "error"]
    data: dict


if __name__ == "__main__":
    tool_info = {
        "round": 2,
        "tool_call_id": "call_example",
        "tool_name": "run_sql",
        "raw_arguments": '{"sql": "SELECT missing_column FROM orders", "params": []}',
    }

    examples = [
        ("工具开始", {"event": "tool_start", "data": tool_info}),
        (
            "工具返回失败，仍可交给模型修正",
            {
                "event": "tool_result",
                "data": {
                    **tool_info,
                    "result": {
                        "ok": False,
                        "error_type": "OperationalError",
                        "error": "no such column: missing_column",
                    },
                },
            },
        ),
        (
            "任务结束，本次需要补充信息",
            {
                "event": "final",
                "data": {
                    "thread_id": "demo-A",
                    "status": "needs_clarification",
                    "answer": "请补充需要统计的年份和月份。",
                    "evidence_query_ids": [],
                    "steps": [],
                    "charts": [],
                },
            },
        ),
        (
            "本次执行无法继续",
            {
                "event": "error",
                "data": {
                    "error_type": "Timeout",
                    "message": "模型请求超时，请稍后重试。",
                },
            },
        ),
        ("把任务状态误写成事件名", {"event": "completed", "data": {}}),
        ("data类型错误", {"event": "tool_start", "data": "run_sql"}),
    ]

    for label, raw_event in examples:
        print(label)
        try:
            validated = AgentEvent.model_validate(raw_event)
            print("格式通过：", validated.model_dump())
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            print("已拦截：", first["loc"], first["type"])
