import sqlite3
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from tools import get_schema, run_sql, draw_chart
from tool_models import TOOL_ARGUMENT_MODELS
from query_store import QueryStore

MODEL_NAME = "deepseek-flash"

TOOL_FUNCTIONS = {
    "get_schema": get_schema,
    "run_sql": run_sql,
    "draw_chart": draw_chart,
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": (
                "获取业务数据库三张表的真实建表SQL，包括字段、约束和关联关系。"
                "编写SQL前先调用。"
            ),
            "parameters": TOOL_ARGUMENT_MODELS["get_schema"].model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "执行以SELECT开头的单条SQLite只读查询，返回query_id、columns、rows、truncated。"
                "query_id是当前任务中本次查询结果的编号。"
                "仅访问orders、products、order_items；"
                "可用函数：sum、count、avg、min、max、coalesce、round、nullif、date、strftime。"
            ),
            "parameters": TOOL_ARGUMENT_MODELS["run_sql"].model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draw_chart",
            "description": (
                "根据当前任务中已保存的查询结果生成柱状图，返回chart_path、row_count和query_id。"
                "先调用run_sql取得query_id，再将该编号和title传入本工具。"
                "引用结果必须恰好两列：第一列类别，第二列数值。"
                "销售额绘图时第二列应已换算为元。"
            ),
            "parameters": TOOL_ARGUMENT_MODELS["draw_chart"].model_json_schema(),
        },
    },
]

def request_model(messages):
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("请在 .env 中配置 DEEPSEEK_API_KEY")

    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": MODEL_NAME,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "thinking": {"type": "disabled"},
        },
        timeout=(10, 60),
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]

def execute_tool(tool_name, arguments, query_store=None):
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具：{tool_name}")
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是字典")

    argument_model = TOOL_ARGUMENT_MODELS[tool_name]
    validated = argument_model.model_validate(arguments)
    arguments = validated.model_dump()
    tool_function = TOOL_FUNCTIONS[tool_name]

    if tool_name == "draw_chart":
        if query_store is None:
            raise ValueError("绘图需要当前任务的查询存储，请先执行查询")

        query_id = arguments["query_id"]
        saved_result = query_store.get(query_id)
        data = tool_function(saved_result, title=arguments["title"])
        return {**data, "query_id": query_id}

    data = tool_function(**arguments)

    if tool_name == "run_sql" and query_store is not None:
        query_id = query_store.save(data)
        data = {**data, "query_id": query_id}

    return data


def call_tool(tool_name, raw_arguments, query_store=None):
    try:
        arguments = json.loads(raw_arguments)
        data = execute_tool(tool_name, arguments, query_store=query_store)
        return {
            "ok": True,
            "data": data,
        }
    except (ValueError, TypeError, sqlite3.DatabaseError, OSError) as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

def run_agent(question, max_rounds=6):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("问题必须是非空字符串")

    query_store = QueryStore()
    steps = []
    charts = []

    messages = [
        {
            "role": "system",
            "content": (
                "你是业务数据分析助手，请用中文回答。"
                "编写或修正SQL前，先通过get_schema确认真实表结构。"
                "统计数值必须来自run_sql；金额以分存储，展示时转为元。"
                "需要绘图时，先用run_sql取得合适的数据，再把其成功反馈中的query_id交给draw_chart。"
                "query_id只在当前任务内有效，不得编造；绘图工具不接收SQL或原始数据。"
                "图片路径使用工具返回值，不推断单价是否含税或优惠。"
                "工具反馈ok=true时，data是有效结果；ok=false表示执行失败，error说明原因。"
                "可修复的错误应修正SQL或参数后重试；权限受限或无法修复时如实说明。"
                "仅依据成功的工具结果回答，结果足够时直接给出答案。"
            ),
        },
        {
            "role": "user",
            "content": question.strip(),
        },
    ]

    for round_index in range(max_rounds):
        message = request_model(messages)
        print(f"第 {round_index + 1} 轮模型返回：")
        print(json.dumps(message, ensure_ascii=False, indent=2))

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return {
                "answer": message.get("content") or "",
                "steps": steps,
                "charts": charts,
            }

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
            result = call_tool(
                tool_name, raw_arguments, query_store=query_store
            )
            steps.append({
                "round": round_index + 1,
                "tool_call_id": tool_call["id"],
                "tool_name": tool_name,
                "raw_arguments": raw_arguments,
                "result": result,
            })

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

    raise RuntimeError(f"已达到{max_rounds}轮请求上限，本次任务尚未生成最终回答。")


if __name__ == "__main__":
    question = input("请输入分析问题：")
    try:
        run_result = run_agent(question)
        print("最终回答：", run_result["answer"])
        print("工具调用次数：", len(run_result["steps"]))
        print("图表路径：", run_result["charts"])
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        print("任务未完成：", exc)