# Qlib 核心采用方案

- 状态：Accepted（架构方向已确认，实施细节随票细化）
- 日期：2026-08-26
- 评估对象：本地检出 `c:\Users\alpha\dev\projects\qlib`（Microsoft Qlib，MIT）
- 结论摘要：**将 Qlib 作为研究、预测、选股、组合构建和研究型回测的核心引擎；本系统保留点时数据、快照、日股市场适配和精确账务，并以适配器把这些能力提供给 Qlib。**

本文所说的“核心采用”不是让 Qlib 接管所有职责，也不是复制 Qlib。目标是在 Qlib 已经成熟的量化研究闭环上建设日本股票能力，把自研集中在本系统真正独特的部分。

---

## 1. 产品目标的调整

系统目标从“实现一个动量策略并完成模拟交易”调整为：

> 建立一个面向日本股票、点时一致且可复现的个人量化研究与投资决策平台，最大化复用 Qlib 的数据表达、特征工程、模型训练、信号分析、选股、组合策略、回测与实验管理能力。

6 个月动量仍是首个黄金基线，用来打通数据和验收链路，但不再定义系统的能力边界。系统应逐步支持：

- Qlib 表达式和 Alpha158/Alpha360 等多因子特征；
- LightGBM、线性模型、时序模型及后续可插拔模型；
- 排序预测、收益预测和风险预测；
- 基于模型分数的选股、规则策略及组合优化；
- 因子、模型、策略、组合和成本的统一实验与比较；
- 从研究结果到可解释目标权重建议的完整链路。

本系统仍只做研究、历史回测和模拟交易，不连接券商，不发送真实订单。

---

## 2. 为什么 Qlib 应当成为核心

Qlib 不是一个“为了少写几行动量 SQL 而引入的库”，而是一套覆盖量化研究生命周期的框架。孤立地比较某一个因子的实现成本，会系统性低估它的价值。

| 研究阶段 | Qlib 提供的能力 | 对本系统的价值 |
|---|---|---|
| 数据与特征 | 表达式引擎、`Dataset` / `DataHandler`、Alpha158/Alpha360 | 从单一动量扩展为可组合、可复用的因子体系 |
| 预测 | 统一模型接口，LightGBM、LSTM、GRU、Transformer 等模型 | 建立收益/排序预测能力，避免自建训练框架 |
| 评价 | IC、Rank IC、ICIR、分组收益、模型与信号记录器 | 统一研究口径并提高实验可比性 |
| 选股策略 | `TopkDropoutStrategy`、`WeightStrategyBase` 等策略接口 | 支持从预测分数到持仓目标的多种映射 |
| Portfolio | 回测执行、账户分析、风险指标与组合分析 | 快速验证组合行为，并为本地精确模拟提供参照 |
| 实验管理 | workflow、recorders、MLflow 集成 | 追踪数据、特征、模型、参数、指标与产物 |

这些能力彼此配合才构成 Qlib 的主要杠杆。系统若只借用个别统计口径，仍需自行搭建特征、训练、评价、选股和组合实验之间的大量连接代码，也会失去 Qlib 生态中新模型和研究方法的可复用性。

---

## 3. 架构职责

### 3.1 总体原则

采用“双层闭环”：Qlib 负责研究决策闭环，本地模块负责可信数据和可执行性闭环。

```text
J-Quants / 本地数据
        ↓
PostgreSQL 点时事实源 + DataSnapshot
        ↓  不可变 QlibDataBundle（Qlib 数据包）
Qlib Dataset / Handler / Feature / Model / Recorder
        ↓
预测分数 → 选股策略 → 目标权重 → Qlib 研究型回测
        ↓
日本市场执行适配 + Decimal 模拟账务
        ↓
成交、持仓、净值、绩效与归因
```

这不是两套相互竞争的系统：

- Qlib 的输出是预测、排名、目标权重、研究回测结果和实验产物；
- 本地执行模块把目标权重转成符合日本市场规则的订单与成交，并形成精确账务；
- 两者由同一个 `DataSnapshot`、策略/模型版本、参数和随机种子关联。

### 3.2 Qlib 核心研究引擎

Qlib 负责：

1. 特征表达、窗口计算和因子组合；
2. Dataset 切分、训练/验证/测试样本组织；
3. 模型训练、推理、预测分数和模型产物；
4. IC、Rank IC、分组收益等信号分析；
5. 选股策略、目标权重与研究型 portfolio 回测；
6. 实验记录、参数比较、模型与结果追踪。

后端通过一个深模块 `QlibResearchEngine` 暴露少量用例级 interface，例如：

- `run_factor_analysis(experiment)`
- `train_model(experiment)`
- `generate_predictions(model_run, as_of)`
- `build_target_portfolio(prediction_run, policy)`
- `run_research_backtest(experiment)`

调用者不直接依赖 Qlib 的目录结构、全局初始化、recorders 或模型类；这些细节留在模块实现内部，便于升级 Qlib 和测试适配器。

### 3.3 PostgreSQL 与 DataSnapshot

PostgreSQL 继续是唯一事实源，负责：

- J-Quants 原始事实、修订历史和发布时间；
- 历史股票池、交易日历和日本市场规则；
- 不可变的 `DataSnapshot` 及回测可复现性；
- 系统任务、模拟订单、成交、持仓和账务结果。

`QlibDataBundle` 是由 `DataSnapshot` 确定性生成、供 Qlib 原生 file provider 读取的不可变数据包，不是第二份事实源。它只保存快照行情事实、日历、instruments 和质量字段，不保存由实验定义的标签或动态研究股票池。数据包身份包含 snapshot id、导出器 schema/版本和 `pyqlib` 版本，并带逻辑内容校验值；可删除、可重建，不得原地更新成另一个快照。该名称特指本系统管理的数据包，不与 Qlib 自身的内存/磁盘缓存机制混称。

研究语义由不可变 `ResearchExperiment` 定义；每次实际执行形成新的 `ResearchRun`，记录代码、依赖锁、`pyqlib`、导出器和运行资源身份。因子研究与模型预测都通过公共 `RankedScores` interface 发布证券原始分数、平均秩、百分位和排除原因，供后续选股与组合模块消费。

### 3.4 日本市场与精确账务

Qlib 内置 region 没有日本，且回测内部广泛使用 float。这些是需要适配的工程事实，不是拒绝整个框架的理由。

- 增加日本交易日历、100 股交易单位、値幅制限、停牌、费用、成交量参与率及结算规则的适配器；
- 使用 Qlib 回测进行快速研究、策略筛选和相对比较；
- 对进入正式比较或目标权重建议的候选策略，用本地 Decimal 执行/账务模块重放；
- 金额、数量、费用、现金和持仓以本地账务结果为准；预测分数、因子值和模型计算允许使用浮点数；
- 对 Qlib 与本地重放的收益、换手、持仓差异设置容差和差异报告，禁止静默分叉。

这样既能充分使用 Qlib portfolio 能力，也不牺牲日本市场可执行性和账务可审计性。

---

## 4. 已知差异及处理方式

| 差异 | 影响 | 处理方式 |
|---|---|---|
| Qlib 原生数据不保存行情修订版本 | 无法单独承担回测复现 | PostgreSQL/DataSnapshot 为事实源；QlibDataBundle 是可重建的数据包 |
| 原生 region 无日本 | 默认交易参数不正确 | 实现日本市场配置与 Exchange/执行适配器，并用固定案例验收 |
| Qlib 账务使用 float | 不满足精确现金账务要求 | 研究回测用 Qlib；正式模拟结果由 Decimal 模块重放并定稿 |
| `trade_unit` 在特定复权价路径可能失效 | 整手约束可能丢失 | 导出原价与复权因子，显式校验字段；本地执行再次强制整手 |
| `risk_analysis` 日频年化因子采用 238 | 日本市场指标有偏差 | 从实际交易日历或实验配置注入年化因子，不沿用固定 238 |
| `TopkDropoutStrategy` 含换股数/最低持有期语义 | 与动量黄金基线不同 | 黄金基线使用自定义 `WeightStrategyBase`；其他实验可显式启用 |
| Qlib 依赖较多 | 镜像、升级和安全维护成本上升 | 当前基线使用 `pyqlib==0.9.7` 并锁定完整依赖；按 Qlib runtime 管理依赖，不要求启用 Redis/MongoDB 服务 |

这些差异均应通过明确的 seam 和验收测试隔离，避免修改 Qlib 核心源码形成长期 fork。只有上游无法扩展且适配器不足以实现日本市场语义时，才考虑维护最小补丁。

---

## 5. 功能映射与采用决策

| 功能 | Qlib 角色 | 本地系统角色 | 决策 |
|---|---|---|---|
| 动量与技术因子 | 表达式引擎、Alpha158/360、信号分析 | 快照供数、展示与审计 | **Qlib 主导** |
| 机器学习预测 | Dataset、模型、训练、推理、recorders | 任务编排、版本绑定、UI | **Qlib 主导** |
| 选股 | 排名预测与策略接口 | 股票池历史、业务约束、解释展示 | **Qlib 主导** |
| 组合构建 | 规则策略、目标权重、研究型 portfolio | 日本市场约束和最终目标权重校验 | **Qlib 主导，适配扩展** |
| 回测 | 快速实验、模型/策略比较 | 点时快照、精确执行重放、审计 | **两层协作** |
| 绩效分析 | Qlib 指标与组合分析 | 日本日历口径、成本分解、最终报告 | **优先复用，校正口径** |
| 模拟账户 | 可用于研究态状态演化 | Decimal 现金、订单、成交、持仓 | **本地主导** |
| 历史重放 | 提供信号和目标组合 | `as_of` 隔离、用户决策分支 | **本地主导，调用 Qlib** |
| 基本面/PIT | `P` / `PRef` 与模型特征 | 披露/修订事实和快照导出 | **协作** |

---

## 6. 实施路线

### Phase A：最小 Qlib 垂直切片

1. 在 Python 3.12 worker 中安装 `pyqlib==0.9.7` 并锁定完整依赖；
2. 从一个 `DataSnapshot` 构建只含快照市场事实的不可变 `QlibDataBundle`；
3. 由 `ResearchExperiment` 定义标签、观察区间、动量参数和股票池策略，由 `ResearchRun` 冻结实际成员与运行身份；
4. 用 Qlib 表达式实现 6-1 动量，产出日度诊断、周度主评价和 `RankedScores`，复现黄金样本；
5. 保存 Parquet 明细及 JSON manifest/summary，并验证数据包可确定性重建。

Phase A 对应 ticket 06，只验证因子研究 seam，不生成 `TargetPortfolio`，不运行 `ResearchBacktest`，也不产生订单或账务。组合与双路径回测在后续阶段完成。

### Phase B：多因子与模型化选股

1. 引入 Alpha158/Alpha360 的适用因子并记录可用性；
2. 建立时间序列训练/验证/测试切分，禁止随机打散造成泄漏；
3. 增加 LightGBM 排序或收益预测基线；
4. 比较动量、线性多因子和机器学习模型的样本外 IC、换手和成本后收益；
5. 前端支持查看特征、模型、预测、选股原因和实验比较。

### Phase C：Portfolio、双路径回测与基本面扩展

1. 从 `RankedScores` 构建 `TargetPortfolio`，增加评分加权、风险约束和组合优化策略；
2. 以同一实验运行 Qlib `ResearchBacktest` 和本地 Decimal `ExecutionReplay`，保存差异报告；
3. 建立风险模型与组合暴露分析；
4. 接入带披露时间和修订语义的基本面数据，使用 `P` / `PRef`；
5. 扩展走步训练、模型滚动更新和稳定性监控。

---

## 7. 验收标准

- 同一 `DataSnapshot`、Qlib/模型/策略版本、参数和随机种子可复现实验；
- Qlib 不能读取快照 cutoff 之后的数据，训练/验证/测试区间无泄漏；
- 现有动量黄金因子基线可由 Qlib 表达式跑通，并通过 `RankedScores` 发布；
- 至少一个 Qlib 模型完成训练、预测、选股、组合和回测闭环；
- 每个目标组合都可由本地日本市场执行模块重放；
- Qlib 研究回测与本地精确重放的差异被量化、解释并保存；
- 模型结果可追溯到数据快照、特征集、标签、代码、参数和模型产物；
- QlibDataBundle 删除后能够从 PostgreSQL 中的对应 `DataSnapshot` 确定性重建。

---

## 8. 明确不做

- 不把 Qlib 的数据目录提升为事实源；
- 不为追求“全面采用”而删除现有点时修订和快照机制；
- 不使用 Qlib float 结果直接记载最终现金、费用和持仓账务；
- 不把动量或 Alpha158 的全量因子等同于有效策略，所有结论仍需样本外和成本检验；
- 不接入 Qlib 在线交易或 RL 模块，除非产品范围未来明确变化；
- 不在没有必要时 fork Qlib 核心实现。

相关架构决定见 [ADR-0001：从 DataSnapshot 构建不可变 QlibDataBundle](../adr/0001-use-qlib-data-bundles.md)。

---

## 附：已核实的 Qlib 源码位置

| 能力或差异 | 位置 |
|---|---|
| 表达式算子 | `qlib/data/ops.py` |
| Alpha158 / Alpha360 | `qlib/contrib/data/loader.py` |
| PIT 财报期间数据 | `qlib/data/pit.py` |
| instruments 多段区间 | `qlib/data/storage/file_storage.py:203-218` |
| 交易成本模型参数 | `qlib/backtest/exchange.py:38-55` |
| 复权价路径与 trade unit | `qlib/backtest/exchange.py:227-228` |
| 现金延迟结算 | `qlib/backtest/position.py:201-222`、`:377` |
| 原生 region | `qlib/config.py:316-327` |
| 风险指标与年化因子 | `qlib/contrib/evaluate.py:26`、`:48-56` |
| IC / ICIR / Rank IC | `qlib/workflow/record_temp.py:295`、`:323-328` |
| TopkDropout 策略语义 | `qlib/contrib/strategy/signal_strategy.py:75-107` |
| 模型实现 | `qlib/contrib/model/` |
| 依赖清单 | `pyproject.toml:27-57` |
