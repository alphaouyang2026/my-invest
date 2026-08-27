# 10 — Qlib 研究回测与本地精确回测编排

**What to build:** 用户从回测中心创建一个历史区间任务后，系统以同一 ResearchExperiment 运行 Qlib ResearchBacktest，并按历史决策时点把 TargetPortfolio 逐期交给本地 Decimal 模块执行 ExecutionReplay；任务完成后保存两条路径的结果及差异报告，使研究速度和日本市场账务可信度可以同时获得。

**Blocked by:** 07 — Qlib 多因子与 LightGBM 预测选股；09 — 日本市场历史模拟成交与 Decimal 账务

**Status:** ready-for-agent

- [ ] 回测中心可选择数据快照、股票池、特征/模型或动量基线、组合策略、历史区间、初始资金、成本和结算模式创建任务
- [ ] BacktestRun 固定 ResearchExperiment、PredictionRun/分数来源、组合策略、数据快照、代码/Qlib/模型版本、市场规则、成本、随机种子和运行模式
- [ ] 同一个任务产出 Qlib ResearchBacktest 与本地 ExecutionReplay；本地结果是订单、成交、现金、持仓和净值的账务事实
- [ ] 差异报告逐期比较持仓、目标/实际权重、换手、成本和收益；超出配置容差时任务不得静默标记为可比较
- [ ] 后台任务由 PostgreSQL 任务表和单工作进程执行，支持排队、进度、失败原因和取消；取消结果不进入策略比较
- [ ] 已完成运行绑定不可变输入，相同快照、版本、参数和随机种子可安全从头重跑并产生一致结果
- [ ] 6-1 动量黄金基线可完成整个历史区间；至少一个 LightGBM PredictionRun 可完成预测、选股、TargetPortfolio、两层回测闭环
- [ ] 端到端测试覆盖正常完成、取消、失败清理、重跑复现以及两条回测路径的可解释差异

