"""验证首次初始化、数据复用与失败清理；只使用临时数据库。"""

from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import patch

import container_start as startup
from eval_data import FIXTURE_DIR, restore_database


def main():
    snapshot = json.loads((FIXTURE_DIR / "eval_fixture_v1.json").read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="agent_container_start_check_") as directory:
        root = Path(directory).resolve()
        assert root.parent == Path(gettempdir()).resolve()
        assert root.name.startswith("agent_container_start_check_")
        template = root / "template.db"
        with closing(sqlite3.connect(template)) as conn:
            restore_database(conn, snapshot["dataset"])

        data = root / "nested" / "runtime"
        assert startup.prepare_data(data, template) is True
        database = data / "business.db"
        assert database.read_bytes() == template.read_bytes()
        assert (data / "outputs").is_dir()
        with closing(sqlite3.connect(database)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 6
            conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", ("KEPT", "2026-11-19", "华东", "paid"))
            conn.commit()
        (data / "sessions.db").write_bytes(b"preserve-session")
        (data / "outputs" / "kept.png").write_bytes(b"preserve-image")
        saved = {path: path.read_bytes() for path in [database, data / "sessions.db", data / "outputs" / "kept.png"]}
        assert startup.prepare_data(data, root / "missing-template.db") is False
        assert all(path.read_bytes() == content for path, content in saved.items())
        print("首次启动创建演示库；已有业务修改、会话文件和图片再次启动时保持不变")
        print("已有有效业务库时可以直接复用，不依赖演示模板")

        missing = root / "missing-target"
        try:
            startup.prepare_data(missing, root / "missing-template.db")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("模板缺失应明确失败")
        assert not (missing / "business.db").exists()
        print("缺少初始化模板时明确失败，不创建空业务库")

        broken = root / "broken-existing"
        broken.mkdir()
        (broken / "business.db").write_bytes(b"not-a-sqlite-database")
        try:
            startup.prepare_data(broken, template)
        except sqlite3.DatabaseError:
            pass
        else:
            raise AssertionError("已有无效数据库应被拒绝")
        assert (broken / "business.db").read_bytes() == b"not-a-sqlite-database"
        print("已有数据库异常时保留原文件并拒绝启动，避免自动重置数据")

        invalid_template = root / "invalid-template.db"
        with closing(sqlite3.connect(invalid_template)) as conn:
            conn.execute("CREATE TABLE orders (order_id TEXT)")
        invalid_target = root / "invalid-template-target"
        try:
            startup.prepare_data(invalid_target, invalid_template)
        except ValueError:
            pass
        else:
            raise AssertionError("不完整模板不应成为正式业务库")
        assert not (invalid_target / "business.db").exists()
        assert not list(invalid_target.glob(".business-init-*.tmp"))
        print("模板校验失败后清理临时文件，不留下半成品业务库")

        interrupted = root / "interrupted"
        def broken_copy(source, target):
            target.write(b"partial")
            raise OSError("offline copy failure")
        with patch.object(startup.shutil, "copyfileobj", side_effect=broken_copy):
            try:
                startup.prepare_data(interrupted, template)
            except OSError:
                pass
            else:
                raise AssertionError("复制失败应向外抛出")
        assert not (interrupted / "business.db").exists()
        assert not list(interrupted.glob(".business-init-*.tmp"))
        assert startup.prepare_data(interrupted, template) is True
        print("初始化中途失败可以清理并重试，成功后才生成正式业务库")

        concurrent = root / "concurrent"
        original_link = startup.os.link

        def other_start_finished(source, target):
            target.write_bytes(saved[database])
            original_link(source, target)

        with patch.object(startup.os, "link", side_effect=other_start_finished):
            assert startup.prepare_data(concurrent, template) is False
        assert (concurrent / "business.db").read_bytes() == saved[database]
        assert not list(concurrent.glob(".business-init-*.tmp"))
        print("同时初始化时保留先创建的业务库，后续进程直接复用")

        command = ["python", "-m", "uvicorn", "main:app"]
        with (
            patch.object(startup, "DATA_DIR", data),
            patch.object(startup, "TEMPLATE_DB_PATH", root / "unused-template.db"),
            patch.object(sys, "argv", ["container_start.py", *command]),
            patch.object(startup.os, "execvp") as execute,
            redirect_stdout(StringIO()),
        ):
            startup.main()
        execute.assert_called_once_with("python", command)
        assert all(path.read_bytes() == content for path, content in saved.items())
        with patch.object(sys, "argv", ["container_start.py"]), patch.object(startup, "prepare_data") as prepare:
            try:
                startup.main()
            except SystemExit:
                pass
            else:
                raise AssertionError("没有启动命令应被拒绝")
            prepare.assert_not_called()
        print("准备数据后交接主进程；缺少启动命令时在初始化前被拒绝")
    print("容器启动初始化检查通过（临时数据库，未启动Docker或请求真实API）")


if __name__ == "__main__":
    main()
