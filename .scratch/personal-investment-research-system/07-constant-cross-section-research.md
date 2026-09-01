# 常数预测截面的通常处理方式

## 结论

横截面选股的评价单位是“同一个预测日期内的一组证券”，因此常数模型检查也应以 `prediction_date` 为单位。把整个 valid 段混在一起计算方差，只能发现“模型在整个时期完全不变”，不能证明模型在任一时点有选股能力。

Qlib 0.9.7 的实际做法是：逐日期计算 IC / Rank IC；无定义的截面留下 `NaN`，汇总时由 pandas 的默认 `skipna=True` 跳过。但 Qlib 不报告被跳过多少个截面，也不会因为所有截面无效而主动把训练判为失败。因此，本项目应在 Qlib 之上补充“逐截面可排名性、有效截面数和覆盖率”的明确策略。

推荐给 07 的策略是：

- 先按既有证券数、预测覆盖率和标签覆盖率门槛筛选截面；
- 再逐 `prediction_date` 检查预测是否可排名；
- 单个常数截面记为 `unavailable`，不参与 IC、Rank IC、ICIR 和 Rank ICIR 的汇总，并记录原因；
- valid 中没有任何可排名截面时，训练失败 `model_no_rankable_validation_cross_section`；
- 部分截面不可排名时可继续，但必须发布 `rankable_cross_section_count / eligible_cross_section_count` 和 warning；
- 整个 valid 段方差只能作为辅助诊断，不应作为主判据；LightGBM 的 gain、叶子数也只作为诊断。

## 1. 为什么必须逐截面判断

Qlib 官方 benchmark 把某个时点的 IC 定义成该时点预测向量与收益向量的相关系数：`IC^(t) = corr(prediction^(t), return^(t))`。这一定义本身就是逐时点、横截面的，而不是把多个日期的证券行混在一起计算。[Qlib benchmark 的 IC 定义](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md#quantitative-investment-metrics)

例如：

| 日期 | A | B | C | 整段是否有变化 | 当日能否排名 |
|---|---:|---:|---:|---|---|
| W1 | 0.1 | 0.1 | 0.1 | 是 | 否 |
| W2 | 0.2 | 0.2 | 0.2 | 是 | 否 |
| W3 | 0.3 | 0.3 | 0.3 | 是 | 否 |

九个值放在一起的方差大于零，但每周内部都没有相对顺序。模型只区分了日期，没区分同一日期内的证券。对逐日期构造 `rank_percentile`、Top-K 和 Rank IC 的系统来说，这仍是无用模型。

## 2. IC / Spearman 遇到常数截面时会怎样

### 数学含义

Pearson 相关系数的分母包含两个变量的标准差；任一变量为常数时标准差为零，相关系数无定义。Spearman 相关系数等价于对秩做 Pearson 相关；预测全相同时，预测秩也全相同，因此同样无定义。SciPy 的官方 `spearmanr` 明确说明：输入为常数时返回 `np.nan`，并发出 `ConstantInputWarning`。[SciPy `spearmanr`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html)

### Qlib 0.9.7 的实际实现

`calc_ic()` 先按 `datetime` 分组，再对每个日期分别调用 pandas `Series.corr()`：普通相关得到 IC，`method="spearman"` 得到 Rank IC。`dropna=False` 是默认值，所以无定义结果默认保留为 `NaN`；只有显式传 `dropna=True` 才立即删除。[Qlib 0.9.7 `calc_ic`](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/contrib/eva/alpha.py#L160-L181)

需要特别注意普通 IC：理论上常数输入一定无定义，但浮点数中的“看起来相同”不保证底层计算恰好得到零方差。项目锁定环境中，`[0.1, 0.1, 0.1]` 对 `[1, 2, 3]` 的 Pearson IC 曾返回 `0.0`，而 Rank IC 返回 `NaN`；换成可精确表示的常数 `0.0` 或 `1.0`，两者才都返回 `NaN`。因此不能依赖相关函数自行识别常数，更不能把返回的 `0.0` 一概解释为“有效但无预测力”。应在算 IC 前显式检查截面分数是否可排名。

近似常数但仍有极小不同值时，Spearman 会把这些差异变成名次，可能产生看似正常甚至很大的 Rank IC。Rank 统计只看顺序，不看间距，所以“多小算太小”没有统计学统一答案；项目应只用数值精度容差消除舍入噪声，不应暗中引入未经定义的经济阈值。

## 3. Qlib `SigAnaRecord` 和 `risk_analysis` 的处理

### `SigAnaRecord`

`SigAnaRecord` 调用 `calc_ic()` 后直接计算：

```python
IC = ic.mean()
ICIR = ic.mean() / ic.std()
Rank_IC = ric.mean()
Rank_ICIR = ric.mean() / ric.std()
```

它没有检查常数截面，也没有记录有效截面数。[Qlib 0.9.7 `SigAnaRecord`](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/workflow/record_temp.py#L295-L347)

pandas `Series.mean()` 默认 `skipna=True`，所以部分日期的 `NaN` 会被静默排除。[pandas `Series.mean`](https://pandas.pydata.org/docs/reference/api/pandas.Series.mean.html) 这意味着 Qlib 的总体 Rank IC 实际是“有定义日期的平均值”。如果 16 周中只有 1 周有效，结果仍可能显示一个 Rank IC 数字；但 `std(ddof=1)` 无法用一个观察值估计，Rank ICIR 会是 `NaN`。如果所有周都无效，均值和标准差也都是 `NaN`。

因此，“沿用 Qlib”并不等于已有完整质量门槛。Qlib 提供计算原语，本项目仍需把以下审计信息显式发布：计划截面数、合格截面数、可排名截面数、被排除原因和最终用于汇总的 `n`。

### `risk_analysis`

`risk_analysis()` 是对收益时间序列算均值、样本标准差、年化收益、信息比率和最大回撤的通用函数；`SigAnaRecord` 计算 IC/ICIR 时并不调用它。[Qlib 0.9.7 `risk_analysis`](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/contrib/evaluate.py#L26-L89)

它同样依赖 pandas 的 `mean()` / `std()` 默认行为：

- 部分 `NaN` 被跳过；
- 全部为 `NaN` 时各项为 `NaN`；
- 常数零收益时 `mean=0`、`std=0`，`information_ratio=0/0=NaN`；
- 只有一个有效观察时，样本标准差和信息比率为 `NaN`。

这说明 Qlib 的共同风格是“让无定义统计量传播成 NaN”，不是把它改写为 0，也不是自动使 Experiment 失败。失败与否是调用方的领域政策。

另外，Qlib 的 long-short 实现按日期调用 `nlargest()` / `nsmallest()`。分数全部并列时，选中谁会受并列处理和行顺序影响，不能视为有意义的 Top/Bottom 组合。[Qlib 0.9.7 `calc_long_short_return`](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/contrib/eva/alpha.py#L87-L126)

## 4. 通常的验证和失败策略

从 Qlib 的定义和实现能直接得到的惯例是：

1. **计算单位是日期截面。** 每个日期独立算 IC / Rank IC。
2. **无定义不是零。** 常数预测、常数标签、有效样本不足产生 `NaN` / `unavailable`，不能填成 0。
3. **聚合只使用有定义截面。** 同时必须给出有效观察数 `n`，否则均值和 ICIR 会掩盖大量无效日期。
4. **训练成功政策另行定义。** Qlib 不提供“至少多少个有效截面”的通用硬门槛；这取决于研究频率、valid 长度及系统用途。

据此，本项目适合采用三级结果：

| 情况 | 推荐处理 |
|---|---|
| 某个合格截面常数或相关无定义 | 当日指标 `unavailable`，保留原因，不进入汇总 |
| valid 尚有可排名截面 | 训练可继续；发布 warning、有效数和比例 |
| valid 没有任何可排名截面 | 训练失败 `model_no_rankable_validation_cross_section` |

如果产品希望比“至少 1 个”更严格，应把 `min_rankable_validation_cross_sections` 或 `min_rankable_validation_ratio` 做成显式、进入 Experiment 指纹的研究参数，而不是藏在相关函数里。至少两个有效截面才可能得到样本标准差和 ICIR；但“ICIR 可算”与“样本量足以支持可靠结论”仍不是一回事。当前预计约 16 个 valid 周，页面继续显示 `n` 是必要的。

## 5. 推荐判据与容差

### 每个日期的判断顺序

```text
1. 对齐该日期的 prediction、label 和 universe。
2. 去掉非有限 prediction / label。
3. 应用 min_valid_securities、预测覆盖率、标签覆盖率门槛。
4. 对剩余 raw_score 做逐截面可排名性检查。
5. 只有通过 1—4 的截面才计算 IC / Rank IC 并进入汇总。
```

预测和标签应分别检查。预测常数说明模型无法排名；标签常数则说明这一期没有可用于检验的收益横截面，但不一定说明模型本身失败。

### 数值判据

不建议直接写 `variance == 0`，也不建议直接使用 `np.isclose()` 的默认容差。NumPy 官方文档提醒其默认 `atol` 不适合量级远小于 1 的数值。[NumPy `isclose`](https://numpy.org/doc/stable/reference/generated/numpy.isclose.html)

建议把“不可排名”定义为所有有限分数在 float64 数值精度内相同：

```python
scores = finite_cross_section_scores.astype("float64")
scale = max(1.0, float(np.max(np.abs(scores))))
tolerance = 64 * np.finfo(np.float64).eps * scale
rankable = float(np.max(scores) - np.min(scores)) > tolerance
```

`np.finfo(...).eps` 是该浮点类型中 1 与下一个可表示数之间的差距。[NumPy `finfo`](https://numpy.org/doc/stable/reference/generated/numpy.finfo.html)

这个阈值只消除浮点舍入量级的假差异，不声称很小但真实的分数差异“没有经济意义”。若将来要按交易成本或分数组间距设置经济阈值，应另立有业务含义、可配置、进入指纹的规则。

还应同时记录：

- `score_min`、`score_max`、`score_range`；
- `distinct_score_count`（原值，以及若展示需要可另给固定精度后的诊断值）；
- 并列率或最大并列组占比；
- 截面内有效证券数与覆盖率；
- `metric_status`，例如 `valid`、`constant_prediction`、`constant_label`、`insufficient_securities`、`insufficient_coverage`。

### valid 段的最终判据

```text
rankable_valid_count = 可排名且满足数据门槛的 valid 截面数

if rankable_valid_count == 0:
    fail("model_no_rankable_validation_cross_section")
elif rankable_valid_count < eligible_valid_count:
    warn("some_validation_cross_sections_are_not_rankable")
```

ICIR / Rank ICIR 继续遵守已有规则：有效 IC 序列的样本标准差为零、无定义，或有效观察不足 2 个时，结果为 `unavailable`，绝不写 0。

## 6. LightGBM 结构指标放在什么位置

LightGBM 官方定义 gain importance 为某特征参与分裂带来的总 gain；split importance 是使用该特征的分裂次数。[LightGBM `Booster.feature_importance`](https://lightgbm.readthedocs.io/en/stable/pythonapi/lightgbm.Booster.html#lightgbm.Booster.feature_importance) 因而：

- `sum(feature_importance("gain")) == 0` 能诊断模型没有产生损失下降的有效分裂；
- 所有树都只有一个叶子能诊断单叶树模型；
- 但两者不能代替逐截面输出检查。模型可以有有效分裂和正 gain，却只区分日期状态，使每个日期内所有证券仍落到相同输出。

所以推荐的优先级是：

```text
主判据：逐 prediction_date 的输出是否可排名
辅助诊断：总 gain、树/叶子结构、best_iteration、整个 valid 段的 score_range
```

## 7. 对 07-design-doc 的具体修改建议

把 §11.2.1 中：

> valid 段预测的方差为 0

改为：

> 在应用有效证券数和覆盖率门槛后，逐 `prediction_date` 检查 valid 预测的截面内分数范围。某截面在 float64 数值精度容差内为常数时，该截面不可排名，不参与 IC 系列汇总并发布结构化原因；若 valid 没有任何可排名截面，运行失败 `model_no_rankable_validation_cross_section`。整个 valid 段方差、LightGBM gain 与叶子数仅作为诊断字段。

并在 §9 / §13 增加三类测试：

1. 每周内部常数、跨周数值不同：整个 valid 方差大于零，但运行仍失败；
2. 部分周常数：运行成功，常数周为 `unavailable`，汇总 `n` 只计算其余周；
3. 分数只相差 float64 舍入量级：按容差视为常数；明显超过容差时可排名。
