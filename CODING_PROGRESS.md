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
