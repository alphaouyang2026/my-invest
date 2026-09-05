
# 07 系统设计复审

评审对象：[07-design-doc.md](../07-design-doc.md)

## 结论

上次 7 项 findings 中有 6 项已经闭环；黄金数据集的修正仍有一个关键误判。另外发现 3 个新问题。

暂不建议把设计从 Draft 转为已确认。

## Findings

### 1. [P1] 黄金 fixture 只有 23 个行情日期，不是连续 485 日

设计文档认为现有 fixture 有“1562 只证券、485 个交易日”，因此只需加宽字段并取证券子集。

但 485 日只是 fixture 中的 `calendar`。现有导出器实际只查询 `dates` 集合中的 23 个离散行情日期：

```python
history = calendar.window_back(AS_OF, DEFAULT_POLICY.required_history_days)
dates = sorted(
    {history[len(history) - o] for o in DEFAULT_POLICY.required_bar_offsets}
    | set(calendar.window_back(AS_OF, DEFAULT_POLICY.liquidity_window_days))
    | {AS_OF}
)
```

随后行情查询也明确使用 `trade_dates=dates`，因此 `bars` 并不包含 485 个连续交易日。

这份数据无法支持：

- Alpha158 的连续 rolling 特征；
- 147 日历史覆盖检查；
- 12/4/4 周 train/valid/test；
- 两个 embargo 截面；
- 尾部标签窗口。

需要同时修改两个维度：

- 加宽字段，补齐 raw/adjusted OHLCV、`trading_value`、`adjustment_factor` 和质量原因；
- 将行情日期从 23 个离散点扩展为至少约 262 个连续交易日。

可以继续使用同一个 fixture 文件，但不能描述成仅“加字段 + 取子集”。合理形态约为 150 只证券 × 262 个连续交易日，行数与当前约 1562 × 23 接近。

### 2. [P1] `model_params` 覆盖 interface 没有白名单和资源上限

`POST /research/model-runs` 接受“参数覆盖”，但文档只限制了 `feature_set` 必须来自注册表，没有定义哪些 LightGBM 参数可以覆盖、参数类型及数值范围。

如果实现为接受任意字典，调用方可能覆盖：

- `objective`，改变研究目标；
- `deterministic`、`force_row_wise`，破坏可复现性；
- `num_boost_round`、`num_leaves`、`min_data_in_leaf`，绕过资源限制；
- `num_threads` 或其他运行环境参数。

建议把创建 interface 定义成类型化白名单：

- 固定且不可覆盖：`objective`、`deterministic`、`force_row_wise`；
- 可覆盖但有明确范围：学习率、叶子数、深度、正则项、boost 轮数等；
- 仅由 settings 控制：`num_threads`；
- 未知参数或越界值直接返回 400，不静默忽略或修正。

白名单展开后的实际参数继续完整进入 Experiment 指纹；运行时资源参数按现有设计记录在 `TrainedModel` 中用于审计。

### 3. [P2] processor 指纹不能识别 `InfToNaN` 的实现变化

当前指纹只包含 processor 的类名和参数：

```json
{
  "class": "InfToNaN",
  "kwargs": {"fields_group": "feature"}
}
```

如果以后修改 `InfToNaN.__call__()` 的行为，但类名和参数没有变化，Experiment 指纹也不会变化。系统仍会把两种研究语义复用为同一个 Experiment。

应用代码版本记录在 `TrainedModel.runtime_identity`，只能区分执行环境，不能满足“任一研究语义变化都会产生新实验”的约束。

建议给自研 processor 增加明确的语义身份，例如：

```json
{
  "class": "InfToNaN",
  "semantics_version": 1,
  "kwargs": {"fields_group": "feature"}
}
```

也可以使用稳定的实现摘要，但不能依赖不稳定的 `repr` 或对象地址。该身份应同时进入 Experiment 指纹和 artifact manifest。

测试至少覆盖：

- 改变 `semantics_version` 会产生不同 Experiment 指纹；
- 只重排 JSON key 不改变指纹；
- artifact manifest 保存与 Experiment 相同的 processor 语义身份。

### 4. [P2] 泄漏测试只验证了第一个 embargo

设计的领域硬约束已经正确覆盖两个接缝：

```text
label_exit(train_last) < feature_cutoff(valid_first)
label_exit(valid_last) < feature_cutoff(test_first)
```

但 §13.3 的完成条件只明确测试第一个不等式。还必须直接验证第二个不等式，否则 valid 标签可能使用 test 开始后的行情，并通过早停影响最终模型。

此外，当前哨兵测试只修改 test 标签，然后断言模型不变。它能发现 test 标签流入训练，却不能发现 test 特征被预处理或训练读取。

建议增加：

- `label_exit(valid_last) < feature_cutoff(test_first)` 的直接断言；
- 独立修改 test 标签后，模型与 train/valid 指标不变；
- 独立大幅修改 test 特征后，`model.txt`、train 指标和 valid 指标仍不变；
- 保留从 Qlib handler 真实读取行为验证 train 特征最大日期的测试。

文档中“任何一条泄漏路径都会让哨兵测试失败”的表述应收窄为“任何 test 标签进入训练的路径都会失败”。

## 上次 Findings 的闭环状态

| 上次 finding | 状态 | 说明 |
|---|---|---|
| `ProcessInf` 行为与设计相反 | 已解决 | 改为自研 `InfToNaN`，明确行为并要求输入输出测试 |
| embargo 约束允许标签跨段 | 已解决 | 改为按真实交易日历检查两个 `label_exit < feature_cutoff` 不等式 |
| 黄金数据集规模不足 | 部分解决 | 证券数和理论跨度已修正，但误把 485 日 calendar 当成连续 bar 数据 |
| `LGBModel` 无法注入取消 callback | 已解决 | 明确 `CancellableLGBModel` seam、callback 合并方式及验收测试 |
| `CSRankNorm` 与 raw score 解释错误 | 已解决 | 写明真实公式，并仅允许 `rank_percentile` 作位次解释 |
| FeatureSet 身份依赖人工版本 | 已解决 | 所选 FeatureSet 的展开有序定义直接进入指纹和 manifest |
| `PredictionRun` 生命周期不完整 | 已解决 | 状态归属 `ResearchRun`，PredictionRun 仅在成功发布后创建 |

## 总体评价

本轮修改已经显著提高了设计的可实现性和一致性，尤其是 Qlib processor 行为、时间隔离、取消 seam 和 PredictionRun 生命周期都已从概念描述落到了可测试的 interface。

确认设计前应优先修复两个 P1：

1. 黄金 fixture 必须导出连续行情，而不只是扩充 23 个离散日期的字段；
2. 创建模型运行的参数覆盖必须定义类型化白名单和资源上限。

两个 P2 可以与上述修改一起完成，避免同一研究语义被错误复用，以及 valid/test 接缝缺少直接泄漏证明。
