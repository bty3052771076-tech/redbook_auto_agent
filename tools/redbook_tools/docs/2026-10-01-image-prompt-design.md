# 每日新闻生图提示词与逐篇保存接口

日期：2026-10-01。实现目录：`E:/AI/codex/redbook_tools`。

## 目标与验收

本次负责提示词设计、代码和离线单元测试，以及主线程的逐篇保存回调。
实际首图通过率目标为 **大于50%**，不调整现有 `ok == true && score >= 70` 审核门槛。
该目标必须通过后续真实生成的第一张图片审核记录验证；单元测试不能证明生图成功率。

需求采用 prompt-optimizer 的条件/行为/可验证结果结构，并参考 imagegen 的
scene-subject-constraints、保留不变量和单次局部修复原则；本次不调用 OpenAI 生图服务。

1. 当事件描述超过40字符时，保留完整事实，不切断句子、名称或限定条件。
2. 当提示词超过旧380字符阈值时，完整传输；超过实际提供商限额时明确报错，不静默截断。
3. 当修图时，继续使用已接受稿件的中文标题和事件，不让原始英文标题覆盖正确中文事件。
4. 当模型建议加文字、品牌标志或改变事实时，只提取局部缺陷类别，不执行其新增事实处方。
5. 首图满足门槛时复用原图；失败时最多增加一张，比较两张，不循环重画。
6. 最终接受的稿件完成本地保存后，在协调线程同步通知调用方，以便立即持久化其身份。

## 原因与设计

旧链路的问题包括：`image_event` 指令要求20-40字、归一化直接切40字符、修图时
`picked.title` 优先覆盖中文标题、按关键词把监管映射成法庭，以及把完整VLM重绘提示
再次嵌入主题后裁剪至380字符。事实和后置约束可能被删，新闻还可能被画成文字密集海报。

新版提示结构为：

```text
竖版3:4，克制清晰的编辑示意插画，不冒充现场照片。
事件：<完整中文事件，通常一至两句>
事实依据：<已接受正文的内容字段；无评价、来源、话题等信息>
构图：一个连续场景、一个主体动作；人物、物体状态和远近地点符合事实。
表达边界：声明、联名信和计划不画成已实施结果，不靠文件或拼贴讲述整篇报道。
画面约束：无文字和标识，物件表面留白，不虚构身份、伤亡、武器或结果。
局部修正：<仅修图时添加，最多三个完整的修复要求>
```

写稿模型的 `image_event` 指令改为一至两句完整中文，通常60-120字是写作建议，
不做字符截断。准确区分远海/近岸、受损/完好、计划/完成。保留现有JSON字段和返回结构。

事实来源优先顺序：已接受的中文 `image_event`（与最终标题相关）→最终标题→原始来源标题。
完整正文中的事实独立进入 `事实依据`，防止短标题丢失状态和地理约束。
不使用来源页面的其他推荐文章作为配图依据；不再用机械英文关键词摘要替换图片事件。
URL、话题和旧提示包装可清理，事实句本身不截断，也不全局删除“记者”等有实际语义的词。

修复反馈不是新的事实来源，映射为以下类别的短指令：

- 字符/标识噪声：去掉文字和标识，用留白表面、实体动作表达。
- 场景/物体状态：依据原有事实纠正地点、远近关系和物体状态。
- 主体/动作：依据原有事实纠正角色与行为。
- 拼贴/复杂构图：一个连续场景，减少无关陪衬。

不原样拼接VLM输出，不追加通用软件界面英文许可，不重新引入具体品牌、旗帜或人物肖像。
未知类别使用保守的事实校正要求。最多三个完整指令保证精简，不按100/120字符切断。

### 回归实例

| 事件 | 必须保留 | 禁止机械补出的内容 |
| --- | --- | --- |
| 游艇远海撞鲸获救 | 距岸500海里、船体损毁/沉没、获救状态 | 近岸度假村、完好游艇 |
| 医护人员联名反对参与执行死刑 | 医务人员及联名表达行为 | 假报纸、庭审或行刑现场 |
| 监管部门要求平台说明数据处理 | 行政调查的具体行为 | 直接变成法院裁决 |
| 公司暂缓IPO | 暂缓状态、公司主营事实 | 凭空增加挂牌仪式或下跌行情 |

## MiniMax实际调用链

```text
写稿JSON.image_event
  -> _normalize_daily_news_image_event
  -> post.platform.news.image_event
  -> _fetch_daily_news_related_images(count=1)
  -> fetch_and_download_related_images
  -> _build_aliyun_image_prompt（名称兼容，实际为多供应商共用）
  -> generate_minimax_image
  -> payload.prompt / payload.n=1 / payload.prompt_optimizer=False
```

修图由 `regenerate_daily_news_post_image` 构建完整提示，以 `prompt_override` 直接传入，
`prompt_hint` 仍只保存纯事件。生成器不会再次包装override。图片metadata保留实际 `prompt`
和 `prompt_version=2026-10-01-single-scene-v1`，便于主流程统计。

MiniMax本地适配器现有上限为1500字符，普通提示和override统一使用该默认上限；
用户显式设置的 `IMAGE_PROMPT_OVERRIDE_MAX_CHARS` 仍有效。超限报错应交由主流程
重新概括事件，不能删掉末尾事实来勉强发送。其他供应商保留其既有校验方式。

长度控制先进行不改变事实的去重：相同完整事实句只保留一次，数字、否定或状态不同的句子
分别保留；若正文已包含事件概括，仅保留一份事实表示。若完整组合仍超过1500字符，优先
去掉从同一正文派生的重复 `image_event` 概括，完整保留已接受的正文事实和全部画面约束，
不调用额外LLM、不按字符截句。包含900-1000字符不同事实的正文加局部修复要求有专门测试，
去重后提示保持在1500内。若事实本身已超出提供商容量，仍明确拒绝，不能丢事实或无限重试。

每日新闻AI请求显式 `count=1`，避免环境中 `AUTO_IMAGE_COUNT` 导致一次生成多张。
两张择优和跨检查点复用仍由现有CLI审核器负责；本子任务不修改CLI、审核门槛或评分器。
离线测试直接调用真实的有界审核函数，验证首图通过不重画、次图变差恢复首图、最多一次修图。
跨进程复用和ledger由主代理的CLI/graph集成测试负责。

## 逐篇保存回调接口

以下两个函数增加可选关键字参数，默认保持旧调用兼容：

```python
post_saved_callback: Callable[[Post], None] | None = None
```

- `create_daily_news_posts` 将参数仅转交 `_run_parallel_daily_news_candidates`。
- worker准备候选，主协调线程完成查重、配额等最终接收检查。
- 构造最终assets和pick_index/pick_total，执行 `save_post(post)`。
- 加入已接受列表，立即调用 `post_saved_callback(post)`，然后保存revision。
- 不向未通过接收检查、被去重或未使用的worker候选发送回调。
- callback/revision失败抛出 `PartialDailyNewsError`，`.posts` 包含刚落盘的稿件和此前接受的稿件。
- 原有批次数量不足路径仍通过 `PartialDailyNewsError.posts` 保留已生成稿。

回调应按run/job/post_id在PostgreSQL中幂等写入；本地文件保存和ledger事务不能天然原子化。
进程若恰好在两者之间被杀死，主代理仍需在恢复时对账本地post记录。
测试验证正常/速度模式下均在调用协调线程运行，保存早于回调，回调早于revision和批次返回。
本次不引入Redis或额外后台服务。

## 风险与图分析范围

GitNexus只注册旧 `auto_redbook`，路径 `E:/AI/codex/redbook_workflow`，索引时间2026-09-24，
commit `3f6f1a8`。对提示构造、事件归一化、修图、生成器与候选协调函数执行了upstream impact，
旧索引返回CRITICAL及跨工作流影响。该索引没有覆盖独立项目，不能视为当前项目的精确影响证明。
因此逐个文本核对独立项目的调用点，并针对直接调用链增加测试；保持已有接口，只追加可选参数。

不进行新依赖安装、API调用、额度同步、上传或secrets修改。共享提示函数仍可能影响材料发帖
及其他未提供override的生图调用，实际视觉效果需要主代理继续实测。

## 验证计划与实网统计

离线测试覆盖：完整中英文事件、中文修图锚点、推荐文章污染防护、JSON/普通正文事实提取、
监管/财报误配、旧提示包装、修复注入隔离、MiniMax原样请求、单图数量、长度边界、
两张择优门槛、主线程保存时序、部分失败保留和旧调用兼容。

测试目录固定到E盘独占位置：
`E:/AI/codex/redbook_runtime/data/runs/tests/image-prompt-20261001-agent`。

主代理实网验收：对本轮连续生成的新闻记录每个post的首次VLM结果、图片ID和提示版本，
用“首图 `ok=true && score>=70` 的不同post数 / 首图完成审核的不同post数”计算通过率。
API/审核调用失败另列，不能静默丢掉样本；两张中的最佳成绩不能算作首图通过。
至少检查10个不同事件；10篇样本需要至少6篇首次通过。单轮6/10只能证明该轮，不能保证长期成功率。
同时人工抽查远近地点、状态、乱码和事件一致性，不降低真实图片审核要求。

本子任务没有执行实网生图，因此不会宣称已达到实际通过率目标。

## 本次离线验证记录

2026-10-01在独立工具项目执行以下精准回归，结果 **40 passed，1 warning，2.01秒**。
警告为既有LangGraph序列化 `allowed_objects` 默认值未来变化提示，不是测试失败。

```powershell
& 'E:/AI/codex/redbook_agent/.venv/Scripts/python.exe' -m pytest `
  tests/test_news_image_prompt.py `
  tests/test_news_image_prompt_integrity.py `
  tests/test_agent_news_image_anchor.py `
  tests/test_daily_news_post_saved_callback.py `
  tests/test_minimax_vision_request.py `
  tests/test_news_content_completeness.py -q `
  --basetemp 'E:/AI/codex/redbook_runtime/data/runs/tests/image-prompt-20261001-agent/final'
```

新增链路与回调测试阻止真实socket连接；MiniMax请求层替换为本地响应，未使用真实账号或生成额度。
900-1000字符独立事实测试验证全部38条不同句子仍在提示内，旧380阈值不生效且最终长度不超过1500。
1200-1500字符override测试验证原文直接到达提供商适配器，超过1500在调用前失败。
需要主代理继续验证的内容：实际首图通过率、PostgreSQL ledger集成、断点续跑及上传结果。

## 实测后的场景传递修复

### 证据与范围

2026-10-01实跑 `52861aab28dc438eac8c5a93ef72332f` 中，
`3c130827312c47e2b0151812acdcc608` 的保存场景等于新闻标题。
原始writer响应没有保存，旧revision在归一化之后才落盘，不能据此断言模型漏字段。
离线复现则确定：完整的“内塔尼亚胡面对记者举手回应提问”场景，经JSON解析仍完整，
但场景与短标题的词面匹配不足，旧归一化函数直接用标题覆盖了场景。
`7da8be76fcf74456bff6b43c4bb5a20b` 保留了完整西岸场景，排除字段被解析器普遍丢弃的判断。

上一版生图模板为 `2026-10-01-single-scene-v2`：唯一画面场景加核对标题，不再重复整篇正文。
前文“全部正文事实进入生图提示”的记录描述v1历史测试，不再是v2要求。
完整正文与评价仍保存在Post并进入原有VLM审核，不能通过减少事实比对来提高通过率。

### 最小实现

- `generate_draft` 对每日新闻/每日我去的系统和用户契约统一要求同次返回 `image_event`，
  内容为具体单场景，不能抄标题或复述全文。普通生活稿保持可选字段行为。
- 归一化保留中文保护和无关场景拒绝。除标题支持外，接受已通过文案流程的正文事实支持；
  不读取原网页的推荐文章、评价或读者偏好来支持场景。
- 人物表态类标题可通过明确主体加表达动作保留合理场景，主体同时需要出现在正文事实中。
  同名人物的无关活动不能仅凭姓名通过。此规则不提供新闻事件到配景的硬编码映射。
- 缺场景、语言不一致或不相关仍回退到兼容标题，不增加模型调用或自动重试轮数。
  回退并不等于画面质量合格，仍需现有图审；审核阈值与最多两张择优逻辑未变。

`platform.news.image_event_audit` 与revision中的同名字段记录：

| 字段 | 含义 |
| --- | --- |
| `writer_value` | `generate_draft` 返回的初稿场景，不冒充提供商原始响应 |
| `rewrite_value` | 若既有文案重写发生，记录该次返回的场景 |
| `input` / `cleaned` | 归一化输入及清理后完整值，无40/380字符裁断 |
| `normalized` | 最终输出场景或兼容标题 |
| `reason` | `headline_supported` / `body_supported` / `entity_supported` / `missing_scene` / `language_mismatch` / `unrelated_scene` |
| `accepted` | 是否保留场景，不是VLM审核结果 |
| `supported_entity` | 人物表态分支核对的主体，其他分支为空 |
| `version` | `scene-anchor-v1`，与生图模板版本分开 |

初稿与质量重写分别记录；单篇及批量生成路径都保存归一化审计。旧稿无需迁移，不因新增审计重生。
不改变正文处理、并发、CLI、图审阈值，也不启动、停止或重启实跑进程。

### TDD与验证

新增 `tests/test_news_scene_contract.py`，先执行得到 **9 failed、3 passed**，
失败覆盖已复现的场景丢失、缺审计接口、契约冲突和同人物无关活动误放行。
实现后执行该文件与原有6个生图/回调/正文回归文件，结果 **53 passed、1 warning，2.17秒**。
警告仍是既有LangGraph兼容性提示。测试阻断网络，模型回复用固定本地JSON替代。
首次联合执行有6个临时目录父级不存在的setup错误，创建E盘独占父目录后全量重跑通过。

```powershell
& 'E:/AI/codex/redbook_agent/.venv/Scripts/python.exe' -m pytest `
  tests/test_news_scene_contract.py tests/test_news_image_prompt.py `
  tests/test_news_image_prompt_integrity.py tests/test_agent_news_image_anchor.py `
  tests/test_daily_news_post_saved_callback.py tests/test_minimax_vision_request.py `
  tests/test_news_content_completeness.py -q `
  --basetemp 'E:/AI/codex/redbook_runtime/data/runs/tests/scene-contract-20261001-subagent/verified'
```

本轮再次执行 `generate_draft`、场景归一化和候选流程的GitNexus upstream impact。
旧索引返回CRITICAL，不能视为独立项目的精确图；独立目录未建Git仓库，`git diff --check`不可用。
实际调用链已以当前文件核实，不提交Git，也不把未知图范围报告为安全。

剩余风险：词面与主体动作检查不是完整语义事实验证，无法取代VLM；已有实图仍有无关武器、
人物状态错误和乱码。确定性字段修复不代表已达到首图通过率目标，不再为此叠加多轮提示词。

## v3候选：场景置首与精简正向模板

### 依据与改动边界

实跑反馈中出现了场景未要求的军械、红色飞溅与衣服乱码。主代理反馈的阶段统计为
v1旧4篇加v2新9篇合计首图通过2/13；这是当时的反馈快照，不是本次重新读取图审后的最终统计。
“通用负面词列表可能引入无关视觉概念”仅是待验证假设，并非已证实根因。
本次同时缩短模板、移除重复标题并调整构图说明，后续结果只能评价整个v3输入链，
不能把通过率变化单独归因于负面词。场景传递修复、候选事件差异也可能影响结果。

本次写入仅限 `src/images/auto_image.py` 的模板函数与版本常量、原有两个生图测试文件和本文档。
版本为 `2026-10-01-single-scene-v3`。未改writer、正文处理、场景归一化、CLI、并发及图审。

修改前已执行GitNexus upstream impact：旧 `auto_redbook` 索引返回 **CRITICAL**，
报告162个受影响符号、94个直接依赖、53条流程、13个模块，涉及生成、修图、审核及Web链路。
索引属于旧工作区，不覆盖独立 `redbook_tools`，不能把这些数字当作独立项目精确范围。
随后文本核实当前builder及版本常量的实际调用：首图通过生图适配器构造提示，
修图通过 `create_post` 传入完整override，元数据沿现有路径记录版本。
共享builder也可能影响其他调用它的提供商，本次离线验证不能替代各提供商真实画质验证。

### 实际模板

```text
场景：{image_event}
竖版3:4，彩色平面编辑示意插画。只呈现上述一个地点、同一时刻的主体动作和状态，背景简洁。
角色、地点与状态以已证实事实为准；声明或计划仅表现表达或讨论，不画成已实施结果。
人物造型概括化、五官简化。衣物及所有物体表面纯色留白，画面无文字或标识。
使用干净平涂色块和清晰轮廓。仅画场景已列出的必要人物、物件与环境。
```

- 有场景时不再追加新闻标题、全文或评价；缺场景的历史稿保留清理后的标题回退。
- 不增加题材到物件的映射。场景本身的真实人物、地点、动作、否定状态完整保留，
  不因为模板删掉某类通用禁词就删除事实中的同一词语。
- 可选局部修正仍以 `局部修正：{repair_hint}` 追加一次，沿用既有修正提示构造，不增加调用轮数。
- 不含场景与修正时，固定开销实测161字符；60-120字符场景对应总长221-281字符。
  这是测量结果，不是对场景的长度裁断。已有40/380事实硬截断不会恢复。
- MiniMax的1500字符提供商上限与override透传不变；正常模板缩短不意味着取消过长输入校验。
- 完整新闻事实与评价继续保存在Post并送入既有视觉审核。门槛、真实性比对和角色状态检查不变；
  首图合格即复用，否则最多再生一张并按原逻辑择优，不循环生图。

### 原任务后续观测口径

不额外执行12张A/B图，不为对照重生历史稿。只在原run
`52861aab28dc438eac8c5a93ef72332f` 后续真实补缺中观察。

1. 总表保留所有版本的全部候选，包括最终未选中的稿件及v1/v2失败，不能只统计最终上传稿。
2. 各版本分别记录：生成首图候选数、已返回首次图审数、首图通过数、待审数、调用/审核错误数。
   首图通过率为“首次图审通过数 / 已返回首次图审数”；待审及错误另列，不伪装成通过或悄悄排除。
   同时报累计首图通过数/全部首图候选数，明确其中仍有未完成项。
3. 通过必须满足原图审的ok与分数门槛；使用首次图审记录，不用最终择优分数或第二张结果替代。
4. 版本以首张图片实际请求与元数据为准，不用修图后覆盖的当前图片版本给首图重新分组。
   无法核对历史首图版本时列入unknown，保留在总统计；v3修复旧v1/v2稿不计为v3首图。
5. 每次报告v3的通过数、分母和样本量；小样本不宣称达到稳定的>50%目标。
   本次未调用模型，因此尚无本轮v3实图通过率。
6. 已加载模块的运行进程不会因文件落盘自动切换版本；由主代理按原任务节奏处理，
   本子任务不启动、停止或重启进程。观察时先核对实际请求版本，不能按落盘时间推断生效。

### 离线验证

TDD红灯：原v2模板运行更新后的两个生图测试文件，**10 failed、16 passed，2.10秒**。
失败覆盖场景置首、版本、精简约束与兼容回退，未删除任何审核门槛测试。
修改模板后执行7个相关文件，**58 passed、1 warning，2.21秒**。
警告为既有LangGraph序列化配置未来变化提示。

```powershell
& 'E:/AI/codex/redbook_agent/.venv/Scripts/python.exe' -m pytest `
  tests/test_news_scene_contract.py tests/test_news_image_prompt.py `
  tests/test_news_image_prompt_integrity.py tests/test_agent_news_image_anchor.py `
  tests/test_daily_news_post_saved_callback.py tests/test_minimax_vision_request.py `
  tests/test_news_content_completeness.py -q `
  --basetemp 'E:/AI/codex/redbook_runtime/data/runs/tests/image-prompt-v3-20261001-subagent/green'
```

测试覆盖完整场景与否定状态保留、无固定题材物件、全文继续图审、局部修正透传、
MiniMax单张请求与版本标记、1200-1500字符override、超限不截断、好图复用与最多一次修图。
网络连接在生图完整性测试中被阻断，提供商请求使用本地替身；未实网生成或上传。
