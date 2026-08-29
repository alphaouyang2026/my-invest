# 07 — Qlib 多因子与 LightGBM 预测选股

**What to build:** 用户可以从现有价格量数据中选择完整的 Qlib Alpha158 或 Alpha360 特征集，按严格时间顺序定义 train / valid / test 区间，以周度实际调仓收益的截面秩为目标训练 LightGBM `mse` 基线；完成后查看样本外 PredictionRun、选股排名、特征重要性和实验指标，并与同一次运行内的 6-1 动量基线比较。

**Design:** [07 系统设计 — Qlib 多因子与 LightGBM 预测选股](../07-design-doc.md)

**Blocked by:** 06 — Qlib 数据适配与动量因子研究

**Status:** ready-for-agent

- [ ] QlibDataBundle exporter 升至 schema v2，按设计生成复权 `$vwap`；缺少所选 FeatureSet 声明字段时硬失败 `missing_bundle_field`，不降级或静默填充
- [ ] 代码内注册完整、有序的 `alpha158_jp_v1`（158 列、默认 baseline）、`alpha360_jp_v1`（360 列、可选择的 experimental）和仅供同运行对照的 `momentum_only_v1`；每列记录表达式、所需字段、窗口、dtype 与列序，展开定义进入 Experiment 指纹和 artifact manifest
- [ ] `ResearchExperiment(kind = model)` 固化 DataSnapshot、完整 FeatureSet 定义、周度标签、显式 train / valid / test 与两个 embargo 截面、processor、fit window、评价口径、白名单展开参数和四个派生种子；`seed` 只有顶层一个入口，`num_threads` 只属于运行身份
- [ ] `ModelExecutionSpec` 只从代码内可信 adapter 编译 Qlib 执行规范；重跑旧 Experiment 时完整定义必须匹配，数据库中的表达式或类名不得成为可执行配置，无法匹配时在初始化 Qlib 前失败 `experiment_execution_definition_unavailable`
- [ ] 时间切分严格按交易日历满足两个 `label_exit < feature_cutoff` 接缝；生产 processor 为 `InfToNaN`、`DropnaLabel`、`CSRankNorm(label)`，不做特征归一化或缺失填充，`fit_start_time` / `fit_end_time` 固定为 train 段
- [ ] 泄漏测试覆盖输入随机打散、两个 embargo 接缝、`ZScoreNorm` fit-window spy、特征真实读取上限，以及分别修改 test 标签和 test 特征的两条哨兵测试
- [ ] 训练和推理使用 `DataHandlerLP → DatasetH → CancellableLGBModel`；recorder 必须先由 `SignalRecord` 生成 test `pred.pkl` / `label.pkl`，再运行 `SigAnaRecord`，不得把缺少父记录导致的 skip 当作成功；三段产品指标统一由本地统计 module 计算
- [ ] 每个成功的 model ResearchRun 原子地产生一个不可变 `TrainedModel` 和一个不可变 `PredictionRun`；TrainedModel 保存模型文件 checksum、fit window、实际参数、runtime identity，以及完整 `inference_contract` 与 checksum，未来推理必须由可信 adapter 匹配该契约
- [ ] PredictionRun 只在成功发布后创建，保存每只 ResearchUniverse 成员的预测时点、`raw_score`、平均秩、`rank_percentile`、诊断用 `normalized_score`、label status、TrainedModel 及 DataSnapshot 引用；不包含 TargetPortfolio、持仓或 top-N 截断
- [ ] `ResearchArtifactPublisher` 替换旧 writer，并由 factor/model 共用；通过 `ResearchArtifactPublication(prepared | committed | failed)`、checksum 与幂等 finalize 协调文件系统、ResearchArtifact、TrainedModel、PredictionRun、ResearchRun 和 Task，故障注入覆盖 rename/commit 的全部崩溃接缝与路径冲突
- [ ] 研究页面可选择 Alpha158/Alpha360、发起训练、查看 phase、best-effort `cancel_requested`、失败原因及置顶 warnings；展示 train/valid/test 指标但仅以 test 为结论，并展示 IC/Rank IC、ICIR、五分组收益、特征重要性、缺失率、训练曲线和完整证券排名
- [ ] 同一次运行内使用同一 ResearchUniverse、标签、覆盖率门槛和统计 module 计算 LightGBM 与 6-1 动量的 test 指标；J-Quants Free 数据限制及实际观察点 `n` 与指标同屏，不给出“模型优于动量”的自动结论
- [ ] 新增独立的连续离线黄金 fixture（约 150 只证券 × 约 262 个交易日），不改写 05 的全市场 fixture；在生产 `min_valid_securities >= 100` 门槛下覆盖 Alpha158 完整链路，并为 Alpha360 提供资源受限但完整的 smoke/determinism 链路
- [ ] 普通 CI 无网络、无生产数据、无需真实 bundle：同容器、同机器和相同 `num_threads` 重跑时 `model.txt` checksum 与预测逐位相同；1/4 线程之间树结构、预测和研究指标相同，但不要求包含线程元数据的原始 `model.txt` checksum 相同

