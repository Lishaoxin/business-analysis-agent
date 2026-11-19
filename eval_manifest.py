"""记录当前评测使用的核心代码、评测脚本和依赖声明的指纹。"""

import hashlib
import json
from pathlib import Path


EVAL_SOURCE_FILES = (
    "agent.py",
    "graph_agent.py",
    "agent_state.py",
    "tools.py",
    "app_paths.py",
    "tool_models.py",
    "result_models.py",
    "completion_checks.py",
    "query_store.py",
    "usage_models.py",
    "run_budget.py",
    "session_runner.py",
    "event_models.py",
    "eval_agent.py",
    "eval_data.py",
    "summarize_eval.py",
    "eval_manifest.py",
    "requirements.txt",
    "requirements-dev.txt",
)


def build_eval_manifest(base_dir=None):
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent
    base_dir = Path(base_dir)
    files = {}

    for name in EVAL_SOURCE_FILES:
        text = (base_dir / name).read_bytes().decode("utf-8-sig")
        # 统一BOM和换行，避免相同文本因Windows/Linux换行不同而变号。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        files[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {
        "version": 1,
        "hash_mode": "utf8-no-bom-lf",
        "files": files,
        "code_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


if __name__ == "__main__":
    manifest = build_eval_manifest()
    print("代码清单格式版本：", manifest["version"])
    print("指纹计算方式：", manifest["hash_mode"])
    print("记录文件数量：", len(manifest["files"]))
    print("代码总指纹：", manifest["code_sha256"])
    for name, fingerprint in manifest["files"].items():
        print(name, fingerprint[:16])
