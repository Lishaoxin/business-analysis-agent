import json

from agent import call_tool


cases = [
    (
        "正确参数",
        {"sql": "SELECT ? AS order_status", "params": ["paid"]},
        None,
    ),
    ("SQL 类型错误", {"sql": 123, "params": []}, "ValidationError"),
    (
        "params 类型错误",
        {"sql": "SELECT ? AS order_status", "params": "paid"},
        "ValidationError",
    ),
    (
        "多余参数",
        {"sql": "SELECT 1", "params": [], "timeout": 999},
        "ValidationError",
    ),
    (
        "只读规则仍然生效",
        {"sql": "DELETE FROM orders", "params": []},
        "ValueError",
    ),
]

for label, arguments, expected_error in cases:
    result = call_tool(
        "run_sql", json.dumps(arguments, ensure_ascii=False)
    )
    print(label, "ok =", result["ok"], "error_type =", result.get("error_type"))

    if expected_error is None:
        assert result["ok"] is True, label
        assert result["data"]["rows"] == [["paid"]], label
    else:
        assert result["ok"] is False, label
        assert result["error_type"] == expected_error, label

print("5 项工具调用链检查通过")