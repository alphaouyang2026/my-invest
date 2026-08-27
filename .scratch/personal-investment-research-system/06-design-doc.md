# 06 系统设计 — Qlib 数据适配与动量因子研究

- 状态：Accepted for implementation
- 日期：2026-08-26
- 配套 ticket：[06 — Qlib 数据适配与动量因子研究](./issues/06-qlib-momentum-research.md)
- 架构决策：[Use immutable Qlib data bundles derived from data snapshots](../../docs/adr/0001-use-qlib-data-bundles.md)

本文记录 `grill-with-docs` 会话 Q1–Q63 的共同决定。实现若要改变本文的领域含义、时间口径或数据身份，必须先修改设计和验收条件；内部类名、表名和函数拆分可以在不改变行为的前提下调整。

---

## 1. 目标与边界

06 建立第一条可复现的 Qlib 因子研究路径：

```text
DataSnapshot
→ QlibDataBundle
→ 每日 ResearchUniverse
→ 6-1 动量与未来收益标签
→ 日度诊断 + 周度黄金口径
→ ResearchArtifact + RankedScores
→ 研究页面
```

用户从一份合格 DataSnapshot 出发，选择研究日期和动量窗口，运行 Qlib 表达式与信号分析，得到可审计的分数、标签、覆盖率、IC、Rank IC 和五分组收益。

### 1.1 本票完成

- 固定并验证 Qlib 运行环境；
- 将点时数据转换为 Qlib 原生 provider 可读的数据包；
- 建立 ResearchExperiment、ResearchRun、ResearchArtifact 和 DataBundleBuildAttempt 的生命周期；
- 用 Qlib 表达式计算默认 126/21 交易日动量；
- 生成日度 5 日标签和周度实际调仓标签；
- 冻结每个因子截面日的 ResearchUniverse；
- 发布日度诊断、周度主指标及 RankedScores；
- 提供创建、进度、取消、结果和数据包管理页面。

### 1.2 明确不做

- 不生成 TargetPortfolio；
- 不运行 Qlib ResearchBacktest；
- 不生成模拟订单、成交、持仓或 Decimal 账务；
- 不训练 LightGBM 或其他模型；
- 不引入 Alpha158/Alpha360 作为本票验收范围；
- 不允许用户提交任意 Qlib 表达式、类路径、module path 或 YAML workflow；
- 不把标签、股票池、研究结果或模型写入共享 QlibDataBundle；
- 不部署 MLflow、Redis 或 MongoDB 服务；
- 不自动清理已发布 ResearchRun 或 ResearchArtifact。

ResearchBacktest 与精确执行的双层比较属于 10；模型预测属于 07；目标组合属于 08；日本市场模拟成交和账务属于 09。

---

## 2. 核心领域对象

| 对象 | 身份与职责 | 可变性 |
|---|---|---|
| DataSnapshot | 一次研究允许读取的点时数据事实 | 不可变 |
| QlibDataBundle | Qlib 原生 provider 可读的市场事实副本 | 发布后不可变，可删除重建 |
| DataBundleBuildAttempt | 构建同一数据包的一次执行尝试 | 终态不可变 |
| ResearchExperiment | “研究什么”的规范化定义 | 不可变，相同定义复用 |
| ResearchRun | 执行一个实验的一次尝试 | 终态不可变，重跑新建 |
| ResearchUniverse | 某个因子截面日实际参与研究的证券集合 | 作为运行产物冻结 |
| ResearchArtifact | 一个运行发布的详细结果 | 发布后不可变 |
| RankedScores | 因子或模型向组合策略提供的统一排名 interface | 只读，不含目标权重 |

### 2.1 Experiment 与 Run

ResearchExperiment 固定研究语义：

- DataSnapshot；
- StockPoolPolicy 及其指纹；
- 因子定义及版本；
- 请求的因子截面日期范围；
- `lookback_days` 和 `skip_days`；
- 日度与周度 LabelWindow 定义；
- FactorCoverage / LabelCoverage 门槛；
- 分组和统计定义。

规范化定义计算实验指纹。指纹完全相同时复用同一个 ResearchExperiment；任一研究语义变化都会产生新实验。

ResearchRun 固定实际执行环境：

- 精确 `pyqlib` 版本；
- 完整依赖锁身份；
- 应用代码版本；
- QlibDataBundle 与导出器版本；
- 开始、结束、状态、日志和资源用量；
- warnings、标量指标和 ResearchArtifact manifest。

同一实验同时最多一个活动运行。重复请求返回现有活动运行；终态后再次运行会创建新的 ResearchRun，并可引用前一次尝试。失败和取消运行不会被重写成成功。

---

## 3. Qlib 运行边界

### 3.1 安装与版本

- 安装固定的 `pyqlib==0.9.7`，Python import 名称为 `qlib`；
- 完整传递依赖进入项目 lockfile；
- 本地 Qlib checkout 只用于源码核查，不作为 editable/path dependency；
- 构建镜像时验证 import、版本、原生 provider 读取、表达式和 `SigAnaRecord`；
- ResearchRun 记录实际 Qlib 与依赖锁身份。

升级 pyqlib 会产生不同的 QlibDataBundle 身份并触发重建，不假设原生 binary 跨版本兼容。

### 3.2 进程 seam

只有后台 worker 初始化并调用 Qlib。FastAPI 请求进程只负责：

- 规范化/复用 ResearchExperiment；
- 创建或查询 ResearchRun；
- 查询 DataBundleBuildAttempt；
- 读取已发布的 ResearchArtifact；
- 请求取消或删除未被活动运行使用的数据包。

调用方通过研究引擎的用例级 interface 工作，不依赖 Qlib 全局初始化、目录、recorder、pickle 或具体类。

---

## 4. QlibDataBundle

### 4.1 身份

QlibDataBundle 的逻辑身份至少包含：

```text
(data_snapshot_id, exporter_schema_version, pyqlib_version)
```

manifest 另记录导出器代码版本、规范化逻辑数据校验值、构建时间、字段 schema、dtype、行数、日期范围、证券数、NaN 统计和文件清单。

QlibDataBundle 是 DataSnapshot 的派生读模型，不是第二份事实源。一个数据包不会原地更新成另一份快照或另一种 schema。

### 4.2 格式

使用 Qlib 原生文件 provider：calendar、instruments 和 feature binary。选择原生 provider 是为了完整复用表达式、Dataset/DataHandler、recorders 以及后续 Alpha/model workflow。

不实现直接 PostgreSQL provider，也不在每次运行中临时拼 DataFrame 绕开 provider。

### 4.3 证券和日历

- Qlib instrument key 使用系统稳定的 `instrument_id`；
- 股票代码、公司名称和来源标识仅是展示/追溯元数据；
- instruments 区间表示该稳定身份在 DataSnapshot 中拥有行情事实的首尾范围；
- 中间缺失交易日保留 NaN，不拆成大量短区间；
- instruments 不表达 Prime、流动性或历史充分性筛选；
- calendar 精确来自 DataSnapshot 绑定的日本 TradingCalendar publication，包括实际开放交易日。

ResearchUniverse 在运行时由 Experiment 的 StockPoolPolicy 逐截面生成并作为 ResearchArtifact 冻结。

### 4.4 字段契约

| Qlib 字段 | 含义 |
|---|---|
| `$open/$high/$low/$close` | J-Quants 复权 OHLC，即 ResearchPrice |
| `$volume` | 复权成交量 |
| `$factor` | `adjusted_close / raw_close` 推导的累计还原因子 |
| 显式 raw OHLCV 字段 | 原始行情，仅供审计或后续研究 |
| `$trading_value` | 原始成交额 |
| 显式 adjustment event 字段 | J-Quants 单日公司行动因子，不映射为 `$factor` |
| 质量状态及原因字段 | 数据质量追溯 |

Qlib 官方语义中 `$factor = adjusted_price / original_price`。不得把 J-Quants 单日 `adjustment_factor` 直接写入 `$factor`。

行级质量映射：

- `untradable` 行的研究行情字段为 NaN，并保留质量状态与原因；
- `excluded` 行仍可使用有效研究字段，原本缺失字段保持 NaN；
- 不前向或后向填充 OHLC、成交量或标签端点；
- 后续模型如需填充，必须由 ResearchExperiment 中显式、可追溯的 processor 完成。

### 4.5 构建与发布

数据包按需延迟构建，也提供显式预构建入口；DataSnapshot 发布不会自动构建所有数据包。

同一逻辑身份可有多个 DataBundleBuildAttempt，但只发布一个 ready 数据包：

```text
queued → building → validating → publishing → ready
                   ↘ failed / cancelled
```

构建写入临时位置，验证全部通过后原子发布。验证至少包括：

- calendar 与 DataSnapshot publication 一致；
- instrument identity 可往返解析；
- 日期、证券、记录范围与预期一致；
- 必需字段、dtype 和 NaN 统计；
- 原始 Decimal 与导出 float32 的抽样容差；
- 规范化逻辑数据校验值；
- Qlib provider 读取 smoke test。

失败尝试保留状态、错误和日志，临时数据删除；重试创建新的 BuildAttempt。ResearchRun 取消不会取消共享数据包构建。

### 4.6 生命周期

- 构建中或被活动 ResearchRun 使用的数据包不可删除；
- 完成运行引用的数据包可以显式删除；
- 删除数据包不删除 ResearchArtifact，既有结果仍可查看；
- 再次执行时按相同身份自动重建；
- 06 不自动执行 LRU 或“只保留最新快照”策略；
- 页面展示状态、大小、最后使用时间和删除资格。

---

## 5. 时间与数据隔离

### 5.1 市场日期

FactorObservationDate 使用东京证券交易所市场日期。FeatureCutoff 是该交易日正式收盘；标签端点按东京市场的实际开放交易日确定。任务和审计时间戳使用 UTC。

周度因子截面是每周最后一个实际交易日，不假设一定为周五。

### 5.2 请求区间与依赖区间

用户选择的是 FactorObservationDate 范围，不是所有读取的硬边界。系统可以：

- 在起点前读取动量预热窗口；
- 在终点后读取已经存在于 DataSnapshot 中的标签窗口。

因历史不足而无法计算因子的首部日期不进入有效因子截面。因整个未来窗口尚未落入 DataSnapshot 的尾部日期不进入评价汇总；其有效因子分数仍可作为 RankedScores 产物发布，并以 `label_not_matured` 标记标签状态。

页面回显请求范围、有效因子范围、有效评价范围及首尾裁剪原因。

### 5.3 两种合法读取权限

- 特征阶段只能读取该截面的 FeatureCutoff 及以前数据；
- 评价阶段只能额外读取 ResearchExperiment 声明的 LabelWindow；
- 标签数据不得进入因子表达式、ResearchUniverse 或任何截面预处理；
- DataSnapshot cutoff 之外的数据在任何阶段均不可读。

时间隔离测试必须从查询与 Qlib handler 两侧证明该约束，而不是只检查页面隐藏。

---

## 6. 动量因子

默认参数：

```text
lookback_days = 126
skip_days = 21
required_history_days = 147
```

对因子截面日 `t`：

```text
momentum(t) = ResearchPrice.close[t - 21]
            / ResearchPrice.close[t - 147]
            - 1
```

等价 Qlib 表达式使用 `$close` 与 `Ref`，窗口参数由已注册的动量定义声明。06 页面只允许调整 `lookback_days` 和 `skip_days`；观察频率、标签和质量门槛只读展示。

单个证券的因子有效条件：

- 两个端点均为正且有效；
- `lookback + skip` 路径中有效 `adjusted_close` 覆盖率至少 90%；
- 证券属于该日 ResearchUniverse；
- 行级质量状态未使所需价格不可用。

不满足时保留证券的股票域成员身份，因子记为 unavailable 并记录原因。缓存层不填充价格。

黄金基线保存原始动量收益和截面平均秩百分位，不 winsorize、不做 z-score。相同原始分数保持相同统计秩；页面展示顺序可以用证券代码稳定排序，但证券代码不改变 IC 或分组。

---

## 7. 标签

标签属于 ResearchExperiment，不属于 QlibDataBundle。

### 7.1 日度诊断标签

对每个开放交易日收盘形成的因子截面：

```text
entry = 下一实际交易日 adjusted_open
exit  = 第六个后续实际交易日 adjusted_open
label = exit / entry - 1
```

即固定五个交易时段的未来复权开盘到开盘收益。

### 7.2 周度黄金标签

```text
entry = 本周因子截面后的下一实际交易日 adjusted_open
exit  = 下一周因子截面后的下一实际交易日 adjusted_open
label = exit / entry - 1
```

它忠实对应周度策略的实际调仓日历；节假日周不强制变成固定五个交易日。

### 7.3 标签缺失

若证券已有因子分数，但 entry 或 exit `adjusted_open` 缺失：

- 保留 ResearchUniverse 成员与因子分数；
- 标签记为 unavailable；
- 记录 `missing_entry_open`、`missing_exit_open` 或其他结构化原因；
- 不用收盘价、最近价格或下一可用价格填充；
- 不因未来停牌、退市或缺失而反向修改当时股票域。

这类证券不参与该截面的 IC 配对，但进入 LabelCoverage 分母和警告统计。

---

## 8. ResearchUniverse 与覆盖率

每个 FactorObservationDate 都以 DataSnapshot 和 ResearchExperiment 固定的 StockPoolPolicy 调用 05 的股票池能力。实际成员、排除原因、政策指纹和校验值作为 ResearchArtifact 发布。

### 8.1 因子覆盖率

```text
FactorCoverage = 有效因子分数数 / ResearchUniverse 成员数
```

截面至少满足：

- FactorCoverage ≥ 90%；
- 有效因子证券数 ≥ 100。

否则截面仍保存和展示，但不进入 IC、Rank IC 或分组汇总。

### 8.2 标签覆盖率

```text
LabelCoverage = 有效未来标签数 / ResearchUniverse 成员数
```

截面至少满足：

- LabelCoverage ≥ 90%；
- 有效标签证券数 ≥ 100。

否则截面仍保存和展示，但不进入相应评价汇总。标签缺失永远不会改变 ResearchUniverse。

---

## 9. 统计口径

### 9.1 IC

- IC：原始动量与未来收益的 Pearson 相关；
- Rank IC：平均秩后的 Spearman 相关；
- 缺失配对在覆盖率门槛通过后按 pairwise valid 计算；
- 因子或标签零方差时相关性为 unavailable，并记录原因，不写成 0。

日度页面展示 IC/Rank IC 时间序列、均值、分布和覆盖率，并明确标记五日标签相互重叠。日度 ICIR 可以保留为 Qlib recorder 原始产物，但不是页面主结论。

周度 IC、Rank IC、ICIR 和 Rank ICIR 是黄金策略主口径。ICIR 定义为：

```text
mean(valid IC) / sample_std(valid IC)
```

不乘 `sqrt(252)` 或其他年化因子，与 Qlib/Pandas 默认定义保持一致。有效周度 IC 少于 2 个时 ICIR 为 unavailable，并产生 `insufficient_weekly_observations` 警告。

### 9.2 五分组收益

- 按平均秩百分位形成五组；
- 相同分数不为追求等人数而强行拆组；
- 每组未来标签使用等权平均；
- 发布五组收益及最高组减最低组收益；
- 若并列导致所需组不存在，多空收益为 unavailable，不写成 0。

### 9.3 Run 成功语义

- 完全没有有效日度因子截面：ResearchRun 失败；
- 存在有效日度截面但周度观察不足：ResearchRun 成功，周度 ICIR unavailable，并携带结构化警告；
- 个别截面覆盖率不足或统计无定义：运行可成功，相关截面不进入对应汇总；
- warnings 同时写入数据库摘要和 artifact manifest，页面及后续比较不得隐藏。

典型 warnings 包含标签损耗、周度观察不足、幸存者偏差、J-Quants Free 历史限制和被裁剪的评价尾部。

---

## 10. 结果发布与 RankedScores

### 10.1 持久化分工

PostgreSQL 保存：

- ResearchExperiment 与定义指纹；
- ResearchRun、状态、运行环境、进度、错误和 warnings；
- 标量摘要；
- ResearchArtifact manifest、schema version、位置和内容校验值；
- QlibDataBundle / BuildAttempt 的身份和状态。

受控本地目录保存不可变 ResearchArtifact：

- manifest 与小型摘要：JSON；
- 每日 ResearchUniverse 与 exclusions：Parquet；
- 有效/无效因子分数与原因：Parquet；
- 有效/无效标签与原因：Parquet；
- 日度/周度 IC、覆盖率和分组收益序列：Parquet。

应用不把 Qlib recorder 的 pickle 或 MLflow 内部目录结构当成长久 interface。Qlib recorder 负责执行分析，应用将稳定结果规范化为上述产物。

产物先写临时位置，全部 schema、行数和校验值验证成功后原子发布。失败或取消运行不发布部分产物，只保留状态与日志。

未知 ResearchArtifact schema version 会返回明确不兼容错误，不猜测列含义，也不原地重写旧产物。

### 10.2 RankedScores

对一个 FactorObservationDate，RankedScores 返回：

- 有效证券的稳定 instrument identity；
- 原始动量分数；
- 平均秩与秩百分位；
- 因子定义与参数；
- 来源 ResearchRun 与 DataSnapshot；
- exclusions 及结构化原因。

RankedScores 不包含目标权重、持仓建议或成交含义。06 的因子产物与 07 的 PredictionRun 都满足该 interface，08 只依赖 RankedScores。

---

## 11. 任务、页面与资源

### 11.1 状态与取消

ResearchRun 使用产品语言展示进度：

```text
queued
→ waiting_for_bundle
→ computing_factors
→ computing_labels
→ evaluating
→ publishing
→ succeeded / failed / cancelled
```

页面回显有效范围、当前处理日期、标签成熟情况、运行耗时和失败原因。取消只终止当前 ResearchRun，不取消共享 DataBundleBuildAttempt；临时研究产物删除。

### 11.2 默认创建体验

研究页面默认：

- 最新的可研究 DataSnapshot；
- 该快照可支持的完整有效因子截面范围；
- `lookback_days=126`；
- `skip_days=21`。

提交前只读展示日度/周度标签、覆盖率门槛、股票池政策、预计数据包构建需求和免费数据限制。

### 11.3 数据包页面

展示每个 QlibDataBundle 的身份、状态、大小、最后使用时间、构建尝试和删除资格。允许预构建及删除非构建中、未被活动运行使用的数据包。

### 11.4 资源控制

- Qlib/BLAS 线程数、worker 内存和数据包目录磁盘预算可配置；
- 构建前估算空间，不足时在写入大文件前失败；
- ResearchRun 记录耗时、峰值内存和产物大小；
- 后台研究不得阻塞普通 API 查询；
- 用户不能通过任意表达式或 class path 绕过资源边界。

---

## 12. 测试与完成条件

### 12.1 快速测试

普通单元测试通过研究引擎 fake 验证：

- Experiment 指纹与 Run 生命周期；
- 时间窗口和东京市场日期；
- 动量端点、90% 路径覆盖和平均秩；
- 日度/周度标签；
- 因子/标签覆盖率门槛；
- IC、Rank IC、ICIR、并列和零方差；
- warnings、取消和原子发布；
- RankedScores 与 exclusions。

### 12.2 Qlib 集成测试

在生产 Python 3.12 容器中安装锁定的 `pyqlib==0.9.7`，验证：

- import 与版本；
- native provider 数据包读取；
- 字段和 `$factor` 语义；
- Qlib 动量表达式；
- Dataset/Handler 的特征与标签隔离；
- `SigAnaRecord` 输出；
- bundle 删除重建；
- artifact 规范化与 schema 读取。

### 12.3 离线黄金数据

提交脱敏的小型真实数据切片，覆盖完整预热和标签窗口，并包含：

- 公司行动及复权价格；
- 缺失开盘价；
- 停牌与行级质量状态；
- 股票域进入/退出；
- 并列动量分数；
- 节假日周度调仓边界。

关键期望值独立推导，不把当前实现输出直接固化成答案；合成数据补充零方差、覆盖率临界值和损坏 manifest 等极端情况。

黄金断言：

- DataBundle 规范化逻辑 manifest、成员身份、日期和原因码完全一致；
- float32 因子、标签和统计使用明确容差；
- 排名与分组一致，并列按设计处理；
- 普通 CI 不访问 J-Quants 或其他网络服务。

---

## 13. 被否决方案

| 方案 | 否决原因 |
|---|---|
| 06 同时运行 ResearchBacktest | TargetPortfolio 到 08 才有正式语义；临时 Qlib 策略会制造第二套组合定义 |
| 直接实现 PostgreSQL Qlib provider | 重复实现 Qlib provider/cache 性能与兼容能力，成本和风险最高 |
| 每次运行临时组装 DataFrame | 绕开 Qlib 原生数据、表达式和 Dataset 生态，数据复制不可复用 |
| 把动态股票池写入 DataBundle | 数据包将依赖实验政策，无法由 DataSnapshot 单独重建 |
| 把标签预计算进 DataBundle | 标签是研究定义，后续不同持有期会复制整套行情 |
| 使用本地 editable Qlib checkout | 当前工作树不是可复现依赖，且部署绑定个人目录 |
| 直接暴露 Qlib pickle/MLflow 文件 | 页面和后续 ticket 会依赖外部库内部格式 |
| 用收盘价填补缺失开盘标签 | 改变持有期和成交假设，掩盖标签损耗 |
| 用未来不可交易状态修改股票域 | 构成前视选择偏差 |
| 将无定义 IC 写成 0 | 混淆“无法计算”和“确认无相关性” |
| 要求 float 逐字节一致 | 把底层实现末位差异误判成研究语义变化 |
