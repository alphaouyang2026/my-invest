# 06 — Qlib 数据适配与动量因子研究

**What to build:** 用户选择一个可研究的 DataSnapshot、因子截面日期范围和 6-1 动量窗口后，可以执行一次可复现的 ResearchRun。系统按需构建并复用 QlibDataBundle，用 Qlib 表达式计算日度动量和未来收益标签，冻结每日 ResearchUniverse，产出日度诊断及周度黄金策略口径的 IC、Rank IC、覆盖率和五分组收益，并通过研究页面与 RankedScores interface 发布完整、可追溯的结果。本票不生成 TargetPortfolio，不运行 ResearchBacktest，也不产生订单、成交或模拟账户账务。

**Blocked by:** 05 — Prime 股票池构建与流动性过滤

**Status:** ready-for-agent

**系统设计：** [06 Qlib 动量因子研究设计](../06-design-doc.md)。实施前完整阅读；精确的数据字段、标签、统计、状态和失败语义以该文档为准。

- [ ] 后台 worker 使用锁定的 `pyqlib==0.9.7` 运行受控的动量研究；API 进程不初始化 Qlib，用户不能提交任意表达式、类路径或 workflow 配置
- [ ] 一个合格 DataSnapshot 可按需生成经过完整校验、原子发布且可删除重建的 QlibDataBundle；相同 snapshot、pyqlib 和导出器身份生成相同逻辑数据
- [ ] 不可变 ResearchExperiment 与逐次 ResearchRun 正确分离；相同定义复用 Experiment，每次终态后重跑创建新 Run，并完整记录实际运行环境、状态、警告和产物身份
- [ ] 每日因子截面严格冻结当时的 ResearchUniverse 和 FeatureCutoff；日度 5 日标签及周度实际调仓标签只在评价阶段读取未来数据，缺失标签不反向改变股票域
- [ ] Qlib 表达式按可配置的 126/21 交易日默认窗口计算 6-1 动量；行级质量、路径完整度、缺失值、因子覆盖率和标签覆盖率按设计文档处理
- [ ] 研究结果包含日度与周度 IC/Rank IC、覆盖率、分布、五分组等权收益和最高减最低组收益；无定义统计保持 unavailable，周度口径作为黄金策略主结论
- [ ] ResearchArtifact 以带 schema/version/checksum 的 Parquet 与 JSON 原子发布，包含成员、有效/无效分数和标签、原因码及时间序列；06 的因子产物通过 RankedScores interface 供 08 使用
- [ ] 研究页面支持默认黄金实验、任务进度、取消、失败原因、数据包磁盘空间和结构化警告；离线黄金测试、时间隔离测试及生产容器内的真实 Qlib 集成测试通过
