# 持续对话与上下文压缩 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 实际状态以实施台账及验收报告为准，不能只凭本计划宣称通过。

**Goal:** 让用户在同一对话中继续未完成目标、确认对剩余任务的修改，并在上下文压缩和进程重启后保留要求、产物及交付记录。

**Architecture:** 在已有PlanService、LangGraph执行图、PostgreSQL检查点外增加会话目标协调。原要求续用原thread，改要求建立新thread并导入有效产物引用；已有压缩模块负责摘要，新装配器按调用目的提供上下文。

**Tech Stack:** Python、现有Web API、PostgreSQL/psycopg、LangGraph、React/TypeScript/Vite、pytest、现有Playwright浏览器测试。

**Spec:** [持续对话智能体与上下文压缩设计](2026-10-15-conversation-continuity-context-design.md)。

## Global Constraints

- 设计交付后用户已授权实施与测试；仅在隔离数据库和临时运行区验证，不自动执行真实模型或平台任务。
- 代码根目录`E:/AI/codex/redbook_agent`，工具根目录`tools/redbook_tools`；下文T/表示工具根目录，不是实际新增文件夹。
- 数据与测试临时文件位于`E:/AI/codex/redbook_runtime`；不在C盘安装依赖，不在项目根目录散落临时日志。
- 不更换Agent框架，不引入Redis，不复制新一套工作流；旧工作流、人工CLI、GUI保持可用。
- PostgreSQL不可用时显式报错，不降级SQLite/JSON执行账本；现有文件产物与平台回执存储保留。
- 不新增公开发布权限；禁止绕过平台风控、结果不确定保护、模型费用边界或图片策略的原有明确约定。
- 不新增120分钟或跨栏目共享任务预算；连接和单次操作超时不等于任务总截止时间。
- 每个现有函数修改前做GitNexus impact；CRITICAL/HIGH先报告。提交前detect_changes必须完整，无partial/truncated未解决结果。
- 每个任务先增加失败用例，再最小实现、回归、审查；仅在用户要求提交时分阶段提交，不擅自push。

## Review Focus

1. “再来3条”与“补齐3条”有歧义：前者澄清，不擅自增加目标；见任务2。
2. 用户确认时原worker刚保存了另一篇：刷新缺口，不把进度变化当作业务修订冲突，也不能多生成；见任务4。
3. 浏览器超时后用户再次发送继续：对账不确定回执，不重复上传；见任务4、8。
4. 压缩期间用户发来新要求：原文留在尾部且未确认前不改变执行；见任务6。
5. 历史摘要里仍有已删除要求：多轮压缩后不得恢复该要求或扩大授权；见任务5、6。

## 1. 文件与接口边界

生产代码仅新增四个模块：

| 路径 | 所有权 |
| --- | --- |
| `backend/conversation_service.py` | 消息分类、关联目标、生成差异、协调确认/恢复 |
| `T/src/agent/goal_store.py` | 三张PG增量表的访问、命令队列、投影、租约围栏 |
| `T/src/agent/conversation_control.py` | worker安全边界、修订迁移清单与导入 |
| `T/src/agent/context_builder.py` | 按用途装配上下文、预算和输入覆盖清单 |

类型放在所属模块，不再新增通用contracts/services目录。已有模型、计划和运行返回类型优先复用；下列新记录只承载本功能需要的字段。

| 模块 | 新增或扩展契约 |
| --- | --- |
| GoalStore | `snapshot(namespace, goal_id) -> GoalSnapshot`；`enqueue_turn(namespace, conversation_id, message_id, command, expected_goal_revision, request_key) -> TurnRecord`；`claim_turn(namespace, turn_id, worker_id) -> TurnClaim`；`record_result(claim, result) -> TurnRecord` |
| ConversationService | `handle_message(conversation_id, request, request_key) -> TurnResponse`；`confirm_turn(conversation_id, turn_id, expected_goal_revision, proposal_hash, request_key) -> TurnResponse` |
| conversation_control | `check_boundary(goal_id, attempt_id, stage, post_id=None) -> ControlDecision`；`build_resume_manifest(goal_snapshot, confirmed_plan) -> ResumeManifest`；`import_revision(manifest, new_run_id) -> InitialState` |
| ContextBuilder | `build(conversation_id, goal_id, purpose, model_limits, trigger_message_id=None) -> ContextBuildResult` |

`GoalSnapshot`含goal_revision、ledger_revision、当前计划、attempt及逐job计数。`TurnRecord`含accepted/applied状态和绑定修订；`TurnClaim`含lease_epoch。`TurnResponse`含turn、action、goal、assistant和可空的plan/run。控制决策为continue/pause/cancel/handoff；迁移清单含来源和目标修订、内容hash、保留/复核/不确定引用。ContextBuildResult含envelope、input_budget、manifest及ready/needs_compaction/too_large状态。

## 2. 实施任务

任务1至8已实施并完成隔离回归：智能体748通过，工具包1187通过、3项JSON专用组合跳过；真实平台验证未执行且不列为已通过。下列步骤保留TDD执行顺序，逐步记录见[实施台账](2026-10-15-conversation-continuity-progress.md)，验收与未验证边界见[验收报告](2026-10-15-conversation-continuity-context-test-report.md)。

### 任务1：目标账本、持久控制命令与缺口投影

**Files:** 新建`T/src/agent/goal_store.py`；修改`T/src/knowledge/store.py::ensure_schema`；新建`tests/test_conversation_goals.py`。

**Interfaces:** 产出上表GoalStore接口；复用既有计划记录、task_artifacts、delivery_state和草稿内容版本回执，不复制正文。

- [x] 增加`test_goal_projection_counts_current_versions_once`：目标10，核验交付6、合格待上传1，remaining=4、新生成数=3；相同post的旧版本及重复回执不增加数量。
- [x] 增加`test_goal_turn_idempotency_and_namespace`：相同请求键/相同内容返回原turn；同键不同内容409；跨namespace访问拒绝；两个worker仅一个认领成功。
- [x] 运行上述用例确认失败原因是功能缺失，不是数据库或依赖异常。
- [x] 实现三张增量表、约束、索引及短事务方法；goal_revision仅随确认业务修改变化，ledger_revision随有效产物/回执变化。
- [x] 增加`test_ledger_rebuild_after_projection_write_crash`与`test_legacy_goal_migration_is_conservative`：投影丢失可重建；历史归属不明确时不自动合并；迁移重复运行不破坏原表。
- [x] 运行`tests/test_conversation_goals.py`及`tests/test_plan_postgres_concurrency.py`，全部通过后审查事务边界。

### 任务2：自然语言继续、状态和明确的新任务

**Files:** 新建`backend/conversation_service.py`；修改`backend/app.py`、`T/apps/web_service.py`；新建`tests/test_conversation_continuity.py`。

**Interfaces:** 消费GoalStore.snapshot/enqueue_turn；实现handle_message和兼容现有messages响应的TurnResponse；复用原单句解析器处理真正的新目标。

- [x] 增加参数化用例：`继续`、`继续补齐剩下3条`、`继续完成10条每日新闻，已完成7条，不要重复`全部关联原goal；模型调用数为0；保留原目标10和原agent_run_id。
- [x] 增加`test_ambiguous_addition_and_multiple_goals_need_selection`：再来3条、多个可续接目标不自动执行；`今天另开一批10条`创建独立goal。
- [x] 增加`test_status_plus_continue_keeps_both_actions`与`test_active_or_completed_goal_does_not_spawn_duplicate`：混合消息不丢控制要求；运行中/完成后不另开worker。
- [x] 运行新文件确认失败后实现规则路由；查不到目标时保持原计划，返回澄清，不保存空计划覆盖活动目标。
- [x] 将原resume按钮入口映射同一会话服务；保留旧客户端content字段，增加client_message_id和expected版本的校验。
- [x] 运行新文件及`tests/test_run_resume_contract.py`、`tests/test_progress_api.py`，验证状态询问和GET无副作用。

### 任务3：可编辑的增量要求与模型校准

**Files:** 修改`backend/conversation_service.py`、`backend/plan_service.py`、`backend/task_recognition.py`；新建`tests/test_conversation_amendments.py`；扩展`tests/test_task_calibration.py`。

**Interfaces:** 实现confirm_turn；候选使用现有计划修订及proposal_hash，绑定goal/turn。校准复用现有RecognitionService，传原消息、已确认基线及新增上下文字段。

- [x] 增加`test_amendment_changes_remaining_only`：新闻7/10已交付；修改剩余主题和图片参考模式；未提到的AI资讯/地图、模型、交付边界完全保留；确认前0执行。
- [x] 增加`test_quantity_semantics_total_additional_remaining`：总数10不变、额外3变13、减少到5时保留已有7且不删除；歧义数字要求确认。
- [x] 增加`test_calibration_uses_user_context_not_regex_as_truth`：当前句只有“剩下改科技”，模型输入仍含原job；允许用户删除偏好；本地语义候选不作为模型输出硬标准。
- [x] 确认用例失败后接入设计第10节续接提示词及结构校验；模型不得创造目标ID、完成数、授权或直接调用工具。
- [x] 实现版本/差异确认和候选放弃；运行中修改请求先排入安全暂停，界面状态不冒充已生效；`revise`能力仅在任务4完成后开启。
- [x] 运行新文件和`tests/test_plan_contract.py`、`tests/test_plan_revisions.py`、`tests/test_task_calibration.py`，验证模型/用户修订不覆盖冻结原计划。

### 任务4：执行器控制边界、原thread恢复和修订交接

**Files:** 新建`T/src/agent/conversation_control.py`；修改`T/src/agent/editorial_agent.py`、`T/src/agent/artifact_store.py`、`T/apps/cli.py`、`T/apps/web_service.py`；新建`tests/test_conversation_handoff.py`。

**Interfaces:** 实现check_boundary/build_resume_manifest/import_revision；原版使用resume_agent_run，改版使用新agent_run_id。无goal_id的旧调用走原流程。

- [x] 增加`test_resume_reuses_original_pending_checkpoint`和`test_amendment_forks_thread_with_retained_artifacts`：原版保持thread；改版新thread，复用post_id，不重复执行完成栏目，不复制失效approved标记。
- [x] 增加`test_confirm_recomputes_deficit_after_inflight_receipt`：确认前新增1条回执时只更新ledger_revision和缺口；候选业务版本未改变，不错误冲突也不多产1条。
- [x] 增加`test_pause_between_batch_uploads`：正在提交的第1篇允许记结果，第2篇不得启动；只发暂停请求不硬杀浏览器。
- [x] 确认失败后实现每节点/逐稿/重试等待/平台写入前的控制检查；领域批处理回调为可选参数，不串行化原本安全的内容生成并发。
- [x] 实现迁移清单的有效性检查及失效范围；保持已交付数量、原日期、profile、内容版本；先确认旧attempt停下再启动新版。
- [x] 增加进程崩溃/双认领/租约失效/DB断连/保存成功但回执未落盘的故障用例；同一post已uncertain时必须先调用既有只读恢复，不重传。
- [x] 运行新文件及`T/tests/test_editorial_agent_retention.py`、`T/tests/test_editorial_agent_retention_postgres.py`、`T/tests/test_editorial_agent_postgres_resume.py`。验证真实PG检查点，不用纯mock代替恢复持久性。

### 任务5：按调用目的装配上下文与模型窗口预算

**Files:** 新建`T/src/agent/context_builder.py`；修改`T/src/agent/execution_context.py`、`T/src/agent/capabilities/context_usage.py`、`T/src/model_platforms/store.py`、`backend/task_recognition.py`；新建`tests/test_conversation_context_builder.py`。

**Interfaces:** ContextBuilder.build提供ContextEnvelope v2和预算；现有主控/校准/写稿输入构造调用它或其领域投影，不重复发送整份对话。

- [x] 增加`test_context_preserves_latest_changes_and_authoritative_state`：当前计划、宿主计数和未处理消息保留；旧摘要、网页注入、已撤回偏好不能覆盖它们。
- [x] 增加`test_context_scopes_writer_image_and_controller_inputs`：写稿只收自己的job/来源；无全对话或图像base64；主控可见缺口；工具调用与结果成对。
- [x] 增加`test_model_window_budget_and_unknown_limit`：按C-O-max(2048,ceil(.10C))计算I；缺少元数据用16384且标estimated；模型窗口变小重新预算；不可压缩输入太长返回明确错误，不截断。
- [x] 增加`test_usage_feedback_is_model_scoped_and_input_only`：只用同次请求的input usage校准；输出token不混入；不同模型不共享倍率；缺失usage保持估算，校准不能扩大窗口上限。
- [x] 失败后实现ContextBuilder；模型目录仅增可选context_window元数据并兼容旧registry，不更改默认模型/密钥保存流程。
- [x] 将校准/摘要等dispatcher外调用也接入既有context_usage审计；manifest存储真正发送的seq、快照、来源hash、估算/实际usage区别，不保存凭据。
- [x] 运行新文件和`tests/test_execution_context.py`、`tests/test_context_request_evidence.py`、`tests/test_model_selection_persistence.py`，以捕获的模型请求体验证覆盖，而非只检查快照存在。

### 任务6：自动压缩、并发提交和故障恢复

**Files:** 修改`T/src/agent/compaction.py`、`T/src/agent/conversation_store.py`、`T/src/agent/capabilities/context_service.py`、`backend/plan_service.py`；扩展`tests/test_compaction_accounting.py`、`tests/test_capability_context_api.py`；新建`tests/test_conversation_compaction_v2.py`。

**Interfaces:** 保留compact_conversation旧调用；可选增加purpose/through_seq/expected_snapshot_version/prefix_hash/model_limits。ContextService返回已有operation_id，模型步骤前使用ContextBuilder检查需求，不在HTTP中循环等600秒。

- [x] 增加`test_auto_compaction_before_later_turn_request`：首次运行未超阈值，后续消息增长超过0.75I时触发；完成后目标不超过0.50I或明确报告不可压缩固定输入。
- [x] 增加`test_tail_append_does_not_invalidate_prefix_snapshot`：压缩中新增消息仍在尾部；目标已确认修改或记忆撤回时候选失效；最多自动重算一次，无无限竞争。
- [x] 增加`test_cancelled_requirement_stays_cancelled_after_two_compactions`：压缩两轮后删除偏好不复活，仍只存草稿，原目标数不被摘要改写。
- [x] 增加`test_summary_failure_keeps_old_snapshot_and_checkpoint`：最多两次摘要尝试；失败但输入仍容纳时业务继续；超限仅暂停依赖的模型步骤；context-length错误只允许一次紧急压缩重试。
- [x] 增加`test_compaction_target_is_not_a_business_gate`：固定输入占0.60I、完整请求占0.80I时，虽未达到0.50I目标仍允许发送；记录目标不可达，不再次循环摘要。
- [x] 增加`test_compaction_snapshot_cannot_advance_execution`：摘要声称已上传但无回执时不增加完成数；摘要缺失可以重建，不整批重做；accepted未applied的消息重启后仍待消费。
- [x] 失败后实现第9节前缀分组、分块摘要、CAS、去重及提示词v2；原始消息不删除；长工具结果改可读artifact引用，不能裁掉未处理用户输入。
- [x] 扩展新对话默认auto、已有明确manual保留；老快照读取适配；operation重启中断可重试，不复用不可重放的旧callback。
- [x] 运行新增文件及既有压缩/API用例；验证强制压缩和手动压缩不会生成新闻、启动上传或偷偷调用备用付费模型。

### 任务7：GUI中的连续交互、差异确认和上下文状态

**Files:** 修改`frontend/src/App.tsx`、`api.ts`、`RunProgress.tsx`、`PlanEditor.tsx`、`TaskCalibration.tsx`、`capabilities/Memory.tsx`及现有样式；新建`tests/test_conversation_continuity_browser.py`。

**Interfaces:** 消费TurnResponse与goal状态；原API事件/poll机制复用，不加第二条实时通道；计划可空时保持已有计划展示。

- [x] 编写headless浏览器用例：7/10目标发送继续、收到202、显示“已接收/等待/已应用”的正确区分；不出现空计划、不二次启动。
- [x] 编写差异确认/取消/多目标选择用例；文本显示新旧值、remaining范围；用户可继续改字段，校准按钮不会自动执行。
- [x] 编写刷新/重开页面用例：已接收未应用的turn仍可见，按钮状态来自服务端；重复点击同幂等键不增加worker。
- [x] 失败后完成局部UI；新增状态用现有Lucide图标和tooltip，计数不依赖LLM文案。
- [x] 编写压缩事件用例：开始/完成/失败、coverage、实际/估算token均能查看；未收到完成事件不得显示完成。
- [x] 运行浏览器新用例及`tests/test_progress_browser.py`、`tests/test_plan_editor_browser.py`；运行frontend的`npm run build`。桌面1366×900与移动390×844截图，验证长中文、错误信息、按钮不遮挡。

### 任务8：回归、性能、故障注入与上线闸门

**Files:** 扩展`tests/test_execution_safety_regressions.py`；新建`tests/test_conversation_continuity_integration.py`；更新README的对话续做说明；记录测试报告到runtime的data/reports，不覆盖本设计。

**Interfaces:** 使用真实PG、实际Web API与执行图，模型和小红书适配器可控替身；仅经后续用户授权才进入真实模型/平台验证。

- [x] 实现集成用例：10+1+1目标首轮新闻7篇交付、2篇保留、1篇未生成；重新进入原PG检查点后一句继续只生成1篇并交付剩余3篇，其他栏目不重跑。
- [x] 实现修订集成用例：暂停后修改剩余主题/参考门槛，确认，新thread接管；旧thread不得继续写；跨日过期稿重新核验，不充当新日交付。
- [x] 注入故障点：提交命令后未启动、产物保存后投影未更新、平台提交后回执前、摘要生成后快照提交前、进程重启时双worker竞争。断言不丢消息/合格产物、不重复已核验交付、不报告虚假完成。
- [x] 完成下节隔离矩阵（C26保留为后续授权实机验证）；回归旧CLI/工作流无goal模式、模型默认值、图片参考模式、查重和平台串行锁；不得将单元测试通过描述为真实平台已上传。
- [x] 每项预热10次后采集100次本地状态/继续请求，排除真实LLM与平台网络，p50/p95与改前基线并列；本地分类p95<500ms，持久化接收p95<1s；未通过减少正确性校验达标。
- [x] 记录受控恢复额外模型调用数、重复生成数、重复上传数及事件应用延迟；输入token使用受控provider usage回归和持久化审计验证，未把估算冒充真实MiniMax用量。边界等待包含在途网络操作，不承诺瞬时停止或固定业务完成分钟数。
- [x] 按“原要求续接 -> 修订交接 -> 自动压缩”顺序实施验证；复用续接总开关及单对话压缩策略，正式部署未重启。回滚先暂停新版attempt并保留状态。重新完整检测图变更与git diff，无截断结果，不擅自提交。

## 3. 测试矩阵与判定

| ID | 条件 | 必须观察到的结果 | 所属任务 |
| --- | --- | --- | --- |
| C01 | 7/10后一句继续 | 原goal/thread，目标仍10，无额外规划调用 | 2、8 |
| C02 | 保留2、缺1 | 只生成1，优先上传合格2 | 1、8 |
| C03 | 已完成/运行中再次继续 | 可读回答，0重复worker | 2 |
| C04 | 多目标、模糊加量 | 澄清，0执行 | 2 |
| C05 | 状态+继续混合一句 | 两动作均保留 | 2 |
| C06 | 用户自述完成数与实际不符 | 报告证据差异，不篡改账本 | 1、2 |
| C07 | 未确认主题/门槛修改 | 原冻结参数不被覆盖，安全暂停已排队 | 3、4 |
| C08 | 已交付7后改剩余 | 前7保留，新条件仅影响剩余 | 3、4 |
| C09 | 修改一个栏目 | 已完成其他栏目不重做 | 4、8 |
| C10 | 原图有pending节点 | 修改后新thread，不往旧节点注入新jobs | 4 |
| C11 | 修改模型/交付或降低数量 | 差异确认，旧稿不删除，不扩权 | 3 |
| C12 | 确认时新增回执 | 最新缺口，不误判业务修订冲突 | 4 |
| C13 | 两浏览器同一操作/超时重发 | 同键同结果，不启动两次 | 1、7 |
| C14 | PG断连/租约过期 | 停止新增写入，无降级执行 | 4、8 |
| C15 | 上传结果不确定 | 只读对账后再决定，非盲重传 | 4、8 |
| C16 | 跨日或归属不明旧稿 | 新鲜度复核/澄清，不凑数 | 1、8 |
| C17 | 连续消息超过阈值 | 后续请求前也自动压缩 | 6 |
| C18 | 压缩时新增消息/已确认修改 | 尾部保留/过期候选不激活 | 6 |
| C19 | 历史删除偏好、网页指令注入 | 不复活、不升级权限 | 5、6 |
| C20 | 工具对、超长日志或图像 | 调用对应正确，可检索引用，非全量重复输入 | 5、6 |
| C21 | 模型窗口未知或减小 | 显示估算、重算，必要时显式阻塞 | 5 |
| C22 | 最新输入本身过长 | 不静默截断，不遗漏关键要求后执行 | 5、6 |
| C23 | 摘要失败/上下文错误 | 有界重试、旧快照保留、不换付费API | 6 |
| C24 | 重启压缩/重启交接 | 完整提交才生效，消息/产物不丢 | 4、6、8 |
| C25 | UI刷新、窄屏、长中文 | 状态真实、上下文可查、无重叠 | 7 |
| C26 | 同一目标实机续做 | 只读回执可验证，未核验不宣称成功 | 8及后续授权验证 |
| C27 | 固定输入超过压缩目标但请求仍容纳 | 允许业务请求，不为压缩率重复摘要 | 6 |
| C28 | 多模型usage反馈或无usage | 校准隔离、只算input、未知仍标估算 | 5 |
| C29 | 摘要与检查点/回执不一致 | 执行与完成数以持久业务证据为准 | 6、8 |
| C30 | 重启时消息已接收但未生效 | 原turn恢复，不丢失、不重复认领执行 | 4、6、8 |

验收不是“永远成功”：信源不合格、额度不足、平台限制与无法核验的写入仍应明确报告。目标是可恢复故障不整批重做，有效内容和用户要求不因新消息/压缩丢失，未达目标不冒报完成。

## 4. 验证执行方式

以下是实现后的复现命令；实际结果以验收报告和完整日志为准。从独立智能体根目录启动pytest，使根或工具包`conftest.py`创建E盘的临时真实PG数据库；不对正式数据库写测试数据，不实例化正式runtime的Workbench。两套完整测试须分别运行，避免历史同名模块在单一收集中的冲突。

```powershell
Set-Location 'E:\AI\codex\redbook_agent'
$env:PYTHONPATH = "$PWD;$PWD\tools\redbook_tools"
$env:REDBOOK_TEST_POSTGRES = '1'
& .\.venv\Scripts\python.exe -m pytest tests/test_conversation_goals.py tests/test_conversation_continuity.py tests/test_conversation_amendments.py tests/test_conversation_handoff.py tests/test_conversation_context_builder.py tests/test_conversation_compaction_v2.py tests/test_conversation_continuity_integration.py -q
```

```powershell
Set-Location 'E:\AI\codex\redbook_agent'
$env:PYTHONPATH = "$PWD;$PWD\tools\redbook_tools"
$env:REDBOOK_TEST_POSTGRES = '1'
& .\.venv\Scripts\python.exe -m pytest tests/test_conversation_continuity_browser.py tests/test_model_selection_persistence.py tests/test_run_resume_contract.py tests/test_execution_safety_regressions.py tests/test_context_request_evidence.py tests/test_compaction_accounting.py tests/test_capability_context_api.py tools/redbook_tools/tests/test_editorial_agent_retention.py tools/redbook_tools/tests/test_editorial_agent_retention_postgres.py tools/redbook_tools/tests/test_editorial_agent_postgres_resume.py -q -rs
```

```powershell
Set-Location 'E:\AI\codex\redbook_agent\frontend'
npm run build
```

真实PG恢复用例不得skip；环境不可用应报告阻塞，不把跳过视为通过。浏览器测试复用已有headless配置和已安装浏览器，不下载到C盘；mock所有模型/平台副作用。新集成测试若自行启动HTTP服务，必须绑定空闲端口并使用临时runtime，结束后关闭自己的进程，不能复用并重启8786正式服务。

只有用户另行授权真实运行后才进行真实模型/专用profile验证：先选1个受控目标，在生成后、上传前暂停，再用一句继续；核对内容版本与回执，期间不公开发布。通过后才执行用户指定的大批量真实任务，不以反复上传正式账号作为单元测试。

## 5. 本次实施与验收状态

- 已将实现分成目标存储、消息路由、增量候选、执行交接、上下文装配、压缩、GUI、集成验证八个可检查单元。
- 每个单元包含失败用例与回归范围；Review Focus五项均有对应测试。
- 代码路径以当前独立项目为准；新增文件已随实施创建，不能凭文件存在宣称测试通过。
- 本设计只依赖现有技术栈；隔离PG/API/GUI与性能验收已完成，真实模型和平台一次性稳定续做仍需另行授权测量，不能由隔离测试推断。
