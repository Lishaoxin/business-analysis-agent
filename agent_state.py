from copy import deepcopy
from typing import TypedDict
from usage_models import summarize_usage

class AgentState(TypedDict):
    question: str
    history: list[dict]
    messages: list[dict]
    model_message: dict | None
    steps: list[dict]
    charts: list[str]
    rounds: int
    max_rounds: int
    requires_query: bool
    query_results: dict[str, dict]
    final_result: dict | None
    model_calls: list[dict]


def create_initial_state(
    question, max_rounds=6, requires_query=True, history=None
) -> AgentState:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("问题必须是非空字符串")

    if history is None:
        history = []
    history = deepcopy(history)

    messages = [
        {
            "role": "system",
            "content": (
                "你是业务数据分析助手，请用中文回答。"
                "历史问答用于理解本轮问题的省略和指代。"
                "遇到“刚才”“那两个月”等指代，必须从当前会话历史中确认具体范围。"
                "如果历史中没有明确对应范围，应使用needs_clarification询问缺失条件。"
                "不得根据数据库只有哪些月份，或查询全库后看到的月份，猜测用户指代。"
                "本轮统计必须重新查询，不得引用历史任务的查询编号。"
                "编写或修正SQL前，先通过get_schema确认真实表结构。"
                "统计数值必须来自run_sql；金额以分存储，展示时转为元。"
                "需要绘图时，先用run_sql取得合适的数据，再把其成功反馈中的query_id交给draw_chart。"
                "query_id只在当前任务内有效，不得编造；绘图工具不接收SQL或原始数据。"
                "图片路径使用工具返回值，不推断单价是否含税或优惠。"
                "工具反馈ok=true时，data是有效结果；ok=false表示执行失败，error说明原因。"
                "可修复的错误应修正SQL或参数后重试；权限受限或无法修复时如实说明。"
                "仅依据成功的工具结果回答，结果足够时直接给出答案。"
                "结束时必须在content中返回一个JSON对象，不使用Markdown代码块或额外文字。"
                "对象只能包含status、answer、evidence_query_ids三个字段。"
                "status只能是completed、needs_clarification、failed；answer保存中文回答或说明。"
                "evidence_query_ids是支持回答的查询编号列表，只能引用当前任务成功返回的编号。"
                f"本任务requires_query={requires_query}。"
                "requires_query=True时，completed必须引用至少一个未截断的成功查询结果。"
                "需要用户补充条件时使用needs_clarification；无法完成时使用failed；这两种状态允许空编号列表。"
            ),
        },
        {
            "role": "user",
            "content": question.strip(),
        },
    ]

    messages = [messages[0]] + deepcopy(history) + [messages[1]]

    return {
        "question": question.strip(),
        "history": history,
        "messages": messages,
        "model_message": None,
        "steps": [],
        "charts": [],
        "rounds": 0,
        "max_rounds": max_rounds,
        "requires_query": requires_query,
        "query_results": {},
        "final_result": None,
        "model_calls": [],
    }


def finish_turn(state: AgentState, conclusion: dict):
    return {
        "history": state["history"] + [
            {"role": "user", "content": state["question"]},
            {"role": "assistant", "content": conclusion["answer"]},
        ],
        "final_result": {
            **conclusion,
            "steps": state["steps"],
            "charts": state["charts"],
            "model_calls": state["model_calls"],
            "usage_summary": summarize_usage(state["model_calls"]),
        },
    }


if __name__ == "__main__":
    state = create_initial_state("  已支付订单共有多少笔？  ")
    print("问题：", state["question"])
    print("消息角色：", [message["role"] for message in state["messages"]])
    print("已调用模型次数：", state["rounds"])
    print("模型调用上限：", state["max_rounds"])
    print("已保存查询结果：", state["query_results"])
    print("最终结果：", state["final_result"])