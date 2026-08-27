# 06 实装计划 — Qlib 数据适配与动量因子研究

- 对应 ticket：[06 — Qlib 数据适配与动量因子研究](./issues/06-qlib-momentum-research.md)
- 设计依据：[06 系统设计](./06-design-doc.md)
- 架构决定：[ADR-0001](../../docs/adr/0001-use-qlib-data-bundles.md)

本计划按可观察的纵向切片推进，每个切片遵循“失败测试 → 最小实现 → 测试通过”。06 到 `RankedScores` 为止，不生成 `TargetPortfolio`，不运行 `ResearchBacktest`。

## 0. 固定运行基线

已在 [`backend/pyproject.toml`](../../backend/pyproject.toml) 声明 `pyqlib==0.9.7` 与 `pyarrow`，并更新 [`backend/uv.lock`](../../backend/uv.lock)。后端继续使用 [`backend/Dockerfile`](../../backend/Dockerfile) 的 Python 3.12 基线。

完成条件：

- `uv sync --frozen` 成功；
- worker 可以导入 `qlib` 并读取实际版本；
- API 模块不导入或初始化 `qlib`。

## 1. 实现不可变研究定义和纯研究计算

新增 `backend/app/research/` 深模块，先实现不依赖数据库和 Qlib 全局状态的公开计算 seam：

```python
ResearchDefinition(...).fingerprint

calculate_momentum_scores(
    closes,
    observation_dates,
    lookback_days,
    skip_days,
) -> DataFrame

calculate_daily_labels(opens, observation_dates) -> DataFrame
calculate_weekly_labels(opens, weekly_observation_dates) -> DataFrame
evaluate_cross_section(scores, labels, universe_size) -> CrossSectionEvaluation
summarize_ic(values) -> IcSummary
```

实现文件：

- [`backend/app/research/definition.py`](../../backend/app/research/definition.py)：规范化 Experiment 定义和 SHA-256 指纹；
- [`backend/app/research/factor.py`](../../backend/app/research/factor.py)：`close[t-skip] / close[t-lookback-skip] - 1`、90% 路径覆盖、平均秩和百分位；
- [`backend/app/research/labels.py`](../../backend/app/research/labels.py)：日度下一开盘至第六个后续开盘、周度实际调仓开盘标签；
- [`backend/app/research/evaluation.py`](../../backend/app/research/evaluation.py)：FactorCoverage、LabelCoverage、Pearson IC、Spearman Rank IC、未年化 ICIR、五分组收益和结构化 unavailable 原因；
- [`backend/app/services/research.py`](../../backend/app/services/research.py)：通过 `/ranked-scores` 发布分数、无效原因和股票域 exclusions。

测试文件：

- [`backend/tests/test_research_definition.py`](../../backend/tests/test_research_definition.py)；
- [`backend/tests/test_momentum_research.py`](../../backend/tests/test_momentum_research.py)；
- [`backend/tests/test_research_labels.py`](../../backend/tests/test_research_labels.py)；
- [`backend/tests/test_research_evaluation.py`](../../backend/tests/test_research_evaluation.py)。

期望值使用手算常量，覆盖并列、零方差、90% 临界值、标签端点缺失、尾部标签未成熟和周度观察不足。

## 2. 构建原生 QlibDataBundle

复用 [`snapshot_member_query`](../../backend/app/services/snapshot_reader.py) 解析 DataSnapshot 可见的行情版本，复用 [`DataSnapshot`](../../backend/app/models/market_data.py) 绑定的 calendar publication。新增：

```python
QlibDataBundleBuilder.ensure(snapshot, task_id=...) -> QlibDataBundle
read_features(bundle_path, instruments, fields, start, end) -> DataFrame
analyze_signals(predictions, labels) -> dict
```

实现文件：

- [`backend/app/research/bundle.py`](../../backend/app/research/bundle.py)：写入 `calendars/day.txt`、`instruments/all.txt`、原生 feature binary 和 JSON manifest；
- [`backend/app/research/bundle_builder.py`](../../backend/app/research/bundle_builder.py)：从 DataSnapshot 构建、校验并原子发布数据包；
- [`backend/app/research/qlib_runtime.py`](../../backend/app/research/qlib_runtime.py)：仅供 worker 使用，负责 `qlib.init`、表达式读取和 `SigAnaRecord` 信号分析；
- [`backend/app/core/config.py`](../../backend/app/core/config.py)：增加数据包目录、研究产物目录、磁盘预算和线程数配置。

字段映射严格为复权 OHLCV、由 `adjusted_close/raw_close` 推导的 `$factor`、显式 raw 字段、成交额、单日 adjustment event、质量状态与原因。`untradable` 研究价格写 NaN，不填充缺失值。

构建过程使用同一父目录中的临时目录，完整验证后原子改名；失败删除临时目录并保留 BuildAttempt 错误。测试通过真实 `qlib.data.D.features` 验证 `$close`、`$factor` 和动量表达式。

## 3. 增加持久化模型和迁移

新增 [`backend/app/models/research.py`](../../backend/app/models/research.py) 与 [`20260827_6a8f06b8d431_qlib_momentum_research.py`](../../backend/migrations/versions/20260827_6a8f06b8d431_qlib_momentum_research.py)，包含：

```text
qlib_data_bundles
data_bundle_build_attempts
research_experiments
research_runs
research_artifacts
```

关键数据库约束：

- bundle 身份唯一：`(data_snapshot_id, exporter_schema_version, pyqlib_version)`；
- Experiment 指纹唯一；
- 一个 Experiment 同时最多一个活动 Run；
- 一个 Run 最多一个已发布 ResearchArtifact；
- 终态运行不被覆盖，重跑创建新 Run；
- artifact 保存 schema version、manifest、checksum、受控相对路径和大小。

更新 [`backend/app/models/__init__.py`](../../backend/app/models/__init__.py)，使测试 `Base.metadata.create_all` 与 Alembic 使用同一模型集合。

## 4. 实现 ResearchRun 用例、worker 和 REST API

复用 [`Task`](../../backend/app/models/task.py)、[`worker registry`](../../backend/app/worker/registry.py) 与单 worker [`runner`](../../backend/app/worker/runner.py)。新增：

```python
SqlResearchApplication.create_run(request) -> dict
SqlResearchApplication.cancel_run(run_id) -> dict
ResearchWorkflow.execute(run_id) -> dict
```

`create_run` 只接受 snapshot、观察区间、lookback 和 skip；它拒绝不可研究快照、任意表达式、类路径或 workflow 配置。相同定义复用 Experiment；存在活动 Run 时返回该 Run；终态后创建新 Run 和通用 Task。

`ResearchWorkflow.execute` 的状态顺序：

```text
queued
→ waiting_for_bundle
→ computing_factors
→ computing_labels
→ evaluating
→ publishing
→ succeeded / failed / cancelled
```

新增 [`backend/app/api/research.py`](../../backend/app/api/research.py) 并挂载到 [`backend/app/api/router.py`](../../backend/app/api/router.py)：

```text
GET    /api/v1/research/config
POST   /api/v1/research/runs
GET    /api/v1/research/runs
GET    /api/v1/research/runs/{run_id}
POST   /api/v1/research/runs/{run_id}/cancel
GET    /api/v1/research/runs/{run_id}/results
GET    /api/v1/research/runs/{run_id}/ranked-scores
GET    /api/v1/qlib-data-bundles
POST   /api/v1/qlib-data-bundles
DELETE /api/v1/qlib-data-bundles/{bundle_id}
```

API 测试从这些公开路由观察 Experiment 复用、Run 生命周期、取消、结果兼容错误和数据包删除资格，不查询私有实现状态。

## 5. 发布稳定 ResearchArtifact

新增 [`backend/app/research/artifacts.py`](../../backend/app/research/artifacts.py)：

```python
ResearchArtifactWriter.publish(run_id, tables, summary, warnings, runtime_identity)
ResearchArtifactReader.summary(relative_path) -> dict
ResearchArtifactReader.table(relative_path, name) -> DataFrame
```

受控目录中原子发布：

- `manifest.json`、`summary.json`；
- `universes.parquet`、`scores.parquet`、`labels.parquet`；
- `daily_metrics.parquet`、`weekly_metrics.parquet`、`qlib_daily_signal_analysis.parquet`。

manifest 保存 schema version、行数、文件 checksum、逻辑 checksum、runtime identity、warnings 和 float 容差。未知 schema version返回明确错误，不读取猜测或原地升级。

## 6. 实现研究页面和数据包管理画面

遵守 [`frontend/AGENTS.md`](../../frontend/AGENTS.md)，编码前读取已安装 Next.js 16 对应文档。新增 `frontend/app/research-center.tsx`，并从 [`frontend/app/page.tsx`](../../frontend/app/page.tsx) 挂载。

画面提供：

- 默认选择最新 eligible snapshot，输入观察日期范围、126/21 参数；
- 提交前只读展示标签口径、覆盖率门槛和股票池政策；
- 创建、轮询和取消 ResearchRun；
- 展示 phase、日期进度、有效范围、warnings 和失败原因；
- 展示日度/周度 IC、Rank IC、覆盖率、五分组和多空收益；
- 按观察日查看 RankedScores 与 exclusions；
- 展示 QlibDataBundle 状态、大小、构建尝试和删除资格。

新增 [`frontend/__tests__/research-center.test.tsx`](../../frontend/__tests__/research-center.test.tsx)，通过 HTTP 路由 fake 验证用户可以创建研究、查看日度/周度结果与 exclusions，以及预构建/删除数据包，不 mock 内部 React helper。

## 7. 最终验证

按以下顺序验收：

1. [x] 纯研究计算测试；
2. [x] PostgreSQL 全量后端测试；
3. [x] `pyqlib==0.9.7` 原生 provider、表达式和 `SigAnaRecord` 集成测试；
4. [x] 前端 Vitest、ESLint 和 Next.js build；
5. [x] 从空 PostgreSQL 数据库执行 Alembic `upgrade head`，并确认 migration 只有一个 head；
6. [x] 重新生成 [`openapi.json`](../../openapi.json)；
7. [ ] 最终全量后端回归及 `git diff --check`。

若本机 Docker 测试数据库不可用，纯测试和前端测试继续执行，但 PostgreSQL 集成测试必须明确报告为环境阻塞，不能以 SQLite 替代或宣称通过。
