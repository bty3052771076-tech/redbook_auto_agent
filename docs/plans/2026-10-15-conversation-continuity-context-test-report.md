# 持续对话与上下文压缩实施验收

日期：2026-10-15。项目：`E:/AI/codex/redbook_agent`。

## 范围与执行方法

按同日[设计](2026-10-15-conversation-continuity-context-design.md)与[实施计划](2026-10-15-conversation-continuity-context-implementation-test-plan.md)实现。先在E盘隔离工作树编码，复用现有依赖，再按绝对路径和SHA256核对同步回独立项目。未安装新依赖，未提交或推送Git，不修改旧工作流源码，不重启正式服务。

测试使用临时真实PostgreSQL数据库、实际LangGraph检查点、实际本地HTTP API及无窗口Chrome。新闻生成、模型和平台提交用受控适配器替换；合成回执只验证程序状态，不代表小红书真的收到稿件。测试自有进程在结束后关闭，不复用8786正式服务。

## 实施内容

- 对话、业务目标、交互命令、执行尝试分离；本地继续/状态/暂停不调用LLM。
- 原要求复用原PG thread；确认修改后建立新thread，按稳定job/post/内容版本引用接管剩余任务。
- PostgreSQL目标投影、命令认领与围栏；保留已交付、待上传及不确定结果，禁止盲目补传。
- 主控及校准请求前装配上下文、自动压缩历史前缀；原文保留，摘要不改变任务账本。
- 模型上下文窗口可选元数据、检查点保留、真实输入用量审计及按模型隔离的保守估算校准。
- GUI展示目标数量、接收/排队/生效、候选差异与压缩状态；新增使用说明见README。

只新增四个生产模块：`conversation_service.py`、`goal_store.py`、`conversation_control.py`、`context_builder.py`；复用现有框架、数据库、压缩、模型、工具和前端组件。

## 测试结果

最终两套完整回归已结束，当前源码与隔离工作树的49个代码/测试/README文件逐一SHA256一致：

| 检查 | 最终结果 |
| --- | --- |
| 独立项目`pytest tests -q --tb=short` | 748通过，362.06秒，4项既有FastAPI弃用警告 |
| 工具包`pytest tools/redbook_tools/tests -q --tb=short -rs` | 1187通过、3跳过，103.21秒，1项既有Pillow弃用警告 |
| 实际HTTP/PG浏览器及性能 | 3项通过，线程均结束，桌面与390×844移动布局通过 |
| 前端`tsc --noEmit && vite build` | 通过，入口JS408.39kB、CSS44.65kB |
| 原工作流及独立工具CLI `--help` | 均退出0 |
| 文件检查 | `git diff --check`通过；旧工作流Git状态干净；变更文件密钥形状扫描无匹配 |

3项跳过仅为JSON检查点不具备图pending-node元数据的组合，真实PG恢复测试未跳过。两套测试分别使用临时数据库运行；未把集成套件中的受控交付称为真实平台上传。

日志与截图：`E:/AI/codex/redbook_runtime/data/tmp/conversation-continuity-20261015/`。最终日志为`agent-validated-final.log`、`tools-validated-final.log`、`gitnexus-validated-final-analyze.log`和`continuity-performance.json`；桌面/移动截图在同目录。早期失败日志为TDD与修复过程证据，不代表最终状态。

## 关键链路

真实PG执行图先生成9篇受控新闻、交付7篇并保留2篇，随后暂停。通过GUI发送一句“继续”，真实API接受UUID和原目标引用，复用原thread，最终达到10篇新闻、1篇AI资讯、1篇地图；新闻只补生成1篇，12个不同post只产生12份受控交付回执。要求改变时的另一项集成测试使用新thread并复用原有效稿件。

最终审查发现并修复以下反例，每项有先失败再通过的回归：禁止上传的补充未生效、取消后旧排队修订复活、未启动目标携空attempt被422拒绝、编辑候选刷新选回旧版。同步修复后续暂停被旧修订覆盖；真实GUI补验未启动目标问答、编辑与刷新不启动内容worker。

完成审计还补齐逐稿保存后的协作检查：复用已有post_saved_callback，在产物持久化后处理暂停，停止继续接纳批次结果，保留已完成稿件。`test_conversation_generation_boundary.py`使用真实PG和实际CLI回调先复现未暂停，再验证暂停与保留；旧工作流不带goal时行为不变。“继续，不要重新开始”也有否定新任务意图的回归，不再另建候选。

最终全量曾暴露一次Windows `WinError 5`：写好的临时检查点原子替换目标时被拒绝。用真实Windows读句柄不共享删除权限可确定性复现同一异常，未断言原失败时的具体持有者。已有写入函数增加最多6次替换尝试，仅处理Windows错误5/32/33，累计等待310ms；不重做模型或平台操作。永久拒绝访问仍抛错、旧检查点保留，其他文件系统错误不重试。新增6项回归先5失败1通过，修复后6通过；没有删除检查点或把写入异常吞掉。

## 验收矩阵

下表列出实际测试证据；不是用代码搜索代替运行。精确名称在相应测试文件，最终整套日志证明这些用例实际执行。

| 场景 | 对应证据 |
| --- | --- |
| C01-C03 原目标继续、缺口生成、防重复 | `test_conversation_continuity_integration.py`、`test_conversation_continuity_live_browser.py` |
| C04-C06 歧义、多目标、混合状态、真实数量 | `test_conversation_continuity.py`、`test_conversation_goals.py` |
| C07-C08 修改待确认、仅剩余生效 | `test_conversation_amendments.py`、`test_conversation_calibration.py` |
| C09-C10 栏目保留、新thread接管 | `test_conversation_continuity_integration.py`、`test_conversation_handoff.py` |
| C11-C12 降量、交付变更、在途新回执 | `test_conversation_remaining_counts.py`、`test_conversation_amendments.py` |
| C13-C14 请求重放、租约、PG依赖 | `test_conversation_goals.py`、`test_plan_postgres_concurrency.py`、`test_execution_safety_regressions.py` |
| C15 不确定结果不盲重传 | `test_conversation_goals.py`及既有PG交付恢复回归；审查者真实PG反例探针确认上传调用仍为1次 |
| C16 跨日及旧数据归属 | `test_conversation_continuity.py`的跨日确认与保守legacy迁移用例 |
| C17-C18 后续请求压缩、并发尾部 | `test_conversation_context_runtime.py`、`test_conversation_compaction_v2.py` |
| C19 已取消要求及记忆失效 | `test_conversation_compaction_v2.py`的两轮压缩、撤回偏好及结构化摘要过滤用例 |
| C20-C22 工具配对、窗口、不可截断输入 | `test_conversation_context_builder.py`、`test_model_context_window.py` |
| C23-C24 摘要失败、紧急重试、重启 | `test_conversation_context_cli.py`、`test_conversation_context_runtime.py`、`test_conversation_handoff.py` |
| C25 刷新、长中文、窄屏 | 10项模拟HTTP浏览器用例及3项真实HTTP/PG界面与性能用例；截图与控件命中检查 |
| C26 平台实机续做 | 本轮未授权真实平台验证，不宣称真实模型生成/小红书上传成功；受控完整链路已覆盖 |
| C27-C30 压缩目标非门槛、usage隔离、摘要不冒充断点、未生效消息 | `test_conversation_context_runtime.py`、`test_conversation_context_builder.py` |

## 性能口径

各操作预热10次，顺序测量100次。路由耗时包含PG读写；HTTP ack包含完整响应体。基线由`git archive 5220fb64dccbb1d26a2481fcb96c9732afad4641`的独立子进程测量，不使用已被修改的当前源码冒充旧版。最终数值以JSON中的同轮结果为准。

| 操作 | 路由及持久化p50/p95 | 完整HTTP响应p50/p95 |
| --- | --- | --- |
| 新版status | 41.12 / 57.40ms | 45.20 / 62.36ms |
| 新版活动continue | 184.48 / 300.93ms | 188.48 / 304.60ms |
| 旧版status | 17.00 / 26.57ms | 20.23 / 30.62ms |

新版因业务账本和持久化交互增加开销，不能宣称状态查询提速；实测均满足路由p95<500ms、接收p95<1s。旧continue没有同目标续跑语义，标为不可比较。

性能阶段新增内容、LLM请求、外部连接和worker均为0。受控恢复总共生成10篇新闻、1篇AI和1篇地图；原9篇新闻未重做，12个不同产物各交付一次。输入token审计及压缩预算使用受控provider usage回归验证，不提供虚构的真实MiniMax消耗或压缩收益。此处是会话控制响应时间，不是新闻采集、推理、生图或平台保存时间，不能据此承诺大批量生成分钟数。

## 取舍与限制

- 保留既有明确manual策略；新PG对话默认auto。未知窗口按16384估算并明确标记，不视为供应商能力承诺。
- 压缩0.50输入预算是优化目标，未达到但仍容纳时继续；当前要求本身过长则明确阻塞，不裁掉后执行。提供商窗口错误最多一次紧急压缩重试，手动策略不被绕过。
- 运行中控制在安全边界应用，在途网络提交不硬杀；外部平台无法承诺exactly-once，回执不确定保留对账保护。
- 双测试树分开执行，避免历史同名测试模块冲突；不会为凑绿改名或削弱原目录安装路径断言。
- 最终图变更检查需要完整返回；动态/跨语言依赖仍以源码和运行测试补证，不将图中的无边当作无影响。
- 最终重新索引后`detect_changes(scope=all)`返回241个变更符号、197条相关流程、28个已跟踪变更文件，符号与流程数量和返回数组一致，无partial/truncated标记。整体风险CRITICAL，相关高风险入口已在编辑前告知并回归。新增模块已索引，但工具的Git差异清单不等于未跟踪文件清单；另外检查新增文件与源码一致性。索引器自身报告流程排名/分支上限，不声称全图穷尽所有动态执行路径。
- 本轮不接入公开发布、不查询订阅额度、不切换付费API，也未安装Redis或改用其他Agent框架。没有把暂停/登录/限额等外部阻塞改成无限循环。
- 当前启用边界为`AGENT_CONVERSATION_CONTINUITY`总开关及已有的单对话manual/auto策略，不增加三套重叠开关。回滚须先安全暂停新版attempt并保留任务、交互与产物，不能直接让旧worker消费新修订。

下次正常启动独立智能体后载入新代码。原工作流仍保留，不要求为人工CLI创建对话目标。未运行真实账号任务，不能推断新闻真实性和实际平台审核必然通过。
