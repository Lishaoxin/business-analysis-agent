"""准备持久化数据目录，然后启动容器的主进程。"""

from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import NamedTemporaryFile

from app_paths import BASE_DIR, DATA_DIR

TEMPLATE_DB_PATH = BASE_DIR / "default_data" / "business.db"


def validate_business_db(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"orders", "products", "order_items"}.issubset(tables):
            raise ValueError("业务数据库缺少必要的表，请检查数据目录")
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("业务数据库完整性检查未通过")


def prepare_data(data_dir, template_db):
    data_dir = Path(data_dir).resolve()
    template_db = Path(template_db).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "outputs").mkdir(exist_ok=True)
    database = data_dir / "business.db"

    if database.exists():
        validate_business_db(database)
        return False
    if not template_db.is_file():
        raise FileNotFoundError("缺少用于首次初始化的演示数据库模板")

    temporary = None
    try:
        with NamedTemporaryFile(prefix=".business-init-", suffix=".tmp", dir=data_dir, delete=False) as target:
            temporary = Path(target.name)
            with template_db.open("rb") as source:
                shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        validate_business_db(temporary)
        try:
            # 发布完整数据库。如果已有另一个启动进程完成写入，则直接复用。
            os.link(temporary, database)
        except FileExistsError:
            validate_business_db(database)
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    command = sys.argv[1:]
    if not command:
        raise SystemExit("请提供容器启动命令")
    created = prepare_data(DATA_DIR, TEMPLATE_DB_PATH)
    print("已初始化演示业务库" if created else "已复用现有业务库", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
