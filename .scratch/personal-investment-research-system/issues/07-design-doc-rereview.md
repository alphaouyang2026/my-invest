# 07 设计文档重新 Review

- Review 对象：[07-design-doc.md](../07-design-doc.md)
- Review 日期：2026-08-29
- 原始结论：发现 2 个 P1、3 个 P2 问题
- 解决状态：**设计层全部解决**（2026-08-29）；代码实现与测试仍由 ticket 07 完成

## Findings

### RESOLVED · P1 — 文件原子发布与数据库提交之间存在不可恢复的崩溃窗口

设计复用“临时目录 → `os.replace` → 数据库发布”的机制，但文件系统重命名和 PostgreSQL 事务不能原子提交。当前实现先执行 `os.replace`，随后才插入 `ResearchArtifact` 并提交数据库：

- [`backend/app/research/artifacts.py:100`](../../../backend/app/research/artifacts.py#L100)
- [`backend/app/research/workflow.py:327`](../../../backend/app/research/workflow.py#L327)
- [`backend/app/research/workflow.py:365`](../../../backend/app/research/workflow.py#L365)

worker 若在中间崩溃，会留下没有数据库记录的“已发布”目录，同时运行被恢复为失败；07 还会增加 `TrainedModel`、`PredictionRun` 三者的一致性问题。这与“失败运行不发布部分产物”冲突。

参考：[07-design-doc.md §10.2](../07-design-doc.md#102-researchartifact-目录)。

建议设计明确的 publication protocol，例如先落 `publishing` 数据库记录，再 rename，最后 finalize；恢复过程按 checksum 完成提交或清理孤儿目录。必须测试在 rename 后、DB commit 前模拟崩溃。

解决：设计新增 `ResearchArtifactPublisher` 深模块与 `ResearchArtifactPublication(prepared | committed | failed)` 日志，规定两笔数据库事务、幂等 rename/finalize、启动恢复、冲突保护、临时目录 scavenger，以及每个崩溃接缝的故障注入测试。参见 [07-design-doc.md §10.2.1](../07-design-doc.md#1021-可恢复的发布协议)。

### RESOLVED · P1 — 旧 Experiment 重跑时可能用新代码执行旧指纹

FeatureSet 展开定义和 processor 身份存进不可变 Experiment，但实际执行又要求只能使用当前代码中的可信常量，禁止数据库内容流入 Qlib 配置。两者之间缺少一致性检查。

典型场景：

1. Experiment A 使用旧 `alpha158_jp_v1` 创建并失败；
2. 后续代码修改某个表达式或 processor；
3. 用户重跑 Experiment A；
4. worker 从当前注册表取得新实现，却仍把结果挂在旧 Experiment A 下。

如果改为直接执行 Experiment 数据库内保存的表达式，又违反“用户或数据库字段不得流入动态 Qlib 配置”的安全约束。

参考：

- [07-design-doc.md §2.1](../07-design-doc.md#21-researchexperiment-的-kind)
- [07-design-doc.md §4.1](../07-design-doc.md#41-形态)
- [07-design-doc.md §7.1](../07-design-doc.md#71-qlib-栈的使用深度)
- [06-design-doc.md §2.1](../06-design-doc.md#21-experiment-与-run)

建议把“编译执行规范”做成深模块：运行前从可信注册表构造完整定义，与 Experiment 中的定义逐字段比较；不一致则拒绝旧 Experiment 重跑。另一种方案是保留可按语义版本选择的旧可信 adapter。

解决：设计新增 `ModelExecutionSpec.compile_new` / `resolve_experiment` / `resolve_inference` interface。数据库定义只用于身份比较与审计；worker 必须从代码内可信 adapter 重建完整定义并逐字段匹配，无法匹配时在初始化 Qlib 前失败 `experiment_execution_definition_unavailable`。参见 [07-design-doc.md §2.1.1](../07-design-doc.md#211-可信执行规范旧-experiment-不得由新代码静默代跑)。

### RESOLVED · P2 — 文档中的 Qlib recorder 链路缺少 `SignalRecord`

文档声明完整栈为：

```text
DataHandlerLP → DatasetH → LGBModel → SigAnaRecord
```

但 pyqlib 0.9.7 的 `SigAnaRecord` 依赖 recorder 中已有的 `pred.pkl` 和 `label.pkl`；这些通常由 `SignalRecord.generate()` 创建。当前设计全文没有 `SignalRecord`，所以按文档直接调用 `SigAnaRecord.generate()` 会发现依赖缺失并跳过分析，而不会生成预期输出。

参考：

- [07-design-doc.md §7.1](../07-design-doc.md#71-qlib-栈的使用深度)
- [07-design-doc.md §13.4](../07-design-doc.md#134-ci-分层)

需要明确以下方案之一：

- 调用 `SignalRecord(model, dataset, recorder).generate()` 后再调用 `SigAnaRecord`；或
- 由本地代码以兼容 schema 显式写入 recorder，并说明这条 adapter 的 interface 和测试。

同时应说明 `SignalRecord` 默认使用 `test` segment，而三段指标仍由本地统一统计模块产生。

解决：Qlib 链路已改为 `DataHandlerLP → DatasetH → LGBModel → SignalRecord → SigAnaRecord`。`SignalRecord` 的 test `pred.pkl` 成为 PredictionRun 的权威原始分数来源，`SigAnaRecord` 只承担 Qlib test 分析与集成证明，三段产品指标继续走本地统一统计 module；CI 明确拒绝把依赖缺失导致的 skip 当作成功。参见 [07-design-doc.md §7.1–7.2](../07-design-doc.md#71-qlib-栈的使用深度)。

### RESOLVED · P2 — `TrainedModel` 的字段定义与后文要求不一致

§7.4 明确说 `fit_start_time` / `fit_end_time` 要“写进 `TrainedModel` 记录”，但 §2.2 的 `TrainedModel` 字段列表没有 `fit_window`，也没有 processor 身份或展开后的 FeatureSet 定义。

参考：

- [07-design-doc.md §2.2](../07-design-doc.md#22-trainedmodel)
- [07-design-doc.md §7.4](../07-design-doc.md#74-训练区间声明现在没有消费者但必须正确)

至少应在模型中增加 `fit_start_time`、`fit_end_time`。更好的做法是给 `TrainedModel` 一个不可变的 `inference_contract` 或定义 checksum，指向其完整 FeatureSet、processor、标签及运行时身份，使票 12 不必穿透 `ResearchRun → Experiment → Artifact manifest` 才能正确加载模型。

解决：`TrainedModel` 已增加 `fit_start_time` / `fit_end_time`、完整 `inference_contract` 与 checksum。契约自包含特征列序、infer processor、bundle 要求和运行时兼容身份，但不能直接执行；加载时仍须由可信 adapter 完整匹配，否则失败 `model_inference_contract_unavailable`。参见 [07-design-doc.md §2.2](../07-design-doc.md#22-trainedmodel)。

### RESOLVED · P2 — 跨 `num_threads` 的“结果相同”不适用于 `model.txt`

使用仓库锁定的 LightGBM 4.7.0、相同数据、相同四个种子及 `deterministic=true`、`force_row_wise=true` 实测：

- 1 线程和 4 线程的预测逐位相同；
- `model.txt` 不同；
- 差异包含 `[num_threads: 1]` 与 `[num_threads: 4]`，因此 SHA-256 不同。

这使“不同线程数结果相同”与模型文件逐位复现承诺存在歧义。

参考：

- [07-design-doc.md §7.5](../07-design-doc.md#75-参数集-lgbm_jp_baseline_v1)
- [07-design-doc.md §13.2](../07-design-doc.md#132-可复现性承诺)

建议把承诺精确定义为：

- 相同线程配置重跑：`model.txt` checksum 与预测逐位相同；
- 不同线程配置：树结构、预测及研究指标相同，但原始 `model.txt` checksum 不承诺相同；
- 如确实需要跨线程模型身份一致，另计算排除运行参数元数据后的语义 checksum，同时保留原文件 checksum 供完整性校验。

解决：复现承诺已拆分。相同线程配置要求原始 `model.txt` checksum 和预测逐位相同；不同线程配置只要求规范化树结构、预测和研究指标相同，明确不承诺原始模型文件 checksum 相同。CI 分别验证两个承诺。参见 [07-design-doc.md §13.2](../07-design-doc.md#132-可复现性承诺)。

## 总结

设计文档的时间泄漏、指标口径和特征身份设计已经比较扎实。本轮指出的跨 seam 恢复与重放问题已在设计层补齐：

1. 文件系统发布与数据库状态的崩溃恢复；
2. 不可变 Experiment 与当前可信代码之间的执行一致性；
3. TrainedModel 自包含的推理 interface。

本 issue 可以作为设计 review 记录关闭；这里的“解决”不代表代码已经实现，具体迁移、实现与自动化测试仍属于 ticket 07 的完成条件。
