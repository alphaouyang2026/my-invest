# 运行实验搜索

2026-09-05 已按本文件顺序完成一轮完整执行（基线 → 结构 24 组 → 参数 80 组 → 三种子复验 15 项 → 移交）。过程与结论见 [execution-2026-09-05.md](execution-2026-09-05.md)，交付物见 [handover-2026-09-05/](handover-2026-09-05/)。

本套工具不创建数据库 ResearchRun，不修改已发布模型。配置位于 `backend/configs/experiments`。
以下命令从 `backend` 工作目录执行；Docker 可在仓库根目录加前缀 `docker compose exec -T backend`。

## 顺序

```powershell
uv run python scripts/run_qlib_baseline.py --config configs/experiments/baseline.json --reference ../result_3.txt --dry-run
uv run python scripts/plan_qlib_experiment.py --config configs/experiments/structure_search.json
uv run python scripts/run_qlib_search.py --config configs/experiments/structure_search.json --max-trials 1
uv run python scripts/run_qlib_search.py --config configs/experiments/structure_search.json --resume
```

Docker 只挂载 backend；基线参考文件在仓库根，容器不可直接读取。先显式复制一份到 backend 下或用本地已配置数据库/依赖的环境运行 baseline。`--reference` 必须是实际文件，不会从聊天内容恢复原始数据。

另有 `smoke.json`：仅一个 Fold、两种停止指标、各最多 3 轮，只有两天评估，仅用于贯通性检查。可运行 `uv run python scripts/run_qlib_search.py --config configs/experiments/smoke.json --output var/experiment-smoke --max-trials 2`。该计划与正式结构计划不同，候选晋级的切分校验会拒绝将其混入正式参数搜索。

命令打印实验目录，将下方 `<实验目录>` 和 `<trial-id>` 替换为实际输出；占位符不是可运行参数。

参数搜索每结构 40 组包含一个入选超参数锚点；该阶段所有组（含锚点）统一采用最多 2000 轮、patience=100。因此锚点用于相同训练预算的比较，原预算下的成绩仍保留在结构阶段。

```powershell
uv run python scripts/evaluate_qlib_candidates.py --experiment <结构实验目录> --select <trial-id-1> <trial-id-2> --candidates-out configs/experiments/selected_structures.json
uv run python scripts/run_qlib_search.py --config configs/experiments/parameter_search.json --dry-run
uv run python scripts/run_qlib_search.py --config configs/experiments/parameter_search.json --max-trials 1
# 预算确认后去掉 --max-trials 并添加 --resume
uv run python scripts/evaluate_qlib_candidates.py --experiment <参数实验目录> --select <五个trial-id> --candidates-out configs/experiments/selected_parameters.json
uv run python scripts/run_qlib_search.py --config configs/experiments/replicate.json --dry-run
uv run python scripts/run_qlib_search.py --config configs/experiments/replicate.json
uv run python scripts/report_qlib_experiment.py --experiment <复验实验目录>
```

`evaluate` 负责读取结果/晋级，实际多种子训练由 `run` + replicate 配置执行。所有 `--help` 无需数据库。plan/dry-run 仅读取 provider 与快照元数据，写计划，不计算特征、不训练。相对 provider/output 路径相对工作目录；候选文件路径相对配置文件目录。

## 时间语义

label 是 `close[t+h+1] / close[t+1] - 1`，h 为交易日跨度。严格要求训练标签结束日早于验证首日、验证标签结束日早于评估首日。统一使用 purge_horizon=20，生成至少 60 日验证且独立于实际 h。排除最长标签未成熟的尾部日期，末个评估 Fold 可少于 20 日。

最近 252 日与 expanding 使用同一最早训练日期约束，历史不足直接失败。模板为短历史探索起点；三个开发 Fold 不足以证明长期有效。

当前快照核验后最早满足全部约束的评估起点为 2026-02-17，最长标签最后成熟日为 2026-04-21，只有 44 个共同日期（20+20+4）。这只能做流程与初步探索；80 组搜索很容易对这段短历史过拟合，正式大规模筛选应先增加历史/新时期数据。

## 产物与恢复

`var/experiment-search/<id>` 保存 manifest、fold_plan、trial_plan 和 trials；同级 `_cache/<data-id>` 缓存相同数据上下文与切分的面板，可跨参数搜索和复验阶段共享。trial 的每次尝试保留在 `attempt-NNNN`，成功后写 checksum 完成标记和 success 指针。报告只读取校验通过的成功项；不完整搜索不能导出候选。失败信息记录异常类型、阶段和调用位置，不保存可能泄漏连接信息的原始异常文本。

`--max-trials` 限制本次新尝试数；`--resume` 跳过已完成且 checksum 正确的项。失败/中断重跑创建新 attempt，不删除旧记录。强制中断可能留下 `.running.lock`，先确认原进程已退出，再由操作者移除此单个锁文件；脚本不会自动抢占锁。

当前分片缓存 schema 强制 `--workers 1`；Qlib 特征按股票池证券并集每 64 只一批计算，默认使用 2 个 Qlib 进程。训练按 Fold 只组装所需矩阵，train/valid 与 evaluation 分开驻留。完成 Alpha360 资源验收前，`--workers 2` 会被明确拒绝。缓存使用 Parquet/JSON，不反序列化 pickle；代码、依赖或缓存 schema 变化会触发新身份，不复用旧实验缓存。cache 是重建数据而非事实来源。

在约 7.65 GiB 的当前容器环境执行 Alpha360 验收时，用外部监控器包装一条单-trial 命令：

```powershell
uv run python scripts/monitor_qlib_memory.py --output var/alpha360-memory-validation.json -- uv run python scripts/run_qlib_search.py --config configs/experiments/alpha360_validation.json --output var/experiment-alpha360-validation --max-trials 1
```

验收使用独立的单组配置 `alpha360_validation.json`（l2）和 `alpha360_validation_2.json`（rank_ic）；两者只有早停指标不同，数据身份一致，因此第二组复用同一份分片缓存。实测结果见 [Alpha360 资源验收记录](alpha360-resource-acceptance.md)。

默认警戒线 5.5 GiB、安全中止线 6.0 GiB、最低可用内存 1.55 GiB，连续三个一秒样本超限才中止。环境内存变化时先按改造方案重新冻结预算。

train/valid 曲线每轮包含 L2 和等权平均日度 Rank IC；仅配置的首指标控制 early stopping。推断时使用最佳迭代，不使用测试数据选择迭代。共享处理只含无拟合参数的 InfToNaN、DropnaLabel、每日 CSRankNorm；将来新增拟合型标准化时必须在每个 Fold 的 train 内拟合。

报告包含完整覆盖标记、逐 Fold、去掉最好 Fold、正值比例、ICIR、seed 波动及区块 bootstrap。区间不做多重搜索校正，不应据此宣称发现可盈利因子。交易成本后表现和未来盲测留给下一阶段。

基线重放保持旧 direct CLI 语义和原始 5 个 Fold，与新搜索的共同 Fold 不同；旧基线不额外保存其原来没有的训练曲线。新搜索中的基线参数锚点则拥有全部逐轮产物。
