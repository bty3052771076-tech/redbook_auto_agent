### 2026-10-24 README 与发布前检查

- DONE: 重写两仓 README，按功能表、局限/待修正、部署与使用组织；加入 7 张已检查的真实本机截图、操作框和目录/流程框。截图不包含凭据，演示计划未执行，真实生成/上传未重复运行。
- VERIFIED: 本地规则六组解析检查确认明确关键词、关于/围绕与图片仅参考可识别；自然口语与排除条件存在未提取情况，已如实写入文档。
- EFFICIENCY: 最新运行 556f329146694396a56f0c73975f665a，2026-10-24 11:45:04–12:24:52，39:47；主控/写稿 MiniMax-M3.1-Flash，生图 opencodex/gpt-image-2，resume_count=0；10 新闻 + 1 AI 集合稿，11 个唯一平台草稿均回读 verified。复用新鲜平台数据快照，图片硬评分关闭；不与昨夜补齐案例混算。
- VERIFIED: 工作流 1758 passed / 8 skipped；智能体主程序 783 / 1；内置工具包 1439 / 36；两前端构建通过。智能体与工具包分别执行，避免同名测试收集冲突；未使用新依赖。
- SECURITY: .gitignore 仅逐文件放行 README 的 7 张截图，本地文档/素材/记录保留；发布前对暂存内容做真实本地凭据比对和常见 token 检查。正式推送时先备份两仓远端原 main，使用正常快进推送。

### 2026-10-24 00:39 GMT+8 原任务续跑完成

- DONE: 原任务 `5652fe3b58a04804afb386f0480b6b86` completed，2/2 栏目完成；8 条原新闻保留，新补 2 条新闻及 1 条 AI 讯息。11 个唯一稿件均 saved_as_draft / readback verified，无公开发布，使用 runtime/data/browser/chrome-profile、headless=True。
- DONE: 两套 AI 工具在自适应模式、最终有限修复耗尽后可保留合格子集，审计被剔除项；不放宽固定数量、证据身份、事实、日期、指定主题及 800 字校验。
- VERIFIED: 独立工具 AI 模块 225 通过；工作流 AI 模块 431 通过；最后审计字段调整后两套定向回归各 36 通过。未跑本轮全仓测试，未安装依赖。
- RUNS: `5cceeecff282589b97faf633516aedf9` 23:21:22 至 00:18:04，安全暂停；`c05c57a6edda52548dbf5c1f556505f5` 00:19:02 至 00:39:35，正常完成。恢复总墙钟 01:18:12，实际两个进程累计 01:17:14，包含同步与维护，并非从零生成 11 条的纯生成时间。
- OUTPUT: 新新闻 `5c4d920ca7d84aa8995947f8fa7c578b`、`557aa79926e54a778f8f6336cfd4efd9`；AI `75ae7f51733740f68b0eb386700299f1`。AI 只有一项 10 月 23 日的词元服务性能报告，出刊日 10 月 24 日，无模型首发；保持原 10 月 23 日目标身份，不另开一批。
- LIMITATIONS: 旧日志 uploaded=29 是累计列表计数，唯一实际草稿为 11。AI 平台标题压缩仍有不自然措辞，未声称已消除全部质量问题。上条 BLOCKED 状态已被本记录取代。
- VERIFIED: 健康接口 ready，后台继续可用。两仓 diff --check 通过，图分析 critical，无 partial/truncated；未提交或推送。

### 2026-10-23 18:41 GMT+8

**Task:** 修复原任务 5652fe3b58a04804afb386f0480b6b86 的数字/场景误判、补位池缩小及 AI 整篇重试。

- DONE: tools/redbook_tools/src/workflow/create_post.py 修复数量翻译与年度/年代、保留正文的概念场景定向修复、具体数字反馈、补位池最小宽度及 AI 最终条目修复。
- DONE: tools/redbook_tools/src/ai_digest/generate.py 保留合格条目，exact-URL 绑定有限重试；异常携带部分成稿，修复后仍执行事实/日期及 800 字检查。
- DONE: 新增数值/场景、补位、AI 单条与工作流集成、启动状态恢复回归；设计记录见 docs/plans/2026-10-23-generation-stall-repair.md。
- VERIFIED: 新闻 327 通过，AI 模块 217 通过；保留/检查点 37 通过、3 跳过；启动恢复 12 通过。工作流对应 AI 模块 423 通过，补位 8 通过。各测试组有重叠，本轮未运行全仓全套。
- VERIFIED: 两仓 diff --check 通过；图分析包含之前未提交修改，critical，无 partial/truncated。保留已有工作，未提交、未推送、未安装依赖。
- BLOCKED: 后台服务启动被执行策略拒绝，已请用户手动启动 Start-Agent.cmd；8786 未可用，尚未恢复真实生成/上传。原 8 条草稿保留，缺 2 条新闻和 1 条 AI 讯息，不宣称完成。
- BACKUP: redbook_runtime/data/tmp/oct23-stall-backup-20261023-181227 保存原状态、日志、检查点。PostgreSQL 已启动，子智能体已关闭；本轮测试进程均结束。

### 2026-10-16 08:46 GMT+8

**Task:** 默认补充要求为选题偏好，并接通AI羊毛的实际偏好消费。

**Git:** main；本次修改尚未提交，未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| backend/task_recognition.py | DONE | 校准默认强度与福利栏目提示词 | 完整回归通过 |
| frontend/src/PlanEditor.tsx | DONE | 默认选中、编辑保留、保存传递强度；福利可编辑 | 构建和浏览器已通过 |
| tools/redbook_tools/src/agent/plan_contract.py | DONE | 空强度默认与福利字段接入 | 已保留硬要求保护 |
| tools/redbook_tools/apps/cli.py | DONE | CLI与智能体传递福利提示词 | 工具包回归通过 |
| tools/redbook_tools/src/wool/workflow.py | DONE | 消费并记录偏好 | 工具包回归通过 |
| tools/redbook_tools/src/wool/collect.py | DONE | 检索偏好与已核验福利排序 | 工具包回归通过 |
| tests/test_plan_contract.py | DONE | 未知强度与不支持评价视角的保护测试 | 已通过 |
| tests/test_requirement_strength_default.py | DONE | 后台、HTTP采用、浏览器保存刷新 | 针对性回归通过 |
| tools/redbook_tools/tests/test_wool_requirement_preferences.py | DONE | 真实传递与事实不变回归 | 已通过 |
| docs/plans/2026-10-16-plan-requirement-default-fix.md | DONE | 原因、边界、验证记录 | 已补齐完整测试结果 |

**Notes**
- Tests/Lint: 51项针对性回归通过；智能体后端765通过、1跳过（357.13秒）；工具包1193通过、36跳过；前端构建和浏览器验证通过。后补两项HTTP用例已在51项针对性回归中通过。
- Risks/Assumptions: 计划契约涉及校准/确认/冻结，高风险；不软化用户明确硬要求，不隐藏尚不支持的硬约束。
- Graph: 19个已跟踪变更符号、26条受影响流程，critical；完整回归已覆盖，未提交或推送。
- Next steps: 正常重启独立智能体后端并刷新页面使用新逻辑；本次不重启正式服务或触发用户任务。

### 2026-10-18 11:39 GMT+8

**Task:** 核查DeepSeek 1017升级漏报，修复模型发布优先。

**Git:** main；保留10月16日已有未提交修改，本轮未提交、未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/ai_digest/fetchers.py | DONE | 日期分节正文、过滤TOC、补DeepSeek关键词 | 全套回归通过 |
| tools/redbook_tools/src/ai_digest/rank.py | DONE | 版本升级识别、跨日期模型优先、修订版历史键 | 全套回归通过 |
| tools/redbook_tools/tests/test_ai_digest_changelog_releases.py | DONE | 7项行为回归含真实采集链 | 针对性全部通过 |
| docs/plans/2026-10-18-ai-digest-model-release-priority.md | DONE | 核对事实、原因、修复边界与验证 | 完整结果已记录 |

**Notes**
- Tests/Lint: 7项针对性通过；内置工具1200通过/36跳过；后端767通过/1跳过；原工作流1540通过/8跳过；diff --check通过。真实官网两天采集得到1017升级并置首，未生成帖子。
- Risks/Assumptions: 排序和历史查重为HIGH/CRITICAL影响范围；不放松日期、完整性、来源与查重规则。
- Next steps: 无运行任务时正常重启常驻服务，后续生成使用新逻辑；本次不重启正式服务、不生成上传。旧草稿不会自动替换。

### 2026-10-19 13:24 GMT+8

**Task:** 排查AI讯息来源集中导致的无限恢复，统一选稿与终审，修正RSSHub按需启动慢等待。

**Git:** main；保留已有未提交修改，未提交、未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/workflow/create_post.py | DONE | 单源自适应单条、明确多条不足、模型前/最终来源检查 | 实际续跑并完成草稿上传 |
| tools/redbook_tools/src/agent/editorial_agent.py | DONE | 来源失败单独计数、连续无进展保护、高影响材料不足保护 | 断点不重跑回归通过 |
| tools/redbook_tools/src/ai_digest/rsshub_local.py | DONE | 直连healthz、失败/占用检测、有界启动与自有子进程清理 | 4项新增测试通过 |
| tools/redbook_tools/tests/test_ai_digest_source_alignment.py | DONE | 单源/多源/硬最低数量/真实生成与终审契约 | 已通过 |
| tools/redbook_tools/tests/test_agent_source_recovery.py | DONE | 有界来源恢复、材料不足、续跑不重传、不同错误不串计数 | 已通过 |
| tools/redbook_tools/tests/test_rsshub_startup_bounds.py | DONE | 实际HTTP健康探测与启动生命周期 | 已通过 |
| tools/redbook_tools/apps/cli.py | DONE | 规范化来源身份与流式历史审核入口 | 全套与实机续跑通过 |
| tools/redbook_tools/src/storage/files.py | DONE | 新增iter_posts，保持list_posts兼容 | 存储及全套回归通过 |
| tools/redbook_tools/src/workflow/quality_gate.py | DONE | 逐篇历史比较，仅保留首个匹配ID | 保留过滤及输出语义 |
| tools/redbook_tools/tests/test_streaming_history_review.py | DONE | 存储兼容、坏文件与历史对象存活回归 | 20个降至最多2个同时存活 |
| tools/redbook_tools/tests/test_cli_agent_non_news_repair.py | DONE | 历史交付审核fixture改用实际迭代入口 | 完整回归通过 |
| docs/plans/2026-10-19-ai-digest-source-recovery-fix.md | DONE | 根因、RSSHub、审核内存、计时和最终平台回执 | 14:26补齐最终验证 |

**Notes**
- Tests/Lint: 最终内置工具1218通过/36跳过（82.14秒）；后端767通过/1跳过（413.23秒）；原工作流1554通过/8跳过（679.38秒）；针对性回归全部通过。
- Risks/Assumptions: 共用选稿、存储和审核路径HIGH/CRITICAL；不放松两天日期、事实、查重或明确最低数量；内部恢复函数UNKNOWN已核对图节点注册与跳转。
- Runtime: 原任务最终completed、jobs=2/2、uploaded=11；10篇新闻未重生成/重传，AI稿c4198d805add4da9aef13826332fe80a回读title/body=True、images=2/2；专用profile草稿，未公开发布。
- Timing: 最终续跑3分51.201秒、其中质量审核59.204秒；仅指保留稿续审上传，不冒充整轮生成时间。独立历史键扫描148.078秒仍是后续优化项。
- Next steps: 本轮无剩余执行任务。用户批准的py-spy未安装；未新增依赖或C盘安装；保留原始失败日志及已有修改，未提交或推送。

### 2026-10-19 15:17 GMT+8

**Task:** 按用户要求取消每日AI讯息的来源配额，并只重生成今日AI草稿。

**Git:** main；保留已有修改，未提交、未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/ai_digest/rank.py | DONE | 默认不设单源上限，显式底层参数保持兼容 | 新行为及全套回归通过 |
| tools/redbook_tools/src/ai_digest/generate.py | DONE | 同源多事件可完整改写、恢复与兜底；移除提示词来源硬配额 | 全套回归通过 |
| tools/redbook_tools/src/workflow/create_post.py | DONE | 不再同源缩减为1条，旧环境来源配额不生效，保留日期事实查重 | 全套通过，实机2条同源保留并交付 |
| tools/redbook_tools/apps/cli.py | DONE | 智能体终审取消来源数与官网数量门槛 | 全套回归通过 |
| tools/redbook_tools/tests/test_ai_digest_unrestricted_sources.py | DONE | 6项新行为，含单厂商6事件与过期拦截 | 先红后绿 |
| tools/redbook_tools/tests/test_ai_digest_source_alignment.py | DONE | 生成/终审同源多条一致，真实数量不足仍拦截 | 全套回归通过 |
| tools/redbook_tools/tests/test_cli_agent_policy.py | DONE | 旧来源配额不恢复且同源不拒绝 | 全套回归通过 |
| tools/redbook_tools/tests/test_cli_agent_non_news_repair.py | DONE | 用过期事实模拟坏稿，继续验证持久替换、历史和交付幂等 | 47项相关回归通过 |
| tools/redbook_tools/tests/test_ai_digest_history_streaming.py | DONE | 历史原始对象与重型来源数据存活20降至最多2 | 红绿及全套回归通过 |
| tools/redbook_tools/src/news/daily_news.py | DONE | HotNews改用已有httpx代理/TLS模式，避开Windows绕过DNS阻塞 | 实机超时及全套回归通过 |
| tools/redbook_tools/tests/test_hotnews_transport.py | DONE | HotNews三项、RSS解析/重定向/大小上限三项，核对代理/TLS/timeout | 6项先红后绿，工具全套通过 |
| tools/redbook_tools/src/sources/service.py | DONE | RSS复用httpx避开Windows代理DNS阻塞，保持TLS/禁止重定向/流式大小上限 | 实机RSS1299项，0.704秒 |
| docs/plans/2026-10-19-ai-digest-unrestricted-sources.md | DONE | 规则、风险、测试、维护干预、原位修正与最终回执 | 全部验证结果已补齐 |

**Notes**
- Tests/Lint: RSS修复后工具完整1231通过/36跳过（94.87秒）；后端来源规则回归767通过/1跳过（722.90秒）；原工作流1567通过/8跳过（366.53秒）；相关224项、HotNews/RSS传输6项通过。保留前期旧官网断言失败及测试误入真实额度API的原始日志，不抹除失败记录。
- Risks/Assumptions: 排序CRITICAL、选稿/发布过滤HIGH；保留事实、日期、事件及跨日查重，来源分布仅统计；没有默认浏览器操作、公开发布、新依赖或C盘安装。
- Runtime: 对话032458f494d7439e85b00e912eb8cd5f，根任务65d6bcaa0a454def9667d68976832192最终completed、1/1、uploaded=1。新稿0881c008b31441a39c5b0c725ba769e6“每日AI|Claude更新与公益基金”含2条Anthropic事件、2图；原位修正执行f89ffae2694943a8a9a3189f3a31824e回读title/body=True、images=2/2，当前版本回执有效，未公开发布。10条旧新闻未重跑；同名草稿不确定与旧发布争议混入经人工核对修正，维护记录保留，不宣称一次无干预完成。共享临时查重开关已撤回。
- Diagnostics: py-spy仅安装在E盘runtime/data/tmp临时目录、未进项目依赖；实测历史扫描私有内存约6.9GB、整机可用约444MB，另定位HotNews的Windows代理绕过DNS阻塞。维护停止仅作用于已核实的只读工作子进程，没有浏览器写入或稿件删除。
- Next steps: 本次交付已完成，原生续跑772263024ee55ba190d1cfe5b569a1f0对账already_current，没有重传。剩余性能风险为知识库ingest_posts的全量list_posts，约5.8GB私有内存；本轮未扩大重构。未提交或推送。

### 2026-10-19 20:09 GMT+8

**Task:** 增加同平台、同订阅Key的MiniMax-M3.1-Flash，不改变现有默认角色。

**Git:** main；保留之前所有修改，未提交、未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/config.py | DONE | 新增Flash预置，M3仍为默认值 | 配置及GUI消费回归通过 |
| tools/redbook_tools/apps/web_service.py | DONE | 同一MiniMax连接暴露Flash，配置相同模型不重复，计费保护不变 | 目录、绑定、冻结测试通过 |
| tools/redbook_tools/src/llm/generate.py | DONE | 正式Flash走已有推理分离分支，不发送禁用思考 | 保留M3/Preview原行为 |
| tools/redbook_tools/tests/test_minimax_flash.py | DONE | 选项、同Key、保存重开、重复、计费保护、真实请求边界 | 新行为先红后绿，工具全套通过 |
| tests/test_minimax_flash_browser.py | DONE | 主控/写稿选择、保存刷新、服务重建、新任务冻结 | 浏览器先红后绿；截图保留E盘 |

**Notes**
- Tests/Lint: 针对性25项、原工作流相关75项、浏览器1项通过；内置工具全套1239通过/36跳过（117.99秒），智能体后端768通过/1跳过（433.45秒），原工作流1574通过/8跳过（400.58秒）。合计3581通过/45跳过；仅既有依赖弃用警告，diff --check通过。三套日志分别为runtime/data/tmp/minimax-flash-{tools,agent,workflow}-20261019.log。
- Risk: GitNexus确认models目录及_summary_provider_kwargs为CRITICAL，调用涉及bootstrap/计划/绑定及draft/json生成；模型常量UNKNOWN经文本核实GUI和额度消费。不新增供应商、密钥副本或依赖，不重配默认角色，不修改运行中快照。
- Live: 官网正式ID为MiniMax-M3.1-Flash，使用原https://api.minimax.cn/v1和原Token Plan Key。早期两次Flash请求返回HTTP529/2064集群负载高，同Key的M3基线成功；随后两次Flash调用均200/stop，最后请求返回模型ID=MiniMax-M3.1-Flash、回答OK、220 tokens、1.172秒。成功探测显式使用官方支持的reasoning_effort=low，不推断默认max档位的速度，也不保证厂商不会再次过载。未使用其他Key/端点/付费回退。
- Next steps: 本轮代码与验证完成；正常重启智能体后在“连接与模型/默认角色”选择。未重启正式服务、未修改用户默认角色、未生成或上传帖子；无C盘安装。

### 2026-10-19 20:25 GMT+8

**Task:** 复核当前版本并上传GitHub，先备份远端原main。

**Git:** main；远端备份backup/main-before-20261019-202408已核验指向c56a1b9ed415872286bbe578fcf83f32ba9254c2；随后提交并正常推送main。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| docs/plans/2026-10-18-ai-digest-model-release-priority.md | DONE | 移除文件末尾多余空行 | 随本次代码提交 |
| tests/test_minimax_flash_browser.py | DONE | 移除文件末尾多余空行，不改变测试行为 | 随本次代码提交 |
| tools/redbook_tools/tests/test_ai_digest_changelog_releases.py | DONE | 移除文件末尾多余空行，不改变测试行为 | 随本次代码提交 |
| tools/redbook_tools/tests/test_ai_digest_unrestricted_sources.py | DONE | 移除文件末尾多余空行，不改变测试行为 | 随本次代码提交 |
| tools/redbook_tools/tests/test_minimax_flash.py | DONE | 移除文件末尾多余空行，不改变测试行为 | 随本次代码提交 |
| CODING_PROGRESS.md | DONE | 记录本轮复核和远端备份 | 随本次代码提交 |

**Notes**
- Tests/Lint: 本轮智能体相关41项、内置工具84项、原工作流相关260项通过，含模型选择的浏览器回归；智能体前端TypeScript检查与Vite构建通过。末尾空行清理后重新执行暂存检查。
- Risks/Assumptions: 两库GitNexus detect_changes(scope=all)均无partial/truncated；核心链路风险为CRITICAL，已核对模型、审核、计划和恢复消费者。新增测试未入旧索引的UNKNOWN已核对实际pytest函数；本轮仅清理末尾空行。
- Security: 智能体484个、工作流355个候选文件扫描未命中密钥规则或已知本地密钥；忽略规则保留API Key、浏览器资料、数据库、生成数据和本地部署，不改默认模型、不重启正式服务。
- Next steps: 提交并推送新版后核验两库远端main和备份分支哈希；本地生成记录及项目文档保留。

### 2026-10-22 10:34 GMT+8

**Task:** 主控、写稿与审核统一采用M3.1 Flash默认medium/131072，保留提示词篇幅约束，不恢复正式任务。

**Git:** main；仅本地修改，未提交、未推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| backend/task_recognition.py | DONE | 校准发送medium及max_completion_tokens=131072，截断错误使用实际上限 | 3项真实HTTP边界模拟通过 |
| tools/redbook_tools/src/llm/request_policy.py | DONE | Flash/Preview统一默认值，显式推理设置仍生效 | 参数回归通过 |
| tools/redbook_tools/src/llm/generate.py | DONE | 主控/压缩JSON和写稿使用统一额度；保留M3专用分支 | 生成与输出完整性回归通过 |
| tools/redbook_tools/src/ai_digest/generate.py | DONE | 汇总与影响审核统一参数，不再使用较小Flash额度 | 工具最终全套通过 |
| tools/redbook_tools/src/workflow/vision_review.py | DONE | Flash使用统一档位与输出上限，M3保留原参数 | 审核请求回归通过 |
| tools/redbook_tools/src/model_platforms/runtime.py | DONE | 三种协议默认值一致，保留显式输出配置，不修改快照 | 协议回归通过 |
| tools/redbook_tools/src/model_platforms/store.py | DONE | Flash允许保存131072，不放宽其他模型 | 新增/编辑及工具最终全套通过 |
| tools/redbook_tools/tests/test_minimax_request_policy.py | DONE | 34项回归覆盖主控/写稿/汇总/审核/协议/HTTP/配置 | 首次新规则23失败8通过，修复后通过 |
| tests/test_minimax_calibration_policy.py | DONE | Flash与Preview使用新参数，M3仍为4096且保持原思考策略 | 先红后绿，3通过 |

**Notes**
- Tests/Lint: 智能体全套771通过/1跳过（380.96秒）；内置工具最终全套1273通过/36跳过（81.32秒）；原工作流全套1608通过/8跳过（288.53秒）。合计3652通过/45跳过，无剩余失败；相关校准3项、工具86项及工作流110项也通过。全部日志在runtime/data/tmp，diff --check通过。
- Diagnostics: 工具首次全套1失败1269通过，暴露旧SimpleNamespace配置没有platform_snapshot；改用兼容读取后全套通过。不掩盖首轮失败记录。自定义配置原64000限制也先以测试复现，再做Flash专用修正。
- Risks/Assumptions: GitNexus规划/生成/压缩路径最高CRITICAL，动态配置UNKNOWN已核对service/CLI实际调用；两库detect_changes无partial/truncated，新文件额外以实参HTTP测试核对。不更改凭据、默认模型、既有快照或付费回退。
- Behavior: 默认medium；MINIMAX_REASONING_EFFORT或自定义模型显式档位仍可覆盖。131072为推理加答案总额度，不要求输出同等长度；保留新闻150–220字、复杂新闻最多300字、评价20–40字及重新总结而非截断的提示与校验。
- Next steps: 三套回归完成，正式服务继续关闭；不恢复生成/上传任务。未调用真实模型，实际生成时延与质量未验证。所有测试临时数据位于E盘且使用隔离数据库，无C盘安装。未提交或推送，正常启动后生效。

### 2026-10-22 10:50 GMT+8

**Task:** 智能体每日新闻评价采用50–100字，同步首次写稿、审核、重写与字段级修复。

**Git:** main；保留已有修改，未提交或推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/news/length_policy.py | DONE | 目标50–100、优选60–80、上限100、低于50仅警告 | 49/50/75/100/101边界回归通过 |
| tools/redbook_tools/src/workflow/create_post.py | DONE | 通用视角提示移除重复的旧字数，采用栏目篇幅要求 | 生成、评价与每日我去回归通过 |
| tools/redbook_tools/src/workflow/news_repairs.py | DONE | 定向修复提示改为50–100，保留只修允许字段及不截断约束 | 范围修复相关回归通过 |
| tools/redbook_tools/tests/test_news_concise_length.py | DONE | 范围内不重写、超100完整重写、前置提示及空白边界 | 工具相关91项通过 |
| tools/redbook_tools/tests/test_minimax_request_policy.py | DONE | HTTP请求评价提示采用50–100，模型参数不变 | 请求边界回归通过 |

**Notes**
- Tests/Lint: 本轮相关91项通过，原工作流相关78项通过；内置工具完整1278通过/36跳过（77.00秒），原工作流最终完整1613通过/8跳过（314.30秒），合计2891通过/44跳过，无剩余失败。仅既有依赖弃用警告，diff --check通过。独立智能体后端未改，本轮不重复上一任务已通过的771项后端全套，不将其计入本轮验证数量。
- Risks/Assumptions: GitNexus篇幅提示影响HIGH，限定只修改评价长度与重复提示，不修改正文150–220字、复杂事件300字、每日我去、事实审查或图片规则。两库detect_changes无partial/truncated，旧模块文本检查未发现残留20–40评价限制。
- Next steps: 本轮完成，正常启动后后续生成采用新评价目标；不恢复正式服务或新闻任务，不调用真实模型，不修改平台草稿或发布内容。日志与测试数据位于E盘runtime/data/tmp（news-comment-50-100前缀），无C盘安装；未提交或推送。

### 2026-10-22 15:19 GMT+8

**Task:** 智能体每日AI讯息正文硬上限800字符，保留完整资讯并避免确定性超长错误无限重试；暂停旧任务并保留断点。

**Git:** main；保留全部既有修改，未提交或推送。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/src/ai_digest/models.py | DONE | AI正文800预算单一来源 | 工具完整回归通过 |
| tools/redbook_tools/src/ai_digest/generate.py | DONE | 前置说明预算；省略重复展示元数据；不截标题，容量不足显示完整重点索引并明确其余见配图 | brief全量条目、摘要、时间及信源不变 |
| tools/redbook_tools/src/workflow/create_post.py | DONE | 采集前预估、正文适配及落盘前硬检查统一800，保存实际长度 | 801即使由错误renderer返回也拒绝保存 |
| tools/redbook_tools/src/validation/rules.py | DONE | AI图文800、普通新闻1000不变；使用元数据识别栏目 | 边界及普通新闻含AI标题测试通过 |
| tools/redbook_tools/src/agent/editorial_agent.py | DONE | AI_DIGEST_BODY_LIMIT识别为明确排版错误，保留进度并提示修正文，不再重新采集 | 一次生成失败即停止该确定性重试，其他任务/错误策略不变 |
| tools/redbook_tools/tests/test_ai_digest_body_budget.py | DONE | 13项覆盖800硬界限、完整数据、单轮完成、无效正文拒存与智能体不重复采集 | 先红后绿 |

**Notes**
- Tests/Lint: 工具新用例12通过；确定性错误先红（旧代码生成两次）后绿，相关40通过；AI来源/增量/完整性/卡片时间相关74通过；工具完整1291通过/36跳过（91.32秒）。工作流完整1625通过/8跳过（341.54秒）；独立界面/续跑/草稿回读接口29通过（7.52秒）。本轮共2945通过/44跳过；后端未重复其余全套，不计入本轮结果。仅既有弃用警告，diff --check通过；日志均位于runtime/data/tmp/ai-body800-*.log。
- Scope: 800仅约束最终发布正文，包含标题行、日期、来源、标点和换行；不是把结构化JSON或所有配图摘要裁成800。完整资讯保留，正文索引容量不足会明确提示未列出的条数与配图；无额外LLM压缩调用、不切换模型或付费路径。
- Runtime: 原任务22bb9cda85fa4d34a26358b5e6d7a407先经REST提交pause_goal。耗时检索未响应时，按用户授权备份checkpoint/events，再只终止已核对run-id的48980进程树。通过已有GoalStore接口、校验原active_attempt与lease_epoch后，目标ac87d5943ec34ddc80515a586ce866e3置paused，turn d0213aa7df07476580cc1eccb1090f0f记applied，避免恢复时重复消费旧暂停。最新API和本地检查点核对：目标paused、worker=0、10条上传记录和completed_job_indices=[0]保留。原运行failed/exit=1是本次维护停止，未删除或重跑新闻。
- Backup: runtime/data/backups/20261022-body800-pause/22bb9cda85fa4d34a26358b5e6d7a407/checkpoint.json及events.jsonl；不含本轮新生成稿件，不修改原业务断点。
- Risk: GitNexus AI生成/排版最高CRITICAL、通用验证HIGH；动态generate节点UNKNOWN由LangGraph add_node注册、实际源码及回归补证。两库detect_changes均无partial/truncated；没有资料库降级、依赖安装、C盘写入安装或默认浏览器操作。
- Next steps: 本轮完成。GUI与PostgreSQL保持运行，但任务继续暂停，不自动续跑、不生成或上传新稿；后续新CLI worker会加载修复。未提交或推送。

### 2026-10-23 10:02 GMT+8

**Task:** 修复每日AI讯息版本流水重复、普通新闻同一政策文件过度集中，以及模型拒选与补位审校的回流漏洞。

**Git:** main；保留先前所有未提交变更；本轮不提交、不推送、不生成或上传新稿。

| File | Status | What changed | Remaining / Next action |
|---|---|---|---|
| tools/redbook_tools/apps/cli.py | DONE | AI影响力审核默认启用、阈值75；尊重显式覆盖，来源与国内外硬配额仍为0 | 新CLI worker加载 |
| tools/redbook_tools/src/ai_digest/rank.py | DONE | 区分Codex CLI/Claude Code与基础模型；等价版本流水择新并保留证据；原始摘录优先阻止无事实扩写 | 真实材料与52项新增回归通过 |
| tools/redbook_tools/src/news/daily_news.py | DONE | 政策发布按文书身份聚类；不同文书/机关/落地执行不误合并；匿名科技快讯只按受限共现证据聚类 | 真实6条变1条；26项新增回归通过 |
| tools/redbook_tools/src/workflow/create_post.py | DONE | rejected_ids真正排除且部分返回不恢复拒选；补位附已选事件重审；不足时保留合格稿继续补位；非模型发布不绕过明确低影响审核；主动关闭审核不再报配置丢失 | 功能回归通过；浏览器独立重测通过 |
| tools/redbook_tools/tests/test_editorial_selection_regressions.py | DONE | 11项覆盖拒选/矛盾/部分/全部拒选、平衡与速度模式、公开工作流入口补位、审核开关和来源无硬配额 | 两套工具新用例一致 |
| tools/redbook_tools/tests/test_ai_digest_release_editorial_dedupe.py | DONE | 52项覆盖多段alpha版本、原始证据、中文维护模板、实质变化、不同项目与模型变体 | W/T一致 |
| tools/redbook_tools/tests/test_news_policy_event_identity.py | DONE | 26项覆盖真实条款、文书差异、匿名证据不足、不同机关、后续执行及跨时区 | W/T一致 |
| tools/redbook_tools/tests/test_cli_agent_policy.py | DONE | 对齐恢复的默认影响力审核策略，保留无来源硬配额断言 | 新增集成测试验证实际筛选效果 |

**Notes**
- Tests/Lint: 最终智能体工具全套1380通过/36跳过（82.69秒）；独立后端全套771通过/1跳过（333.36秒），使用E盘隔离PostgreSQL测试库并按conftest清理。工作流最终全套1713通过/1失败/8跳过（335.79秒），唯一失败为浏览器测试分配到Chrome禁止的临时6000端口，Page.goto报ERR_UNSAFE_PORT；未修改浏览器安全策略或产品代码，同一浏览器测试文件独立重测2通过（11.79秒）。两套CLI --help均exit=0。最终3865个不同用例通过或复测通过、45跳过，但不称一次性全套零失败。diff --check通过，仅既有弃用及CRLF提示。
- RED/GREEN: 主线先8失败，修复后通过；部分审核返回另有1失败后修正。政策回归两库先14失败/12通过，后26通过；AI版本回归及真实扩写样本均先红后绿，最终52通过。新增共89项/每套。
- Diagnostics: 首轮相关新闻回归268通过/1失败，旧默认0/0断言在收集时已加载；更新断言后相关17通过。工作流首轮全套1712通过/1失败/8跳过，失败test_collect_ai_digest_updates_uses_search_backfill_for_daily_digest_quotas的Claude Code测试材料仅说更新、无具体事实；更换为具体MCP认证变化夹具，保留原8条和配额断言后相关12项通过，最终完整重跑中。
- Evidence: 只读回放2026-10-22稿0a45c8541dac4f61a9e67f8232fd50f4：20条中排除7条（6条只有版本号的Codex alpha、1条泛泛Claude维护），保留13条候选；Claude Code有明确不同变化的3条仍可候选，不按厂商硬限额删掉。discovery 7fdb0b0df5a54c0ab01dbb784eaacefe中的6条中办国办政策条款经事件去重保留1条。回放不代表重新生成或通过真实LLM影响力审核；原稿、草稿、检查点均未改写。
- Risk: 两库GitNexus预编辑影响HIGH/CRITICAL，直接链路为pick_news_items/discovery._review/create_daily_news_posts/CLI与智能体工具。UNKNOWN的测试函数已用源码及pytest收集补证，不作未使用判断。最终detect_changes未标记partial/truncated；新助手与测试未建索引部分由回归和真实材料回放补证。
- Boundaries: 不恢复来源/厂商/国内外硬配额；匿名政策合并仍为受限推断，同机关同媒体30分钟内两份未具名科技政策可能误聚，证据不足保守保留。新闻正文150–220、复杂最多300、评价50–100、AI正文800、MiniMax medium/131072保持。无新依赖或C盘安装，不调用真实LLM、生图、上传或发布接口，不重启正式服务。
- Completion: 2026-10-23 10:08 GMT+8验证结束，新增回归每套89项通过。两个子智能体已冻结并关闭；临时HTTP测试服务由finally清理。正式已上传旧稿不自动重写，后续新任务使用修复。未重启正式服务、未提交或推送。随机浏览器测试端口仍有碰到浏览器保留端口的环境风险，原始失败与重测日志均保留。
