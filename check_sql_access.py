import sqlite3
from tools import run_sql

cases = [
    ("业务表查询", "SELECT COUNT(*) AS order_count FROM orders"),
    ("系统表查询", "SELECT name FROM sqlite_master"),
    ("未开放函数", "SELECT randomblob(1)"),
]

for name, sql in cases:
    print(name)
    try:
        result = run_sql(sql)
        print("查询成功：", result)
    except sqlite3.DatabaseError as exc:
        print("已拦截：", exc)