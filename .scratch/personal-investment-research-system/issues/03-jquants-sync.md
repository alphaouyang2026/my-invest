# 03 — J-Quants 主数据与日线同步

**What to build:** 交付一条可从数据中心页面触发和观察的 J-Quants API V2 Free 同步纵切。系统通过自有的薄 adapter 导入证券主数据、来源日历和全日日线：来源日历只用于发现账户实际可见的抓取日期，正式 TradingCalendar 领域模型与一致性校验留给 04；03 保存日线端点返回的全部证券事实，不按当前 Prime 名单提前过滤，Prime 普通股投资范围留给 05。同步必须幂等、可审计、可取消，在端点完整成功前不向正式查询暴露半批数据。

**Blocked by:** 01 — 项目脚手架与基础设施

**Status:** ready-for-agent

## Scope and invariants

- 用户只触发 `sync now`，不输入日期或全量/增量模式：首次运行抓取账户实际可见的完整历史；后续运行重取最近 30 个自然日；距离上次完整核对超过 30 天时自动执行完整核对。
- `/v2/markets/calendar` 在 adapter 内用于发现可见日期；`/v2/equities/bars/daily` 按 `date` 逐交易日请求并遍历 `pagination_key`；`/v2/equities/master` 使用日线实际 `coverage_end` 作为 `date`，使主数据快照与行情截止日对齐。
- 03 保存来源日历的原始响应、请求证据和本次目标日期，但不创建供股票池/回测使用的正式 TradingCalendar interface；04 负责规范化、校验和提供该领域能力。
- 日线端点返回的所有记录均保存；同步阶段不按当前市场或证券类别过滤。当前主数据无法匹配的代码先建立 `classification=unknown` 的来源证券身份，05 在构建历史股票池时再按点时证据筛选 Prime 普通股。
- 正式数据只读取状态为 `published` 的 endpoint publication generation。获取、分页、解析或结构校验失败的 generation 不可见，不改变此前已发布数据。
- 重复抓取不是按“日期曾抓过”跳过：相同业务键和相同规范化内容哈希为 `unchanged`；相同业务键而内容哈希不同为上游修订，插入不可变新版本并在发布后切换 current pointer，旧业务值不物理覆盖。
- 一个 `sync now` 创建一对一的 `SyncRun` 和 `Task`。目标日期冻结后在同一 Task 中分成多个 `SyncBatch`，由 Workflow 按 ordinal 连续执行，batch size 只是内部资源粒度，不截断本次业务目标。

## Data model

- `Instrument` 保存稳定内部身份：`instrument_id`（UUID）、`source`、`source_code`、`exchange`、`currency`、`created_at`；`(source, source_code)` 是首版外部身份映射，明确不承诺识别证券代码被重新使用的情况。
- `InstrumentMasterSnapshot` 至少保存 `snapshot_id`、`source`、`as_of_date`、`sync_run_id`；成员表保存 `instrument_id`、原始代码与名称、市场代码/名称、行业/规模字段、推导证券类别和 `content_hash`。
- 市场归属、名称和上市状态不覆盖写入 Instrument。成员出现在某个快照中只表示来源在该 `as_of_date` 返回了它；缺席不得自动推断为退市。
- Prime/Standard/Growth 分别保留来源 `Mkt`；普通股分类根据 JPX 五位代码规则推导（普通股末位 `0`），同时保存分类方法/版本。03 不用该分类删除行情。
- 全日日线业务键为 `source + instrument_id + trade_date + session`，`session` 首版固定为 `full_day`；同时保存原始 OHLCV/成交额、调整后 OHLCV、调整因子、首次/最近观察时间和内容哈希，数值使用精确定点数。
- `SyncRun` 和逐端点 publication 记录运行模式、adapter/API 版本、响应 schema 指纹、请求参数、目标/已处理日期、页数、总行数、新增/未变/修订行数、实际最早/最晚日期、开始/结束时间、状态和脱敏错误摘要。

## Adapter, security, and failure behavior

- 使用同步 `httpx.Client` 实现只覆盖所需 V2 端点的薄 adapter；调用者不接触认证、分页、限流、重试或 J-Quants 字段名。
- Task Runner 是独立常驻进程，整个部署只允许一个 Worker。Worker 每次领取一个最早的 `queued` Task，在当前线程同步、串行运行整个 handler；handler 返回前不领取下一个 Task，也不并行执行多个 SyncBatch。
- `POST sync now` 的同步 HTTP 路由只在事务中创建 SyncRun/Task 并返回 `202` 与标识符，不在 Web 请求内调用 J-Quants。同步 `httpx.Client` 只在独立 Worker 进程中阻塞执行。
- Worker 启动时持有 PostgreSQL session-level advisory lock 作为单例保护；第二个 Worker 取锁失败必须退出。本 ticket 不引入 Activation、lease、heartbeat、fencing token 或运行中多 Worker 接管。
- 所有请求共享限流器，相邻请求至少间隔 12.5 秒。网络错误、HTTP 408/429/5xx 最多重试 5 次；429 优先遵守 `Retry-After`，否则使用带随机抖动的指数退避。其他 4xx 不重试。连接超时 10 秒、读取超时 60 秒，任务本身不设整体超时。
- API key 只允许通过 `JQUANTS_API_KEY` 或 `JQUANTS_API_KEY_FILE` 配置；两者同时存在时启动失败并报告配置冲突。日志、错误、数据库、UI 和导出中不得出现 key、认证 header、key 片段或密钥文件路径。
- 同一时刻最多一个 J-Quants 同步处于 queued/running；重复触发返回已有活动任务。用户可取消任务，worker 在请求之间检查取消状态，但不打断 publication 的最终短事务。
- 同次运行可重试当前页；新运行不复用旧 `pagination_key`，从目标范围起点重抓并依靠业务键与内容哈希去重。
- Worker 进程崩溃时不通过 lease 超时启动另一 Worker。由 Docker/systemd/Kubernetes 重启唯一 Worker；新进程取得单例锁后，将遗留 `running` Task 重新置为 `queued`，再次领取时增加有上限的 `attempt_count`，并依 Calendar/SyncBatch/publication 持久化 checkpoint 从第一个未发布批次继续。
- 首次日线为空、与 `coverage_end` 对齐的主数据为空、必需字段缺失/类型错误、或同一运行内相同业务键内容冲突时，该端点失败。增量日线为空可成功标记为 `no_change`，且不得清空已有数据。合法的空 OHLC/成交量进入 03，由 04 判断质量；未知新增字段保留在原始响应中但不阻塞发布。
- 原始响应按 `publication_id + page_index` 脱敏后保存为 JSONB，默认保留 90 天；同时清理到期的失败/未发布 generation 和临时 staging。规范化版本、主数据快照、SyncRun/publication 元数据、覆盖范围和内容哈希永久保留。

## Data center experience

- 页面显示数据源 `not_configured/configured/invalid` 状态、醒目的 Free 数据约 12 周延迟/约两年窗口说明，以及最近发布数据的实测覆盖范围。
- 页面提供“立即同步”和取消操作；存在活动任务时禁用重复创建并链接到已有任务。
- 运行详情展示当前端点、`processed_dates / target_dates`、当前日期、页数、行数、新增/未变/修订计数、逐端点发布状态和脱敏错误；不展示虚假预计完成时间、原始 payload 或秘密信息。
- 通用 Task 任一端点失败时为 `failed`；独立的 SyncRun 可为 `partial_failed`，并准确显示哪些端点已经完整发布。

## Acceptance criteria

- [ ] 从数据中心触发 `sync now` 后，后台任务按来源日历发现的可见交易日逐日导入 J-Quants V2 Free 全日日线；首次全量、30 日重叠增量和超过 30 天后的完整核对模式均按定义选择目标日期
- [ ] 一次 `sync now` 只创建一个 SyncRun/Task；单 Worker 在同一 handler 中同步、串行执行所有 SyncBatch，整个 Task 完成前不领取其他 Task
- [ ] Instrument 稳定身份与带 `as_of_date=coverage_end` 的主数据快照分离；市场/名称等可变属性保存在快照成员中，当前缺席不会被推断为退市
- [ ] 日线不按当前 Prime 名单提前过滤；未匹配主数据的来源代码仍被保存为 `classification=unknown`，并且普通股/市场分类规则有来源和版本
- [ ] 原始与调整后 OHLCV、成交额及调整因子使用精确定点数保存；业务键、内容哈希和不可变版本使重复同步为 no-op、上游修订可追溯且旧值不被物理覆盖
- [ ] 分页、共享 5 calls/min 限流、429 `Retry-After`、指数退避、超时、取消和从头安全重跑均有确定行为及离线测试
- [ ] generation 在完整获取和结构校验后原子发布；失败、取消、空首批或冲突批次不会污染此前正式数据，端点级部分成功可查
- [ ] API key 的互斥配置和全链路脱敏生效；日志、数据库、UI、错误和导出不包含秘密信息
- [ ] 每个端点记录 adapter/API 版本、schema 指纹、请求范围、实际最早/最晚日期、页数、行数和新增/未变/修订计数；原始 payload 与临时数据按 90 天策略清理，永久发布证据不被删除
- [ ] 数据中心可触发/取消同步并展示配置状态、Free 延迟警告、实际覆盖范围、活动任务、日期进度、逐端点状态和脱敏失败原因
- [ ] 自动化测试使用脱敏 fixtures、可注入 HTTP transport/时钟/随机源和真实 PostgreSQL，覆盖分页、限流、重试、schema 变化、空响应、去重、修订、generation 可见性及部分失败；普通 CI 不调用真实 J-Quants
- [ ] 测试覆盖 Task Runner 单例锁、第二 Worker 拒绝启动、Worker 重启后遗留 RUNNING Task 重新排队、恢复时跳过已发布批次，以及超过 `attempt_count` 上限后明确失败
- [ ] 使用本机 Free key 完成 calendar、master 和至少一个交易日 bars 的手工 smoke sync，并只记录脱敏的端点状态、字段集合和实测日期；未完成该 smoke test 时 ticket 标记为等待真实数据源验收

## Explicitly deferred

- TradingCalendar 的正式表、可替换业务 interface、日历/行情一致性和质量状态 → 04
- 历史时点 Prime 普通股资格、`classification=unknown` 处置和幸存者偏差警告 → 05
- 财务摘要与决算发表日历 → 13
- 定时调度、用户自选日期范围、CSV bulk、分钟/Tick、TOPIX 和其他付费端点不属于本 ticket

## Comments

**已知缺陷：半日交易日从未被同步（由 04 的 grilling 会话发现，修复归 04）**

`_calendar_dates()`（`jquants_sync_workflow.py:1484`）只把 `HolDiv == "1"` 的日期纳入同步目标。但 J-Quants V2 的 `HolDiv` 有四个取值，不是两个（官方枚举：<https://jpx-jquants.com/ja/spec/mkt-cal/holiday-division>）：

| 值 | 官方英文 | 官方日文 |
|---|---|---|
| `0` | Non-business day | 非営業日 |
| `1` | Business day | 営業日 |
| `2` | Day of TSE Half-Day Trading Sessions | 東証半日立会日 |
| `3` | Non-business days (with holiday trading) | 非営業日(祝日取引あり) |

`HolDiv=2` 是**东证半日立会日**——有真实成交、有真实 K 线的交易日，但当前实现把它当成非交易日跳过，这些日期的日线从未被抓取过。`HolDiv=3` 是大阪交易所的假日衍生品交易，对本系统（东证现货股票）应视为休市，跳过是正确的。

修复不在本票：04 会把判定改为 `HolDiv in {"1","2"}`，并因为其迁移本就要 purge 重同步，缺失的半日 K 线随之补齐。若不修，04 的交易日历一致性规则会把每一个半日交易日报成"开市日缺 K 线"——把本系统自己的 bug 当成数据质量问题上报。

顺带记录两条同源事实，供后续 ticket 参考：

- 日历端点响应**只有 `Date` 和 `HolDiv` 两个字段**，无市场/交易所维度、无 session 信息，也**没有 `pagination_key`**（现有 `_fetch_all` 的分页循环在此跑一轮即退出，无害）。
- Free 档日历的数据窗口是「12 周前 ~ 2 年 12 周前」，**不含未来日期**（付费档可取到次年年末）。日历每年 3 月底左右批量发布次年数据——这是日历会被真实修订的原因。
