"""只检查结束信息和任务内证据，不调用模型、不访问数据库。"""

from completion_checks import validate_conclusion
from query_store import QueryStore


store = QueryStore()
query_id = store.save({
    "columns": ["order_count"],
    "rows": [[4]],
    "truncated": False,
})
truncated_id = store.save({
    "columns": ["order_id"],
    "rows": [["O001"]],
    "truncated": True,
})
empty_id = store.save({
    "columns": ["order_id"],
    "rows": [],
    "truncated": False,
})
other_store = QueryStore()
other_id = other_store.save({
    "columns": ["order_count"],
    "rows": [[4]],
    "truncated": False,
})


def make_conclusion(status, answer, ids):
    return {
        "status": status,
        "answer": answer,
        "evidence_query_ids": ids,
    }


completed = make_conclusion("completed", "已支付订单共有4笔。", [query_id])
checks = [
    ("当前任务的有效证据", completed, True, None),
    (
        "编造的编号",
        {**completed, "evidence_query_ids": ["query_fake"]},
        True,
        "当前任务中没有这个查询结果",
    ),
    (
        "其他任务的编号",
        {**completed, "evidence_query_ids": [other_id]},
        True,
        "当前任务中没有这个查询结果",
    ),
    (
        "分析完成却没有证据",
        {**completed, "evidence_query_ids": []},
        True,
        "必须引用至少一个成功查询编号",
    ),
    (
        "需要澄清时没有证据",
        make_conclusion("needs_clarification", "请补充统计月份。", []),
        True,
        None,
    ),
    (
        "执行失败时没有证据",
        make_conclusion("failed", "查询失败，未能完成分析。", []),
        True,
        None,
    ),
    (
        "非查询任务由程序允许无SQL证据",
        make_conclusion("completed", "表结构说明已完成。", []),
        False,
        None,
    ),
    (
        "截断结果不能作为完整分析的依据",
        {**completed, "evidence_query_ids": [truncated_id]},
        True,
        "查询结果被截断",
    ),
    (
        "成功查询但没有匹配记录",
        make_conclusion("completed", "没有符合条件的订单。", [empty_id]),
        True,
        None,
    ),
    (
        "失败说明也不能引用假编号",
        make_conclusion("failed", "无法完成分析。", ["query_fake"]),
        True,
        "当前任务中没有这个查询结果",
    ),
    (
        "空白回答",
        {**completed, "answer": "   "},
        True,
        "不能为空白",
    ),
]

for label, data, requires_query, expected_error in checks:
    try:
        result = validate_conclusion(
            data, store, requires_query=requires_query
        )
    except ValueError as exc:
        if expected_error is None:
            raise
        assert expected_error in str(exc), (label, str(exc))
        print(label, "已拦截：", exc)
    else:
        assert expected_error is None, f"{label}应该被拦截"
        assert result == data, (label, result)
        print(label, "通过：", result["status"])

try:
    validate_conclusion(
        {**completed, "evidence_query_ids": query_id}, store
    )
except ValueError as exc:
    assert type(exc).__name__ == "ValidationError", exc
    print("格式检查仍然生效：ValidationError")
else:
    raise AssertionError("编号列表类型错误应该被拦截")

print("12 项结束信息与证据检查通过")
