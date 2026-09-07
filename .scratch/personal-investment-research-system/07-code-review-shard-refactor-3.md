# 代码审查（两轴，第三轮）：分片重构 + 两轮修复后

- 审查日期：2026-09-07
- 固定点：`HEAD`（`02e05400fb80608644f85d7dafc9dec50d0c4f26`）；对象：**工作树**，未提交
- 规模：24 文件、+1572 / −1173，外加 9 个新增未跟踪文件
- 本轮指令：**把修复本身当作审查对象**，代码注释与文档断言一律视为待验证的主张

---

## Standards

### 硬性违规

1. `docs/design/qlib-lightgbm-direct-run.md:397` 写「训练由 `app/experiments/lgbm_seam.py` 承担」——该文件不存在，模块已改名 `booster_training.py`。**本轮新增的文档—代码矛盾**。
2. `docs/experiments/validation.md:11,13` 本次改过，却仍只列两个测试文件与「29 passed」，漏掉新增的 `test_booster_training.py`、`test_feature_shard_cache.py`。

### 上一轮「修复」的表面功夫

3. `feature_shard_cache.py:306-307` 的 schema 校验**不可达**：`FEATURE_CACHE_SCHEMA_VERSION` 已在 `cache_identity:261` 内，schema 变则换目录，297 行已验证。看似防线实为装饰。且 305 行 `read_json` 落在 295-304 的 try 之外，与 298-302「不让错误逃逸成未捕获异常」的注释自相矛盾。
4. `require_days:119` 只是约定，不是不变式：`load_segment:140` 不校验，全仓仅 `qlib_lightgbm_direct.py:226` 调一次，任何新调用点漏调即绕过。修法：把所需日期传进 `assemble_sharded_dataset`，让未校验的 reader 无法存在。
5. `booster_training.py:60` 的 `feature_label_parts` 全仓无调用者。第二轮已按 `lgbm_seam.py:60` 报过，改名搬家却没删（Speculative Generality）。

### 基线气味

6. **Feature Envy / 依赖倒置**：`feature_shard_cache.py:37` 反向 import `booster_training.TrainingMatrix`。把 `as_training_matrix:87` 搬到 `run_fold`。
7. **Duplicated Code + 不实断言**：`search_runner.py:55-62` 另建一份 `DirectPredictionConfig`，且 `label_horizon=config["purge_horizon"]`，与 `direct_config:142,147` 的映射不同；而 docstring:6-7、113-127 宣称只有一个编译器。
8. **反射渲染缺穷尽检查**：`search_report.py:111-118` 的 else 原样透出，配合 `write_json(default=str)` 与手工维护的 `PREDICT_PARSERS`、手写 flag 列表，新增 date/tuple 字段会静默串化。补 else raise。
9. **测试耦合**：`test_booster_training.py:25` 从 `tests.test_experiment_search` 借 `config`，再跑 `trial_plan` 只为取 `resolved_params`（50-57）；同文件 79/87/95 却直接用字面量。改用 `resolve_model_params`。
10. `Invoke-FrozenCandidate.ps1:153-158` 全部 seed 失败仍退出 0，与 `Invoke-DbMigration.ps1:71,75` 的 `exit $LASTEXITCODE` 约定相反；72/80 未用 `-LiteralPath`。
11. `qlib_lightgbm_direct.py:555` 返回循环泄漏的 `pool.policy_fingerprint`：缓存身份取决于最后一天的池，`days` 为空则 NameError。

ADR-0002 与 0001 不冲突（显式重申 0001），格式一致；`CONTEXT.md` 的 `FeatureShardCache` 词条与 `cache_identity` 一致。

---

## Spec

### 严重

1. **缓存完整性校验是自指的。** `design.md:352` 称「把实际写出的每一个交易日记入 `covered.json`…封存前校验它与股票池所声明的日期集合完全一致」——只到「日」为止：`feature_shard_cache.py:397-404` 比的是**日期集合**，而 labels 由同一份 restricted features 派生，故某日 Qlib 少返回一批证券时特征与标签同时缺失，`load_segment` 的逐行校验（:187-197）无从发现。test 日尚有 `factor_coverage>=0.90` 兜底，train/valid 无任何门槛——用 30% 股票池训练依旧静默通过。

2. **`require_days` 结构上不可能失败。** universe 由 `_span_dates`（`qlib_lightgbm_direct.py:513`）按 `train.start..test.end` 全量构建，:226 传入的 fold 日期可证恒为其子集，identity 又已锁 span。`design.md:352` 与 `ADR-0002:16` 称其为拦截「fold 几何丢中间日期」的防线，**不成立**；`test_feature_shard_cache.py:333` 也只能手工篡改 `ShardedDataset` 才触发。

### 中等

3. `design.md:527`「损坏或身份不符的缓存以数据错误退出（2），不是未捕获异常」只做一半：仅 root 级 `verified` 被转为 `FeatureCacheError`（:296-302），分片级 `verified`（:340）在 try 外，checksum 不符时 ValueError 直穿，CLI 落到 `except Exception` 返回 1。且无测试。
4. `design.md:497`「predict 对特征缓存目录持有排他锁，因此同一数据身份的两次 predict 会串行」不实：锁仅覆盖 assemble（:295），第二个进程以 `FileExistsError` 崩溃而非串行。
5. `design.md:533` 要求候选 JSON「由 `direct_config` 渲染」，但 `configs/experiments/selected_parameters-direct-run-0b61155b….json` 缺 `feature_batch_size` / `qlib_kernels` / `scan_batch_rows`，顶层键也不符 `write_json` 的 sort_keys——并非当前渲染器产物。

### 轻微

6. `design.md:397` 写 `lgbm_seam.py`（同 Standards 1）；`validation.md:13` 仍称「29 passed」（现 37）。
7. Scope creep：`Invoke-FrozenCandidate.ps1` 与 `.expected.json` 无 spec 出处，其 `-TestRange` 可改评价窗口，与 search-prompt 阶段 7「三项对账」矛盾。
8. ±inf 只注入特征列（`test_feature_shard_cache.py:59-63`），`$close` / 标签路径未构造 inf，而 inf 标签会被 `evaluation.py:86` 的 `notna()` 计为有效。

**未见破坏**：`purge_horizon` 链路、07 产品路径（`day_provider` 仅改 smoke read 采样；golden 新增测试只读）。

---

## 独立复现

**Spec 2 成立 —— 我加的防线是空的。** 四种 fold 几何下，fold 日期恒为 span 子集：

```
variant          span days fold days  fold ⊄ span ?
expanding              190       190  never
window=60              190       150  never
purge=20               190       187  never
step=5                 190       190  never
```

`require_days` 在生产中永远不会触发。真正起作用的是「股票池按整跨度构建」这个设计，而不是这道检查。它作为不变式的可执行断言仍有价值，但文档把它说成拦截活跃失效模式，是错的。

**Spec 1 成立 —— 逐日「存在」不等于逐日「完整」。** 构造某天 Qlib 只返回 10 只中的 3 只：

```
SEALED WITHOUT COMPLAINT
require_days PASSED
universe declares 10 members every day; cache holds per day:
2020-01-07     3      <- 其余各日均为 10
```

封存自检与 `require_days` 全部通过，train/valid 会静默用 30% 的股票池训练。

---

## 汇总

- **Standards**：硬性违规 2 项、表面功夫 3 项、气味 6 类，共 11 项；本轴最严重是 `design.md:397` 指向已不存在的模块——本轮新引入的文档矛盾。
- **Spec**：严重 2 项、中等 3 项、轻微 3 项，共 8 项；本轴最严重是封存自检只比日期集合，某日证券大量缺失仍静默通过（已复现）。

两轴不做跨轴排序。
