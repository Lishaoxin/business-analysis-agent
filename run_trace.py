from time import perf_counter
from uuid import uuid4


class RunTrace:
    """记录一次分析的追踪编号和耗时；计时对象不放入会话State。"""

    def __init__(self, clock=perf_counter):
        self.trace_id = "trace_" + uuid4().hex
        self._clock = clock
        self._started_at = clock()
        self._finished_at = None

    def snapshot(self):
        end = self._finished_at
        if end is None:
            end = self._clock()
        return {
            "trace_id": self.trace_id,
            "elapsed_ms": round((end - self._started_at) * 1000, 3),
        }

    def finish(self):
        # 只记录第一次结束时间，之后读取同一份最终耗时。
        if self._finished_at is None:
            self._finished_at = self._clock()
        return self.snapshot()


if __name__ == "__main__":
    # 演示用可控时钟，不等待真实时间；实际使用RunTrace()即可。
    demo_time = 100.0

    def demo_clock():
        return demo_time

    trace = RunTrace(clock=demo_clock)
    print("本次分析编号：", trace.trace_id)
    print("开始时耗时（毫秒）：", trace.snapshot()["elapsed_ms"])

    demo_time = 100.125
    print("模拟经过125毫秒：", trace.snapshot()["elapsed_ms"])

    demo_time = 100.250
    print("结束记录：", trace.finish())

    demo_time = 101.000
    print("结束后再次读取（毫秒）：", trace.finish()["elapsed_ms"])

    next_trace = RunTrace(clock=demo_clock)
    print("两次分析的编号不同：", trace.trace_id != next_trace.trace_id)
    print("下一次分析从0开始（毫秒）：", next_trace.snapshot()["elapsed_ms"])
