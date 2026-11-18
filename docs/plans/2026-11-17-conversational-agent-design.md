# 真正的对话采编智能体改造

日期：2026-11-17。目标程序：`redbook_agent`，端口 8786；不是另一个 8790 工作台。

## 1. 目标与边界

默认对话由语言模型决定下一步工具调用，读取结果后继续推理、交付或询问用户。不得把整批任务交给旧 `run_editorial_agent` 后无限补位，也不得用关键词解析冒充智能体。

保留已有工作流、CLI、模型连接管理、新闻工具、专用 profile、草稿、知识库、人工审稿和历史记录。已有来源和日期校验不能为了完成数量而关闭。公开发布、平台删除、任意 shell、自动安装 MCP/Skills、擅自切换付费接口仍然不允许。

## 2. 已确认的问题

- 现有对话首先由正则转成计划，再启动 `redbook_tools agent` 批处理。模型不是对话执行循环的主控。
- `ConversationService` 只将取消写入排队控制；`ConversationControl.check_boundary` 主要在图节点切换时检查。
- `generate` 节点内可能尝试数十个新闻候选。资源等待虽支持 stop_event，但对话取消未贯通，因此一整个生成阶段结束前取消难以生效。
- 实机曾出现取消未及时执行；停止任务自己的 worker 后，目标确认为 cancelled，原有草稿和检查点保留，其他程序未停止。

## 3. 框架选择

采用 LangChain `create_agent` 的模型/工具循环，使用 LangGraph 的 `interrupt` / `Command(resume=...)`，检查点存真实 PostgreSQL。不是再编写一个固定 plan -> generate -> review -> upload 调度图。

本机现有 LangChain 1.2.0、LangGraph 1.0.5 与 prebuilt 1.0.13 存在导入冲突：`ExecutionInfo` 缺失。兼容性是启动硬门槛；必须用匹配依赖验证真实框架，不能捕获异常后降级为自研循环或内存检查点。依赖修复仅在 E 盘独立环境进行，需用户同意，不修改共享虚拟环境或其他服务。

模型适配复用已有 `RuntimeClient` 的 Chat Completions / Responses / Anthropic 编解码、凭据、模型角色、限流和订阅限制，通过 LangChain `BaseChatModel` 暴露工具调用。主控、写稿、生图保持独立。模型循环不进行未授权的供应商回退。

参考：
- https://reference.langchain.com/python/langchain/agents/factory/create_agent
- https://reference.langchain.com/python/langchain/agents/middleware/human_in_the_loop
- https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/human-in-the-loop.mdx

## 4. 架构与职责

```text
React 对话 / 能力申请 / 暂停与取消
  -> FastAPI 会话接口与控制接口
     -> 持久会话、工具动作和授权账本（PostgreSQL）
     -> 独立会话执行进程
        -> LangChain create_agent + LangGraph PostgresSaver
           -> 已有模型平台 RuntimeClient
           -> 受限领域工具 / 已登记 MCP / Skills
              -> 新闻材料、单篇写稿、配图、审查、草稿保存、RAG
```

独立进程隔离用于可中止的执行环境，不代表固定脚本编排。进程内的下一步由框架模型/工具循环决定。旧手工计划和 CLI 保留为人工入口，默认聊天不再启动旧整批执行器。

## 5. 工具粒度与保留机制

工具应复用已有领域函数和能力登记，不重复实现新闻抓取、生图或上传客户端。

| 类别 | 粒度与结果 |
| --- | --- |
| 环境、能力、进度、RAG、历史草稿 | 只读，返回真实状态，不暗中启用其他能力 |
| 材料采集与查验 | 返回有 ID、日期、来源和去重信息的候选；采集不到返回缺口 |
| 新闻/我去写稿 | 每次一个指定材料或一个候选，不把10篇整批藏在工具内 |
| AI讯息/AI福利/全球图 | 一次一篇集合稿；材料不足或无可领取福利是明确结果，不旧闻凑数 |
| 图片生成/修改 | 一次一个稿件的图片操作；遵守当前绑定和参考图策略 |
| 审稿与草稿保存 | 单篇审查；独立保存合格稿。平台操作串行，回执核对后才交付 |
| MCP/Skills | 从现有登记目录发现并按权限调用；未知能力只申请配置，不执行模型提供的命令 |

每次动作具有持久 ID、参数摘要、来源会话、阶段、产物和平台回执。完成结果可复用；重启后 running 动作先记 uncertain，不能自动重放。保存动作按稿件内容版本去重，不只按模型生成的 tool_call_id 去重。人工工作流的数据不搬迁、不删除。

## 6. 能力申请与失败处理

未启用、未授权、缺凭据、缺模型路由时，框架中断并在对话中显示：需要什么工具、为何需要、将操作什么、风险、授权范围和下一步。

用户可允许本次、拒绝或前往能力中心配置。授权只能针对明确工具、参数/稿件范围、有效期和数量，不允许模型给自己开全局权限。能力被全局撤销、平台限制、公开发布等禁用项不能被会话授权覆盖。

生成与平台保存分开申请。批量目标可授予有边界的生成额度，避免每一篇都重新询问；平台保存授权绑定实际稿件范围，不能隐式扩展。

- 参数或材料错误：至多一次有明确变化的修正尝试；相同原因再次失败，进入 waiting_input，询问用户。
- 缺权限、缺配置、额度不足、平台风控：立即询问，不自动重试。
- 模型持续调用同一无效动作或换关键词绕过失败：宿主按操作类别和错误指纹限制，不能只靠提示词。
- 平台写入不确定：先只读核对，未核对不得重传。
- 无 AI 福利：明确告知当日没有查到可核验且可领取的活动。

不使用120分钟共享任务预算。单次网络调用和退出清理仍有合理超时；超时是询问/恢复的条件，不是重跑整批的理由。

## 7. 暂停、取消与对话恢复

| 操作 | 语义 |
| --- | --- |
| 暂停对话 | 立即停止当前主控和未开始动作，保留消息、授权申请和检查点；同一对话可继续 |
| 取消执行 | 持久撤销该次执行授权，停止对应 owned worker，取消其未开始动作；不删除已有稿件 |
| 继续 | 从已确认检查点和动作结果继续，不把已经完成的生成/保存再执行一遍 |
| 运行中补充要求 | 先中止当前轮调度，再将新消息送入同一会话；对已提交但结果未知动作保留不确定状态 |

控制接口不经过模型识别、不等待长工作锁。先落库撤销授权，再发送停止信号；到达短暂宽限期后只结束该执行身份匹配的子进程树。不能全局杀 Python/Chrome/Codex/数据库。

单个已发出的上游请求可能已经产生用量，不能承诺撤销远端计费。平台提交被中断时记录 uncertain，不声称已撤回保存。聊天接口必须诚实显示 stopping，确认 worker 退出后才显示 paused/cancelled。

## 8. 前后端接口契约

- 默认对话入口走新的会话模型循环；保留人工计划入口，避免破坏现有人工脚本使用。
- `GET /api/conversations/{cid}/assistant`：返回引擎、状态、轮次、最新事件、待处理能力申请、已保留产物。
- `POST /api/conversations/{cid}/assistant/messages`：接收新消息，持久化幂等键后异步返回，不在 HTTP 请求内等待整个生成任务。
- `POST /api/conversations/{cid}/assistant/control`：pause/cancel，绑定当前执行身份；重复控制幂等。
- `POST /api/conversations/{cid}/assistant/requests/{request_id}`：允许/拒绝/用户回答，校验版本和授权范围后原检查点恢复。

接口复用现有本机 Origin、cookie、校验头和会话所有权检查。模型不能直接调用这些管理接口。浏览器刷新只读状态，不重复创建轮次。

界面在同一对话显示简短模型说明、工具开始/结束、阻塞原因、授权申请、明确的暂停/取消/继续入口。不展示内部推理链，不把命令行日志当成唯一进度。

## 9. 模块与实施顺序

1. 独立依赖兼容门槛、设计与基线保护；保留用户当前未提交修改。
2. 修复旧任务即时控制，防止现存会话继续消耗资源。
3. 原生框架会话服务、PostgreSQL检查点、已有模型适配和受限工具执行。
4. 对话能力申请与失败中断，旧计划退回人工入口，接入React交互。
5. 独立测试智能体复测，父智能体审查；通过后再加载正式服务。

不新建与已有能力中心、模型平台、知识库重复的管理系统。只有框架会话生命周期与动作幂等等现存模块确实不能承担的职责才新增模块。

## 10. 已实现的模块边界

| 文件 | 职责 |
| --- | --- |
| `backend/dialogue_api.py` | 认证后的异步消息、审批、暂停/取消入口；不在HTTP线程等待模型 |
| `backend/dialogue.py` | 对话轮次、模型角色快照、进程身份与控制；启动失败可重试，补充消息先停止旧轮次 |
| `backend/dialogue_store.py` | PostgreSQL会话、事件、审批与动作账本；CAS、动作去重、执行级授权、公开消息过滤 |
| `backend/dialogue_worker.py` | 真实create_agent、原生interrupt/Command、上下文压缩与失败中断中间件 |
| `backend/dialogue_model.py` | 现有RuntimeClient到BaseChatModel的薄适配；保留订阅限制，屏蔽内部思考标签 |
| `backend/dialogue_tools.py` / `dialogue_tool_worker.py` | 15项有界领域工具；另有原生request_user_input；复用现有采集、生成、保存、RAG/MCP/Skills实现 |
| `frontend/src/DialogueChat.tsx` | 持续对话、工具结果、授权卡片、暂停/取消和错误处理；人工计划保留在App原组件中 |

审批身份绑定宿主保存的原始AI消息、工具、参数和调用身份，不能只依赖模型提供的tool_call_id。授权绑定当前执行、日期、次数及栏目/稿件/MCP工具范围；原生审批续跑只继承同源授权，新消息不继承旧轮次消费额度。模型改用同一个调用ID不能扩大生成授权为平台写入授权。

长期上下文使用原生SummarizationMiddleware的工具配对保留策略：约12000个本地估算token触发，压缩后保留约4000 token的近期上下文及完整工具配对。原始消息仍在PostgreSQL，动作账本不压缩；imported_event_id防止下一轮将已压缩旧消息重新导入。无效摘要停止并保留原始历史，不用错误文本代替摘要。

依赖修复使用 `data/assistant-python` 私有覆盖目录，已验证LangChain 1.2.0 / LangGraph 1.0.5 / prebuilt 1.0.5。`scripts/setup_dialogue.ps1`只在E盘安装，`-CheckOnly`不安装；共享虚拟环境、CC Switch和其他程序不改动。正式服务仍使用Start-Agent.cmd和8786，数据库不降级。
