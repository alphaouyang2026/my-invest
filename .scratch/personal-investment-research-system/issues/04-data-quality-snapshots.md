# 04 — 点时版本、数据质量校验与数据快照

**What to build:** 为导入数据附加点时元数据（event_time/publish_time/ingested_at/effective_from-to/source/source_version），执行数据质量检查（主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变），并允许用户在数据中心标记可用于回测的数据快照；严重错误批次被拒绝且不得用于回测，错误原因可追溯。

**Blocked by:** 03 — J-Quants 主数据与日线同步

**Status:** ready-for-agent

- [ ] 导入数据表包含 event_time/publish_time/ingested_at/effective_from/effective_to/source/source_version 字段
- [ ] 质量检查规则覆盖主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变，检查结果可查询
- [ ] 严重错误的数据批次被标记且不可用于回测，保留错误原因
- [ ] 关键价格缺失时对应证券标记为不可交易；非关键字段缺失时排除证券并告警；缺失影响已持有证券估值或组合账务时整个任务失败（不静默产生结果）
- [ ] 用户可在数据中心创建/查看数据快照（snapshot_id、coverage_start/end、created_at、version），已完成回测绑定的快照不随后续数据修订自动改变
- [ ] 数据适配/质量测试覆盖修订与覆盖范围场景
