# 独立智能体 Web 入口不限时与断点身份修复

## 目标与范围

自动生成的采编计划不再推算 30/120 分钟截止时间；续跑始终关联原 PostgreSQL thread；前端显示已保留稿件和每栏目的进度。

本子任务修改独立工具包 `apps/web_service.py`、独立智能体后端进度接口与前端必要显示，不修改 `apps/cli.py` 或 `src/agent/editorial_agent.py`。核心补缺、图像审核与 PostgreSQL 恢复由其他代理实现、主代理整体验证。

## 已证实的原因

1. `_parse_agent_message` 按条数计算共享时间预算，最高 120 分钟；`plan` 又把 `0` 限制成最少 0.1 分钟。
2. 恢复提交虽然传入原 `--run-id`，`submit` 却将新的 Web job ID 保存为 `agent_run_id`。
3. `/api/runs/{id}` 使用 Web job ID 读取检查点，造成已审查、已保存结果缺失；再次恢复找不到检查点或计划。
4. 前端用原计划 `job_id` 判断“执行中”，续跑产生新 ID 后显示成“计划已执行”。

## 执行身份契约

| 字段 | 含义 | 恢复行为 |
| --- | --- | --- |
| `id` | 本次 Web worker 尝试 ID | 每次恢复产生新 ID |
| `agent_run_id` | 原任务 PostgreSQL checkpoint/thread ID | 始终不变 |
| `resume_of` | 本次从哪个 Web 尝试恢复 | 指向恢复请求中的运行 ID |
| plan.`job_id` | 首次提交的 Web 尝试 | 不覆盖 |
| plan.`resume_job_id` | 最新恢复尝试 | 恢复后更新 |
| plan.`agent_run_id` | 计划对应的原检查点 ID | 首次执行及恢复时保存 |

新建执行时 `id == agent_run_id`。恢复命令必须同时使用原 ID 的 `--run-id` 和 `--resume-from data/runs/agent/<原ID>/checkpoint.json`，由 CLI 用同一 thread 读取 PostgreSQL 状态。Web 不复制检查点、不创建替代线程、不直接读写 PostgreSQL checkpoint 表。

兼容旧续跑记录：优先使用正确的 `agent_run_id`；旧版本把它写成自身且不存在对应检查点时，通过所属对话的原计划定位原 ID。恢复成功提交后修正旧尝试的关联元数据，保证再次查看旧记录仍能读到原任务。

## 时间契约

- 所有对话自动生成计划、首次确认和旧计划恢复均传 `budget_minutes=0.0`。
- `Workbench.plan` 默认 `0`，接受有限非负数；`0` 表示不限时；拒绝负数、布尔值、NaN、Infinity、null。
- 不自动从栏目数推算预算，不从旧 plan 恢复 120 分钟。
- 显式正值仍按原值透传供调用方兼容；执行器如何处理由 CLI/核心代理契约决定。本次不承诺正值能触发运行截止。
- 模型请求及浏览器步骤原有独立超时仍存在；没有新加任务共享时间截止。

## 进度契约

`GET /api/runs/<WebID>` 先解析原 `agent_run_id`，再读取它的本地审计检查点；该检查点由核心执行器与 PostgreSQL 状态同步持久化。

- `job_states[栏目索引]` 保存非当前栏目的 `post_ids`、`reviewed_post_ids`；当前栏目的顶层快照优先。
- 数量按稿件 ID 去重，检查点状态优先于过往日志的成功条数。
- `retained_post_ids` 为已审查但尚未交付的稿件，排除已上传和本地交付完成的稿件。
- `post_rows` 合并所有栏目本地稿件和已上传稿件，提供标题、图片数与回读证据。
- `activity.jobs[].retained` 与 `activity.counts.retained` 为保留待上传数量；前端在原进度区域显示数量和逐篇标题，不重新设计页面。
- `saved`、`verified` 继续区分：本地保留不等于平台保存，平台保存不等于回读确认。

## 主代理实跑 API 顺序

服务默认 `http://127.0.0.1:8786`；Host 必须对应当前 `REDBOOK_AGENT_PORT`，不能换成 localhost。写请求携带 `X-Workbench: 1` 和 JSON Content-Type；保留 session cookie。确认与恢复可携带 `Idempotency-Key`（16–80 位字母、数字或连字符）。

1. `POST /api/session`，保存响应的 `redbook_agent` cookie。
2. `POST /api/conversations`，JSON `{"title":"今日采编"}`，取 `id`。
3. `POST /api/conversations/<id>/messages`，JSON `{"content":"用 MiniMax 订阅生成10条每日新闻、1条每日AI讯息、1条每日全球事件关注图，审核并上传小红书草稿箱"}`，取 `plan.id`、`plan.version`。发消息只生成计划，不启动 worker。
4. `POST /api/plans/<plan.id>/confirm`，JSON `{"conversation_id":"<id>","version":1}`，使用实际返回版本。返回 Web job `id` 和稳定 `agent_run_id`。保存这两个 ID。
5. `GET /api/runs/<WebID>` 轮询进度。不要重复发生成消息或重复创建计划。
6. 主代理受控中断 worker 后，先确认该 Web 记录已为终态、worker 已结束，再 `POST /api/runs/<WebID>/resume`，JSON `{"conversation_id":"<id>"}`；新恢复尝试使用新幂等键。
7. 以后轮询返回的新 Web `id`；再次恢复可使用最新 Web ID，`agent_run_id` 应始终等于首次 ID。

恢复接口拒绝跨会话、仍在运行、缺少检查点和已经完成的任务。恢复没有“新建生成”语义，不需要追加对话消息。独立后端当前没有专门的停止接口，本子任务不新增停止路由；受控中断方式由主代理的整体验证方案管理。

## 测试隔离与验收

- 使用 `redbook_agent/.venv`，不新增依赖。
- API 测试使用真实 FastAPI 路由和 Workbench 命令构建，替换会话存储和执行 worker，所有文件放在 E 盘测试独占目录。
- 实际 HTTP 测试在 127.0.0.1 的随机空闲端口启动 Uvicorn，替换启动迁移和会话存储，验证 session → 对话 → 确认 → 中断标记 → 恢复 → 进度读取；结束关闭服务。
- 浏览器测试使用现有 Chrome headless、临时 profile，并将所有请求在本地拦截响应；不连接创作者中心或真实业务 API。
- 不访问生产数据库、不启动新闻生成、不调用模型、不上传草稿、不读取或写出密钥内容。
- 覆盖新旧预算、重复确认、连续两次恢复、旧错误 ID 兼容、逐栏目保留、回读计数、桌面/390px 移动布局。
- GitNexus `api_impact` 未找到独立后端路由；旧 `auto_redbook` 索引只覆盖拆分前代码。`Workbench.plan` 的图风险为 HIGH，调用链包含 submit/execute/resume，已用实际文件引用和对应测试补充验证。不能将旧图的空结果当成独立后端无影响。

## 本轮验证结果

2026-10-01，本子任务测试共 100 项通过：

| 验证 | 结果 |
| --- | --- |
| `redbook_tools/tests/test_web_gui.py` | 58 passed |
| `tests/test_run_resume_contract.py` + `tests/test_progress.py` + `tests/test_progress_api.py`，排除生产 PostgreSQL 测试 | 41 passed，1 deselected |
| `tests/test_retained_progress_browser.py` | 1 passed |
| `frontend/npm run build` | TypeScript + Vite 构建成功 |

实际 HTTP 测试覆盖 Uvicorn startup、身份会话、三栏目一句话计划、确认启动、恢复和进度读取；生成 worker 与数据库迁移是显式测试替身。测试记录不能替代真实 PostgreSQL/模型/平台验收。

测试独占目录：`E:/AI/codex/redbook_runtime/data/runs/tests/web-entry-resume-20261001-c41b/`。最终浏览器截图位于其 `browser-final2/test_resumed_progress_renders_0/retained-desktop.png`、`retained-mobile.png`，移动截图等待原侧栏收起动画完成，已人工查看。早期失败证据归档在 `early-checks/`。

现有 FastAPI `on_event` 和 LangChain 的弃用警告仍存在，与本次修改无关；没有升级依赖或改生命周期实现。

本次未重启生产后端。主代理整体验证前需在确认现有 worker 已结束后受控重启独立后端，使新的 Python 代码生效；前端 dist 已构建。生产 PostgreSQL + MiniMax + 平台断点续跑交由主代理实跑验收，不将离线合同测试等同实网成功。

## 主代理集成验收回报

主代理于本轮交接时提供以下结果，此处为集成回报，不重复运行或冒充本子代理执行：

- 独立 agent 全套：53 passed，包含 browser，31.89 秒。
- tools 提示词、缓存与 Web 测试：88 passed。
- PostgreSQL 实机 checkpoint 断点测试：1 passed。

下一阶段由主代理重启 8786 服务，进行三栏目单句实跑及受控中断恢复；本子任务停止代码修改，不修改任何生产 job 数据。
