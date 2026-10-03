# 独立采编智能体

本程序位于 `E:\AI\codex\redbook_agent`，通过 `redbook_tools` 调用新闻采集、写稿、生图和草稿上传工具；PostgreSQL、草稿、图片、日志和专用浏览器 profile 保存在 `E:\AI\codex\redbook_runtime`。启动和运行不读取旧 `redbook_workflow` 目录。旧项目及其 `Start-Web-GUI.cmd`、`apps.cli` 仍保留，可继续人工使用。

## 快速开始

1. 双击 `Start-Agent.cmd`。脚本仅在 E 盘启动独立 PostgreSQL（端口 5433），然后启动本地 API 与已构建的前端。
2. 用浏览器打开 <http://127.0.0.1:8786>。浏览网页可以用默认浏览器；小红书登录请在“连接与模型”中点击“打开专用浏览器”。两者的浏览器 profile 彼此独立。
3. 在“连接与模型”检查数据库状态，分别选择智能体主控、写稿和生图模型。“自动使用当前配置”会使用运行区的本地配置。密钥只填在 `E:\AI\codex\redbook_runtime\.env.gui`，不要放进代码、对话或 Git。
4. 回到“对话任务”，输入例如“生成 1 条每日新闻，只生成本地稿，不上传平台”。查看右侧计划，确认栏目、数量、交付方式后再点“确认并执行”。对话中的“运行播报”会持续更新当前阶段、耗时、各栏目完成数量和错误原因；生成结果见“草稿审查”。
5. 草稿页展示正文、图片、原始信源、日期和平台回读状态。来源、日期、内容、图文一致性应逐项人工核验。独立界面目前不执行公开发布；需要公开发布时请在平台人工确认。

浏览器刷新不会取消后台任务。API 只绑定 `127.0.0.1`，不要将端口直接暴露公网。当前专用浏览器 profile 在新运行区，必须单独登录；没有验证登录前，不要假定能上传。

## 实时了解任务

- 对话中的“运行播报”和右侧“最近一次运行”使用中文摘要。详细页面仍在“运行记录”；原始输出放在可展开的“技术日志”中。
- “产出记录”“通过审查”“平台保存”“回读确认”分别统计。未知数量显示“待记录”；本地成稿不等于上传成功，平台保存不等于回读确认。
- 点击“询问进度”，或者输入“现在进度如何”“卡在哪里”“完成了吗”，会返回本次任务的实际状态，不创建新任务、不调用模型。无法可靠预测时，不编造剩余时间。
- 无新事件时显示最近动态的时间；连接中断时保留最后状态并重连。任务结束后，已执行计划不能重复点击提交。
- 更新后请等待当前任务结束，再重启 `Start-Agent.cmd` 载入新版后台；不要为更新界面中止正在运行的生成/上传。旧后台仍运行时，新界面兼容显示阶段、耗时及已有平台记录，完整栏目统计和持久化进度问答在重启后启用。

## 本地 AIHOT 信源

“每日AI讯息”可额外读取部署在 `E:\AI\codex\AIHOT` 的开源 AIHOT。运行前执行 `& 'E:\AI\codex\AIHOT\scripts\local-stack.ps1' start -Worker`，结束后执行相同脚本的 `stop`；站点地址为 <http://127.0.0.1:8768>。独立运行区的 `.env.gui` 已配置本地 API 地址。AIHOT 使用另一套 E 盘 PostgreSQL（5434），不会读取旧项目的浏览器 profile 或草稿。若该服务未启动，智能体会报告该聚合信源的连接失败，并继续尝试其余信源。AIHOT 仅提供可核验原始网址和发布时间的候选线索，最终仍由智能体执行时效、查重与内容质量检查。

## 安装与更新

### ChatGPT 订阅生图

运行区 `.env.gui` 的 `OPENCODEX_IMAGE_ENABLED=1` 启用本机 OpenCodex 图片连接；在“连接与模型”的生图角色选择 `opencodex:gpt-image-2`，主控和写稿可继续使用 MiniMax。额度显示为“订阅额度未查询”，不是免费无限额度。新连接最多 2 并发，普通生图可用 `OPENCODEX_IMAGE_FALLBACK=minimax` 切换 MiniMax Token Plan，但不会切到付费 API。

AI鸡蛋/AI福利沿用每日羊毛栏目，通过运行区 `assets/wool` 的构图参考与对应厂商人设编辑封面。成功图片持久化缓存；超时或进程中断后不能确定是否完成的请求不会自动重发。双图编辑不自动降级成普通文生图。OpenCodex 更新不会覆盖工具包，但代码或接口变化时必须重新核验，不能保证未知版本自动兼容。

参考图与人设图必须位于独立运行区，不能只复制代码。新旧入口使用同一个 E 盘 `OPENCODEX_IMAGE_LOCK_DIR` 和 `OPENCODEX_IMAGE_STATE_DIR`，共同限制两路并发；响应不确定时，换稿件 ID 也不能绕过重复提交保护。遇到 `OPENCODEX_PREVIOUS_REQUEST_UNCERTAIN` 时先核查原请求，不要删除记录盲目重试。

现有 E 盘虚拟环境、前端构建和 PostgreSQL 已准备好。重新构建前端：

```powershell
Set-Location E:\AI\codex\redbook_agent\frontend
npm ci
npm run build
```

重建独立 Python 环境时，在 E 盘执行：

```powershell
Set-Location E:\AI\codex\redbook_agent
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`tools/redbook_tools` 是本仓库自带的领域工具包；安装 `requirements.txt` 后，`python -m redbook_tools --help` 可查看人工命令，不需要旁边存在旧工作流目录或另一个工具包目录。工具包代码是当前独立工具包的版本快照，旧项目入口仍保持原实现，**后续修复尚不会自动双向同步**；在正式切换旧 CLI 到共享实现前，应分别运行新旧入口回归。不要删除旧目录或其数据。

独立数据库文件位于运行区 `data/knowledge/postgresql-local/pgdata`。首次部署才运行运行区的 `data/runtime/postgresql/initialize.ps1`，它拒绝覆盖现有数据库。日常启动只使用 `Start-Agent.cmd`。旧知识库已非破坏性备份和导入，新旧 PostgreSQL 分别使用 5432/5433；后续旧项目新增数据不会自动同步，需要单独执行增量迁移。

## 验证

### AI鸡蛋候选图库

左侧“AI鸡蛋图库”可浏览候选图、参考原图与厂商人设。候选图先查看大图和作者来源，确认成年、非露骨及使用权限后才可入选；“参考原图”可指定当前使用的构图图。入选后，生成 AI鸡蛋/AI福利时自动用 ChatGPT 订阅进行双图描改，人物身份取自对应厂商人设，禁止付费 API 降级。图库数据在 `E:\AI\codex\redbook_runtime\assets\wool`，与旧程序的人工批准状态分开保存。新界面和新路由需重启本程序后载入，不要中断正在运行的任务。

```powershell
Set-Location E:\AI\codex\redbook_agent
.\.venv\Scripts\python.exe -m pytest -q tests
Set-Location E:\AI\codex\redbook_tools
E:\AI\codex\redbook_agent\.venv\Scripts\python.exe -m pytest -q tests
```

本地运行区、`.env.gui`、数据库凭据、浏览器 profile、草稿图片、日志和模型缓存均不得加入 Git。新程序的 `.gitignore` 与运行区的忽略规则已覆盖这些路径；提交前仍需检查暂存文件清单。没有完成新 profile 的真实平台登录、上传与回读验证之前，不应将“草稿已上传”作为测试通过结论。

## 从 GitHub 部署

本仓库包含后端、前端源码、测试、领域工具包及无凭据的数据库初始化/管理脚本，不包含本机已经准备好的运行区。克隆源码不等于自动获得可用的模型账号、数据库或浏览器登录态。

1. 将仓库克隆到 E 盘，按上述命令建立 `.venv`、安装根目录 `requirements.txt` 并构建 `frontend`。Python 需要 3.10 或以上；Node.js 需满足 Vite 7 的版本要求。pip/npm/Playwright 的下载缓存也应设置到 E 盘。
2. 新建 `E:\AI\codex\redbook_runtime`。将 `deployment/postgresql/initialize.ps1`、`manage.ps1` 放到运行区的 `data/runtime/postgresql/`。该目录还需自行准备 PostgreSQL 18.6 的 `18.6/pgsql/` Windows 二进制及匹配的 pgvector 扩展；本仓库不分发数据库二进制。不要覆盖已经存在的运行区和数据库。
3. 仅在全新数据库部署中执行 `initialize.ps1`，再执行 `manage.ps1 -Action start`。凭据会在运行区生成，脚本拒绝覆盖现有数据库。用本仓库 Python 在本地执行 `from src.knowledge.store import KnowledgeStore; KnowledgeStore.from_env().ensure_schema()` 前，设置 `KNOWLEDGE_DB_CREDENTIALS` 为运行区的 `data/knowledge/postgresql-local/connection.json`，以创建应用表结构。生产必须使用 PostgreSQL/pgvector，不降级。
4. 运行区 `.env.gui` 中填写本人的供应商密钥和模型配置；不要提交该文件。ChatGPT 订阅生图需要另行准备本地 OpenCodex 和已授权的账号连接；仅克隆本仓库不会获得该连接。参考图/人设图、地图 GeoJSON、可选 AIHOT/RSSHub 等外部服务也需要自行配置。
5. 启动 `Start-Agent.cmd`，通过连接页面在专用 profile 中完成平台登录，然后先验证本地生成和草稿上传。首次部署的完整生成与平台链路需要部署者实机验证；本仓库的自动化测试不能替代账号权限和平台审核结果。

现有本机 `.venv` 之前引用旁边的 `redbook_tools`，此次发布不会中断它。下一次重新安装根目录依赖会改用仓库自带工具包。更新时先等待当前任务结束，不要同时运行两份后台竞争同一 profile。
