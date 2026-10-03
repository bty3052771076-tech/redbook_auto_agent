# 首图身份与首次审核记录

## 范围与影响分析

- 独立工具仓库：`E:\AI\codex\redbook_tools`。
- GitNexus 仅有旧 `auto_redbook` 索引，不覆盖独立工具仓库。编辑前 impact 返回 CRITICAL（162 个受影响符号、94 个直接关联、53 条流程、13 个模块），不能作为独立仓库的精确调用图。
- 文本核实独立仓库：`create_post_with_draft` 单稿新建、`_prepare_daily_news_candidate` 并行候选新建，均先构造新 `Post` 再获得首图；并行质量回调可能重画。身份记录放在首图文件已生成之后、质量回调之前，而不是最终接受稿件时。
- 不修改 CLI、正文、提示词、审核门槛、重画次数、生产配置或进程。每日我去、AI 资讯及用户直接提供图片的流程不新增身份记录。

## 持久化结构

`post.platform.image_lineage` 与可变的 `platform.image` 并列：

```text
schema_version: 1
first_image:
  post_id
  image_path
  image_sha256
  prompt_version
  prompt_hash
  scene_audit_version
  provider / model
  captured_at
first_review:  # 尚未审核时不存在
  image_sha256
  ok / score
  provider / model
  issues
  recorded_at
```

- SHA256 从实际文件字节计算，不信任资产缓存 hash；路径只作取证参考，不作为身份依据。
- `prompt_hash` 是初次响应元信息中完整 `prompt` 的原始 UTF-8 SHA256，不裁断、不 trim、不重建；无 prompt 时为 null。版本缺失为 null，不用当前模板版本补齐。
- 场景审计版本来自创建当时 `news.image_event_audit.version`，缺失为 null。
- 两项记录均只写一次。旧稿未记录身份时始终未知，不能用重画后的 `platform.image` 回填历史。
- 这是应用层 write-once 约定，不是数据库不可变约束；调用方不能直接改写该字典。getter 返回深拷贝。

## Helper 接口与 CLI 接入

```python
record_initial_news_image(post, *, image_path, image_meta) -> bool
get_first_image_identity(post) -> dict | None
record_first_image_review(post, *, ok, score, provider, model, issues) -> bool
first_image_pass_summary(posts) -> dict
```

1. 创建 helper 已接入上述两处新稿初次生成路径，返回是否新增记录。不得在旧稿加载、恢复任务、重画函数或审核回调中调用它补身份。
2. 主代理在 `_review_with_bounded_image_repair` 的原始 `review_fn` 返回后、任何重画前调用 `record_first_image_review`。必须传原始审核结论；记录 helper 不改写结论，统计 helper 另按下述 `ok` 与 70 分双条件计数。
3. 评分 helper 检查当前 `post.assets[0]` 实际字节等于首图 SHA256；无身份、已记录评分、缺文件、非图片或 hash 不同均返回 False，不写入。未知版本但身份已核实可正常记录评分。
4. helper 不调用 `save_post`、不写 ledger。创建流程沿用现有保存点；CLI 应在首图审核前持久化身份，在首次评分后立即保存 Post 与候选 ledger，包括审核失败、最终筛掉的候选。只有最终入选稿不足以计算所有候选通过率。
5. 若 CLI 在审核前已复制 `original_post`，须在重画前给该副本同步相同首次评分，或在记录后重新深复制；不要只更新当前对象后恢复未记录评分的旧副本。同步可在原图尚在时，对两个对象分别调用评分 helper，之后保存。
6. 身份计算读文件失败会在写入记录前抛出；评分阶段读文件失败返回 False。调用者应记录无法采证，不能伪造通过/失败评分。

## 统计口径

- 纯函数，只读传入的 Post，不读磁盘、网络或当前图片元信息；按唯一 post ID 计数，重复 ID 明确报错，避免重复计算重画或重载快照。
- `total` 和 `by_prompt_version` 均含 `candidates/reviewed/passed/failed/pending/identity_missing`；`unknown` 始终单列。无身份旧稿不归入当前版本、不读取 `quality_gate` 猜测首图结果。
- 已核实首图的审核只有在 `ok=True` 且 `score` 为有限的 int/float（排除 bool）、`score >= 70` 时计为 passed；`ok=True/65分`、缺分、字符串、NaN、无穷值或 `ok=False` 均计为 failed，仍留在分母。缺审核或无法核实身份则保持 pending，不伪造失败证据。原始记录不会被统计函数修改。
- `pass_rate = passed / candidates`，分母包含失败与待审候选；未审核不被标为审核失败，而列 pending。
- `reviewed_pass_rate = passed / reviewed` 同样包含已审失败；它不是全部候选完成后的最终通过率。
- `above_50_percent` 只在有候选且全部首次审核证据齐全时计算，严格大于 50%；未完成时为 null。空分母的率为 null。
- v1/v2/v3 分版本呈现且同时保留总计，不能删除旧版本失败或将修复图的结果当作首图。历史未采集的身份保持 unknown，本修改不修写历史记录。

## 验证状态与风险

按本次要求不执行测试、不调用模型、不生成或上传，不 reload worker。仅静态核实两处初次创建接入、质量回调顺序及重画对 `platform.image` 的替换范围。

尚未通过运行验证：新旧稿兼容、文件原地被重画覆盖时的 hash 拒绝、重复评分不覆盖、原图副本恢复、失败候选 ledger 完整性、跨版本统计。CLI 保存及副本同步由主代理完成。首图身份元信息缺失时保留 null；不能据此宣称首图通过率已超过 50%。
