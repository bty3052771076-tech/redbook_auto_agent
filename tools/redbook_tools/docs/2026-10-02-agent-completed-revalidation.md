# 已完成栏目的恢复重审

## 目标与范围

为旧 AI 摘要错误分类、地图错误定位提供定向修复入口。恢复时不能仅凭旧 `completed` 或上传成功标记跳过新质量规则；也不能因此重新生成已合格栏目。

本次仅修改 `src/agent/editorial_agent.py`，新增专项测试与本文档。CLI 回调接线、审核版本管理和平台原草稿更新由主控负责。未修改 CLI，未启动、停止或重载生产 worker，未进行实机生成/上传。

## 回调协议

`EditorialAgentTools` 增加末尾可选参数，默认 `None`，兼容既有调用：

```python
revalidate_completed: Callable[
    [AgentJob, list[Any], dict[str, Any]], list[str]
] | None = None
```

回调只在显式恢复时用于已 completed 栏目；首次运行不调用。未提供回调保留既有恢复行为。提供回调时，core 按该栏目的 `post_ids` 调用 `load_posts` 实读全部稿件，不能只用 PostgreSQL/JSON 中旧的 Post 对象。

传入 context 保留该 job 已存上下文，并覆盖以下稳定字段：

- `agent_run_id`：原始运行 ID，不随 web attempt 改变。
- `agent_job_key`：原始计划中的字符串索引。
- `agent_target_count`：该栏目目标数。
- `agent_approved_post_ids=[]`：不允许把旧批准缓存当作本次重审证据。

回调必须返回 `list[str]`：空列表表示本次校验无问题；非空列表是具体失败原因。`None`、字符串、包含非字符串或空白错误的列表视为协议错误，不能当作通过。回调应当只读，不修改稿件，不上传、不调用生成流程；修复由恢复后的 review 执行。单次外部请求超时仍由适配器负责，core 不增加整个任务的时间预算。

## 恢复行为

1. 从实际 checkpoint 的 completed 索引和 job 状态识别完成栏目，逐个加载稿件并调用回调。
2. 无问题：维持完成状态，不执行该 job 的 generate/review/upload，不清空原有上传凭据。
3. 有质量问题：只将该 job 设为 pending、`review_complete=False`，清空其 `reviewed_posts`、`reviewed_post_ids`、`approved_versions`，从 `completed_job_indices` 移除。保留其全部原稿与 ID、其他栏目状态和上传历史。
4. 缺少 loader、缺少稿件引用、加载异常、返回缺稿/错 ID：以 `RETAINED_POST_UNAVAILABLE` 阻断对应 job，不能默默跳过或重新生成替代原稿。回调异常/返回结构错误以 `COMPLETED_REVALIDATION_UNAVAILABLE` 阻断。错误记录带栏目索引，并沿用脱敏逻辑。
5. 平台风险、登录、待审核、写入不确定等已有终止码仍设置 platform pause；真实额度/认证失败仍阻断。恢复不会无条件清除平台暂停。显式恢复可重试旧 provider pause，但本次重审发现的新额度/认证问题仍暂停。
6. JSON 恢复沿用原来的重新进入同步/调度语义；修正 completed 空列表被 `or` 当成缺失、从 job_index 推导成已完成的问题。
7. PostgreSQL 恢复以真实 graph checkpoint 为权威，JSON 仅为指针/审计投影。有待执行节点时更新状态 channels 后继续 `invoke(None)`，不改待执行节点、不从 START 重跑已有任务。
8. 原先已全部完成的运行若本次发现不合格栏目，会重新选中该栏目并进入 review。若已有待执行节点恰为 finish，先按原断点执行 finish，再调度本次重新打开的 pending 栏目。

质量错误不会直接批准稿件。恢复 review 仍须通过现有质量门禁；仍不合格时保留稿件和错误状态，而非重新加回 completed。既有公平轮转和无共享墙钟预算保持不变。

## CLI 配合点

```python
def revalidate_completed(job, posts, context):
    issues = []
    for post in posts:
        # 按 job.kind 调用对应已存产物验证器。
        # AI 可调用 stored_ai_digest_review_issues(post.platform.get("ai_digest"))。
        # 地图则验证来源位置证据与输出元数据。
        ...
    return issues
```

主控在 `EditorialAgentTools(..., revalidate_completed=...)` 接线即可。无需另发生成消息；后续对原 run 执行受控恢复时触发。

**重要边界：保留上传审计历史，但历史 ID 不再代表当前版本已交付。** core 仅在当前 `item_key = job_index:post_id:_content_version(post)` 的状态为 `saved`，或状态为 `skipped_local` 且当前仍然 `upload_enabled=False` 时跳过。原本只生成本地稿、后来启用上传的运行，不能凭 `skipped_local` 跳过平台交付。

无版本 `legacy_key`、纯 `uploaded_post_ids`、其他内容版本的 `saved` 都不构成当前版本已交付的证据；上传启用时这些稿件进入现有 `upload_batch` 或串行 `upload` 适配器，由 CLI 核对幂等回执。原 ID、旧 item_status、上传对象和审计事件保留，不删除记录。`load_posts(uploaded_post_ids)` 只用于恢复历史展示对象，不从可能已经被修改的文件推断历史内容版本，也不通过其对象内容补造 `saved` 状态。

CLI 对自己已有 `xhs_draft` 的旧版本应定向 `update` 原草稿，而非重复 `create`；只有核对当前版本确已交付后才返回成功。失败/不确定仍通过现有上传错误与平台风险码处理，不能将“本地语义修好”当作“远端草稿已修好”。core 本次只改变跳过条件，沿用现有内容版本计算与成功回执写入方式，不实现 CLI 的草稿更新动作，也不更改审核批准协议。

本次静态检查的分支预期（未运行测试）：当前版本 `saved` 跳过；当前版本 `skipped_local` 在本地模式跳过、上传模式交给适配器；只有 legacy/历史 ID/其他版本回执时，上传模式交给适配器，本地模式只记录当前版本 `skipped_local`。平台暂停分支优先于上述判断，保持阻断。

## 影响分析

编辑前 GitNexus upstream impact：`run_editorial_agent` 返回 LOW、1 个直接 CLI 调用；`EditorialAgentTools` 返回 LOW、2 个调用。索引仍是旧 `auto_redbook`，内部 `reopen_retained` 与新函数为 UNKNOWN；随后核对独立目录实际调用与恢复代码，没有把 UNKNOWN 当作无影响。更改只落在授权的 core 文件，没有修改共享 CLI。

上传版本 P1 修复前再次执行 GitNexus upstream impact，精确目标为 `_build_graph.upload`：结果 UNKNOWN、未解析出调用者；索引仍为 `E:/AI/codex/redbook_workflow` 的 `auto_redbook`（2026-09-24），不是独立工具目录。随后文本核实 `E:/AI/codex/redbook_tools/src/agent/editorial_agent.py` 中 `graph.add_node("upload", upload)`、`after_review`/`after_sync_context` 路由及批量、逐条适配器调用。实际影响是所有进入该上传节点的栏目及断点恢复，不将索引的零调用者视为无风险。只改该节点的版本跳过判定与本文档。

## 已进行的验证

以下测试发生在用户最新“停止测试”指令之前：

- TDD 首轮：24 失败、3 跳过。主要失败证明既有恢复没有调用回调、没有撤销错误完成状态；其中 finish 中断夹具随后改为在已有 finish 待执行点直接模拟中断，以避免恢复 API 把中断确认后执行完。
- 实现后专项：**24 passed，3 skipped，1 warning，10.25 秒**。
- 用例覆盖 JSON 和 PostgreSQL 恢复分支、实读文件新 revision、合格栏目不重生成/上传、单栏目退回 review、批准缓存清空、修复仍失败不算完成、原稿加载失败、回调异常、平台风险阻断，以及 PG pending generate/upload/finish 保留。
- PostgreSQL 分支使用 `InMemorySaver` 替代连接管理器，执行真实 LangGraph checkpoint/恢复逻辑；**这不是实机 PostgreSQL 数据库验证**。
- 3 个 skip 是 JSON 参数下不适用的 PG pending-node 用例。warning 是 LangGraph 序列化器 allowed_objects 默认值将变化的依赖警告。
- 解释器：`E:\AI\codex\redbook_agent\.venv\Scripts\python.exe`。
- 测试临时目录：`E:\AI\codex\redbook_runtime\data\runs\tests\core-revalidate-green1-20261002`。

## 尚未验证

收到最新指令后未再启动任何测试。收尾时调整了 pending PG 恢复中的 provider pause 处理：先按原显式恢复语义清除旧供应商暂停，再保留新回调发现的暂停；**这处最终小调整未重新测试**。

后续上传版本 P1 修复同样按用户要求未运行测试、编译或生产任务，仅静态核对分支。上方 24 passed 是此前专项结果，不覆盖此修复。Descartes 既有全套结果仍为 26 failed、345 passed、8 skipped，其中 24 个 completed_revalidation 失败；不得以此次静态修改宣称全套已通过。

未跑全套回归、未验证真实 PostgreSQL、未接入 CLI、未验证平台同一草稿远端更新。生产 worker 保持不动，新接口不会自动热加载到既有进程。
