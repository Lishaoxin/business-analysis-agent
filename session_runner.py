from pathlib import Path
import sqlite3
from threading import Lock

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_state import create_initial_state
from graph_agent import build_agent_graph


SESSION_DB_PATH = Path(__file__).resolve().parent / "sessions.db"
_active_sessions = set()
_active_guard = Lock()


class SessionBusyError(RuntimeError):
    pass


def run_session_agent(
    question, thread_id, request_model, call_tool,
    max_rounds=6, requires_query=True, db_path=None,
):
    if not isinstance(thread_id, str):
        raise ValueError("thread_id必须是非空字符串")
    thread_id = thread_id.strip()
    if not thread_id or len(thread_id) > 128:
        raise ValueError("thread_id长度必须为1到128个字符")
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError("max_rounds必须是正整数")

    path = Path(db_path) if db_path is not None else SESSION_DB_PATH
    path = path.resolve()
    session_key = (str(path), thread_id)

    # 只在登记、移除会话编号时持锁，不把整个模型调用锁住。
    with _active_guard:
        if session_key in _active_sessions:
            raise SessionBusyError("该会话正在处理另一个问题，请等待完成")
        _active_sessions.add(session_key)

    conn = None
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        serde = JsonPlusSerializer(allowed_msgpack_modules=[])
        checkpointer = SqliteSaver(conn, serde=serde)
        graph = build_agent_graph(
            request_model, call_tool, checkpointer=checkpointer
        )
        config = {
            # 与同一数据库里的教学演示会话区分。
            "configurable": {"thread_id": f"business:{thread_id}"},
            "recursion_limit": 2 * max_rounds + 3,
        }

        saved = graph.get_state(config).values
        history = saved.get("history", [])

        # 只带回已结束的问答历史，其余执行字段按新问题初始化。
        state = create_initial_state(
            question,
            max_rounds=max_rounds,
            requires_query=requires_query,
            history=history,
        )
        final_state = graph.invoke(state, config)
        return {
            **final_state["final_result"],
            "thread_id": thread_id,
        }
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            with _active_guard:
                _active_sessions.discard(session_key)
