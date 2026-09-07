# 代码审查（两轴）：`main` → `feat/07-qlib-multifactor-lightgbm`

- 审查日期：2026-09-05
- 固定点：`main`（`5b04bd244b8bb88186a5b3263e13102073d008e0`，与分支的 merge-base 相同，`main` 未移动）
- 审查对象：`HEAD`（`02e05400fb80608644f85d7dafc9dec50d0c4f26`）
- 差异命令：`git diff main...feat/07-qlib-multifactor-lightgbm`（三点）
- 提交范围：`git log main..HEAD --oneline`（12 次提交）
- 规模：106 文件、+31604 / −254（其中相当一部分是 fixture / 生成物 / 锁文件）
- 工作树状态：干净，无未提交改动

Spec 来源：`.scratch/personal-investment-research-system/issues/07-qlib-multifactor-lightgbm.md`（14 条验收）+ `.scratch/personal-investment-research-system/07-design-doc.md`。
Standards 来源：`README.md`、`CONTEXT.md`、`CLAUDE.md`、`frontend/AGENTS.md`、`frontend/CLAUDE.md`、`docs/agents/`、`docs/adr/`，外加 Fowler《重构》第 3 章气味基线。

两个子代理并行执行、互不知晓对方结论。下面两轴分别呈现，**不做跨轴合并或重排**。

---

## Standards

### 硬性违规（已文档化标准）

**未发现。** 逐条核对：

- typed API client（`README.md:68-75`）已重新生成 —— `openapi.json` 与 `frontend/lib/api/schema.d.ts` 被 `.gitignore:38-39` 排除，本地均已含 `model-runs`；
- 迁移遵循 `README.md:46-58`，`20260829_b17c4e0a55d1` 只做 `ALTER TYPE`，正是文档所述例外；
- `frontend/AGENTS.md` 自动生成块未被改动。

**需提请注意**：`README.md:46-58` 的迁移规则本身由本分支改写，以容纳本分支所需的 enum 拆分 —— 标准与实现同批提交，评审时应单独确认该规则变更本身可接受。

### 判断项（气味基线）

**Duplicated Code（重复代码，本轴最强信号）**

- `backend/app/experiments/search_runner.py:437-489` 与 `:492-560`：`_run_legacy_trial` 与 `run_trial` 几乎逐行相同，仅数据加载方式不同。`run_trial:493` 用 `isinstance(data, ShardedData)` 分派（**Repeated Switches**），`fit_model:350-361` 对同一对类型再做一次 isinstance 分支。修法：让元组形态实现 `MatrixSegment` / `ShardedData` 同一接口，删掉 legacy 分支。全新文件带 "legacy" 路径也属 **Speculative Generality**。
- `backend/app/experiments/qlib_lightgbm_direct.py:522` 与 `backend/app/research/dataset.py:199`（`_restrict_to_universe`）；`qlib_lightgbm_direct.py:395` 与 `model_workflow.py:189`（`_build_universe`，policy 构造与 `{str(member.instrument_id) ...}` 完全同形）。修法：抽到 `app/research/` 共享，`experiments/` 调用。
- `frontend/app/model-research-center.tsx:109-116` 与 `research-center.tsx:69-76` 的 `api<T>` helper、`:81-99` 与 `:53-66` 的 `ACTIVE` / `PHASES` 逐字重复。修法：提到 `frontend/lib/`。

**Data Clumps（数据泥团）**

六个日期 `train_start…test_end` 结伴出现于 `backend/app/api/research.py:43-48`、`services/model_research.py:151-158`、`research/splits.py:112-117`、`frontend/app/model-research-center.tsx:250`。而 `splits.py:37` 的 `Segment` 正是那个想要诞生的类型。修法：请求体与 `build_split_plan` 改收 `{train, valid, test}` 三个区间对象。

**私有名跨模块引用**

`backend/app/services/research.py:243` `from app.services.model_research import _instrument_codes`；`search_runner.py:349` `from app.experiments.qlib_lightgbm_direct import _feature_label_parts`。下划线宣告私有却被跨模块消费，等于封装谎言。修法：去掉下划线并移到共享位置。

**Middle Man（中间人）**

`services/research.py:220-232` `SqlResearchApplication.get_ranked_scores` 判断 kind 后整体转交 `SqlModelResearchApplication`；`api/research.py:57-62` 的 `ModelResearchApplication` Protocol 与 `ResearchApplication` 方法集高度重叠。注释已说明理由（RankedScores 对外同形），可接受，但两个 Protocol 值得合一。

**Mysterious Name（神秘命名）**

`search_runner.py:345-400, 437-560` 大量单字母 / 双字母变量 `f, d, n, tx, ty, vx, vy, ts, vs, yr, yc`，并用 `;` 串行语句（`:489`、`:549`），与仓库其余部分的命名风格差距明显。修法：至少 `f`→`fold`、`tx/ty`→`train_features/train_labels`。

**领域语言缺口**

`CONTEXT.md` 本分支未变更，而新增了 TrainedModel、ArtifactPublication、FeatureSet、SplitPlan、DayProvider 等核心概念（`backend/app/models/research.py`、`research/day_provider.py:29`）。`docs/agents/domain.md`「Use the glossary's vocabulary」要求：概念不在词表里就是信号 —— 请补入 `CONTEXT.md`。

另注：`day_provider_export.py:1-6` 已明确自己是唯一 seam、bundle_builder 是适配器，与 ADR-0001 不冲突，是本分支的良性去重。

**次要**

- `bundle_builder.py:23-24` 现仅转发 `EXPORTER_SCHEMA_VERSION` / `PYQLIB_VERSION`，`services/research.py:27` 仍从它导入（轻度 Middle Man）。
- `docs/experiments/handover-2026-09-05/` 提交了上万行生成物（`replicate-manifest.json` 10388 行），与刚刚忽略 40MB 基线的取向不一致，建议同样按 checksum 引用。

---

## Spec

### 严重

1. **常数模型判据用了设计明确否决的做法。**
   设计 `07-design-doc.md:1168`「用整个 valid 段的方差判定常数模型 | …必须逐 `prediction_date` 判断」、`:842`「gain==0、单叶树…只作为**辅助诊断字段**…不能代替逐截面输出检查」、`:838` 失败码 `model_no_rankable_validation_cross_section`。
   实现 `backend/app/research/lgbm.py:187-212` 恰好是整段 `std()==0` 加 gain / 单叶树三条并联判据；`backend/app/research/model_workflow.py:341` 传入的还是 `results["test_scores"]`（test 段，不是 valid），失败码写成 `model_no_useful_iterations`。§11.2.1 的 float64 容差、`rankable_cross_section_count`、`constant_prediction` 状态、`some_validation_cross_sections_are_not_rankable` warning 全部缺失（全仓无此标识符）。

2. **发布协议顺序与 §10.2.1 相反。**
   `07-design-doc.md:751` 要求第二笔事务在 rename 之后同时创建 `ResearchArtifact` / `TrainedModel` / `PredictionRun` 并置 run、task 为 succeeded。实现 `publication.py:24-28`、`model_workflow.py:415-464` 在 rename **之前**就提交了三张领域表和 `run.status=SUCCEEDED`（docstring 自认「step 2 marks the run succeeded while the directory is not yet in place」）。
   `:750` 的 `artifact_path_conflict` 无实现 —— `publication.py:205-210` 见到 final 已存在直接 return，不校验 checksum；`artifact_staging_missing` 亦缺（改用 `artifact_publication_lost`）；publication 表 `models/research.py:320-334` 没有 manifest / result_metadata 列；Task 终态不在同一事务。

3. **Scope creep：票外的实验搜索工具链，且违反已否决项。**
   `backend/app/experiments/`（direct_run + search_plan / runner / report / cli，约 1700 行）、`backend/scripts/*`、`backend/configs/experiments/*.json`、`docs/experiments/**`（约 17000 行生成物）不在 14 条验收内。更要紧的是 `app/experiments/qlib_lightgbm_direct.py:53,236` 用 `Ref($close,-6)/Ref($close,-1)-1` 的日度 5 日收盘到收盘标签训练，同时踩中 `07-design-doc.md:306`「否决日度标签训练」与「否决 Qlib 默认收盘到收盘标签」两条。

### 中等

4. `07-design-doc.md:119` / `:159` 的 `resolve_inference` 与 `model_inference_contract_unavailable` 完全未实现；`execution_spec.py:107-116` 的契约也缺 bundle schema / 字段、模型格式、pyqlib / LightGBM 身份。

5. `:949`「页面全部只读而 API 敞开，等于把未受约束的入口藏在 UI 后面」—— `frontend/app/model-research-center.tsx:270-278` 只列锁定项，从不渲染可覆盖项及其范围，也不提交 `model_params`。

6. warnings 清单不全：`model_workflow.py:355-378` 无 `zero_gain_features`，且 `high_test_missing_rate` 被限定为 experimental 特征集才发（Alpha158 的 40% 漂移无声）。

7. `execution_spec.py:138,198` 直接取库内 `model_params` 执行，未经 §7.5.1 白名单重新编译，指纹自校验对该字段是恒真的。

### 轻微

8. 测试缺口：`:910` 要求的 CancellableLGBModel 与原版 `LGBModel` 逐位相同、`artifact_path_conflict` 故障注入、`:221` v1/v2 数据包上 06 动量一致，均无对应测试。

### 关于 spec 本身的改动

本分支新增了设计文档并重写了 ticket 的 14 条验收。改写方向是**收紧而非放宽**（明确了错误码、embargo 口径、fixture 规模），属合理的产品决策更新；但上述 1、2、3 恰恰偏离了改写后更严的口径 —— 不是 spec 迁就实现，而是实现落后于 spec。

---

## 汇总

- **Standards**：硬性违规 0 项，判断项 7 类；本轴最严重是 `backend/app/experiments/search_runner.py:437-560` 的 `_run_legacy_trial` / `run_trial` 近乎逐行重复（并带出两处 isinstance 分派）。
- **Spec**：严重 3 项、中等 4 项、轻微 1 项，共 8 项；本轴最严重是常数模型判据（`lgbm.py:187-212` + `model_workflow.py:341`）用整段 test 方差代替设计要求的逐 `prediction_date` valid 截面判断，且失败码与设计不符。

两轴不做跨轴排序 —— 分离正是为了避免一轴掩盖另一轴。
