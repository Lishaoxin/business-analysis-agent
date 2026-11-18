"""供执行边界检查整次任务的时间预算；不负责中断正在运行的请求。"""

from math import isfinite
from time import perf_counter


class TaskBudgetExceededError(RuntimeError):
    pass


class TaskBudget:
    def __init__(self, max_seconds=30.0, clock=perf_counter):
        if type(max_seconds) not in (int, float):
            raise ValueError("max_seconds必须是大于0的有限数字")
        try:
            limit = float(max_seconds)
        except OverflowError as exc:
            raise ValueError("max_seconds必须是大于0的有限数字") from exc
        if not isfinite(limit) or limit <= 0:
            raise ValueError("max_seconds必须是大于0的有限数字")

        self.max_seconds = limit
        self._clock = clock
        self._started_at = clock()

    def remaining_seconds(self):
        elapsed = self._clock() - self._started_at
        return max(0.0, self.max_seconds - elapsed)

    def check(self):
        # 判断使用原始精度，不将剩余时间四舍五入后再比较。
        if self.remaining_seconds() <= 0:
            raise TaskBudgetExceededError(
                f"已达到本次分析的{self.max_seconds:g}秒时间预算"
            )


if __name__ == "__main__":
    # 可控时钟只用于演示，不等待真实时间。
    demo_time = 100.0

    def demo_clock():
        return demo_time

    budget = TaskBudget(max_seconds=2, clock=demo_clock)
    assert budget.remaining_seconds() == 2.0
    print("初始剩余（秒）：", budget.remaining_seconds())

    demo_time = 100.75
    assert budget.remaining_seconds() == 1.25
    budget.check()
    print("模拟经过0.75秒后剩余（秒）：", budget.remaining_seconds())
    print("预算未耗尽：允许继续")

    demo_time = 102.0
    try:
        budget.check()
    except TaskBudgetExceededError as exc:
        print("刚好达到预算时已拦截：", exc)
    else:
        raise AssertionError("刚好达到预算时应当拒绝继续")

    demo_time = 103.0
    assert budget.remaining_seconds() == 0.0
    print("超过预算后的剩余（秒）：", budget.remaining_seconds())

    next_budget = TaskBudget(max_seconds=2, clock=demo_clock)
    assert next_budget.remaining_seconds() == 2.0
    print("新任务的剩余（秒）：", next_budget.remaining_seconds())

    precise_time = 0.0

    def precise_clock():
        return precise_time

    precise_budget = TaskBudget(max_seconds=2, clock=precise_clock)
    precise_time = 1.9999
    assert precise_budget.remaining_seconds() > 0
    precise_budget.check()
    print("剩余不足1毫秒时仍按真实剩余时间判断")

    for label, invalid in [
        ("零", 0), ("负数", -1), ("字符串", "30"),
        ("布尔值", True), ("无穷大", float("inf")),
        ("非数值NaN", float("nan")), ("过大整数", 10 ** 400),
    ]:
        try:
            TaskBudget(max_seconds=invalid)
        except ValueError:
            print(f"错误预算（{label}）已拦截：ValueError")
        else:
            raise AssertionError("错误预算没有被拒绝")

    print("任务时间预算检查通过（可控时钟演示，未请求真实API）")
