# MiniMax 生图传输与安全错误诊断

## 状态与范围

2026-10-02：代码与设计已落盘。用户最新指令要求停止测试、网络探测及内容任务；
此后仅回收此前测试会话输出并补齐本文档，不再启动任何验证任务。
未重启、停止或reload生产worker，未修改生产环境、全局代理、提示词、其他提供商或审核门槛。

本轮文件：

- `src/images/minimax_images.py`：提交与下载改用已有httpx，安全错误摘要。
- `src/images/auto_image.py`：仅MiniMax分支记录错误类别与原因，安全显示放弃原因。
- `tests/test_minimax_image_transport.py`：此前新增的离线回归用例，保留但不继续运行。
- 本文档。

## 证据与目标

此前只读探测捕获到urllib的实际阻塞堆栈：
`proxy_open -> proxy_bypass_registry -> socket.getfqdn`。
使用系统代理与certifi的请求约16.125秒后返回401；5秒socket超时不能限制连接前的该解析步骤。
同时httpx不使用显式代理和使用显式代理的两轮探测均返回401。
401只证明TLS和HTTP可达，不证明生成接口、账号额度或订阅调用成功。

本次目标是移除MiniMax提交及下载中的这一额外阻塞路径，并保留可诊断的安全错误。
此前的间歇TLS EOF、urllib Errno2未定位到唯一根因；本次修改不宣称已修复所有网络故障。
生产任务曾在未更改代理或SSL时自行恢复，因此不能认定为固定代理配置错误。

## 修改前影响分析

使用GitNexus impact后再核实文本调用。唯一索引为旧项目auto_redbook，索引日期2026-09-24，
不覆盖独立redbook_tools。`_request_json`、`_download` 返回UNKNOWN，不视为无影响；
共享 `fetch_and_download_related_images` 返回CRITICAL（旧图162符号、53流程）。
这不是独立项目的精确范围。当前文件核实的链路为：
`generate_minimax_image -> _request_json/_download`，上游为auto_image及create_post。
因此仅修改MiniMax传输与错误分支，保持原有函数入口和调用方兼容，不改CLI或工作流。

## 传输设计

1. 提交使用短生命周期httpx.Client发送JSON POST；下载使用独立Client发送GET。
   下载客户端不携带订阅Authorization，避免把生成凭证发往图片CDN。
2. `verify=True`、`trust_env=True`，不固定代理地址、不切换用户全局设置、不关闭证书校验。
   HTTPX继续读取系统代理和HTTP(S)_PROXY等环境设置；显式NO_PROXY沿用其原生规则。
3. HTTPX读取Windows系统代理地址，但不会调用urllib的proxy_bypass_registry/getfqdn路径。
   HTTPX原生NO_PROXY与Windows注册表ProxyOverride的全部规则不保证逐项等价；
   本轮未增加自定义代理规则翻译，特殊注册表例外仍是兼容性风险。
4. 保留调用方传入的提交/下载超时，默认仍为180/60秒；这是网络各阶段超时，不承诺总墙钟上限。
   保持重定向支持、现有请求参数、单次图片数量、模型选择及重试次数。
5. 下载完整响应后才检查最小16字节并写文件；过短、HTTP失败或网络异常不覆盖原文件。
   此检查不是图片像素尺寸或视觉真实性审核；原有后续图审继续负责其职责。
6. 订阅only及禁止付费余额/paygo的配置门禁保留，缺Key或违规付费配置在传输前失败。
   没有新增额度同步或实机模型请求。

## 错误契约与安全

保留 `MiniMaxImageAPIError` 的url/status/code/message属性和原有错误代码：
HTTP错误为http_error，连接及超时错误为network_error，业务码保留，JSON与结构错误分别为
invalid_json、invalid_response。下载错误也统一使用该类型，便于上游获取阶段和HTTP状态。

- 网络诊断保留submit/download阶段、异常类及最多4层原因，包含ConnectError、ReadTimeout、
  ProxyError、SSLEOFError等；循环异常链被限制。
- 在截短之前移除已知请求Key、环境中的凭证值、Bearer/API-key/token等字段值，
  清除URL用户信息、查询串和片段。摘要压缩为单行并限制600字符。
- 请求Key通过参数提供给脱敏器，不额外读取本地secret文件。
- MiniMax的ImageGenerationAbandoned保留attempts和安全errors，并把末次原因放入消息，
  上层只记录str(exc)时也能看到失败类别。其他提供商的该异常消息和行为保持不变。
- 对外异常堆栈不再串接可能含原始凭证的底层异常；原因以脱敏文字保留。
  不把实际Key、原始HTTP请求对象或完整响应体作为新的日志字段。

脱敏不是对任意供应商自由文本的形式化保证；未知格式凭证仍有残余风险。
本轮未追溯清洗既有历史日志或元数据。

## 首图身份暂缓

本轮没有新增first_image字段，也没有改写旧稿的prompt_version/hash。
下载成功不等于“该post的第一张图片”：旧稿重画也经过相同传输入口。
要防止重画覆盖首图版本，需要由拥有post首次保存/重画流程的代码持久化不可变首图身份；
仅在auto_image或下载函数中根据当前图片补写可能误标旧历史。
按用户允许的最小范围优先交付网络与诊断，首图跨重画稳定身份仍待独立实现。

## 此前运行的测试记录

以下全部发生在用户要求停止新增测试之前启动的会话中，不代表停止指令后的再次验证。

| 阶段 | 结果 |
| --- | --- |
| 初始TDD，旧urllib与旧错误输出 | 21 failed、4 passed，0.87秒 |
| 初步实现 | 25 passed，0.29秒 |
| 补充异常堆栈泄密测试 | 1 failed、24 passed，0.49秒 |
| 修复堆栈并补代理/TLS/缺Key覆盖 | 30 passed，0.37秒 |
| 完整离线集，此前会话最终输出 | 26 failed、345 passed、8 skipped、2 warnings，41.35秒 |

针对性用例使用真实httpx客户端与本地MockTransport；真实socket及urllib旧路径被阻断。
另有客户端路由测试替换最底层Transport，验证系统代理、显式环境代理、NO_PROXY的实际选择，
而不是仅断言配置字面量。无效CA路径测试验证不会自动关闭TLS校验或继续联网。

完整集使用独立子进程，关闭PostgreSQL实机测试开关并限制网络为测试用本地高端口。
它不是全绿结果，不能报告为“完整验证通过”。收到停止指令时回收的会话已自行结束，退出码1；
没有为此终止生产worker。

24个失败位于 `tests/test_editorial_agent_completed_revalidation.py`，涉及以下用例族及参数变体：

- test_completed_valid_jobs_are_loaded_once_without_regeneration_or_upload（2）
- test_only_invalid_completed_job_returns_to_review_with_original_posts（2）
- test_revalidation_does_not_lower_quality_when_repair_still_fails（2）
- test_completed_post_load_failure_cannot_remain_completed（8）
- test_revalidation_platform_risk_still_blocks（2）
- test_revalidation_callback_failure_fails_closed（4）
- test_postgres_pending_node_preserved_while_completed_job_is_reopened（3）
- test_tools_accept_optional_completed_revalidation（1）

上述失败呈现未执行预期重新审核/回调等断言差异。本子任务未修改该模块，尚未归因，
不以“既有问题”或“网络隔离导致”直接定性；交由主代理处理，不越界修复。

另外2个失败为 `tests/test_web_gui.py` 中：

- test_agent_conversation_http_routes_use_existing_job_service
- test_http_auth_origin_static_secret_protection

这2个堆栈明确停在本次测试隔离器offline_connect，被允许范围之外的连接拦截；
不能据此宣称生产Web路由已回归。按停止要求不放宽隔离并重跑。
2个warning分别为既有LangGraph序列化默认值提示及Pillow getdata弃用提示。

临时产物仅位于：
`E:/AI/codex/redbook_runtime/data/runs/tests/minimax-transport-20261002-subagent/`。

## 尚未验证与交接

- 未进行真实MiniMax生成/下载、延迟对比、TLS间歇故障恢复验证。
- 未证明全工作区回归通过；上述26个失败未在本子任务中修复或重测。
- 稳定首图版本/hash身份暂未实现，旧历史未篡改。
- worker未reload，落盘改动不保证已经被运行中进程采用；由主控决定后续部署时机。
- 最新停止指令之后没有新增测试、网络探测、生成或上传操作。
