"""验证绘图只读取已保存的结果；SQL 使用本地只读连接，不调用模型。"""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import agent
from query_store import QueryStore
import tools


store = QueryStore()
query_feedback = agent.call_tool(
    "run_sql",
    json.dumps({
        "sql": "SELECT ? AS category, ? AS sales_yuan",
        "params": ["示例类别", 100.0],
    }, ensure_ascii=False),
    query_store=store,
)
assert query_feedback["ok"] is True, query_feedback
query_id = query_feedback["data"]["query_id"]
query_feedback["data"]["rows"][0][1] = 999.0

original_renderer = agent.TOOL_FUNCTIONS["draw_chart"]
received_results = []


def record_renderer(result, title="查询结果"):
    received_results.append(deepcopy(result))
    return original_renderer(result, title=title)


def forbid_sql(*args, **kwargs):
    raise AssertionError("绘图阶段不应再次查询数据库")


def draw(arguments, context=store):
    return agent.call_tool(
        "draw_chart",
        json.dumps(arguments, ensure_ascii=False),
        query_store=context,
    )


# 查完数据以后，将两处 SQL 入口替换为“被调用就报错”的函数。
with patch.object(tools, "run_sql", side_effect=forbid_sql):
    with patch.dict(
        agent.TOOL_FUNCTIONS,
        {"run_sql": forbid_sql, "draw_chart": record_renderer},
    ):
        feedback = draw({"query_id": query_id, "title": "已保存结果测试（元）"})
        assert feedback["ok"] is True, feedback
        assert feedback["data"]["query_id"] == query_id
        assert feedback["data"]["row_count"] == 1
        chart_path = Path(feedback["data"]["chart_path"])
        assert chart_path.is_file()
        assert received_results[0]["rows"] == [["示例类别", 100.0]]
        assert store.get(query_id)["rows"] == [["示例类别", 100.0]]
        print("绘图使用保存的100元结果，未使用被改为999元的反馈")
        print("绘图期间未执行新的SQL")
        print("图表查询编号：", feedback["data"]["query_id"])
        print("测试图表：", chart_path)

        invalid_cases = [
            (
                "未知编号",
                {"query_id": "query_missing", "title": "测试"},
                store,
                "ValueError",
            ),
            (
                "其他任务的编号",
                {"query_id": query_id, "title": "测试"},
                QueryStore(),
                "ValueError",
            ),
            (
                "没有任务存储",
                {"query_id": query_id, "title": "测试"},
                None,
                "ValueError",
            ),
            (
                "旧的SQL绘图参数",
                {"sql": "SELECT 1", "params": [], "title": "测试"},
                store,
                "ValidationError",
            ),
        ]
        rendered_count = len(received_results)
        for label, arguments, context, expected_error in invalid_cases:
            failed = draw(arguments, context=context)
            assert failed["ok"] is False, (label, failed)
            assert failed["error_type"] == expected_error, (label, failed)
            assert len(received_results) == rendered_count, label
            print(label, "已拦截：", failed["error_type"])

        truncated_id = store.save({
            "columns": ["category", "sales_yuan"],
            "rows": [["示例类别", 100.0]],
            "truncated": True,
        })
        failed = draw({"query_id": truncated_id, "title": "不完整结果"})
        assert failed["ok"] is False and failed["error_type"] == "ValueError"
        print("不完整的截断结果被拒绝绘图")

print("按 query_id 绘图检查通过")
