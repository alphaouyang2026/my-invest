# 日本股票量化研究

本上下文描述系统中数据、研究决策与模拟执行之间的核心概念，确保 Qlib 与本地模块使用一致语言。

## Language

**DataSnapshot（数据快照）**:
某次研究能够读取的数据事实及其发布上限的不可变引用，是所有实验和回测可复现性的起点。
_Avoid_: Qlib 数据集、最新数据

**QlibDataBundle（Qlib 数据包）**:
由一个 DataSnapshot、导出器版本和 pyqlib 版本确定性生成、供 Qlib 读取且可删除重建的数据集合。
_Avoid_: Qlib 缓存、数据源、Qlib 主库

**FeatureShardCache（特征分片缓存）**:
由一个 DataSnapshot、它所生成的 QlibDataBundle、FeatureSet 定义、标签周期、股票池策略和日期跨度确定性生成的派生特征存储；按证券分片、逐文件校验和封存，可随时删除重建，且不是任何事实的来源。与 QlibDataBundle 的区别在于内容：后者存行情事实，它存由行情算出的特征与标签。
_Avoid_: 数据源、Qlib 缓存、特征仓库

**DataBundleBuildAttempt（数据包构建尝试）**:
构建一个 QlibDataBundle 的单次执行记录；失败或重试不会改变已经结束的构建尝试。
_Avoid_: QlibDataBundle、ResearchRun

**ResearchExperiment（研究实验）**:
固定数据快照、研究股票池、特征集、标签、时间切分、模型、策略、参数和随机种子的不可变研究定义。
_Avoid_: 研究运行、回测任务、随手试验

**ResearchRun（研究运行）**:
执行一个 ResearchExperiment 的单次尝试，具有独立状态、日志、指标和产物；重试会产生新的 ResearchRun。
_Avoid_: 研究实验、回测运行

**FactorObservationDate（因子截面日）**:
对当时研究股票池中的全部证券计算因子分数，并在标签成熟后形成一次截面评价的交易日。
_Avoid_: 观察日、查看日期

**FeatureCutoff（特征截止点）**:
一个因子截面允许用于计算特征的最晚已知时点。
_Avoid_: 标签截止点、数据快照截止点

**LabelWindow（标签窗口）**:
从因子截面之后的可交易时点开始、仅用于事后评价因子预测能力的未来收益区间。
_Avoid_: 特征窗口、回测区间

**LabelCoverage（标签覆盖率）**:
一个因子截面中具有有效未来收益标签的证券数占 ResearchUniverse 成员数的比例；标签缺失不会反向改变研究股票域。
_Avoid_: 因子覆盖率、股票池覆盖率

**FactorCoverage（因子覆盖率）**:
一个因子截面中具有有效因子分数的证券数占 ResearchUniverse 成员数的比例。
_Avoid_: 标签覆盖率、行情覆盖范围

**ResearchUniverse（研究股票域）**:
由一个研究实验的股票池规则决定、并在每个因子截面日实际参与计算和评价的证券集合。
_Avoid_: Qlib instruments、当前股票名单

**ResearchArtifact（研究产物）**:
属于一个 ResearchRun、具有固定 schema 和内容身份的不可变详细结果。
_Avoid_: QlibDataBundle、数据库事实

**ResearchPrice（研究价格）**:
经过公司行动调整、用于跨期特征和未来收益标签的可比价格。
_Avoid_: 成交价格、原始价格

**ExecutionPrice（模拟成交价格）**:
历史市场当时实际报价、用于模拟订单成交和精确账务的未复权价格。
_Avoid_: 研究价格、Qlib 默认价格

**PredictionRun（预测运行）**:
某个已训练模型在固定 DataSnapshot 和预测时点上生成证券分数的不可变结果。
_Avoid_: 信号、推荐股票

**RankedScores（排序分数）**:
因子研究或模型预测向组合策略提供的统一证券分数与排名，不包含目标权重或持仓含义。
_Avoid_: PredictionRun、TargetPortfolio、持仓建议

**TargetPortfolio（目标组合）**:
由预测或规则策略产生、尚未经过成交与精确账务处理的证券目标权重集合。
_Avoid_: 持仓、成交组合

**ResearchBacktest（研究型回测）**:
Qlib 用于快速比较因子、模型、选股和组合策略的回测结果，不构成最终账务事实。
_Avoid_: 精确回测、模拟账户

**ExecutionReplay（执行重放）**:
本地模块按日本市场规则和定点数账务把 TargetPortfolio 转换为订单、成交、现金与持仓的可审计模拟。
_Avoid_: Qlib 回测、实盘执行
