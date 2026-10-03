# 非每日新闻失败稿的最小修复与恢复契约

## 范围

仅修改 `apps/cli.py` 的非 `daily_news` 生成/审核分支，新增对应测试。适用每日 AI 讯息、全球事件关注图、每日我去、每日羊毛。不修改每日新闻分支、质量门禁实现、agent core、backend 或上传函数。

## 根因与影响核对

1. core 在已有 `posts` 时保留生成结果；原非新闻审核仅返回错误，没有替换动作，重试仍审核原稿。
2. core 在审核后保留新旧稿 ID 的并集；PG `AgentArtifactStore.load()` 读取所有 phase，单纯从当前列表删除旧稿或把 phase 改成 rejected 都不能阻止旧稿恢复。
3. 非新闻生成函数原来直接返回，没有在进入 review 前提交逐篇 artifact，生成到审核间中断会丢失 PG 恢复证据。

修改前已对 `editorial_agent_command.generate/review` 执行 GitNexus upstream impact。旧 `auto_redbook` 图不覆盖当前独立 tools 的最新调用关系，结果为 UNKNOWN，不作为低风险证明。实际文件确认调用链为 CLI `EditorialAgentTools` 注入 -> core `tools.generate/tools.review`；core 使用显式通过 ID 决定上传，并保留所有产出 ID。PG 按 run/job 隔离，但不按 phase 排除旧稿。

## 最小实现

- 原非新闻生成逻辑提取为 `generate_non_news`，正常生成及替换均在返回前逐篇 `retain_artifact(..., "generated")`。
- `review_non_news` 逐篇执行原来源检查及 `_run_auto_quality_gate`。仅确实失败且供应商错误非终止类的稿件可以触发一次替换；本次审核不递归重试替换稿。
- 替换直接调用新生成路径，不经过 `generate` 的 retained-artifact 快速返回。AI 摘要/每日我去携带失败原因用于修正，不改日期、官方来源、查重、评分要求及模型计费策略。
- 新稿必须有新 ID。新稿 payload 中保存 `agent_item_replacement`：`job_kind`、`replaces_post_id`、`replaces_version`。新稿关联和 payload 一起提交 PG，之后才把旧 artifact 的 phase 标记为 superseded。
- 恢复审核时，只有本栏目新稿关联的旧 ID、旧内容指纹同时匹配，才从本轮候选输入中排除旧稿。PG 即使返回所有 phase、core 即使保留旧 ID，也不会重新审核这个已替换输入。
- 返回显式 `approved_post_ids/rejected_post_ids`。旧失败稿从不被改判通过；替换稿仍须独立过审。新稿仍失败则继续拒绝，下次处理新稿而非旧祖先。其他栏目 artifact 和已通过稿件保持不变。
- 保留完整历史查重：质量门禁仍使用原 `list_posts()` 历史；同栏多篇候选也额外进行原批次去重。没有设置跳过历史开关，没有清空历史，没有排除其他已上传/已发布稿件。仅不再把本次已拒绝且已被替换的旧输入作为待审核候选。
- 不新增公开发布动作，不修改可见性和上传批次行为。

## 验证

测试使用 `redbook_agent/.venv`，临时数据仅位于：

`E:/AI/codex/redbook_runtime/data/runs/tests/non-news-repair-20261001-9f62/`

先执行失败测试：5 failed，1.65 秒。失败分别证明无替换调用，以及四类非新闻生成没有立即提交 ledger。

完成后执行：

```powershell
& 'E:/AI/codex/redbook_agent/.venv/Scripts/python.exe' -m pytest tests/test_cli_agent_non_news_repair.py tests/test_cli_agent_policy.py tests/test_editorial_agent_retention.py tests/test_review_retention.py tests/test_agent_serial_draft_batch.py -q --basetemp=E:/AI/codex/redbook_runtime/data/runs/tests/non-news-repair-20261001-9f62/final -p no:cacheprovider
```

结果：**41 passed，18.40 秒**；仅已有 LangChain 序列化弃用提示。

新增 15 项用例涵盖：四类生成提交、失败摘要替换、PG 全 phase 回读排除旧稿、提交新稿后中断、失败替换不冒充成功、失败链下一轮处理最新候选、日期/历史重复/低评分错误继续阻断、真实质量门禁仍检出已交付历史重复、终止类额度错误不继续生成、保留其他成功项、新 ID 校验，以及真实 core 回调链仅交付通过的新稿。

PG 使用模拟其全 phase 读取和 payload 深拷贝行为的 ledger 测试替身；最后一项使用实际 core 与 CLI 回调，模型和上传均为离线替身。未调用生产模型、真实平台、生产 PG，未修改真实 run 或服务状态。主代理负责原 run 的 PG 实机 resume 验证。

## 恢复交接

无需迁移 schema，也不需要改旧 checkpoint 或 job JSON。重启/续跑加载新 CLI 后，原 run 中的每日新闻原样保留；轮到非新闻失败稿时才进入替换路径。应观察“替换未通过稿件”事件、新 ID 的 generated/approved 或 rejected artifact，以及仅包含新通过 ID 的审核结果。若原信源或地图渲染本身不可用，仍如实返回失败，不保证一次替换必定合格。
