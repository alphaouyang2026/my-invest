# J-Quants 连续批次同步模型设计

- 状态：Revised Proposal
- 日期：2026-08-16
- 取代：本文档的初版设计
- 审查依据：[最新复审意见](./new_review.md)
- 历史审查：[初版设计审查](./jquants-continuous-batch-sync-review.md)（其阻塞结论已由本文修订取代）
- 关联规范：`docs/personal-investment-research-system-spec.md` 第 6.4、11、16.2 节
- 影响范围：市场数据模型、同步 Workflow、单 Worker 同步 Task Runner 恢复、数据快照与回测读取

## 1. 决策摘要

本设计采用以下模型：

```text
一个用户 sync now
  └─ 一个 SyncRun                         业务运行与冻结计划
       ├─ 一个 Task                         单 Worker 中可重试的同步执行载体
       ├─ 一个 Calendar publication
       ├─ 多个 SyncBatch
       │    └─ 每次尝试一个 Bars publication
       │         └─ PublicationBarObservation
       │              └─ 复用不可变 BarVersion
       ├─ 零或一个 Master publication / snapshot
       └─ 成功后创建一个不可变 DataSnapshot
            └─ DataSnapshotHead 原子指向最新完整快照
```

核心语义：

1. `SyncRun` 表示一次完整同步操作，批次只是 implementation 细节。
2. 一个 `Task` 与 `SyncRun` 一对一；单 Worker 同步、串行执行，恢复时重新排队同一 Task。
3. `BarVersion` 只表示不可变行情内容，不再属于单一 publication。
4. `PublicationBarObservation` 表示某个 publication 观察并选择了哪个版本。
5. 每个批次独立原子发布，失败批次不改变 `CurrentBar`。
6. 只有整个运行成功后才创建 `DataSnapshot`；回测绑定 snapshot，而不是读取当前行情。
7. `DataSnapshot.bar_publish_sequence` 冻结当时全部已发布行情事实；`coverage` 表示累计可读范围，`verified` 表示本次成功核验窗口。
8. 仅增量模式允许空 bars 结果成功；它继承当前 head 的行情 cutoff、coverage 和 master，创建语义等价的新 snapshot。
9. 普通 `sync now` 不接受日期范围；内部 `batch_size` 属于 `SyncPolicy`，不属于用户 interface。

## 2. 目标与非目标

### 2.1 目标

- 用户一次触发即可完成首次历史导入、30 自然日增量重取或完整核对。
- 初始和完整核对可以持续数小时，并显示稳定的总体进度。
- Worker 进程崩溃或可重试错误后，进程管理器重启唯一 Worker，并从第一个未发布批次恢复。
- 相同内容、历史版本恢复和失败重试都不修改旧 publication 的审计关系。
- 部分发布的市场事实不能被误当作完整回测快照。
- 滚动来源窗口之外已保存的旧行情可以继续进入累积快照，但必须与本次成功核验窗口明确区分。
- 同一数据源同一时间最多有一个活动运行；整个部署最多有一个 Task Runner 进程。

### 2.2 非目标

- 不并行调用 J-Quants 日线端点。
- 不在一个 Task 执行期间领取另一个 Task。
- 不引入 Redis、Kafka 或分布式队列。
- 不支持多 Worker 竞争、lease/heartbeat 超时接管或 fencing token。
- 不提供公开的用户自选日期范围。
- 不保证整个多小时运行全局原子；原子单位是单个 publication generation。
- 不在运行期间动态扩大已冻结计划。
- 不在本设计中实现定时同步。

## 3. 领域术语

| 术语 | 定义 |
|---|---|
| SyncRun | 用户发起的一次完整同步业务运行，拥有不可变计划和总体结果 |
| Task | 后台队列记录；单 Worker 同步执行，并以 `attempt_count` 记录重新排队次数 |
| SyncBatch | 完整日期计划中的一个顺序分片，是日线 publication 的原子范围 |
| EndpointPublication | 一个 endpoint generation；STAGING 时不可见，PUBLISHED 后成为正式市场事实 |
| BarRecord | 一条日线事实的稳定业务身份：来源、证券、交易日、session |
| BarVersion | BarRecord 的一种不可变内容版本 |
| PublicationBarObservation | publication 对一个 BarRecord 观察到的 BarVersion |
| CurrentBar | 面向当前查询的已发布版本指针，不用于冻结回测 |
| DataSnapshot | 一次完整同步成功后创建的不可变累积研究数据视图，区分累计 coverage 与本次 verified window |
| DataSnapshotHead | 每个来源当前推荐使用的完整 DataSnapshot 指针 |

## 4. Module 与 interface

连续批次编排位于 `JQuantsSyncWorkflow` 深 module 中。HTTP 层和 Worker 只跨该 module 的 interface，不管理 planner、批次循环、publication 或 checkpoint 恢复。

```python
@dataclass(frozen=True)
class SyncNow:
    """普通用户的幂等同步命令；没有日期或批次参数。"""


@dataclass(frozen=True)
class SyncOutcome:
    run_id: uuid.UUID
    status: SyncRunStatus
    mode: SyncMode
    snapshot_id: uuid.UUID | None
    coverage_start: date | None
    coverage_end: date | None


class JQuantsSyncWorkflow:
    def start(
        self,
        command: SyncNow = SyncNow(),
        *,
        idempotency_key: str | None = None,
    ) -> SyncRunView: ...

    def execute(
        self,
        run_id: uuid.UUID,
    ) -> SyncOutcome: ...

    def inspect(self, run_id: uuid.UUID) -> SyncRunView: ...
    def request_cancel(self, run_id: uuid.UUID) -> SyncRunView: ...
    def resume(self, run_id: uuid.UUID) -> SyncRunView: ...
```

Interface 约束：

- `start()` 在同一事务内取得来源锁，创建一对一的 `SyncRun` 和 `Task`。
- 相同 `idempotency_key` 返回同一运行；没有 key 时，活动运行会被合并返回。
- `execute()` 由唯一 Worker 同步调用，且对同一个 `run_id` 幂等；返回前已在同一事务中终结 SyncRun 和 Task。
- `resume()` 复用冻结计划和已发布 checkpoint，将原 Task 重新置为 QUEUED，不创建新 Task。
- `request_cancel()` 只请求取消；Workflow 到达安全点后确认取消。
- `batch_size`、批次重试上限和 Task 重启上限属于注入的 `SyncPolicy`。

```python
@dataclass(frozen=True)
class SyncPolicy:
    batch_size: int = 5
    max_task_attempts: int = 3
```

如未来需要历史回填，应新增独立的内部运维命令 `AdminBackfill`，不能给普通 `SyncNow` 增加相互冲突的可选参数。

## 5. 总体关系模型

```text
Task 1 ─── 1 SyncRun
                                      │
                                      ├── N SyncBatch
                                      │      └── N SyncTargetDate
                                      │
                                      ├── N EndpointPublication
                                      │      ├── N RawSourcePage
                                      │      └── N PublicationBarObservation
                                      │                  │
                                      │                  ├── 1 BarRecord
                                      │                  └── 1 BarVersion
                                      │
                                      ├── 0..N InstrumentMasterSnapshot
                                      └── 0..1 DataSnapshot

BarRecord 1 ─── N BarVersion
BarRecord 1 ─── 0..1 CurrentBar

DataSnapshotHead 1 ─── 1 DataSnapshot
BacktestRun      N ─── 1 DataSnapshot
```

## 6. 同步运行模型

### 6.1 SyncRun

`SyncRun` 是业务状态的事实来源，与队列中的 `Task` 一对一。Task 只表达执行状态，不代替 SyncRun、SyncBatch 和 publication 中的业务 checkpoint。

```text
SyncRun
  id UUID PK
  task_id UUID FK UNIQUE NOT NULL
  source varchar NOT NULL
  mode sync_mode NULL
  status sync_run_status NOT NULL
  phase sync_phase NOT NULL
  idempotency_key varchar NULL

  batch_size int NOT NULL
  coverage_before date NULL
  planned_start date NULL
  planned_end date NULL
  plan_fingerprint char(64) NULL

  target_dates int NOT NULL DEFAULT 0
  processed_dates int NOT NULL DEFAULT 0
  total_batches int NOT NULL DEFAULT 0
  completed_batches int NOT NULL DEFAULT 0
  current_batch int NULL

  pages_received bigint NOT NULL DEFAULT 0
  rows_received bigint NOT NULL DEFAULT 0
  rows_new bigint NOT NULL DEFAULT 0
  rows_unchanged bigint NOT NULL DEFAULT 0
  rows_changed bigint NOT NULL DEFAULT 0
  actual_min date NULL
  actual_max date NULL

  cancel_requested_at timestamptz NULL
  error_code varchar NULL
  error_summary text NULL
  created_at timestamptz NOT NULL
  started_at timestamptz NULL
  finished_at timestamptz NULL
```

状态枚举：

```text
QUEUED
RUNNING
CANCELLING
SUCCEEDED
NO_CHANGE
PARTIAL_FAILED
FAILED
CANCELLED
```

阶段枚举：

```text
DISCOVERING_CALENDAR
PLANNING
BARS
MASTER
ACTIVATING_SNAPSHOT
COMPLETE
```

数据库约束：

```sql
CREATE UNIQUE INDEX uq_sync_run_idempotency
ON sync_runs (source, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX uq_sync_run_active_source
ON sync_runs (source)
WHERE status IN ('queued', 'running', 'cancelling');
```

`resumable` 不落库。它由运行状态、错误类别、冻结计划是否完整、Task 的 `attempt_count` 和 publication 状态推导，避免布尔字段漂移。

### 6.2 Task 执行模型

Task Runner 是一个独立常驻进程，只启动一个 Worker。它每次只领取一个 Task，在当前线程同步调用 handler；`workflow.execute()` 返回前不会领取下一个 Task。`httpx.Client` 发出的 J-Quants 请求也是同步阻塞的。对于 `jquants_sync`，Workflow 是 SyncRun 与 Task 业务终态的唯一写入者；通用 Task Runner 不在 handler 返回后另行提交 SUCCEEDED/FAILED。

```text
Task
  id UUID PK
  task_type varchar NOT NULL
  status task_status NOT NULL
  payload jsonb NOT NULL
  progress jsonb NOT NULL
  attempt_count int NOT NULL DEFAULT 0
  error text NULL
  created_at timestamptz NOT NULL
  started_at timestamptz NULL
  finished_at timestamptz NULL
```

状态：

```text
QUEUED → RUNNING → SUCCEEDED
                 ↘ FAILED → QUEUED  (手动恢复)
                 ↘ CANCELLED → QUEUED (手动恢复)
RUNNING → QUEUED                         (Worker 重启恢复)
```

单 Worker 是 deployment invariant，不使用 lease、heartbeat、Activation 或 fencing token。Worker 启动时获取一个 PostgreSQL session-level advisory lock：

```text
lock key = stable_hash("task-runner:singleton")
```

获取失败的第二个 Worker 必须立即退出，不进入轮询。该锁由持有它的长连接维持，连接断开时自动释放。这是防止误启动两个 Worker 的部署保护，不是任务接管协议。

Worker 领取 Task 时将 `attempt_count` 加一并设为 RUNNING。发布事务只需验证 `Task.status = RUNNING`、`SyncRun.status = RUNNING` 且未请求取消。`attempt_count` 用于限制进程崩溃恢复次数和记录 publication 来源，不用于多 Worker fencing。

所有正常成功、业务失败和取消路径必须在同一事务中同时终结 SyncRun 与 Task，并写入两者的 `finished_at`。因此持久化新状态不能出现“终态 SyncRun + RUNNING Task”；启动恢复仍会识别并修复旧 implementation 可能遗留的这种组合。

### 6.3 SyncBatch

```text
SyncBatch
  id UUID PK
  sync_run_id UUID FK NOT NULL
  ordinal int NOT NULL
  status sync_batch_status NOT NULL
  target_start date NOT NULL
  target_end date NOT NULL
  target_dates int NOT NULL
  attempt_count int NOT NULL DEFAULT 0
  published_publication_id UUID FK NULL
  rows_received bigint NOT NULL DEFAULT 0
  rows_new bigint NOT NULL DEFAULT 0
  rows_unchanged bigint NOT NULL DEFAULT 0
  rows_changed bigint NOT NULL DEFAULT 0
  error_code varchar NULL
  error_summary text NULL
  started_at timestamptz NULL
  finished_at timestamptz NULL

Unique(sync_run_id, ordinal)
```

状态：

```text
PENDING → STAGING → PUBLISHED
                  ↘ FAILED
                  ↘ CANCELLED
```

失败恢复时复用同一个 batch，并创建下一 publication attempt。成功后 `published_publication_id` 固定，不能被后续恢复覆盖。

### 6.4 SyncTargetDate

```text
SyncTargetDate
  id UUID PK
  sync_run_id UUID FK NOT NULL
  sync_batch_id UUID FK NOT NULL
  trade_date date NOT NULL
  source_calendar_code varchar NOT NULL
  status sync_target_status NOT NULL

Unique(sync_run_id, trade_date)
```

状态：`PENDING`、`STAGING`、`PUBLISHED`、`FAILED`、`CANCELLED`。

迁移应使用复合外键或数据库触发器保证 `SyncTargetDate.sync_run_id` 与所属 `SyncBatch.sync_run_id` 一致。

## 7. Publication 模型

### 7.1 EndpointPublication

```text
EndpointPublication
  id UUID PK
  sync_run_id UUID FK NOT NULL
  sync_batch_id UUID FK NULL
  created_by_task_id UUID FK NOT NULL
  created_by_task_attempt int NOT NULL
  endpoint varchar NOT NULL
  scope_ordinal int NOT NULL
  attempt int NOT NULL
  status publication_status NOT NULL
  publish_sequence bigint UNIQUE NULL
  api_version varchar NOT NULL
  adapter_version varchar NOT NULL
  schema_fingerprint char(64) NULL
  request_params jsonb NOT NULL
  stats jsonb NOT NULL
  error_code varchar NULL
  error_summary text NULL
  published_at timestamptz NULL
  created_at timestamptz NOT NULL

Unique(sync_run_id, endpoint, scope_ordinal, attempt)
```

约定：

- calendar 和 master 使用 `scope_ordinal = 0`、`sync_batch_id = NULL`；
- daily bars 使用对应 `SyncBatch.ordinal` 和 `sync_batch_id`；
- 每次重试增加 `attempt`，保留失败 generation；
- `publish_sequence` 只在发布事务中从 PostgreSQL sequence 分配；
- STAGING、FAILED 和 CANCELLED publication 的 `publish_sequence` 必须为 NULL；
- 所有正式读取必须同时要求 `status = PUBLISHED`。

calendar 和 master 的 attempt 通过查询相同 `(run, endpoint, scope_ordinal)` 的最大 attempt 加一生成。bars 的 attempt 同时更新 `SyncBatch.attempt_count`。

### 7.2 RawSourcePage

保留现有模型和 90 天保留策略：

```text
RawSourcePage
  publication_id UUID FK
  page_index int
  payload jsonb
  content_hash char(64)
  expires_at timestamptz

Unique(publication_id, page_index)
```

失败 publication 的原始页仍保留用于审计。新的 attempt 使用新的 publication，不与旧页索引冲突。

## 8. 行情版本模型

### 8.1 BarRecord

新增 `BarRecord`，承载稳定业务键：

```text
BarRecord
  id UUID PK
  source varchar NOT NULL
  instrument_id UUID FK NOT NULL
  trade_date date NOT NULL
  session varchar NOT NULL
  created_at timestamptz NOT NULL

Unique(source, instrument_id, trade_date, session)
```

### 8.2 BarVersion

`BarVersion` 不再持有 `publication_id` 或 `is_current`：

```text
BarVersion
  id UUID PK
  bar_record_id UUID FK NOT NULL
  content_hash char(64) NOT NULL
  raw/adjusted OHLCV
  trading_value
  adjustment_factor
  first_seen_at timestamptz NOT NULL
  last_seen_at timestamptz NOT NULL
  quality_status varchar NOT NULL

Unique(bar_record_id, content_hash)
```

相同内容只存储一次。版本从 A 修订为 B 后再恢复为 A 时，复用原 A 版本，不插入重复内容，也不修改旧 publication。行情数值和 `content_hash` 不可变；`last_seen_at` 可以在创建新 observation 时更新，`quality_status` 由数据质量流程管理。

### 8.3 PublicationBarObservation

```text
PublicationBarObservation
  publication_id UUID FK NOT NULL
  bar_record_id UUID FK NOT NULL
  bar_version_id UUID FK NOT NULL
  disposition bar_observation_disposition NOT NULL
  observed_at timestamptz NOT NULL

Primary Key(publication_id, bar_record_id)
Unique(publication_id, bar_version_id)
```

`disposition`：

- `NEW`：发布前没有正式 CurrentBar；
- `CHANGED`：内容与当前正式版本不同且是首次出现；
- `REVERTED`：内容与当前正式版本不同，但复用了历史 BarVersion；
- `UNCHANGED`：内容与当前正式版本相同。

数据库或写入逻辑必须保证 observation 的 `bar_record_id` 与 `bar_version.bar_record_id` 一致。

所有返回行都创建 observation，包括 UNCHANGED。这样 publication 能完整表达“本次来源观察到了什么”，快照也可以按发布序列解析版本。

### 8.4 CurrentBar

```text
CurrentBar
  bar_record_id UUID PK FK
  bar_version_id UUID FK NOT NULL
  publication_id UUID FK NOT NULL
  publish_sequence bigint NOT NULL
  updated_at timestamptz NOT NULL
```

`CurrentBar` 是当前查询的性能投影，不是历史快照的正确性来源。只有 publication 发布事务可以更新它。

批次失败或取消时：

- BarVersion 和 observation 可以保留作为审计事实；
- publication 不获得 publish sequence；
- CurrentBar 不变化；
- 不再需要按 publication 删除 BarVersion。

## 9. 不可变 DataSnapshot

系统规范已经定义 `DataSnapshot`。本设计用发布序列实现轻量不可变快照，避免为每个同步结果复制全部行情行。

### 9.1 DataSnapshot

```text
DataSnapshot
  id UUID PK
  source varchar NOT NULL
  sync_run_id UUID FK UNIQUE NOT NULL
  mode sync_mode NOT NULL
  bar_publish_sequence bigint NOT NULL
  calendar_publication_id UUID FK NOT NULL
  master_snapshot_id UUID FK NOT NULL
  coverage_start date NOT NULL
  coverage_end date NOT NULL
  verified_start date NULL
  verified_end date NULL
  plan_fingerprint char(64) NOT NULL
  created_at timestamptz NOT NULL
```

`bar_publish_sequence` 表示该快照允许读取的最大日线发布序列。`coverage_start/end` 是按该 cutoff 解析出的全部可读 BarRecord 的累计最小/最大交易日，不只是本次运行实际返回的范围。`verified_start/end` 是本次冻结计划中所有目标日期均完成请求和 publication 校验后的首尾日期；空计划时均为 NULL。它只表达“本次成功核验了哪个来源窗口”，不裁剪累计历史。

快照中的某个 `BarRecord` 版本定义为：

```text
在 source 相同、publication.status = PUBLISHED、
publication.publish_sequence <= snapshot.bar_publish_sequence 的 observations 中，
选择该 BarRecord publish_sequence 最大的 BarVersion。
```

读取 module 返回上述全部累计成员；成员交易日必须落在声明的 `coverage_start/end` 内。激活事务从解析后的成员计算 coverage，并验证数据库中不存在范围外成员。旧 observation 可以使新 snapshot 继续读取来源滚动窗口之外的历史，但这些日期不会因此进入本次 `verified` 窗口。

必须提供封装查询或数据库 view，调用方不能自行拼接该规则。建议索引：

```text
EndpointPublication(status, publish_sequence)
PublicationBarObservation(bar_record_id, publication_id)
BarRecord(source, trade_date, instrument_id)
```

### 9.2 DataSnapshotHead

```text
DataSnapshotHead
  source varchar PK
  snapshot_id UUID FK UNIQUE NOT NULL
  updated_at timestamptz NOT NULL
```

非空 bars 运行完成后，在一个短事务中：

1. 按 `SyncRun → Task → DataSnapshotHead` 顺序锁定记录；
2. 验证所有 SyncBatch 均为 PUBLISHED；
3. 验证 master snapshot 已发布；
4. 以本次最后一个 bars publish sequence 为 cutoff，解析累计成员并计算 `coverage_start/end`；
5. 从冻结计划写入 `verified_start/end`；
6. 创建不可变 `DataSnapshot`；
7. 原子更新 `DataSnapshotHead`；
8. 在同一事务中将 SyncRun 标记为 SUCCEEDED 或 NO_CHANGE，并将 Task 标记为 SUCCEEDED；
9. 同时写入 SyncRun 与 Task 的完成时间后提交。

增量空结果使用第 12 节的继承路径，仍在同一个最终事务中创建 snapshot、切换 head 并同时终结 SyncRun 与 Task。失败和取消也必须通过锁定同一对记录的事务同时终结二者。

PARTIAL_FAILED、FAILED 和 CANCELLED 运行不能创建或激活 DataSnapshot。

### 9.3 读取规则

- 当前行情页面可以读取 `CurrentBar`。
- 研究、回测和 replay 必须传入 `DataSnapshot.id`。
- BacktestRun 和 ReplaySession 保存不可变 `data_snapshot_id`。
- 已完成回测永远使用原 snapshot，不随 `DataSnapshotHead` 或 CurrentBar 更新。
- planner 的 `coverage_before` 从 `DataSnapshotHead` 读取，不从部分推进的 CurrentBar 推导。
- snapshot 的 `coverage` 用于描述累计可读成员，`verified` 用于展示本次成功核验的来源窗口；调用方不能把两者混用。

如果某个运行部分发布后被放弃，下一次新运行可能重复拉取那些日期。这是有意选择：完整快照水位优先于减少重复请求；内容哈希和 observation 使重复处理保持幂等。来源可见窗口向前滚动时，已保存的更早行情继续属于后续累积 snapshot，但 `verified` 只记录后续运行实际核验的窗口。

## 10. 规划规则

业务范围计算与批次切分完全解耦：

```python
def choose_target_dates(
    visible_dates: list[date],
    *,
    snapshot_coverage_end: date | None,
    last_full_snapshot_at: datetime | None,
    now: datetime,
) -> SyncPlan: ...


def chunk_target_dates(
    target_dates: list[date],
    *,
    batch_size: int,
) -> list[list[date]]: ...
```

规则：

| 条件 | 模式 | 冻结目标 |
|---|---|---|
| 没有 DataSnapshotHead | INITIAL | 全部账户可见交易日 |
| 最近成功 INITIAL/FULL_RECONCILE DataSnapshot 已满 30 天 | FULL_RECONCILE | 全部账户可见交易日 |
| 其他情况 | INCREMENTAL | `coverage_end - 30自然日` 之后全部可见交易日 |

`last_full_snapshot_at` 只来自成功创建 DataSnapshot 的 INITIAL 或 FULL_RECONCILE 运行。PARTIAL_FAILED 不推进完整核对水位。

计划只计算一次，并以全部有序日期的哈希写入 `plan_fingerprint`。恢复时验证 fingerprint 后读取原 `SyncTargetDate`，绝不重新查询日历或重新规划。

### 10.1 Calendar 发布与计划冻结事务

Calendar 的外部请求和数据库发布分为两个阶段。

外部请求阶段：

1. 创建 STAGING calendar publication。
2. 获取全部分页并保存 RawSourcePage。
3. 校验 schema、日期字段和分页完整性。
4. 从完整响应计算 `visible_dates`、SyncPlan、SyncBatch 和 `plan_fingerprint`。

随后在一个数据库事务中同时完成：

1. 按 `SyncRun → Task → EndpointPublication` 顺序锁定记录。
2. 验证 run 和 Task 均为 RUNNING，且没有取消请求。
3. 将 calendar publication 标记为 PUBLISHED，并分配 `publish_sequence`。
4. 写入全部 SyncBatch 和 SyncTargetDate。
5. 写入 mode、计划边界、目标数、批次数和 `plan_fingerprint`。
6. 将 SyncRun.phase 切换为 BARS。
7. 一次提交。

因此新 implementation 只允许以下稳定 checkpoint：

```text
Calendar 尚未开始
Calendar STAGING，计划不存在
Calendar PUBLISHED，完整计划已冻结
```

“Calendar 已 PUBLISHED 但计划不存在”和“计划只写入一部分”不能由新 implementation 产生。

### 10.2 Calendar 恢复决策

| Calendar publication | 冻结计划 | 恢复动作 |
|---|---|---|
| 不存在 | 不存在 | 创建 attempt=1 并请求 Calendar |
| STAGING | 不存在 | 将旧 attempt 标为 FAILED，创建下一 attempt 并完整重取 |
| FAILED/CANCELLED | 不存在 | 创建下一 attempt 并完整重取 |
| PUBLISHED | 完整存在 | 验证 `plan_fingerprint`，跳过 Calendar，从首个未发布批次继续 |
| PUBLISHED | 不存在 | 仅作为旧数据兼容：从已发布 RawSourcePage 重建并一次性冻结计划，不重新请求 Calendar |
| PUBLISHED | 计划残缺或 fingerprint 不匹配 | 抛出 `SyncInvariantError`，禁止自动继续 |
| 非 PUBLISHED | 已存在冻结计划 | 抛出 `SyncInvariantError`，禁止自动继续 |

STAGING publication 不能原地续写，因为系统无法证明所有分页已经完整接收，也无法安全确定下一 `page_index`。新的 attempt 使用新的 publication，避免混合两次来源响应。

对于旧数据的“PUBLISHED 但无计划”状态，已发布 RawSourcePage 是该运行的日期发现事实。缺页、已过期或解析失败时应报告 `SyncInvariantError`，不能通过重新请求 Calendar 悄悄改变同一个 SyncRun 的含义。

一旦 `plan_fingerprint` 存在，恢复过程绝不重新调用 Calendar。运行期间新增的可见日期由下一次 `sync now` 处理。

停止条件是冻结计划中的所有 SyncBatch 都为 PUBLISHED。空计划采用确定规则，implementation 不得访问空列表首尾：

- INITIAL：没有可导入的 bars，运行以 `EMPTY_INITIAL_BARS` 失败；
- FULL_RECONCILE：完整核对没有目标日期，运行以 `EMPTY_FULL_RECONCILE_PLAN` 失败，不能推进完整核对水位；
- INCREMENTAL：按第 12 节的空增量继承路径创建等价 snapshot，并以 NO_CHANGE 完成。

“计划非空但所有已发布 bars batch 均为零行”使用同一模式判定：INITIAL/FULL_RECONCILE 失败，只有存在当前 DataSnapshotHead 的 INCREMENTAL 可以成功继承。部分日期为空、但本次至少观察到一行时走普通非空路径，空日期仍必须完成请求与 publication 校验后才能进入 `verified` 窗口。

## 11. 单批执行与发布事务

每个 bars batch：

1. 检查取消状态。
2. 创建新的 STAGING publication attempt。
3. 按日期获取所有分页并保存 RawSourcePage。
4. 校验 schema、必填字段、数字格式和返回日期与请求日期的一致性。
5. 创建或复用 BarRecord、BarVersion。
6. 为每个返回行写入 PublicationBarObservation。
7. 目标日期全部完成后执行发布事务。

发布事务必须：

1. 按 `SyncRun → Task → SyncBatch → EndpointPublication` 的固定顺序锁定记录。
2. 验证 `SyncRun.status = RUNNING`、`Task.status = RUNNING`、`cancel_requested_at IS NULL` 且 batch attempt 正确。
3. 为 publication 分配唯一 `publish_sequence`。
4. 根据全部 observation upsert CurrentBar；只有新 `publish_sequence` 大于现有值时才允许更新。
5. publication → PUBLISHED。
6. target dates → PUBLISHED。
7. batch → PUBLISHED，并写入 `published_publication_id`。
8. 从该 publication 的 observation 聚合 batch 计数。
9. 从所有已发布 batch 聚合 run 计数和本次运行的 `actual_min/actual_max`；它们不直接充当累计 snapshot coverage。
10. 一次提交。

计数以 observation 和已发布 batch 为事实来源，不通过不受约束的 `+=` 恢复。事务回滚后所有正式状态保持不变。

## 12. Master 与快照激活

master snapshot 也按 publication attempt 建模，不能继续使用当前 `(source, as_of_date, sync_run_id)` 唯一约束，否则失败 attempt 会阻止同一运行重试：

```text
InstrumentMasterSnapshot
  id UUID PK
  source varchar NOT NULL
  as_of_date date NOT NULL
  sync_run_id UUID FK NOT NULL
  publication_id UUID FK UNIQUE NOT NULL
  created_at timestamptz NOT NULL
```

每个 master publication attempt 可以拥有一个 snapshot；只有 PUBLISHED publication 对应的 snapshot 能被 DataSnapshot 引用。失败 attempt 的 snapshot 和成员按失败 generation 的保留策略处理。

所有 bars batch 发布后先根据本次 observation 总数分支。

非空路径：

1. 从本次已发布 batch 计算实际 `coverage_end`。
2. 以该日期获取 master。
3. 创建新的 master publication attempt。
4. 创建 `InstrumentMasterSnapshot` 及成员。
5. 原子发布 master。
6. 创建并激活 DataSnapshot。

一个非空 SyncRun 最多只成功发布一个 master snapshot。若 bars 完成但 master 失败，运行进入 PARTIAL_FAILED；恢复从 master 阶段继续，不重复 bars。

空结果路径：

1. INITIAL 或 FULL_RECONCILE 空结果按第 10 节失败；不得请求 master、创建 DataSnapshot 或推进 `last_full_snapshot_at`。
2. INCREMENTAL 空结果要求事务内锁定并重新读取当前 DataSnapshotHead；若不存在 head，抛出 `SyncInvariantError`。
3. 不创建本运行的 master publication 或 InstrumentMasterSnapshot；新 DataSnapshot 继承当前 head snapshot 的 `bar_publish_sequence`、`coverage_start/end` 和 `master_snapshot_id`。
4. 新 DataSnapshot 使用本运行已经发布的 calendar publication、mode 和 plan fingerprint；`verified_start/end` 来自非空冻结计划，空计划时为 NULL。
5. 在一个最终事务中创建该等价 DataSnapshot、切换 DataSnapshotHead，并同时将 SyncRun 标记为 NO_CHANGE、Task 标记为 SUCCEEDED。

因此一个成功的非空运行有一个本运行 master snapshot；成功的空增量运行没有本运行 master snapshot，但其 DataSnapshot 显式复用上一完整 snapshot 的 master。`SyncOutcome.snapshot_id` 始终返回本运行新创建的 DataSnapshot id，而不是旧 head id。

## 13. 失败、取消与恢复

### 13.1 状态判定

| 场景 | SyncRun 状态 |
|---|---|
| 尚无 endpoint 发布即失败 | FAILED |
| 已有 calendar 或 bars publication 发布后失败 | PARTIAL_FAILED |
| 完成并激活 snapshot，存在新增或修订 | SUCCEEDED |
| 完成并激活 snapshot，没有新增或修订 | NO_CHANGE |
| 协作式取消完成 | CANCELLED |

错误至少保存 `code`、`retryable`、`phase`、`batch_ordinal` 和脱敏 summary。`retryable` 可以由错误 code 映射，不必作为可漂移字段持久化。

除等待安全点的 CANCELLING 外，SyncRun 进入任何终态时，关联 Task 必须在同一事务进入对应终态：SUCCEEDED/NO_CHANGE 对应 Task.SUCCEEDED，FAILED/PARTIAL_FAILED 对应 Task.FAILED，CANCELLED 对应 Task.CANCELLED。Task 没有 NO_CHANGE 或 PARTIAL_FAILED 状态。

### 13.2 取消

`CANCELLING` 只属于 SyncRun。Task 不增加 CANCELLING 状态：运行中的 Worker 仍需要完成当前同步 HTTP 响应处理和 staging 清理，因此在安全点确认取消前保持 RUNNING。

状态映射：

| 时点 | SyncRun | Task |
|---|---|---|
| 尚未领取 | QUEUED | QUEUED |
| QUEUED 时直接取消后 | CANCELLED | CANCELLED |
| Worker 正在执行 | RUNNING | RUNNING |
| 已请求取消、等待安全点 | CANCELLING | RUNNING |
| Worker 清理并确认后 | CANCELLED | CANCELLED |

取消流程：

1. `request_cancel()` 按 `SyncRun → Task` 顺序锁定记录。
2. 如果 run 尚为 QUEUED，则同时取消 SyncRun 和 Task，Worker 不会执行它。
3. 如果 run 为 RUNNING，则设置 `SyncRun.status = CANCELLING` 和 `cancel_requested_at`；Task 暂时保持 RUNNING。
4. Workflow 在每个外部请求前、日期之间、publication 发布事务前和 master 前检查取消。
5. 到达安全点后，将当前 STAGING publication、未发布 batch 和 target 标为 CANCELLED，不改变 CurrentBar。
6. 最后在同一事务中将 Task 和 SyncRun 标为 CANCELLED，并记录两者完成时间。

取消事务与发布事务必须锁定同一个 SyncRun，并遵循相同的锁顺序。竞态结果由谁先提交决定：

- 发布事务先取得锁并提交：该 batch 保持 PUBLISHED；取消事务随后把 run 设为 CANCELLING，Workflow 在下一安全点停止。
- 取消事务先取得锁并提交：发布事务随后读到 CANCELLING 或 `cancel_requested_at != NULL`，验证失败，当前 generation 必须转为 CANCELLED，不能更新 CurrentBar。

因此 publication 发布条件必须是 `SyncRun.status = RUNNING AND cancel_requested_at IS NULL`，不能只检查“不是终态”。如果 `request_cancel()` 发现运行已经进入 SUCCEEDED、NO_CHANGE、FAILED、PARTIAL_FAILED 或 CANCELLED，则不修改状态并返回 409。

### 13.3 手动恢复

`resume(run_id)`：

1. 取得 source advisory lock 并锁定 SyncRun。
2. 确认运行可恢复，且 Task 当前不是 QUEUED/RUNNING。
3. 按持久化 checkpoint 分支：
   - Calendar 不存在、STAGING、FAILED 或 CANCELLED，且计划不存在：按 10.2 节接受该状态；重新领取后创建下一 Calendar attempt，或在从未开始时创建 attempt=1；
   - Calendar 已 PUBLISHED、计划不存在：只允许按 10.2 节的旧数据兼容路径从 RawSourcePage 重建完整计划；
   - Calendar 已 PUBLISHED、计划完整：校验 `plan_fingerprint`；
   - 计划残缺、fingerprint 不匹配，或非 PUBLISHED Calendar 已存在计划：抛出 `SyncInvariantError`。
4. 仅当完整计划存在时，将 FAILED/CANCELLED batch 及其未发布 target 恢复为 PENDING；PUBLISHED batch 和 target 不变。
5. 清除 Task 的本次终态时间和错误，将同一 Task 重新置为 QUEUED；下次领取时增加 `attempt_count`。
6. SyncRun 回到 QUEUED，清除 `cancel_requested_at`、`finished_at` 和本次终态错误，保留已存在的计划、进度和已发布事实。

因此 QUEUED 时直接取消、Calendar 请求期间取消以及计划冻结后的取消都可以恢复，但恢复动作由 checkpoint 决定，而不是无条件要求 fingerprint 已存在。

### 13.4 Worker 崩溃恢复

Worker 启动时：

1. 先取得 Task Runner 单例 advisory lock；取得失败则立即退出，不作恢复。
2. 查找遗留的 RUNNING Task。由于单例锁已证明不存在另一个活 Worker，这些 Task 都是前一进程留下的 orphan。
3. 在一个事务中按 `SyncRun → Task → EndpointPublication` 锁定记录。
4. 若 SyncRun 已是终态，这是旧 implementation 遗留的不一致：不得重新排队；将 Task 修复为该运行对应的 SUCCEEDED、FAILED 或 CANCELLED，并补齐完成时间。
5. 若 SyncRun 仍是 RUNNING/CANCELLING，将遗留 STAGING publication 标为 FAILED，但 observation 和 raw pages 按保留策略留存；CANCELLING 按 13.2 节直接完成协作式取消。
6. 对仍为 RUNNING 的运行，如果计划尚未冻结，按 10.2 节验证 Calendar checkpoint；计划已冻结时验证 fingerprint，不再请求 Calendar。
7. 若 `attempt_count < max_task_attempts` 且 SyncRun 可恢复，在该事务最后将原 Task 和 SyncRun 重新置为 QUEUED；超过上限则在同一事务标记 Task 为 FAILED，SyncRun 为 FAILED/PARTIAL_FAILED。
8. 提交恢复事务后才进入轮询；Workflow 随后从第一个非 PUBLISHED batch、master 或 snapshot 激活阶段继续。

不使用超时猜测 Worker 是否崩溃，也不在运行中启动第二 Worker 接管。必须先由 Docker/systemd/Kubernetes 确认旧进程结束并重启唯一 Worker，再由启动恢复执行上述步骤。发布事务本身的原子性可区分“已提交”和“未提交”，重启后以持久化状态为准。

## 14. 单 Worker 约束与并发写入

两类锁不能混淆：

1. Task Runner 启动时持有 session-level `task-runner:singleton` 锁，保证整个部署只有一个 Worker 执行 Task。
2. `start()`、`resume()` 和启动恢复使用以下 transaction-level source 锁，保护来自多个 HTTP 请求的并发写入：

```text
lock key = stable_hash("market-data-sync:" + source)
```

锁内再次检查：

- 是否已有活动 SyncRun；
- 关联 Task 是否已为 QUEUED/RUNNING；
- idempotency key 是否已经存在；
- Task `attempt_count` 是否已超过恢复上限。

部分唯一索引是最终数据库保护，advisory lock 用于提供可预测的返回语义。违反唯一约束时重新读取并返回已有运行，不能创建第二个活动运行。

即使只有一个 Worker，HTTP 取消请求与 Worker publication 事务仍可并发。二者必须按相同顺序锁定 SyncRun，使第 13.2 节的竞态结果可确定。

## 15. HTTP 与 Worker

### 15.1 HTTP

普通请求不再暴露日期范围或 batch size：

```http
POST /data-sync/jquants
Idempotency-Key: optional-value
Content-Type: application/json

{}
```

保留或增加：

```text
POST /data-sync/jquants
GET  /data-sync/runs/{run_id}
POST /data-sync/runs/{run_id}/cancel
POST /data-sync/runs/{run_id}/resume
```

有活动运行时返回该运行。`resume` 在运行不可恢复或 Task 已为 QUEUED/RUNNING 时返回 409。

### 15.2 Worker

生产与本地 Docker Compose 都使用独立进程启动：

```text
uv run python -m app.worker.main
```

部署必须配置为单副本并在进程退出后重启。数据库单例锁是误配置保护；它不把多副本变成受支持的运行方式。Worker 启动顺序固定为：注册 handler → 取得单例锁 → 恢复 orphan Task → 进入串行轮询。

Task payload：

```json
{
  "sync_run_id": "..."
}
```

Task Runner 的运行形式是：

```python
while True:
    task = claim_oldest_queued_task()
    if task is None:
        time.sleep(poll_interval)
        continue
    handler(task.payload)  # 同步阻塞到整个 SyncRun 结束
```

`jquants_sync` handler 只解析 `sync_run_id` 并调用 `workflow.execute(run_id)`。Workflow 内部按 ordinal 连续执行全部未发布 SyncBatch；Worker 不知道日期计划、批次、publication 或 snapshot 规则。

HTTP 路由使用同步 `def` 与 Worker 使用同步 `httpx.Client` 不改变两个进程的分工：HTTP 请求只在数据库创建 Task 并立即返回；实际 J-Quants 请求在独立 Worker 中同步、串行执行。

## 16. 进度模型

`inspect()` 返回的正式进度只来自已发布 batch：

```json
{
  "id": "0ea5...",
  "status": "running",
  "phase": "bars",
  "mode": "initial",
  "task_attempt": 2,
  "target_dates": 487,
  "processed_dates": 135,
  "total_batches": 98,
  "completed_batches": 27,
  "current_batch": 28,
  "current_date": "2025-01-16",
  "coverage_before": null,
  "coverage_current": "2025-01-15",
  "rows_new": 120000,
  "rows_changed": 12,
  "rows_unchanged": 4500,
  "snapshot_id": null,
  "resumable": true,
  "error": null
}
```

当前 STAGING publication 的临时行数可以独立显示，但不能计入 `processed_dates` 或正式汇总。

## 17. 数据迁移

建议采用分阶段迁移，避免同时改写所有读写路径。

### 阶段 1：增加新表和兼容字段

- 新增 SyncBatch、BarRecord、PublicationBarObservation、CurrentBar、DataSnapshot、DataSnapshotHead。
- DataSnapshot 同时保存累计 `coverage_start/end` 与本次 `verified_start/end`；允许空增量复用上一 snapshot 的 master snapshot 和 bar cutoff。
- 给 Task 增加 `attempt_count`，给 EndpointPublication 增加 batch、task attempt、scope、attempt 和 publish_sequence。
- 将 InstrumentMasterSnapshot 的唯一性迁移为 `Unique(publication_id)`，允许同一运行存在多个 master attempt。
- 给 SyncRun 增加 phase、计划、批次、取消和结构化错误字段。
- 暂时保留 `SyncRun.task_id`、`BarVersion.publication_id` 和 `is_current`。

### 阶段 2：回填

1. 为现有 Task 回填 `attempt_count`：已经开始过的 Task 为 1，其余为 0。
2. 按现有 BarVersion 业务键创建 BarRecord。
3. 将 BarVersion 关联到 BarRecord。
4. 根据旧 `publication_id` 创建 PublicationBarObservation。
5. 根据 `is_current=True` 创建 CurrentBar。
6. 按 `published_at, id` 的稳定顺序为历史 PUBLISHED publication 分配 publish_sequence。
7. 为最新成功运行创建初始 DataSnapshot 和 DataSnapshotHead；历史运行是否全部回填 snapshot 可后续决定。

### 阶段 3：切换读写

- 新同步只写 observation，并通过 publish 事务更新 CurrentBar。
- 当前行情改读 CurrentBar。
- 新回测必须绑定 DataSnapshot。
- Workflow 使用 Task 重试计数和 batch checkpoint。
- J-Quants Workflow 在同一事务终结 SyncRun 与 Task；Task Runner 不在 handler 返回后重复写入业务终态。

### 阶段 4：删除旧耦合

- 删除 `BarVersion.publication_id`。
- 删除 `BarVersion.is_current`。
- 保留 `SyncRun.task_id` 一对一关系作为 Task Runner 查询与恢复入口。
- 删除旧 `(sync_run_id, endpoint)` 唯一约束。
- 增加所有新 NOT NULL、FK、CHECK 和部分唯一索引。

## 18. 验收测试

### 18.1 计划与连续批次

1. 12 个日期、内部 batch size=5 时，单个 SyncRun 顺序发布 3 个 bars generation。
2. INITIAL 处理全部可见日期。
3. FULL_RECONCILE 不受 batch size 影响。
4. 30 日重叠窗口全部遍历，不重复卡在最早 N 日。
5. PARTIAL_FAILED 不推进 `last_full_snapshot_at`。
6. Calendar 发布和完整计划冻结在同一事务提交，不存在半计划状态。
7. Calendar STAGING 时崩溃，恢复会失败旧 attempt 并完整重取。
8. Calendar PUBLISHED 且计划已冻结时崩溃，恢复不会再次请求 Calendar。
9. 兼容旧的 Calendar PUBLISHED 但无计划状态时，从 RawSourcePage 重建计划。
10. Calendar 与计划状态矛盾或 fingerprint 不匹配时抛出 `SyncInvariantError`。
11. 空 INITIAL 和空 FULL_RECONCILE 明确失败，且不创建 snapshot 或推进完整核对水位。
12. 空 INCREMENTAL 继承当前 head 的 bar cutoff、累计 coverage 和 master，创建新的 NO_CHANGE snapshot。
13. 计划非空但所有 bars publication 都为零行时，使用与空计划相同的模式判定。

### 18.2 版本与 publication

1. 失败 attempt 重试相同内容时不修改旧 publication。
2. 内容 A → B → A 时复用原 A BarVersion，并创建新的 observation。
3. UNCHANGED 行也进入 observation，但 CurrentBar 指针不变。
4. FAILED/CANCELLED publication 永远没有 publish_sequence，且不改变 CurrentBar。

### 18.3 快照

1. 第二批失败时，第一批 CurrentBar 可见，但不创建新 DataSnapshot。
2. 完整运行成功后一次性创建 snapshot 并切换 head。
3. snapshot 查询只能看到 `bar_publish_sequence` 以内的 observations。
4. 已创建回测继续读取原 snapshot，不受后续修订影响。
5. 来源窗口向前滚动后，新 snapshot 的累计 coverage 仍包含已保存旧行情，而 `verified` 只包含本次冻结计划窗口。
6. snapshot 声明的 coverage 等于按 cutoff 解析出的成员最小/最大交易日，不存在范围外成员。
7. 空增量创建新的 snapshot id，但其 bar cutoff、累计 coverage 和 master 与旧 head 相同。

### 18.4 单 Worker、重启与并发请求

1. 第二个 Worker 无法取得单例 advisory lock，必须在轮询 Task 前退出。
2. Worker 崩溃并由进程管理器重启后，原 RUNNING Task 重新排队，并从第一个未发布 batch 恢复。
3. 并发两次 `resume()` 只会将同一 Task 重新排队一次。
4. 并发两次 `start()` 最多创建一个活动 SyncRun。
5. 同 idempotency key 返回同一个运行。
6. 快照激活事务提交后 SyncRun 与 Task 同时进入终态，不存在成功 Run 对应 RUNNING Task 的提交状态。
7. 模拟旧数据中的“终态 SyncRun + RUNNING Task”，Worker 启动时只修复 Task 终态，不重新排队运行。
8. Workflow 最终事务前崩溃时，两者保持 RUNNING 并按 checkpoint 恢复；事务提交后崩溃时，两者均保持终态。

### 18.5 取消与 master

1. 第二批取消后第一批保持 PUBLISHED，当前批次不改变 CurrentBar。
2. bars 完成但 master 失败时，恢复只重试 master。
3. 计划冻结后的 CANCELLED 运行恢复时复用冻结计划。
4. QUEUED 时取消以及 Calendar STAGING 时取消的运行，可以在没有 fingerprint 时按 Calendar checkpoint 恢复。
5. QUEUED 时取消会原子取消 SyncRun 和 Task。
6. RUNNING 时请求取消后，Run 为 CANCELLING，Task 在清理完成前保持 RUNNING。
7. 取消事务先提交时，迟到的 publication 不能发布或更新 CurrentBar。
8. 发布事务先提交时，该批保持 PUBLISHED，并在下一安全点取消后续工作。

## 19. 实施顺序

1. 实现 BarRecord、BarVersion 新关系、Observation 和 CurrentBar。
2. 实现 publication attempt 与原子发布事务。
3. 实现累计 DataSnapshot 查询、verified window、空增量继承和 DataSnapshotHead 激活。
4. 拆分完整目标规划与内部批次切分。
5. 实现 SyncBatch 循环和汇总进度。
6. 为 Task Runner 增加单例启动锁、`attempt_count`、Run/Task 原子终结和 Worker 重启恢复。
7. 将当前“遗留 RUNNING 直接失败”改为“根据 checkpoint 重新排队”。
8. 收敛 HTTP `sync now` interface，移除公开日期范围与 batch 参数。
9. 切换回测和 replay 到不可变 DataSnapshot。
10. 完成旧字段清理迁移。

## 20. 关键权衡

### 逐批原子，而非全运行原子

早期成功批次可以推进 CurrentBar，降低多小时任务失败后的重做成本；代价是 PARTIAL_FAILED 时当前市场事实可能部分更新。DataSnapshot 负责阻止部分结果进入回测。

### 发布序列快照，而非物化全部成员

`bar_publish_sequence` 避免每次同步复制全部行情版本，存储成本低；代价是 snapshot 查询需要按 publication sequence 解析最新 observation。snapshot 采用累积语义：`coverage` 来自解析后的全部成员，`verified` 单独说明本次成功核验窗口。该复杂性必须封装在查询 module 或数据库 view 后面。

### 重新排队同一 Task，而非引入 Activation

在单 Worker、同步串行的 deployment invariant 下，Activation、lease、heartbeat 和 fencing 不产生当前价值。恢复时重新排队同一 Task，由 `attempt_count` 控制上限，由 SyncBatch/publication checkpoint 保存业务进度。代价是不保留每次进程执行的完整独立历史；若未来支持多 Worker 或需要该审计粒度，再引入 TaskAttempt/Activation 模型。

### 普通 SyncNow 不暴露执行参数

批次大小和重试策略隐藏在 `SyncPolicy` 中，使 interface 更深并符合产品规范；代价是运维调参需要配置或专用内部命令，而不能由普通用户请求临时指定。
