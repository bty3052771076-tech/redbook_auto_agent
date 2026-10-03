# AI 摘要库版本误判与标题语义修复

日期：2026-10-01。范围：`src/ai_digest/rank.py`、`src/ai_digest/generate.py`、专项测试与本文档。未修改 CLI、agent core、数据库、运行检查点或平台草稿；未调用生成模型、执行上传或安装依赖。

## 证据与根因

原运行 `52861aab28dc438eac8c5a93ef72332f` 的 AI 稿 `e26ff2b590d24c97b33ab4002d5f4e44` 已保存平台草稿，其第 4 条标题为“Qwen2.5-VL GitHub Re开放权重模型发布”。主控已核对官方来源：

`https://github.com/huggingface/transformers/releases/tag/v5.18.0`

这是 Transformers 库的 Release5.18.0。正文新增模型接入，并在视觉编码器引用与 temporal RoPE 修复中提及 Qwen2.5-VL；该材料不能证明 Qwen 新模型首发。

测试夹具 `tests/fixtures/ai_digest_transformers_v5_18_0.json` 保留该已存条目的公开内容与原始摘录，不含账户凭证。复现时将标题恢复为 `Release5.18.0`、摘要取摘录首段；这不是声称恢复了未保存的模型请求或响应。

已确认的错误链：

1. 分类器将 URL 中的 `tag v5.18` 识别为通用“模型名+版本”，结合全文 release/open-weight 等词，误排到模型首发。
2. 回退函数从长正文任意位置匹配 Qwen2.5-VL，并把通用元数据 `GitHub Release` 拼为产品名。
3. `_title_with_action` 按字符数硬切主体，产生 `GitHub Re`。上游 `_clean_subject` 也有 48 字符截断。
4. 初次 grounding 失败会触发回退，但回退后仅有中文、长度等门禁，没有重新校验库更新与模型发布的事件关系。

日志里的 `retry=2 ... expected exactly 5 items, got 0` 只能证明第一次响应解析未得到期望条目。随后产物记录 `generation_mode=llm`，不能声称两次调用均失败或直接推断 token 不足；本次不扩建原始响应日志框架。

## 影响分析

编辑前对分类、标题回退、grounding、恢复及内容门禁函数进行了 GitNexus upstream impact 查询。仅有旧 `auto_redbook` 索引，名字查询返回 CRITICAL，精确函数 UID 查询返回 UNKNOWN。已告知风险；旧索引不能证明独立 `redbook_tools` 没有其他调用者。

随后核对实际目录文本调用点：改动集中在 AI 资讯排序、主体提取、回退恢复与最终内容验证。`_clean_subject` 的实际调用点均在本文件。通过既有 AI 测试覆盖正常首发、来源多样性、查重与时间行为，而非把 UNKNOWN 当作安全许可。未修改共享 CLI/core；未提交 Git。

## 实现

### 分类与证据身份

- 新增 `ai_library_release_identity`，按 GitHub 主机、仓库路径和 `/releases/tag/` 识别已知库，并返回库名与完整 tag。
- 当前明确识别 Transformers、Diffusers、PEFT、PyTorch、vLLM、SGLang、llama.cpp 的指定仓库。未知仓库不会因“GitHub Release”元数据就被假定为模型或已验证的库。
- 已知库发行页优先归为 `technical_tool`，不受正文背景模型名称影响。
- 通用版本识别排除 tag/tags、version/versions、release/releases。真实 `Qwen3 released with open weights` 等模型事件仍保留模型首发分类。
- 不修改来源等级或可信度，不把 aggregator 自动提升为 official，不改日期、来源上限和历史去重规则。

### 标题与回退

- 库发行页回退标题基于仓库与 tag，例如“Transformers v5.18.0版本发布”。不再从正文偶然出现的模型名称推导主体。
- `GitHub Release` 等通用产品标签不再拼接到模型专名；去掉模型名后误吸入的 released 等动作词。
- `_title_with_action` 保留旧 `limit` 参数以兼容调用，但不再切割主体。`_clean_subject` 也不再按 48 字符切割；排版应处理完整标题。
- 新增库事件关系门禁，检查最终标题主体及标题/摘要中的发布断言。库新增支持与模型首发、开放权重不得互换。
- 支持与修复子句允许保留；“发布Qwen...并新增API支持”不能因为后半句含“支持”而绕过检查。
- grounding 使用匹配来源的 URL 校验关系；所有恢复、替补和中文回退结束后再检查一次。最终 `validate_ai_digest_concrete_content` 也拒绝已存的错误条目。
- 无法正确修复时返回具体“事件关系错误”，不靠空标题、删掉条目或伪造新模型发布让结果通过。

边界：这是针对库发行页身份和发布断言的确定性防护，不是通用自然语言事实判定器。新增库需要可靠仓库身份；人物观点、任意能力指标、全文真实性仍需现有来源和质量审核。历史已完成稿件不会因为部署新代码而自动重审。

## TDD 与验证

1. 修改生产代码前，用真实保存夹具建立失败测试：11 失败、3 通过。无关条目的测试补齐既有默认标签，避免把原有标签规范化误判为本次 bug。
2. 首轮实现后 14 项全部通过。
3. 新增合法“开源模型支持”、库修复与错误发布混合子句边界：先得到 4 失败、18 通过，再完成关系修复。
4. 新增上游长专名截断用例：先失败，再去除 `_clean_subject` 的硬切片。
5. 最终联合回归：**161 passed，2 warnings，1.78 秒**。包括 23 项新测试及旧工作区的 138 项 AI 生成、排序和 release-first 测试。两条 warning 为预导入 anyio/langsmith 导致的 pytest assertion rewrite 警告，并非测试失败。

解释器：`E:\AI\codex\redbook_agent\.venv\Scripts\python.exe`。

临时目录：`E:\AI\codex\redbook_runtime\data\runs\tests\ai-release-integrity-final-20261001`。

回归运行先显式导入独立目录的 `src.ai_digest.generate/rank`，打印并核对模块路径，再加载旧工作区用例；以测试替身替换模型构造器，并禁止 socket 实际连接。不加载本地额度、不调用模型、不访问生产 PostgreSQL或平台。没有重复执行全项目测试。

仅重跑新增用例：

```powershell
Set-Location 'E:\AI\codex\redbook_tools'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'E:\AI\codex\redbook_agent\.venv\Scripts\python.exe' -B -m pytest tests/test_ai_digest_release_integrity.py -q --basetemp 'E:\AI\codex\redbook_runtime\data\runs\tests\ai-release-integrity-rerun'
```

## 原 run 的同草稿定向修复方案（未执行）

以下为主控/CLI 的最小后续协议建议，不是本次新增的可调用命令：

1. 用稳定的 `agent_run_id=52861aab28dc438eac8c5a93ef72332f`、原 AI job key 和 `post_id=e26ff2b590d24c97b33ab4002d5f4e44` 从 PostgreSQL 读取权威 artifact。续跑的 web attempt ID 不能代替稳定 run/job ID。锁定该稿修复，避免与 worker 同时编辑。
2. 保存原 artifact revision、内容指纹与平台草稿 ID 的审计记录。对已 done 稿调用新内容验证器，记录具体失败条目；只重新打开这个 AI 稿的 review/update 状态，不清空整个运行的 posts、不重新生成新闻或地图。
3. 保留合法条目及原始 URL、日期、置信度和证据。针对第 4 条按库版本更新修订标题；保留经核实的模型接入说明。若无法证明摘要事实或已超过当日时效窗口，应重新检索并替换该条，不能修改旧时间假装新发布。本文的离线标题修复不等于整篇已经重新合格。
4. 构造候选新 revision，重新执行来源、日期、事件去重、每源上限、摘要关系及图文完整性审核。只对本稿旧 revision 的自我匹配豁免查重，其他历史稿照常参与。根据正文修订重新渲染受影响图片，不复用包含错误标题的旧图。
5. 更新时保持同一平台草稿 ID，使用“编辑已有草稿”而非新建或强制重复上传。建议以 `(run_id, job_key, post_id, candidate_content_hash)` 为幂等操作键。旧上传 receipt 不能证明新内容已同步。无法唯一确认平台草稿时暂停并请求确认，不批量删除或猜测匹配。
6. 使用专用 profile 串行保存草稿，读回标题、正文和图片数量/对应内容，取得新 revision 的正向证据后，才更新 PostgreSQL artifact、receipt 和 AI job 完成状态。跨数据库与平台不能假装是原子事务：平台保存后进程中断时，先核对平台实际内容并补提交 receipt，不盲目再次新建。
7. 登录、额度、平台限制或写入结果不明确时保留原 revision 与候选 revision，记录可恢复原因；不把渲染成功或图审满分当作事实正确/远端修复完成。

## 文件进度与未完成部分

- `rank.py`：分类与 release 身份防护已完成并测试。
- `generate.py`：主体、完整标题、关系门禁与回退复核已完成并测试。
- `tests/test_ai_digest_release_integrity.py`：23 项已通过。
- `tests/fixtures/ai_digest_transformers_v5_18_0.json`：真实公开条目夹具，已用于复现。
- 本文档：证据、风险、测试结果和定向修复协议已完成。
- 生产稿 `e26...`：本次未改，仍待主控定向修复与远端验证。未更改共享根目录进度文件，以遵守并行代理的文件所有权边界。

## 补充：已存摘要可复用审核 API

新增入口位于 `src.ai_digest.generate`：

```python
AI_DIGEST_CONTENT_REVIEW_VERSION = "2026-10-01.release-integrity.v1"

def stored_ai_digest_review_issues(digest: object) -> list[str]:
    ...
```

输入是 `post.platform["ai_digest"]` 字典，或 `AIDigestBrief`；不是 Post 对象、完整平台字典、文件路径或 JSON 字符串。入口不做 I/O、不调用模型、不修复内容、不修改输入、不跳过异常行，也不信任旧审核/上传成功标记。

返回值：

- `[]`：通过本地内容门禁，不代表来源真实性、时效、历史查重、图文一致性和远端保存状态全部合格。
- 非空 `list[str]`：各异常条目的原始一基序号与具体原因。坏条目不会因前一行损坏而被跳过；整个摘要缺失/空列表也按失败返回。
- 实际坏标题返回“第4条：事件关系错误：库版本更新的标题必须以实际库为主体，不能冒充模型首发”。
- HTML 残留在 Pydantic 清洗前拒绝；缺少可解析 HTTP(S) 原始 URL 不能绕过基于发行页的关系核验。结构损坏返回错误列表，不把输入或全文回显到错误日志。

CLI 最小接线建议（本次未修改 CLI）：

```python
from src.ai_digest.generate import (
    AI_DIGEST_CONTENT_REVIEW_VERSION,
    stored_ai_digest_review_issues,
)

# 在 _agent_ai_digest_review_issues 内合并，不替代原有来源等审核。
issues.extend(stored_ai_digest_review_issues(digest))
```

主控使已完成 AI artifact 的旧审核版本失效后，仍须主动调用此入口。仅部署该函数不会令 core 重新检查 completed job。审核通过记录建议同时绑定 `AI_DIGEST_CONTENT_REVIEW_VERSION` 与内容指纹；任何内容变化都重新审核，不因版本相同而无条件复用通过结果。拒绝后按上文同草稿定向修订协议处理。

本轮遵守 TDD：先新增 13 个调用缺失入口的失败用例，再实现入口；之后补充畸形字段与真实模型发布正常通过的边界验证。累计专项测试 **42 项**；与 138 项既有 AI 回归联合运行，结果 **180 passed，2 warnings，4.32 秒**。warning 仍仅为预导入的 pytest assertion rewrite 提示。

实际反例验证同时覆盖：旧稿带 completed/100 分/上传成功标记仍拒绝；真实保存条目经现有回退流程产生正确的“Transformers v5.18.0版本发布”且保留合法摘要后，通过 stored 校验；真实 Qwen 模型发行页不误拒绝。所有测试无实网、模型调用或生产状态写入，临时目录为 `E:\AI\codex\redbook_runtime\data\runs\tests\ai-stored-digest-final-20261001`。
