"""离线检查 HTTP 会话契约；替代 Agent 执行，不调用真实模型或写会话数据库。"""

from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
import requests

import main as api
from session_runner import SessionBusyError


def main():
    calls = []

    def fake_run_agent(question, *, thread_id):
        calls.append({"question": question, "thread_id": thread_id})
        return {
            "status": "completed",
            "answer": "这是接口检查使用的预设回答。",
            "evidence_query_ids": ["query_api_example"],
            "steps": [],
            "charts": [str(Path(api.OUTPUT_DIR) / "chart_api_example.png")],
            "thread_id": thread_id,
        }

    with TestClient(api.app) as client:
        with patch.object(api, "run_agent", side_effect=fake_run_agent):
            first = client.post("/analyze", json={"question": "  9月销售额是多少？  "})
            assert first.status_code == 200, first.text
            first_result = first.json()
            first_id = first_result["thread_id"]
            assert UUID(first_id).version == 4
            assert calls[-1] == {"question": "9月销售额是多少？", "thread_id": first_id}
            print("首问未传编号：API生成编号并交给Agent，响应返回相同编号")

            second = client.post(
                "/analyze", json={"question": "那10月份呢？", "thread_id": first_id}
            )
            assert second.status_code == 200, second.text
            assert second.json()["thread_id"] == first_id
            assert calls[-1] == {"question": "那10月份呢？", "thread_id": first_id}
            print("追问传回旧编号：API把同一编号继续交给Agent")

            third = client.post("/analyze", json={"question": "开始一个新问题"})
            assert third.status_code == 200, third.text
            assert third.json()["thread_id"] != first_id
            null_id = client.post(
                "/analyze", json={"question": "新问题", "thread_id": None}
            )
            assert null_id.status_code == 200, null_id.text
            assert UUID(null_id.json()["thread_id"]).version == 4
            print("再次不传编号或传null：创建新会话")

            normalized = client.post(
                "/analyze", json={"question": "查订单", "thread_id": "  demo-A  "}
            )
            assert normalized.status_code == 200, normalized.text
            assert calls[-1]["thread_id"] == "demo-A"
            assert normalized.json()["thread_id"] == "demo-A"
            print("会话编号首尾空格统一去除")

            assert first_result["status"] == "completed"
            assert first_result["evidence_query_ids"] == ["query_api_example"]
            assert first_result["steps"] == []
            assert first_result["charts"] == [
                "http://testserver/outputs/chart_api_example.png"
            ]
            assert client.get("/outputs/file_that_does_not_exist.png").status_code == 404
            print("原有状态、证据、工具记录和图表URL字段保持可用")

            invalid_requests = [
                ({}, 422),
                ({"question": 123}, 422),
                ({"question": ""}, 400),
                ({"question": "   "}, 400),
                ({"question": "查订单", "thread_id": ""}, 422),
                ({"question": "查订单", "thread_id": "   "}, 422),
                ({"question": "查订单", "thread_id": "x" * 129}, 422),
                ({"question": "查订单", "thread_id": 123}, 422),
                ({"question": "查订单", "thread_id": True}, 422),
                ({"question": "查订单", "thread_id": []}, 422),
                ({"question": "查订单", "session_id": "wrong-field"}, 422),
            ]
            before = len(calls)
            for payload, expected_code in invalid_requests:
                response = client.post("/analyze", json=payload)
                assert response.status_code == expected_code, (payload, response.text)
            assert len(calls) == before
            print("空问题、错误编号和多余字段在启动Agent前被拒绝")

        errors = [
            (SessionBusyError("该会话正在处理另一个问题，请等待完成"), 409),
            (requests.Timeout("test timeout"), 504),
            (requests.ConnectionError("test connection error"), 502),
            (RuntimeError("test runtime error"), 500),
            (ValueError("test value error"), 500),
        ]
        for error, expected_code in errors:
            with patch.object(api, "run_agent", side_effect=error):
                response = client.post(
                    "/analyze", json={"question": "查订单", "thread_id": "error-case"}
                )
                assert response.status_code == expected_code, response.text
        print("同会话请求冲突返回409，模型超时504和服务请求失败502继续正确区分")

        for task_status in ("needs_clarification", "failed"):
            result = {
                "status": task_status,
                "answer": "这是经过检查的任务说明。",
                "evidence_query_ids": [],
                "steps": [],
                "charts": [],
                "thread_id": "status-case",
            }
            with patch.object(api, "run_agent", return_value=result):
                response = client.post(
                    "/analyze", json={"question": "查订单", "thread_id": "status-case"}
                )
                assert response.status_code == 200, response.text
                assert response.json()["status"] == task_status
        print("HTTP请求成功时仍保留needs_clarification/failed业务状态")

    print("会话API接入检查通过")


if __name__ == "__main__":
    main()
