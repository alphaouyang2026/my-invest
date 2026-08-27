# 11 — Qlib Portfolio 分析与策略比较

**What to build:** 用户查看已完成 BacktestRun 时，可以基于本地精确账务获得日本市场口径的收益、风险、成本和归因，并复用 Qlib portfolio 分析比较因子、模型、选股及组合策略。页面同时展示 Qlib 研究结果、本地正式结果及两者差异，避免把研究近似值误认成模拟账务。

**Blocked by:** 10 — Qlib 研究回测与本地精确回测编排

**Status:** ready-for-agent

- [ ] 本地精确结果计算累计/年化收益、年化波动率、最大回撤、Sharpe、Sortino、Calmar、胜率、盈亏比、换手、交易次数和持有期
- [ ] 年化口径来自实际日本 TradingCalendar 或实验配置，不沿用固定 238 个交易日
- [ ] 显式费用、滑点、现金占用和无法成交的影响分开呈现，行业、市场和个股贡献可下钻到信号、目标组合、订单、成交和持仓变化
- [ ] 内部基准使用当时可投资股票池的月度等权组合，展示使用相同成本模型的毛收益与净收益；策略主要与净基准比较
- [ ] 分析页面明确区分 Qlib ResearchBacktest、本地 ExecutionReplay 和差异报告，并支持两个以上运行并列比较
- [ ] 比较维度包含数据快照、特征集、标签、模型、选股策略、组合策略、参数、成本和样本外区间
- [ ] 策略结论限定为 EXPERIMENTAL、PROMISING 或 INSUFFICIENT_DATA；J-Quants Free 阶段不能标记 VALIDATED
- [ ] 固定回放数据验证指标、基准、年化口径和归因结果，Qlib 与本地结果差异超差时有回归测试

