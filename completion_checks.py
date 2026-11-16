from query_store import QueryStore
from result_models import TaskConclusion


def validate_conclusion(data, query_store: QueryStore, requires_query=True):
    conclusion = TaskConclusion.model_validate(data)

    if not conclusion.answer.strip():
        raise ValueError("回答或说明不能为空白")

    if conclusion.status == "completed" and requires_query:
        if not conclusion.evidence_query_ids:
            raise ValueError("数据分析完成时必须引用至少一个成功查询编号")

    for query_id in conclusion.evidence_query_ids:
        result = query_store.get(query_id)

        if conclusion.status == "completed" and result["truncated"]:
            raise ValueError("引用的查询结果被截断，请缩小范围或聚合后再完成分析")

    return conclusion.model_dump()