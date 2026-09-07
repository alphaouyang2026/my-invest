# 代码审查（两轴）：分片重构（工作树，未提交）

- 审查日期：2026-09-07
- 固定点：`HEAD`（`02e05400fb80608644f85d7dafc9dec50d0c4f26`）
- 审查对象：**工作树**，改动未提交
- 差异命令：`git diff HEAD`（两点）
- 规模：11 文件、+2003 / −1062，外加 4 个 `git diff` 不可见的新增未跟踪文件：
  `artifact_cache.py`、`lgbm_seam.py`、`Run-FrozenCandidate.ps1`、`selected_parameters-direct-run-*.json`

改动意图：把 `search_runner.py` 中重复实现的特征装配、折叠计划、训练循环、评价收拢进 direct 入口；
把按证券分片的特征缓存搬进 direct 以解决 OOM；新增 `purge_horizon` 让多个 label horizon 共享评价日。

两个子代理并行执行、互不知晓对方结论。两轴分别呈现，不做跨轴合并或重排。

---

## Standards

### 硬性违规（仓库文档化标准）

1. `docs/design/qlib-lightgbm-direct-run.md:527` 验收项仍引用本次已删除的 `fit_model` / direct `_train`；`:529` 称「trial horizon 与 purge_horizon 不一致时拒绝导出」，而 `search_report.py:99` 实际只校验 `train_window`。文档与代码相反，按现状重写。
2. 同文件新增的 `9.4`（`:326`）插在 `9.3`（`:360`）之前，编号乱序。
3. `docs/agents/domain.md` 要求输出只用 `CONTEXT.md` 的词汇：分片特征缓存（`qlib_lightgbm_direct.py:562`）是一类新的、长期存活的派生磁盘产物，词汇表无对应词条，ADR-0001 只覆盖 QlibDataBundle。补词条或补 ADR。

### 判断项（气味基线）

4. **Divergent Change** — `qlib_lightgbm_direct.py` 已 980 行，几何、缓存、训练、评价四类原因都改它。修法：把 `:529-805`（`_cache_identity` / `assemble_sharded_dataset` / `_write_shards` / `ShardedDataset` / `_parquet`）抽成 `feature_shard_cache.py`。
5. **Duplicated Code** — `scripts/qlib_lightgbm_direct.py:21-33,104-108` 手抄了 `DirectPredictionConfig:131-142` 的字段名与默认值，加一个字段要改四处。修法：用 `dataclasses.fields` 派生，只留 `PREDICT_PARSERS`。
6. **Duplicated Code** — 原子写 parquet 有两份（`search_runner.py:93`、`qlib_lightgbm_direct.py:801`）。修法：并入 `artifact_cache.py`。
7. **Duplicated Code** — `search_report.py:86` 与 `search_runner.py:120` 是两套「fold plan → 运行配置」编译器。修法：合一。
8. **Middle Man** — `search_plan.py:10-17` 纯转发 `artifact_cache`；`tests/test_qlib_lightgbm_direct.py:783,829` 仍经 search 取 `write_json`，正是 `artifact_cache.py:8-9` 声称要断开的耦合。修法：直接导入、删转发。
9. `_cache_identity:663` 自称「缓存只是数据的函数」，却把 `feature_batch_size`/`qlib_kernels` 纳入身份（`:678`），纯性能旋钮一动即全量重建。修法：移出。
10. **Speculative Generality** — `search_runner.py:221` 的 `workers` 只为被拒绝而存在、`:158` 的 `num_threads` 未使用；`run_fold` 的 `segment=`（`:819`）、`ShardedDataset.universe_sizes`（`:577`）、`_validate_dates`（`:488`）均无生产调用方。修法：删或内联。
11. **Mysterious Name** — `load_segment`（`:580-638`）名为装载，实则兼做缓存完整性审计，且 `learning: bool` 同时切换列名与丢弃行为。修法：拆出校验、改显式段角色。另：`__all__`（`:967`）与模块 docstring「三个公开函数」（`:3`）已不覆盖真实公开面。
12. `Run-FrozenCandidate.ps1` 结构与 `Invoke-DbMigration.ps1` 一致（comment-based help、`CmdletBinding`、`Stop`、`Push/Pop-Location`），但 `:11` 自称「没有手抄的数字」，`:61-65` 与 `:169` 的期望 Rank IC 恰恰是手抄的；`:145` 输出名硬编码 `0b61155b`，而 `$Config` 是参数。修法：期望值从配置读，或删掉该自述。

---

## Spec

### 严重

1. **分片缓存身份漏掉决定内容的输入。**
   spec `docs/design/qlib-lightgbm-direct-run.md:331-334` 只列到「日期跨度」，实现 `qlib_lightgbm_direct.py:669-680` 照抄成 `[train.start, test.end]`。但缓存内容是按 universe 过滤后写入的（同文件 `:759`），而 universe 由 fold 日期集合决定（`:196`、`:477-478`），受 `purge_horizon` / `train_window` / `rolling_step` / `valid` 影响。
   `search_runner.py:150-151` 让同一 `(feature_set, horizon)` 组内 `train_window` 在 expanding/252 间变化却共用同一缓存目录；`load_segment`（`:585-638`）以 labels.parquet 为准，缺失日期不报错 → **训练集被静默截断**。
   `shard-plan` 一致性校验（`:735`）只在缓存未封存时执行，封存后完全不设防。

### 中等

2. 新增验收与实现相反：`:529`「trial 的 horizon 与计划 purge_horizon 不一致…拒绝导出」，而 `search_report.py:86-116` 与测试 `test_qlib_lightgbm_direct.py:823` 恰恰刻意允许。
3. `:527` 要求对比「搜索入口 `fit_model` 与 direct 入口 `_train`」，两者本次改动后均不存在（`_train` 仅存于产品路径 `app/research/model_workflow.py:217`），该验收空转——属于为迁就实现改写验收。
4. `workers` 能力移除未同步文档：`search_runner.py:221-231` 硬拒 `workers≠1`，但 `docs/experiments/README.md:52`、`docs/experiments/qlib-oom-mitigation-plan.md:183` 仍称「资源验收后再评估并发」，`docs/experiments/validation.md:13` 仍声称测试覆盖「双 worker 一致性」（该测试已删）。
5. float32（`:313`）：HEAD 的 direct 走 float64 DatasetH，`scripts/run_qlib_baseline.py:27-44` 以 `atol=1e-9` 比对分数并要求 `folds`/`best_iterations` 全等，旧基线参照大概率失配。产品路径 `app/research/dataset.py` 未动，07 验收 #24 逐位复现不受影响。

### 轻微

6. `:523`「逐值相同」实测为 `rtol=1e-6`（`test_qlib_lightgbm_direct.py:462-468`）。
7. Scope creep：`scripts/Run-FrozenCandidate.ps1`（178 行、内嵌期望 Rank IC）与 `configs/experiments/selected_parameters-direct-run-*.json` 无 spec 依据；后者还缺 `purge_horizon`，是旧版产物。

### 核查通过

CSRankNorm 公式与 Qlib 一致；「先归一后 dropna」与 Qlib「先 dropna 后归一」实测逐值相同；InfToNaN 用项目自有 processor 且参照管线测试存在；前瞻标签由未过滤 `$close` 计算（`:772-777`），池外未来价可取；`purge_horizon >= label_horizon` 校验正确（`:348-356`）；成熟边界用 `snapshot.coverage_end`；产品路径不变量未被破坏。

---

## 对 Spec 严重项的独立复现

在报告之上另行构造了最小复现，确认该项成立：

```
identity equal?           True
expanding universe days   190  2025-01-01 -> 2025-09-23
windowed(60) universe     150  2025-02-26 -> 2025-09-23
expanding 需要而 windowed 缓存没有的日期：40 天

purge_horizon=20 identity 与默认相同：True
purge=20 universe 187 天 | 默认需要而它缺的日期：3 天
```

即：`[train.start, test.end]` 相同但 `train_window` 或 `purge_horizon` 不同的两次运行，共用同一缓存目录，
而后跑的那次会静默拿到不完整的训练集。这正是 `structure_search.json` 的 `train_windows: ["expanding", 252]`
在同一 `(feature_set, horizon)` 组内会遇到的情形。

---

## 汇总

- **Standards**：硬性违规 3 项、判断项 9 项，共 12 项；本轴最严重是三处文档与代码直接矛盾（`qlib-lightgbm-direct-run.md:527,529`）。
- **Spec**：严重 1 项、中等 4 项、轻微 2 项，共 7 项；本轴最严重是分片缓存身份漏掉 `train_window` / `purge_horizon` / `rolling_step` / `valid`（`qlib_lightgbm_direct.py:669-680`），已独立复现，会导致训练集静默截断。

两轴不做跨轴排序 —— 分离正是为了避免一轴掩盖另一轴。
