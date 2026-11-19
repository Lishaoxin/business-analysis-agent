"""用临时报告检查审核状态规则和旧报告兼容，不运行Agent。"""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from eval_data import content_hash
import summarize_eval


MISSING = object()


def write_pair(root, *, actual="completed", expected="completed", passed=True,
               report_version=2, execution="completed", requires_chart=False,
               notes="离线预设人工核验。", wrong_hash=False):
    root.mkdir()
    result = {"answer": "离线预设回答。", "charts": []}
    if actual is not MISSING:
        result["status"] = actual
    row = {
        "id": "E01", "question": "离线预设问题", "expected": {},
        "expected_status": expected, "execution_status": execution,
        "result": result, "requires_chart": requires_chart,
        "elapsed_seconds": 0.1, "log_file": "E01.log",
        "review": {"passed": None, "notes": ""},
    }
    report = {
        "requested_model": "offline", "finished_at": "2026-11-18T00:00:00+08:00",
        "requested_cases": ["E01"], "rows": [row],
        "dataset_sha256": "fixture-data", "cases_sha256": "fixture-cases",
    }
    if report_version is not MISSING:
        report["report_version"] = report_version
    review = {
        "version": 1, "method": "离线预设审核",
        "source_report_sha256": "wrong" if wrong_hash else content_hash(report),
        "reviews": [{"id": "E01", "passed": passed, "notes": notes}],
    }
    path = root / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (root / "review.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return path


def rejected(path):
    before = path.read_bytes()
    try:
        summarize_eval.load_reviewed_report(path)
    except ValueError:
        pass
    else:
        raise AssertionError("错误审核记录被允许")
    assert path.read_bytes() == before


def check():
    with tempfile.TemporaryDirectory(prefix="agent_eval_review_check_") as name:
        root = Path(name).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("agent_eval_review_check_")

        for actual in ["completed", "needs_clarification", "failed"]:
            path = write_pair(root / ("matching-" + actual), actual=actual, expected=actual)
            before = path.read_bytes()
            report = summarize_eval.load_reviewed_report(path)
            stats = summarize_eval.summarize(report)
            assert stats["passed"] == 1 and stats["execution_completed"] == 1
            assert path.read_bytes() == before
        print("完成、澄清和业务失败均按题目预期状态核验，正确匹配后可人工判通过")

        for actual in ["failed", "needs_clarification", "complete", MISSING]:
            path = write_pair(root / f"mismatch-{len(list(root.iterdir()))}", actual=actual)
            rejected(path)
        print("新报告中业务失败、错误澄清、状态拼错或缺失，均不能冒充统计完成")

        path = write_pair(root / "failed-execution", actual="failed", expected="failed", execution="failed")
        rejected(path)
        print("服务执行异常不能冒充预期业务拒绝，原执行失败保护仍有效")

        path = write_pair(root / "marked-false", actual="failed", passed=False)
        report = summarize_eval.load_reviewed_report(path)
        assert summarize_eval.summarize(report)["passed"] == 0
        path = write_pair(root / "wrong-expected", expected="wrong", passed=False)
        rejected(path)
        print("未通过记录正常保留，错误预期状态被拒绝")

        for folder, options in [
            ("missing-chart", {"requires_chart": True}),
            ("empty-notes", {"notes": "  "}),
            ("wrong-hash", {"wrong_hash": True}),
            ("wrong-bool", {"passed": "true"}),
            ("unknown-version", {"report_version": 999}),
        ]:
            rejected(write_pair(root / folder, **options))
        print("图表要求、审核说明、布尔值与本批报告指纹绑定保护均保留")

        path = write_pair(root / "legacy", actual=MISSING, report_version=MISSING)
        report = summarize_eval.load_reviewed_report(path)
        assert summarize_eval.summarize(report)["passed"] == 1
        path = write_pair(root / "legacy-explicit-failure", actual="failed", report_version=MISSING)
        rejected(path)
        print("旧报告缺少业务状态时保留原人工核验方式，已有失败状态仍会检查")

        path = write_pair(root / "cli")
        before = path.read_bytes()
        with patch.object(sys, "argv", ["summarize_eval.py", "--report", str(path)]), redirect_stdout(StringIO()):
            summarize_eval.main()
        merged = json.loads(path.with_name("report_reviewed.json").read_text(encoding="utf-8"))
        assert merged["summary"]["passed"] == 1
        assert "函数正常返回" in path.with_name("summary.md").read_text(encoding="utf-8")
        assert path.read_bytes() == before
        print("命令行正常生成新汇总，原始报告保持不变")
    print("评测审核状态检查通过（临时报告，未请求真实API）")


if __name__ == "__main__":
    check()
