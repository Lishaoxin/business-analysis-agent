from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class TaskConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["completed", "needs_clarification", "failed"]
    answer: str = Field(min_length=1)
    evidence_query_ids: list[str] = Field(
        description="支持本次回答的查询编号；没有引用查询结果时填空列表"
    )


if __name__ == "__main__":
    examples = [
        (
            "完成分析",
            {
                "status": "completed",
                "answer": "已支付订单共有4笔。",
                "evidence_query_ids": ["query_example"],
            },
        ),
        (
            "需要澄清",
            {
                "status": "needs_clarification",
                "answer": "你想统计哪个月份的销售额？",
                "evidence_query_ids": [],
            },
        ),
        (
            "执行失败",
            {
                "status": "failed",
                "answer": "查询执行失败，本次未获得可用结果。",
                "evidence_query_ids": [],
            },
        ),
        (
            "状态拼写错误",
            {
                "status": "done",
                "answer": "已完成。",
                "evidence_query_ids": [],
            },
        ),
        (
            "编号列表类型错误",
            {
                "status": "completed",
                "answer": "已支付订单共有4笔。",
                "evidence_query_ids": "query_example",
            },
        ),
    ]

    for label, data in examples:
        print(label)
        try:
            conclusion = TaskConclusion.model_validate(data)
            print("格式通过：", conclusion.model_dump())
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            print("已拦截：", first_error["loc"], first_error["type"])