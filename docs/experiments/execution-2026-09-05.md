# 实验执行记录：2026-09-05

执行依据：[qlib-oom-mitigation-plan.md](qlib-oom-mitigation-plan.md) 第 6 节。本次完成 D 步（真实 Alpha158 对照）和 F 步（在新代码身份下重跑完整 24 组）。E 步的资源验收另见 [Alpha360 资源验收记录](alpha360-resource-acceptance.md)。未修改训练代码、实验配置、原始参考文件或数据库；未发布模型、未执行交易。

容器本次会话曾重启，cgroup `oom_kill` 计数从此前记录的 2 归零。以下三次运行的前后计数均为 `0 → 0`，无新增 OOM。

## D 步：原始基线在新代码身份下复现

参考文件由 `docker compose cp` 放入容器卷 `/app/var/reference_result_3.txt`，SHA256 `26d18fe4acb2647dffdf0c5854389a27d8bfd019641da988227e860ed26426fe`，与 [execution-2026-09-04.md](execution-2026-09-04.md) 记录一致。

新基线目录 `var/experiment-baseline/e1fbf66b10fc3d5f8ed6`，旧目录 `bfad9a7eb2a30f175da7` 保留。身份变化来自代码摘要，配置未改。

`comparison.json` 返回 `matches=true`、`differences=[]`、绝对容差 `1e-9`。与旧产物逐项比对：

| 产物 | 形状 | SHA256 相同 | 最大绝对差 |
|---|---|---|---:|
| predictions.parquet | (70099, 8) | 是 | 0 |
| daily_ic.parquet | (92, 20) | 是 | 0 |
| feature_importance.parquet | (790, 6) | 是 | 0 |

`best_iterations` 仍为 `[1, 40, 27, 1, 54]`；`summary.json` 顶层键集合相同，5 个浮点标量中只有 `elapsed_seconds` 不同。IC 均值 0.01422344052486983、ICIR 0.19411425703421226、Rank IC 均值 0.02071751705154083、Rank ICIR 0.2903294082246482。

内存记录 `var/baseline-rerun-memory.json`：275 个采样、274.1 秒、峰值 4.585 GiB、最低可用 2.570 GiB、警戒 0 次、越限 0 次。基线按方案约定保留旧 direct CLI 语义，不走分片路径，仍是全量读取，因此峰值高于分片实验；它在 5.5 GiB 警戒线以下，不构成资源问题，也不能当作分片路径的内存证据。

## D 步：结构首组新旧对照

新结构实验 `var/experiment-search/1d49ec752e05c902cb5d`，旧的 `5e16ac1eea9e3be0cc90` 及其中断证据、两个陈旧锁原样保留。

计划核对：`fold_plan.json` 与旧实验逐字段相同；`trial_plan.json` 的 24 个 trial ID 及顺序相同，首组仍为 `804088b1f61ae87cc6f9`。manifest 记录的代码差异只有四个文件：`app/experiments/search_plan.py`、`app/experiments/search_runner.py`、`app/research/qlib_runtime.py`、`scripts/monitor_qlib_memory.py`，与方案 §3 声明的修改范围一致。配置差异为新增的 `feature_batch_size`、`qlib_kernels`、`scan_batch_rows`、`cache_schema_version`。

首组单独运行内存记录 `var/structure-rerun-memory.json`：248 个采样、247.1 秒、峰值 2.923 GiB、最低可用 4.146 GiB、警戒 0 次、越限 0 次。新 Alpha158 分片缓存 `_cache/7c630a3420dc83950016` 在此阶段约 207 MB，旧全量缓存 `_cache/3408480cbe3c9bb15e3b` 为 2.1 GB。

### 12 组 Alpha158 全量对照

旧实验完成的 12 组在新实验中全部完成，逐组比对结果一致：

- `predictions.parquet`、`daily_metrics.parquet`、`learning_curves.parquet`、`feature_importance.parquet` 四个产物 **SHA256 逐字节相同**。
- `metrics.json` 除 `elapsed_seconds` 和 `folds` 外的所有汇总键完全相等，包括 `rank_ic_mean`、`ic_mean`、`icir`、`rank_icir`、`worst_fold`、`without_best_fold`、`coverage`、`valid_dates`、`expected_dates`。
- 逐 Fold 的 `best_iteration`、`evaluated_rounds`、`train_rows`、`valid_rows`、`test_rows`、`best_valid_l2`、`best_valid_rank_ic` 全部相等。

### 唯一超出容差的差异：`constant_baseline_l2`

12 组的每个 Fold 上，诊断字段 `constant_baseline_l2` 相差约 5e-8，超过约定的 1e-9 容差。以首组 Fold 1 为例：

| 来源 | 值 |
|---|---|
| 旧实验记录 | 0.997630228138108 |
| 新实验记录 | 0.9976301789283752 |
| 用新路径标签按 float32 重算 | 0.9976301789283752（与新值 bit 级相同） |
| 用同一标签转 float64 重算 | 0.9976302289695032（与旧值相差 8.3e-10，在容差内） |

成因已定位：分片路径按方案 §5.1 将标签固定为 `float32`，旧的 pandas 路径为 `float64`；该字段由 `np.mean((vy - ty.mean())**2)` 在 Python 侧计算，直接继承标签精度，float32 与 float64 结果相差 5.004e-08。

影响范围：仅此一个诊断字段。LightGBM 内部标签本就是 float32，两条路径喂给它的数值一致，因此预测、学习曲线、特征重要性、最佳迭代和全部选择用指标都逐字节相同。该字段不参与早停、不参与候选选择、不出现在报告排名中。

这是方案自身规定的类型变更带来的可预期精度差异，不是回归；按"发生差异先定位，不通过放宽容差掩盖变化"的要求记录在此，未修改容差。

## F 步：完整 24 组

在新实验 `1d49ec752e05c902cb5d` 上执行 `--resume`，返回 `{'attempted': 23, 'completed': 24, 'failed': 0, 'planned': 24}`，`report_qlib_experiment.py` 状态 `complete: 24/24`，`failed` 为空。

恢复语义核验：正式多-trial 计划的恢复允许继续运行 pending trial，因此不以 `attempted=0` 为条件。恢复前对已完成的 `804088b1f61ae87cc6f9` 保存了快照，恢复后比对确认 12 个文件校验和、`attempts=['attempt-0001']` 与 `success` 指针全部未变，也没有新增文件。

内存记录 `var/structure-full-memory.json`：1052 个采样、1051.4 秒、峰值 4.489 GiB、最低可用 3.121 GiB、警戒 0 次、越限 0 次、`resource_limit=false`。运行结束后新缓存 `_cache/7c630a3420dc83950016` 约 749 MB（含 Alpha158 与 Alpha360 两套分片），旧全量缓存仍为 2.1 GB。

### 完整结果

| 特征 | label | 训练窗口 | 早停 | 平均 Rank IC | 最差 Fold | 去掉最好 Fold |
|---|---:|---|---|---:|---:|---:|
| alpha158 | 5 | 252 日 | l2 | 0.024460 | -0.002548 | -0.000710 |
| alpha158 | 5 | 252 日 | rank_ic | 0.027931 | 0.013343 | 0.016757 |
| alpha158 | 5 | expanding | l2 | 0.046630 | 0.018064 | 0.039934 |
| alpha158 | 5 | expanding | rank_ic | 0.035033 | 0.007650 | 0.029777 |
| alpha158 | 10 | 252 日 | l2 | 0.064899 | -0.044007 | 0.048502 |
| alpha158 | 10 | 252 日 | rank_ic | 0.051535 | -0.036101 | 0.035081 |
| alpha158 | 10 | expanding | l2 | 0.093035 | -0.009173 | 0.068951 |
| alpha158 | 10 | expanding | rank_ic | 0.086809 | -0.000042 | 0.059393 |
| alpha158 | 20 | 252 日 | l2 | 0.066109 | 0.023078 | 0.054723 |
| alpha158 | 20 | 252 日 | rank_ic | 0.072962 | 0.023078 | 0.058149 |
| alpha158 | 20 | expanding | l2 | 0.088768 | 0.004701 | 0.067261 |
| alpha158 | 20 | expanding | rank_ic | 0.097683 | 0.011925 | 0.077602 |
| alpha360 | 5 | 252 日 | l2 | 0.029782 | -0.036839 | -0.004187 |
| alpha360 | 5 | 252 日 | rank_ic | 0.041952 | -0.006294 | 0.018124 |
| alpha360 | 5 | expanding | l2 | 0.012770 | -0.036299 | -0.035376 |
| alpha360 | 5 | expanding | rank_ic | 0.012299 | -0.036299 | -0.036239 |
| alpha360 | 10 | 252 日 | l2 | 0.073231 | -0.019886 | 0.055063 |
| alpha360 | 10 | 252 日 | rank_ic | 0.056501 | -0.019886 | 0.024391 |
| alpha360 | 10 | expanding | l2 | 0.083463 | -0.104520 | 0.040957 |
| alpha360 | 10 | expanding | rank_ic | 0.059588 | 0.033247 | 0.034535 |
| alpha360 | 20 | 252 日 | l2 | 0.068434 | 0.065592 | 0.066961 |
| alpha360 | 20 | 252 日 | rank_ic | 0.068434 | 0.065592 | 0.066961 |
| alpha360 | 20 | expanding | l2 | 0.077864 | -0.026968 | 0.054006 |
| alpha360 | 20 | expanding | rank_ic | 0.072547 | -0.026968 | 0.054006 |

所有 24 组覆盖率均为 100%，共同成熟日期 44 个。

Alpha360 首次完整进入训练。在当前时期，最高的 Alpha158 组合（20 日标签、expanding、rank_ic 早停，0.097683）高于最高的 Alpha360 组合（10 日标签、expanding、l2，0.083463），但后者最差 Fold 为 -0.104520，跨 Fold 稳定性明显更差。20 日标签整体优于 5 日标签的模式在两个特征集上都出现。

这些只描述 44 个共同成熟日期上的开发期表现。样本量不足以支持长期泛化、扣费后收益或特征集优劣的结论；20 日标签存在重叠，区间未做多重搜索校正。候选选择应按既有流程另行执行。

### 关于 trial ID 的说明

`trial_id` 是模型规格（特征集、horizon、训练窗口、早停指标、参数、seed）的摘要，不包含数据身份，因此同一 ID 会出现在不同实验中。本次 `73e81f6789b719ade307` 和 `5d2fc5487cf03d23fe92` 同时出现在结构搜索和 Alpha360 验收实验中，但两处数值不同：

| trial | 验收实验 | 结构搜索 |
|---|---:|---:|
| `73e81f6789b719ade307` | 0.007137 | 0.012770 |
| `5d2fc5487cf03d23fe92` | 0.014326 | 0.012299 |

原因是股票池按已配置特征集的最大回看窗口构建，`max(max_window, 20)`。验收配置只含 alpha360，窗口为 59；结构配置含 alpha158，窗口为 60。多要一天历史使池内行数从 268,703 变为 268,693，policy 指纹不同，逐 Fold 训练行相差 5 行，Fold 3 最佳迭代由 9 变为 13。

数据身份（`data_id` 含 `feature_sets`）已正确将两者分入不同缓存，没有交叉复用。引用结果时必须同时给出实验目录，单独的 trial ID 不是唯一键。

## 遗留项

- 旧结构实验 `5e16ac1eea9e3be0cc90` 及其 `_cache/3408480cbe3c9bb15e3b` 仍各有一个内容为已退出 PID 1266 的 `.running.lock`，按锁规则未自动清理。
- 阶段级内存峰值仍未由监控器计量，监控记录只有时间序列。针对训练阶段的诊断归因见 [Alpha360 资源验收记录](alpha360-resource-acceptance.md)。
- 下一阶段按 [qlib-lightgbm-search-prompt.md](qlib-lightgbm-search-prompt.md) 从结构候选选择进入参数搜索；本记录不做候选晋级。

## 阶段 5：参数搜索（80/80）

依据 [qlib-lightgbm-search-prompt.md](qlib-lightgbm-search-prompt.md) 阶段 4～6，在 F 步产出的结构结果上继续。

### 结构候选选择

从 24 组中选定两个结构，导出至 `configs/experiments/selected_structures.json`，来源实验 `1d49ec752e05c902cb5d`：

| 结构 | trial | 选择理由 |
|---|---|---|
| alpha158 / h20 / expanding / rank_ic | `7a636cb4376aa08bf66d` | 六项标准全部第一或并列第一：平均 Rank IC 0.097683、最差 Fold 0.011925 为正、去掉最好 Fold 0.077602、正值比 0.86、Rank ICIR 1.322、无 best_iteration=1 |
| alpha360 / h20 / 252 日 / l2 | `3292f1ea5c788060a341` | 三个 Fold 几乎一致（0.070/0.067/0.066），是唯一稳定性不依赖 4 日 Fold 的组合；特征集与训练窗口均与前者不同，参数搜索探索面更宽 |

未选 `6602cd1bac24dd9cf61a`（均值第二 0.093035，但最差 Fold 为负 -0.009173、Rank ICIR 0.923 最低）与 `9fa04f18a3d45571b88c`（与第一名仅早停指标不同，两个名额会落在同一结构附近）。`f1aec0092e7b9b7cdde6` 与 `3292f1ea` 指标完全相同，按"统计接近时优先简单"取 l2 者。

"最差 Fold"这一列的区分度有限：最差项几乎总是只有 4 个评估日的 Fold 3。选择时按此折算，未把它当作独立的稳定性证据。

### 计划与执行

实验目录 `var/experiment-search/ae9432e12e0300b5a847`。dry-run 核验：`fold_plan.json` 与 `context` 与来源实验完全相同；80 组 = 两结构各 40 组，无其他组合混入；全部统一 `num_boost_round=2000`、`early_stopping_rounds=100`；每组内恰有 1 组锚点参数。锚点的 trial ID 与结构阶段不同，因为轮数预算进入摘要。

`{'attempted': 80, 'completed': 80, 'failed': 0, 'planned': 80}`，报告 `complete: 80/80`。内存记录 `var/parameter-search-memory.json`：5028 个采样、5028.7 秒、峰值 4.309 GiB、最低可用 3.004 GiB、警戒 0 次、越限 0 次、无新增 OOM。缓存未新建，仍为 `_cache/7c630a3420dc83950016`，数据身份保持一致。

### 结果与偏差警示

平均日度 Rank IC 从结构阶段的 0.097683 / 0.068434 升到 0.156124 / 0.154148，而两个锚点在同预算下分别排第 55 位（0.085792）和第 64 位（0.068434）。在 44 个共同成熟日期上搜 80 组出现这种幅度的提升，符合多重搜索偏差的形态，不能据此宣称模型改善。

比单组名次更可信的是容量方向上的一致效应：

| num_leaves | 组数 | 平均 Rank IC | 最好 | 最差 |
|---:|---:|---:|---:|---:|
| 7 | 35 | 0.115924 | 0.156124 | 0.038318 |
| 15 | 16 | 0.087513 | 0.123934 | 0.049602 |
| 31 | 18 | 0.082665 | 0.113091 | 0.051012 |
| 63 | 11 | 0.076785 | 0.103805 | 0.045938 |

前 20 名中 18 组为 `num_leaves=7`。锚点使用 31，这解释了其排名。该趋势跨 35 组单调，比任何单组成绩稳健，但仍属开发期观察。

### 五个候选

导出至 `configs/experiments/selected_parameters.json`：

| # | trial | 结构 | Rank IC | 最差 Fold | 去掉最好 Fold | 正值比 | Rank ICIR |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | `6eb0c169b05133dbe90d` | alpha158/exp/rank_ic | 0.156124 | 0.061372 | 0.102609 | 1.00 | 1.731 |
| 2 | `6ecb699f9ee74147c294` | alpha360/252/l2 | 0.154148 | 0.124558 | 0.132667 | 0.98 | 1.714 |
| 3 | `be508a35ae11c0348c47` | alpha158/exp/rank_ic | 0.153307 | 0.054897 | 0.091081 | 0.98 | 1.539 |
| 4 | `2b11cd19f5aec5b6795b` | alpha360/252/l2 | 0.142750 | 0.113078 | 0.121329 | 0.95 | 1.620 |
| 5 | `0b61155b01509d06732e` | alpha360/252/l2 | 0.141454 | 0.125254 | 0.136779 | 0.98 | 2.020 |

第 5 位使用 `0b61155b01509d06732e` 而非均值排第四的 `f481a2af8729cb1a1924`（0.144163）。两者均值相差 0.0027，在 44 个日期上小于噪声；而 `0b61155b` 在最差 Fold（0.125254 对 0.050739）、去掉最好 Fold（0.136779 对 0.104648）和 Rank ICIR（2.020 对 1.715）上明显更好，按"统计接近时优先跨 Fold 稳定"取后者。五个候选全部保留完整参数，未只保留赢家。

## 阶段 6：三种子复验（15/15）

实验目录 `var/experiment-search/19338bbefe9323021961`，seeds `[20260829, 20260830, 20260831]`。`{'attempted': 15, 'completed': 15, 'failed': 0, 'planned': 15}`，报告 `complete: 15/15`。内存记录 `var/replicate-memory.json`：948 个采样、947.3 秒、峰值 4.261 GiB、最低可用 3.089 GiB、警戒 0 次、越限 0 次、无新增 OOM。

### 逐候选 seed 稳定性

| 候选 | 结构 | 三 seed Rank IC | seed 均值 | seed 标准差 | 极差 | 区块 bootstrap 95% |
|---|---|---|---:|---:|---:|---|
| `0b61155b` | alpha360/252/l2 | 0.141454 / 0.149197 / 0.149000 | 0.146550 | 0.004415 | 0.007743 | [0.120991, 0.168504] |
| `2b11cd19` | alpha360/252/l2 | 0.142750 / 0.139929 / 0.134589 | 0.139089 | 0.004145 | 0.008161 | [0.109813, 0.159127] |
| `6ecb699f` | alpha360/252/l2 | 0.154148 / 0.098534 / 0.143894 | 0.132192 | 0.029596 | 0.055614 | [0.115606, 0.148595] |
| `be508a35` | alpha158/exp/rank_ic | 0.153307 / 0.047591 / 0.114786 | 0.105228 | 0.053502 | 0.105716 | [0.065167, 0.184755] |
| `6eb0c169` | alpha158/exp/rank_ic | 0.156124 / 0.040591 / 0.052892 | 0.083202 | 0.063450 | 0.115532 | [0.042945, 0.160387] |

### 关键结论：参数搜索冠军是 seed 假象

`6eb0c169` 在阶段 5 以 0.156124 排第一，换两个 seed 后落到 0.040591 和 0.052892，seed 均值 0.083202，回到结构阶段水平；三个 seed 中有两个最差 Fold 转负。按 seed 均值排序，它是五个候选的最后一名。另一个 alpha158 候选 `be508a35` 呈同样形态。

两个 alpha158 候选的 seed 标准差为 0.053502 与 0.063450，三个 alpha360/252/l2 候选为 0.004145～0.029596。因此阶段 5 观察到的"大幅提升"集中在 seed 敏感的组合上，属于搜索噪声被选中，不是模型改善。多种子复验按设计发挥了识别作用。

阶段 5 中按稳定性优先把 `0b61155b` 换入的判断，事后看与 seed 均值排序一致；但这是五个候选中的单个个案，不足以证明该规则普遍成立。

### 两个区块长度的敏感性

区块长度取 `purge_horizon=20`，长区块为 40。44 个日期满足 `>= 2*20`，因此短区块区间可计算；不满足 `>= 2*40`，因此全部五个候选的 `longer_block_interval` 均为 `null`。这是按既定规则明确标注为无法推断，不是缺失或失败。

### 冻结的最终候选

冻结时间：2026-09-05。选定 `0b61155b01509d06732e`，来源 `var/experiment-search/ae9432e12e0300b5a847`，复验目录 `var/experiment-search/19338bbefe9323021961`。

结构：alpha360，horizon 20，训练窗口 252 日，早停指标 l2。参数：

```text
learning_rate      = 0.029532950413954037
num_leaves         = 7
max_depth          = 3
min_data_in_leaf   = 1999
feature_fraction   = 0.674980171860092
bagging_fraction   = 0.86005763729995
bagging_freq       = 1
lambda_l1          = 30.0
lambda_l2          = 1.2036944971085592
objective          = mse
num_boost_round    = 2000
early_stopping_rounds = 100
seed / bagging_seed / feature_fraction_seed / data_random_seed = 20260829
deterministic      = True
force_row_wise     = True
```

锚点 seed 下逐 Fold 最佳迭代 `[1, 167, 269]`，实际评估轮数 `[101, 267, 369]`，IC 均值 0.1253396566814834，ICIR 1.8446550371331618，Rank ICIR 2.0196585653022696。

选择理由：seed 均值 0.146550 为五者最高；seed 标准差 0.004415 与极差 0.007743 属最小一档；三个 seed 的最差 Fold 均在 0.12 以上，没有任何 seed 出现负值 Fold。它同时满足"平均 Rank IC 为主"和"跨 Fold、跨 seed 稳定"。

风险与样本不足项：

- 该候选与其余四个都在同一段 44 个共同成熟日期上选出，历经 24 组结构搜索加 80 组参数搜索加 5 候选比较；区间未做多重搜索校正，下界不代表真实下界。
- 长区块（40）敏感性无法计算，缺少更保守的时间依赖检验。
- 最后一个 Fold 只有 4 个评估日，逐 Fold 统计权重不均。
- 20 日标签在评估期内高度重叠，日度指标不是独立样本。
- 未计交易成本、未做独立盲测、未在未参与选择的时期验证。
- `min_data_in_leaf=1999` 与 `num_leaves=7`、`max_depth=3` 构成很强的容量约束，在更长历史或不同市场状态下未必仍是合适设定。

本记录不发布模型、不执行交易。

## 阶段 7：移交

交付物已从容器卷复制到 [handover-2026-09-05/](handover-2026-09-05/)：三个阶段的 `report.md`/`report.json`/`leaderboard.csv`、复验实验的 manifest 与 fold/trial 计划、两个候选配置文件。模型、预测、逐日指标、学习曲线与校验和仍留在容器卷，未复制入库。

该目录的 [README.md](handover-2026-09-05/README.md) 列出了交易回测接入所需的统一成本与执行规则（成本与滑点、执行假设、组合构造、统计口径、数据时点），这些规则当前没有任何脚本实现；并列出了新时期盲测需要用户确认的五项事宜。在这些确认完成前，本轮候选不应投入实盘或半实盘用途。

本轮脚本不提供交易回测，也不提供独立盲测结论。
