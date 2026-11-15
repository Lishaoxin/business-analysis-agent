from time import monotonic
from tools import run_sql

slow_sql = """
SELECT (
    WITH RECURSIVE numbers(n) AS (
        SELECT 1
        UNION ALL
        SELECT n + 1 FROM numbers WHERE n < 100000000
    )
    SELECT SUM(n) FROM numbers
) AS total
"""

started = monotonic()

try:
    result = run_sql(slow_sql)
    print("查询正常完成：", result)
except TimeoutError as exc:
    print("超时拦截：", exc)
finally:
    print(f"耗时：{monotonic() - started:.2f} 秒")

result = run_sql("SELECT COUNT(*) AS order_count FROM orders")
print("超时后的普通查询：", result)