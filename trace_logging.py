import json
import logging


# 复用Uvicorn的日志配置；不修改全局root logger。
logger = logging.getLogger("uvicorn.error")


def log_trace_event(event, trace_info, **fields):
    # 只记录调用摘要；参数、SQL结果与回答不放入这份追踪日志。
    record = {**fields, **trace_info, "event": event}
    level = logging.ERROR if event == "analysis_error" else logging.INFO
    logger.log(level, json.dumps(record, ensure_ascii=False))
