FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    AGENT_DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check

COPY container_start.py app_paths.py agent.py agent_state.py completion_checks.py event_models.py \
     graph_agent.py main.py query_store.py result_models.py \
     run_budget.py run_trace.py session_runner.py stream_runner.py \
     tool_models.py tools.py trace_logging.py usage_models.py \
     init_db.py seed_data.py index.html ./

RUN AGENT_DATA_DIR=/app/default_data python init_db.py \
    && AGENT_DATA_DIR=/app/default_data python seed_data.py \
    && mkdir -p /app/data \
    && touch /app/data/.keep

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

ENTRYPOINT ["python", "container_start.py"]

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]