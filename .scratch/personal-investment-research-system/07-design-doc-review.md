# 07 系统设计 Review

评审对象：[07-design-doc.md](../07-design-doc.md)

结论：暂不建议把文档从 Draft 转为已确认。发现 4 个会阻塞实现或验收的问题，以及 3 个需要澄清的设计问题。

## Findings

### 1. [P1] `ProcessInf` 的实际行为与设计相反

文档在 §7.3 称 `ProcessInf` 执行 `±inf → NaN`，以此实现“不填充缺失值”。但锁定的 pyqlib 0.9.7 实现会把每个截面中的 `±inf` 替换为该列非无限值的均值。

这实际上是截面均值填充，会破坏 §7.3 的三项联动决策，也会让缺失率诊断低估问题。

建议：

- 实现并注册一个语义明确的 `InfToNaN` processor；或者接受 Qlib 的均值填充行为，并相应重写缺失值设计。
- processor 的完整规范继续进入 Experiment 指纹。
- 添加行为测试，直接断言 `±inf` 的输出为 NaN；不能只检查 processor 的类名。

### 2. [P1] embargo 的硬约束存在 off-by-one，可能让标签跨入下一段

周度标签的退出点是“下一周截面后的下一实际交易日开盘”。因此，训练集最后一个截面的标签可能要到验证期开始之后才能确定。

例如：

```text
1/10 周五：train 最后一个因子截面
1/13 周一：该样本按开盘价进入
1/17 周五：valid 第一个因子截面
1/20 周一：train 样本退出，得到训练标签
```

虽然 `train_end=1/10` 与 `valid_start=1/17` 相差一周，但训练标签使用了 1/20 的价格。模型因此使用了验证期开始后才能获得的信息。

当前“相邻两段间距 ≥ 标签窗口”的约束过于模糊，可能把上述切分判为合法。文档 §13.3 中的测试实际上规定了更准确的条件。

建议把以下不等式直接提升为领域硬约束：

```text
label_exit(train_last) < feature_cutoff(valid_first)
label_exit(valid_last) < feature_cutoff(test_first)
```

这两个条件必须按东京证券交易所的真实交易日历计算，不能简单地增加 7 个自然日。通常需要在 train 与 valid 之间真正排除一个周度截面，例如：

```text
1/03：train 最后截面
1/10：embargo，不属于任何一段
1/17：valid 第一截面
```

### 3. [P1] 黄金数据集无法产生成功的端到端运行

§13.1 计划使用约 30 只证券、约 200 个交易日，并形成 12/4/4 周的 train/valid/test 切分，但它与复用自 06 的约束不兼容：

- 06 要求一个有效截面至少包含 100 只有效证券；30 只证券不可能通过该门槛。
- `required_history_days=147` 后，200 个交易日只剩约 53 个交易日，即约 10 周，不足 20 周。
- 上述计算还没有包含两个 embargo 和尾部标签窗口。

按当前设计，所有测试截面都会因证券数不足而被排除，运行最终会触发 `no_valid_test_cross_section`。

建议：

- 黄金切片至少覆盖 100 只证券；
- 按真实交易日历从 147 日预热、20 个有效周度截面、两个 embargo 和尾部标签窗口反推所需跨度；
- 如果使用测试专用的较低覆盖门槛，必须明确它不能替代生产门槛的端到端验收，并另行测试 100 只证券门槛。

### 4. [P1] 当前 `LGBModel` interface 无法注入取消 callback

§11.3 要求在 LightGBM 训练中注册 callback，每 20 轮检查 `cancel_requested`。但是 pyqlib 0.9.7 的 `LGBModel.fit()` 会在内部固定向 `lgb.train()` 传递自己的 `callbacks` 列表。调用方再通过 `**kwargs` 传入 callbacks 会产生重复关键字，无法按文档描述实现。

建议在 Qlib seam 上明确提供一个受控的 `CancellableLGBModel` adapter 或 subclass：

- 合并 Qlib 原有 callbacks 与取消 callback；
- 保持 `LGBModel` 的训练、早停、recorder 和预测行为；
- 测试取消后运行进入 `cancelled`，临时产物被删除，且不会发布部分模型；
- 测试新增 callback 不会丢失训练曲线或 recorder 指标。

### 5. [P2] `CSRankNorm` 的数值语义和模型输出解释不正确

§5.2 称标签被转换为 0–1 的秩百分位，并将类似 `raw_score=0.87` 的输出解释为“预计排在前 13%”。实际 pyqlib 0.9.7 的 `CSRankNorm` 计算：

```text
(rank_percentile - 0.5) × 3.46
```

目标范围约为 `[-1.73, 1.73]`，不是 `[0, 1]`。此外，回归模型的原始预测值本身也不是经过校准的百分位，即使训练目标使用 `[0, 1]` 也不能直接作百分位解释。

建议：

- 将 `raw_score` 定义为无收益量纲、也无百分位量纲的回归输出；
- 只有在同一个预测截面内对 `raw_score` 再次排名后得到的 `rank_percentile`，才能解释为位次百分位；
- 页面只用派生的 `rank_percentile` 表达排名，不对 `raw_score` 作“前百分之多少”的解释。

### 6. [P2] FeatureSet 身份仍然依赖人工版本维护

设计正确地要求完整 processor 列表进入 Experiment 指纹，但 FeatureSet 只通过名称、版本以及注册表总版本参与身份。

这会产生两类风险：

- 修改某个特征表达式但忘记升版时，系统可能错误复用旧 Experiment；
- 修改一个无关 FeatureSet 的注册表总版本时，现有 FeatureSet 的实验身份可能无意义地改变。

建议把本次选择的 FeatureSet 的有序、展开后定义规范化，并直接进入 Experiment 指纹与 artifact manifest，至少包括：

```text
feature name
expression
required fields
window
dtype
column order
```

名称和版本继续用于人类识别，但不应成为保证研究语义身份的唯一机制。

### 7. [P2] `PredictionRun` 尚未真正支持独立复用已训模型

§2.2 声称把 `TrainedModel` 与 `ResearchRun` 分开后，可以在新 DataSnapshot 上重新推理。但当前 `PredictionRun` 没有自己的 `research_run_id`、运行环境、错误信息或明确的 artifact 所有权；同时它被声明为“不可变”，却又包含会变化的 `status`。

07 中一次训练 Run 恰好创建一个 PredictionRun 时，可以经由 TrainedModel 间接查找；但 12 在新快照上独立推理时，会缺少承载执行尝试、失败、取消和发布生命周期的对象。

建议采用以下一种模型：

- `PredictionRun` 表示发布后的不可变预测结果，并关联一个负责状态、错误和执行环境的 `ResearchRun`；或者
- 引入独立的 PredictionAttempt，由它承担执行生命周期，PredictionRun 只在成功发布后创建。

无论选择哪一种，都应明确新预测产物属于哪个 artifact，以及失败或取消时是否创建 PredictionRun。

## 总体评价

领域边界、ResearchArtifact 原子发布、测试集不参与训练以及统一 `RankedScores` seam 的方向是清晰的。

最优先需要修正的是：

1. `ProcessInf` 的真实行为；
2. embargo 的交易日约束；
3. 黄金数据集的规模；
4. 可取消训练的实际实现 seam。

前三项会直接导致研究语义错误或验收无法通过，第四项会阻塞文档所承诺的训练取消能力。
