# 12 — Qlib 辅助的历史重放与决策分支

**What to build:** 用户在历史重放页面按周推进时，只能看到当前 as_of 已知的数据、Qlib PredictionRun 和候选 TargetPortfolio，可以接受建议或填写自己的目标权重；推进后由本地历史模拟成交和 Decimal 账务计算结果。修改已经看到后续结果的决定会创建新分支，重放结束后比较用户、Qlib 策略和内部基准。

**Blocked by:** 11 — Qlib Portfolio 分析与策略比较

**Status:** ready-for-agent

- [ ] ReplaySession 和 ReplayDecision 保存 as_of、父分支、决策日期、所用 PredictionRun/TargetPortfolio、用户目标权重及数据快照
- [ ] 后端会话强制注入 as_of，Qlib 数据适配、预测、股票池和普通查询均不能读取 as_of 之后的信息；客户端请求未来日期会被拒绝
- [ ] 默认在每周最后一个实际交易日收盘后决策，并在下一实际交易日开盘模拟成交
- [ ] 页面同时展示 Qlib 预测排名、候选目标组合、约束警告和用户编辑后的目标权重
- [ ] 推进操作调用与 BacktestRun 相同的日本市场模拟成交和 Decimal 账务语义
- [ ] 修改已有后续结果的历史决定会创建不可覆盖的新分支，原分支的数据和已见结果保持不变
- [ ] 重放结束后比较用户、Qlib 策略和内部净基准的收益、回撤、换手、成本、持仓差异及信号一致程度
- [ ] 时间隔离和端到端测试覆盖 Qlib 特征/预测无未来泄漏、非交易周、分支创建以及用户与模型组合比较

