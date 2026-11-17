from copy import deepcopy
import json

from langgraph.graph import END, START, StateGraph

from agent_state import AgentState, create_initial_state, finish_turn
from completion_checks import validate_conclusion
from event_models import AgentEvent
from query_store import QueryStore
from time import perf_counter
from usage_models import parse_usage


def route_after_model(state: AgentState):
    if state["final_result"] is not None:
        return END
    if state["model_message"].get("tool_calls"):
        return "tools"
    return "validate"


def route_after_validation(state: AgentState):
    if state["final_result"] is not None:
        return END
    return "model"


# on_event接收进度字典；它是运行时函数，不放进State。
def build_agent_graph(request_model, call_tool, checkpointer=None, on_event=None):
    if on_event is not None and not callable(on_event):
        raise ValueError("on_event必须是可调用对象或None")

    def emit_event(event_name, data):
        if on_event is None:
            return
        event = AgentEvent(event=event_name, data=deepcopy(data))
        # 给观察方一份独立数据，避免其修改影响工具记录或查询结果。
        on_event(event.model_dump(mode="json"))

    # 模型节点先检查请求预算，再保存模型响应和新的调用次数。
    def model_node(state: AgentState):
        if state["rounds"] >= state["max_rounds"]:
            return finish_turn(state, {
                "status": "failed",
                "answer": (
                    f"已达到{state['max_rounds']}轮请求上限，"
                    "未能生成通过校验的结束信息。"
                ),
                "evidence_query_ids": [],
            })

        started_at = perf_counter()
        reply = request_model(state["messages"])
        duration_ms = round((perf_counter() - started_at) * 1000, 3)

        if "message" in reply:
            message = reply["message"]
            usage = parse_usage(reply.get("usage"))
        else:
            # 兼容之前只返回消息的预设模型函数。
            message = reply
            usage = None

        rounds = state["rounds"] + 1
        model_calls = state["model_calls"] + [{
        "round": rounds,
        "duration_ms": duration_ms,
        "usage": usage,
        }]

        print(f"第 {rounds} 轮模型返回：")
        print(json.dumps(message, ensure_ascii=False, indent=2))

        return {
            "model_message": message,
            "rounds": rounds,
            "model_calls": model_calls,
        }

    # 各节点从同一任务的State恢复查询结果；对象本身不放入State。
    def tools_node(state: AgentState):
        query_store = QueryStore(state["query_results"])
        # 通过新列表返回更新，避免直接修改传入State中的列表。
        messages = list(state["messages"])
        steps = list(state["steps"])
        charts = list(state["charts"])
        message = state["model_message"]
        tool_calls = message.get("tool_calls") or []

        assistant_tool_calls = []
        for tool_call in tool_calls:
            assistant_tool_calls.append({
                "id": tool_call["id"],
                "type": tool_call["type"],
                "function": tool_call["function"],
            })

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": assistant_tool_calls,
        })

        for tool_call in tool_calls:
            tool_name = tool_call["function"]["name"]
            raw_arguments = tool_call["function"]["arguments"]
            tool_info = {
                "round": state["rounds"],
                "tool_call_id": tool_call["id"],
                "tool_name": tool_name,
                "raw_arguments": raw_arguments,
            }
            emit_event("tool_start", tool_info)
            # 只测本次工具调用，进度回调不计入。
            started_at = perf_counter()
            result = call_tool(
                tool_name, raw_arguments, query_store=query_store
            )
            duration_ms = round(
                (perf_counter() - started_at) * 1000, 3
            )
            step = {
                **tool_info,
                "result": result,
                "duration_ms": duration_ms,
            }
            steps.append(step)

            if tool_name == "draw_chart" and result["ok"]:
                charts.append(result["data"]["chart_path"])

            if result["ok"]:
                print("工具调用成功：", tool_name)
            else:
                print("工具调用失败：", tool_name)
            print("工具反馈：")
            print(json.dumps(result, ensure_ascii=False, indent=2))

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })
            emit_event("tool_result", step)

        return {
            "messages": messages,
            "steps": steps,
            "charts": charts,
            "query_results": query_store.snapshot(),
        }

    # 校验合格才写入final_result；不合格只追加反馈，路由回模型节点。
    def validate_node(state: AgentState):
        content = state["model_message"].get("content") or ""
        query_store = QueryStore(state["query_results"])
        try:
            data = json.loads(content)
            conclusion = validate_conclusion(
                data,
                query_store,
                requires_query=state["requires_query"],
            )
        except (ValueError, TypeError) as exc:
            print("结束信息校验失败：", exc)
            messages = state["messages"] + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        f"结束信息未通过程序校验：{exc}。"
                        "请继续处理原问题，必要时调用工具，再返回符合约定的JSON。"
                        "查询失败不能当作0或无记录；需要用户补充条件时使用"
                        "needs_clarification，无法完成时使用failed。"
                    ),
                },
            ]
            return {"messages": messages}

        return finish_turn(state, conclusion)

    # 构图阶段只注册节点和路由，不请求模型或执行SQL。
    builder = StateGraph(AgentState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_node("validate", validate_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model", route_after_model, ["tools", "validate", END]
    )
    builder.add_edge("tools", "model")
    builder.add_conditional_edges(
        "validate", route_after_validation, ["model", END]
    )
    return builder.compile(checkpointer=checkpointer)


def run_graph_agent(
    question, request_model, call_tool, max_rounds=6, requires_query=True,
    on_event=None,
):
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError("max_rounds必须是正整数")

    state = create_initial_state(
        question, max_rounds=max_rounds, requires_query=requires_query
    )
    graph = build_agent_graph(request_model, call_tool, on_event=on_event)
    # 图步数包含工具和校验节点，与模型请求次数不同。
    final_state = graph.invoke(
        state,
        config={"recursion_limit": 2 * max_rounds + 3},
    )
    return final_state["final_result"]
