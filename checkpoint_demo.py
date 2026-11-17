import argparse
from pathlib import Path
import sqlite3
from typing import TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


DB_PATH = Path(__file__).resolve().parent / "sessions.db"


class CheckpointState(TypedDict):
    text: str
    history: list[str]


def remember(state: CheckpointState):
    return {
        "history": state.get("history", []) + [state["text"]],
    }


def build_demo(checkpointer):
    builder = StateGraph(CheckpointState)
    builder.add_node("remember", remember)
    builder.add_edge(START, "remember")
    builder.add_edge("remember", END)
    return builder.compile(checkpointer=checkpointer)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["write", "read"], required=True)
    args = parser.parse_args()

    config_a = {"configurable": {"thread_id": "checkpoint-demo-A"}}
    config_b = {"configurable": {"thread_id": "checkpoint-demo-B"}}

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    try:
        # 本例的状态只有普通字符串和列表。
        serde = JsonPlusSerializer(allowed_msgpack_modules=[])
        checkpointer = SqliteSaver(conn, serde=serde)
        graph = build_demo(checkpointer)

        if args.mode == "write":
            graph.invoke({
                "text": "2026年9月销售额",
                "history": [],
            }, config_a)
            result_a = graph.invoke({
                "text": "那10月份呢",
            }, config_a)
            result_b = graph.invoke({
                "text": "查询商品类别",
                "history": [],
            }, config_b)
            print("A两轮后的历史：", result_a["history"])
            print("B的历史：", result_b["history"])
        else:
            state_a = graph.get_state(config_a).values
            state_b = graph.get_state(config_b).values
            print("新进程读取A：", state_a.get("history", []))
            print("新进程读取B：", state_b.get("history", []))
    finally:
        conn.close()


if __name__ == "__main__":
    main()