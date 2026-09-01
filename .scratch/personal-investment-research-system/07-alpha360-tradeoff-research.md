# Alpha360 在 ticket 07 中的交付与启用取舍

调查日期：2026-08-29

## 结论

`alpha360_jp_v1` 应在 ticket 07 中**交付为可选择的实验性 FeatureSet，但不作为创建实验时的默认选择**。默认仍用 `alpha158_jp_v1`。

当前设计的“注册但停用”如果表示 API 或页面不能选择、不能成功训练，就不满足 ticket；如果它实际只想表达“不是默认值”，则应改成不含歧义的三项状态：

```text
selectable = true
is_default = false
maturity = experimental
```

不建议完全取消 Alpha360，原因不是它已被证明更好，而是：

1. ticket 明确要求用户可以选择 Alpha158/Alpha360，并要求交付经过字段可用性验证的两类特征；
2. Alpha360 是 Qlib 官方定义并正式提供 LightGBM workflow 的基线，不是一个尚不支持的设想；
3. schema v2 为 Alpha158 增加的六类行情事实已经覆盖 Alpha360，没有新的上游数据依赖；
4. 小样本与缺失值风险足以支持“非默认、实验性和强警告”，但不足以支持“本票不可用”。

## 1. Ticket 到底要求什么

本地 ticket 的 `What to build` 写的是“用户可以从现有价格量数据中**选择适用的 Qlib Alpha158/Alpha360 特征**”；第一条验收条件又要求“提供经现有字段可用性验证的 **Alpha158/Alpha360 特征子集**，每个特征记录表达式、所需字段、窗口和版本”：

- `.scratch/personal-investment-research-system/issues/07-qlib-multifactor-lightgbm.md:3`
- `.scratch/personal-investment-research-system/issues/07-qlib-multifactor-lightgbm.md:9`

这里要求的是两类特征都能被用户选中并走通训练路径。它**没有**要求 Alpha360 成为页面默认值，也没有要求证明 Alpha360 优于 Alpha158。

当前设计一方面把 `alpha360_jp_v1` 列入“本票完成”，另一方面写“默认停用/注册但停用”，且把真正启用推给未来 ticket：

- `.scratch/personal-investment-research-system/07-design-doc.md:35`
- `.scratch/personal-investment-research-system/07-design-doc.md:231-239`

因此需区分：

| 状态 | 是否满足 ticket | 说明 |
|---|---:|---|
| 注册信息存在，但 API 拒绝运行或页面不可选择 | 否 | 用户实际上不能选择 Alpha360 |
| API/页面可选择并能完成训练，页面默认选 Alpha158 | 是 | “交付可用”与“默认启用”被正确分开 |
| 页面默认选 Alpha360 | 是，但不推荐 | ticket 没要求，且当前数据长度不支持把它当稳健默认 |
| 完全不交付 Alpha360 | 否 | 除非先修改 ticket 的范围与验收条件 |

## 2. Alpha360 与 Alpha158 的确切内容

### 2.1 Alpha360

Qlib 官方 `Alpha360DL.get_feature_config()` 生成六组特征：

| 组 | 位置 | 数量 | 表达式口径 |
|---|---|---:|---|
| CLOSE | t-59 … t | 60 | 历史 close / 当前 close |
| OPEN | t-59 … t | 60 | 历史 open / 当前 close |
| HIGH | t-59 … t | 60 | 历史 high / 当前 close |
| LOW | t-59 … t | 60 | 历史 low / 当前 close |
| VWAP | t-59 … t | 60 | 历史 vwap / 当前 close |
| VOLUME | t-59 … t | 60 | 历史 volume /（当前 volume + 1e-12） |

合计 `6 × 60 = 360` 列。官方源码称其目标是提供最近 60 日的原始价格量数据，并用当前价/量消除量纲：[Qlib `Alpha360DL` 官方源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py#L4-L58)。

### 2.2 Alpha158

Qlib 当前默认 Alpha158 是：

- 9 个 K 线形态特征；
- 当前 OPEN/HIGH/LOW/VWAP 相对当前 close 的 4 个特征；
- 29 类 rolling 特征，每类使用 `[5, 10, 20, 30, 60]` 五个窗口，共 `29 × 5 = 145` 列。

总数是 `9 + 4 + 145 = 158`。默认 handler 配置见 [Qlib `Alpha158` 官方源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py#L98-L152)，完整生成器见 [Qlib `Alpha158DL` 官方源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py#L61-L310)。

29 类 rolling 特征为：`ROC/MA/STD/BETA/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN/IMXD/CORR/CORD/CNTP/CNTN/CNTD/SUMP/SUMN/SUMD/VMA/VSTD/WVMA/VSUMP/VSUMN/VSUMD`。

设计表把两者的“最大窗口”都写成 60（`07-design-doc.md:233-237`），作为产品展示口径可以成立；但实现历史读取时要注意：Alpha158 的部分 60 日 rolling 表达式内部还引用 `Ref(..., 1)`，完整输入依赖可触及 t-60。Qlib 的 rolling 又使用 `min_periods=1`，因此“窗口未满时所有特征一定为空”也不成立。[Qlib rolling operator 官方源码](https://github.com/microsoft/qlib/blob/main/qlib/data/ops.py#L713-L779)

### 2.3 两者的数据要求并没有分叉

两套默认定义最终都依赖：

```text
open, high, low, close, vwap, volume
```

Alpha360 没有引入 Alpha158 之外的新行情字段，名义回看长度也没有超过 60 日。设计已经为了两者把 bundle 升到 schema v2 并增加 `$vwap`（`07-design-doc.md:159-185`）；股票池又因为 6-1 动量对照保留 147 日预热（`07-design-doc.md:241-245`）。因此交付 Alpha360 不需要再扩大上游历史范围或增加新的 fixture 字段。

## 3. 引入 Alpha360 的收益

### 3.1 满足 ticket 的真实用户能力

交付一个可运行的 `alpha360_jp_v1` 后，`GET /research/feature-sets`、创建实验表单和 `ResearchExperiment` 才真正覆盖 ticket 声明的两类 Qlib 特征。只把 360 个名字登记在注册表，却禁止创建运行，属于元数据交付，不是功能交付。

### 3.2 保留少人工工程化的对照

Qlib 官方将 Alpha158 描述为人工设计的 tabular 特征，将 Alpha360 描述为少量特征工程、沿时间维具有强结构的原始价量表示：[Qlib 官方 benchmark 说明](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md#L605-L608)。

两者回答的是不同研究问题：

- Alpha158：先把技术分析归纳偏置写进特征，再让模型选；
- Alpha360：保留 60 日路径，让模型自己组合滞后位置。

这使 Alpha360 有资格作为对照，而不是 Alpha158 的重复版本。它也为未来序列模型保留规则的 `6 × 60` 输入结构；不过未来价值不是本票默认启用它的理由。

### 3.3 Qlib 官方确认 Alpha360 可用于 LightGBM

Qlib 不仅用 Alpha360 驱动 LSTM/GRU/TCN，也正式提供 `LGBModel + Alpha360` workflow：[Qlib Alpha360 LightGBM 配置](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha360.yaml)。所以“Alpha360 只适合序列模型，不能用于 LightGBM”不是成立的停用理由。

官方 benchmark 同时报告 Alpha158 和 Alpha360 的 LightGBM 结果，但结果有输有赢，不能推出 Alpha360 普遍优于 Alpha158；而且官方数据是中国 A 股日频、长训练区间，不能外推到本项目的日本股票、周频标签和约两年历史。[Qlib 官方 benchmark](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)

## 4. 成本与风险

### 4.1 过拟合风险应怎样准确表达

原设计用“约 85 个周度截面上 360 维极易过拟合”解释停用（`07-design-doc.md:239`）。方向上有风险，但把 `360` 直接与 `85` 比较并不准确：LightGBM 的训练行是“截面 × 证券”，不是每周只有一行。

按设计默认切分，约 85 个可用截面扣除 embargo 后大致为 train 50、valid 16、test 16（`07-design-doc.md:335-337`）：

| 场景 | 名义训练行数 | 真正薄弱之处 |
|---|---:|---|
| 每截面 100–150 只、train 约 50 周 | 5,000–7,500 | 只有约 50 个市场时间状态；同周证券并非 100–150 个独立宏观状态 |
| 生产设计所举约 1,000 只、train 约 50 周 | 约 50,000 | 行数不小，但时间状态仍只有约 50 个 |
| 07 CI fixture：train 12 周 × 100–150 只 | 1,200–1,800 | 只有 12 个训练时间状态，只能验机制，不能验证模型有效性 |

这里还需纠正一个混淆：设计正文在训练规模讨论中写的是“约 85 周 × 约 1000 只”（`07-design-doc.md:263`）；100–150 只是最低有效截面和新 CI fixture 的量级（`07-design-doc.md:903-923`），不应当成生产股票池的固定规模。

因此更准确的风险陈述是：

- Alpha360 比 Alpha158 多 `360 / 158 ≈ 2.28` 倍列，且相邻 lag 高度相关；树在有限的约 50 个训练时间状态中有更多候选切分，拟合偶然时序形状的机会更大；
- 验证段约 16 周，无法可靠地区分两个特征集的微小差异，反复比较/调参本身会对 valid 过拟合；
- 但名义训练行并不是 85，不能用“360 个变量大于 85 个样本”这种线性回归式表述，也不能据此证明 Alpha360 必然失败。

这支持 `experimental + non-default`，不支持完全停用。

### 4.2 缺失值结构

Alpha360 的确会把缺失位置直接展开。例如某个预测截面的最近 60 日里有 5 个完整行情日缺失，六组各有 5 个 lag 缺失，因而可出现 30 个 NaN。但还要看到两个更重要的共同分母效应：

- 当前 close 缺失或无效，会影响以它为分母的五个价格组，最多波及 300 列；
- 当前 volume 为零时，官方表达式用 `volume + 1e-12`，历史 volume 比值可能异常巨大；当前 volume 缺失则会影响整组 volume lag。

这些是官方表达式直接推导出的数据质量风险，应由日本市场的字段有效性规则、`ProcessInf` 和逐特征缺失率产物覆盖。

但“有结构化 NaN”与“不填充、交给 LightGBM”并不矛盾：

- LightGBM 官方默认启用缺失值处理，并以 NaN 表示缺失：[LightGBM Missing Value Handle](https://lightgbm.readthedocs.io/en/latest/Advanced-Topics.html#missing-value-handle)；
- Qlib 官方 Alpha360 handler 的默认 processor 包含填充，但它的官方 **LightGBM Alpha360** workflow 明确覆盖为 `infer_processors: []`，说明“不填充”本来就是一条官方使用路径：[handler 源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py#L37-L98)、[LightGBM Alpha360 配置](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha360.yaml#L19-L20)。

因此不需要为了启用 Alpha360 推翻设计 §7.3。合理做法是沿用同一套 `inf → NaN`、不填充、LightGBM 原生缺失分支，并增加以下可观察性：

- 每列、每段缺失率；
- 每个截面的有效特征/证券覆盖；
- current close/volume 无效引起的整组异常计数；
- train 与 test 缺失率漂移警告。

缺失模式可能携带停牌、流动性或上市年龄信息，这是模型风险和解释问题，但只要全部来自 FeatureCutoff 之前，就不是时间泄漏。Alpha158 的 rolling/Ref 特征同样会产生结构化缺失，不能把它当成 Alpha360 独有的问题。

### 4.3 计算与存储成本

在相同行数和 dtype 下，Alpha360 的稠密特征矩阵约是 Alpha158 的 2.28 倍。以 float64 粗算：

- 50 周 × 150 只的训练矩阵：Alpha360 约 20.6 MiB，Alpha158 约 9.0 MiB；
- 85 周 × 150 只的全段矩阵：Alpha360 约 35.0 MiB，Alpha158 约 15.4 MiB。

这不含 DataFrame、LightGBM bins、索引和产物开销，但说明 100–150 只的 fixture 上内存不是阻止交付的硬障碍。

训练阶段候选特征更多，通常会增加建 bins、扫描直方图和模型产物成本；但不能武断声称墙钟时间一定是 2.28 倍，因为 Alpha360 的表达式主要是简单 `Ref`，而 Alpha158 有许多 rolling 回归、相关、分位数等更昂贵的特征计算。真实性能应在同一 fixture 上记录 `feature_compute_seconds`、峰值内存、`training_seconds` 和 artifact 大小后比较。

### 4.4 实现和测试成本

新增成本主要在产品面和验收面，不在数据适配：

1. 用确定性生成器登记 6 × 60 个有序条目，并把展开定义纳入 Experiment 指纹；
2. API 和页面允许选择 Alpha360，同时显示“实验性、非默认”和观察点警告；
3. 校验 360 列的数量、顺序、首尾表达式、required fields 和窗口，而不是手写 360 份重复代码；
4. 在真实 Qlib bundle 上验证几个哨兵表达式、列顺序和缺失传播；
5. 用同一 07 fixture 至少跑一个 Alpha360 端到端训练/预测/模型保存往返和确定性测试；
6. 复用 Alpha158 已有的泄漏、切分、常数预测、指标和 artifact 测试矩阵，不复制整套业务测试。

若为了节省 CI 时间只测“注册表里有 360 个名字”而不跑一次真实训练，仍不能证明 ticket 所说的“用户可以选择”。可以让 Alpha158 保持完整黄金路径，Alpha360 使用较少 boosting rounds 的真实 smoke/determinism 路径。

## 5. 推荐的产品与验收语义

### 5.1 产品状态

| FeatureSet | 可选择 | 创建页默认 | 成熟度 | 说明 |
|---|---:|---:|---|---|
| `alpha158_jp_v1` | 是 | 是 | baseline | 小数据下有较强人工归纳偏置，作为黄金口径 |
| `alpha360_jp_v1` | 是 | 否 | experimental | 360 个原始 lag；显示样本长度、缺失率与过拟合警告 |
| `momentum_only_v1` | 对照路径 | 否 | baseline | 不经 LightGBM，保持 06 对照语义 |

Alpha360 不需要单独的一套默认模型参数才能算“交付”。为让特征集比较更清楚，可以先沿用本项目受约束的 `lgbm_jp_baseline_v1`，所有实际参数仍进入 Experiment 指纹；不要直接搬用 Qlib 在 2008–2020 中国市场 benchmark 上的参数。Qlib 官方甚至给 Alpha158 与 Alpha360 的 LightGBM 示例设置了不同 learning rate，这进一步说明官方超参不是跨数据集定律：[Alpha158 配置](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml)、[Alpha360 配置](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha360.yaml)。

### 5.2 最小验收条件

要同时满足 ticket 和控制风险，至少应断言：

- `GET /research/feature-sets` 返回 Alpha360 的 360 个有序展开定义；
- 页面默认仍选 Alpha158，但用户能选择 Alpha360；
- 以 `alpha360_jp_v1` 创建合法 Experiment 不会收到 `feature_set_disabled`；
- 同一 07 fixture 上 Alpha360 能完成 Qlib 特征计算、LightGBM 训练、valid 早停、test PredictionRun 和模型保存/加载；
- 产物记录每列/每段缺失率、运行时间、实际样本数、Qlib/LightGBM 版本和完整特征定义；
- Alpha360 的测试只证明机制和复现，不声称模型有效或优于 Alpha158。

## 6. 最终判断

| 方案 | Ticket 一致性 | 风险控制 | 评价 |
|---|---:|---:|---|
| 默认启用 Alpha360 | 满足 | 较弱 | 不推荐；有限历史下没有证据支持它做默认 |
| **交付可选、非默认、实验性** | **满足** | **强** | **推荐** |
| 注册但禁止运行 | 不满足 | 强 | 把功能交付退化成目录展示 |
| 完全不交付 | 不满足 | 最强 | 只有先改 ticket 才合理 |

所以 review 的冲突应当修，但不应把修法理解成“让 Alpha360 成为默认”。正确修法是把“默认停用”改成“可选择、默认不选、实验性”，并让它至少通过一次真实的 Qlib + LightGBM 端到端验收。

