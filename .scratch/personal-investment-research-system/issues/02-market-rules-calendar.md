# 02 — 市场规则配置与可替换交易日历接口

**What to build:** 定义 Instrument 和 TradingCalendar 核心数据模型，并提供日本 Prime Market 市场规则配置（交易日历与交易时段、结算周期、最小交易单位与价格精度、涨跌停/停牌规则占位、手续费税费占位、公司行动处理规则占位、现金结算规则），交易日历通过可替换的适配器接口访问，为后续所有依赖市场规则和交易日历的模块提供统一入口。

**Blocked by:** 01 — 项目脚手架与基础设施

**Status:** ready-for-agent

- [ ] Instrument 表包含 instrument_id、market、exchange、symbol、currency、status 等字段
- [ ] TradingCalendar 表包含 market、trade_date、is_open、session
- [ ] 市场规则以配置形式管理（非硬编码于业务逻辑中），可查询当前 Prime Market 规则集
- [ ] 交易日历访问通过可替换适配器接口暴露；第一版实现可为占位/空实现，但接口签名需支持未来替换为正式日历数据源
- [ ] 单元测试覆盖市场规则配置的读取
