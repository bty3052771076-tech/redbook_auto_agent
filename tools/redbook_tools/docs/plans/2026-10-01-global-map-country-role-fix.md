# 全球事件地图国家歧义与航班目的地最小修复

## 已复现问题

已上传稿件 `34c5a4bdcfb9435aae6ca73a4126c13c` 中，标题 `Bolivia arrests attorney general accused by U.S. of drug cartel ties` 被定位到美国。旧地理表只有少量国家标签锚点，没有 Bolivia，因而把文本中唯一识别出的 U.S. 当作位置。Peru、Chile 同类标题也能复现，不是单条新闻的问题。

另一个问题是航班路线保护依赖 `crash / stab / divert` 等少量事故关键词；`terrifying flight bound for Israel` 未进入保护分支，会沿用目的地国家甚至上游坐标。

## 影响与约束

- GitNexus 绑定旧 `auto_redbook`（索引日期 2026-09-24）；定位解析和地图快照构建均返回 CRITICAL。图未覆盖独立 tools 仓库，数字不作为精确调用范围。
- 实文件确认解析器由 `global_map/workflow.py` 调用，快照构建供地图 service/生成流程使用。
- 仅改地理解析、地图流程、新增测试及本文档。不改正文、AI 资讯、智能体核心、生产稿件、上传流程或 checkpoint。
- 不联网、不安装，不从多边形计算“事件坐标”。

## 最小实现

1. 使用已有本地 WorldMonitor `countries.geojson` 的 `properties.name` 列表补全国家/地区识别。本机文件有 258 个要素；读取缓存以路径、修改时间和大小为键。
2. 英文名称与现有别名合并，最长重叠名称优先，避免名称包含关系制造歧义；U.S. 与 United States of America 归于同一国家。
3. 所有识别到的国家均参与歧义判断，即使没有现有标签锚点。多个国家且无明确发生地证据，返回未知，不让已知他国抢占位置。
4. 本次不扩建标签坐标表：单独识别到的新国家但没有可信标签锚点，也保留未知。国家名称补全不代表可以确定事件发生地。
5. 本地名单缺失、无效或为空时，文本推断失败关闭，不退回不完整的旧名单继续猜测。已有非航班明确经纬度保持不变。
6. 航班事故或路线表述（包括 bound / en route / to / from）进入保守分支。目的地和上游目的地坐标不能证明发生地；只接受明确已备降/已着陆的国家文本。不接受计划、可能、否认的着陆；多个着陆国家也不选其中一个。
7. 显式 `basemap_path` 从创建流程透传至定位解析，与绘制使用同一名单文件。默认继续使用既有 `GLOBAL_MAP_BASEMAP_PATH` / `WORLDMONITOR_DIR` 本地路径约定。

这是保守文本定位规则，不是通用事件关系解析模型。证据不足减少可定位数量是预期行为；不能为满足地图数量而猜测位置。

## 测试记录

临时目录：`E:/AI/codex/redbook_runtime/data/runs/tests/map-country-roles-20261001-f761`。

- 有效红测：14 failed / 2 passed，复现三国被美国抢占、上游国家标签污染、名单不可用、多个航班路线写法及错误的着陆证据。
- 初步定向回归：22 passed，包括现有航班合并、定位安全测试。
- 最终地图回归：43 passed，3.41 秒，覆盖定位、聚合、查重、翻译、来源兜底及本地地图审查。
- 新增测试共 19 项，包含读取真实本地 WorldMonitor 文件的离线验证，以及显式底图路径、唯一已知国家、非航班坐标保持和多国未知的回归。
- 全套 tools 回归：257 passed / 11 failed / 5 skipped，42.53 秒；PostgreSQL 实机测试禁用，未调用生产模型或平台。
- 两条已有依赖警告：LangGraph 序列化默认值未来调整、Pillow `getdata` 弃用。

全套失败均位于本任务未修改的 `tests/test_ai_digest_release_integrity.py`，交由对应代理处理，不掩盖为通过：

- `test_library_release_is_a_tool_update_even_with_background_models`
- `test_url_version_is_not_a_model_identity[tag/version/release]`（3项）
- `test_saved_title_fallback_uses_repository_not_incidental_qwen`
- `test_generic_github_product_is_not_appended_to_real_model`
- `test_title_keeps_complete_long_model_name`
- `test_existing_bad_artifact_fails_final_semantic_gate`
- `test_library_named_title_still_cannot_claim_qwen_release`
- `test_fallback_result_is_revalidated_not_silently_accepted`
- `test_repair_keeps_unrelated_valid_item_and_provenance`

已上传旧稿件未改写或重传；主代理可在后续恢复时加载此定位规则。
