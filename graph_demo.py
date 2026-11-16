from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class DemoState(TypedDict):
    question: str
    steps: list[str]
    answer: str


def prepare_question(state: DemoState):
    print("整理节点收到的问题：", repr(state["question"]))
    return {
        "question": state["question"].strip(),
        "steps": state["steps"] + ["prepare_question"],
    }


def finish_demo(state: DemoState):
    print("结束节点收到的问题：", repr(state["question"]))
    return {
        "answer": f"这是状态传递演示，收到的问题是：{state['question']}",
        "steps": state["steps"] + ["finish_demo"],
    }

def ask_for_question(state: DemoState):
    print("补充问题节点：问题为空")
    return {
        "answer": "请先输入你要分析的问题。",
        "steps": state["steps"] + ["ask_for_question"],
    }


def route_question(state: DemoState):
    if not state["question"]:
        return "ask_for_question"
    return "finish_demo"

builder = StateGraph(DemoState)
builder.add_node("prepare_question", prepare_question)
builder.add_node("finish_demo", finish_demo)
builder.add_node("ask_for_question", ask_for_question)

builder.add_edge(START, "prepare_question")
builder.add_conditional_edges(
    "prepare_question",
    route_question,
    ["finish_demo", "ask_for_question"],
)
builder.add_edge("finish_demo", END)
builder.add_edge("ask_for_question", END)

graph = builder.compile()


if __name__ == "__main__":
    for question in ["  已支付订单共有多少笔？  ", "   "]:
        print("\n本次输入：", repr(question))
        result = graph.invoke({
            "question": question,
            "steps": [],
            "answer": "",
        })
        print("最终状态：", result)