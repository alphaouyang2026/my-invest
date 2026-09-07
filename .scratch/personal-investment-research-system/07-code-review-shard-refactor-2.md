# 代码审查（两轴，第二轮）：分片重构 + 首轮修复后

- 审查日期：2026-09-07
- 固定点：`HEAD`（`02e05400fb80608644f85d7dafc9dec50d0c4f26`）
- 审查对象：**工作树**，改动未提交
- 差异命令：`git diff HEAD`；18 文件、+1529 / −1124，外加 7 个新增未跟踪文件
- 本轮明确要求子代理独立重判，并把代码中的注释与 docstring 当作**被审查的主张**而非事实

---

## Standards

### 硬性违规

1. `docs/design/qlib-lightgbm-direct-run.md:347` 的 digest 清单把 `feature_batch_size, qlib_kernels` 列入缓存身份，`:350` 又说「也不含」；代码 `feature_shard_cache.py:242-252` 依 `:350`。同一节自相矛盾，删 `:347` 那两项。
2. `CONTEXT.md:16-17` 新词条正文使用「Day Provider」，又把它列进 `_Avoid_`，且该词不在词汇表（对应词是 QlibDataBundle）。违反 `docs/agents/domain.md`「用 CONTEXT.md 的术语，勿漂移到 _Avoid_ 词条」。
3. ADR `0002` 首句称直接入口「不读 QlibDataBundle」，但 `feature_shard_cache.py:319` 正是从 day provider 读，而按 CONTEXT.md 定义 day provider 即 QlibDataBundle——与 ADR-0001 冲突却未按 domain.md 显式 flag。改措辞或加冲突说明。
4. `search_cli.py:19` 仍允许 `--workers 2`，`search_runner.py:231` 恒拒绝。删 choices。

### 判断项

- **Duplicated Code**：fold 几何两套实现（`search_plan.py:61-91` / `qlib_lightgbm_direct.py:407-502`），`direct_config` docstring 断言二者一致却无测试；`search_report.py:110-123` 手抄 14 字段，漏掉 `direct_config` 设的三个缓存字段，应改 `asdict` + 白名单；`DateSpan`（`feature_shard_cache.py:53`）与 `DateRange`（`:111`）同形。
- **Speculative Generality / 死代码**：`cache_schema_version`（`search_plan.py:20,34`）无人读取且与 `FEATURE_CACHE_SCHEMA_VERSION=1` 不符；`run_trial` / `_attempt` 的 `num_threads` 形参未用（`search_runner.py:159,197`）；`lgbm_seam.py:60` 的 `feature_label_parts` 无调用者；`qlib_lightgbm_direct.py` 四个未用 import（仓库无 ruff/CI，工具不会拦）。
- **Middle Man**：`search_runner.py:255` 未排序即 `groupby`，与直接遍历等价，注释所称「分组预热」不成立——先排序或删注释。
- **Mysterious Name**：`FeatureCacheSpec` docstring（`feature_shard_cache.py:61`）与测试名（`test_feature_shard_cache.py:341`）称「只含影响缓存内容的字段」，其中 3 个却被 `:365` 证明不影响身份；`load_and_validate_snapshot`（`qlib_lightgbm_direct.py:342`）实为全量配置校验。
- **撞名 / 测试错位**：新 `app/experiments/lgbm_seam.py` 与既有 `tests/test_lgbm_seam.py`（测的是 `app/research/lgbm.py`）撞名；新模块无自己的测试，用例寄居 `test_experiment_search.py:94`。
- **Data Clumps**：`_build_universe` 返 5 元组且读循环外泄的 `pool`（`:562`）；`_span_dates`（`:521`）造 `{"valid":(),"test":()}` 假字典凑签名。
- **PowerShell**：`Run-FrozenCandidate.ps1` 用非认可动词 Run（既有 `Invoke-DbMigration.ps1` 用 Invoke）；默认 config 的 `provider_root=/app/var/...` 是容器路径，`-Local` 必失败。

---

## Spec

**总判断**：文档改动总体是合理更新——§14 净增 9 条更严的验收，ADR 0002 如实记录了 float32 需重冻基线、并发不可恢复；`run_qlib_baseline.py` 亦照实写明失配。唯一迁就实现的是 §14:532。

### 严重

1. **`covered.json` 挡不住内部缺日期。** `load_segment` 只比端点（`feature_shard_cache.py:121-125`），写入的 `rows`（`:374-378`）从不校验；`written.all()`（`:176`）仅保证「有标签的行有特征」，整天缺失时目标行数为 0，不报错。会话日历有而 provider 没有的日期即静默读短——正是 §9.4:352「作为第二道防线…越界立即报错」要排除的失效模式。

### 中等

2. §14:527「fold 几何…**也不得改变缓存内容**」只测了字段名（`test_feature_shard_cache.py:341-351`），无任何测试改 `train_window` / `rolling_step` 后比对缓存行；上轮的静默截断只靠结构代理防守。代码本身成立：`_span_dates`（`qlib_lightgbm_direct.py:518`）与 fold 无关，代数核对 `fold_plan` 与 `_build_rolling_plan` 逐 fold 一致；`max_window` 已含入 `definition_checksum`，policy fingerprint 覆盖两个 history 字段。
3. `direct_run_config` 以 `window > min_train_days` 拒绝导出（`search_report.py:98`），而 `fold_plan` 已按 `max(min_train_days, *fixed)` 留够历史（`search_plan.py:74`）——跑成功的长窗口候选反被拒。§14:532 是照这段错代码补写的验收。
4. 等价性验收近乎空转：fixture 特征无 ±inf/NaN、两证券 `$close` 完全相同（`test_feature_shard_cache.py:42-51`），故 §14:525 点名的 `InfToNaN` 从未与参照管线比对，`CSRankNorm` 只比了一个全并列截面。

### 轻微

5. §13:497 称「不提供跨进程锁」，但 predict 现对 cache root 加锁（`feature_shard_cache.py:278`）；崩溃残留 `.running.lock` 使后续 predict 永久失败，设计未同步。
6. `verified()` 抛 `ValueError`（`artifact_cache.py:78`），逃过 `:247` 的 `FeatureCacheError` 捕获，缓存损坏时退出码为 1 而非 §4:160 的 2。
7. slow test 未断言 `label_not_mature` 尾日（§14:537 vs `test_model_research_golden.py:643`）。

**产品路径未误伤**：`bundle_builder` 走 `day_provider_export`，本次 `_validate_provider` 改动不涉及；golden 测试只增不改。搜索侧无训练代码、`purge_horizon` 链路、候选 JSON 的 CLI 往返均已核实成立。

---

## 独立复现（在两份报告之上另行验证）

**Spec 严重项成立。** 构造一份中间日缺失的缓存：

```
requested span 2020-01-02..2020-01-06
days returned : ['2020-01-02', '2020-01-06']
rows returned : 4
NO ERROR RAISED
```

端点检查通过，2020-01-03 整天消失且不报错。上一轮「越界立即报错就能挡住这类失效」的说法被高估了：它只挡端点移动，而 `purge_horizon` 变化恰好丢的是内部日期。

**Standards 死代码项成立。** AST 扫描：

```
qlib_lightgbm_direct.py  未用 import: bisect_right, restrict_to_universe,
                                     TrainingMatrix, feature_label_parts, read_features
feature_shard_cache.py   未用 import: Any
search_runner.py         未用 import: np
```

**Spec #3 成立。** `search_plan.py:74` 是 `required = max([config["min_train_days"], *fixed])`，而 `search_report.py:98` 只比 `min_train_days`。正确判据应为 `plan["folds"][0]["train_days"]`。

**Standards #4 成立。** `search_cli.py:19` 的 `choices=[1,2]` 与 `search_runner.py` 的恒拒绝矛盾，用户会在训练开始后才拿到错误。

**Standards 撞名成立。** `tests/test_lgbm_seam.py` 早已存在，测的是 `app/research/lgbm.py`。

---

## 汇总

- **Standards**：硬性违规 4 项、判断项 7 类；本轴最严重是 `qlib-lightgbm-direct-run.md:347` 与 `:350` 在同一节内自相矛盾地描述缓存身份。
- **Spec**：严重 1 项、中等 3 项、轻微 3 项；本轴最严重是 `covered.json` 只比端点，内部整天缺失仍静默读短（已独立复现）。

两轴不做跨轴排序。
