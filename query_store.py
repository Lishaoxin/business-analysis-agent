from copy import deepcopy
from uuid import uuid4


class QueryStore:
    def __init__(self, results=None):
        if results is None:
            results = {}
        if not isinstance(results, dict):
            raise ValueError("查询存储快照必须是字典")

        self._results = deepcopy(results)

    def save(self, result):
        if not isinstance(result, dict):
            raise ValueError("查询结果必须是字典")

        query_id = f"query_{uuid4().hex}"
        self._results[query_id] = deepcopy(result)
        return query_id

    def get(self, query_id):
        if not isinstance(query_id, str) or query_id not in self._results:
            raise ValueError("当前任务中没有这个查询结果，请先执行查询")

        return deepcopy(self._results[query_id])

    def snapshot(self):
        return deepcopy(self._results)


if __name__ == "__main__":
    example_result = {
        "columns": ["category", "sales_yuan"],
        "rows": [["示例类别", 100.0]],
        "truncated": False,
    }

    store = QueryStore()
    query_id = store.save(example_result)
    print("查询编号：", query_id)
    print("取回结果：", store.get(query_id))

    example_result["rows"][0][1] = 999.0
    assert store.get(query_id)["rows"][0][1] == 100.0
    print("修改原始对象后，保存的金额：", store.get(query_id)["rows"][0][1])

    retrieved = store.get(query_id)
    retrieved["rows"][0][1] = 888.0
    assert store.get(query_id)["rows"][0][1] == 100.0
    print("修改取回对象后，保存的金额：", store.get(query_id)["rows"][0][1])

    another_store = QueryStore()
    try:
        another_store.get(query_id)
    except ValueError as exc:
        print("不同存储之间读取被拒绝：", exc)
    else:
        raise AssertionError("不同存储不应共享查询结果")

    print("查询结果存取检查通过")