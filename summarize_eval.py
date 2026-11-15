import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eval_data import content_hash


def load_reviewed_report(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    review = json.loads(
        path.with_name("review.json").read_text(encoding="utf-8")
    )
    if review["version"] != 1:
        raise ValueError("不支持的审核文件版本")
    if content_hash(report) != review["source_report_sha256"]:
        raise ValueError("审核文件与本次报告不匹配")
    if not report.get("finished_at"):
        raise ValueError("评测尚未运行结束")

    ids = [row["id"] for row in report["rows"]]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("报告题目必须非空且ID唯一")
    requested = report["requested_cases"]
    if set(ids) != set(requested) or len(ids) != len(requested):
        raise ValueError("报告记录与请求题目不一致")

    judgements = {item["id"]: item for item in review["reviews"]}
    if len(judgements) != len(review["reviews"]) or set(judgements) != set(ids):
        raise ValueError("审核记录必须与报告题目一一对应")

    for row in report["rows"]:
        item = judgements[row["id"]]
        if type(item["passed"]) is not bool:
            raise ValueError("passed必须是true或false")
        if not isinstance(item["notes"], str) or not item["notes"].strip():
            raise ValueError("每题必须填写审核说明")
        if item["passed"] and row["execution_status"] != "completed":
            raise ValueError("执行失败的任务不能标记为通过")
        if item["passed"] and row.get("requires_chart"):
            if not row.get("result", {}).get("charts"):
                raise ValueError("要求绘图的任务缺少图表")
        row["review"] = {"passed": item["passed"], "notes": item["notes"]}

    report["review_method"] = review["method"]
    report["source_report_sha256"] = review["source_report_sha256"]
    report["reviewed_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    return report


def summarize(report):
    rows = report["rows"]
    times = [row["elapsed_seconds"] for row in rows]
    passed = sum(row["review"]["passed"] is True for row in rows)
    return {
        "total": len(rows),
        "execution_completed": sum(
            row["execution_status"] == "completed" for row in rows
        ),
        "passed": passed,
        "pass_rate_percent": round(passed / len(rows) * 100, 2),
        "mean_seconds": round(statistics.mean(times), 3),
        "median_seconds": round(statistics.median(times), 3),
        "max_seconds": max(times),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.resolve()
    report = load_reviewed_report(report_path)
    stats = summarize(report)
    report["summary"] = stats

    reviewed_path = report_path.with_name("report_reviewed.json")
    reviewed_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 固定任务评测汇总", "",
        f"- 模型：{report['requested_model']}",
        f"- 核验通过：{stats['passed']}/{stats['total']}（{stats['pass_rate_percent']}%）",
        f"- 执行完成：{stats['execution_completed']}/{stats['total']}",
        f"- 平均单题耗时：{stats['mean_seconds']} 秒",
        f"- 单题耗时中位数：{stats['median_seconds']} 秒",
        f"- 数据指纹：{report['dataset_sha256']}",
        f"- 题集指纹：{report['cases_sha256']}", "",
        "| 题目及日志 | 核验结果 | 耗时（秒） |",
        "| --- | --- | --- |",
    ]
    for row in report["rows"]:
        verdict = "通过" if row["review"]["passed"] else "未通过"
        lines.append(
            f"| [{row['id']}]({row['log_file']}) | {verdict} | {row['elapsed_seconds']:.3f} |"
        )
    lines.extend([
        "", f"核验方法：{report['review_method']}。",
        "范围：固定模拟数据与本批任务；耗时采用采集脚本记录的Agent任务耗时。", "",
        "[原始报告](report.json) · [审核记录](review.json) · [详细核验记录](report_reviewed.json)",
    ])
    summary_path = report_path.with_name("summary.md")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("核验通过：", stats["passed"], "/", stats["total"])
    print("平均单题耗时：", stats["mean_seconds"], "秒")
    print("单题耗时中位数：", stats["median_seconds"], "秒")
    print("合并报告：", reviewed_path)
    print("评测汇总：", summary_path)


if __name__ == "__main__":
    main()