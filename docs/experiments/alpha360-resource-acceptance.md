# Alpha360 资源验收记录

- 日期：2026-09-05
- 依据：[qlib-oom-mitigation-plan.md](qlib-oom-mitigation-plan.md) 第 6 节 E 步
- 结论：内存与产物门槛全部达标；阶段级峰值归因未按计划要求计量，D 步复现门槛尚未执行，因此不宣称 E 步整体完全通过。

## 运行环境

容器 `MemTotal` 8020612 kB（约 7.65 GiB），`memory.max=max`。阈值沿用改造方案预先冻结的数值，未在看到结果后调整：警戒 5.5 GiB、安全中止 6.0 GiB、最低可用 1.55 GiB、连续 3 个一秒样本。

本次会话前容器重启过，cgroup `oom_kill` 计数从此前记录的 2 归零。两次运行各自的前后计数在下表分别记录，不跨重启比较。

## 两次运行

配置除早停指标外完全相同：`alpha360_validation.json`（l2）与新增的 `alpha360_validation_2.json`（rank_ic）。两者 `feature_sets`、`horizons`、切分参数、`feature_batch_size=64`、`qlib_kernels=2`、`scan_batch_rows=4096`、`cache_schema_version=2` 一致，因此共享同一数据身份与分片缓存。

| 项 | 运行 1（l2） | 运行 2（rank_ic） |
|---|---|---|
| 实验目录 | `var/experiment-alpha360-validation/8889fec07b5b48096bda` | `.../958a6e26d7e1afde18fc` |
| trial | `73e81f6789b719ade307` | `5d2fc5487cf03d23fe92` |
| 监控记录 | `var/alpha360-memory-validation.json` | `var/alpha360-memory-validation-2.json` |
| 采样数 / 总时长 | 306 / 305.1 s | 68 / 67.0 s |
| `memory.current` 峰值 | 4315336704 B（4.019 GiB）@ 281.1 s | 4236623872 B（3.946 GiB）@ 59.0 s |
| 最低 `MemAvailable` | 3.425 GiB | 3.364 GiB |
| 警戒样本 / 越限样本 | 0 / 0 | 0 / 0 |
| `resource_limit` | false | false |
| `oom_kill` 前 → 后 | 2 → 2 | 0 → 0 |
| 退出码 | 0 | 0 |
| trial 内训练评价耗时 | 49.03 s | 62.87 s |

运行 1 由本会话之前的执行产生，本次只核验其监控记录与产物，未现场观察该次运行。运行 2 由本次会话执行。

### 缓存复用与内存回落

运行 2 未新建缓存目录，仍使用 `_cache/598ca32c0a4de2f91dc0`（1108 只证券、18 个批次、批大小 64、约 545 MB），特征准备阶段被完整跳过：10 秒内即进入训练。总时长从 305 s 降到 67 s。运行结束后容器 `memory.current` 回落到约 1.01 GiB。

### 分片准备期驻留

运行 1 的 0～205 s 为分片准备期，`memory.current` 从 0.645 GiB 缓慢移动到 0.681 GiB，13 个采样点内累计漂移约 36 MiB，未随已处理分片数持续增长。随后约 205～265 s 升至 1.2～1.6 GiB，265 s 后进入训练与评价并到达全程峰值。

上述阶段边界是按时间线推断的，不是监控器标注的阶段。监控器只采集 `memory.current`、`MemAvailable` 和耗时，没有记录阶段字段；runner 也只在失败路径写 `phase`。因此改造方案要求的"索引准备、矩阵填充、LightGBM Dataset 构建、训练、预测"分阶段峰值目前无法从记录中直接给出，这一条未满足。

## 产物核验

两次运行均为三个 Fold，`expected_dates=44`、`valid_dates=44`、覆盖 100%，模型、学习曲线、逐日指标、预测、特征重要性和 `complete.json` 校验和齐备，`report_qlib_experiment.py` 均返回 `complete: 1/1`。运行结束后无残留 `.running.lock`。

| trial | 早停 | best_iteration | 评估轮数 | 平均 Rank IC | 最差 Fold | 去掉最好 Fold | Rank ICIR |
|---|---|---|---|---:|---:|---:|---:|
| `73e81f67…` | l2 | [1, 1, 9] | [51, 51, 59] | 0.007137 | -0.035600 | -0.001437 | 0.0745 |
| `5d2fc548…` | rank_ic | [1, 176, 5] | [51, 226, 55] | 0.014326 | -0.016837 | 0.011715 | 0.1503 |

这两组只是资源验收的副产物，样本仍是 44 个共同成熟日期，不构成 Alpha360 的效果结论，也不参与候选选择。两次的 Fold 1 best_iteration 均为 1，属于诊断项。

注意这两个 trial ID 也出现在正式结构搜索 `1d49ec752e05c902cb5d` 中，但数值不同（0.012770 与 0.012299）。`trial_id` 只摘要模型规格，不含数据身份；本验收配置只含 alpha360，股票池回看窗口为 59，而结构配置含 alpha158 时为 60，池内行数与逐 Fold 训练行因此不同。引用时必须连同实验目录一起给出，详见 [执行记录](execution-2026-09-05.md)。

## 恢复幂等

对 `alpha360_validation.json` 执行 `--resume --max-trials 1`，返回 `{'attempted': 0, 'completed': 1, 'failed': 0, 'planned': 1}`。实验身份仍解析为 `8889fec07b5b48096bda`，说明代码与配置身份未变。恢复前后对该 trial 全部文件做 SHA256 快照比对，结果完全一致：attempt 目录仍只有 `attempt-0001`，`success.json` 仍指向 `attempt-0001`，12 个文件校验和无变化。

## 未完成项

1. 阶段级峰值：需要监控器与 runner 交换阶段标记，或由 runner 自行记录各阶段峰值，才能满足"不能只报告最终最大值"的要求。
2. 改造方案 D 步已于本日稍后执行并通过，见 [执行记录](execution-2026-09-05.md)：基线与 12 组 Alpha158 产物逐字节相同，唯一超出 1e-9 的是诊断字段 `constant_baseline_l2`（float32 标签所致，已定位，不影响任何选择用指标）。
3. 旧结构实验 `var/experiment-search/5e16ac1eea9e3be0cc90` 及其 `_cache/3408480cbe3c9bb15e3b` 仍保留两个内容为已退出 PID 1266 的 `.running.lock`，按锁规则未自动清理。
4. F 步已完成：新实验 `var/experiment-search/1d49ec752e05c902cb5d` 24/24，失败 0，峰值 4.489 GiB，无新增 OOM。

## 复现命令

```powershell
docker compose exec -T backend uv run python scripts/monitor_qlib_memory.py --output var/alpha360-memory-validation-2.json -- uv run python scripts/run_qlib_search.py --config configs/experiments/alpha360_validation_2.json --output var/experiment-alpha360-validation --max-trials 1
docker compose exec -T backend uv run python scripts/run_qlib_search.py --config configs/experiments/alpha360_validation.json --output var/experiment-alpha360-validation --resume --max-trials 1
```
