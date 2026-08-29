# 07 系统设计 — Qlib 多因子与 LightGBM 预测选股

- 状态：Accepted for implementation
- 日期：2026-08-28
- 配套 ticket：[07 — Qlib 多因子与 LightGBM 预测选股](./issues/07-qlib-multifactor-lightgbm.md)
- 前置设计：[06 Qlib 动量因子研究设计](./06-design-doc.md)
- 架构决策：[Use immutable Qlib data bundles derived from data snapshots](../../docs/adr/0001-use-qlib-data-bundles.md)

本文记录 `grilling` 会话 Q1–Q19 的共同决定。实现若要改变本文的领域含义、时间口径、数值口径或数据身份，必须先修改设计和验收条件；内部类名、表名和函数拆分可以在不改变行为的前提下调整。

06 已经落地的一切（QlibDataBundle、ResearchExperiment/Run/Artifact 生命周期、逐日冻结的 ResearchUniverse、周度标签、IC/分组统计、RankedScores 接口）在 07 中**复用而非重建**。本文只描述增量。

---

## 1. 目标与边界

07 建立第一条可复现的模型预测路径：

```text
DataSnapshot
→ QlibDataBundle (schema v2, 含 $vwap)
→ 每日 ResearchUniverse（与 06 同一政策）
→ FeatureSet 特征矩阵 + 周度标签
→ 按时间三段切分（含 embargo）
→ LightGBM 训练
→ TrainedModel
→ 样本外 PredictionRun
→ RankedScores + 与 6-1 动量并列比较
→ 研究页面
```

### 1.1 本票完成

- 把 QlibDataBundle 导出器升到 schema `2`，新增 `$vwap`；
- 建立代码内 `FeatureSet` 注册表，交付 `alpha158_jp_v1`（默认基线）、`alpha360_jp_v1`（可选择、非默认、实验性）、`momentum_only_v1`（对照）；
- 扩展 `ResearchExperiment` 支持 `kind = model`，新增 `TrainedModel` 与 `PredictionRun`；
- 按时间顺序的 train/valid/test 三段切分与 embargo；
- 通过 Qlib 的 `DataHandlerLP` / `DatasetH` / `LGBModel` 完成训练、早停、推理；
- 发布样本外 `PredictionRun`、三段评价指标、特征重要性、特征缺失率、训练曲线；
- 在同一次运行内产出 6-1 动量对照曲线，供并列比较；
- 研究页面支持发起训练、查看进度与失败原因、查看上述全部结果；
- 固定数据与随机种子下逐位可复现，普通 CI 不依赖网络或生产数据。

### 1.2 明确不做

- 不生成 `TargetPortfolio`（08）；
- 不运行 `ResearchBacktest`（10）；
- 不产生订单、成交、持仓或 Decimal 账务（09）；
- 不引入基本面因子（13）；
- 不交付 LightGBM 以外的模型（线性、LSTM、GRU 等留给后续票）；
- 不做换手率与成本敏感性分析（10）；
- 不提供"对已有 TrainedModel 在新快照上发起新 PredictionRun"的入口（12），但数据模型为它留出结构；
- 不允许用户提交任意表达式、类路径、module path 或 YAML workflow；
- 不部署 MLflow、Redis 或 MongoDB 服务；
- 不发布原始特征矩阵；
- 不自动清理已发布的 ResearchRun 或 ResearchArtifact。

---

## 2. 核心领域对象

| 对象 | 身份与职责 | 可变性 | 来源 |
|---|---|---|---|
| DataSnapshot | 一次研究允许读取的点时数据事实 | 不可变 | 04 |
| QlibDataBundle | Qlib 原生 provider 可读的市场事实副本 | 发布后不可变，可删除重建 | 06，07 升 schema v2 |
| ResearchExperiment | "研究什么"的规范化定义 | 不可变，相同定义复用 | 06，07 加 `kind` |
| ResearchRun | 执行一个实验的一次尝试 | 终态不可变，重跑新建 | 06，07 加两个 phase |
| **TrainedModel** | 一次训练产出的模型身份与产物引用 | **不可变** | **07 新增** |
| **PredictionRun** | 某个 TrainedModel 在固定快照与预测时点上的分数结果 | **不可变** | **07 新增** |
| ResearchArtifact | 一个运行发布的详细结果 | 发布后不可变 | 06 |
| RankedScores | 因子或模型向组合策略提供的统一排名 interface | 只读，不含目标权重 | 06 |

### 2.1 ResearchExperiment 的 kind

`ResearchExperiment` 新增 `kind` 列（`factor` | `model`），沿用同一张表、同一套指纹复用机制。`kind = model` 的规范化定义包含：

- `data_snapshot_id`；
- `feature_set`：所选特征集**展开后的有序完整定义**（§4.1），名称与版本仅随行供人识别；
- `label`：沿用 06 的 `WEEKLY_LABEL_DEFINITION`；
- `splits`：train / valid / test 三段的**显式起止日期**，以及被排除在三段之外的 embargo 截面日期（§6.1）；
- `model_params`：完整 LightGBM 参数集，含 `deterministic`、`force_row_wise` 与**展开后的四个种子**（§7.5.2），**不含 `num_threads`**，也不单独收录顶层 `seed`；
- `processors`：解析后的 infer / learn processor 列表本身（见下）；
- `fit_window`：`fit_start_time` / `fit_end_time` 的实际取值（§7.4）；
- `stock_pool_policy_fingerprint`；
- `evaluation`：与 06 完全相同的覆盖率门槛、分组数与统计定义。

`kind` 进入指纹 payload，因此 06 与 07 的实验不会在指纹空间上相撞。

**processor 列表必须原样进入指纹，而不是用一个手动维护的 `pipeline_version` 代替。** processor 虽然写死在代码里（§7.3），但它决定研究语义：把 `CSRankNorm` 换成 `CSZScoreNorm`、或删掉 `InfToNaN`，结果就会变。若不进指纹，系统会认定这是"同一个实验"并复用既有的 `ResearchExperiment`，让两次含义不同的运行挂在同一个实验下——直接违反 06 "任一研究语义变化都会产生新实验"的规矩。

选"列表本身"而非版本号，是因为版本号要靠人记得改，而改 processor 的人恰恰是最容易忘记的那个。把列表哈希进去，**改了就自动是新实验，不需要任何人记住任何事**。指纹 payload 形如：

```json
"processors": {
  "infer": [{"class": "InfToNaN", "semantics_version": 1,
             "kwargs": {"fields_group": "feature"}}],
  "learn": [{"class": "DropnaLabel", "kwargs": {}},
            {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}}]
}
```

**自研 processor 必须带 `semantics_version`。** 类名加参数认不出实现变化：改掉 `InfToNaN.__call__()` 的行为而不动类名和 kwargs，指纹纹丝不动，两种语义仍会复用同一个 Experiment。应用代码版本记在 `TrainedModel.runtime_identity`，那只能区分执行环境，且每次提交都变——用它当研究身份会让每个 commit 都产生新实验。

这里确实回到了本节上文反对的"手动版本号"，原因是两者的可展开性不同：**FeatureSet 的定义是数据**（表达式可以原样展开进指纹），**processor 的行为是代码**（没有可展开的声明）。取实现源码摘要会被换行和注释影响，不稳定。所以手动语义版本是这里最不坏的选项，其"忘记改"的失效模式由 §13.4 的测试兜住。

Qlib 自带的 processor 不加这个字段，它们的行为由 `pyqlib==0.9.7` 锁定（见 §3.3 关于升级的处理）。

同一份 processor 身份同时写入 artifact manifest，使产物能脱离代码自证它经过了什么加工。

#### 2.1.1 可信执行规范：旧 Experiment 不得由新代码静默代跑

Experiment 中保存的展开定义是**身份与审计事实，不是可执行配置**。数据库里的 FeatureSet 表达式、processor 类名、kwargs 或 module path 在任何时候都不得直接传给 Qlib；否则一个数据库写入入口就会绕过 §7.1 的任意代码加载防线。

为解决“不可变 Experiment”与“当前代码注册表”之间的漂移，在研究引擎内部设置一个 `ModelExecutionSpec` 深模块。它提供三个用例级操作，组成一个小 interface：

```text
compile_new(request) -> TrustedExecutionSpec
resolve_experiment(experiment_definition) -> TrustedExecutionSpec | unsupported
resolve_inference(inference_contract) -> TrustedInferenceSpec | unsupported
```

其 implementation 独占以下复杂性：从代码内 FeatureSet 注册表和 processor 常量构造规范化定义、展开参数与种子、计算定义指纹、生成 Qlib 对象，以及把当前可信定义与数据库中的 Experiment 定义逐字段比较。FastAPI、worker 和测试都不能各自重新拼装这些配置。

- 创建新 Experiment 时，`compile_new` 只接受类型化请求字段和注册表键；返回的完整定义同时用于落库和计算指纹；
- 执行已有 Experiment 时，`resolve_experiment` 从**当前代码内的可信 adapter**重新构造候选定义，再与已存定义做规范化后的逐字段比较；只有完全相同才允许运行；
- 若特征表达式、processor、参数展开或其他研究语义已变化，而代码中不再存在能产生该旧定义的可信 adapter，运行在接触 Qlib 前失败 `experiment_execution_definition_unavailable`，不得拿当前实现替旧 Experiment 执行；
- 若未来必须重放旧 Experiment，应在代码内保留能产生该完整旧定义的可信 adapter；选择依据是完整定义相等，不是仅凭名称或手工版本号；
- `TrustedExecutionSpec` 是进程内只读值，不写回或修改既有 Experiment。数据库定义只参与相等性检查和审计，永远不成为动态 import 或表达式执行来源。

这样 FeatureSet 或 processor 改动后，新请求自然得到新指纹；旧 Experiment 要么由仍受信任且完全匹配的 adapter 重放，要么明确失败，不存在“旧指纹、新实现”的第三种状态。

### 2.2 TrainedModel

```text
TrainedModel
  id
  research_run_id          唯一，一次运行恰好一个
  feature_set_name/version
  inference_contract       完整、有序、不可变的推理契约（见下）
  inference_contract_checksum
  label_definition
  train/valid/test 区间     冗余存放，便于脱离 Experiment 查询
  fit_start_time/fit_end_time
  model_params             实际生效的完整参数（含 num_threads，用于审计）
  seed
  best_iteration           训练结果，不进指纹
  runtime_identity         pyqlib / lightgbm / 依赖锁 / 代码版本
  data_snapshot_id
  bundle_id
  relative_path            指向 ResearchArtifact 目录内的 model.txt
  model_checksum           sha256
  created_at
```

`TrainedModel` 与 `ResearchRun` 分开而不是合并，因为**模型必须能脱离产生它的那次运行被引用**：12 的历史重放要在新的 DataSnapshot 上重新推理，那时不应也不能重训。合并成一个对象会让"复用已训模型"在数据模型上无法表达。

`inference_contract` 至少包含：展开后的有序 FeatureSet（表达式、字段、窗口、dtype、列序）、infer processor 的完整身份、所需 bundle schema 与字段、特征输出列顺序、模型格式，以及 `pyqlib` / LightGBM 兼容身份。其规范化 JSON 计算 `inference_contract_checksum`，同一份内容也写入 artifact manifest。

它让 12 可以直接判断“这个模型需要怎样的输入”，但仍然**不是可执行配置**：加载模型时必须把它交给 §2.1.1 的 `ModelExecutionSpec.resolve_inference`，由当前代码内可信 adapter 产生完全相同的契约；匹配不到则失败 `model_inference_contract_unavailable`。12 不需要穿透 `ResearchRun → Experiment → Artifact manifest` 拼凑推理规则，也不能直接执行数据库里的表达式或类名。

### 2.3 PredictionRun

```text
PredictionRun
  id
  research_run_id          产生它的那次执行
  trained_model_id
  data_snapshot_id
  prediction_start/end     覆盖的预测时点范围
  cross_section_count
  artifact_relative_path   指向 predictions.parquet
  created_at
```

**`PredictionRun` 只在成功发布后创建，且没有 `status` 字段。** 早期版本既声明它"不可变"又给了它一个会变的 `status`，这两者不能同时成立。

执行生命周期——排队、进行中、失败、取消——一律由 `ResearchRun` 承载：

- 07 里，它就是那次训练运行（§11.1 的 `predicting` phase）；
- 12 里在新 DataSnapshot 上独立推理时，会是那次推理自己的 `ResearchRun`，`research_run_id` 就是它的挂载点。

因此失败或取消**不产生 `PredictionRun`**，只在 `ResearchRun` 上留下状态与错误；`artifact_relative_path` 指向的产物归属明确——它属于 `research_run_id` 那次运行发布的 `ResearchArtifact`（§10.2）。这样"不可变"是真的：一条 `PredictionRun` 记录一经写入就不再改动。

07 的一次 `ResearchRun` 产出**恰好一个 `TrainedModel` 和一个 `PredictionRun`**，后者覆盖 test 段的全部周度截面。

---

## 3. QlibDataBundle schema v2

### 3.1 为什么必须升版

Qlib 的 `Alpha158` 默认配置含 `price.feature = ["OPEN","HIGH","LOW","VWAP"]`，`Alpha360` 含 60 列 `VWAP*`。两者都依赖 `$vwap`，而 06 的 schema v1 数据包没有这个字段。

三条备选路径中：

- 在 handler 层用 `$trading_value/$volume` 合成——**否决**：会让"这个特征需要哪些字段"无法在数据包层面被校验，正是 ticket 第一条验收要求的反面；
- 把 `VWAP` 从特征集里剔除——**否决**：J-Quants 真实提供成交额，VWAP 是可得的事实而非近似，且 08/10 的成交假设也会用到它；
- **采纳**：升 exporter schema 到 `2`，把 `$vwap` 作为真实字段写入数据包。

### 3.2 `$vwap` 的定义

数据包持有原始成交额 `trading_value`（日元）与原始成交量 `rawvolume`（股）。Qlib 的 `$close` 是复权价，`$vwap` 必须与之同基准：

```text
raw_vwap = trading_value / rawvolume
factor   = adjusted_close / raw_close        （Qlib 官方语义，v1 已有）
$vwap    = raw_vwap * factor
```

有效性条件，任一不满足则为 NaN，不填充：

- `rawvolume > 0`；
- `trading_value` 有效；
- `factor` 有效（即 `raw_close > 0` 且 `adjusted_close` 有效）；
- 行级质量状态不是 `untradable`（与 v1 对研究字段的处理一致）。

### 3.3 升版的连带后果

数据包身份是 `(data_snapshot_id, exporter_schema_version, pyqlib_version)`，因此 schema v2 产生**新身份**并触发一次重建。既有 v1 数据包与 06 已发布的 ResearchArtifact 都不受影响。

**v2 只增列，不改动任何既有列的取值。** 因此同一 DataSnapshot 上，06 的动量结果在 v1 与 v2 数据包上必须完全一致——这作为一条集成测试断言固化下来，是"只增列"这个承诺的唯一检验。

#### 3.3.1 升级 pyqlib 是一次研究语义变更评审

`pyqlib_version` **不进 Experiment 指纹**，沿用 06 的分工：Experiment 是"研究什么"，Run 记录"用什么执行的"（`runtime_identity`）。

但要承认这个分工在 07 留下一个缺口：`CSRankNorm` 的 `3.46` 常数、`Alpha158` 的表达式清单、`LGBModel` 的训练循环全部住在 pyqlib 里。升级 pyqlib 可能在指纹不变的前提下改变研究语义——数据侧由 §3.3 的 bundle 重建挡住了，模型侧没有结构性防线。

因此以流程补上：**升级 `pyqlib` 必须作为一次研究语义变更评审**，而不是普通依赖更新。评审至少复核本文写死为事实的三处——§5.2 的 `CSRankNorm` 算式、§7.3 关于 `ProcessInf` 的行为判断、§11.3 关于 `LGBModel.fit()` 的 callbacks 结构——确认它们在新版本下仍成立；若任一改变，先改设计再改依赖。

这是刻意选择的**流程约束而非结构约束**：把库版本塞进指纹会让每次升库使全部历史实验身份失效，代价大于它挡住的风险。选择本身记录在此，以便日后重估。

### 3.4 字段可用性校验

`FeatureSet` 的每个条目声明 `required_fields`。运行开始时按数据包 manifest 的 `fields` 校验：**缺字段是硬失败**（`missing_bundle_field`），不降级、不用 NaN 静默代替。

---

## 4. FeatureSet 注册表

### 4.1 形态

特征集在**代码内注册**，不接受用户提交的表达式、类路径或 YAML——与 06 的约束一脉相承。每个特征条目记录：

```text
(name, expression, required_fields, window, dtype)
```

`column_order` 不是条目上的字段，而是条目在有序列表中的**位置**：重排即不同的特征矩阵，因而是不同的定义。

`window` 以 **lag** 计，与 06 `StockPoolPolicy.required_bar_offsets` 把 6-1 动量端点写成 `(147, 21)` 的口径一致。若改用"含当日的 bar 数"，每个窗口都会大 1，`required_history_days` 被推到 148，`policy_fingerprint` 随之改变，§4.3 赖以成立的"06 与 07 逐日解析出同一个 ResearchUniverse"就不再为真。窗口取自表达式中的整数实参（含 `Quantile($close, 20, 0.8)` 的中间实参），不手工声明。

**进入 Experiment 指纹的是所选特征集展开后的有序完整定义，不是它的名称与版本号，也不是注册表的总版本。**

名称与版本（`alpha158_jp_v1` / `1`）保留给人识别与页面展示，但不作为研究语义身份的保证机制。理由与 §2.1 对 processor 的处理完全相同——版本号靠人记得改：

- 若只用名称 + 版本：某个特征的表达式被改而忘记升版，系统会错误地复用旧 `ResearchExperiment`，两次含义不同的运行挂在同一实验下；
- 若用注册表**总版本**：给一个毫不相干的特征集加个新条目，也会改变现有实验的身份，凭空产生一堆语义相同的"新实验"。

展开定义进指纹后，改了表达式就自动是新实验，动了别的特征集则本实验身份不变。同一份展开定义也写入 artifact manifest，使产物能脱离代码自证它算的是什么。

`GET /research/feature-sets` 把注册表原样暴露为只读列表。

每个 FeatureSet 另带三个产品元数据：`selectable`、`is_default`、`maturity`（`baseline` | `experimental`）。它们控制 API/UI 的可选性与提示，不属于特征计算语义，因此不进入 Experiment 指纹；真正进入指纹的仍是所选集合展开后的有序完整定义。

### 4.2 交付的三个集合

| 名称 | 内容 | 最大窗口 | selectable | is_default | maturity |
|---|---|---:|---:|---:|---|
| `alpha158_jp_v1` | Qlib `Alpha158` 的 158 列（kbar + price + rolling） | 60 | `true` | `true` | `baseline` |
| `alpha360_jp_v1` | Qlib `Alpha360` 的 360 列（CLOSE/OPEN/HIGH/LOW/VWAP/VOLUME 各 60 个 lag） | 59 | `true` | `false` | `experimental` |
| `momentum_only_v1` | 6-1 动量单列，复刻 06 | 147 | `false`（仅对照路径） | `false` | `baseline` |

表中最大窗口以 lag 计（见 §4.1）。Alpha360 每组有 60 列，但 lag 取值是 `0..59`，最大 lag 因此是 **59** 而非 60；两者都远小于 147，`required_history_days` 由动量对照的 147 决定，与 06 相同。

`alpha360_jp_v1` 必须能从 API 和页面被真实选择并完成训练，不能只登记 360 个名字后返回 `feature_set_disabled`。ticket 要求用户可选择 Alpha158/Alpha360，但没有要求 Alpha360 成为默认，也没有证据证明它在日本周频短历史上优于 Alpha158，因此默认继续使用 `alpha158_jp_v1`。

Alpha360 的收益是保留最近 60 日的原始价格量路径，让模型自行组合滞后位置，并为 Alpha158 的人工 rolling 特征提供不同归纳偏置的对照。Qlib 官方同时提供 Alpha360 与 LightGBM 的 workflow，所以它不是“只适合序列模型”的未支持组合。两套特征最终都只依赖 `open/high/low/close/vwap/volume`，schema v2 与 147 日预热已经覆盖，启用 Alpha360 不增加上游字段或历史范围。

风险也必须原样展示：360 列是 158 列的约 2.28 倍，相邻 lag 高度相关，而默认 train 只有约 50 个市场时间状态、valid 约 16 周，模型更容易拟合偶然的时序形状。不能把风险简化成“360 维大于 85 个样本”——训练行是“截面 × 证券”，但时间状态确实很少。官方中国日频长历史 benchmark 只能证明组合可运行，不能外推为本项目中的效果保证。

Alpha360 的结构化缺失同样不构成停用理由。Qlib 官方 LightGBM+Alpha360 配置使用 `infer_processors=[]`，LightGBM 原生处理 NaN，因此继续沿用 §7.3 的 `inf → NaN`、不填充策略。作为补偿，每次 Alpha360 运行必须发布逐列逐段缺失率、current close/volume 无效导致的整组异常计数，以及 train/test 缺失率漂移 warning；页面在特征集名称旁持续显示“实验性、短历史、结果仅供探索”。

### 4.3 股票池政策

`required_history_days = max(feature_set.max_window, 147) = 147`，`required_bar_offsets = (147, 21)`。

这与 06 默认实验的取值**完全相同**，因此 `policy_fingerprint` 相同：同一 DataSnapshot 下，06 与 07 的 ResearchUniverse 逐日一致。这使 §9.3 的跨票比较在股票域这一维上直接成立。

---

## 5. 标签与训练目标

### 5.1 标签

训练与评价都使用 06 已实现的**周度实际调仓标签**：

```text
entry = 本周因子截面后的下一实际交易日 adjusted_open
exit  = 下一周因子截面后的下一实际交易日 adjusted_open
label = exit / entry - 1
```

复用 `calculate_weekly_labels`，不重新定义。日度 5 日标签保留为诊断产物（与 06 一致），**不参与训练，不构成结论**。

**否决日度标签训练**：日度 5 日标签有 4/5 重叠，LightGBM 会把高度相关的重复样本当作独立观测，等效样本量远低于名义值；而且训练口径与评价口径分叉会让 §9 的并列比较失去意义。约 85 个周度截面 × 约 1000 只 ≈ 8–9 万名义行足以运行 LightGBM，但市场时间状态仍只有约 85 个；因此 Alpha158 可作为默认基线，Alpha360 只能作为带警告的实验性选择，二者都不得把行数直接解释成等量独立样本。

**否决 Qlib 默认标签** `Ref($close,-2)/Ref($close,-1)-1`：收盘到收盘，与本系统"下一交易日开盘成交"的假设不符。

### 5.2 标签归一化

`learn_processors = [DropnaLabel, CSRankNorm(fields_group="label")]`。

即把每个截面的收益率换成截面内的名次，再用普通回归（`loss = "mse"`）去拟合。

`CSRankNorm` 的确切算式是（pyqlib 0.9.7，`processor.py:351-358`）：

```python
t = df[cols].groupby("datetime").rank(pct=True)   # 秩百分位，(0, 1]
t -= 0.5
t *= 3.46                                          # 使标准差约为 1
```

即 `(rank_pct − 0.5) × 3.46`，**取值范围约 `[-1.73, 1.73]`，不是 `[0, 1]`**。这是一个仿射变换，不改变名次，因此下面的取舍不受影响；但设计和实现都不得把它当成 0–1 的百分位。

选 `CSRankNorm` 而非 `CSZScoreNorm` 的理由：只有约 85 个周度截面，日股个股周收益尾部厚（一次盈利预警 -30% 很常见），z-score 之后极端值**依然是极端值**，会主导整段训练损失。秩归一化把每个截面压成均匀分布，各周、各证券对损失的贡献严格相等。

**代价必须写明**：模型输出 `raw_score` **既没有收益量纲，也没有百分位量纲**。它是一个回归输出——目标虽然由名次导出，但回归值本身未经校准，不落在任何固定区间，也不能读作"排在前百分之多少"。

唯一可作位次解释的是 `rank_percentile`：**在同一个预测截面内对 `raw_score` 重新排名**得到的百分位（§8.1）。页面只用它表达排名，不对 `raw_score` 作任何百分比或预期收益的解释（§12.2）。

这个代价与系统设计是自洽的——`RankedScores` 接口本就只传分数与排名，08 拿到分数后只用位次。

评价始终用**原始收益率**，不用归一化后的标签。

**注意"归一化"在本文指两件事**，两者的决定相互独立：

| | 标签归一化（本节） | 特征归一化（§7.3） |
|---|---|---|
| 换算什么 | 未来收益（答案） | 158 个特征（题目） |
| 怎么换 | 换成截面名次，`(rank_pct − 0.5) × 3.46` | 减均值除标准差（`ZScoreNorm` 类） |
| 本设计 | **做** | **不做** |
| 为什么 | 让每个截面在训练里的分量相等 | 树对单调变换不变，做了不改变任何结果 |
| 是否需要先统计一遍数据 | 不需要（逐截面独立） | 需要（因此有泄漏风险，见 §7.4） |

最后一行是两者最要紧的区别：特征归一化要先算出均值和标准差，这个"先看一遍"的动作若看了 valid / test 段就是泄漏；标签归一化只在单个截面内部排名次，不跨时间，没有偷看的可能。

### 5.3 否决 lambdarank

Qlib 的 `LGBModel` 只支持 `loss ∈ {mse, binary}`；用 lambdarank 需要自写模型类、定义分组、把连续收益离散成相关度档位。这把一张"接一条基线"的票变成"造一个轮子"，而在约 1000 只 × 85 周的规模上相对 mse-on-rank 没有可靠优势——lambdarank 的主场是"只有前若干条重要"的场景，本系统对整个排序都关心。

---

## 6. 时间切分与泄漏隔离

### 6.1 三段切分

```text
train  →  [embargo]  →  valid  →  [embargo]  →  test
```

**embargo 是"整个周度截面被排除在所有分段之外"，不是"两段之间隔开一周"。**

推导：周度标签 `label(Wk)` 的 exit 是 **W(k+1) 之后的下一实际交易日**，即约 `Wk + 1 周 + 1 天`。所以训练段最后一个截面的标签，要到下一个周度截面**之后**才能确定。

```text
1/03  W1  train 最后一个因子截面
1/06      该样本按开盘价进入
1/10  W2  embargo —— 不属于任何分段
1/13      W1 的样本退出，train 标签在此刻才确定
1/17  W3  valid 第一个因子截面（特征截止 = 1/17 收盘）
```

`1/13 < 1/17` ✓。若把 valid 的起点提前到 W2（1/10），train 的标签就要用到 1/13 的价格，而 valid 的特征截止是 1/10 收盘——训练标签用上了验证期开始之后才有的信息。

因此每个 embargo **正好吞掉一个周度截面**：`valid_first = W(k+2)`，其中 `W(k)` 是 train 的最后一个截面。

按此计算，J-Quants Free 约 2 年滚动历史扣掉 147 日预热后约 85 个周度截面，切分预算是 `train + valid + test + 2 = 85`，例如 train ≈ 50 / valid ≈ 16 / test ≈ 16 加 2 个 embargo 截面 = 84，**余量只有 1 个截面**。创建页面推导默认切分时必须先扣掉 2 个 embargo 截面再按比例分配，不能先分配再挤 embargo。

`ResearchExperiment` 固化的是**显式日期**，比例只是创建页面的默认推导（60/20/20，在该快照的有效周度截面上推算）。以 J-Quants Free 约 2 年滚动历史计，扣掉 147 日预热后大致是 train ≈ 50 周 / valid ≈ 16 周 / test ≈ 16 周。

### 6.2 硬约束

顺序约束：

```text
train_start ≤ train_end < valid_start ≤ valid_end < test_start ≤ test_end
```

隔离约束——**这一条才是 embargo 的真正定义**：

```text
label_exit(train 最后一个截面) < feature_cutoff(valid 第一个截面)
label_exit(valid 最后一个截面) < feature_cutoff(test  第一个截面)
```

其中 `feature_cutoff(W)` 是该截面当日收盘，`label_exit(W)` 按 §5.1 取"W 之后的下一个周度截面之后的下一实际交易日开盘"。

**两个不等式必须按东京证券交易所的真实交易日历求值，不得用"加 7 个自然日"或"加 N 个日历天"近似。** 节假日周会让周度截面之间的实际间隔从 5 个交易日缩到 3 个，自然日近似在那种周上会给出错误答案。

之前版本写的"相邻两段间距 ≥ 标签窗口"是**错的**：它会放行 `train_end = W(k)` / `valid_start = W(k+1)` 这种切法——两段确实隔了一周，但 `label_exit(W(k))` 落在 `W(k+1)` 之后，泄漏照样发生。差的正好是一个周度截面。

违反任一条直接 400 拒绝，**不做静默修正**。§13.3 的"标签越界"测试断言的就是上面第一个不等式。

**test 区间进入 Experiment 指纹**：改切分 = 新实验。这是防止反复盯着测试集调参的唯一结构性手段。

### 6.3 三种合法读取权限

沿用 06 §5.3，并加一条：

- 特征阶段只能读取该截面 FeatureCutoff 及以前的数据；
- 评价阶段只能额外读取 Experiment 声明的 LabelWindow；
- **训练阶段只能读取 train 段的行；早停只能读取 valid 段的行；test 段在训练全程不可见**；
- DataSnapshot cutoff 之外的数据在任何阶段均不可读。

这些约束由 §13.3 的测试从真实读取行为一侧证明，不是靠代码审读确认。

---

## 7. 训练管道

### 7.1 Qlib 栈的使用深度

使用完整的 Qlib 栈：`DataHandlerLP` → `DatasetH` → `LGBModel` → `SignalRecord` → `SigAnaRecord`。

`DataHandlerLP` 的 `fit_start_time` / `fit_end_time` 机制是 ticket "训练预处理只拟合训练区间"这条验收的**现成、可被测试证明的实现**；自己重写等于重新实现它并重新证明它。

**硬约束：任何用户输入都不得流入 `init_instance_by_config`。**

Qlib 的 handler、dataset、model、processor 全部由配置字典经 `init_instance_by_config` 实例化，其实现是按字符串 import 模块再取属性：

```python
m_path, cls = split_module_path(config[key])
if m_path == "":
    m_path = config.get("module_path", default_module)
module = get_module_by_module_path(m_path)
_callable = getattr(module, cls)          # 可加载任意模块的任意属性
```

即 `{"class": "任意名", "module_path": "任意.模块"}` 能加载任何东西。使用 Qlib 的 handler 就绕不开这个机制，因此边界必须由调用方守住：喂给它的 `class`、`module_path` 和 `kwargs` **全部是模块级常量**，不经过任何请求体、数据库字段或配置文件。这是 06 "用户不能提交任意表达式、类路径或 workflow 配置"在 07 的具体落点。

### 7.2 recorder 目录

`LGBModel.fit()` 内部调用 `R.log_metrics`，`R.get_exp(create=True)` 会自动创建一个本地 MLflow 目录。Qlib 的默认 uri 是 `file:<cwd>/mlruns`——**不配置就会写进 worker 的工作目录**。

因此：

- 通过 `qlib.init` 的 `exp_manager` 把 uri 显式指向受控路径 `settings.qlib_recorder_dir/{run_id}`；
- 训练在显式的 recorder 作用域内进行；
- 训练结束后以 `LGBModel.predict(dataset, segment=...)` 分别生成 train / valid 分数；test 分数由 `SignalRecord(model, dataset, recorder).generate()` 生成并保存为 recorder 的 `pred.pkl`，同时生成 `label.pkl`；
- `SigAnaRecord(recorder).generate()` 必须在 `SignalRecord` 成功后调用。pyqlib 0.9.7 的 `SigAnaRecord` 依赖 `pred.pkl` / `label.pkl`，缺少父记录时只会跳过，不能把“没有抛异常”误判为分析成功；
- `SignalRecord` 的 test `pred.pkl` 是 §8 `PredictionRun` 的权威原始分数来源，应用把它规范化为 `predictions.parquet`；不得再走第二次独立 test 推理。train / valid 分数仍通过同一个 `LGBModel.predict` interface 取得；
- `SigAnaRecord` 只负责 Qlib 自身的 test IC/Rank IC 记录与栈集成证明；产品展示的 train / valid / test 指标仍全部经过 §9.2 的本地统一统计 module。集成测试断言 `SigAnaRecord` 的 test IC/Rank IC 与本地统计在相同有效行上、给定容差内一致；
- **运行结束后删除该子目录**。所有需要长期保存的东西（valid 曲线、指标、模型）都已规范化写进 `ResearchArtifact`，留着那个目录唯一的作用是诱惑后来者去读 Qlib 的内部布局——正是 06 明确否决过的事；
- 配置开关 `keep_qlib_recorder_dir`（默认 `false`）供调试，开启时页面标注"调试产物，非接口"；
- 该目录的路径不出现在任何 API 响应中。

### 7.3 processors

**以下列表是模块级常量，不是配置入口。** 它不出现在任何请求体、数据库字段或配置文件中；用户只能选 `feature_set` 的名字（§12.1），不能增删或替换其中任何一道工序。列表本身进入 Experiment 指纹（§2.1）。

```python
infer_processors = [InfToNaN(fields_group="feature")]   # 自研，见决定 2
learn_processors = [DropnaLabel, CSRankNorm(fields_group="label")]
```

本节以下所有"归一化"均指**特征归一化**；与 §5.2 的标签归一化是两件事，对照见该节末尾的表。

这份列表由三个**互相依赖**的决定共同确定：

```text
决定 1  不做特征归一化
   └→ 填 0 失去"填成训练集均值"这个唯一合理的语义
      └→ 决定 3  不填充缺失值
         └→ 依赖 LightGBM 原生 NaN 分支
            └→ 前提是没有 inf 混入
               └→ 决定 2  必须清理 inf
```

**三者不能分开改。** 单独推翻任何一个，另外两个的正确答案都会跟着变。最糟的改法是各取一半——做了归一化却不填充（归一化白做），或不归一化却填 0（填出没有量纲的假值）。要么整条走"归一化 + 填充"（神经网络那条路），要么整条走"不归一化 + 不填 + 只清理 inf"（本设计）。

**决定 1：不做特征归一化。** 决策树对单调变换不变——把所有数乘以 2，分裂阈值跟着变，树的结构与预测逐位相同。Qlib 官方的 LightGBM+Alpha158 基准 `infer_processors=[]` 正是这个原因；`RobustZScoreNorm` 只出现在神经网络基准里。加它不改变任何结果，却多出一层需要理解、测试和排查的状态。

**决定 2：必须清理 inf，且必须自己写这道工序。** Alpha158 含 `Rsquare`、`Resi`、`Slope` 等在常数序列上退化的算子——连续停牌导致价格方差为零时分母为 0。`inf` 对 LightGBM 是有毒的：它不是缺失值，会被当作真实极端值参与分裂点搜索。

**不能用 Qlib 自带的 `ProcessInf`。** 它的名字听起来只是"处理无穷"，实现却是逐截面均值填充：

```python
# qlib/data/dataset/processor.py:161-176（pyqlib 0.9.7）
def process_inf(df):
    for col in df.columns:
        # FIXME: Such behavior is very weird
        df[col] = df[col].replace([np.inf, -np.inf], df[col][~np.isinf(df[col])].mean())
    return df
data = datetime_groupby_apply(data, process_inf)
```

外层套 `datetime_groupby_apply`，所以 `±inf` 被换成**该截面该列的均值**——正是决定 3 明文否决的"把停牌股伪装成各项指标完全平均的股票"。Qlib 自己在实现里留了 `FIXME: Such behavior is very weird`。

因此注册一道语义明确的自研 processor `InfToNaN`，只做 `±inf → NaN`，无状态、逐行、不看任何其他数据。它**以实例形式**传给 `DataHandlerLP`（`check_transform_proc` 的 `if not isinstance(p, Processor)` 分支会让实例原样通过），不经 `class` / `module_path` 字符串——这同时满足 §7.1 的硬约束。

验收测试必须**直接断言 `±inf` 的输出为 NaN**，不能只检查 processor 的类名——这条 bug 正是靠名字判断行为才漏进设计的。

**决定 3：不填充缺失值。** 交给 LightGBM 的原生 NaN 分支——训练时它为每个分裂点学习"缺失往左还是往右"，等于让模型自己判断"算不出这个特征"是否携带信息。而在日股这高度非随机（停牌、退市、流动性枯竭），这个信息是真的。填 0 在未做特征归一化时是胡填（"20 日均价是当前价的 0 倍"），在做了特征归一化后等于把停牌股伪装成"各项指标完全平均的股票"。

06 §4.4 立过规矩："后续模型如需填充，必须由 ResearchExperiment 中显式、可追溯的 processor 完成。" 07 是那句"后续模型"第一次到场，**行使这条豁免权的方式是不行使它**。

作为交换，**每个特征在 train / valid / test 三段的缺失率必须作为诊断产物发布**（`feature_missing_rate`）。一个在 train 段缺 3%、在 test 段缺 40% 的特征是数据问题而非模型问题，不发布这张表就发现不了。

### 7.4 训练区间声明：现在没有消费者，但必须正确

**本节是 §7.3 决定 1 的直接后果。**

processor 分两类：一类逐截面独立计算（`InfToNaN` / `CSRankNorm` / `DropnaLabel`），一类必须先把一批数据统计一遍才能开始加工（`ZScoreNorm` / `RobustZScoreNorm` 等，这个"先看一遍"的动作即拟合）。**只有后者会泄漏**——若拿全区间去估计均值和标准差，valid / test 段的信息就经由那个均值渗进了训练。ticket 那条"训练预处理只拟合训练区间"防的就是这件事。

按 §7.3，管道里三道工序**全属前一类**，一道有拟合状态的都没有。因此该验收当前没有实际的检查对象——要防的事故在这里物理上不可能发生。

**不为此硬塞一个无用的特征归一化。** 那是本末倒置：引入一层不改变任何输出的状态，只为让验收表格上能打勾。处理办法是：

1. `fit_start_time` / `fit_end_time` 仍显式设为 train 段，写进 `TrainedModel` 记录并进入 Experiment 指纹（§2.1）——规矩已经写下，只是眼下没有工序受它约束；
2. 补一条测试：注入一个 `ZScoreNorm` 的 spy，断言它 `fit()` 时看到的行 100% 落在 train 段内，一行都不越界。这道工序是测试专用的，不进生产流水线，唯一目的是当探针，证明那个日期声明真的生效了。

不填这个声明（"反正没人用"）是最坏的选项：将来谁加了一道有拟合状态的 processor——引入线性模型或 NN 时必然会加——它会默默拿全区间去估计参数，泄漏就此发生且不报任何错。

区别在于**"我们保证不会泄漏"和"我们验证过不会泄漏"**：前者是一句话，后者是一条会在 CI 里失败的测试。

### 7.5 参数集 `lgbm_jp_baseline_v1`

```text
objective            = mse
learning_rate        = 0.05
num_leaves           = 31
max_depth            = 6
min_data_in_leaf     = 200
feature_fraction     = 0.8
bagging_fraction     = 0.8
bagging_freq         = 1
lambda_l1            = 1.0
lambda_l2            = 10.0
num_boost_round      = 1000
early_stopping_rounds= 50          （在 valid 段上）
deterministic        = true
force_row_wise       = true
seed                 = 固定默认值；由顶层 seed 字段控制（§7.5.2），
                       服务端展开为 seed / bagging_seed /
                       feature_fraction_seed / data_random_seed
num_threads          = settings.qlib_threads（默认 2）
```

Qlib 官方基准的参数（`num_leaves=210, max_depth=8, learning_rate=0.2, lambda_l1=205.7`）是在 CSI300 十余年数据上调出的，直接搬到约 8–9 万行、约 85 个截面上会严重过拟合。

**`num_threads` 不进指纹。** `deterministic=true` 配合 `force_row_wise=true` 时，本项目承诺不同线程数下的**树结构、预测分数与研究指标相同**；把线程数放进指纹会让调整资源配置变成新建实验。LightGBM 4.7.0 会把 `[num_threads: N]` 写进原生 `model.txt`，所以 1 线程与 4 线程的文件字节及 sha256 **明确允许不同**，不能把原始文件 checksum 当作跨线程语义身份。`num_threads` 仍写入 `TrainedModel.model_params` 与 manifest 供审计。这些承诺由 §13.2 在 CI 中真跑验证，不只信文档。

`best_iteration` 是训练**结果**，记在 `TrainedModel` 上，不进指纹。

#### 7.5.1 可覆盖参数白名单

`POST /research/model-runs` 接受参数覆盖，但**只接受下表列出的键，且必须落在给定范围内**。这是一个类型化白名单，不是任意字典。

| 参数 | 可覆盖 | 范围 |
|---|---|---|
| `objective` | **否** | 固定 `mse`。改它就是换研究目标（§5.2），属于新的设计决定而非调参 |
| `deterministic` / `force_row_wise` | **否** | 固定 `true`。可覆盖等于允许调用方废掉 §13.2 的可复现性承诺 |
| `num_threads` | **否** | 仅由 `settings.qlib_threads` 决定 |
| `seed` 及三个派生种子 | **否** | 不走参数覆盖，改由顶层 `seed` 字段控制，见 §7.5.2 |
| `learning_rate` | 是 | `(0, 0.5]` |
| `num_leaves` | 是 | `[2, 255]` |
| `max_depth` | 是 | `[2, 12]` |
| `min_data_in_leaf` | 是 | `[20, 5000]` |
| `feature_fraction` / `bagging_fraction` | 是 | `(0, 1]` |
| `bagging_freq` | 是 | `[0, 10]` |
| `lambda_l1` / `lambda_l2` | 是 | `[0, 1000]` |
| `num_boost_round` | 是 | `[1, 5000]`（资源上限） |
| `early_stopping_rounds` | 是 | `[1, 500]` |

**出现白名单以外的键，或任何值越界，直接 400 拒绝**——不静默忽略、不裁剪到边界。静默修正会让用户以为跑的是他填的参数，而 Experiment 指纹记的是另一套。

白名单**展开后的完整参数集**（含未被覆盖的默认值与全部锁定项）进入指纹；`num_threads` 等运行环境参数按 §2.2 记在 `TrainedModel` 中供审计。

#### 7.5.2 seed 只有一个入口

`seed` 是 `POST /research/model-runs` 的**顶层类型化字段**，不是参数覆盖里的一个键：

```json
{
  "seed": 42,
  "model_params": { "learning_rate": 0.03 }
}
```

**`model_params` 中出现 `seed`、`bagging_seed`、`feature_fraction_seed` 或 `data_random_seed` 一律 400 拒绝**，错误码 `seed_must_be_top_level`。

两个入口必须消灭其中一个。留着两个就要额外定义"谁覆盖谁"，而这条优先级规则只会在下面这种请求里被人读到：

```json
{ "seed": 42, "model_params": { "seed": 99 } }
```

——用户显然认为自己指定了种子，却说不清跑的是哪一个，指纹里记的又是第三种可能。**可复现性是本票的验收条件**（§13.2 承诺同机重跑逐位一致），而一条只在冲突时才生效的隐式规则，恰恰是最容易在实现和文档之间漂移的那种约定。宁可拒绝请求。

选顶层而非白名单，因为 `seed` 与其余参数的性质不同：它不是一个可调的模型超参，而是**一次运行的可复现性锚点**，且要由服务端展开成四个 LightGBM 参数：

```text
seed → seed / bagging_seed / feature_fraction_seed / data_random_seed
```

展开在服务端完成，四个值全部相同，用户无法单独设置其中任何一个。**展开后的四个种子随白名单参数一同进入 Experiment 指纹**（顶层 `seed` 本身不单独进，避免同一事实在指纹里出现两次）。

顶层字段可省略，省略时取 §7.5 的固定默认值。

---

## 8. 预测与 RankedScores

### 8.1 PredictionRun 的行

对 test 段的每个周度截面，每只 ResearchUniverse 成员一行：

| 列 | 含义 |
|---|---|
| `prediction_date` | 预测时点，即 FactorObservationDate |
| `instrument_id` | 稳定证券身份 |
| `raw_score` | 模型原始输出 |
| `average_rank` | 截面平均秩（并列取平均） |
| `rank_percentile` | 秩百分位，**RankedScores 接口字段** |
| `normalized_score` | 截面 z-score，仅供展示与诊断 |
| `label_status` | `valid` / `label_not_matured` / 结构化缺失原因 |
| `trained_model_id`、`data_snapshot_id` | 追溯 |

**PredictionRun 不含目标权重、不含持仓、不含 top-N 截断。** "选股排名"就是按 `rank_percentile` 排序的展示；截断是 08 的策略决定。

### 8.2 接口同构

`GET /research/runs/{id}/ranked-scores` 对 `kind = factor` 和 `kind = model` 必须返回**逐字段同构**的 payload。`RankedScores` 这个接口的全部意义就是让 08 不必关心分数从哪来；一处字段分叉就要让 08 写两遍代码。

### 8.3 标签未成熟的尾部

test 段末尾若标签窗口尚未落入 DataSnapshot，该截面的预测分数**照常发布**并标 `label_not_matured`，但不进入任何评价汇总。与 06 §5.2 一致。

---

## 9. 评价与比较

### 9.1 三段都算，只有 test 是结论

train / valid / test 三段都计算完整指标并展示，但页面对 train / valid 明确标注"样本内，不构成结论"。

理由：train 的 Rank IC 是 0.15 而 test 只有 0.01，这个对比本身就是过拟合最直接的证据。只展示 test 等于把证据藏起来。

### 9.2 指标清单

每段、按周度截面，**逐项复用 06 已实现的 `evaluate_cross_section` 与 `summarize_ic`**，不写一行新统计代码：

- IC（Pearson，预测分数 vs **原始收益率**）；
- Rank IC（Spearman）；
- ICIR / Rank ICIR（`mean(valid IC) / sample_std(valid IC)`，**不年化**）；
- 五分组等权收益 + 最高减最低组；
- 预测覆盖率、标签覆盖率、合格截面数、可排名截面数，以及最终进入汇总的 `n`。

新增一个标量：

- `group_monotonicity` = Spearman(组序号, 组收益)。五组收益是否随预测分数单调递增，比 IC 更直观地回答"这个模型有没有用"，且对少数极端截面比 IC 均值更鲁棒。

零方差与无定义一律 `unavailable`，**绝不写 0**（06 §9.1）。这里的零方差必须按单个周度截面判断：预测常数记 `constant_prediction`，标签常数记 `constant_label`；该截面的 IC、Rank IC、五分组和 `group_monotonicity` 均不进入汇总。汇总只使用有定义截面，并把实际 `n` 与总截面数同屏发布，不能依赖 pandas `skipna` 静默跳过后只显示均值。

### 9.3 与 6-1 动量的并列比较

**主对照在同一次运行内产生**：test 段的每个周度截面上，除 LightGBM 预测分数外，同时计算 6-1 动量原始分数，两条曲线共享股票域、标签、覆盖率门槛与统计代码，唯一变量是"分数怎么来的"。

共用同一份统计代码是"可并列比较"能成立的技术前提——两条曲线只要有一处口径分叉，比较就失去意义。

注意 `momentum_only_v1` 的定位：它是**不经训练、直接作为对照分数**参与同一评价，**不是**"用 LightGBM 训一个单特征模型"。后者测的是管道差异而非特征价值。

**与 06 既有 ResearchRun 的并列**也允许，且因 §4.3 的股票池政策完全相同，在股票域这一维上直接可比。但必须同屏显示差异维度：`data_snapshot_id`、评价区间、观察频率。当 DataSnapshot 或评价区间不一致时页面标注"口径不同，仅供参考"，不隐藏也不静默对齐。

### 9.4 观察点数量与指标同屏

J-Quants Free 的历史限制与 test 段的周度观察点数量（预计约 16 个）必须与所有指标同屏，紧贴数字而非放在页脚：写作 `Rank ICIR = 0.42 (n=16)`。16 个观察点的 ICIR 置信度极低，这个事实必须和数字长在一起，否则整个比较会被过度解读。

### 9.5 特征重要性

LightGBM 给两种口径，**两种都存，页面默认展示 `gain`**：

- `gain`：该特征带来的损失下降总量；
- `split`：该特征被选作分裂点的次数。

`split` 会系统性高估高基数的连续特征（它们天然更容易被反复选中），`gain` 更接近"贡献了多少"。两个并列才能看出"被用了几百次、几乎不贡献"这类噪声特征。

表结构：`feature_name, gain, split, gain_rank, split_rank`。额外发布标量"gain 为 0 的特征数"——它一眼说明 158 个特征里有多少根本没被用上。

---

## 10. 产物、持久化与生命周期

### 10.1 PostgreSQL 保存

- `ResearchExperiment`（含 `kind`）与定义指纹；
- `ResearchRun`、状态、运行环境、进度、错误和 warnings；
- `TrainedModel`、`PredictionRun`；
- `ResearchArtifactPublication` 发布日志（`prepared` / `committed` / `failed`，失败时带结构化错误），用于跨文件系统与数据库的崩溃恢复；
- 标量摘要；
- `ResearchArtifact` manifest、schema version、位置和内容校验值。

### 10.2 ResearchArtifact 目录

文件内部仍沿用 06 的原子发布机制（临时目录 → 全量校验 → `os.replace`），但 **`os.replace` 与 PostgreSQL commit 不是一个原子事务**。07 以 `ResearchArtifactPublisher` 深模块**替换**现有 `ResearchArtifactWriter`，不是在旧 writer 外再叠一层；`kind = factor` 与 `kind = model` 此后共用这一条发布 seam。该模块封装文件系统、发布日志、相应的不可变领域记录以及 ResearchRun/Task 终态，不再把一次 rename 称为整个发布成功。调用方只有一个幂等 interface：

```text
publish(run_id, validated_staging_artifact,
        result_metadata: FactorResult | ModelResult) -> PublishedResearchResult
```

`ModelResult` 必须同时携带 `TrainedModel` 与 `PredictionRun` 元数据；`FactorResult` 不携带这两项。类型判别来自已经锁定的 `ResearchExperiment.kind`，调用方不能提交互相矛盾的组合。

07 新增的 artifact 内容：

| 文件 | 内容 |
|---|---|
| `model.txt` | LightGBM 原生文本格式模型 |
| `predictions.parquet` | §8.1 的全部行 |
| `train_metrics.parquet` / `valid_metrics.parquet` / `test_metrics.parquet` | 逐截面指标 |
| `momentum_benchmark_metrics.parquet` | §9.3 的对照曲线 |
| `feature_importance.parquet` | §9.5 |
| `feature_missing_rate.parquet` | §7.3 |
| `training_curve.parquet` | valid loss vs iteration |
| `summary.json` | 标量摘要 |
| `manifest.json` | schema version、runtime identity、inference contract 与 checksum、warnings、逐文件 sha256 |

**用 LightGBM 原生 `save_model()` 文本格式，不用 pickle。** pickle 跨版本不可读，而 ticket 要求模型产物与版本身份绑定且可复现。

模型文件放进 artifact 目录使得"模型产物与数据快照、特征集、标签、参数、Qlib/代码版本绑定"这条验收**结构上自动满足**——manifest 有 `runtime_identity`、definition 指纹和 `inference_contract`；`TrainedModel` 冗余保存推理契约及其 checksum，并以相对路径与文件 checksum 定位、校验模型。文件缺失或契约不匹配均报明确错误，不静默降级。

#### 10.2.1 可恢复的发布协议

`ResearchArtifactPublisher` 按下面的固定协议工作：

1. 在最终目录的同一文件系统内写临时目录，完成 schema、行数、manifest 和逐文件 checksum 校验；此时数据库与最终目录都不可见；
2. 第一笔数据库事务锁定 `ResearchRun`，确认它仍处于 `publishing`，插入唯一的 `ResearchArtifactPublication(status = prepared)`，保存 `run_id`、临时/最终相对路径、完整 manifest、logical checksum 和类型化 `result_metadata`（model kind 包含待创建的 `TrainedModel` / `PredictionRun` 元数据）；提交后才允许 rename；
3. 执行 `os.replace(staging, final)`。若 final 已存在，不直接报 `FileExistsError`：先校验其 manifest 与日志中的 logical checksum；完全相同视为上一次尝试已经完成 rename，继续下一步；不同则失败 `artifact_path_conflict`，且不得覆盖未知内容；
4. 第二笔数据库事务再次锁定 run、对应 `Task` 与 publication，校验 final 目录，然后在**同一个事务**中创建 `ResearchArtifact`；model kind 同时创建 `TrainedModel`、`PredictionRun`。随后把 publication 改为 `committed`，把 run 与 task 都改为 `succeeded` 并写入 summary / progress / warnings / finished_at；该 kind 的全部领域记录和两个成功终态要么同时可见，要么都不可见；
5. `publish` 被重复调用时，`committed` 直接返回既有结果，`prepared` 从第 3 步继续，`failed` 重放已记录的结构化错误而不自动重试。所有表以 `research_run_id` 唯一约束保证重复恢复不会生成第二份记录。

研究任务的启动恢复必须先查询 publication 日志，再套用普通的“孤儿运行失败”规则；不能先把带 `prepared` 日志的 run 永久判成失败。即使通用 runner 已给孤儿 Task 填了默认失败状态，研究任务 recovery 仍按日志完成下面的确定性协调，并在同一事务中覆盖 Task 终态：

- 临时目录存在、最终目录不存在：校验临时目录后执行 rename 并 finalize；
- 最终目录存在且 checksum 匹配：直接 finalize；
- 两者都不存在：在同一事务中把 publication、run 与 task 标成失败 `artifact_staging_missing`，日志保留供审计；
- 最终目录 checksum 不匹配：在同一事务中把 publication、run 与 task 标成失败 `artifact_path_conflict`，不删除、不覆盖，由人工处理。

在 publication 日志提交前崩溃可能留下无主临时目录。scavenger 只删除超过 TTL、名称符合本模块临时目录格式、且没有任何 `prepared` publication 引用的目录。不得按宽泛 glob 删除，也不得清理 final 目录。

`prepared` 是发布的恢复提交点：到达此状态后，恢复器会完成一个已经全量校验的发布。结合 §11.3 的 best-effort 取消语义，之后才到达的取消请求不回滚 publication；API 仍只承诺请求已登记。

### 10.3 不发布原始特征矩阵

最多 360 列 × 约 40 万行，体积可达数百 MB 至 GB 级，且完全可由 bundle + FeatureSet 展开定义重算。发布它会让 artifact 体积涨两个数量级，换来的只是省一次重算。

### 10.4 生命周期

- `ResearchArtifact` 不自动清理，07 不提供删除入口（与 06 一致）；
- QlibDataBundle 可以删（运行结束后不再需要），删了不影响已发布结果；
- 未知 artifact schema version 返回明确不兼容错误，不猜测列含义，不原地重写旧产物。

---

## 11. 状态机、失败语义与取消

### 11.1 phase

`research_run_status` 枚举**只加两个值**：`training`、`predicting`。

```text
queued → waiting_for_bundle → computing_factors → computing_labels
       → training → predicting → evaluating → publishing
       → succeeded / failed / cancelled
```

`computing_factors` 在 07 里就是“计算所选 FeatureSet 的特征矩阵”（Alpha158 为 158 列，Alpha360 为 360 列），`computing_labels` 就是“算周度标签”——阶段语义未变，不值得另造名字。`kind = factor` 的运行永远不会进入 `training` / `predicting`。

### 11.2 成功与失败的界线

| 情形 | 结果 |
|---|---|
| train 段没有任何有效样本 | **失败** `no_training_samples` |
| valid 段没有任何有效样本 | **失败** `no_validation_samples` |
| valid 部分合格截面的预测不可排名 | **成功**，截面记 `constant_prediction`、不进汇总并警告 |
| valid 没有任何可排名截面（判据见下） | **失败** `model_no_rankable_validation_cross_section` |
| test 段个别截面覆盖率不足 | **成功**，该截面保存但不进汇总 |
| test 段尾部标签尚未成熟 | **成功**，分数照发并标 `label_not_matured` |
| test 段没有任何合格截面 | **失败** `no_valid_test_cross_section` |
| 数据包缺 `FeatureSet` 声明的字段 | **失败** `missing_bundle_field` |
| 当前可信 adapter 无法完整复现已有 Experiment 定义 | **失败** `experiment_execution_definition_unavailable`，且不得初始化 Qlib |
| publication 的临时与最终目录都丢失 | **失败** `artifact_staging_missing`，保留发布日志 |
| final 路径已有不同 checksum 的内容 | **失败** `artifact_path_conflict`，不覆盖、不删除 |

#### 11.2.1 常数模型的判据：逐截面看输出，不看 `best_iteration`

系统的排名语义是“同一个 `prediction_date` 内比较证券”，所以要拒绝的是**在 valid 中没有任何一个可排名横截面的模型**，不是“整个 valid 段所有行只有一个值”。例如 W1 所有证券都得 0.1、W2 都得 0.2、W3 都得 0.3，整段方差大于零，但每周内部仍全部并列；模型只区分了日期，完全没有截面选股能力。

**`best_iteration == 0` 不是这件事的充分判据。** LightGBM 在完全无法分裂时仍会产出一棵**只有一个叶子的树**：

```text
best_iteration = 1
num_trees      = 1
所有预测分数相同
```

此时 `best_iteration == 1`，旧判据放行，而模型是空洞的。§7.5.1 的白名单允许 `min_data_in_leaf` 最高到 5000，在本票这个数据规模（约 8–9 万行、单截面约 1000 只）上很容易触发——一个合法的参数覆盖就能造出这种模型。

因此训练结束后先应用 §9.2 的有效证券数、预测覆盖率和标签覆盖率门槛，再对每个 valid `prediction_date` 独立检查预测是否可排名。只使用有限的 `raw_score`，按 float64 数值精度消除舍入量级的假差异：

```python
scores = finite_cross_section_scores.astype("float64")
scale = max(1.0, float(np.max(np.abs(scores))))
tolerance = 64 * np.finfo(np.float64).eps * scale
rankable = float(np.max(scores) - np.min(scores)) > tolerance
```

这个容差只排除浮点运算噪声，不声称“真实但很小的分差没有经济意义”。若以后要按交易成本或最小有效分差设置业务阈值，必须另立显式、可配置且进入 Experiment 指纹的参数。

逐截面结果和 valid 总体门槛如下：

| 情形 | 处理 |
|---|---|
| 某个合格 valid 截面不可排名 | 保存 `metric_status = constant_prediction`、分数范围与原因；该截面指标 `unavailable`，不进入聚合 |
| valid 仍有至少一个可排名截面 | 模型可以继续发布；写入 `some_validation_cross_sections_are_not_rankable` warning，并发布 `rankable_cross_section_count / eligible_cross_section_count` |
| valid 没有任何可排名截面 | 运行失败 `model_no_rankable_validation_cross_section`，不创建 `PredictionRun` |

“至少一个”只是能否产生排名的最低运行门槛，不代表样本量足以支持结论。ICIR / Rank ICIR 仍要求至少两个有定义观察且样本标准差非零；否则为 `unavailable`。若产品需要更严格的 `min_rankable_validation_cross_sections` 或比例门槛，它必须作为显式研究参数进入 Experiment 指纹，不能藏在统计函数中。

`sum(feature_importance_gain) == 0`、模型每棵树都只有一个叶子、整个 valid 段的 `score_range` 和方差只作为**辅助诊断字段**。它们通常能解释退化原因，但不能代替逐截面输出检查：模型可能有正 gain 和多个叶子，却只学到日期状态，使每个日期内部仍全部同分。

`best_iteration` **保留在 `TrainedModel` 中供审计**，但不再作为成功与否的判据。保留训练日志与 valid 曲线供诊断。

warnings 同时写入数据库摘要与 artifact manifest，页面与后续比较不得隐藏。典型 warnings 沿用 06，并新增：`insufficient_test_observations`、`high_test_missing_rate`、`zero_gain_features`、`some_validation_cross_sections_are_not_rankable`、`experimental_feature_set`。

### 11.3 取消

沿用 06 的语义（只终止本 run，不取消共享的数据包构建，临时产物删除），但 07 有一个 06 没有的问题：**LightGBM 的训练是一次长调用，中途没有天然的取消点。**

取消采用 **best-effort** 语义。取消接口成功仅表示 `cancel_requested` 已持久化，不表示 worker 已经观察到请求，也不保证运行最终一定进入 `cancelled`。worker 每 20 轮通过 LightGBM callback 检查一次；观察到请求时抛 `ResearchCancelled`。若训练、早停或后续发布在 worker 观察到请求之前完成，则运行允许进入 `succeeded`，已经完整发布的产物有效。

因此页面与 API 必须区分 `cancel_requested` 和终态 `cancelled`：前者只表示“取消请求已登记”，不得显示成“运行已取消”。已经进入终态的运行不再接受取消。这个取舍允许最多 20 轮的取消观察延迟，但避免为取消引入逐轮数据库查询。

**但这个 callback 无法从外部注入。** `LGBModel.fit()` 在内部固定构造 callbacks 列表，随后才展开 `**kwargs`（pyqlib 0.9.7，`gbdt.py:70-84`）：

```python
self.model = lgb.train(
    self.params, ds[0],
    num_boost_round=...,
    valid_sets=ds, valid_names=names,
    callbacks=[early_stopping_callback, verbose_eval_callback, evals_result_callback],
    **kwargs,                       # 调用方再传 callbacks → 重复关键字
)
```

调用方经 `fit(..., callbacks=[...])` 传入会得到 `TypeError: got multiple values for keyword argument 'callbacks'`。

因此在 Qlib seam 上提供一个受控子类 **`CancellableLGBModel`**：**合并** Qlib 原有的三个 callback（早停、日志、`record_evaluation`）与取消 callback，其余行为——参数、早停语义、`R` recorder 指标、`predict()`——与 `LGBModel` 完全一致。

#### 11.3.1 取消 callback 必须显式声明 `order = 25`

**LightGBM 不按列表顺序执行 callback，而是按 `order` 属性排序。** `engine.py` 先给没有 `order` 的 callback 补一个**负数**，再按 `order` 排序：

```python
for i, cb in enumerate(callbacks):
    cb.__dict__.setdefault("order", i - len(callbacks))     # 无 order → 负数
...
callbacks_after_iter = sorted(callbacks_after_iter_set, key=attrgetter("order"))
```

内置 callback 的 `order` 是固定的：

```text
_LogEvaluationCallback     order = 10
_RecordEvaluationCallback  order = 20
_EarlyStoppingCallback     order = 30
```

于是"把取消 callback 放在列表最后"**恰恰让它最先执行**（`order = -1`）。那一轮的指标还没被 `record_evaluation` 记下，取消就抛出了 `ResearchCancelled`——训练曲线会缺最后一行。

反过来设 `order > 30` 会让同一检查轮上的早停先触发，取消 callback 没有机会观察已经存在的请求。虽然 best-effort 语义允许尚未被观察的请求与成功终态竞争，但当 callback 已经到达约定的检查轮时，应让它完成检查，因此仍把它放在 early stopping 之前。

因此显式声明：

```python
cancel_callback.order = 25
cancel_callback.before_iteration = False
```

落在 `record_evaluation`(20) 之后、`early_stopping`(30) 之前。准确的表述是**"指标记录后、早停前执行"**，不是"排在最后"。

必须测到的五件事：

- 取消后运行进入 `cancelled`，临时产物删除，**不发布任何部分模型或部分 artifact**；
- 合并 callback 后**训练曲线不丢**（`training_curve` 的行数与 `best_iteration` 仍正确），且**被取消的那一轮指标已被记录**——这是 `order = 25` 而非负数的直接检验；
- 合并 callback 后 `R` recorder 的指标仍被记录（§7.2 的 recorder 作用域照常工作）；
- **取消检查轮恰好也是早停触发轮、且请求在该轮检查前已经持久化时，结果必须是 `cancelled`**——这是 `order = 25` 而非 `> 30` 的直接检验；若请求尚未到达下一个检查点而训练或早停已经完成，则按本节 best-effort 语义允许 `succeeded`；
- 未取消时，`CancellableLGBModel` 与原版 `LGBModel` 在同一数据同一参数下产出**逐位相同**的 `model.txt`——这是"只加了一个观察者、没改变训练"的唯一证明。

---

## 12. API 与页面

### 12.1 端点

| 端点 | 处理 |
|---|---|
| `POST /research/model-runs` | **新增**。snapshot、feature_set 名与版本、三段切分日期、白名单内的参数覆盖（§7.5.1）、顶层 `seed`（§7.5.2，`model_params` 内出现种子键即 400） |
| `GET /research/runs`、`GET /research/runs/{id}`、`POST /research/runs/{id}/cancel` | **复用**，响应体加 `kind` |
| `GET /research/model-runs/{id}/results` | **新增**。三段指标 + 对照曲线 + 特征重要性 + 缺失率 + 训练曲线 |
| `GET /research/runs/{id}/ranked-scores` | **复用，响应体与 06 逐字段同构** |
| `GET /research/feature-sets` | **新增**。只读注册表，返回展开定义及 `selectable` / `is_default` / `maturity` |

创建端点分开是因为参数差异是真实的；读取端点合并是因为消费者不该看见这个差异。

`POST /research/model-runs` **只接受注册表里存在且 `selectable = true` 的 `feature_set` 名称**，不接受任意表达式、类路径或 YAML。`alpha158_jp_v1` 与 `alpha360_jp_v1` 都必须被接受；选择 `maturity = experimental` 的 Alpha360 不需要额外确认，也不得静默改回 Alpha158，但运行记录、artifact manifest 和页面必须带 `experimental_feature_set` warning。

### 12.2 页面

新建 `ModelResearchCenter` 组件，与 `ResearchCenter` 平级挂在 `page.tsx`；共享的"运行列表 + 取消"抽成公用子组件。不引入路由——当前项目是单页堆叠组件的形态，改路由不属于这张票。

内容组织，重点在**防止约 16 个观察点被当成结论**：

1. 三段指标同屏，test 占主位，train / valid 次位并带"样本内，不构成结论"徽标；
2. 观察点数量紧贴指标，不放页脚；
3. LightGBM 与 6-1 动量并排两列，共享行标签；
4. 特征重要性默认 top 20 by gain，可展开全部；同屏给"gain 为 0 的特征数"；
5. 缺失率表默认只显示异常项（test 段缺失率比 train 段高 10 个百分点以上），可展开所选 FeatureSet 的全部列——158/360 行全部铺开没人看，筛出来的十几行才是信息；
6. 排名列表按 `rank_percentile` 排序、分页、不做 top-N 截断；分数显示为位次百分比，**不加百分号、不写"预期收益"**；
7. 训练曲线：valid loss vs iteration，标出 `best_iteration`；
8. 警告置顶不折叠。

**创建表单默认值**：最新可研究 DataSnapshot、`alpha158_jp_v1`、按 60/20/20 从有效观察区间推导出的**具体日期**（先扣 2 个 embargo 截面再分配，§6.1）、seed 固定默认值。

特征集选择器列出两个可训练集合：Alpha158 显示“baseline”并默认选中；Alpha360 显示“experimental”徽标和“360 个高度相关 lag、历史较短、结果仅供探索”的常驻说明。该说明不是禁用条件，用户选择 Alpha360 后提交必须创建真实运行。

`lgbm_jp_baseline_v1` 的参数按 §7.5.1 分两组呈现：**可覆盖项可编辑并显示允许范围，锁定项（`objective` / `deterministic` / `force_row_wise` / `num_threads`）只读并注明锁定原因**。页面与 API 的可写面必须一致——页面全部只读而 API 敞开，等于把一个未受约束的入口藏在 UI 后面。

**页面不得出现"模型优于动量"或任何胜负判定的措辞。** 16 个观察点上的差异几乎不可能达到统计显著；页面的职责是把数字和 `n` 摆出来，判断留给人。这是这个页面最容易犯、后果最大的错误。

---

## 13. 测试与完成条件

### 13.1 黄金数据集

#### 现状（先纠正一个错误前提）

本文早期版本写的"扩展 06 的切片"**不成立：06 没有黄金切片**。`backend/tests/test_momentum_research.py` 全部使用行内合成 DataFrame。仓库里唯一的真实数据 fixture 是 05 的 `backend/tests/data/stock_pool_golden.json.gz`：

- 行情：35,918 行 = 1562 只证券 × **23 个离散交易日**（2025-10-09 → 2026-05-22）；
- 字段：**只有** `symbol / date / adjusted_close / volume / turnover / quality_status`；
- 另有一个 485 项的 `calendar` 数组——**那是交易日历，不是行情**。

**两个维度都不够，而不只是字段不够。** 导出器只取 05 判定一个决策日所需的离散日期：`required_bar_offsets` 的两个端点（147、21）∪ 20 日流动性窗口 ∪ `AS_OF`，共 23 天，随后把行情查询限死在这些天上：

```python
dates = sorted(
    {history[len(history) - o] for o in DEFAULT_POLICY.required_bar_offsets}
    | set(calendar.window_back(AS_OF, DEFAULT_POLICY.liquidity_window_days))
    | {AS_OF}
)
resolved = snapshot_member_query(..., trade_dates=dates).subquery()
```

23 个离散日撑不起 Alpha158 的连续 rolling 窗口、147 日历史覆盖检查、12/4/4 周切分、两个 embargo 截面或尾部标签窗口。**不要把 `calendar` 的 485 当成行情覆盖**——本文早期版本正是这样误读的。

#### 做法

**保留 05 的 fixture 原样不动。** `backend/tests/data/stock_pool_golden.json.gz` 继续只服务 05 的全市场股票池黄金测试，其固定决策日、1562 只 Prime 普通股、810 只入池、各排除原因计数以及证券哨兵断言均不得因 07 改写。它验证的是“真实市场上的完整股票池规则”，缩成 150 只后即使同步修改 expected，也不再具有同等证明力。

07 新建独立的连续行情 fixture `backend/tests/data/model_research_golden.json.gz`，并使用独立导出入口 `.master-backfill/export_model_research_golden.py`。这次导出相对 05 同时改变两个维度：

- **字段加宽**：补齐 raw/adjusted OHLCV、`trading_value`、`adjustment_factor` 与行级质量原因；
- **日期连续化**：把 `dates` 从 23 个离散点换成一段**连续交易日区间**（约 262 天，见下）。

两份 fixture 的职责不同，不要求拥有相同证券域：

- 05 fixture 继续覆盖固定决策日的完整真实市场，保护 05 已独立核验的黄金答案；
- 07 fixture 覆盖约 150 只证券的连续时序，保护特征、切分、泄漏、训练、产物和复现机制；
- §9.3 的 6-1 动量与 LightGBM **都从同一份 07 fixture、同一个 ResearchUniverse、同一组标签和同一套统计代码产生**。跨票比较要求的是这两条分数曲线的数据相同，不要求它们与 05 的全市场黄金测试共用文件。

07 fixture 必须在 payload 中固定并校验 `source_data_snapshot_id`、导出器 schema/语义版本、导出参数、日期范围、有序证券列表及内容 checksum。重新导出必须显式评审差异，不能用当前实现输出直接覆盖黄金期望；这比强行把两个不同用途的切片塞进一个文件更直接地控制漂移。

#### 规模由日历反推，不拍脑袋

早期版本写的"约 30 只证券 × 约 200 个交易日、12/4/4 周"**在本设计下必然失败**，两处都不够：

- **证券数**：§9.2 沿用 06 的门槛——有效证券数 ≥ 100 且覆盖率 ≥ 90%。30 只证券的截面永远达不到 100，全部截面被排除在汇总之外，运行最终触发 `no_valid_test_cross_section`（§11.2）而失败。
- **跨度**：200 个交易日扣掉 147 日预热只剩约 53 天 ≈ 10.6 周，不足 20 周；而且这还没算两个 embargo 截面和尾部标签窗口。

正确的反推：

```text
147                       预热（§4.3）
+ 12 + 1 + 4 + 1 + 4 = 22 个周度截面（train / embargo / valid / embargo / test）
+ 1                       尾部：最后一个 test 截面的标签要等下一个周度截面之后
≈ 23 周 ≈ 115 个交易日
────────────────────────
≈ 262 个交易日
```

证券数取 **约 150 只**：要保证每个计入汇总的截面有 ≥100 只有效证券且覆盖率 ≥90%，必须为退市、停牌、流动性不足和历史不足留出余量。

**新增的 07 fixture 不会因此变臃肿**：150 × 262 ≈ 39,300 行，与 05 fixture 当前的 35,918 行相当。换来的是 23 个离散点变成 262 个连续交易日——新增文件的行数仍可控，但它第一次能支撑连续 rolling 特征与完整的三段切分。

CI 必须分别运行两组黄金测试：05 原有七条真实市场断言全部保持不变；07 端到端测试使用新 fixture，并在生产门槛 `min_valid_securities >= 100` 下跑通。不能用 07 的 150 只子集重新定义 05 的 `EXPECTED_PRIME_COMMON`、`EXPECTED_POOL_SIZE` 或排除原因计数。

#### 关于降低门槛

若某条测试为了缩小 fixture 而声明更低的 `min_valid_securities`（它本就在 Experiment 定义与指纹内，§2.1），必须同时满足两点：

- 明确标注它**不能替代**生产门槛的端到端验收；
- 另有一条测试在 ≥100 只证券的真实门槛下跑通完整链路。

否则黄金测试会在一个永远达不到生产门槛的世界里"全绿"。

#### 07 切片不用来证明模型有效

它训不出有意义的模型，也不该期待它训得出。**黄金测试验证的是机制，不是效果**：切分对不对、泄漏有没有、产物完不完整、结果可不可复现。

### 13.2 可复现性承诺

| 场景 | 承诺 |
|---|---|
| 同容器、同机器、相同 `num_threads` 重跑 | **逐位一致**：`model.txt` 的 sha256 相同，预测分数逐位相同 |
| 不同 `num_threads`（1 vs 4） | 树结构、预测分数与研究指标相同；原始 `model.txt` 含线程元数据，文件 sha256 不承诺相同 |
| 跨 CPU 架构 / 不同 LightGBM 版本 | **不承诺**（与 06「不要求 float 逐字节一致」一脉相承） |

指标类断言用明确相对容差（1e-6），不做逐位比较。

普通 CI 对 1 / 4 线程各训练一次：预测数组逐位比较；对 `Booster.dump_model()` 中决定预测语义的 feature name、tree structure、split、threshold、leaf value 做规范化后比较；研究指标按上述容差比较。测试**不得**断言两份原始 `model.txt` checksum 相同。另有同线程重复训练测试，单独断言原始模型 checksum 相同。

### 13.3 泄漏测试

- **随机打散**：输入行顺序被打乱时切分函数仍产出相同三段；三段日期集合两两不相交且严格递增；
- **标签越界（两个接缝都要测）**：
  - `label_exit(train 最后一个截面) < feature_cutoff(valid 第一个截面)`；
  - `label_exit(valid 最后一个截面) < feature_cutoff(test 第一个截面)`。

  第二条不能省。valid 的标签若伸进 test 段，会经由**早停**选出的 `best_iteration` 影响最终模型——这条路径比第一条更隐蔽，因为它不经过训练损失；
- **预处理泄漏**：§7.4 的 `ZScoreNorm` spy；
- **特征端时间隔离**：构建 train 段特征矩阵时，从 bundle 实际读到的最大日期 ≤ `train_end`，从 Qlib handler 的**真实读取行为**验证而非读代码确认；
- **哨兵测试（标签与特征各一条，不能合并）**：
  - 只把 test 段的**标签**改成哨兵值重跑，断言 train / valid 指标与 `model.txt` 的 checksum 一个字节都没变；
  - 只把 test 段的**特征**大幅改动重跑，断言同上。

  必须拆成两条：改标签只能证明 test 标签没流进训练，证明不了 test 特征没被预处理或训练读取——而无状态 processor 之外，任何一处越界的 `prepare()` 调用都会走特征这条路。

  相应地，**不要把哨兵测试说成"任何一条泄漏路径都会让它失败"**。它的准确表述是：任何 test **标签或特征**进入训练的路径都会让它失败。别的泄漏形态（例如股票池用了未来信息）由各自的测试负责。

### 13.4 CI 分层

**普通 CI**（无网络、无 bundle）：切分、指标、指纹、状态机、API 契约、前端、**以及全部确定性断言**。

逐截面可排名性至少覆盖：

- 每周内部为常数、不同周取不同常数：整个 valid 方差大于零，但 `rankable_cross_section_count == 0`，运行失败 `model_no_rankable_validation_cross_section`；
- valid 只有部分周为常数：运行成功，常数周为 `unavailable`，warning 存在，汇总 `n` 只计算其余周；
- 分数差异不超过 float64 精度容差：视为常数；明显超过容差：视为可排名；
- LightGBM 单叶树给出 `best_iteration == 1` 且预测全同：仍由逐截面输出判据识别并失败；
- 模型有正 gain 或多个叶子，但构造出的各日期内部预测全同：结构诊断不能放行，仍因没有可排名 valid 截面而失败。

FeatureSet 交付至少覆盖：

- Alpha360 生成器产出严格有序的 360 列：CLOSE/OPEN/HIGH/LOW/VWAP/VOLUME 各 60 个 lag，并校验首尾表达式、`required_fields` 与最大窗口；
- `GET /research/feature-sets` 同时返回 Alpha158 与 Alpha360 为 `selectable = true`，且恰好 Alpha158 为 `is_default = true`；
- 页面默认选择 Alpha158，Alpha360 带 `maturity = experimental` 提示但可以提交；
- `POST /research/model-runs` 接受 `alpha360_jp_v1`，不返回 `feature_set_disabled`，也不把它静默替换为 Alpha158；
- Alpha360 运行记录和 manifest 带 `experimental_feature_set` warning，并保存完整展开定义。

其中指纹一项须包含一组**语义变更检测**测试：

- 改动 processor 列表（换 `CSRankNorm` 为 `CSZScoreNorm`、删掉 `InfToNaN`）产生不同指纹；
- **改动自研 processor 的 `semantics_version` 产生不同指纹**——这是 §2.1 那个手动版本号唯一的保护；
- 改动 `fit_window` 取值产生不同指纹；
- 改动所选 FeatureSet 的任一表达式产生不同指纹，改动**别的** FeatureSet 则指纹不变（§4.1）；
- 仅重排 JSON key **不**改变指纹（规范化必须先于哈希）；
- artifact manifest 中的 processor 与 FeatureSet 身份，与 Experiment 指纹所用的完全一致。

这些是 §2.1「原样进指纹」与 §4.1「展开定义进指纹」的唯一检验——否则它们会在某次重构里被悄悄换成一个手动版本号而无人察觉。

可信执行规范至少覆盖：

- 新建 Experiment 时，落库定义、指纹 payload 与 `TrustedExecutionSpec` 的规范化定义完全相同；
- 修改当前 FeatureSet 表达式或 processor 后重跑旧 Experiment，`resolve_experiment` 在任何 Qlib 初始化之前失败 `experiment_execution_definition_unavailable`；
- 代码内保留一个能产生旧完整定义的可信 adapter 时，旧 Experiment 可以重放；只匹配名称或版本、完整定义不同不能放行；
- 数据库中的表达式、类名和 kwargs 放入恶意哨兵，断言它们从未流入 `init_instance_by_config` 或表达式求值；
- `TrainedModel.inference_contract_checksum`、artifact manifest 和可信 adapter 产生的规范化契约三者完全相同；找不到匹配 adapter 时加载模型失败 `model_inference_contract_unavailable`。

发布协议用故障注入覆盖每一个提交接缝：

- 临时目录完成后、`prepared` 日志提交前崩溃：无领域记录，scavenger 只清理无日志引用的过期临时目录；
- `prepared` 提交后、rename 前崩溃：恢复器执行 rename 并 finalize；
- rename 后、finalize 事务前崩溃：恢复器校验 final checksum 后 finalize；
- finalize commit 后响应丢失并重试：返回既有结果，不产生第二条 `ResearchArtifact`、`TrainedModel` 或 `PredictionRun`；
- final 路径已有不同 checksum：失败 `artifact_path_conflict`，未知内容不被覆盖或删除；
- 任一 model 恢复成功后，`ResearchArtifact`、`TrainedModel`、`PredictionRun`、`ResearchRun.succeeded` 与 `Task.succeeded` 在同一事务中同时可见；factor 恢复则断言 `ResearchArtifact` 与两个成功终态同时可见。

`lightgbm` 已在 `uv.lock` 中（pyqlib 传递依赖），用合成的小 DataFrame 直接喂 `LGBModel` 即可测确定性，不需要 bundle 也不需要网络。

**容器内 Qlib 集成测试**：真实 bundle 上的 `$vwap` 语义、Alpha158 表达式求值、`DataHandlerLP` 的 fit 窗口行为、`model.txt` 存取往返、`SignalRecord` 生成 `pred.pkl` / `label.pkl` 后 `SigAnaRecord` 确实生成 IC/Rank IC（缺少父记录时测试必须失败而非把 skip 当成功）、Qlib 与本地 test 指标在相同有效行上一致、schema v2 数据包重建、**v1 与 v2 上 06 动量结果一致**（§3.3）。

Alpha360 不能只停在注册表单元测试：同一份 07 fixture 上必须至少有一条资源受限但完整的 smoke/determinism 路径，实际完成 360 列 Qlib 求值、LightGBM train/valid 训练与早停、test `PredictionRun`、`model.txt` 保存/加载往返、逐列逐段缺失率及异常计数发布，并验证固定输入和种子下结果可复现。这条测试证明“用户可以选择并运行”，不以 IC 高低证明 Alpha360 有效或优于 Alpha158。

把确定性测试放进**普通 CI 而非集成测试**是刻意的：确定性是最容易被一次无心的参数改动（比如有人删掉 `force_row_wise`）悄悄破坏的性质，它必须待在跑得最勤、反馈最快的那一层里。

---

## 14. 被否决方案

| 方案 | 否决原因 |
|---|---|
| 用 `$trading_value/$volume` 表达式合成 VWAP | "特征需要哪些字段"无法在数据包层面校验，与第一条验收要求相反 |
| 把 VWAP 从 Alpha158 剔除以避免升 schema | J-Quants 真实提供成交额，VWAP 是可得事实；08/10 也需要它 |
| 用日度 5 日标签训练 | 标签 4/5 重叠，等效样本量远低于名义值；训练与评价口径分叉 |
| 用 Qlib 默认收盘到收盘标签 | 与"下一交易日开盘成交"的系统假设不符 |
| 标签用 `CSZScoreNorm` | 约 85 个截面上，日股厚尾在 z-score 后仍是极端值，会主导训练损失 |
| lambdarank 目标 | `LGBModel` 不支持，需自写模型类 + 分组 + 离散化；本规模下无可靠优势 |
| 注册 Alpha360 但禁止用户运行 | ticket 要求用户可选择 Alpha158/Alpha360；正确的风险控制是 Alpha360 可选择、非默认、实验性并带完整警告与验收，不是把功能推给未来票 |
| 直接使用 Qlib 自带的 `ProcessInf` | 它的实现是逐截面均值填充（源码自带 `FIXME: Such behavior is very weird`），与决定 3「不填充」直接冲突 |
| 按名字判断 processor 行为、只测类名 | `ProcessInf` 这个 bug 正是这样漏进设计的；必须直接断言输入输出 |
| embargo 定义为"两段间距 ≥ 标签窗口" | 差一个周度截面：`label_exit(train 末截面)` 仍落在 valid 首截面之后 |
| 用自然日（加 7 天）计算 embargo | 节假日周的实际交易日间隔会缩短，自然日近似给出错误答案 |
| 对特征做 `RobustZScoreNorm` | 树对单调变换不变，不改变任何结果却新增一层状态 |
| 用 `Fillna(0)` 填充缺失特征 | 未做特征归一化时是胡填；做了之后把停牌股伪装成"完全平均的股票" |
| 为让"预处理只拟合训练区间"有检查对象而硬加特征归一化 | 本末倒置：引入不改变输出的状态层，收益为零 |
| 不填 `fit_start_time` / `fit_end_time`（"反正没人用"） | 将来加了有拟合状态的 processor，它会默默拿全区间估参数且不报错 |
| 让用户配置 processor 列表 | `init_instance_by_config` 按字符串 import 任意模块，等于任意代码加载 |
| 用手动维护的 `pipeline_version` 代替 processor 列表进指纹 | 版本号靠人记得改，而改 processor 的人正是最容易忘记的那个 |
| 重跑时直接执行 Experiment 数据库中的表达式或类名 | 数据库事实是身份与审计记录，不是可信代码；会绕过 `init_instance_by_config` 防线 |
| 重跑旧 Experiment 时只按 FeatureSet 名称/版本选择当前 adapter | 同名实现可能已经改变，会出现旧指纹、新语义；必须比较完整规范化定义 |
| 沿用 Qlib 官方基准超参 | 在 CSI300 十余年数据上调出，搬到约 85 个截面会严重过拟合 |
| 把 `num_threads` 放进 Experiment 指纹 | `deterministic + force_row_wise` 下树结构、预测与指标跨线程一致；原始 `model.txt` 仅因线程元数据不同而允许 checksum 不同。线程数是运行身份，不是研究语义，放进指纹会让调资源变成新实验 |
| 把 `best_iteration` 放进指纹 | 它是训练结果不是研究定义 |
| 用 pickle 保存模型 | 跨版本不可读，与"产物与版本身份绑定且可复现"矛盾 |
| 让票 12 穿透 Run / Experiment / manifest 拼装模型输入规则 | 推理 interface 分散且容易漏字段；`TrainedModel.inference_contract` 应自包含，并由可信 adapter 校验后执行 |
| 保留 mlruns 目录并展示 | 会让页面与后续票依赖 Qlib 内部布局（06 已否决过同类做法） |
| 直接调用 `SigAnaRecord`，不先生成 `SignalRecord` | pyqlib 0.9.7 缺少 `pred.pkl` / `label.pkl` 时会跳过分析；无异常不代表 recorder 链成功 |
| 发布原始特征矩阵 | 体积涨两个数量级，换来省一次可完全重算的计算 |
| 把 `os.replace` 当成文件与数据库的整体原子发布 | rename 后、数据库 commit 前崩溃会留下孤儿 final 目录；必须用 prepared publication 日志恢复并幂等 finalize |
| `TrainedModel` 与 `ResearchRun` 合并 | 12 需要在新快照上复用已训模型，合并后无法表达 |
| `ranked-scores` 对两种 run 返回不同 payload | `RankedScores` 的全部意义就是让 08 不必关心分数来源 |
| 只用 `best_iteration == 0` 判定常数模型 | 单叶子树给出 `best_iteration = 1`，旧判据放行；`min_data_in_leaf` 放宽即可触发 |
| 用整个 valid 段的方差判定常数模型 | 不同日期可以取不同常数而使整段方差大于零，但每个日期内部仍完全不可排名；必须逐 `prediction_date` 判断 |
| 常数模型记为成功 | 下游会拿到语法合法、语义空洞的模型，无从分辨 |
| 靠"放在 callbacks 列表最后"决定取消 callback 的执行时机 | LightGBM 按 `order` 排序，无 `order` 会被补成负数，反而最先执行 |
| 给取消 callback 设 `order > 30` | 在约定的取消检查轮上，早停仍会抢先抛出停止，使 callback 无法观察已经持久化的取消请求；best-effort 允许检查点到达前的竞争，不应跳过已经到达的检查点 |
| 训练阶段不设取消检查点 | 用户点取消将完全无反应直到训练自然结束 |
| 经 `LGBModel.fit(**kwargs)` 注入取消 callback | `fit()` 内部已固定传 `callbacks`，重复关键字直接 `TypeError` |
| 把 `raw_score` 解释为百分位 | `CSRankNorm` 的目标范围是 `[-1.73, 1.73]`，且回归输出未经校准 |
| 用特征集名称+版本或注册表总版本作研究身份 | 改表达式忘升版会错误复用实验；改无关特征集会凭空改变本实验身份 |
| `PredictionRun` 带 `status` 字段 | 与"不可变"自相矛盾；执行生命周期属于 `ResearchRun` |
| 30 只证券 × 200 交易日的黄金切片 | 达不到 100 只有效证券门槛，且扣除预热后周数不足，运行必然失败 |
| 为缩小 fixture 而全面下调 `min_valid_securities` | 会让黄金测试在一个永远达不到生产门槛的世界里全绿 |
| 只给黄金 fixture 加字段、沿用现有 23 个离散日期 | 离散日撑不起连续 rolling 特征、147 日覆盖检查与三段切分；必须重新导出连续区间 |
| 把 fixture 的 `calendar` 长度当作行情覆盖 | `calendar` 是交易日历，`bars` 才是行情；本文早期版本正是这样误读的 |
| `model_params` 接受任意字典 | 可覆盖 `deterministic` / `force_row_wise`，直接废掉 §13.2 的可复现性承诺 |
| 越界参数静默裁剪到边界 | 用户以为跑的是他填的参数，而指纹记的是另一套 |
| 页面参数只读但 API 敞开 | 等于把一个未受约束的入口藏在 UI 后面 |
| `seed` 同时作为顶层字段和白名单参数 | 需要一条只在冲突时生效的隐式优先级规则，最易在实现与文档之间漂移 |
| 自研 processor 只用类名+kwargs 作身份 | 改实现不改类名，指纹不变，两种语义复用同一实验 |
| 用应用代码版本充当 processor 语义身份 | 每次提交都变，会让每个 commit 产生一批新实验 |
| 只测第一个 embargo 接缝 | valid 标签伸进 test 会经由早停影响模型，且不经过训练损失，更隐蔽 |
| 用单条哨兵测试覆盖全部泄漏 | 改 test 标签证明不了 test 特征没被读；标签与特征必须各测一条 |
| 页面给出"模型优于动量"的结论 | 约 16 个观察点上的差异几乎不可能显著 |
| 用 07 的 150 只连续切片覆盖 05 fixture，或同步缩小 05 的 expected | 会丢失 05 对完整真实市场股票池的独立黄金证明；05 fixture 保持不变，06 动量与 07 LightGBM 改为共用新增的 07 fixture |
| 确定性测试只放容器内集成测试 | 它最容易被无心改动破坏，必须待在反馈最快的一层 |
