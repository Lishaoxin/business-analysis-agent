"""统一定义业务数据库、会话数据库和图表的保存路径。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def resolve_data_dir():
    configured = os.getenv("AGENT_DATA_DIR")
    if configured is None:
        return BASE_DIR

    configured = configured.strip()
    if not configured:
        raise ValueError("AGENT_DATA_DIR不能为空白")

    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError("AGENT_DATA_DIR必须使用绝对路径")
    return path.resolve()


DATA_DIR = resolve_data_dir()
DB_PATH = DATA_DIR / "business.db"
SESSION_DB_PATH = DATA_DIR / "sessions.db"
OUTPUT_DIR = DATA_DIR / "outputs"


if __name__ == "__main__":
    print("代码目录：", BASE_DIR)
    print("数据目录：", DATA_DIR)
    print("业务数据库：", DB_PATH)
    print("会话数据库：", SESSION_DB_PATH)
    print("图表目录：", OUTPUT_DIR)