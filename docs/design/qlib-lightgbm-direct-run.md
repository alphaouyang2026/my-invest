# DataSnapshot + Qlib Day Provider + LightGBM 直接实验设计

- 状态：Implemented
- 日期：2026-09-03
- 目的：保留 07 的产品化实现，追加一条不使用 bundle 数据库状态、job 和结果 publish，并支持多日标签与滚动重训的同步实验路径。

## 1. 决策摘要

保留 Qlib bundle 的**文件形式**，取消它在新功能中的**管理对象形式**，并拆成三个互不隐式调用的功能：

```text
1. build-provider(snapshot_id)   PostgreSQL/DataSnapshot -> Qlib day provider
2. delete-provider(snapshot_id)  删除该 snapshot 的 day provider
3. predict(snapshot_id, ...)     已有 day provider -> 动态股票池 -> Alpha -> 多日标签 -> Rolling LightGBM
```

`predict` 不自动构建 provider；目录不存在或不可读时直接失败。`build-provider` 只生成目录，不启动预测。`delete-provider` 只删除明确指定 snapshot 对应的目录，不触发重建。

三个功能都不创建或更新 `QlibDataBundle`、`DataBundleBuildAttempt`、`Task`、`ResearchRun` 或 artifact 数据，也不使用 bundle build job、worker、结果 publish、API 或页面。

## 2. 三个独立 interface

新功能没有 `queued/building/validating/publishing/ready/failed` 状态，也不记录 build attempt：

```python
@dataclass(frozen=True)
class DayProviderRef:
    path: Path
    manifest: DayProviderManifest


@dataclass(frozen=True)
class BuildDayProviderResult:
    provider: DayProviderRef
    created: bool


def build_day_provider(
    session: Session,
    snapshot_id: UUID,
    provider_root: Path,
) -> BuildDayProviderResult:
    """Synchronously create or return this snapshot's day provider."""


def delete_day_provider(
    snapshot_id: UUID,
    provider_root: Path,
) -> bool:
    """Delete exactly this snapshot's provider; False when it does not exist."""


def run_direct_prediction(
    session: Session,
    config: DirectPredictionConfig,
) -> DirectExperimentResult:
    """Run against an existing provider; never build or delete one."""
```

build 和 predict 在 implementation 内共同使用一个内部 seam：

```python
def open_day_provider(
    snapshot_id: UUID,
    provider_root: Path,
) -> DayProviderRef:
    """Validate identity, required structure and Qlib smoke readability."""
```

已有目录只有通过 `open_day_provider` 才算 build 成功；新目录原子 rename 后也必须通过它。predict 只调用这个读取 seam，不调用 build。

构建仍使用临时目录和原子 rename，避免失败进程留下可见的半成品；失败时只清理本次创建的临时目录。这是单次文件写入的完整性，不是生命周期状态管理。

删除前必须把目标解析为绝对路径，并验证它严格位于 `provider_root/<snapshot-id>/` 内。目标存在时，枚举其下全部版本子目录；每个版本都必须包含可读 manifest，且所有 `snapshot_id` 都必须匹配。任一 manifest 缺失、损坏或身份不符时拒绝删除。该检查只防止删除错误路径，不做磁盘容量、剩余空间或保留策略管理。

## 3. Day provider 身份与目录

目录由以下身份决定：

```text
DataSnapshot.id
+ exporter schema version
+ pyqlib version
```

建议路径：

```text
var/qlib-direct-providers/
└── <snapshot-id>/
    └── schema-2_pyqlib-0.9.7/
        ├── calendars/day.txt
        ├── instruments/all.txt
        ├── features/<instrument-id>/*.day.bin
        └── manifest.json
```

manifest 至少包含：

```json
{
  "snapshot_id": "...",
  "snapshot_bar_publish_sequence": 0,
  "calendar_publication_id": "...",
  "exporter_schema_version": "2",
  "pyqlib_version": "0.9.7",
  "coverage_start": "...",
  "coverage_end": "...",
  "instrument_count": 0,
  "fields": [],
  "logical_checksum": "..."
}
```

`build-provider` 遇到已有目录时必须通过 `open_day_provider` 才返回；失败时不覆盖。`predict` 使用同一读取 seam，不负责修复。`delete-provider` 在全部版本 manifest 通过校验后删除整个 `<snapshot-id>` 子目录，使该 snapshot 的所有 exporter/pyqlib 版本一起移除。

## 4. 用户入口

在现有 Docker 环境中，只需启动 `backend` service 作为脚本执行容器；不需要启动 worker 或 frontend，也不通过 backend API 发起任务：

```powershell
# 启动并确认脚本入口
docker compose up -d backend
docker compose exec backend uv run python scripts/qlib_lightgbm_direct.py --help

# 1. 生成 provider
docker compose exec backend uv run python scripts/qlib_lightgbm_direct.py build-provider `
  --snapshot-id 00000000-0000-0000-0000-000000000000

# 2. 删除 provider
docker compose exec backend uv run python scripts/qlib_lightgbm_direct.py delete-provider `
  --snapshot-id 00000000-0000-0000-0000-000000000000

# 3. 运行预测
docker compose exec backend uv run python scripts/qlib_lightgbm_direct.py predict `
  --snapshot-id 00000000-0000-0000-0000-000000000000 `
  --feature-set alpha158 `
  --train 2022-01-04:2024-12-30 `
  --valid 2025-01-06:2025-12-30 `
  --test 2026-01-05:2026-08-31 `
  --label-horizon 5 `
  --rolling-step 20

# 4. 运行搜索导出的候选：整份配置来自文件，单个字段可用显式 flag 覆盖
docker compose exec backend uv run python scripts/qlib_lightgbm_direct.py predict `
  --config configs/experiments/selected_parameters-direct-run-<trial-id>.json `
  --seed 20260830
```

Compose 中 backend 的工作目录是 `/app`，`./backend` 挂载到 `/app`。数据库连接复用现有 `POSTGRES_*` 配置。容器内 provider root 默认 `/app/var/qlib-direct-providers`，由 `research_assets` named volume 持久化；三个子命令都可用 `--provider-root` 修改。`--feature-set` 只允许 `alpha158` 和 `alpha360`，默认 `alpha158`。`--label-horizon` 默认 `5`，`--rolling-step` 默认 `20`，两者单位都是 snapshot 日历中的交易日。

`--model-params`、`--stop-metric` 与 `--train-window` 是搜索所优化的三个维度，因此必须能在这里执行；没有它们，搜索选出的候选只是一段无法运行的描述。`--model-params` 接受内联 JSON 或 `@path`，仍然只过 `OVERRIDABLE_PARAMS` 白名单，越界或写入 seed 一律拒绝而不夹取。`--stop-metric` 为 `l2`（默认）或 `rank_ic`。`--train-window` 为 `expanding`（默认）或一个固定交易日数，后者随 fold 滑动。

`--purge-horizon` 默认等于 `--label-horizon`。只有在需要让多个 label horizon 落在同一批评价日上时才显式给出——实验搜索比较 5/10/20 日标签时按最长的统一 purge，否则三者的成绩不可比。它只能大于等于 `label_horizon`。

`--config` 读取 `evaluate_qlib_candidates.py` 导出的 JSON，字段名与上述 flag 一致；显式 flag 优先于文件，未出现在两者中的字段才取默认值。文件出现未知字段时拒绝执行，而不是静默忽略。

命令的 stdout 只承载一份 JSON 文档，`predict > result.json` 必须可解析。Qlib 在 import 时把缺失的可选后端打到 stdout，structlog 的默认 console renderer 也把每条日志打到 stdout，因此整个命令体在 stdout 指向 stderr 的状态下执行，只有最终结果写回真正的 stdout。诊断照常可见，但不进结果文件。

三个命令都在前台同步执行。成功退出码为 `0`；参数、snapshot、provider 或数据错误为 `2`；预测训练错误为 `1`。

## 5. Module 与 interface

```python
@dataclass(frozen=True)
class DirectPredictionConfig:
    snapshot_id: UUID
    feature_set: Literal["alpha158", "alpha360"]
    train: DateRange
    valid: DateRange
    test: DateRange
    provider_root: Path
    seed: int = 20260829
    num_threads: int = 2
    label_horizon: int = 5
    rolling_step: int = 20


@dataclass(frozen=True)
class DirectExperimentResult:
    summary: dict[str, object]
    daily_ic: pd.DataFrame
    predictions: pd.DataFrame
    feature_importance: pd.DataFrame


def run_direct_prediction(
    session: Session,
    config: DirectPredictionConfig,
) -> DirectExperimentResult:
    ...
```

三个 public function 构成外部 seam。provider 文件转换只存在于 build implementation；动态股票池、Qlib 特征、dataset、训练和评价只存在于 prediction implementation；delete implementation 只负责精确路径删除。

## 6. 建议目录与现有 07 的关系

```text
backend/app/research/
  day_provider_export.py       # 无状态 exporter 深 module
  bundle_builder.py            # 07：数据库状态 adapter，继续调用 exporter
backend/app/experiments/
  __init__.py
  qlib_lightgbm_direct.py      # 新：filesystem-only application service + 实验
backend/scripts/
  qlib_lightgbm_direct.py      # 薄 CLI：解析参数、调用 application service、输出结果
backend/tests/
  test_day_provider_export.py
  test_qlib_lightgbm_direct.py
```

`backend/scripts/qlib_lightgbm_direct.py` 不包含 provider、股票池、训练或评价逻辑，避免 Docker 入口和可测试的 application service 形成两套实现。

当前 `QlibDataBundleBuilder` 把两种职责放在一起：

- PostgreSQL/DataSnapshot → day provider 文件的转换；
- bundle ORM 状态、attempt、磁盘预算和生命周期。

实施时把第一部分提取为：

```python
def export_snapshot_day_provider(
    session: Session,
    snapshot: DataSnapshot,
    destination: Path,
) -> DayProviderManifest:
    ...
```

07 的 `QlibDataBundleBuilder` 继续负责原有状态和 job，只把文件生成委托给这个深 module；现有 07 interface、表结构和行为不变。新 direct experiment 直接调用无状态 exporter，不 import `QlibDataBundleBuilder`。

这样转换规则只有一份，不会出现“07 的 `$vwap` 正确、direct 版本计算不同”的双实现。

## 7. Provider 生成内容

exporter 继续使用 `snapshot_member_query(snapshot.source, snapshot.bar_publish_sequence)`，禁止读取 `CurrentBar`。按 instrument 流式读取 `BarVersion`，输出 schema v2 字段：

- 调整后的 `$open/$high/$low/$close/$volume`；
- `$factor`；
- `$vwap = trading_value / raw_volume * factor`；
- raw OHLCV、`trading_value`、调整事件；
- quality status 和 quality rule 字段。

`quality_status == untradable` 的研究价格和成交量写 NaN，不前向填充。日历来自 snapshot 固定的 `calendar_publication_id`。

exporter 只生成市场数据，不生成股票池、标签、特征矩阵、模型或结果。

## 8. 动态股票池

train、valid、test 中的每个预测日都调用现有 `build_stock_pool`：

```python
calendar_port = SessionCalendarPort(session, snapshot.calendar_publication_id)
direct_policy = replace(
    DEFAULT_POLICY,
    required_history_days=max(feature_set.max_window, 20),
    required_bar_offsets=tuple(
        sorted({feature_set.max_window, 20, 5, 1}, reverse=True)
    ),
)

pool = build_stock_pool(
    session,
    snapshot,
    as_of=prediction_date,
    calendar=calendar_port,
    policy=direct_policy,
)
```

`max_window` 来自现有可信 FeatureSet 注册表；Alpha360 的 60 根 bar 对应最大 lag 59。检查点至少包含最大 lag、20、5、1，而不是继续使用动量策略的 147/21 默认值。它们只验证关键端点，不承诺滚动窗口中每一天都有行情；中间缺失继续表现为 NaN。

由此形成 `universe_by_date`。Qlib 计算出的特征必须在写入分片缓存前按 `(date, instrument)` 过滤；不允许全市场训练后只在最终排名阶段过滤。标签是例外，见 9.2 第 5 条。

继续沿用现有股票池的点时 roster、Prime 国内普通股、20 日平均成交额、历史价格和预测日可交易性规则。`PoolWarning` 进入结果 summary，不创建数据库记录。

## 9. Qlib 特征与 Dataset

### 9.1 Snapshot 与日期契约

在读取 Qlib 特征前统一完成以下校验：

- `DataSnapshot` 必须存在，且 `source == "jquants"`、`is_backtest_eligible == true`；
- train、valid、test 的起止日期必须全部位于 snapshot 的 coverage 内；
- 三个 segment 的起止日期都必须是该 snapshot 固定日历中的开市日；
- 每个 segment 满足 `start <= end`，三者按 train → valid → test 严格递增且互不重叠；
- `train`、`valid` 是首个 rolling fold 的请求窗口，`test` 是所有 rolling fold 要覆盖的总体预测范围；
- 程序按下述 label purge 自动缩短每个 fold 的有效 train/valid 末端，并在 summary 返回真实区间；
- test 尾部若没有完整的多日标签，仍作为 prediction-only 日期输出排名，但不进入 IC/Rank IC 评价；成熟边界以 `DataSnapshot.coverage_end` 的实际行情覆盖为准，不能使用可能延伸到未来的 calendar publication 末日；
- 两个接缝的 purge 长度取 `purge_horizon`，默认等于 `label_horizon`；调用方显式给出更长值时按更长的 purge，这是多个 horizon 共享同一批评价日的唯一方式。

以上任一条件不满足都返回 `DirectExperimentConfigError`，不进入 provider 特征读取、股票池构建或训练。

### 9.2 特征、标签与切分

1. 定位已有 day provider 并调用 `qlib.init(provider_uri=...)`；目录不存在立即失败。
2. 将 CLI preset 映射到现有 `alpha158_jp_v1` 或 `alpha360_jp_v1` FeatureSet 注册项，使用其中展开的表达式、列名、最大窗口和 definition checksum。
3. 按证券分批向 Qlib 读取特征以及生成标签所需的 `$close`，每批 `feature_batch_size` 只证券（默认 64），写成校验封存的分片缓存（见 9.4）。
4. 标签为从下一开市日开始持有 `H` 个交易日的收盘收益，默认 `H=5`：`close(t+H+1) / close(t+1) - 1`，等价于 Qlib 表达式 `Ref($close, -(H+1)) / Ref($close, -1) - 1`。
5. 按 `universe_by_date` 过滤特征。**标签不按股票池过滤**：前瞻收益必须能取到证券离开股票池之后的收盘价，否则每个边界附近的标签都被悄悄截断。
6. 每个 fold 从缓存中流式装配自己的 train / valid / test 矩阵。

processors 固定为，且不再经由 Qlib 的 `DataHandlerLP` 施加，而是在缓存的三个明确位置各自实现：

```text
写分片时     ：特征 ±inf -> NaN                      （InfToNaN）
合并标签时   ：(rank(pct) - 0.5) * 3.46 逐截面归一化 （CSRankNorm(label)）
装载学习段时 ：丢弃标签为 NaN 的行                    （DropnaLabel）
```

三者都是无状态的（不含任何 fit window），因此预先计算不引入泄漏。这份等价关系不是断言而是验收项：测试同时构造 `DataHandlerLP` 参照管线，要求两条路径产出的学习矩阵逐值相同。

特征在缓存中以 `float32` 存储。这是为了让整段面板能一次性预分配，也使本入口与实验搜索在数值上完全一致。

LightGBM 训练目标使用逐日截面 rank-normalized label，测试评价使用未归一化的原始多日收益。预测和标签始终按 `(datetime, instrument)` index 对齐。`label_not_mature` 的判断使用 snapshot 实际行情覆盖截止点：只有标签退出日不晚于 `DataSnapshot.coverage_end` 才算已经成熟；日历中存在更晚的开市日并不代表 snapshot 已经含有这些行情。

标签的信息退出日为 `t+P+1`，其中 `P` 为 `purge_horizon`（默认等于 `H`）。每个 fold 自动执行严格 purge，使下一 segment 的首个特征日不出现在上一 segment 的标签中：

```text
label_exit(effective_train.end) < valid.start
label_exit(effective_valid.end) < test.start
```

实现依据 snapshot 交易日历将有效末端裁剪为满足约束的最后一个开市日；裁剪后 train 或 valid 为空时，在读取 Qlib 特征前拒绝执行，不允许按自然日近似。

### 9.3 Rolling fold

`rolling_step=S` 将总体 test 按连续 `S` 个交易日切块，最后一块允许不足 `S` 日。第 `k` 个 fold：

- train 候选终点相对首个 fold 前移 `k*S` 个交易日；`train_window=expanding` 时起点固定，形成 expanding train，固定交易日数时起点随之前移，每个 fold 训练长度相同；
- valid 起止日期整体前移 `k*S` 个交易日，保持验证窗口长度；
- test 为总体 test 中第 `k` 个连续块；
- train/valid 候选末端再按 9.2 自动 purge；
- 每个 fold 从头调用一次 `lightgbm.train`，不复用上一 fold 的 Booster。

`rolling_step` 控制重训频率和单个模型负责的预测长度，不是 train_set 或 valid_set 的长度。

固定 `train_window` 只允许滑动，不允许缩短：首个 fold 从 `train.start` 到其 purge 后的 train 末端不足 `train_window` 个交易日时返回 `DirectExperimentConfigError`，而不是让首个 fold 用比其余 fold 更短的历史训练。

### 9.4 特征分片缓存

整块面板从不在内存中出现。缓存目录由**数据身份**唯一决定：

```text
digest(cache schema 版本, DataSnapshot 身份, provider logical_checksum,
       FeatureSet definition_checksum, label_horizon, 股票池 policy fingerprint,
       日期跨度)
```

刻意**不含代码摘要**：否则任何一次提交都会作废一份内容并未改变的缓存。也不含 `feature_batch_size` / `qlib_kernels`：它们决定活怎么分，不决定缓存里存什么，半成品缓存由 `shard-plan.json` 一致性校验兜底。任一输入变化即换目录重建，不存在原地覆盖。

**跨度足够，是因为股票池按整个跨度构建，而不是按 fold 恰好触及的日期。** 缓存内容按股票池过滤，而 `train_window` / `purge_horizon` / `rolling_step` 与 valid 窗口都只在跨度内部移动 fold。若改按 fold 派生的股票池过滤，这些参数就会进入缓存内容而不进入身份，后跑的运行将静默读到不完整的训练集——这正是本设计要排除的失效模式。封存前逐日核对**行数**：股票池说某日有 N 个成员，缓存该日就必须恰好有 N 行。只核对「日期是否出现」不够——特征与标签派生自同一份过滤结果，provider 只答了一批证券中的一部分时两者一起变短，逐行校验看不出异常，而 train / valid 段没有任何覆盖率门槛可以事后兜底。不一致即拒绝封存：写出一份已经短了的缓存，只会把失败推给下一个读它的人。

运行所需的交易日集合是 `assemble_sharded_dataset` 的必填参数，缺任意一天即拒绝返回 reader——校验是拿到 reader 的前提，而不是拿到之后的一句约定，否则新增调用点漏调即可绕过。就当前实现而言这项检查恒为真（股票池按整跨度构建，fold 日期必为其子集），它的作用是把这条不变式写成可执行的断言：一旦有人把过滤改回 fold 派生，它会立刻失败。

按**证券**而非按日期分片，因为前瞻标签沿每只证券自己的序列 `shift` 计算，按日期切会在每个边界处截断标签。

目录结构与写入顺序：

```text
<cache_root>/<identity前20位>/
    shard-plan.json          证券清单与分批，身份不符时拒绝复用
    batch-0001/
        features.parquet     该批证券、全时间跨度、已过滤股票池
        raw-labels.parquet   该批证券的原始前瞻收益
        complete.json        逐文件 checksum，封存后才算存在
    ...
    labels.parquet           合并后的窄标签表，含 raw_hH 与 learn_hH
    complete.json
```

只有窄标签表被合并成单文件；特征始终散在分片中。装载一个 segment 时：按日期谓词下推读取窄标签、（学习段）丢弃 NaN 标签、稳定排序确定最终行序、一次性预分配 `float32` 矩阵，再用 pyarrow scanner 以 `scan_batch_rows` 行为单位流式扫过全部分片，按索引散射写入。每行必须恰好被写一次，否则报缺行或重复行。

峰值内存因此是「该 segment 的最终矩阵 + 一个扫描批」，而不是整个实验的面板。

`complete.json` 的语义是：目录要么完整要么不存在。身份不符是错误而非缓存未命中，checksum 不符同样是错误——断点续跑不看目录是否存在，只认逐字节校验。

## 10. LightGBM

使用原生 `lightgbm.train`，避免 Qlib `LGBModel.fit` 对全局 Recorder/MLflow 的依赖。固定参数：

下表是基线 `lgbm_jp_baseline_v1` 的默认值；`--model-params` 可以按 `OVERRIDABLE_PARAMS` 白名单逐项覆盖，`objective` 与两个 reproducibility 开关是锁定项，不可覆盖。

| 参数 | 默认值 |
|---|---|
| objective | `mse`（锁定） |
| learning_rate | `0.05` |
| num_leaves / max_depth | `31` / `6` |
| min_data_in_leaf | `200` |
| feature_fraction / bagging_fraction | `0.8` / `0.8` |
| bagging_freq | `1` |
| lambda_l1 / lambda_l2 | `1.0` / `10.0` |
| num_boost_round | `1000` |
| early_stopping_rounds | `50` |
| reproducibility | deterministic、force_row_wise、四个 seed 相同 |

每个 fold 中 train 是唯一建树数据，valid 只用于 early stopping，test 只在训练完成后用于预测和评价。

训练本身由 `app/experiments/booster_training.py` 的 `train_booster` 承担，逐 fold 的「训练 → 预测 → 常数模型检查 → 日度评价 → 特征重要性」由本模块的 `run_fold` 承担。实验搜索不再持有这两者的副本：它把一个 trial 编译成 `DirectPredictionConfig` 后调用 `run_direct_prediction`，因此搜索排名所依据的模型和 predict 实际运行的模型必然是同一段代码，否则"搜索出的最佳参数"只是关于搜索自身的结论。早停指标由 `stop_metric` 选择，通过 `feval` 同时记录 `l2` 与 `rank_ic`，只有第一项参与早停（`first_metric_only`）。`stop_metric=l2` 与 LightGBM 内建 `l2` 早停选出同一轮、预测逐位相同，`model.txt` 仅 `[metric: ...]` 一行不同。

### 10.1 IC/Rank IC 有效性

沿用现有 07 `evaluate_cross_section` / `summarize_ic` 的研究数据门槛和语义：`MIN_COVERAGE = 0.90`、`MIN_VALID_SECURITIES = 100`。每个 test 预测日都保留一行 `daily_ic` 诊断，至少包含：

- `datetime`、`pool_size`、`valid_factor_count`、`valid_label_count`、`paired_count`；
- `factor_coverage = valid_factor_count / pool_size`；
- `label_coverage = valid_label_count / pool_size`；
- `ic_eligible`、`ic`、`rank_ic` 和 `unavailable_reasons`。

只有 factor coverage 和 label coverage 均至少为 `0.90`，且有效 score 数和有效 label 数各自至少为 `100` 时，该日才具备评价资格；这与现有 07 完全一致。随后在相同 `(datetime, instrument)` 配对行上计算 IC/Rank IC；配对后 score 或 label 零方差等情况继续记录为 unavailable，不进入均值与 ICIR 汇总。未达门槛的日期保留预测和诊断；prediction-only 的 test 尾日增加 `label_not_mature` 原因。如果 test 中没有任何可汇总的 IC 日期，预测以 `DirectExperimentDataError` 失败，不输出误导性的零值指标。

## 11. 执行流程

### 11.1 build-provider

1. 读取并校验 `DataSnapshot`。
2. 计算确定性的最终目录；已存在时调用 `open_day_provider`，完全通过身份、结构和 smoke read 后才返回 `created=false`，失败时不覆盖。
3. 从 PostgreSQL/DataSnapshot 同步导出到随机临时目录。
4. 用真实 Qlib 对临时目录执行 `$close/$factor/$vwap` smoke read；按有界 instrument 批次向后检查，直到找到有限 `$close`，避免排序靠前但全期不可交易的证券误判整个 provider，不存在任何有限 `$close` 时失败。
5. 写 manifest，并用原子 rename 生成最终目录。
6. 对 rename 后的最终目录调用 `open_day_provider`，通过后输出 provider path、manifest 和 `created=true`；不启动预测。

不估算输出大小，不检查磁盘预算或剩余空间。

### 11.2 delete-provider

1. 根据 `provider_root` 和 `snapshot_id` 解析唯一目标目录。
2. 验证绝对路径位于指定 root 内，枚举目标下所有版本子目录；每个版本都必须具有可读 manifest，且其中的 snapshot identity 全部匹配。
3. 递归删除该 snapshot 子目录。
4. 目标不存在时幂等成功并返回 `deleted=false`。

目标存在但没有版本子目录，或任一 manifest 缺失、损坏、身份不匹配时拒绝删除。

不查询数据库 bundle、Task 或正在执行的预测。

### 11.3 predict

1. 按 9.1 读取并校验 `DataSnapshot`、日期范围和 feature-set preset。
2. 调用 `open_day_provider` 打开已有 day provider；缺失时返回 `provider_not_found`，不自动调用 build。
3. 生成 rolling folds 并按多日标签自动 purge；对所有 fold 的 train / valid / test 日期调用 `build_stock_pool`，形成去重后的 `universe_by_date`。
4. 初始化 Qlib day provider，读取 Alpha158 或 Alpha360 和 `$close`。
5. 生成默认5日标签，按每日股票池过滤特征，写入或复用 9.4 的分片缓存。
6. 对每个 fold 从缓存装载 train / valid / test 三个矩阵，调用 `run_fold` 执行一次 LightGBM early-stopping 训练并预测该 fold 的 test chunk。
7. 拼接互不重叠的 fold 预测，按 10.1 复用现有 `evaluate_cross_section` / `summarize_ic` 计算逐日 IC、Rank IC 与汇总。
8. 返回 summary、逐 fold 完整 prediction、逐 fold feature importance；CLI 直接显示，不 publish。

## 12. 输出

`build-provider` 输出 `provider_path`、`created` 和完整 manifest；`delete-provider` 输出 `provider_path` 和 `deleted`。`predict` 的 summary 至少包含：

```json
{
  "snapshot_id": "...",
  "snapshot_version": 1,
  "provider_path": "...",
  "provider_logical_checksum": "...",
  "feature_set": "alpha158",
  "feature_count": 158,
  "feature_definition_checksum": "...",
  "stock_pool_policy_fingerprint": "...",
  "label_horizon": 5,
  "label_expression": "Ref($close, -6) / Ref($close, -1) - 1",
  "rolling_step": 20,
  "rolling_train_policy": "expanding",
  "fold_count": 0,
  "folds": [],
  "prediction_dates": 0,
  "valid_prediction_dates": 0,
  "min_pool_size": 0,
  "max_pool_size": 0,
  "train_rows": 0,
  "valid_rows": 0,
  "test_rows": 0,
  "best_iterations": [],
  "test_ic_dates": 0,
  "excluded_metric_dates": 0,
  "test_ic_mean": 0.0,
  "test_icir": 0.0,
  "test_rank_ic_mean": 0.0,
  "test_rank_icir": 0.0,
  "elapsed_seconds": 0.0,
  "pool_warnings": []
}
```

`folds` 逐项记录真实的 train/valid/test 区间、三类行数和 `best_iteration`；顶层 `train_rows`、`valid_rows` 是各次 LightGBM 调用实际消费行数之和，不代表去重后的数据量。`valid_prediction_dates` 是至少产生一个有限 score 并完成排名的 test 日期数；`test_ic_dates` 是通过 10.1 门槛、实际进入 IC 汇总的日期数。`daily_ic` 逐日给出 fold、pool size、有效 score/label 数、配对数、两类 coverage 和不可用原因，不能只返回最终均值。

`predictions` 包含 `fold`、`datetime`、`instrument_id`、`symbol`、`score`、`label`、`label_status`、`rank`。最后一个 test date 即使标签未成熟也展示预测排名。`feature_importance` 同样带 `fold`，不把不同 Booster 的 importance 冒充为单个模型结果。

结果默认不写 JSON、CSV、Parquet、model.txt，不写任何数据库表。

## 13. 错误与并发

- `DirectExperimentConfigError`：snapshot/date/feature-set/thread/seed 非法；
- `DirectProviderError`：build 无法生成，或 predict 遇到 provider 缺失、manifest 不匹配、必要文件缺失、Qlib smoke test 失败；
- `DirectExperimentDataError`：日历历史不足、某日股票池或 segment 为空、有效截面不足；
- `DirectExperimentTrainingError`：LightGBM 失败、模型为空或预测非有限。

build 与 delete 不提供跨进程锁，也不支持对同一 snapshot 并发执行 build/delete/predict。predict 在装配特征缓存期间对缓存目录持有排他锁，同一数据身份的第二个 predict 会**立即以 `FileExistsError` 失败而不是等待**；训练与预测阶段不再持锁。进程被强杀（例如 OOM）会遗留 `.running.lock`，需人工删除后才能继续。调用者负责串行执行。build 的临时目录使用随机后缀，失败时仅清理本进程自己的临时目录；若另一个 build 已先生成最终目录，本进程验证其身份后返回。

常数模型不得作为成功结果：预测整体零方差、所有树都是单叶或总 gain 为零时返回 training error。

## 14. 测试与验收

普通 CI 覆盖：

- 不读写 `QlibDataBundle`、`DataBundleBuildAttempt`、Task 或研究结果表；
- `backend/scripts/qlib_lightgbm_direct.py --help` 可执行，三个 subcommand 只负责参数适配并路由到对应 application service；
- build、delete、predict 互不调用；
- predict 在 provider 缺失时失败，不触发 PostgreSQL 导出；
- 相同 snapshot 第二次 build 仅在 `open_day_provider` 完整通过后返回已有 day provider；已有目录损坏时 build 失败且不覆盖；
- delete 只删除解析后严格位于 provider root 内的 snapshot 目录，且不存在时幂等成功；目标下无版本目录，或任一版本 manifest 缺失、损坏、identity 不匹配时拒绝删除；
- 不完整临时目录永远不会成为最终目录；
- manifest 身份不符、必要文件缺失或 smoke read 失败时拒绝读取；
- snapshot 不存在、非 J-Quants、不可回测、segment 超出 coverage、端点不是开市日、顺序非法，或 purge 后 segment 为空时，在 Qlib 读取和训练前失败；
- 默认标签严格等于 `close(t+6) / close(t+1) - 1`，可通过正整数 `--label-horizon` 修改；
- 默认每20个交易日生成一个 fold；train expanding、valid sliding、test chunks 连续且没有重复或缺口；
- 每个 fold 的 train/valid 末端按交易日历 purge，并逐 fold 独立调用 LightGBM；
- test 尾日没有完整多日标签时仍输出排名，并以 `label_not_mature` 排除在 IC 汇总之外；
- provider 用真实 pyqlib 0.9.7 读取 `$close/$factor/$vwap`；
- provider 中复权、VWAP、untradable NaN 与现有 07 exporter 逐位一致；
- Alpha158 为 158 列，Alpha360 为 360 列；
- 每个预测日调用 `build_stock_pool`，非成员行不能进入训练或排名；
- 两个 segment 接缝都满足 `label_exit < next_segment.feature_cutoff`，非法日期在训练前失败；
- test 特征/标签不能影响训练 matrix；
- application service 的完整编排只读取 session，逐 fold 重新调用训练并拼接连续且无重复的预测；
- 分片缓存与 `DataHandlerLP` + `InfToNaN`/`DropnaLabel`/`CSRankNorm` 参照管线产出的学习矩阵在 `rtol=1e-6` 内一致（特征以 float32 存储，参照管线为 float64），标签在 `rtol=1e-9` 内一致；
- 相同数据身份的第二次装配不再调用 Qlib；分片计划在同一身份下改变时拒绝复用；装载 segment 时缺行、重复行或标签外的多余行一律报错；
- 任一交易日的缓存行数与股票池声明的成员数不一致时拒绝封存（整日缺失与部分缺失同等对待）；运行所需交易日不在缓存内时，在得到 reader 之前失败；损坏或身份不符的缓存——无论在根目录还是某个分片——都以数据错误退出（2），不是未捕获异常；
- 导出的候选 JSON 中容器路径保持 POSIX 形式；配置新增渲染器无规则的字段类型时导出失败，不静默串化；
- `FeatureCacheSpec` 的字段集合恰为「跨度、label_horizon、三个分批旋钮」：fold 几何（`train_window` / `purge_horizon` / `rolling_step` / valid 窗口）不改变缓存身份，也不得改变缓存内容；分批旋钮改变身份则视为回归；
- 股票池过滤不截断前瞻标签：仅一天在池中的证券，其标签仍由池外未来收盘价算出；
- `purge_horizon` 大于 `label_horizon` 时按前者 purge，实验搜索因此可对多个 horizon 使用同一批评价日；
- 搜索侧不含任何训练代码：`search_runner` 源码中不出现 LightGBM、训练 seam、特征读取或 segment 装载，一个 trial 只被编译成 `DirectPredictionConfig` 后交给 `run_direct_prediction`；
- 越界或写入 seed 的 `--model-params` 在任何训练前失败；固定 `train_window` 逐 fold 滑动且长度恒定，长于首个 fold 可用历史时拒绝；
- `evaluate_qlib_candidates.py` 导出的候选 JSON 由 `direct_config` 渲染而非二次编译，能被 `predict --config` 原样消费，显式 flag 覆盖文件字段；train_window 长于计划首个 fold 实际训练天数时拒绝导出；
- prediction/label 按 MultiIndex 对齐；
- 每日输出 pool size、有效 score/label 数量、配对数和 coverage；只有两类 coverage 均至少 0.90 且有效 score 数、有效 label 数各自至少 100 的日期具备评价资格；没有可汇总 IC 日期时失败；
- 运行前后数据库相关表行数不变，artifact 目录不变。

slow test 使用真实连续 DataSnapshot 分别跑 Alpha158 和 Alpha360 direct rolling 链路，断言三个 fold、预测、IC/Rank IC 和 prediction-only 尾日均产生；同时比较运行前后的 bundle/build attempt、Task、ResearchRun、模型、PredictionRun、publication 表行数和 artifact 目录，证明直接入口没有写入产品状态。

Docker 验收使用第 4 节的脚本命令，只要求 `backend` service 能连接 PostgreSQL；不要求启动 worker 或 frontend，也不调用 backend API。默认 provider 在 `research_assets:/app/var` 中跨容器重建保持存在。

## 15. 实施顺序

1. 从现有 builder 提取无状态 `export_snapshot_day_provider`，用原测试证明 07 输出未变。
2. 实现共享的 `open_day_provider`，再实现独立的 `build_day_provider` 和 `delete_day_provider`。
3. 实现 snapshot/date 契约，以及只消费已有目录的 `run_direct_prediction`。
4. 接入逐预测日动态股票池、Alpha158/Alpha360、分片缓存、LightGBM 和带有效性门槛的评价。
5. 增加 `backend/scripts/qlib_lightgbm_direct.py` 薄入口及三个 CLI subcommand，并用第 4 节 Docker 命令在真实 J-Quants DataSnapshot 上验收。

## 16. 明确非目标

- 不管理 bundle 数据库状态、build attempt 或生命周期；
- 不提供 provider 列表、自动清理或恢复功能；
- 不做磁盘预算、剩余空间或容量检查；
- 不使用 job、结果 publish、API 或页面；
- 不做 portfolio/backtest，也不自动宣称模型具有可交易性；
- 不修改 PostgreSQL 市场数据。
