# 09 — 模拟账户：现金、待结算现金、持仓与净值

**What to build:** 成交结果驱动虚拟现金、待结算现金、持仓、成本和净值更新，金额与数量使用定点数（禁止二进制浮点数）；集中配置市场结算周期，并支持两种现金结算模式（默认模式允许卖出所得同日调仓使用；严格结算模式在结算完成前不可用于买入）；模拟账户页面展示现金/持仓/净值时间序列。

**Blocked by:** 08 — 成交模型与模拟成交

**Status:** ready-for-agent

- [ ] SimAccount/SimPosition/PortfolioSnapshot 使用定点数记录 cash/unsettled_cash/nav/pnl/quantity/cost/market_value
- [ ] Prime Market 结算周期通过集中配置读取，严格结算模式按 TradingCalendar 的开放交易日计算可用日期
- [ ] 默认结算模式与严格结算模式均可配置并生效
- [ ] 账务恒等式（现金 + 持仓市值 = 净值等）由属性测试验证
- [ ] 模拟账户页面展示指定账户的现金、待结算现金、持仓和净值时间序列
- [ ] 内部时间统一使用 UTC，同时保留市场时区
