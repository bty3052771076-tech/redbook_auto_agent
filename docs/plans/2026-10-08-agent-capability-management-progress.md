# 能力中心实施记录

设计依据：2026-10-08-agent-capability-management-design.md、2026-10-08-agent-capability-management-ui-acceptance.md。

## 范围与验证

- [ ] 统一能力目录、PostgreSQL 配置版本、快照、调用和撤销。
- [ ] 智能体 GUI/CLI 执行策略贯通及内部子能力检查。
- [ ] MCP 路径、连接管理、stdio/HTTP、工具绑定、按需生命周期。
- [ ] SKILLS 导入预览、版本、停用和实际选用记录。
- [ ] 长期偏好、知识库命名空间/用途排除、上下文和断点管理。
- [ ] 六页签能力中心与对话本次能力联动。
- [ ] 自动化、真实 PostgreSQL/MCP、浏览器响应式与小规模草稿链路验收。

## 当前证据

2026-10-08：工作树原来只有两份设计文档未跟踪；代码基线 b7ba98c。独立仓库 GitNexus 索引与 HEAD 一致。已分析 execute_agent_plan、EditorialAgentTools、MCPManager、SkillCatalog、KnowledgeStore、_agent_memory_for_execution、compacted_context、App。

compacted_context 风险 HIGH：影响 agent_context_status、execute_agent_plan、confirm_plan。App 图中 UNKNOWN，实际由 frontend/src/main.tsx 导入渲染确认入口。

## 实施决定

- 继续在用户现有目录实现；用户此前明确不需要隔离副本。
- 实施记录保留在项目文档中；测试产物进入 E 盘 runtime/data/tmp，不放根目录。
- 不提交或推送 Git，用户本次授权为编码和测试。
- PostgreSQL 保持生产唯一持久化；测试资源采用独立 namespace 和唯一 ID。

## 验证记录

前次实施已有以下局部证据，不等于完整能力中心验收通过：

- 核心策略/版本/真实 PG：6 项通过。
- MCP 本地真实发现/只读调用及配置/schema：4 项通过。
- Skills 导入/hash/版本：4 项通过。
- 偏好/作用域/检索用途与 namespace：4 项通过。
- 管理 API 读取/版本/检测/偏好：4 项通过。
- 前端构建通过；最近浏览器轮次 6 通过、2 失败。

尚未完成共享执行入口、内部子调用、真实技能/偏好注入、MCP 生命周期、上下文完整集成、完整恢复和真实草稿链路验收。全部清单项仍未视为完成。

## 2026-10-09 本轮边界

以用户最新“现在设计”请求为准，停止继续业务代码修改；已通知前端协作者停止，并保留现有工作树改动，不回滚。

补充[设计复核与实施边界](2026-10-09-agent-capability-management-design-review.md)。本轮只检查并完善文档，没有继续执行新功能测试、平台写入或 Git 提交。
