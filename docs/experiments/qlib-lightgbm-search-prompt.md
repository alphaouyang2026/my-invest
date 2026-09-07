# Qlib + LightGBM 对照实验执行 Prompt

你是本项目的实验执行者。目标是在固定数据与共同时间切分下找到稳定的开发期候选，而不是最大化已知测试期成绩。遵循本文件的阶段门槛，每完成一个阶段先报告证据，再进入下一阶段。搜索的目的是给 `scripts/qlib_lightgbm_direct.py` 找出可执行的参数：每个 trial 都由 `run_qlib_search.py` 编译成 `DirectPredictionConfig` 后交给同一个 `run_direct_prediction` 执行，搜索侧不含任何特征装配、折叠或训练代码。因此阶段 7 的对账是交付条件而不是可选步骤。

## 边界

- 读取 `CONTEXT.md`、`docs/design/qlib-lightgbm-direct-run.md` 和本目录 `README.md`。参数以 `backend/configs/experiments/` 为准。
- 数据库和现有 provider 只读；输出写入显式实验目录。不发布模型、不下单、不自动重建 provider。
- 当前 result_3 的时期已经参与开发，所有搜索报告都标记 development；独立盲测使用未来未参与选择的时期。
- 数据不足时报告不足并请求扩大快照或调整研究范围；保持验证集长度与 purge 规则，不能为跑通而偷偷缩短窗口。
- 修改代码、数据、依赖、股票池、标签或配置后生成新实验身份。恢复运行要求所有身份一致，校验失败立即停止。

## 命令约定

下列命令以 `backend` 为工作目录。配置默认使用容器中的 provider 路径；在仓库根目录使用 Docker 时，在每条 `uv run ...` 前添加 `docker compose exec -T backend`。本地执行需先配置可访问的 provider 和数据库，参见本目录 `README.md`。

`<结构实验目录>`、`<参数实验目录>`、`<复验实验目录>` 分别替换为对应 plan/run 打印的路径，`<trial-id-N>` 替换为该阶段报告中的真实 ID。占位符不能原样执行。跨阶段在同一环境中运行，保留候选文件引用的源目录。

以下命令按阶段执行，不整段自动运行；训练命令仍受各阶段授权与完成条件约束。

## 阶段 1：基线

运行 baseline 脚本的 dry-run，核对快照、seed 和原始日期。获得长任务授权后运行复现。

使用 `scripts/run_qlib_baseline.py`，输入为原始配置和 `result_3.txt`：

```powershell
uv run python scripts/run_qlib_baseline.py --config configs/experiments/baseline.json --reference ../result_3.txt --dry-run
# 取得长任务授权后执行真实复现
uv run python scripts/run_qlib_baseline.py --config configs/experiments/baseline.json --reference ../result_3.txt
```

Docker 内不能直接读取仓库根的 `../result_3.txt`；先将参考文件显式复制到容器可见位置，再替换 `--reference` 路径。输出目录为脚本打印的 `var/experiment-baseline/<id>`；真实复现生成 `comparison.json`。dry-run 不生成比较结论。

完成条件：`comparison.json` 的 matches=true，预测行键、分数、四个指标和 fold 信息均一致。否则停在复现阶段，报告差异；不能带着未知差异搜索。

**参考文件的时效**：2026-09-07 之前捕获的 `result_3.txt` 必定不匹配。direct 入口当时经 `DataHandlerLP` 以 float64 组装面板、每个 fold 只报四个字段；现在流式读取 float32 分片并附带早停诊断，`prediction_scores` 与 `folds` 因此必然不同。这不是容差问题，不得放宽比较来通过。正确做法是从当前实现重新冻结一份参考，并记录它由哪一版产生。

## 阶段 2：共同计划

执行结构配置的 plan 脚本。检查真实日历上的训练长度、60 日验证长度、两个 purge、共同成熟评估日、fold 数以及 24 个结构组合。将计划表和预计训练调用数报告给用户。

使用 `scripts/plan_qlib_experiment.py`：

```powershell
uv run python scripts/plan_qlib_experiment.py --config configs/experiments/structure_search.json
```

将打印的目录记为 `<结构实验目录>`，检查其中的 `manifest.json`、`fold_plan.json` 和 `trial_plan.json`。本步骤不计算特征、不训练。

完成条件：计划通过所有校验，各 horizon 使用完全相同的评估日期；用户知道当前样本跨度与不确定性。

## 阶段 3：小规模试运行

经授权使用 `--max-trials 1`，检查完整模型、逐轮曲线、逐日指标及失败记录。测量耗时、内存与磁盘，再估算 24 组预算。确认恢复命令跳过已经验证的完成项。

使用 `scripts/run_qlib_search.py` 运行正式计划中的第一组，再用 `scripts/report_qlib_experiment.py` 检查部分结果：

```powershell
uv run python scripts/run_qlib_search.py --config configs/experiments/structure_search.json --max-trials 1
uv run python scripts/report_qlib_experiment.py --experiment <结构实验目录>
```

检查成功 attempt 下的 `models/`、`learning_curves.parquet`、`daily_metrics.parquet`、`predictions.parquet`、`metrics.json` 和 `complete.json`。此时正式报告应为 `partial_not_for_selection`。

如需先做更小的贯通与恢复检查，使用独立的 `smoke.json`：

```powershell
uv run python scripts/run_qlib_search.py --config configs/experiments/smoke.json --output var/experiment-smoke --max-trials 2
uv run python scripts/run_qlib_search.py --config configs/experiments/smoke.json --output var/experiment-smoke --resume --max-trials 2
```

两项成功后，第二条应返回 `attempted=0`。smoke 不替代正式第一组的预算测量，也不能晋级到参数搜索。注意：正式计划仅完成一组时执行 `--resume --max-trials 1` 会训练下一组，并非仅检查跳过。

完成条件：一组真实数据运行成功，报告包含每轮 train/valid L2 和平均日度 Rank IC，文件 checksum 通过。失败项保持可追踪，不用零分替代异常。

第一组会写出特征分片缓存（`var/experiment-search/_cache/<身份>/`），其后同 `(特征集, horizon)` 的 trial 直接复用，不再调用 Qlib。预算测量要区分这两者：首个 trial 含建缓存成本，其余不含。缓存身份只由数据决定，因此阶段 4/5/6 在同一数据上共用同一份。

## 阶段 4：结构搜索

经授权完成 24 组，不根据中途成绩更改组合。生成报告。仅当所有计划 trial 成功时选择两个完整覆盖的结构；以平均日度 Rank IC 为主，结合最差 Fold、正值比例及去掉最好 Fold 的结果。记录人工选择理由，再导出明确 trial ID 的候选文件。

训练使用 `scripts/run_qlib_search.py`，汇总使用 `scripts/report_qlib_experiment.py`，候选导出使用 `scripts/evaluate_qlib_candidates.py`：

```powershell
uv run python scripts/run_qlib_search.py --config configs/experiments/structure_search.json --resume
uv run python scripts/report_qlib_experiment.py --experiment <结构实验目录>
# 24/24 成功且两个结构的选择理由已记录后执行
uv run python scripts/evaluate_qlib_candidates.py --experiment <结构实验目录> --select <trial-id-1> <trial-id-2> --candidates-out configs/experiments/selected_structures.json
```

报告输出为 `report.md`、`report.json` 和 `leaderboard.csv`；`selected_structures.json` 是下一阶段的输入。

完成条件：24 组均有校验通过的结果，两个候选及理由已冻结。若统计接近，优先简单结构。best_iteration=1 本身不是淘汰条件。

## 阶段 5：参数搜索

使用 parameter_search 配置；候选文件引用上阶段真实结果，不手填虚构成绩。每结构 40 组，第一组为入选参数锚点，其余固定种子随机采样。保持原 Fold 和全部上下文不变。

使用 `scripts/run_qlib_search.py` 先计划再训练，使用 `scripts/evaluate_qlib_candidates.py` 导出五个候选：

```powershell
uv run python scripts/run_qlib_search.py --config configs/experiments/parameter_search.json --dry-run
# 核验 80 组计划并确认预算后执行
uv run python scripts/run_qlib_search.py --config configs/experiments/parameter_search.json --max-trials 1
uv run python scripts/run_qlib_search.py --config configs/experiments/parameter_search.json --resume
uv run python scripts/report_qlib_experiment.py --experiment <参数实验目录>
# 80/80 成功且五个候选的选择理由已记录后执行
uv run python scripts/evaluate_qlib_candidates.py --experiment <参数实验目录> --select <trial-id-1> <trial-id-2> <trial-id-3> <trial-id-4> <trial-id-5> --candidates-out configs/experiments/selected_parameters.json
```

将 dry-run 打印的目录记为 `<参数实验目录>`。配置读取阶段 4 的 `selected_structures.json`，输出的 `selected_parameters.json` 供阶段 6 使用。

完成条件：80 组计划都成功，生成前五个候选及完整参数，不只保留赢家。失败组合须解释并通过一个新的、提前冻结的计划处理，不能静默删除它们后晋级。

## 阶段 6：多种子复验

导出五个候选，使用 replicate 配置运行三个预定 seed。逐 seed 检查稳定性。bootstrap 以日期区块为单位；同日多 seed 先平均，不冒充额外交易日。样本不足时区间为 null，明确标注无法推断。

实际复验仍使用 `scripts/run_qlib_search.py`；`evaluate_qlib_candidates.py` 只读取/导出候选，不负责训练：

```powershell
uv run python scripts/run_qlib_search.py --config configs/experiments/replicate.json --dry-run
# 核验 15 项计划并确认预算后执行
uv run python scripts/run_qlib_search.py --config configs/experiments/replicate.json
uv run python scripts/report_qlib_experiment.py --experiment <复验实验目录>
```

将 dry-run 打印的目录记为 `<复验实验目录>`。输入为阶段 5 的 `selected_parameters.json`；查看 `report.json` 中的 `robustness` 和各 trial 的逐 Fold 指标。若中断，给同一训练命令添加 `--resume`。

完成条件：15 次完整复验及两个区块长度的敏感性统计完成。选出一个候选，记录参数、风险、样本不足项和冻结时间。

## 阶段 7：交回 direct 入口

搜索的产出必须能被生产入口执行，否则冻结的只是一份无法运行的描述。`evaluate_qlib_candidates.py` 在导出候选文件的同时，为每个 trial 生成一份 `<候选文件名>-direct-run-<trial-id>.json`，字段与 `scripts/qlib_lightgbm_direct.py predict` 的 flag 一一对应。

用它跑一次 direct，并与搜索报告对账：

```powershell
uv run python scripts/qlib_lightgbm_direct.py predict `
  --config configs/experiments/selected_parameters-direct-run-<trial-id-N>.json `
  > var/direct-<trial-id-N>.json
```

对账三项，任一不符即停止并报告，不得带着差异移交：

- `summary.fold_count`、每个 fold 的 train/valid/test 区间与 `fold_plan.json` 一致；
- `summary.model_params`、`stop_metric`、`rolling_train_policy` 与该 trial 的 `trial_plan.json` 一致；
- `summary.test_rank_ic_mean` 与该 trial `metrics.json` 的 `rank_ic_mean` 一致。导出的配置带 `purge_horizon`，任何 horizon 的候选都能在 direct 上重现被评分时的折叠几何。

从阶段 3 起，训练本身已经由 `run_qlib_search.py` 调用 `run_direct_prediction` 完成——搜索只负责把一个 trial 编译成运行参数并记录产物。因此本阶段核对的是"同一段代码在两个入口下给出同一结果"，而不是两套实现的近似程度；出现差异说明配置编译有误，不是数值容差问题。

若要跑多个 seed，用同一份 `--config` 加显式 `--seed` 覆盖，不要另建配置文件。

`--test` 可以放开到快照覆盖终点以取得可用预测（尾部标签未成熟的日期会标 `label_not_mature` 并排除在 IC 之外）。但**对账必须用未放开的原始评价窗口**：改了评价窗口就换了比较对象，三项对账随即失去意义。

完成条件：冻结候选在 direct 入口成功运行，三项对账一致，输出 JSON 与搜索报告一并交付。

## 阶段 8：移交

交付 manifest、fold/trial 计划、leaderboard、报告与候选配置。列出交易回测接入所需的统一成本/执行规则，并与用户确认新时期盲测；本轮预测搜索脚本不提供简化交易回测或独立盲测的虚假结论。

使用 `scripts/report_qlib_experiment.py` 重新生成最终汇总：

```powershell
uv run python scripts/report_qlib_experiment.py --experiment <复验实验目录>
```

交付该目录的 `report.md`、`report.json`、`leaderboard.csv` 和计划文件，阶段 4/5 的候选文件及选择理由，以及阶段 7 的 direct-run 配置与其运行输出。交易回测与未来盲测尚无对应脚本，需另行设计与授权，不能把本命令当作回测。

完整搜索或真实数据长任务只有在用户明确授权后启动。发生权限、数据覆盖或存储阻塞时，报告已完成阶段及下一步所需条件。
