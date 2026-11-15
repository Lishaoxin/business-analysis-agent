import json
from agent import call_tool

cases = [
    ("错误字段查询", "SELECT missing_column FROM orders"),
    ("正常订单数量查询", "SELECT COUNT(*) AS order_count FROM orders"),
]

for name, sql in cases:
    raw_arguments = json.dumps({
        "sql": sql,
        "params": [],
    })
    result = call_tool("run_sql", raw_arguments)
    print(name)
    print(json.dumps(result, ensure_ascii=False, indent=2))