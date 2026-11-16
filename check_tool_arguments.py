import json
from pathlib import Path

from agent import TOOL_DEFINITIONS, TOOL_FUNCTIONS, call_tool
from tool_models import TOOL_ARGUMENT_MODELS
from query_store import QueryStore


# 三份注册信息必须对应，模型看到的说明也必须与后端校验一致。
definition_names = {
    item["function"]["name"] for item in TOOL_DEFINITIONS
}
assert definition_names == set(TOOL_FUNCTIONS) == set(TOOL_ARGUMENT_MODELS)
for item in TOOL_DEFINITIONS:
    name = item["function"]["name"]
    assert item["function"]["parameters"] == (
        TOOL_ARGUMENT_MODELS[name].model_json_schema()
    ), name

print("3 个工具的说明与参数模型一致")

store = QueryStore()
chart_query = call_tool(
    "run_sql",
    json.dumps({"sql": "SELECT '测试分类' AS category, 100.0 AS sales_yuan", "params": []}),
    query_store=store,
)
assert chart_query["ok"] is True, chart_query
chart_id = chart_query["data"]["query_id"]

cases = [
    ("读取表结构", "get_schema", {}, None),
    (
        "表结构工具拒绝额外参数",
        "get_schema",
        {"unexpected": 1},
        "ValidationError",
    ),
    (
        "正确 SQL 参数",
        "run_sql",
        {"sql": "SELECT ? AS order_status", "params": ["paid"]},
        None,
    ),
    ("SQL 类型错误", "run_sql", {"sql": 123, "params": []}, "ValidationError"),
    (
        "SQL 参数列表类型错误",
        "run_sql",
        {"sql": "SELECT ? AS order_status", "params": "paid"},
        "ValidationError",
    ),
    (
        "SQL 工具拒绝额外参数",
        "run_sql",
        {"sql": "SELECT 1", "params": [], "timeout": 999},
        "ValidationError",
    ),
    (
        "只读规则仍然生效",
        "run_sql",
        {"sql": "DELETE FROM orders", "params": []},
        "ValueError",
    ),
    (
        "正确绘图参数",
        "draw_chart",
        {"query_id": chart_id, "title": "参数校验测试（元）"},
        None,
    ),
    (
        "绘图标题类型错误",
        "draw_chart",
        {"query_id": chart_id, "title": 123},
        "ValidationError",
    ),
    (
        "绘图缺少标题",
        "draw_chart",
        {"query_id": chart_id},
        "ValidationError",
    ),
    (
        "绘图编号类型错误",
        "draw_chart",
        {"query_id": 123, "title": "测试"},
        "ValidationError",
    ),
]

for label, tool_name, arguments, expected_error in cases:
    feedback = call_tool(
        tool_name, json.dumps(arguments, ensure_ascii=False), query_store=store
    )
    print(
        label,
        "ok =", feedback["ok"],
        "error_type =", feedback.get("error_type"),
    )

    if expected_error is not None:
        assert feedback["ok"] is False, label
        assert feedback["error_type"] == expected_error, label
        continue

    assert feedback["ok"] is True, label
    data = feedback["data"]
    if tool_name == "get_schema":
        assert set(data) == {"orders", "products", "order_items"}, label
    elif tool_name == "run_sql":
        assert data["rows"] == [["paid"]], label
    elif tool_name == "draw_chart":
        assert data["query_id"] == chart_id, label
        assert data["row_count"] == 1, label
        chart_path = Path(data["chart_path"])
        assert chart_path.is_file(), label
        with chart_path.open("rb") as chart_file:
            assert chart_file.read(8) == b"\x89PNG\r\n\x1a\n", label
        print("测试图表：", chart_path)

print(f"{len(cases)} 项工具调用链检查通过")
