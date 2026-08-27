# 动量研究工作流（ResearchWorkflow）

- 状态：As Built
- 日期：2026-08-28
- 实现：[`backend/app/research/workflow.py`](../../backend/app/research/workflow.py)
- 关联设计：[Qlib 评估](./qlib-evaluation.md)、[数据模型](./data-model.md)
- 架构决定：[ADR-0001 使用不可变 Qlib 数据包](../adr/0001-use-qlib-data-bundles.md)
- 影响范围：worker 研究任务、ResearchRun 生命周期、ResearchArtifact 产物

本文描述 `ResearchWorkflow.execute()` 一次运行从入口到产物的实际流程，以及可以从日志观察到什么。它记录代码现在的行为，不是目标状态。

## 1. 边界

`execute(run_id)` 是**只在 worker 进程内运行**的用例。FastAPI 请求进程从不调用它，也从不初始化 Qlib——这条进程 seam 由 [`qlib_runtime.py`](../../backend/app/research/qlib_runtime.py) 守住，全仓只有 `read_features()` 会调 `qlib.init()`。

输入是一个 `ResearchRun` 的主键。运行所需的一切都从它出发解析：

```text
ResearchRun
  └─ ResearchExperiment          不可变研究定义（观察区间、lookback、skip）
       └─ DataSnapshot           point-in-time 事实源
            ├─ TradingCalendar publication
            └─ QlibDataBundle    由快照派生的只读副本，Qlib 直接读
```

工作流**不接受任何表达式、类路径或 workflow 配置**。因子定义由 `lookback_days` / `skip_days` 两个参数确定，其余是注册死的。

## 2. 阶段状态机

```text
queued
  → waiting_for_bundle      确保数据包存在（可能触发一次完整导出）
  → computing_factors       逐日构建股票池 → 读 Qlib 特征 → 打分
  → computing_labels        日度与周度前瞻收益标签
  → evaluating              逐截面 IC / Rank IC / 五分组
  → publishing              原子发布 ResearchArtifact
  → succeeded | failed | cancelled
```

每次转换都由 `_phase()` 完成：写状态、提交、并记录**上一个阶段的墙钟耗时**。这是定位"慢在哪一步"的唯一依据，见第 7 节。

## 3. 各阶段做什么

### 3.1 waiting_for_bundle

调用 `QlibDataBundleBuilder.ensure(snapshot, task_id=run.task_id)`。

- 若该 `(snapshot, exporter_schema, pyqlib_version)` 三元组已有 `ready` 数据包且目录仍在，直接复用（日志 `qlib_bundle.reused`）；
- 否则**在本次运行内**完整导出一份，可能耗时分钟级。

注意数据包构建的 `DataBundleBuildAttempt` 记在**研究运行自己的 `task_id`** 上。这是 worker 崩溃恢复必须同时关闭两类行的原因（见 [`recover_momentum_research`](../../backend/app/worker/tasks.py)）。

随后 `run.bundle_id` 落库，并检查一次取消请求。

### 3.2 computing_factors

分三步，是整个运行最重的阶段。字段清单与打分规则见第 9 节。

**逐日构建股票池。** 对观察区间内每个开市日调用 `build_stock_pool()`，用 point-in-time 名册回答"当天可投资的是哪些证券"。策略从 `DEFAULT_POLICY` 派生，只覆盖两个字段：

```python
required_history_days = lookback_days + skip_days
required_bar_offsets  = (lookback_days + skip_days, skip_days)
```

即因子两个端点所在的交易日必须有行情，否则该证券当日出局。抛 `StockPoolError` 的日期被记入 `skipped_dates` 并跳过，不中断运行；`run.processed_dates` 每日提交，前端进度条读的就是它。**取消请求在这个循环里逐日检查**——这是运行开始后唯一会响应取消的地方。

**读 Qlib 特征。** 一次性读入整个日历范围的三列：

```text
$close, $open, Ref($close, skip)/Ref($close, lookback+skip)-1
```

然后 `_matrix()` 把 Qlib 的 (instrument, datetime) 长表转成日期 × 证券的宽表。

**打分。** 逐日调用 `calculate_momentum_scores()`，再用 Qlib 表达式的结果**覆盖** `raw_score`，最后在有效证券内重排名。

这个覆盖是有意的交叉校验：Python 侧负责**有效性判定**（端点价格为正、`lookback+skip` 路径覆盖率 ≥ 90%、结构化 `factor_reason`），Qlib 侧负责**数值本身**。两条独立路径算同一个因子，任何一侧的口径漂移都会在结果里显形。

### 3.3 computing_labels

- **日度标签**：次日开盘买入，第六个后续开盘卖出（五个交易日持有）。尾部不足六日的观察日标签未成熟，记 NaN，不用收盘价补。
- **周度标签**：`_weekly_observations()` 按 ISO 周取每周**最后一个**已定价观察日，作为实际调仓截面。

两者都与 `universe_rows` 做 inner join，因此标签只覆盖当日股票池成员——池外证券不会因为有行情就混进评估。

完整口径与失效原因见第 10 节。

### 3.4 evaluating

- `_qlib_signal_analysis()` 把（分数, 日度标签）配对喂给 Qlib 的 `SigAnaRecord`，得到官方口径的 IC / Rank IC；
- `_metrics()` 逐截面调 `evaluate_cross_section()`，产出覆盖率、Pearson IC、Spearman Rank IC、五分组收益和多空收益，日度周度各一份；
- 门槛：`factor_coverage ≥ 0.90` 且 `valid_factor_count ≥ 100` 的日度截面才算有效，**一个都没有就整体失败**；
- `summarize_ic()` 在**周度** IC 序列上给出均值和未年化 ICIR。无法计算时返回 `None` 加结构化原因（`no_valid_observations` / `insufficient_observations` / `zero_variance`），绝不写 0——"算不出来"和"确认无相关性"是两回事。

各评价值的定义与取值含义见第 11 节。

`group_returns` 在这里由 `flatten_group_returns()` 摊平成 `group_return_1..N` 列。整数键的映射写不进 Parquet（Arrow 的 struct 字段名与 map 键必须是字符串），而领域层保留整数键是因为多空收益要做 `group_returns[N] - group_returns[1]`。

### 3.5 publishing

`ResearchArtifactWriter.publish()` 写临时目录后**原子改名**，七张表：

| 表 | 内容 |
|---|---|
| `universes` | 每个观察日的股票池成员及成交额 |
| `exclusions` | 被剔除的证券及结构化原因 |
| `scores` | 原始分数、平均秩、百分位、路径覆盖率、失效原因 |
| `labels` | 日度与周度标签合并 |
| `daily_metrics` / `weekly_metrics` | 逐截面评估结果 |
| `qlib_daily_signal_analysis` | Qlib `SigAnaRecord` 的 IC / Rank IC |

外加 `summary.json` 和带 checksum 的 `manifest.json`。发布成功后才写 `ResearchArtifact` 行并置 `run.status = succeeded`。

### 3.6 warnings

`free_data_limit` 恒有（J-Quants Free 的历史长度限制推断能力）；`insufficient_weekly_observations` 当有效周度 IC 少于 2 个；`trimmed_observation_dates` 当有日期被股票池规则跳过。

## 4. 取消

`cancel_requested` 是请求，不是即时终止。`_check_cancel()` 只在两处调用：数据包就绪后一次，以及**股票池循环内逐日一次**。

因此取消在 `computing_factors` 之后不再生效——`computing_labels` / `evaluating` / `publishing` 期间提交的取消要等运行自然结束。考虑到这三个阶段合计约占总耗时的 20%（第 7 节），当前可以接受，但这是已知限制而不是设计意图。

命中取消时 `_cancel()` 在**同一事务**内把 run 和 task 都置为 `cancelled`，然后 `execute()` **正常返回** `{"cancelled": True}`，不向上抛。runner 因此看到一个已终态的 task，不会覆盖它。

## 5. 失败

`except Exception` 把 run 置 `failed`、`error_code = "research_failed"`、`error_summary` 记异常文本，提交后**重新抛出**。runner 的 `_run_task` 接住并把 task 也置 `failed`。

产物是最后一步原子发布的，所以任何未到 `publishing` 的失败**不会留下半份产物**——重跑即恢复，且因为运行对快照是确定性的，重跑不需要任何清理。

进程被硬杀（OOM、容器重启）时这段代码根本不执行，善后由 worker 启动时的孤儿恢复完成。

## 6. 可观测性

`execute()` 入口用 `bind_contextvars` 绑定 `research_run_id` 和 `experiment_id`，`finally` 解绑。导出器、股票池、Qlib seam 都在很深的调用栈里打日志，靠这个才能对上是哪次运行。

`info` 级是阶段边界和每轮一次的里程碑；`debug` 级是逐日/逐票明细（`LOG_LEVEL=DEBUG` 开启）。structlog 的过滤 logger 会把关闭级别的调用变成真正的空函数，所以默认级别下逐日日志零开销。

关键事件：

```text
research_run.started / phase / bundle_ready
research_run.universe_progress（每 25 天） / universe_built
research_run.features_read / scores_built / labels_built / evaluated
research_run.artifact_published / succeeded | cancelled | failed
qlib.initialized（含 provider_uri——"到底读的哪个数据包"）
qlib_bundle.*（复用、构建各阶段、发布、失败）
```

## 7. 实测特征

一次真实运行（快照 `11e402a7`，2024-12-26 → 2026-05-26，126/21，4719 只证券，1459 个开市日）：

| 阶段 | 耗时 | 备注 |
|---|---|---|
| waiting_for_bundle | 0.02s | 复用已有数据包；首次构建约 90s |
| computing_factors | 126.4s | 其中逐日股票池约 118s，读 Qlib 特征仅 4s |
| computing_labels | 19.9s | |
| evaluating | 9.0s | |
| publishing | ~1s | 产物 8.06 MB |
| **合计** | **155s** | 峰值 RSS 1.73 GB |

产出规模：340 个观察日、233,718 行股票池成员、312,607 行剔除记录、339 个日度截面、75 个周度截面。

**性能上第一个该看的地方是逐日股票池**——它占了整个运行的四分之三，而每天都在重复扫描大体重叠的名册与行情。

内存的大头已经不是数据导出（那部分改成流式后与总量无关），而是工作流自己持有的几张全量表：`universe_rows`、`exclusion_rows`、`scores`。它们要活到 `publishing` 才能释放。

## 8. 已知限制

1. **取消在 `computing_factors` 之后失效**（第 4 节）。
2. **逐日股票池未做缓存**，相邻交易日的池子高度重叠却完全重算。
3. **`scores` / `universe_rows` / `exclusion_rows` 全量驻留内存**至发布，规模随观察区间线性增长。
4. **`peak_memory_bytes` 在非 Unix 平台为 `None`**——`resource` 模块不可用时不伪造 0。

---

## 9. computing_factors 细节

### 9.1 读了哪些 Qlib 特征

一次 `read_features()` 调用，三列，`instruments="all"`，时间范围是**整个日历**（`calendar[0]` → `calendar[-1]`）而不是观察区间——因子要回看 `lookback+skip` 个交易日，标签要前看，两端都需要观察区间之外的行情。

| 字段 | 语义 | 用途 |
|---|---|---|
| `$close` | 复权收盘价（ResearchPrice） | 有效性判定：端点价格、路径覆盖率 |
| `$open` | 复权开盘价 | **仅**用于标签的买入/卖出价 |
| `Ref($close, skip)/Ref($close, lookback+skip)-1` | 动量表达式 | 最终写入 `raw_score` 的数值 |

Qlib 的 `Ref($close, N)` 是"N 个交易日之前的收盘价"，所以默认 126/21 展开为：

```text
Ref($close, 21) / Ref($close, 147) - 1
```

即 `close[t-21] / close[t-147] - 1`。跳过最近 21 个交易日是动量因子的标准做法，避开短期反转效应。

数据包的字段契约里还有 `$high`、`$low`、`$volume`、`$factor`、raw OHLCV、`$trading_value`、质量状态等，**本工作流一概不读**。

`_matrix()` 把 Qlib 返回的 (instrument, datetime) 多级索引长表 `unstack` 成日期 × 证券的宽表，并把索引从 `Timestamp` 归一成 `date`、列名转成字符串，得到 `closes` / `opens` / `qlib_scores` 三张矩阵。

### 9.2 打分逻辑

逐观察日进行。当日候选 = 股票池成员 ∩ 数据包中存在的证券（`available`）；候选为空则跳过该日。

**第一步：`calculate_momentum_scores()` 判定有效性**（[`factor.py`](../../backend/app/research/factor.py)）

以 `required = lookback + skip = 147` 为准：

```text
start      = close[t-147]                        起点
end        = close[t-21]                         终点
raw_score  = end / start - 1                     Python 侧的动量
coverage   = notna(close[t-147 .. t-1]) 的比例    147 个交易日的路径覆盖率
```

有效性两个条件同时成立：

1. `start > 0` 且 `end > 0` 且 `raw_score` 非 NaN；
2. `coverage ≥ 0.90`。

历史长度不足 147 个交易日的观察日**整日跳过**，不产出任何行。

失效的证券**仍然保留一行**，分数字段为空，但带 `path_coverage` 和结构化 `factor_reason`：

| `factor_reason` | 含义 |
|---|---|
| `invalid_factor_endpoint` | 两个端点之一缺失、非正或算不出 |
| `insufficient_path_coverage` | 端点有效，但 147 日窗口内有效收盘价不足 90% |
| `null` | 有效 |

这是前端 exclusions 面板"为什么这只票今天没有分数"的数据来源。

**第二步：用 Qlib 表达式覆盖数值**

```python
scored["raw_score"] = scored["instrument_id"].map(qlib_scores.loc[day].to_dict())
```

**第三步：在有效集合内重排名**

```python
valid = scored["raw_score"].notna() & scored["factor_reason"].isna()
ranked = scored.loc[valid, "raw_score"].rank(method="average")
scored.loc[valid, "average_rank"]    = ranked
scored.loc[valid, "rank_percentile"] = ranked / len(ranked)
```

`rank` 升序、并列取平均秩，除数是**当日有效证券数**。因此 `rank_percentile` 越接近 1 动量越强，越接近 0 越弱。

分母只算有效证券，是为了让百分位在不同日期之间可比——用当日全池做分母的话，覆盖率波动会直接污染排名分布。

**为什么要两条路径算同一个数**

Python 侧从 `closes` 矩阵按位置取端点，Qlib 侧在自己的表达式引擎里做 `Ref`。两者读的是同一份数据包，结果理应一致。让 Python 管有效性、Qlib 管数值，等于给因子口径加了一道交叉校验。

> **注意**：代码目前不比较两者的差异，只是覆盖。若 Python 判定有效而 Qlib 返回 NaN，该行会落在 `valid` 之外，`average_rank` / `rank_percentile` 会保留第一步的旧值而不是清空——这是一个已知的不一致，见第 8 节。

## 10. computing_labels 细节

标签衡量的是"按这个分数在观察日之后建仓，实际赚到多少"。两个频率共用同一套结构，差别只在持有区间。

**买入卖出都用开盘价**，且都在观察日**之后**。观察日当天的收盘价参与打分，用它成交就是用尚未可知的信息交易；用次日开盘则是决策日收盘后下单、次日开盘成交的现实口径。

### 10.1 日度标签（五个交易日持有）

```text
观察日 t
买入   open[t+1]          次日开盘
卖出   open[t+6]          第六个后续开盘
label  open[t+6] / open[t+1] - 1
```

成熟条件是 `t+6` 必须落在日历内。区间尾部不满足的观察日**不用收盘价或任何替代价格补齐**——那会改变持有期和成交假设，把标签损耗藏起来。

### 10.2 周度标签（实际调仓到实际调仓）

先由 `_weekly_observations()` 按 ISO 周（`isocalendar()[:2]`）取每周**最后一个**已定价观察日，作为该周的调仓截面。然后：

```text
本周调仓日 t，下周调仓日 t'
买入   open[t+1]
卖出   open[t'+1]
label  open[t'+1] / open[t+1] - 1
```

即"这周决策后的开盘买入，持有到下周决策后的开盘卖出"。这样持有期就是真实的调仓间隔，而不是固定的 5 或 7 天——遇到长假自动变长，符合实际。

序列的**最后一个**周度观察日没有下一个调仓日，恒为未成熟。

### 10.3 输出与失效原因

两个函数都对 `opens` 的**每一列**（数据包中全部证券）产出一行，随后在工作流中与 `universe_rows` 做 inner join 收窄到当日股票池成员。

| 列 | 含义 |
|---|---|
| `observation_date` | 观察日（决策日） |
| `instrument_id` | 证券稳定身份 |
| `label_frequency` | `daily` / `weekly` |
| `entry_date` / `exit_date` | 实际买入、卖出所用的开盘价日期 |
| `label` | 前瞻收益；无法计算时为空 |
| `label_reason` | 为空时的结构化原因 |

| `label_reason` | 含义 |
|---|---|
| `label_not_matured` | 持有区间尚未走完（区间尾部，或最后一个周度截面） |
| `missing_entry_open` | 买入日开盘价缺失或非正（停牌、`untradable` 写 NaN） |
| `missing_exit_open` | 卖出日开盘价缺失或非正 |
| `null` | 标签有效 |

"标签算不出来"和"标签为零"被严格区分——后者是一个真实的持平收益。

## 11. evaluating 各评价值的含义

每个截面调用 `evaluate_cross_section(day_scores, day_labels, universe_size=len(pools[day].members))`，默认门槛 `min_coverage=0.90`、`min_valid_securities=100`、`group_count=5`。

### 11.1 覆盖率与计数

| 指标 | 定义 | 读法 |
|---|---|---|
| `factor_coverage` | 有分数的证券数 / **当日股票池规模** | 这天有多大比例的池子能打出分 |
| `label_coverage` | 有标签的证券数 / **当日股票池规模** | 这天有多大比例的池子能算出收益 |
| `valid_factor_count` | 有分数的证券数 | 覆盖率的分子 |
| `valid_label_count` | 有标签的证券数 | 同上 |
| `paired_count` | 同时有分数**和**标签的证券数 | IC 实际使用的样本量 |

分母是**当日冻结的股票池规模**，不是"出现在表里的行数"。这是 `evaluate_cross_section` 那句 "without changing its denominator" 的意思：如果分母随可用数据缩水，覆盖率永远好看，门槛也就形同虚设。

`factor_coverage < 0.90` 或 `valid_factor_count < 100` 时记 `insufficient_factor_coverage`，标签侧同理记 `insufficient_label_coverage`，并**直接返回**——IC、Rank IC、分组全部为空。样本太少的相关系数没有解释力，给出来只会被当真。

### 11.2 IC 与 Rank IC

| 指标 | 定义 | 读法 |
|---|---|---|
| `ic` | `corr(raw_score, label)`，Pearson | 因子值与前瞻收益的线性相关 |
| `rank_ic` | `corr(average_rank, rank(label))`，在秩上做 Pearson，即 Spearman | 因子**排序**与收益排序的一致性 |

日频 IC 的量级通常很小，0.02–0.05 就算有信号。**Rank IC 更稳健**：它不受少数极端收益拖动，而组合构建实际用的也是排序而非分数本身。

配对样本中 `raw_score` 或 `label` 的**不同取值少于 2 个**时，记 `zero_variance`，两者都为空——常数序列的相关系数在数学上无定义。

### 11.3 五分组与多空收益

```python
group = min(5, max(1, ceil(rank_percentile * 5)))
group_returns[g] = mean(label of group g)
long_short_return = group_returns[5] - group_returns[1]
```

`rank_percentile` 升序，所以 **第 5 组是动量最强的一档，第 1 组最弱**。

| 指标 | 读法 |
|---|---|
| `group_return_1..5` | 各档的平均前瞻收益；单调递增说明因子在整个分布上都有区分度，而不只是靠尾部 |
| `long_short_return` | 做多最强档、做空最弱档的收益差，因子强度最直接的度量 |

任一极端档缺失时 `long_short_return` 为空并记 `missing_extreme_group`。

**这不是回测结果**：没有交易成本、没有持仓约束、等权、且允许做空。它衡量的是因子的区分能力，不是可实现的收益。真正的组合语义要到 ticket 08 才有。

### 11.4 unavailable_reasons

| 原因 | 触发条件 |
|---|---|
| `insufficient_factor_coverage` | 因子覆盖率 < 90% 或有效证券 < 100 |
| `insufficient_label_coverage` | 标签覆盖率 < 90% 或有效证券 < 100 |
| `zero_variance` | 配对样本中分数或标签是常数 |
| `missing_extreme_group` | 第 1 组或第 5 组为空 |

一个截面可以同时命中多条。**任何无法计算的指标一律为 `null`，绝不写 0**——`0` 表示"确认无相关性"，`null` 表示"算不出来"，把后者写成前者是在编造结论。

### 11.5 跨截面汇总

`summarize_ic()` 作用在**周度** IC 序列上（日度截面只用于有效性门禁和产物，不进汇总）：

| 字段 | 定义 | 读法 |
|---|---|---|
| `count` | 非空 IC 的个数 | 汇总的样本量 |
| `mean` | IC 均值 | 因子的平均预测力 |
| `icir` | `mean / std(ddof=1)`，**未年化** | 信号的稳定性：IC 均值相对其波动有多大 |

`icir` 刻意不年化——年化需要一个观察频率假设，而周度截面数量随观察区间和交易日历变化，乘一个 `√52` 只会制造一个看起来精确的假数。

三种无法汇总的情形：`no_valid_observations`（一个有效 IC 都没有）、`insufficient_observations`（少于 2 个，无法算标准差）、`zero_variance`（标准差为 0）。

### 11.6 运行级门禁

```python
valid_daily = [m for m in daily_metrics
               if m["factor_coverage"] >= 0.90 and m["valid_factor_count"] >= 100]
if not valid_daily:
    raise RuntimeError("No valid daily factor cross-section")
```

截面级门槛在运行级再判一次：**一个有效日度截面都没有，整个运行失败**，而不是发布一份全是空指标的产物。
