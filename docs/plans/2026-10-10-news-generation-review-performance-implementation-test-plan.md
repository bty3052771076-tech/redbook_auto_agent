# 新闻生成与审核提速实施与测试计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development（用户选择委派后）或 executing-plans 逐任务实施。每步使用复选框；当前仅完成设计，以下实施和测试均未执行。

**Goal:** 减少生成/审核返工，支持按篇复用与串行交付，在不降低真实性、时效性和查重标准下缩短 10 条新闻 + 1 篇 AI 讯息的完整交付时间。

**Architecture:** 现有 LangGraph 保留计划和恢复职责；生成工具内部按证据、正文、场景、图片、交付拆阶段，PostgreSQL 保存阶段产物和调度状态。先实现定向修复与复用，再启用共享模型队列、栏目并发和唯一平台消费者。

**Tech Stack:** 现有 Python、pytest、Pydantic/dataclasses、psycopg、LangGraph、Playwright、React/Vite；不安装新依赖，不引入 Rust/Redis。

**Spec:** [总体设计](2026-10-10-news-generation-review-performance-design.md)。实施前完整阅读两份文件。

## Global Constraints

- 本文路径 A 为 `E:/AI/codex/redbook_agent/tools/redbook_tools`；B 为 `E:/AI/codex/redbook_agent`；W 为 `E:/AI/codex/redbook_workflow`；R 为 `E:/AI/codex/redbook_runtime`。前端路径相对于 B，其余明确标 A/W。
- 普通正文 150–220 字，复杂事件按既有证据规则最多 300 字；评价 20–40 字、最多 40 字；重新总结，不截句。
- 每日 AI 最终仅发布当天及前一天；事实、日期、同批/历史去重、权限、费用和用户硬要求不放宽。
- MiniMax 文本组初始上限 5、生图上限 2，同账户的全部栏目及进程共享；供应商更严时服从更严限制。平台同 profile 会话数/写入并发均为 1。
- PostgreSQL 真实集成必须通过；数据库不可用不降级。日志、缓存、截图、基准输出放 R/data，不放根目录，不记录 API-key。
- 不恢复 120 分钟共享预算；请求超时和阶段修复次数不等于整批任务截止。
- 测试默认离线/无平台写入。实机测试单独确认费用策略与指定 profile，仅保存草稿，不公开发布，不删除现有草稿。
- 不新建隔离工作树；保留现有未提交改动，不改 .env，不自动 commit/push。每个可交付阶段是评审点；若用户要求提交，先完成图变更检查。

## Review Focus

1. 同一数值但不同实体/伤亡角色/日期含义不能误放行；普通跨语言等价表达不能误杀。归属任务 2。
2. 场景或评价修复意外改动已审正文，导致二次事实错误或缓存污染。归属任务 3、4。
3. 两栏目临时环境、上下文和账户队列相互串扰，5 个槽变成每栏目 5 个。归属任务 5。
4. 保存成功后进程崩溃，恢复造成重复草稿；租约过期旧工作者晚返回覆盖新状态。归属任务 4、6。
5. 时间变快但 AI 只剩一条、官方覆盖下降、旧闻进稿或硬要求被悄悄软化。归属任务 7、8。

## 运行约定

以下命令供之后实施使用，本次不要执行。

```powershell
$Python = 'E:\AI\codex\redbook_agent\.venv\Scripts\python.exe'
$Agent = 'E:\AI\codex\redbook_agent'
$Tool = Join-Path $Agent 'tools\redbook_tools'
Set-Location $Tool
# 以下任务中的 pytest 路径以 $Tool 为当前目录，B/tests 除外
```

不要把密钥加载命令或配置文件内容打印到终端日志。各任务开始前做 GitNexus context/impact，索引过期先更新；HIGH/CRITICAL 必须说明影响边界。W 需要单独索引和回归，不能用 A 的结果替代。

## Task 1: 计时契约与固定样本基准

**Files:**
- Modify A:`src/workflow/performance.py`、`src/workflow/model_queues.py`。
- Create A:`src/workflow/news_contracts.py`、`tests/test_news_pipeline_telemetry.py`、`tests/fixtures/news_pipeline/manifest.json`。
- Create B:`scripts/benchmark_news_pipeline.py`、`tests/test_news_benchmark_contract.py`。

**Interfaces:** `StageContext`、`ReviewIssue`、`EvidenceBundle`、`TextArtifact`、`SceneArtifact`、`ReadyArtifact` 的字段按设计第 5 节；`request_span(context, stage, *, request_id, attempt, provider, model)` 记录排队和服务区间；`summarize_performance(events) -> dict` 按根任务聚合、按 request_id 去重。

- [ ] 建立 10-10 原运行只读基线 manifest：记录作业 ID、完整日志 hash、模型/策略、缓存状态及失败交付。保存本地新闻样本仅限测试需要片段，不把私有账号数据或完整付费原文提交仓库。
- [ ] 写失败测试 `test_overlapping_spans_do_not_sum_into_wall_time`、`test_resume_wait_is_not_active_runtime`、`test_retry_and_request_counts_are_distinct`、`test_secrets_are_redacted_but_token_counts_remain`；断言父子 span/并发事件不会重复累计。
- [ ] 运行 `& $Python -m pytest tests/test_news_pipeline_telemetry.py -q`，确认因缺少契约失败，不因网络/密钥失败。
- [ ] 扩展现有记录器，添加 monotonic 耗时与 UTC 关联；不复用候选 deadline 推算全部阶段的起点。记录未完成 span，不能把进程崩溃到重启的离线时间算服务耗时。
- [ ] 编写基准入口：`--fixture-only --output <R内目录>` 默认拦截外部 HTTP/平台；真实模式必须显式 `--live --delivery save_draft` 并使用已确认计划。冷/热缓存使用隔离 namespace，不清空生产库。
- [ ] 重跑上述测试及 B:`tests/test_news_benchmark_contract.py`。产物：可比较的请求、阶段与整批时间报告，不宣称优化已经生效。

## Task 2: 可追溯事实包与数字核验

**Files:**
- Create A:`src/workflow/news_evidence.py`、`tests/test_news_evidence_bundle.py`、`tests/test_news_numeric_alignment.py`。
- Modify A:`src/workflow/create_post.py` 中证据构建、`_daily_news_has_unsupported_numeric_claim` 的适配入口。
- Regress A:`tests/test_news_numeric_lead_integrity.py`、`test_news_person_numeric_claims.py`、`test_news_exact_lead_diagnostics.py`。

**Interfaces:** `build_evidence_bundle(item, *, snapshot, context) -> EvidenceBundle`；`review_numeric_claims(text, bundle) -> list[ReviewIssue]`；`validate_claim_refs(refs, bundle) -> list[ReviewIssue]`。数值比较使用 Decimal，引用区间基于不可变的已清洗原文版本，不把生成文案当来源。

- [ ] 将原 54 次数字拒绝按原文 hash/候选/阶段去重，人工标注正确拒绝、误判、证据不足；保留原记录，标注不确定项不计作正确通过。
- [ ] 写用例：`30,000 bottles -> 3万瓶` 合法；近/至少变成精确、伤者变死者、计划变已发生、同数值跨实体、公告日期变事件日期均拒绝；中文/英文角色、万/亿、百分比与百分点分别覆盖。
- [ ] 运行 `& $Python -m pytest tests/test_news_evidence_bundle.py tests/test_news_numeric_alignment.py -q`，记录红测。
- [ ] 实现 supported/contradicted/unresolved。未识别表达可请求一次带原文引用的定向核对，但不能覆盖确定性矛盾；引用不存在或仍无支持则不通过。按句覆盖全部数字断言，不能只校验模型自报的 claim_refs。
- [ ] 接入首稿的事实包输入；尽量确定性构建，不为每候选固定增加一次 LLM 请求。
- [ ] 重跑新测试及三组原回归。旧用例只有经人工重新标注、证明是误判才修改期望；不能为绿测删除反例。

## Task 3: 正文、评价与场景定向修复

**Files:**
- Create A:`src/workflow/news_repairs.py`、`tests/test_news_scoped_repair.py`。
- Modify A:`src/workflow/create_post.py` 的 `_prepare_daily_news_candidate`、现有图文提示词适配；`src/llm/generate.py` 的结构化响应适配。
- Reuse A:`src/news/length_policy.py`、现有场景源安全校验。

**Interfaces:** `plan_repair(artifact, issues, *, context) -> RepairRequest | None`；`apply_repair(artifact, patch, *, request) -> TextArtifact | SceneArtifact`；`RepairRequest` 包含 scope、allowed_fields、base_hash、immutable_hashes、evidence_spans、attempt。

- [ ] 写失败测试：仅场景失败时正文 hash 和文本模型写稿计数不变；仅评价过长不改正文；超长正文只重总结一次且句子完整；越权 patch/错误基版本被拒绝。
- [ ] 运行 `& $Python -m pytest tests/test_news_scoped_repair.py -q`。
- [ ] 按设计第 6 节落地首稿与定向修复提示词；文本修复共 1 次、场景修复 1 次；一次 patch 同时修多个文本问题也只消耗一个文本修复次数。
- [ ] 使正文通过产物先落地，场景重试不能触发正文生成；确定性概念构图仍走相同事实约束，失败替换该候选，禁止转成库存图静默兜底。
- [ ] 回归 `test_news_scoped_repair.py`、`test_news_writer_scene_format.py`、`test_news_scene_source_safety.py`、`test_news_scene_grounding.py`、`test_news_scene_contract.py`、`test_optional_image_score.py`。
- [ ] 检查每个拒绝记录都有 scope 和原始原因；reference 模式低分不产生 scene/text 错误，不伪造满分。

## Task 4: PostgreSQL 阶段缓存和恢复

**Files:**
- Create A:`src/workflow/stage_store.py`、`tests/test_news_stage_cache.py`、`tests/test_news_stage_store_postgres.py`。
- Modify A:`src/agent/artifact_store.py`、`src/workflow/news_discovery.py`、`src/workflow/review_cache.py`。

**Interfaces:** `NewsStageStore.claim_stage(key, *, owner, lease_seconds) -> StageLease | None`；`commit_stage(lease, *, output_hash, artifact_ref) -> bool`；`get_cached(key) -> StageArtifact | None`；`pending(root_run_id, stage) -> list[WorkItem]`。键、CAS version、lease_epoch 和表字段遵循设计第 7 节。

- [ ] 写缓存命中/失效测试：原文、标题正文、模型配置修订、规则、权限命名空间、日期资格变化；只改评价不重采原文，改变正文事实必须失效场景和相关图片审核。
- [ ] 写真实 PostgreSQL 竞争测试：两个 worker 同键只有一个领取者；旧 epoch 晚返回不覆盖；数据库断开不退回 SQLite/内存；跨账号不能读取缓存；同键不同产物报冲突而非覆盖。
- [ ] 先运行缓存测试和 PostgreSQL 组，缺少数据库明确记 BLOCKED，不把 skip 视为通过。
- [ ] 使用现有 migration 角色建立 news_stage_cache、news_work_items 两张表和必要索引，应用角色最小授权；网络调用在事务外；同键 single-flight，错误负缓存绑定原因和有效期。容量租约表归任务 5。
- [ ] 实现正文/场景/图片阶段恢复和来源 304 复用；新鲜度资格每次交付前独立重查。外部结果未知不因为租约超时就自动再调用计费服务。
- [ ] 运行新组及 `test_editorial_agent_retention_postgres.py`、`test_agent_postgres_retention_integration.py`、`test_review_retention.py`、`test_image_repair_resume.py`。产物：已完成阶段恢复时新增模型调用为 0。

## Task 5: 配置隔离与共享模型容量

**Files:**
- Modify A:`src/workflow/model_queues.py`、`src/workflow/pipeline.py`、`src/model_platforms/runtime.py`、`src/model_platforms/integration.py`、`src/agent/execution_context.py`、对应 LLM/图片适配器。
- Modify A:`src/agent/plan_contract.py`；B:`backend/plan_service.py`、`backend/task_recognition.py` 的冻结字段适配。
- Create A:`tests/test_shared_model_capacity.py`、`tests/test_parallel_job_config_isolation.py`。
- Create A:`src/workflow/model_capacity.py`，负责设计中的 model_request_leases 迁移和跨进程容量，不复制 Postgres 连接池。

**Interfaces:** 在 StageContext 关联的不可变 runtime 中传模型/评分/代理绑定；`ModelWorkQueues` 接收共享 limiter；`ModelCapacityStore.acquire(group_key, request_id, *, modality, deadline_at)` 返回有 fencing 的容量租约；`generation_pipeline_version` 仅接受 legacy/staged_v2 并随计划冻结。quota_group_id 分组与未知账户保守归组按设计第 8 节。

- [ ] 写新闻/AI 两栏目使用不同模型与代理的并发测试，断言配置始终隔离；测试并发路径不写 `os.environ`。
- [ ] 写两个进程共享同组文本峰值 <=5、生图 <=2，以及更严格账户总上限；429 退避时其他组继续，明确额度耗尽暂停相关组。未知远端请求不得因 TTL 立即释放并重复派发。
- [ ] 写同账户连接别名、未知分组保守共享、进程失联后仍在远端执行、迟到结果与组上限冲突测试；分开报告本地派发并发和无法证明的远端并发。
- [ ] 红测后显式传入运行时；旧适配器尚不能接收配置的路径保持串行，不能假装已安全并发。复用能力 dispatcher 的权限上下文和 request 证据。
- [ ] 实现新闻:AI=3:1 的可借槽轮询；待办 AI 不饥饿，没有 AI 时新闻可用全部 5 槽。不得在模型池 worker 内等待同池新 future。
- [ ] 测试 late-response、用户停止、请求超时、能力撤销/新任务更新不污染冻结任务；追加 `test_model_queue_cooperative_stop.py` 与 B:`tests/test_execution_context.py`、`tests/test_plan_capability_freeze.py` 回归。
- [ ] 通过前跨栏目并发保持关闭。此任务不自行扩到 10/20 并发，不改变费用和供应商优先级。

## Task 6: 按篇流水线与唯一平台消费者

**Files:**
- Create A:`src/workflow/news_execution.py`、`tests/test_news_pipeline_delivery.py`、`tests/test_news_pipeline_resume.py`。
- Modify A:`src/workflow/create_post.py`、`src/agent/editorial_agent.py`、`apps/cli.py`。
- Modify A:`src/publish/playwright_steps.py` 的会话适配、`src/publish/delivery_state.py` 的外层锁接入；保留 `draft_recovery.py` 的核对约束。

**Interfaces:** `run_news_pipeline(jobs, *, context, stage_store, queues, on_ready) -> PipelineResult`；`enqueue_ready(artifact: ReadyArtifact) -> None`；消费者从持久化 READY 待办读取，使用现有 delivery action key 和 reconciliation。LangGraph 由单一协调者提交状态。

- [ ] 写三篇测试：一篇拒绝不影响另两篇入队；首篇可在后篇生图期间保存；多生产者写入峰值为 1；READY 提交后通知丢失仍可被消费。
- [ ] 写跨进程同 profile/同账号锁测试；持锁进程死亡后新持锁者先核对 submitting/uncertain 动作。Playwright 对象只能在专用线程中使用。
- [ ] 写统一账号/profile 取锁顺序、锁连接断开、原子文件落地后元数据提交失败、跨日恢复过期稿的测试，禁止死锁、失锁后新增写入或给旧闻改当天日期。
- [ ] 红测后添加渐进 on_ready，不要求整批成稿。生图前预留事件/目标槽，原子处理并发重复，晚到备选保留但不多传；最终缺口不能被部分成功掩盖。
- [ ] 将平台读写纳入唯一消费者，健康专用会话可复用；先保存回执和状态，再异步排队非关键知识库嵌入。必要去重信息不能延迟到嵌入完成才可见。
- [ ] 注入 5 个断点：正文通过后、图片生成后、READY 落库后、点击保存后但回执前、回读成功后但任务汇总前。检查恢复不重生成通过阶段、不重复草稿、未知写入先只读核对。
- [ ] 回归新组及 `test_agent_partial_delivery.py`、`test_editorial_agent_retention.py`、`test_draft_button_safety.py`、`test_draft_recovery.py`。平台禁用/验证码不允许自动消除，默认浏览器调用次数为 0。

## Task 7: AI 条目处理与可解释进度

**Files:**
- Modify A:`src/ai_digest/collect.py`、`src/ai_digest/generate.py`、`src/ai_digest/rank.py`、`src/workflow/content_evidence.py`（仅契约接入）、`apps/cli.py`、`apps/web_service.py`。
- Modify B:`backend/progress.py`、`frontend/src/RunProgress.tsx`、`frontend/src/api.ts`；W GUI 复用等价结构化阶段事件。
- Create A:`tests/test_ai_digest_incremental_summary.py`；B:`tests/test_pipeline_progress.py`、`tests/test_pipeline_progress_browser.py`。

**Interfaces:** `summarize_ai_items(items, *, context, evidence, queues) -> list[ItemSummary]`，每请求最多 6 个 item_id，缺项仅重试对应条目；progress 增加 text_approved/image_ready/draft_verified/repairing/rejected、首篇/全量时间与等待原因，旧字段兼容。

- [ ] 写不同厂商同批摘要不串事实、缺一个 ID 不重写整批、未来/过期/无日期不放行、重复转载合并、用户指定发布优先核验的测试。
- [ ] 写“生成1篇”仍保留全部满足冻结策略的条目、不能为了提速减少官方覆盖；官网超时显示未知，不显示“今天没有”。
- [ ] 红测后按条目证据摘要与局部修复，本地排版只接收净化后的展示字段。新闻和 AI 共享取回原文，不共享事件资格决策。
- [ ] UI 显示文本/图片/交付分别计数；两栏目不重复累计；恢复只读回执不计作新上传；图片参考低分与内容拒绝分开，历史已解决错误不作当前阻塞。
- [ ] 运行新组及 `test_ai_digest_comprehensive_sources.py`、B:`tests/test_partial_delivery_progress.py`；浏览器用本地模拟数据验证长错误信息、中文标题、桌面/窄屏无溢出，不自动打开平台或消费模型额度。

## Task 8: 两程序兼容、受控性能及发布闸门

**Files:**
- Port A 的公共模块、测试和 CLI 合约到 W 对应路径，按文件差异适配，不复制 runtime/data、.env、浏览器 profile、凭据或数据库。
- Extend B:`scripts/benchmark_news_pipeline.py`、B:`tests/test_news_benchmark_contract.py`。
- Add A:`tests/test_news_pipeline_compatibility.py`；更新两程序 README 中已实现且验证过的选项，不提前宣称提速。

- [ ] 先列 A/W 文件 hash 和差异，确认两边各自依赖与现有改动。测试通过后单独移植；禁止把 W 改为 import A 的绝对路径。
- [ ] legacy 默认和旧检查点原样运行；staged_v2 只对显式选择的新计划生效。缺少 PostgreSQL 时 v2 明确拒绝启动、不静默退回；legacy 原行为不在本计划中另行改变。
- [ ] 终端 smoke：A 的 `-m redbook_tools --help` 与 W 的 `-m apps.cli --help`；GUI 通过 mock backend 确认计划、阶段状态和恢复一致。测试启动脚本/导入根遵守现有包装器，不要求新增环境。
- [ ] 两环境分别跑任务 1–7 的相关回归和各自现有完整测试集，区分失败/通过/跳过；PostgreSQL 集成不得以离线 mock 代替。
- [ ] 固定材料、冻结策略、模型/代理、日期时钟和图片评分模式；对 legacy（含已修复的交付）与 staged_v2 各做 3 次冷缓存、3 次热缓存离线回放，交替顺序；记录全部失败、调用及关键路径。离线延迟回放仅证明调度收益，不等于真实供应商提速。
- [ ] 获得实机测试授权后，执行一次真实 10+1 的保存草稿验收；要统计稳定速度需再安排至少 3 组同条件、完整成功的配对模型试验，同时列出全部尝试的失败率。模型对照可只生成本地、不反复上传同批稿件；此时仅比较生成阶段，不能把单次平台耗时拼上去宣称已证实端到端降幅。两种生图供应商作为独立对照组，不能混入算法优化比较。
- [ ] 对同一次实机根任务安排受控中断并从 UI 恢复。回读全部 11 篇当前版本；分别统计端到端等待、活跃执行并集、暂停和模型重试，不把最终补传时间当整批生成时间。
- [ ] 内容金标全过、并发/恢复边界全过、实机交付 11/11 才进入灰度。只有受控性能目标也达标，才把新模式设为默认；否则保留可选，并报告瓶颈，不自动放松质量。

## 验收矩阵

| 层级 | 必须验证 | 成功条件 |
| --- | --- | --- |
| 事实金标 | 原 54 次拒绝归类 + 数字/主体/日期反例 | 正确拒绝不回归；误判修正可追溯；未知不伪装通过 |
| 定向修复 | 场景、评价、正文分别失败 | 仅允许字段变化；场景引发正文重写=0；无残句 |
| 参考图门禁 | 低分/不可用、损坏文件、明确错事件 | 参考分不拦；确定性安全错误仍隔离 |
| 资源与上下文 | 两栏目 + 两进程 + 不同配置 | 共享上限、无串扰、无饥饿、无死锁 |
| 真实 PostgreSQL | 缓存、CAS、租约、崩溃恢复 | 不降级、不覆盖新版、不泄漏其他账户 |
| 平台模拟 | 专用 profile、锁、uncertain、限流 | 写入峰值1、不盲重试、不公开发布 |
| UI/CLI | 计划版本、运行计数、警告和恢复 | 同一状态口径；无整批误拦和旧错误误导 |
| 完整实机 | 10新闻 + 1AI，指定 profile | 11份对应当前版本的保存和回读证据 |
| 受控性能 | 冷/热、失败、缓存和供应商分组 | 中位活跃耗时目标下降>=35%，质量/完整率不降 |

辅助目标：健康服务下 30–40 分钟完成 10+1、文本请求减少>=50%、有效事实包首稿通过率>=70%。这些均待测，不是硬中断阈值。不要用降低输出数量、延长旧闻窗口、隐藏失败、取消查重或自动付费实现目标。

## 交付、自检与回滚

- 阶段 A 为任务 1–4，先验证调用量和返工下降；阶段 B 为任务 5–8，再验证并行重叠与端到端收益。未通过 A 不先开启 B。
- 每次评审包含修改清单、graph impact、相关测试命令/结果、迁移回滚说明和新增外部副作用。提交前执行 GitNexus detect_changes，partial/truncated 不能作为干净检查。
- `generation_pipeline_version` 冻结后不会被 GUI 默认值变化覆盖。紧急回滚只停止新增 v2 任务；已有 v2 任务保留冻结解释器和阶段状态，或者显式暂停，绝不清库后重跑。
- 文档自检要求：设计章节均有实施任务，接口名称一致，5 个 Review Focus 均有具体测试，所有“预计/目标”与“实测”分开，日志和数据保留。
- 本计划不授权立即生成、上传、删除草稿或公开发布；实施及实机执行需要用户后续指令。
