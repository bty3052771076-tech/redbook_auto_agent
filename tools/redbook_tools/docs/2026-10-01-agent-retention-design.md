# 智能体逐项保留与持续补缺设计

日期：2026-10-01。范围：`src/agent/editorial_agent.py` 与 `tests/test_editorial_agent*.py`。CLI、图片提示词、视觉缓存与逐篇产物数据库由对应模块负责。

## 问题与目标

旧实现中，review 复制 posts 后，工具补选增加的稿件未写回状态；任何批量错误都会清空 reviewed_posts；recover 再清空全部稿件并重新 generate。首轮8篇、次轮9篇合格的工作因此丢失。全局墙钟预算和总重试次数又使后续栏目无法开始。

本设计要求：合格稿、未合格稿及其已付出的工作均可恢复；仅继续审核和补缺；每个栏目有独立状态并公平轮转；默认持续完成目标；明确不可操作时保留原因与恢复入口；质量审核要求不变。

## 与 CLI 的最小协议

core 在每次 generate/review/upload 前提供以下 context 字段：

| 字段 | 语义 |
| --- | --- |
| `agent_run_id` | 原始运行编号；断点恢复不改为 Web 新请求编号 |
| `agent_job_key` | 冻结编排顺序中的栏目索引字符串，例如 `"0"`；同栏目恢复不变 |
| `agent_target_count` | 栏目要求的最终合格稿件数 |
| `agent_approved_post_ids` | core 当前确认且内容版本未变化的合格稿件 ID |

CLI 使用 `(agent_run_id, agent_job_key)` 访问 PostgreSQL `agent.task_artifacts`。generate 首先恢复已有产物；review 首先合并该栏目的持久产物，并在每次生成、审核结束后逐篇写入。每次 review 最多做一轮补选，只补目标缺额；已失败候选的事件标识保留以排除重复选中。图片审核缓存由 CLI 按内容与图片指纹校验。

推荐 review 返回：

```python
{
    "errors": ["当前尚缺2篇合格稿"],
    "approved_post_ids": ["post-a", "post-b"],
    "rejected_post_ids": ["post-c"],
    "retryable": True,
    "retry_after_s": 0,
}
```

`approved_post_ids` 是本次审核后的完整权威名单，不能把上轮已经失效的批准盲目并入。未知 ID 不得计入目标。省略该字段时保留仍有效的已有审核证据；旧的 `list[str]` 返回仍兼容，只有空错误列表才可把本次返回列表视为合格。需要跨审核失败保留合格子集的工具必须使用结构化返回。

`retryable=False` 仅用于明确不可操作问题，例如需要用户补充认证、永久配置错误、平台限制或无法恢复的文件损坏。临时超时、普通 HTTP429、暂时没有合格候选应返回 True。普通429会退避重试；明确 `insufficient_quota`、余额不足、Token Plan用量上限等才暂停供应商调用。不得从普通限速推断付费订阅已耗尽。

## 状态与持久化

每个栏目在 `job_states[job_key]` 保留 posts、post_ids、reviewed_posts、reviewed_post_ids、approved_versions、context、last_failure、审核完整状态和退避进度。顶层当前栏目字段继续保留，兼容原工具和审计读取。`completed_job_indices` 明确记录已真正完成的栏目，不能把“调度游标已走过”当作完成。

review 无论返回错误还是在补选后抛异常，均合并修改后的 posts 并保存 IDs。审核失败不清空产物。recover 记录进展和等待时间后轮转栏目，不再调用 generate 重造已有批次。上传失败仍复用同一审核稿件；平台风险、登录、验证或结果不确定时保留写入暂停状态，其他栏目可以继续完成本地生成。

生产环境 PostgreSQL LangGraph checkpoint 是调度权威；CLI 的逐项 PostgreSQL ledger 覆盖节点执行到一半、尚未写入 graph checkpoint 的窗口。JSON 仅为审计投影；即使 JSON 的 jobs/posts 被修改、文件丢失或损坏，恢复仍从原线程 PostgreSQL 状态读取。数据库不可用时不得降级为 JSON。历史 JSON 模式仅保留原有本地兼容与隔离测试用途。

已有本地稿件恢复失败必须报告 `RETAINED_POST_UNAVAILABLE`，不能悄悄重新生成一份。完成的 PostgreSQL 线程再次恢复直接返回原结果，不重做栏目。被明确阻断的栏目在显式恢复后重开，但保持已完成栏目和所有产物。

## 调度与时限

取消总墙钟时间预算。旧 `max_elapsed_s` 参数保留用于调用兼容，归一化为0；不存在120分钟共同预算。旧 `max_attempts_per_job` 不再限制可恢复工作总次数。

每一轮执行 sync/generate/review/upload 或 recover，经过 next_job 后在 PostgreSQL 中保留下一节点并结束本轮 graph invoke。驱动循环对同一线程 `invoke(None)` 继续。每个 invoke 都有有限 recursion_limit，但整个任务不受其累计次数限制。JSON 兼容模式以返回状态开始下一轮，生产 PostgreSQL 不使用该路径。

补缺按照 round-robin 轮转，每个栏目状态独立。已完成栏目退出调度；暂时无进展的栏目延迟重试，先运行其他就绪栏目。默认等待2秒起，指数增长，单次最多60秒。增加合格数量或成功上传才算有效进展，仅不断增加不合格候选不清零停滞计数。

默认 `no_progress_limit=0`，不因第2轮或第6轮仍未完成就退出。该参数只允许显式启用有限轮次，例如隔离测试；不作为生产默认兜底。持续无进展会持续等待并输出原因，不会高速空转或伪造完成。明确 `retryable=False`、认证/额度不可用、知识库不可用、无法恢复本地产物或平台写入限制可以保持可恢复 blocked/partial 状态。

取消的是任务总时限，不是单次网络请求时限：模型、采集、数据库连接/查询和浏览器工具各自保留请求超时。core 不使用无法安全取消的线程超时包装，避免函数已经被报超时后仍在后台生成或上传。

完整事件写入 run 目录的追加事件日志；检查点仅保留最近200条事件和错误，避免长期等待使每次 PostgreSQL 存储越来越重。

## 完成判定与质量

必须有实际存在的合格稿件，数量达到目标、审核无阻断问题、上传成功或明确配置为仅本地生成，才标记栏目完成。未知批准ID、空稿件列表、平台结果不确定均不得当作成功。合格版本变化必须重新审核；结构化审核结果可撤销旧批准。

平台写入仍串行，未改变 batch upload 的单浏览器契约。core 不自动解除平台写入暂停；仅在 adapter 明确核实后通过 `platform_write_ready=True` 恢复写入。不得用重试绕过登录验证、平台限制或未确认的发布结果。

## 验证计划与已验证结果

使用 `E:\AI\codex\redbook_agent\.venv\Scripts\python.exe`；所有测试临时目录指定到 `E:\AI\codex\redbook_runtime\data\runs\tests\<独占名称>`。不安装依赖，不调用真实模型、生图或平台上传。

覆盖以下行为：

- 8/10式部分成功保留，review 补选的 posts 写回，generate 不重跑。
- 运行跨越旧墙钟预算仍完成全部栏目。
- 70轮补缺越过旧最大重试/步骤数，AI栏目在新闻未完成时先执行。
- 默认连续8轮无进展后仍能恢复并完成；显式停滞限制返回可恢复状态。
- 部分批准撤销、未知批准ID拒绝、明确额度不足暂停、普通429恢复。
- 上传失败沿用原稿；不确定写入阻止后续平台提交。
- 真实 PostgreSQL 断点关闭连接后重新连接；保留新增稿件与已批准ID。
- JSON投影篡改或缺失不改变 PostgreSQL 恢复结果。
- 完成栏目和完成任务再次恢复不重复生成/上传。

真实 PostgreSQL 集成测试使用 `REDBOOK_TEST_POSTGRES=1` 与本地 `KNOWLEDGE_DB_CREDENTIALS` 路径，不输出凭据。每个测试使用独立随机线程编号，最后只删除该测试线程数据。

本文件记录编排核心设计与针对验证；实际模型质量、真实上传表现和整机强制终止后的 CLI ledger 行为需由主任务的端到端验收验证，不能用模拟工具测试替代。

2026-10-01 验证结果：四个 `test_editorial_agent*` 测试文件合计 **37 passed，25.52秒**。其中 PostgreSQL 真实连接测试3项，分别验证部分审核后新连接恢复、阻断栏目恢复且已完成栏目不重复、JSON审计投影缺失时从数据库恢复；对应内存检查点测试也通过。仅有已安装 LangGraph 依赖的一条默认序列化配置弃用预告，无测试失败。

最终测试临时目录：`E:\AI\codex\redbook_runtime\data\runs\tests\core-retention-20261001-final`。真实数据库测试线程均按各自随机编号清理，不修改生产任务、稿件或上传记录。
