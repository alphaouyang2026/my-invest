# 04 实施步骤

配套 ticket：[`issues/04-data-quality-snapshots.md`](issues/04-data-quality-snapshots.md)（设计决定见其 `## Design decisions`，Q1–Q31 已定稿）。

本文件只讲**怎么做、按什么顺序做**；**为什么这么做**在 ticket 里，不重复。

## 三个决定顺序的既有事实

实施前已核实，都影响排序：

1. **测试的 schema 由 `Base.metadata.create_all` 建立，不走迁移**（[`backend/tests/conftest.py:64`](../../backend/tests/conftest.py#L64)）。
   → 模型与测试可一路推进，**迁移只在最后生成一次**，不必逐步插 migration。
2. **`_freeze_plan` 是日历落库的准确挂载点**（[`jquants_sync_workflow.py:436-440`](../../backend/app/services/jquants_sync_workflow.py#L436-L440)）。它的 docstring 写明"在同一事务里发布日历并写完整计划"——publication-scoped 的日历行正该进这个事务，直接继承已有的原子性保证。
3. **`source_calendar_code="1"` 是硬编码的**（[`jquants_sync_workflow.py:480`](../../backend/app/services/jquants_sync_workflow.py#L480)）。`HolDiv=2` 纳入后，它应记录该日实际的 HolDiv 值，属于 Q21 的连带修改。

## 步骤

### 步骤 0 · 基线

跑通现有测试全绿，确认 `db-test` 服务在跑。后续每一步都对照这条基线，避免把既有失败误认成新引入的。

### 步骤 1 · 日历规范化的纯函数层

**规模**：小 · **依赖**：无 · **无 DB，纯单元测试**

把 [`_calendar_dates()`](../../backend/app/services/jquants_sync_workflow.py#L1481-L1486) 换成 `normalize_calendar(rows) -> list[CalendarDay]`，`CalendarDay` 携带 `(trade_date, hol_div, is_open, session)`。`HolDiv` 四值映射在这里落地：

| HolDiv | is_open | session |
|---|---|---|
| `0` 非営業日 | `False` | — |
| `1` 営業日 | `True` | `full_day` |
| `2` 東証半日立会日 | `True` | `half_day` |
| `3` 非営業日(祝日取引あり) | `False` | — |

**这一步即修掉 03 的半日交易日漏数据**（见 `issues/03-jquants-sync.md` 的 `## Comments`）。两个调用点（[`:397`](../../backend/app/services/jquants_sync_workflow.py#L397)、[`:427`](../../backend/app/services/jquants_sync_workflow.py#L427)）同时受益，因为这是唯一收口。

必测：四个取值各一条；**未知取值**（源头哪天加个 `4` 不该让同步崩掉）；空响应；重复日期去重。

> **待确认的 seam**（上次会话中断在此）：
> 新模块 `backend/app/services/calendar_normalization.py`，公开 `CalendarDay` 与 `normalize_calendar`；测试落 `backend/tests/test_calendar_normalization.py`。
> 依据：[`sync_planner.py`](../../backend/app/services/sync_planner.py) 已经是"纯函数 + frozen dataclass + 模块 docstring 讲清职责边界"的先例，且有配套的 `test_sync_planner.py`。seam = 该模块的公开函数，**不测** workflow 内部、**不测** 私有函数。

### 步骤 2 · `trading_calendar` 表与写入

**规模**：中 · **依赖**：步骤 1

- 建模型：`market`、`trade_date`、`is_open`、`session`、`publication_id`。
- `_freeze_plan` 的 `visible_dates: list[date]` 参数改为携带完整规范化日历——**计划只要开市日，但落库要全部日期**（含休市日，一致性规则需要）。
- 在发布日历 publication 的同一事务写入整份日历。
- `source_calendar_code` 硬编码改为实际 HolDiv 值。

必测：整份日历落库；**修订场景**——第二次同步产生独立的整份日历，旧 publication 的行逐字节未变。

### 步骤 3 · `CalendarPort`

**规模**：中 · **依赖**：步骤 2

- Protocol + `CalendarCoverageError`。
- DB 实现，绑 `calendar_publication_id`（不绑整个 `DataSnapshot` 对象）。
- `backend/tests/fakes.py` 内存实现，与现有 `FakeAdapter` 并列。

关键测法：**两个实现跑同一组断言**——这才是"可替换"的实证，而不是"Protocol 存在"。另必测覆盖范围外抛异常（Q29 的全部价值所在）。

### 步骤 4 · `QualityPolicy` 与行内规则

**规模**：中 · **依赖**：无（可与 1–3 并行）

- `QualityPolicy` dataclass，照 [`SyncPolicy`](../../backend/app/services/jquants_sync_workflow.py#L88-L91) 的模式。
- `bar_versions.quality_status` 由 `String(30)` 改 `pg_enum`（`ok`/`excluded`/`untradable`），加规则代码数组列。
- 在 `_observe_rows` 插入 `BarVersion` 时评估。

必测：各关键/非关键字段缺失；负价；OHLC 逻辑错误；多规则同时命中；**回退复用行（A→B→A）时结论一致**——这是把行内规则放在插入时的全部理由。

### 步骤 5 · findings 表与上下文 pass

**规模**：大（本票重心） · **依赖**：步骤 3、4

- findings 模型：锚 `sync_run_id`，粒度 `(规则, 交易日)` + 命中数 + 有上限的证券样本。
- pass 服务：4 条上下文规则 + 行内结论按日聚合套升级阈值。
- 加 `SyncPhase.EVALUATING_QUALITY`。
- 接进 workflow：`MASTER` 之后、`_activate_snapshot` 之前。
- pass 抛异常 → 整个 Task 失败（不建快照、head 不前移）。

必测：逐条规则；日历∩行情覆盖的交集内外；阈值临界（0.9% 不升级 / 1.1% 升级）；pass 崩溃时 Task 确实失败。

### 步骤 6 · 快照可回测性与 version

**规模**：中 · **依赖**：步骤 5

- `data_snapshots` 加 `is_backtest_eligible` + `version`。
- `_activate_snapshot` 合成 eligibility；行锁下取 `MAX(version)+1`。
- `_inherit_snapshot` 继承结论与 findings 指针，跳过 pass。

必测：拒绝性 finding → `false`；**告警不影响 eligibility**（容易写反）；version 连续无断号；空增量继承。

### 步骤 7 · 点时派生视图与映射文档

**规模**：小 · **依赖**：无

`effective_from/to` 的派生查询（基于 `publication_bar_observations`）；七个点时字段到现有列的映射写进文档。

### 步骤 8 · 迁移

**规模**：中 · **依赖**：步骤 1–7 全部完成（模型定稿）

1. `alembic revision --autogenerate`（01 的硬性约定：**绝不手写**）。
2. 手工补 `_purge_previous_sync_data`，顺序：
   `current_bars` → `publication_bar_observations` → `bar_versions` → `bar_records` → `data_snapshot_heads` → `data_snapshots` → `raw_source_pages` → `instrument_master_snapshot_members` → `instrument_master_snapshots` → `sync_target_dates` → `sync_batches` → `endpoint_publications` → `sync_runs` → `tasks WHERE task_type='jquants_sync'`。
   **保留 `instruments`**；用显式按序 `DELETE`，不用 `TRUNCATE CASCADE`。
3. ⚠️ **人眼检查 autogenerate 有没有正确处理 `String(30)` → `pg_enum` 这个类型变更**——本步最可能出错的地方。
4. `downgrade()` 里给每个新建 enum 补 `DROP TYPE`。
5. 验证 upgrade→downgrade→upgrade 往返不炸。

> ⚠️ 从这一步起本地数据清空，所以真实数据验收必须排在其后。

### 步骤 9 · API 与前端

**规模**：中 · **依赖**：步骤 6、8

- 快照列表/详情端点；findings 下钻端点。
- 重新生成 TS client（`openapi-typescript` 脚本）。
- [`frontend/app/data-center.tsx`](../../frontend/app/data-center.tsx) 加快照区块与下钻；[`PHASE_LABELS`](../../frontend/app/data-center.tsx#L42-L49) 补 `EVALUATING_QUALITY` 的中文标签。
- 前端测试。

### 步骤 10 · 真实数据验收

**必须做，不能只靠 fixtures。** 用本机 Free key 重同步，然后：

1. **钉死 `AdjFactor` 公式** —— 拿真实的公司行动日验证 `adjusted_*` 与 `raw_*` 的关系。公式确认前，复权一致性规则不参与任何升级判定。
2. **抽查 `HolDiv=3` 的 `is_open`** —— 验证"东证休市、大证交易"这个推论是对的。官方文档未单独讲明，判反会让一批日期的开闭市状态反向。
3. **确认半日交易日的 K 线真的补上了** —— 找一个 `HolDiv=2` 的日期，查它有没有 bar。

## 依赖关系速览

```
0 基线
├─ 1 日历规范化 ─→ 2 trading_calendar ─→ 3 CalendarPort ─┐
└─ 4 行内规则 ───────────────────────────────────────────┴─→ 5 findings + pass ─→ 6 快照可用性 ─┐
   7 点时派生视图（独立）────────────────────────────────────────────────────────────────────┴─→ 8 迁移 ─→ 9 API/前端 ─→ 10 真实数据验收
```

1→2→3 打底（日历），4→5→6 是质量主体，7 收尾，8 定稿，9 出口，10 兜底。步骤 5 是重心，其余相对机械。

## 两处必须在步骤 10 关掉的风险

来自 ticket 的「未解的经验性不确定」，此处只做提醒：

- `AdjFactor` 与 `AdjO/AdjC` 的确切代数关系**未查证**。规则按"仅告警"落地，公式钉死前不参与升级判定。
- `HolDiv=3` = "东证休市、大证衍生品交易"是**推论**，官方未单独讲明。
