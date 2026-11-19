"""只使用内存样例；不调用模型或数据库，绘图使用模拟函数。"""

from copy import deepcopy
import json
from unittest.mock import patch

import agent
from agent_state import create_initial_state
from completion_checks import validate_conclusion
from query_store import QueryStore
import tools


example = {
    "columns": ["category", "sales_yuan"],
    "rows": [["示例类别", 100.0]],
    "truncated": False,
}
store = QueryStore()
query_id = store.save(example)
second_id = store.save({
    "columns": ["order_count"],
    "rows": [[4]],
    "truncated": False,
})

# 从State中取回数据时，原编号必须保留，不能重新生成。
state = create_initial_state("离线快照检查")
state["query_results"] = store.snapshot()
restored_state = json.loads(json.dumps(state, ensure_ascii=False))
restored = QueryStore(restored_state["query_results"])
assert restored.get(query_id) == example
assert restored.get(second_id)["rows"] == [[4]]
assert set(restored.snapshot()) == {query_id, second_id}
print("State序列化后恢复：两次查询及原编号均保留")

# 导出快照不能暴露内部可变对象。
snapshot = store.snapshot()
snapshot[query_id]["rows"][0][1] = 999.0
assert store.get(query_id)["rows"][0][1] == 100.0
print("修改导出快照不会污染原存储")

# 恢复时也要复制输入，避免后续节点互相污染。
restored_input = store.snapshot()
restored = QueryStore(restored_input)
restored_input[query_id]["rows"][0][1] = 888.0
assert restored.get(query_id)["rows"][0][1] == 100.0
restored_snapshot = restored.snapshot()
restored_snapshot[query_id]["rows"][0][1] = 777.0
assert restored.get(query_id)["rows"][0][1] == 100.0
print("恢复时的输入和恢复后导出的快照均相互隔离")

new_id = restored.save({
    "columns": ["new_value"],
    "rows": [[1]],
    "truncated": False,
})
assert new_id in restored.snapshot()
assert new_id not in store.snapshot()
assert new_id not in restored_input
print("恢复后新增查询不会写入原存储")

# 全新任务仍从空快照开始，不能读取先前任务的编号。
fresh_state = create_initial_state("另一个任务")
fresh_store = QueryStore(fresh_state["query_results"])
try:
    fresh_store.get(query_id)
except ValueError:
    print("全新任务的空存储仍拒绝旧编号")
else:
    raise AssertionError("全新任务不应拥有旧结果")

for bad in [[], "bad", 123]:
    try:
        QueryStore(bad)
    except ValueError:
        continue
    raise AssertionError(f"错误快照类型应被拒绝：{type(bad).__name__}")
print("错误的快照类型已拦截")

# 结束检查继续使用既有校验规则，编号来自恢复后的存储。
conclusion = validate_conclusion({
    "status": "completed",
    "answer": "示例类别销售额为100元。",
    "evidence_query_ids": [query_id],
}, restored)
assert conclusion["evidence_query_ids"] == [query_id]
print("恢复后的编号可用于结束信息校验")

received = []


def forbid_sql(*args, **kwargs):
    raise AssertionError("恢复或绘图取数不应执行新的SQL")


def fake_renderer(result, title):
    received.append(deepcopy(result))
    return {"chart_path": "offline_snapshot.png", "row_count": len(result["rows"])}


with patch.object(tools, "run_sql", side_effect=forbid_sql):
    with patch.dict(agent.TOOL_FUNCTIONS, {
        "run_sql": forbid_sql,
        "draw_chart": fake_renderer,
    }):
        feedback = agent.call_tool(
            "draw_chart",
            json.dumps({"query_id": query_id, "title": "快照检查"}),
            query_store=restored,
        )

assert feedback["ok"] is True, feedback
assert feedback["data"]["query_id"] == query_id
assert received == [example]
print("绘图工具取得恢复后的100元数据，未执行新SQL")
print("查询结果快照检查通过")
