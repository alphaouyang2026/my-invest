# 04 — 点时版本、数据质量校验与数据快照

**What to build:** 定义 TradingCalendar 模型和可替换的日历适配器，为导入数据附加点时元数据（event_time/publish_time/ingested_at/effective_from-to/source/source_version），执行数据质量检查（主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变），并允许用户在数据中心标记可用于回测的数据快照；严重错误批次被拒绝且不得用于回测，错误原因可追溯。

**Blocked by:** 03 — J-Quants 主数据与日线同步

**Status:** ready-for-agent

- [ ] 导入数据表包含 event_time/publish_time/ingested_at/effective_from/effective_to/source/source_version 字段
- [ ] TradingCalendar 表包含 market、trade_date、is_open、session；日历通过可替换适配器访问，接口支持后续替换正式数据源
- [ ] 同步目标日期判定覆盖 `HolDiv=2`（东证半日立会日）；03 遗漏的半日交易日 K 线在本票的重同步中补齐
- [ ] 质量检查规则覆盖主键重复、字段缺失、时间倒序、价格为负、成交量异常、OHLC 逻辑错误、停牌/零成交/交易日历一致性、复权因子异常跳变，检查结果可查询
- [ ] 复权及公司行动相关数据保留来源和版本；本 ticket 只校验复权一致性，不模拟分红、拆股等账户事件
- [ ] 严重错误的数据批次被标记且不可用于回测，保留错误原因
- [ ] 关键价格缺失时对应证券标记为不可交易；非关键字段缺失时排除证券并告警；缺失影响已持有证券估值或组合账务时整个任务失败（不静默产生结果）
- [ ] 用户可在数据中心创建/查看数据快照（snapshot_id、coverage_start/end、created_at、version），已完成回测绑定的快照不随后续数据修订自动改变
- [ ] 数据适配/质量测试覆盖日历替换、修订与覆盖范围场景

## Design decisions

经 `/grilling` 会话确认（Q1–Q31），实施前定稿。整张票不新增后台任务类型，全部挂在现有 `jquants_sync` 这一条 Task 内。

**点时元数据**

- 不重命名 03 已上线且有测试覆盖的字段。`event_time`≈`BarRecord.trade_date`、`source`=`BarRecord.source`、`ingested_at`≈`BarVersion.first_seen_at`、`publish_time`≈`EndpointPublication.published_at`、`source_version`≈`EndpointPublication.api_version`+`adapter_version`——后两者经 `publication_bar_observations` 关联可达，写映射文档而非复制列。
- `effective_from`/`effective_to` **不落列**，做成基于 `publication_bar_observations` 的派生视图。原因：A→B→A 修订回退时 `BarVersion` 会复用原行（`market_data.py:343-345`），单行上的一对 from/to 无法表达两段不相连的有效期。区间是观察记录的严格派生物，落成独立存储只会多一份能与事实源不一致的副本。

**交易日历**

- 新表 `trading_calendar`：`market`、`trade_date`、`is_open`、`session`、`publication_id`。每次日历发布写入**整份**日历并打上自己的 `publication_id`；修订靠 publication 隔离（旧 publication 的行永不被触碰），**不做**行级 version + current 指针。日历一年约 250 行，整份复制的代价可忽略，换来的是直接复用 `DataSnapshot.calendar_publication_id` 这个已存在的指针，不必引入第二套 `publish_sequence` 解析逻辑。
- 每次同步自动填充，**不回填** 03 已存的原始日历分页，从下次同步开始正向填充。
- `HolDiv` 四值映射（官方枚举：`0` 非営業日 / `1` 営業日 / `2` 東証半日立会日 / `3` 非営業日(祝日取引あり)）：`0`→休市、`1`→开市/`full_day`、`2`→**开市**/`half_day`、`3`→休市。
- `market` 硬编码 `"TSE"`（源日历本身不区分市场；填 `"JPX"` 会让 `HolDiv=3` 那些行的 `is_open` 无论填什么都是错的）。`session` 从 `HolDiv` 推导——那是源头给出的唯一真实 session 信息。
- **顺带修掉 03 的漏数据**：同步目标日期判定从 `HolDiv == "1"`（`jquants_sync_workflow.py:1484`）改为 `in {"1","2"}`。半日交易日是有真实成交的交易日，此前从未进入过同步目标；因本票要 purge 重同步，缺失 K 线自动补齐。不修的话，交易日历一致性规则会把我们自己的 bug 报成数据质量问题。
- `CalendarPort` 是 Protocol，绑定到某个 `calendar_publication_id`（而非整个 `DataSnapshot` 对象）——质量 pass 用本次 plan 的 publication、下游消费方用 `snapshot.calendar_publication_id`，同一条构造路径，也解开了"pass 运行时快照尚不存在"的先后问题。方法约为 `is_open` / `previous_open` / `next_open` / `window_back` / `open_days_between`。
- `window_back(end, n)` 返回**半开区间 `[.., end)`**——`end` 之前的 n 个开放交易日，**不含 `end` 当天**。决策日当天的价格不能进入信号，含进去就是前视偏差。这条语义绑住 05 的 20/21/126 日窗口，差一天会让所有回测结果整体偏移。
- 覆盖范围外抛 `CalendarCoverageError`，**绝不返回 `None`、绝不外推**。窗口向前数不满 n 天（Free 日历只有约 2 年，而 126+21 交易日约 7 个月，靠近覆盖起点必然数不满）同样抛错，不返回残缺窗口。Free 日历窗口永远止于约 12 周前，外推会在日本节假日上凭空造出交易日，静默污染 05 的动量窗口与之后每一次回测。
- bar 的 `session` 保持 `"full_day"` 不变（含义是"非日内切分"，非"完整长度时段"；`/v2/equities/bars/daily` 本身也无半日标识）。一致性规则**只比对 `is_open`**，两个 `session` 是不同词汇表，写进文档。把日历事实塞进 bar 身份键会让日历修订改变 bar 身份。

**质量规则：执行位置**

按规则性质拆开，这不是折中而是被数据模型逼出来的唯一自洽解：

- **行内规则**（单行内容的纯函数）在 `BarVersion` 插入时计算并写入。此时是首次写入，不可变性完好；因结论是 `content_hash` 的函数，回退复用行时天然一致。
- **上下文规则**在所有批次发布完、`_activate_snapshot` **之前**跑一遍独立 pass，结果写 findings 表，**绝不回写 `BarVersion`**。

**质量规则：行内**

- 存储：`bar_versions.quality_status` 由 `String(30)` 改为 `pg_enum`（`ok` / `excluded` / `untradable`，遵循 01 的 `app.db.types.pg_enum` 约定），另加一列规则代码数组保留"具体命中哪几条"。该列现无数据，改类型无迁移负担。
- 关键字段 = `raw_close` / `adjusted_close`（驱动估值与 06 的动量计算），缺失 → `untradable`。
- 非关键字段 = `raw_open`/`high`/`low`、成交量、成交额、`adjustment_factor`，缺失 → `excluded`。
- 价格为负 · 成交量为负 · OHLC 逻辑错误 → `untradable`。
- 标记只是标记：**不做物理排除**，该 bar 照常参与快照解析，由下游消费方（05 建池、06 算动量）按标记自行过滤。物理排除会在唯一的解析路径（`snapshot_reader.py:36-61`）上叠加隐式条件，且"排除"的正确尺度取决于消费方。

**质量规则：上下文**

| 规则 | 判据 | 分级 |
|---|---|---|
| 主键重复 | 同一 generation 内业务键重复 | 严重，一次即拒绝快照 |
| 时间倒序 | 源返回 `Date` ≠ 该页请求日期，或 `trade_date` 晚于摄取日 | 严重，一次即拒绝快照 |
| 交易日历一致性 | 开市日缺 K 线 / 休市日有 K 线 | 告警 |
| 停牌/零成交 | 开市日 `volume=0` | 告警 |
| 复权一致性 | `adjusted_*` 与 `raw_*` 的关系确由 `AdjFactor` 解释 | 告警 |

- 时间倒序按此定义**一次出现即严重**：它意味着请求与响应的对应关系已断，是系统性故障而非脏数据。明确**不算**异常的两种情况（同一 publication 内日期非按序、后发布的 publication 含更早 trade_date）写进文档，免得日后重新引入。
- 「成交量异常」**只剩负值这一种情况，完全退化为行内规则**，上下文部分取消。统计离群绝大多数是真实市场事件（财报、指数调仓），报成质量问题比不报更糟；"休市日却有成交量"本就是日历一致性的内容，不重复实现。
- 复权只校验一致性、不校验幅度（`:12` 的字面要求）；幅度边界 `[1/K, K]` 作为近乎零成本的护栏附带。
- 交易日历一致性**仅在日历覆盖 ∩ 行情覆盖的交集内评估**，交集外记「未评估」而非产生 finding。两个端点更新节奏不同（日历每年 3 月底批量发次年，行情逐日滚动），边缘必然错位；每次同步都刷边界告警会训练用户忽略 findings。
- 该 pass 额外承担一件事：**把行内结论按日聚合以套用升级阈值**。

**升级阈值与配置**

- 价格为负超过阈值 → 从证券级升级为快照级拒绝。分母 = **该日 publication 实际返回的 bar 行数**（规则实际检查过的总体），默认 1%。用主数据成员数当分母会把 bars 端点根本没返回的证券算进去，在数据缺失最严重时让规则最不敏感。
- 所有阈值、关键字段清单、findings 样本上限放在可注入的 `QualityPolicy` dataclass（照 `jquants_sync_workflow.py:88-91` 的 `SyncPolicy` 模式）。不用 pydantic `Settings`（改阈值要重启容器），不用数据库表（那是 14 的活）。

**findings 表**

- 锚 `sync_run_id` 而非 `snapshot_id`：pass 运行时快照尚不存在，而 `DataSnapshot` 上已有 `UniqueConstraint("sync_run_id")`（`market_data.py:427`），"快照↔findings"经由 run 是天然 1:1。额外好处是**没走到创建快照就失败**的 run 也留下可查的原因。
- 粒度 `(规则, 交易日)` + 命中数 + **有上限的**证券样本（JSONB）。升级阈值本就按日计算，按日聚合恰是决策所需粒度；逐 `(规则, 证券, 日)` 在 2 年 × 约 2000 只下会产生百万级行，而行内规则的细节可从 `quality_status` 反查。

**快照与可回测性**

- 快照仍由同步流程自动创建，**不新增手动创建入口**——平行的手动快照会产生两条路径生成同一种对象。
- 新增 `data_snapshots.is_backtest_eligible` + 原因指针。严重错误**不阻止**快照创建、也不阻止 `DataSnapshotHead` 前移；真正拒绝发生在回测绑定环节（10）。理由：质量只有在 03 的 publish 之后才可知，卡不住发布；而"错误原因可追溯"需要一个持久化对象来挂载原因。
- `is_backtest_eligible = false` 当且仅当存在**至少一条**快照级拒绝性 finding；告警永不参与合成（否则等于把刚划定的严重/告警界线偷偷改回去）。
- 创建时算一次，**永久冻结**，无人工覆盖。人工覆盖推给 10——现在没有任何回测会被这个字段挡住，覆盖动作没有可观察的后果。
- 空增量（`_inherit_snapshot`，`jquants_sync_workflow.py:940-967`）**跳过整个 pass**，原样继承上一快照的 `is_backtest_eligible` 与 findings 指针：它的 `bar_publish_sequence` 与上一个完全相同，解析出的每条 bar 版本逐字节一致，质量结论按构造必然相同。
- 质量 pass 自身抛异常（代码 bug 而非数据问题）时 **整个 Task 失败**，不建快照、head 不前移。崩掉的检查器两个答案都没给出；标成 `false` 会与"检查过、确实有问题"无法区分，用户会去追查不存在的数据问题。
- 新增 `SyncPhase.EVALUATING_QUALITY`，位于最后一个批次与 `ACTIVATING_SNAPSHOT` 之间。
- 「已完成回测绑定的快照不随后续数据修订自动改变」**无需新代码**：bar 侧靠 `publish_sequence <= cutoff` 已天然不可变，日历侧由 publication 隔离同样保证。

**数据中心**

- 新增 `data_snapshots.version`：按 source 单调递增整数，在 `_activate_snapshot` 已有的 `DataSnapshotHead` 行锁（`jquants_sync_workflow.py:875`）下取 `MAX(version)+1`。不用 Postgres sequence——做不到按 source 独立，且回滚留断号，断号在用户眼里像丢数据。
- 落在现有 `frontend/app/data-center.tsx` 页面内加区块，不新开页面：快照是同步的产物，拆成两页会割裂这层因果。
- 展示深度 = 快照汇总（可用性 + 各严重度命中数 + 拒绝原因）**加下钻**到逐规则、逐交易日的命中数与样本证券。只给汇总不算满足「检查结果可查询」；逐证券完整浏览需要已明确不存的明细。

**迁移**

- 再次 purge，**保留 `instruments`**（刻意设计的稳定身份层，本票没有任何东西让 `instrument_id` 失效）。旧行的 `quality_status` 全是默认 `"ok"` 却从未被检查过，留着等于让数据库断言一件假事。
- 用显式按序 `DELETE`，**不用 `TRUNCATE CASCADE`**——CASCADE 会顺着外键悄悄波及清单外的表（首当其冲 `instruments`），而显式清单存在的意义正是防这个。
- 顺序：`current_bars` → `publication_bar_observations` → `bar_versions` → `bar_records` → `data_snapshot_heads` → `data_snapshots` → `raw_source_pages` → `instrument_master_snapshot_members` → `instrument_master_snapshots` → `sync_target_dates` → `sync_batches` → `endpoint_publications` → `sync_runs` → `tasks WHERE task_type='jquants_sync'`。
- 迁移**自动生成**（01 的硬性约定，手写曾导致三次模型/迁移漂移）；新建的 native enum 必须在 `downgrade()` 里 `DROP TYPE`。

**测试**

- 在 `backend/tests/fakes.py` 写第二个**内存实现**的 `CalendarPort`（与现有 `FakeAdapter` 并列），用它驱动 05 式窗口查询，完全不依赖 J-Quants 数据。一个从未被真正替换过的 Protocol，"可替换"只是未经验证的声明。
- 覆盖：日历替换、日历修订、覆盖范围边缘（交集内/外）、升级阈值临界、空增量继承、pass 崩溃时 Task 失败。

**经验性不确定 —— 已用真实数据结清（2026-08-16，2024-05-24 ~ 2026-05-22，2,143,478 行）**

- **`AdjFactor` 公式已钉死**：`adjusted_close = raw_close × 后续 AdjFactor 累积乘积`，**舍入到 0.1 日元**。该舍入使拆股股票的 `adjusted/raw` 比值每日在第 5 位小数抖动——精确比较在真实数据上报 52,253 次，而实际公司行动仅约 500 次。规则改为比较"上一日比值推算出的 adjusted 价"，容差 0.15 日元（当日舍入 ±0.05 + 比值自身舍入 ±0.05 + 日间价格波动余量）。加容差后**误报归零**。仍保持仅告警、不参与升级判定。
- **`HolDiv=3` 已实证**：直接向 J-Quants 请求 `2025-05-05`、`2025-01-03` 两个 `HolDiv=3` 日期的日线，均返回**零行**。东证现货确实不交易，映射为休市正确。（注意：库内"这些日期无 K 线"**不能**作为证据——我们本来就没请求过它们，属循环论证。）
- **`HolDiv=2` 无法验证**：本次 Free 窗口（2 年）的日历中只出现 `HolDiv` 0/1/3，**一个半日交易日都没有**。修复本身正确且有测试覆盖，但缺乏真实样本佐证，待日后窗口内出现半日交易日再验。

**生产数据暴露、单元测试无法发现的两处缺陷（已修）**

- 质量 pass 原先把全部观测读进 Python 内存（210 万行 ORM 对象），worker 被内核连杀 3 次触及尝试上限。改为全部在 SQL 内聚合。测试用约 1000 行，生产是它的 2000 倍——这是规模缺陷，只有真实数据能暴露。
- 无成交日 J-Quants 返回的是 **NULL 而非 0**（92,772 行全字段 NULL，零行 volume=0）。原按 `== 0` 判断使 `no_trading_activity` 在生产中永不触发，而它恰是数据中占比最大的一类。

**明确不做（推给后续 ticket）**

- 分红/拆股等账户事件模拟、公司行动建模 → 出现真实用例时另行立项
- 缺失影响已持有证券估值/组合账务时整任务失败 → 09（当前代码库无任何 `Position`/`Portfolio` 模型，为不存在的消费方预搭钩子属于提前设计）
- 快照可回测性的人工覆盖 → 10
- 质量规则的用户可编辑界面 → 14
- 逐证券质量明细浏览
