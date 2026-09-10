# 交付物：2026-09-10 一轮完整搜索

执行过程与全部证据见 [../execution-2026-09-10.md](../execution-2026-09-10.md)。本目录只放交付文件。

全部结果标记 `development_only_not_blind_test`：44 个共同成熟日期（2026-02-17 → 2026-04-21）已参与选择，不构成盲测。

## 冻结候选

`59edc062fa2f2681cb52` —— alpha360 / 标签 20 日 / 252 日滚动训练窗口 / rank_ic 早停。冻结时间 2026-09-10。

| 项目 | 值 |
|---|---|
| 三个 seed 的 Rank IC | 0.144621 / 0.145144 / 0.149997 |
| seed 均值 | 0.146587 |
| seed 标准差 | 0.002964 |
| 区块 bootstrap 95%（区块长 20） | [0.117925, 0.161956] |
| 区块长 40 的区间 | null（44 个日期不满足 `>= 2×40`，无法推断） |
| 逐 Fold Rank IC（seed 20260829） | 0.154399 / 0.126923 / 0.184229 |
| 逐 Fold 最佳迭代 | 11 / 48 / 46 |

参数：`num_leaves=7`、`max_depth=3`、`min_data_in_leaf=321`、`learning_rate=0.0161003`、`feature_fraction=0.884898`、`bagging_fraction=0.978425`、`lambda_l1=0`、`lambda_l2=51.6968`、`num_boost_round=2000`、`early_stopping_rounds=100`、`objective=mse`、`deterministic=true`、`seed=20260829`。

## 文件

| 文件 | 内容 |
|---|---|
| `structure-report.md` / `.json` / `structure-leaderboard.csv` | 阶段 3 的 24 组结构搜索 |
| `parameter-report.md` / `.json` / `parameter-leaderboard.csv` | 阶段 4 的 80 组参数搜索 |
| `replicate-report.md` / `.json` / `replicate-leaderboard.csv` | 阶段 5 的 15 项三种子复验 |
| `replicate-manifest.json` / `replicate-fold_plan.json` / `replicate-trial_plan.json` | 复验实验的身份与计划 |
| `selected_structures.json` | 阶段 3 的两个结构候选 |
| `selected_parameters.json` | 阶段 4 的五个参数候选 |
| `selected_parameters-direct-run-59edc062fa2f2681cb52.json` | 冻结候选的 direct 入口配置 |
| `direct-run-59edc062fa2f2681cb52.summary.json` | 阶段 6 的 direct 运行结果（summary 与逐日 IC；35333 行预测与 1080 行特征重要性留在容器卷 `/app/var/direct-59edc062fa2f2681cb52.json`，9.9 MB） |

实验目录（容器卷 `research_assets`）：结构 `var/experiment-search/7fc89912a48db569ada9`，参数 `var/experiment-search/5fab269e3779787f6a65`，复验 `var/experiment-search/2b9fd17242cd581fb203`。

## 阶段 6 对账结论

`model_params` 全部键值相同，`stop_metric=rank_ic`、`rolling_train_policy=252` 与 `trial_plan.json` 一致；`summary.test_rank_ic_mean = 0.1446214406876005` 与该 trial `metrics.json` 的 `rank_ic_mean` 逐位相同；三个 Fold 的 valid/test 区间与训练结束日与 `fold_plan.json` 逐字相同，逐 Fold 的 train/valid/test 行数与搜索侧完全一致，最佳迭代同为 [11, 48, 46]。

一处口径说明：`fold_plan.json` 记录的训练**起点**是 expanding 几何（三个 Fold 均为 2024-09-02），而本候选是 252 日滚动窗口，fold 2/3 的训练起点分别为 2024-10-02 与 2024-10-31，各自跨度正好 252 个交易日。因此 prompt 阶段 6 第一项「每个 fold 的 train/valid/test 区间与 `fold_plan.json` 一致」对任何非 expanding 候选都不可能字面成立，需改为比对训练结束日与训练行数。详见执行记录。

## 交易回测接入所需的统一规则

本轮只产出预测分数，**没有**任何回测或盲测结论。要把 `RankedScores` 接到 `ExecutionReplay`，下列规则必须先固定并冻结，否则不同次回测不可比：

- **成交价格**：使用 `ExecutionPrice`（未复权的当时报价），不能用训练标签所依据的 `ResearchPrice`。
- **下单时点与撮合**：标签定义为 `close[t+h+1] / close[t+1] - 1`，即 t 日收盘后决策、t+1 开始持有。回测的下单时点必须与此一致，否则评价对象与训练目标不是同一件事。
- **成本**：手续费、税费、滑点、借券成本（若做空）的具体数值与计费方式。
- **组合构造**：选股数或分位、权重方式、单票与行业上限、现金比例。
- **调仓频率与持有期**：标签是 20 日跨度且区间重叠，重叠持仓的处理规则必须明确。
- **可交易性**：停牌、涨跌停、流动性下限、退市的处理。
- **股票池一致性**：回测每日的可选集合必须与研究时的 `ResearchUniverse` 同源（本轮窗口 59 天，policy 指纹见 `replicate-manifest.json`）。

## 未来盲测

需要一段**未参与本轮任何选择**的时期。当前快照的最长标签最后成熟日为 2026-04-21，之后 19 个交易日（至 2026-05-22）标签未成熟。盲测必须在快照扩展后另行安排，并在冻结候选参数不变的前提下一次性执行。本轮脚本不提供盲测结论。
