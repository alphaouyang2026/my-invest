# direct-run 审查指摘复核（修订版）

- 原审查范围：`git diff main...HEAD` 中 direct-run 设计及相关实现，原报告 3 项 P2
- 复核日期：2026-09-06
- 基线：`main` = `5b04bd2`，HEAD = `02e0540`
- **工作树已含针对这 3 项的未提交修改**：`backend/app/experiments/qlib_lightgbm_direct.py`、`backend/app/research/day_provider.py`、`backend/tests/test_qlib_lightgbm_direct.py`、`backend/tests/test_model_research_golden.py`、`docs/design/qlib-lightgbm-direct-run.md`（+422 / −31）
- 复核结论：**3 项 P2 全部已修复**，且设计文档同步记录了新口径

## Standards

未发现需要单独报告的明确规范违规。CLI、provider 导出和预测逻辑的职责划分基本清楚。本轮修改未改变这一判断。

## Spec

### 1. [P2 → 已修复] 标签成熟状态使用日历末端，可能误判 snapshot 尾部

**原指摘**：`qlib_lightgbm_direct.py` 用 `manifest.calendar` 的最后日期判断成熟，而 exporter 导出的是整个日历 publication。若行情截至 2026-05-26、日历覆盖到 2026-06-30，尾部缺失标签会被标成 `label_unavailable`，不会得到设计要求的 `label_not_mature`（已用该边界复现）。此问题影响诊断，未发现它会把缺失标签计入 IC。

**当前实现**：已按 snapshot 实际行情覆盖截止点判断。

- `qlib_lightgbm_direct.py:606-616` 新增 `_mature_label_dates(calendar, snapshot_coverage_end, label_horizon)`，用 `bisect_right(days, snapshot_coverage_end)` 取已覆盖的开市日数，再减去 `label_horizon + 1`，只有标签退出日落在行情覆盖内的日期才算成熟。
- `:505-509` 与 `:631-634` 两个调用点统一走这个函数，参数为 `snapshot_coverage_end or provider.manifest.coverage_end`；`:155`、`:222` 两处入口传入的是 `snapshot.coverage_end`，日历末日不再参与判断。
- 回归测试：`test_qlib_lightgbm_direct.py:265` `test_label_maturity_stops_at_snapshot_bar_coverage`、`:242` `test_daily_metrics_keep_tail_diagnostics_but_mark_it_ineligible`。
- 设计文档同步：`docs/design/qlib-lightgbm-direct-run.md:276`、`:296` 明确写入「成熟边界以 `DataSnapshot.coverage_end` 的实际行情覆盖为准，不能使用可能延伸到未来的 calendar publication 末日」。

### 2. [P2 → 已修复] 第一只股票没有有效收盘价，会导致整个正常 provider 被拒绝

**原指摘**：`day_provider.py` 只选择 `all.txt` 第一只股票做 smoke check 并要求其 `$close` 非空；全期 untradable 的股票仍会被 exporter 收录（研究价格全为 NaN），若它按 UUID 排在首位，其他股票正常时 build/predict 仍失败。

**当前实现**：smoke read 改为按有界批次向后扫描，直到找到有限 `$close`。

- `day_provider.py:85-103`：`found_finite_close = False` 后遍历 `_instrument_batches(path / "instruments" / "all.txt")`，任一批次读到非空且 `$close` 有 `notna()` 即 `break`；只有全部批次都读不到有限 `$close` 才抛 `DayProviderError("Qlib provider smoke read returned no finite close")`。分批边界见 `:174` 的 `_instrument_batches`。
- 回归测试：`test_qlib_lightgbm_direct.py:105` `test_open_provider_continues_past_a_batch_without_research_prices`。
- 设计文档同步：`docs/design/qlib-lightgbm-direct-run.md:356` 第 4 步写明「按有界 instrument 批次向后检查……避免排序靠前但全期不可交易的证券误判整个 provider」。

### 3. [P2 → 已修复] 第 14 节承诺的关键验收测试尚未落实

**原指摘**：设计第 14 节要求验证逐 fold 独立训练、测试数据扰动不影响训练矩阵、数据库与 artifact 不变，以及真实 Alpha158/Alpha360 direct 链路；当时 direct 测试主要覆盖辅助函数，没有调用 `run_direct_prediction` 的完整编排测试，07 golden test 也不能代替这条独立入口。

**当前实现**：四项承诺各自有了对应测试。

- **完整编排 + 逐 fold 独立训练 + 只读 session**：`test_qlib_lightgbm_direct.py:477` `test_direct_prediction_retrains_each_fold_and_only_reads_session` 真实调用 `run_direct_prediction`，断言 `trained_folds == [1, 2]`（逐 fold 重新训练）、预测日期连续无重复（`nunique() == 40`）、session 只发生一次 `get` 且不具备 `add` / `commit`。
- **测试数据扰动不影响训练矩阵**：`:417` `test_test_values_cannot_change_the_prepared_training_matrix`。
- **真实 Alpha158 / Alpha360 direct 链路 + 数据库与 artifact 不变**：`test_model_research_golden.py:587` `test_direct_rolling_chain_uses_the_real_snapshot_without_product_state`，`@pytest.mark.slow` 且按 `["alpha158", "alpha360"]` 参数化，断言 `fold_count == 3`、`test_ic_dates > 0`、尾日进入预测、逐 fold 日期为 `[20, 20, 6]`，并对比运行前后的 `row_counts()` 与 artifact 目录列表完全一致。
- 设计文档同步：第 14 节新增一条「application service 的完整编排只读取 session，逐 fold 重新调用训练并拼接连续且无重复的预测」，slow test 一条也从「Alpha158 完整链路和 Alpha360 smoke」改写为两条 direct rolling 链路加产品状态零写入的对照。

## 验证

- `python -m pytest tests/test_qlib_lightgbm_direct.py -q` → **15 passed**（原报告时为 11 项）。
- `test_direct_rolling_chain_uses_the_real_snapshot_without_product_state` 带 `@pytest.mark.slow`，本次未执行，需要真实连续 snapshot 与 `--slow` 才能跑；它是第 3 项里唯一尚未在本次复核中实测通过的部分。

## 结论

Standards：0 项。Spec：原 3 项 P2 现均已修复（其中真实链路 slow test 未实测执行），设计文档已同步记录成熟边界与 smoke 扫描两处口径。

静态核对仍未发现滚动窗口、H+1 purge 或 IC 索引对齐的明显错误，**因此这 3 项修复不解释此前 IC 为负** —— 原报告的这一判断保持不变，IC 为负需要另行归因。
