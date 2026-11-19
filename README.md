# 业务数据分析 Agent

用自然语言查询模拟电商订单，支持查数、条件澄清、多轮追问与绘图。`DeepSeek` 通过 Tool Calling 提出查询或绘图请求，`LangGraph` 管理执行流程，`FastAPI` 提供 JSON 和 SSE 接口。

![按商品类别统计已支付销售额](docs/images/category-sales.png)

## Docker Compose 启动

使用 Docker Desktop 的 Linux 容器，在项目根目录执行：

```powershell
if (-not (Test-Path -LiteralPath .\.env)) {
    Copy-Item -LiteralPath .\.env.example -Destination .\.env
}
```

在 `.env` 中填入 `DEEPSEEK_API_KEY`，然后启动：

```powershell
docker compose up --build --wait
```

打开 [分析网页](http://127.0.0.1:8003/) 或 [API 文档](http://127.0.0.1:8003/docs)。初次启动会准备 6 笔演示订单，可试问“2026 年 9 月已支付订单总销售额是多少元？另外请按商品类别绘制柱状图。”预期总额为 1400 元。继续追问“按刚才相同口径，只告诉我 10 月的总销售额”，预期为 1700 元。

业务库、会话库和图片保存在挂载到 `/app/data` 的命名卷中。已实际验证强制重建容器后，业务库被复用，原会话可继续追问，重建前的图片仍返回 HTTP 200。`docker compose down` 停止服务并保留卷。执行 `docker compose down -v` 会删除卷及其数据。

<details>
<summary>直接用 Python 本地启动</summary>

在 Windows 的项目根目录准备 Python 3.12 和已配置的 `.env`：

```powershell
if (-not (Test-Path -LiteralPath .\venv\Scripts\python.exe)) {
    python -m venv venv
}
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -X utf8 init_db.py
.\venv\Scripts\python.exe -X utf8 seed_data.py
.\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

打开 [本地开发网页](http://127.0.0.1:8001/)。程序默认从项目目录读写运行数据，也可以将 `AGENT_DATA_DIR` 设为绝对路径。代码默认从 `.env` 配置读取 Key。

</details>

## 执行流程

1. 浏览器以 `POST /analyze/stream` 提交问题和会话编号。SQLite 检查点提供已完成的问答历史，每个新问题重新建立查询状态。
2. 模型调用 `get_schema` 获取表结构。`run_sql` 使用 SQLite 只读连接，限制访问表、函数、查询时间和返回行数。
3. 成功查询保存结果并返回本次任务的 `query_id`。`draw_chart` 按编号读取两列结果绘图，复用同一次查询的数据。
4. 结束校验检查结构化结果和证据编号，并区分 `completed`、`needs_clarification`、`failed`。网页通过 SSE 显示工具进度，最终收到正式回答和图表链接。

常用入口为 [main.py](main.py)、[图流程](graph_agent.py)、[SQL 和绘图工具](tools.py)与 [Docker Compose](compose.yaml)。JSON 接口为 `POST /analyze`，与 SSE 接口使用相同的分析入口。接口返回的查询引用可用于核对答案，业务语义仍按评测标准复核。

## 评测

正式评测使用同一份 1,000 笔模拟订单、12 种商品、2,481 条明细的快照。结果来自真实模型采集和逐题人工复核。

| 题集 | 核验结果 | 记录 |
| --- | --- | --- |
| V2 开发集 | 20/20 | [汇总](eval_runs/run_ab1b34091677/summary.md) · [原始报告](eval_runs/run_ab1b34091677/report.json) · [审核记录](eval_runs/run_ab1b34091677/review.json) |
| V2 保留集 | 查询 18/18，澄清 2/2 | [汇总](eval_runs/run_732ca98b0c5d/summary.md) · [原始报告](eval_runs/run_732ca98b0c5d/report.json) · [审核记录](eval_runs/run_732ca98b0c5d/review.json) |

两份归档保存了工具调用、原始答案、逐题日志和图表。查看 [保留集订单笔数图](eval_runs/run_732ca98b0c5d/outputs/chart_2681ad4e2d3243139c3a938cd558653c.png)。固定数据与题集在 [evaluation/fixtures](evaluation/fixtures/)。评测恢复独立 SQLite 库，Agent 只接收问题和执行上限。历史评测结论对应报告中的记录版本与运行配置。

在本地环境中执行准备检查，验证快照、题集和报告元数据：

```powershell
.\venv\Scripts\python.exe -X utf8 eval_agent.py --suite v2-test --case all --check-only
```

去掉 `--check-only` 后会请求真实模型。新运行目录默认保留在本地的 `eval_runs/`。第一版评测与早期实现记录可从 [整理前的历史版本](https://github.com/Lishaoxin/business-analysis-agent/tree/b8f81da) 查看。

## 自动回归

使用 Python 3.12 和 Node 22，运行：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -X utf8 run_checks.py --include-web
```

共 28 项，覆盖工具参数、查询与证据、预算、会话、API、SSE、评测隔离、容器初始化和网页逻辑。脚本集中在 [checks](checks/)，统一入口在临时目录执行，使用预设模型响应。`--list` 查看清单，`--only check_draw_query_id.py` 可以只选一项。[GitHub Actions 工作流](.github/workflows/offline-checks.yml)运行同一入口。
