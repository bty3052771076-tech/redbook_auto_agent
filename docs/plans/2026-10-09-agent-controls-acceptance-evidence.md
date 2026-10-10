# 智能体控制面验收证据台账

## 范围与判定

对应能力中心42项和可编辑计划61项，共103项。编号使用CAP和PLAN前缀避免重名。
这是一份实施中的核对表，不是全部通过声明。`已有回归`表示所列自动化路径有通过结果；
`部分`表示用例的部分观察已有证据，整个用例仍未验收；`未验证`不能推定通过。
实机供应商、真实平台草稿、全量响应脱敏及完整消费者行为不能由界面替身代替。

历史基线：根目录tests全量v10，294通过、无跳过、3项既有弃用警告，101.04秒。v23根目录及10个嵌入工具测试文件最终600通过、3跳过、3警告，197.10秒；不将重叠组合数量相加。
本台账随后新增的修复须以新的测试记录为准，不与重叠组合测试相加。
所有日志和截图在 `E:/AI/codex/redbook_runtime/data/tmp/`；没有公开发布，没有同步额度。

## 证据索引

- E01：`agent-controls-suite-v10.log`，此前完整根目录回归。
- E02：`startup-lease-green-resume.log`，38项提交、上下文、真实PG竞争及恢复。
- E03：`confirmation-journal-green-v3.log`，63项确认别名、故障日志及相关回归。
- E04：`dirty-plan-calibration-green.log`，20项计划、校准、能力中心浏览器回归。
- E05：`forgotten-context-green.log`，33项遗忘、压缩、上下文、冻结及运行上下文。
- E06：`mcp-consumption-green.log`，44项准备结果、实际CLI提示词消费、MCP及能力调度。
- E07：`knowledge-purpose-ui-green-v2.log`，21项用途值、浏览器及记忆回归。
- E08：旧URL五项失败复现后，模型叶节点、平台、冻结、校准、提交组合65项通过。
- E09：资料目录真实PG两项失败复现后，资料、API、空文档组合20通过、1跳过；跳过不算通过。
- E10：`agent-controls-performance/`，目录和同输入离线模型边界性能报告；不是实网生成时间。
- E11：`context-request-green-v2.log`，72项实际请求引用、发送失败、上下文API和CLI技能消费；响应token为离线夹具，不替代实机供应商证据。
- E12：`mcp-http-green-v2.log`，34项真实本地HTTP、401/超时/协议、同名服务、撤销凭据及相关回归。
- E13：`mcp-evidence-green-v2.log`32项、`mcp-controller-consumer.log`1项、`mcp-policy-green.log`22项；阶段授权、实际主控输入、父子流水和内置MCP检索依赖。
- E14：`memory-knowledge-green.log`32项、`knowledge-version-check.log`1项；偏好完整性及真实PG+API索引改版。
- E15：`plan-ime-green.log`13项；Chrome组合输入事件和四视口。v16已重新验证抽屉边界和视口截图；1440/390截图人工核看，未宣称操作了所有系统输入法。
- E16：`agent-controls-suite-v15.log`为470通过、1失败、3跳过、31夹具错误；定位为CLI测试泄漏模型配置目录/namespace到后续测试。隔离修复组合94项通过，随后根目录+9个嵌入文件v16为502通过、3跳过，284.81秒。v17最终530通过、3跳过、3项警告，222.83秒。三项跳过仍为JSON断点缺少图节点信息，不计通过。
- E17：`calibration-address-red.log`四项失败，复用既有基址校验后`calibration-address-green.log`70项通过；校准不接受URL凭据、查询参数、片段和无效端口，不回显测试秘密。
- E18：`live-task-calibration-run.log`实际MiniMax-M3订阅调用一次，候选约10.203秒出现，首轮后续界面断言失败。候选包含不支持的fast及未经确认的硬要求，保持可编辑且不可执行。复用同一PG候选的`live-task-calibration-resume-v3.log`最终通过：5篇、移除年龄核验、增加游戏退款、修改评价/速度/交付；刷新和PG读取为版本2，保存与回显0.594秒。续验新模型调用0、内容运行0、平台写入0。中间两次续验为诊断脚本未选择历史对话/浏览器等待方法错误，未重调模型。截图和结果在`live-task-calibration-330e594ff86e40459c053cafc7f986ef/`。PowerShell把依赖的stderr弃用警告标为NativeCommandError，本证据以脚本result.json和PG读回为准，不宣称该包装命令退出码为0。
- E19：真实PG+浏览器离线供应商回包复现校准完成后的旧会话revision保存冲突；候选开放编辑前刷新会话版本后，`calibration-pg-browser-green.log`19项通过，52.76秒。另修复重复提醒与fast误显示为平衡，`calibration-display-green.log`24项通过，60.28秒。TypeScript/Vite v18构建通过。
- E20：`plan-cli-consumers-v2.log`22项通过；保存修订、冻结、Workbench参数、实际CLI及生成/草稿回调接收5篇、新选题、评价、模式、日期和评分。实际核心编排、生成与平台动作在该消费者专项中隔离；不据此宣称实网生成/平台写入已通过。首轮4个错误为旧夹具使用test模型namespace，改为调用真实冻结实现，未放宽生产namespace限制。
- E21：独立审查发现旧持久化内置MCP执行目标、空容器、后台发现导致旧编辑采用新revision等缺陷。逐项先复现再修复：内置目标在保存、读取、公共目录及执行前校验；异常旧记录独立隔离且可退役；工具政策与schema变更同步回显；编辑抽屉锁定打开时的revision。`controls-last-boundaries-green2.log`67项后端通过，21.53秒；`controls-last-boundaries-green.log`中的18项浏览器用例通过，但该组合有2项后端失败，不能将整份日志称为通过。后两项分别为实际headers字段映射错误与不完整builtin夹具，均已修复。独立审查随后确认本轮已列缺陷关闭。
- E22：v18完整根目录406通过，178.86秒。v19根目录+10个嵌入工具文件593通过、1失败、3跳过，204.72秒。唯一失败为直接旧Workbench入口使用新版v3计划却没有确认冻结快照；将该测试明确为legacy v2兼容场景后，`controls-legacy-score-v21.log`89项通过，5.30秒。v3缺少快照仍拒绝恢复的独立回归保留，不放宽生产保护；上述数量有重叠，不相加。
- E23：`controls-real-boundaries-v22.log`31项通过，7.94秒。真实本地HTTP覆盖2026-07-28、2025-06-18、2024-11-05，验证发现/枚举/调用及旧文本结果；新协议无initialize，旧协议确实使用握手。实际Windows目录联接被拒绝，未激活导入，目标原文不变。服务是测试拥有的本地协议服务，不代表已测全部第三方MCP。
- E24：真实数据库连接故障和Chrome离线状态先复现失败。测试仅将单个测试实例的pool连接到测试独占的不响应握手端口，不停生产数据库。修复后静态内置目录可读、控制禁用、写操作503、恢复原连接后持久化记录和本地产物保留。`controls-pg-offline-green2.log`7项后端通过，8.14秒；前一组合25项通过、1失败，54.66秒，含19项浏览器全部通过，剩余失败为新增测试使用不支持的global作用域，已改为workspace并增加准备阶段成功断言。没有内存/文件持久化降级，没有离线执行策略。
- E25：单次试运行、每个MCP连接的网络策略、内置技能个人副本、被覆盖偏好的原文/来源/原因均已实现。`operation-trial-uncertain-green.log`26通过；中断试运行保留原运行ID、同键不重新提交，普通只读检测仍可明确重试。运行与接口测试不是所有第三方服务的兼容认证。
- E26：`live-capability-trial-2b812a4998ca42598fc05df2fdf9d76e/result.json`实际状态passed，总墙钟658.781秒。独立namespace中实际加载1项技能正文和1个附件，并经统一调度器调用本地只读MCP；随后GUI只确认一次新闻生成，原运行`f4c1e6cbc7044d33afa005a68b08665a`完成一条草稿，另用只读命令核对标题、完整正文和图片数均一致。稿件`fb435ea5e81341cfa134c89f07146ac8`，标题“波黑伊斯兰共同体明日选举新任大穆夫提”，1张图，saved_as_draft。浏览器使用`E:/AI/codex/redbook_runtime/data/browser/chrome-profile`的Default、headless；主控/写稿MiniMax-M3，生图opencodex:gpt-image-2，没有擅改既有模型绑定、同步额度或公开发布。技能加载为独立预检；该条新闻skill_mode=off，不能称其写稿使用了预检技能。实际主控自行选择runtime_status并留下调用记录；实际供应商usage为输入930、输出393、合计1323 token，不用估算替代。生产PG统计随真实同步变化，不能沿用4390的固定样例。
- E27：`capability-detail-final-red.log`先复现历史非ASCII运行ID及缺少依赖跳转；工具契约/API/浏览器/性能组合`capability-detail-final-green.log`50通过、3警告，73.57秒。内置工具展示实际参数、数组结果字段及工作流并发策略；旧配置的空schema不掩盖最新登记。正在使用的任务依据真实PG运行租约而非历史快照，原快照保留；依赖详情只作展示，不写入执行策略。
- E28：`capability-upload-outcome-red.log`5项失败证明返回式上传失败被流水误记成功及父调用丢失uncertain；修复后`capability-upload-outcome-green.log`38通过、1警告，3.89秒。单条/批量返回值保持不变，流水按failed/uncertain记录，不为测试再次操作平台；仅成功返回才记成功。异常不确定状态传递至父调用。
- E29：概览分页统计先复现2项失败；`capability-catalog-counts-green.log`42通过、3警告，72.08秒。可用数、登记总数、启用数均由服务端全筛选结果统计，翻页不改变登记总数；空筛选返回0。暖目录20样本p50=243.708ms/p95=346.895ms，无模型/MCP探测，低于500ms目标。`agent-controls-frontend-final.log`TypeScript/Vite构建通过，包装命令6.043秒。
- E30：最终代码图索引27.6秒完成，9282节点/23353边/393模块簇/809流程。GitNexus对scope=all的已跟踪差异返回248变更符号、221影响流程、48文件，partial/truncated均未标记，风险critical；此统计不包括Git尚未跟踪的新文件。已检查Workbench.plan及PlanService.confirm消费者，并保留动态接收者/流程预算导致的图下界说明，不把没有边视作无影响。`git diff --check`退出0，仅有现有LF/CRLF提示，没有提交。
- E31：本版GUI已在`http://127.0.0.1:8786/`后台启动。`agent-controls-live-ui-final-v4.log`真实Chrome独立临时上下文检查通过：业务数据与PG就绪、实际契约字段、依赖抽屉跳转、无历史任务误报运行中、1440/390无横向溢出、JS错误0。截图在`agent-controls-live-ui-final/`。首次诊断未指定已安装Chrome通道而找不到Playwright专属浏览器，没有安装；v3因测试选择器未限定抽屉而匹配两按钮失败，v4限定后通过；未改变产品行为或触发内容任务。
- E32：统计修复后的最终新进程回归`agent-controls-suite-final-v2.log`退出0，根tests及14个相关嵌入工具文件640通过、3跳过、3警告，222.77秒。三个跳过全部为`test_editorial_agent_completed_revalidation.py:213`的旧JSON断点缺少graph pending-node元数据；不算通过，不替代真实PG断点测试。同一组合包含真实PG审稿中断后的产物保留/恢复测试，生成次数保持1、完整后再恢复不增加上传次数；此测试外部生成和平台回执为确定性隔离工具，不宣称真实平台故障实测。此前638通过记录是统计修复前的基线，不能与640相加。

最终文本检查保持项目的既有换行配置，`agent-controls-diff-check-final.log`退出0。一次临时强制关闭autocrlf的检查把现有CRLF文件中的回车当作尾随空白而失败；没有据此对整个工作树做换行重写或删除用户修改。

后续以命令日志补充实机结果。台账中的测试文件都位于独立程序，不引用旧工作流测试作为新程序通过证据。

## 能力中心42项

| 编号 | 状态 | 已有证据与剩余观察 |
| --- | --- | --- |
| CAP-A01 | 部分 | capability_api、browser：目录与分页；技能计数和全部目录一致性待逐项复核 |
| CAP-A02 | 已有回归 | capability_api：读取不启动外部工具；管理页浏览器无生成、上传 |
| CAP-A03 | 部分 | E12：构造错误持久化、本地路径故障不隐藏其他目录；新版错误的GUI逐项显示待复核 |
| CAP-A04 | 已有回归 | capability_store、capability_mcp：独立.venv解释器、运行区cwd及真实本地只读协议 |
| CAP-A05 | 部分 | capability_execution、model_leaves：业务与叶节点停用；全部内部回退路径仍需核对 |
| CAP-A06 | 已有回归 | capability_store、plan_capability_freeze：默认变更不替换冻结版本 |
| CAP-A07 | 部分 | model_leaves：后续请求前撤销有效；已提交平台操作的实机证据未验证 |
| CAP-A08 | 部分 | capability_mcp、skills：未知平台工具和正文不会执行；完整越权组合仍需验证 |
| CAP-M01 | 已有回归 | capability_mcp、managed_mcp_runtime：真实stdio目录、调用、用途、父子流水和关闭 |
| CAP-M02 | 已有回归 | E12：本地真实HTTP成功、401、超时、协议错误原因分开，公共结果不含测试Header密钥 |
| CAP-M03 | 已有回归 | capability_mcp及browser：发现差异、schema变化撤回旧批准、阶段政策与保存结果回显；编辑中的旧revision不会被后台发现偷偷替换 |
| CAP-M04 | 已有回归 | E12：两个HTTP服务暴露search，用各自认证实际调用，ID/结果没有串服务 |
| CAP-M05 | 部分 | E06/E13：准备、证据阶段授权与实际主控输入已贯通；真实供应商与证据阶段HTTP组合未全部验证 |
| CAP-M06 | 已有回归 | managed_mcp_runtime：同任务复用、空闲关闭、任务关闭，不处理别的进程 |
| CAP-M07 | 已有回归 | E23：真实本地HTTP三版本协商/调用、新版无握手、旧版握手和文本JSON；分页不完整差异另有回归，不代表全部第三方服务 |
| CAP-S01 | 部分 | capability_skills、execution、freeze：导入、hash、正文与资源加载；完整GUI到生成提示词待复核 |
| CAP-S02 | 已有回归 | skills：五个中文栏目自动匹配、关闭不注入、手动最多3项；版本及浏览器回显另有回归 |
| CAP-S03 | 已有回归 | E23及skills：实际NTFS目录联接、非法frontmatter/ZIP穿越拒绝且无半激活；预览变更拒绝、版本不覆盖 |
| CAP-S04 | 已有回归 | skills、freeze：新版本追加、冻结正文与资源不擅换 |
| CAP-S05 | 部分 | browser显示脚本不执行、资源只读；恶意正文进入真实模型边界仍需补证 |
| CAP-K01 | 已有回归 | 真实PG跨实例保存、版本、流水、会话恢复，无文件/内存持久化降级 |
| CAP-K02 | 已有回归 | capability_memory真实PG插入4390/4372/18样例，待索引0，测试独立namespace事后清理 |
| CAP-K03 | 已有回归 | E14：真实PG原文改版、管理API异步增量索引、当前检索排除旧片段且保留旧版本 |
| CAP-K04 | 已有回归 | capability_memory：真实PG用途排除，保留查重命中；E07修正界面用途值 |
| CAP-K05 | 已有回归 | capability_memory真实PG同名两namespace：入库、列表、索引状态、详情和检索隔离；测试向量验证SQL/版本，不评价语义质量 |
| CAP-K06 | 已有回归 | execution_context、model_leaves及browser：当前要求优先、栏目不串用；被覆盖旧偏好的具体内容、来源与原因可查看 |
| CAP-K07 | 部分 | E05：按作用域失效，原消息保留、重新压缩不重注入；不宣称可识别所有同义表达 |
| CAP-C01 | 已有回归 | compaction_accounting、context_api：同范围比较、无收益、无需压缩、版本和原文保留 |
| CAP-C02 | 已有回归 | 真实PG新消息在压缩中到达，旧摘要提交被CAS拒绝，消息保留；不宣称手动操作所有并发时序 |
| CAP-C03 | 部分 | E11/E26：实际绑定及真实MiniMax usage已记录；压缩算法/CAS有回归，另一次真实供应商压缩未运行 |
| CAP-C04 | 已有回归 | execution_context：超过16/32条历史消息不静默丢弃，策略明确 |
| CAP-C05 | 部分 | E11：实际发送捕获摘要/近期消息/偏好/技能版本并读回，失败不造成功；全部适配器与RAG引用尚需复核 |
| CAP-R01 | 部分 | E25/E28：返回式失败、父子uncertain、中断原ID保留、不重复提交已验证；E26真实只读草稿核对通过。未故意在真实平台提交中杀进程，故完整实网故障恢复未认证 |
| CAP-R02 | 部分 | 保留产物与冻结恢复组合已有证据；管理面全链路真实重启未验证 |
| CAP-R03 | 已有回归 | store、operations、PG竞争：revision冲突、原子快照和变更流水 |
| CAP-R05 | 部分 | E12：已有HTTP连接的DPAPI凭据撤回后，下一次调用在发送前拒绝；配置轮换+整个任务恢复待补 |
| CAP-R04 | 已有回归 | E24及browser：真实连接不可用时写入503、静态目录、禁用控件、恢复连接记录保留；失败刷新只提供不可执行的上次状态预览 |
| CAP-U01 | 部分 | browser的10页×1440/1024/390自动布局、焦点/返回通过；补充人工核看1440工具/偏好、1024上下文、390概览/偏好/断点；不宣称30张均人工看过 |
| CAP-U02 | 已有回归 | browser、capability API：旧记录显示未采集，不补写伪历史 |
| CAP-U03 | 部分 | 同源和凭据响应已有测试；E08拒绝URL密钥；全面导出与路径边界尚需审计 |
| CAP-P01 | 已有回归 | E10：50项暖目录20样本p50=223.74ms/p95=241.13ms，无隐藏探测，内部冻结60工具仍完整 |
| CAP-P02 | 部分 | E10：同输入同模型离线边界管理开销约8ms；不代表实网单篇完整生成用时 |

## 可编辑计划61项

| 编号 | 状态 | 已有证据与剩余观察 |
| --- | --- | --- |
| PLAN-A01 | 已有回归 | E18/E19：长原句产生可编辑候选，模型错误强度/模式由用户修正并保存；不把一次模型结果当作始终正确 |
| PLAN-A02 | 已有回归 | task_plan_v3：规则数量不否决模型候选，差异保留 |
| PLAN-A03 | 已有回归 | v3候选可以移除关键词；编辑及采用路径有回归 |
| PLAN-A04 | 部分 | v3候选可以改主题且有来源提醒；同义表达UI截图待补 |
| PLAN-A05 | 已有回归 | task_calibration：交付不自动变更，采用/确认分开 |
| PLAN-A06 | 已有回归 | v3附加说明引用错误保留核心计划，标未核验 |
| PLAN-A07 | 部分 | 引用可定位不等于语义已验证；跑题说明的独立界面场景待补 |
| PLAN-A08 | 已有回归 | task_plan_v3：不存在编号只产生提醒，不编造原文 |
| PLAN-A09 | 已有回归 | calibration/reference_formats：损坏JSON、重复键、HTML失败且不重试 |
| PLAN-A10 | 已有回归 | plan_contract、task_plan_v3：非法数量与重复栏目可定位、不能执行 |
| PLAN-A11 | 已有回归 | v3附加说明和建议的格式错误不污染核心候选 |
| PLAN-A12 | 已有回归 | plan_contract：强度须确认，不支持硬要求不能默默放宽 |
| PLAN-A13 | 部分 | E19在校准发送边界确认原始输入且没有base_plan/local_plan；E20实际CLI消费人工修订，已有覆盖不等于所有原文场景完整 |
| PLAN-B01 | 已有回归 | E18/E19/E20：PG版本2为5篇；修订、冻结、CLI加载及生成回调count一致为5 |
| PLAN-B02 | 已有回归 | editor、contract：删除旧偏好、新增退款、重编译并刷新 |
| PLAN-B03 | 已有回归 | revisions、v3：人工新增主题允许保存且标user_edit |
| PLAN-B04 | 已有回归 | contract/v3：显式空值和人工清空保留，不恢复旧主题 |
| PLAN-B05 | 已有回归 | contract：旧prompt不作为隐藏执行来源，改写说明重新编译 |
| PLAN-B06 | 部分 | 独立字段契约和CLI编译；全部消费者分离验证尚需补 |
| PLAN-B07 | 已有回归 | contract：删除栏目tombstone、AI固定1篇，不补回新闻 |
| PLAN-B08 | 已有回归 | task_calibration、execution_context：栏目关键词和记忆不串用 |
| PLAN-B09 | 部分 | E20：评价、运行模式、评分实际消费者及所选平台草稿分支；角色叶节点有独立回归，自定义角色与该完整组合仍需核对 |
| PLAN-B10 | 已有回归 | contract：删除标签但旧说明冲突为needs_input，不能宣称完全删除 |
| PLAN-B11 | 已有回归 | v3：人工覆盖再次校准保留、建议显式选择 |
| PLAN-B12 | 已有回归 | revisions/v3：建议值、人工值、基础值差异及过期检查 |
| PLAN-B13 | 已有回归 | execution_context/leaf捕获：旧摘要数量、选题不回填已确认任务 |
| PLAN-B14 | 已有回归 | revisions与editor：历史恢复追加修订并重建覆盖，不执行 |
| PLAN-B15 | 已有回归 | contract：说明强度绑定内容摘要，修改后重新核对 |
| PLAN-C01 | 已有回归 | revisions、browser、calibration：PG修订，保存/采用无worker与上传 |
| PLAN-C02 | 已有回归 | revisions：同键同载荷返回原修订，不重复写 |
| PLAN-C03 | 已有回归 | PG两个独立连接：保存胜者唯一；browser冲突保留输入 |
| PLAN-C04 | 已有回归 | calibration：候选过期不能覆盖新修订 |
| PLAN-C05 | 部分 | CAS与相关输入摘要检查；非语义状态变化独立场景待补 |
| PLAN-C06 | 已有回归 | PG namespace和校准跨会话ID拒绝，无别会话变更 |
| PLAN-C07 | 部分 | 保存不可用错误；真实PG断开与恢复重试未验证 |
| PLAN-C08 | 已有回归 | 新实例PG持久化、校准中断记录，不自动再调用模型 |
| PLAN-C09 | 已有回归 | 双PG连接认领同身份，确认键别名绑定同run |
| PLAN-C10 | 已有回归 | E02/E03：启动租约、提交日志故障、启动后未回写，避免第二次任务 |
| PLAN-C11 | 已有回归 | submission/editor：原计划冻结，复制需明确确认且创建新修订 |
| PLAN-C12 | 部分 | resume_contract/review_regressions：检查点身份、原profile/模型/namespace冻结、文件丢失重建与篡改拒绝；新编辑和完整恢复并行场景待补 |
| PLAN-C13 | 已有回归 | 旧model路由走统一修订服务，相关兼容回归通过 |
| PLAN-C14 | 已有回归 | 两个独立PG连接保存/认领竞争，不混用修订 |
| PLAN-C15 | 已有回归 | 版本递增后的幂等原结果、换载荷409及确认别名覆盖 |
| PLAN-C16 | 已有回归 | submission/freeze：不完整冻结文件从同PG快照重建，hash不一致不启动 |
| PLAN-D01 | 已有回归 | 当前计划、候选共享PlanEditor，浏览器编辑/采用 |
| PLAN-D02 | 已有回归 | calibration browser：模型失败保留当前计划和编辑入口 |
| PLAN-D03 | 已有回归 | E15：Chrome组合输入事件含229提交保护，主动Enter只增一次；未宣称验证所有OS输入法 |
| PLAN-D04 | 部分 | 长标签与说明换行；全部长度边界截图待核对 |
| PLAN-D05 | 部分 | E15/v16：1440/1024/768/390边界与独立滚动通过，1440/390视口图人工核看；其他视口最终视觉复核尚缺 |
| PLAN-D06 | 已有回归 | editor/shared Drawer：Escape、未保存提示、焦点恢复 |
| PLAN-D07 | 已有回归 | editor：编辑时执行禁用，取消恢复已保存预览 |
| PLAN-D08 | 已有回归 | editor：刷新、409保留、保存失败不虚报 |
| PLAN-D09 | 已有回归 | E04四路径：返回/失败零模型，成功用新修订，放弃用旧修订，不生成上传 |
| PLAN-E01 | 已有回归 | contract与task intent：白名单不新增公开发布、删除或付费权限 |
| PLAN-E02 | 已有回归 | contract：服务端字段、路径、prompt等注入被拒绝 |
| PLAN-E03 | 部分 | 模型不可用及角色字段错误可见；全部编辑后消费者选择还需复核 |
| PLAN-E04 | 已有回归 | contract：v2迁移保留原字段与来源，首存追加新修订 |
| PLAN-E05 | 已有回归 | 旧失败记录不伪造候选，保留人工编辑及主动重试 |
| PLAN-E06 | 部分 | 旧CLI专项和参数加载已有回归；完整嵌入工具测试未全量执行 |
| PLAN-E07 | 部分 | schema脱敏、禁止执行提示文本；全部模型输出边界审计尚缺 |
| PLAN-E08 | 已有回归 | contract/editor：尚未接入字段能显示和逐项清除，不强迫删整个栏目 |

## 验收范围说明

此前独立审查所列四个功能差距均已实现，见E25及各浏览器/API测试；最近状态只读预览也已补齐。真实MiniMax校准与人工编辑见E18/E19，真实新闻生成、草稿保存和读回见E26，v23已结束而非仍在运行。

表中的“部分”仍是证据范围限制，不能改写成103项全部实机通过。未做真实平台提交后的破坏性中断、所有第三方MCP兼容、全供应商语义质量或真实网络条件下的前后完整生成配对比较；不为覆盖这些范围而重复生成/上传，亦不测试公开发布。P02离线同输入边界用于隔离新增本地开销，不声称658.781秒比旧工作流更快。

实现、回归、构建、图检查和本版GUI已交付，最终结果见E25至E32。保留上述未验证范围，不因功能完成而把每个范围标成实机认证。
