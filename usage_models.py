"""校验模型服务返回的基础Token计数；本模块不发起API请求。"""

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class TokenUsage(BaseModel):
    # 服务可能附带缓存等扩展信息；此处只提取三个基础计数。
    model_config = ConfigDict(extra="ignore", strict=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def check_total(self):
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens必须等于prompt_tokens与completion_tokens之和")
        return self


def parse_usage(raw_usage):
    # None表示服务未提供用量，不能用0冒充已知计数。
    if raw_usage is None:
        return None
    return TokenUsage.model_validate(raw_usage).model_dump()

def summarize_usage(model_calls):
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    usage_call_count = 0

    for call in model_calls:
        usage = parse_usage(call.get("usage"))
        if usage is None:
            continue

        usage_call_count += 1
        for name in totals:
            totals[name] += usage[name]

    complete = usage_call_count == len(model_calls)

    return {
        "model_call_count": len(model_calls),
        "usage_call_count": usage_call_count,
        "complete": complete,
        "prompt_tokens": totals["prompt_tokens"] if complete else None,
        "completion_tokens": totals["completion_tokens"] if complete else None,
        "total_tokens": totals["total_tokens"] if complete else None,
    }

if __name__ == "__main__":
    examples = [
        (
            "正常用量，附带服务扩展字段",
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
             "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36},
            None,
        ),
        ("服务未返回用量", None, None),
        (
            "服务明确返回零用量",
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            None,
        ),
        (
            "数字被写成字符串",
            {"prompt_tokens": "100", "completion_tokens": 20, "total_tokens": 120},
            "int_type",
        ),
        (
            "布尔值冒充整数",
            {"prompt_tokens": True, "completion_tokens": 20, "total_tokens": 21},
            "int_type",
        ),
        (
            "负数计数",
            {"prompt_tokens": -1, "completion_tokens": 20, "total_tokens": 19},
            "greater_than_equal",
        ),
        (
            "总数与分项不一致",
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 999},
            "value_error",
        ),
        (
            "缺少必需计数",
            {"prompt_tokens": 100, "completion_tokens": 20},
            "missing",
        ),
        ("用量类型错误", [], "model_type"),
    ]

    for label, raw, expected_error in examples:
        print(label)
        try:
            parsed = parse_usage(raw)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            assert first["type"] == expected_error, first
            print("已拦截：", first["loc"], first["type"])
        else:
            assert expected_error is None, label
            if raw is None:
                assert parsed is None
            else:
                assert set(parsed) == {"prompt_tokens", "completion_tokens", "total_tokens"}
                assert parsed["total_tokens"] == raw["total_tokens"]
            print("解析结果：", parsed)

    print("Token用量格式检查通过（预设数据，未请求真实API）")
