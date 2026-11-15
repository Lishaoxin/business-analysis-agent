from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RunSQLArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sql: str = Field(min_length=1, description="查询 SQL，条件值使用 ? 占位符")
    params: list[str | int | float] = Field(
        description="按占位符顺序填写参数，无参数时传空列表"
    )

class GetSchemaArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DrawChartArguments(RunSQLArguments):
    title: str = Field(min_length=1, description="中文图表标题，标明统计范围和单位")


TOOL_ARGUMENT_MODELS = {
    "get_schema": GetSchemaArguments,
    "run_sql": RunSQLArguments,
    "draw_chart": DrawChartArguments,
}

if __name__ == "__main__":
    examples = [
        (
            "正常参数",
            {
                "sql": "SELECT COUNT(*) FROM orders WHERE status = ?",
                "params": ["paid"],
            },
        ),
        ("SQL 类型错误", {"sql": 123, "params": []}),
        (
            "params 类型错误",
            {"sql": "SELECT COUNT(*) FROM orders", "params": "paid"},
        ),
        (
            "多余参数",
            {"sql": "SELECT COUNT(*) FROM orders", "params": [], "timeout": 999},
        ),
    ]

    for label, arguments in examples:
        print(label)
        try:
            validated = RunSQLArguments.model_validate(arguments)
            print("通过：", validated.model_dump())
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            print("已拦截：", first_error["loc"], first_error["type"])