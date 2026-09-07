# 07 审查指摘有效性复核

复核日期：2026-09-06。对象：`07-code-review-main-HEAD.md`。
当前 HEAD 为 `02e05400fb80608644f85d7dafc9dec50d0c4f26`，main 为 `5b04bd244b8bb88186a5b3263e13102073d008e0`，与原报告一致。当前工作树有未跟踪文件，不能沿用原报告“工作树干净”的描述；本次未修改产品代码。

## Spec

结论：8 项中，6 项成立（1、2、4、5、6、8），1 项部分成立（7），1 项不成立（3）。成立不意味着原报告所有措辞、影响范围与优先级都准确。

| 原编号 | 判定 | 证据与修正 |
|---|---|---|
| 1 常数模型判据 | 成立，优先修复 | `backend/app/research/lgbm.py:187-212` 使用整个 Series 的 std，并将零 gain、单叶树作为独立拒绝理由；`model_workflow.py:341` 传入 test 分数。与设计 §11.2.1（819-846 行）的逐 valid 日期、float64 容差、可排名截面计数及错误码不符。valid 每天同分但不同日期值不同，可能漏过；valid 可排名而 test 同分，又会按错误门槛拒绝。 |
| 2 发布协议 | 成立，优先修复 | `model_workflow.py:415-464` 在 rename 前创建领域记录并提交 run 成功；`publication.py:205-210` 对已存在 final 直接返回，未验证 checksum。与设计 749-761 行固定协议冲突。publication 未持久化完整 manifest/result_metadata，缺少约定错误码，Task 成功也不在同一事务。 |
| 3 实验工具链越界/违反标签要求 | 不成立为严重缺陷 | `docs/design/qlib-lightgbm-direct-run.md:5,19,217` 明确追加独立同步实验路径并保留 07 产品行为；285 行明确 close(t+H+1)/close(t+1) 标签，296 行明确逐日训练。`docs/experiments/qlib-lightgbm-search-prompt.md` 明确搜索任务，`execution-2026-09-04.md:37` 记录继续探索要求。代码超出 07 单票不等于未经设计的功能。可讨论拆分 PR，但不能用产品周频要求否决独立日频实验。 |
| 4 推理契约 | 成立，属于契约交付缺口 | `execution_spec.py:107-116` 仅包含 feature_set/processors/label/fit_window/model_params，没有所要求的 bundle schema、模型格式和库兼容身份；未实现 resolve_inference 和对应错误码。后续推理消费属于 12，不能据此断言当前训练链已经不能运行。 |
| 5 UI 参数覆盖 | 成立 | `frontend/app/model-research-center.tsx:171-175` 请求不提交 model_params；250-278 行只有日期、seed 和锁定项说明，没有白名单参数输入及范围提示。与设计 949 行冲突。 |
| 6 warnings | 成立，需收窄措辞 | `model_workflow.py:355-378` 没有 zero_gain_features warning，但 385 行已有 summary 计数，不能说完全没有诊断。high_test_missing_rate 被 experimental 条件限制，Alpha158 若出现同样漂移不会告警；“40% 漂移”只是举例，不是本次实测数据。 |
| 7 参数重新编译 | 部分成立 | `execution_spec.py:138,174,198` 直接使用已存参数，没有按当前白名单/锁定项重新校验。**“指纹自校验恒真”不准确**：只改 learning_rate 而不改指纹会被拒绝，已有 `test_execution_spec.py:143-154` 覆盖。入口 `services/model_research.py:143` 已做白名单处理，未发现普通 API 绕过。真正缺口是旧参数在当前策略收紧后，或定义与 hash 一致但参数非法时，没有重新验证当前可信策略。 |
| 8 测试缺口 | 成立 | 未找到 CancellableLGBModel 与原版 LGBModel 的同输入逐位对照；seam 测试实际直接调用 lgb.train。已有发布恢复测试，但没有 final checksum 不同的冲突注入。已有动量对照和模型复现测试，但没有 v1/v2 bundle 的 06 动量等价断言。缺测试不直接证明对应功能错误。 |

### 发布协议的实际影响补充

`publication.py:264-268` 恢复发布只更新 publication；随后 `worker/runner.py:101-110` 把仍 RUNNING 的 Task 当孤儿标失败。模型复用 `worker/tasks.py:235` 的 recover_momentum_research，它在 run 已成功时直接返回（250-251 行），不修复 Task。因此 rename 前后崩溃、但 run 成功事务已提交时，启动恢复可能留下 **run succeeded / Task failed**。这不是单纯顺序偏好。

同时，`readable_artifact` 已用 committed 状态阻止读取尚未发布的产物。因此不应把当前实现描述成完全没有发布保护；它有保护，但未满足原子终态与冲突校验要求。

## Standards

这些主要是维护性判断，不应升级为硬性规范或运行错误。

| 指摘 | 判定与修正 |
|---|---|
| trial 流程重复 | 成立，但不是仅加载方式不同：还包含标签处理、索引与内存释放差异。`search_runner.py:437-560` 可抽公共流程。 |
| isinstance / legacy 推测性设计 | 部分成立。350 行判断 MatrixSegment，493 行判断 ShardedData，并非同一对类型。`test_experiment_search.py:123-134` 确实注入 tuple 数据走 legacy；它不是无消费者的死代码，而是为测试保留了第二套执行流程。 |
| `_restrict_to_universe` 重复 | 成立。`qlib_lightgbm_direct.py:522` 与 `dataset.py:199` 高度相似，但空数据处理与字符串转换不同，提取时须保留这些差异。 |
| `_build_universe` 重复 | 部分成立。direct 的 offsets 是 `{required,20,5,1}`，产品路径是 `(required_history_days,21)`；缺池处理、取消、进度和事务也不同。不能声称 policy 相同并原样合并。 |
| 前端 helper / ACTIVE / PHASES 重复 | api helper 确实逐字重复。ACTIVE/PHASES 并非逐字重复：模型多 training/predicting，中文阶段说明也不同。共享请求 helper 合理；阶段配置需保留领域差异。 |
| 六日期 Data Clumps | 合理但非必要的重构建议。Segment 还包含 name 和日历派生 observations，不能直接作为日期请求体替代品。改请求结构涉及公开 API 变更。 |
| 跨模块私有 helper | 成立，维护性问题。`services/research.py:243`、`search_runner.py:349` 等跨模块导入下划线函数，可改为显式共享接口。 |
| Middle Man / Protocol 合一 | 不构成有效缺陷。按 kind 路由 RankedScores 是合理 facade；两个 Protocol 的 create_run 参数类型、方法集合不同，直接合一会扩大契约。 |
| 短变量名与分号 | 成立，低优先级。fold、train_features 等能改善可读性；相关系数计算中的局部数学符号不必一律重命名。 |
| 领域词表缺口 | 部分成立。TrainedModel 等值得补充；`docs/agents/domain.md:35-41` 要求记录语言缺口，不等于所有技术类型都必须进 CONTEXT。 |
| bundle_builder 常量转发 | 事实存在，极弱建议。模块自身也消费这些常量，不是整个模块仅做转发；旧导入兼容亦有合理性。 |
| 生成物与忽略基线不一致 | 不成立为缺陷。handover README:10-21 明确保留报告、计划与身份 manifest；大模型、预测、每日指标仍在卷中。审计元数据与 40MB 原始结果并非同类，行数不能直接证明仓库策略矛盾。 |

未发现上述项目构成新的硬性标准违规。原报告关于 README enum 例外本分支新增、frontend/AGENTS.md 未修改的事实成立；本次未重新生成 API 客户端或执行数据库迁移，不把原报告的验证陈述当成本次执行结果。

## 验证范围

- 对照当前提交、设计、独立实验设计、实现和相关测试，Standards 与 Spec 分别复核。
- 执行 `backend/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider backend/tests/test_lgbm_seam.py`：**9 passed**，4 条依赖弃用 warning。
- AST 提取当前 `is_constant_model` 原函数体，以两日期各自同分 `[1,1]`、`[2,2]`，正 gain、多叶树替身运行：返回 `(False, [])`，验证逐日期常数漏检。此为函数级反例，不冒充完整训练实验。
- AST 提取当前 `_move_into_place` 原函数体，在临时 final 目录放置无关文件、不放 manifest：函数直接成功返回，验证没有冲突校验。此为文件操作函数级验证，未运行数据库恢复集成测试。

建议先修复 Spec 1、2；撤回 Spec 3 的严重缺陷判断；其余按上述边界保留。原报告中“PHASES 逐字重复”“Protocol 应合一”“指纹恒真”等表述应修订。
