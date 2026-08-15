# 04 — 点时版本、数据质量校验与数据快照

**What to build:** 定义 TradingCalendar 模型和可替换的日历适配器，为导入数据附加点时元数据（event_time/publish_time/ingested_at/effective_from-to/source/source_version），执行数据质量检查（主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变），并允许用户在数据中心标记可用于回测的数据快照；严重错误批次被拒绝且不得用于回测，错误原因可追溯。

**Blocked by:** 03 — J-Quants 主数据与日线同步

**Status:** ready-for-agent

- [ ] 导入数据表包含 event_time/publish_time/ingested_at/effective_from/effective_to/source/source_version 字段
- [ ] TradingCalendar 表包含 market、trade_date、is_open、session；日历通过可替换适配器访问，接口支持后续替换正式数据源
- [ ] 质量检查规则覆盖主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变，检查结果可查询
- [ ] 复权及公司行动相关数据保留来源和版本；本 ticket 只校验复权一致性，不模拟分红、拆股等账户事件
- [ ] 严重错误的数据批次被标记且不可用于回测，保留错误原因
- [ ] 关键价格缺失时对应证券标记为不可交易；非关键字段缺失时排除证券并告警；缺失影响已持有证券估值或组合账务时整个任务失败（不静默产生结果）
- [ ] 用户可在数据中心创建/查看数据快照（snapshot_id、coverage_start/end、created_at、version），已完成回测绑定的快照不随后续数据修订自动改变
- [ ] 数据适配/质量测试覆盖日历替换、修订与覆盖范围场景
