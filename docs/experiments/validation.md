# 实施验证记录

验证日期：2026-09-04。此次交付是实验工具和有界贯通验证，未执行完整搜索、原始基线重放或模型发布。

## 自动测试

仓库根目录运行：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_experiment_search.py backend/tests/test_qlib_lightgbm_direct.py -q --basetemp=backend/.pytest-tmp-search-memory-final -p no:cacheprovider
```

结果：58 passed（`test_experiment_search.py`、`test_qlib_lightgbm_direct.py`、`test_feature_shard_cache.py`、`test_booster_training.py`）。覆盖切分和标签成熟、确定性采样、两种真实 LightGBM 早停、缓存复用、失败重试、校验和、候选晋级、报告以及 UTF-16 基线文件读取。双 worker 一致性测试已随并发能力一并移除。

## 真实数据验证

- 原始 `result_3.txt` 基线 dry-run 通过；没有执行预测复现，不能宣称与原始结果一致。
- 正式结构计划：24 trials，3 folds，44 个共同成熟日期。容器目录为 `/app/var/experiment-search/5e16ac1eea9e3be0cc90`。
- 最小 smoke：2/2 完成，失败 0；两种停止指标各最多 3 轮、1 个 Fold、2026-02-17 至 2026-02-18 两个评估日。
- 两项均记录 `best_iteration=2`、`evaluated_rounds=3`，预测 1553 行，覆盖率 100%。两项平均日度 Rank IC 均为 0.01711041994；这是贯通检查，不是效果结论。
- 学习曲线包含 train/valid 的 L2 和 Rank IC；模型、逐日指标、预测、特征重要性及完成校验和均已生成。
- 同配置 `--resume --max-trials 2` 返回 attempted=0、completed=2，确认校验后跳过已完成项。
- smoke 产物目录：`/app/var/experiment-smoke/cb4f7f1eb7edbbc45cc2`，位于容器数据卷，不是宿主机工作区文件。

可从仓库根目录重新生成报告：

```powershell
docker compose exec -T backend uv run python scripts/report_qlib_experiment.py --experiment var/experiment-smoke/cb4f7f1eb7edbbc45cc2
```

首次 smoke 在特征计算后异常退出，容器记录一次 OOM kill。随后移除首次缓存写入后的重复读取，并将股票池过滤提前到特征复制前；回归测试验证未来标签仍使用完整价格序列。修订后的 smoke 完成，OOM kill 计数未增加。旧中断目录及锁保留，未自动清理或抢占。

## 下一阶段门槛

按执行 Prompt 先完成原始基线复现。正式大规模筛选前，应增加历史或新时期数据，并确认训练预算；现有 44 个日期不足以支持可靠的最优组合结论。Alpha360 的真实内存占用与训练耗时已于 2026-09-05 实测（见 [Alpha360 资源验收记录](alpha360-resource-acceptance.md)），默认仍保持串行。

## 分片改造验证（2026-09-05）

- 本地回归：34 passed。
- 新 Alpha158 smoke：2/2 完成，恢复返回 attempted=0；两种停止指标的平均 Rank IC 均与旧 smoke 完全一致。
- 特征读取从一次 214.8 万行全市场面板改为 17 个证券分片；常规分片约 31,168 行，末分片约 10,714 行。
- 新 smoke 缓存约 182 MB，旧 smoke 缓存约 1.44 GB；OOM kill 计数保持为 2，没有增加。
- Alpha360 三 Fold 已在 2026-09-05 执行，两次连续 trial 峰值 4.019 / 3.946 GiB，无新增 OOM；详见 [Alpha360 资源验收记录](alpha360-resource-acceptance.md)。阶段级峰值归因和改造方案 D 步的复现比较仍未完成。
