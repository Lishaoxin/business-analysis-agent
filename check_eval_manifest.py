"""用临时文本文件检查评测代码清单，不导入应用或请求模型。"""

from copy import deepcopy
from pathlib import Path
import tempfile

from eval_manifest import EVAL_SOURCE_FILES, build_eval_manifest


def check():
    with tempfile.TemporaryDirectory(prefix="agent_eval_manifest_check_") as name:
        root = Path(name).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("agent_eval_manifest_check_")
        for filename in EVAL_SOURCE_FILES:
            (root / filename).write_bytes(f"# fixture: {filename}\nvalue = 1\n".encode("utf-8"))

        baseline = build_eval_manifest(root)
        assert len(baseline["files"]) == len(EVAL_SOURCE_FILES) == 18
        assert set(baseline["files"]) == set(EVAL_SOURCE_FILES)
        assert len(baseline["code_sha256"]) == 64
        assert baseline == build_eval_manifest(root)
        print("18个声明文件均被记录，相同内容重复计算结果一致")

        for filename in ["graph_agent.py", "eval_agent.py", "summarize_eval.py", "requirements.txt"]:
            path = root / filename
            original = path.read_bytes()
            path.write_bytes(original + b"# changed\n")
            changed = build_eval_manifest(root)
            assert changed["code_sha256"] != baseline["code_sha256"]
            differences = {name for name in baseline["files"]
                           if baseline["files"][name] != changed["files"][name]}
            assert differences == {filename}
            path.write_bytes(original)
        print("图逻辑、采集脚本、审核脚本或依赖声明变化，均会改变总指纹")

        for filename in EVAL_SOURCE_FILES:
            path = root / filename
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
        assert build_eval_manifest(root) == baseline
        print("UTF-8 BOM和CRLF换行变化不影响相同文本的指纹")

        modified = deepcopy(baseline)
        modified["files"]["agent.py"] = "changed"
        assert build_eval_manifest(root) == baseline
        print("修改已返回的清单不会污染后续计算")

        missing = root / "graph_agent.py"
        missing.unlink()
        try:
            build_eval_manifest(root)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("必需源码缺失却被静默忽略")
        print("必需文件缺失时明确报错，避免生成不完整清单")
    print("评测代码清单检查通过（临时文本文件，未请求真实API）")


if __name__ == "__main__":
    check()
