# 每日新闻场景锚点误拒绝最小修复

## 范围与风险

仅修改 `src/workflow/create_post.py` 的 `_normalize_daily_news_image_event` 及其新增场景匹配 helper，增加 `tests/test_news_scene_grounding.py`。不修改生成提示词、视觉审查及评分阈值、重绘次数、AI 资讯、地图、编排或上传代码，不修改生产 post/checkpoint，不调用生产模型，不重启 worker。

按 systematic-debugging、test-driven-development、gitnexus-impact-analysis 执行。修改前 GitNexus upstream impact 返回 CRITICAL（94 个直接影响、138 个总影响、52 个流程）；已作风险提示。该索引只覆盖旧 `auto_redbook`，不覆盖独立 `redbook_tools`，且结果存在文件展开，不能将这些数字视为独立仓库的准确调用范围。随后按实文件确认调用入口为图片提示更新、单篇/材料生成和每日新闻候选生成，均需保留现有接口和失败关闭行为。

## 根因与复现

两个真实样本只读取标题、正文和 `platform.news.image_event_audit` 等必要字段，不读取或记录签名图片 URL：

- `241a0fbf6e0f406d80345bdf70f85919`：正文含乌克兰持续打击俄罗斯炼油厂，writer/rewrite 含炼油厂、储罐、蒸馏塔及烟火，却被归一化为短标题。
- `6769e4d77410410e9c4a9d3ddb736c77`：正文明确描述耶路撒冷街头群体，writer 的商业街、市民场景却被拒绝。

原归一化复用 `_daily_news_text_matches_context`。该函数先调用标题清洗器 `_clean_daily_news_title_candidate`，会在逗号等标点处截断。耶路撒冷的关键事实句被清成“报道描述”；长场景也只剩首个物件短语，正文中的地点和活动锚点在比较前就丢失。即使仅移除截断，长篇构图描述仍会稀释原有整体相似度，不能仅靠降阈值处理。

这证明归一化环节会丢失已有合法场景；不据此断言已生成图片的所有错误均由此造成。

## 修复约定

1. 场景比较使用完整文本，不再经过标题清洗。共享标题匹配器及生成提示词保持不变。
2. 仅从已接受正文的“内容”部分提取事实句，不以评价、读者偏好或抓取页面的关联推荐作事实证据。
3. 提取场景与事实句共享的多字符锚点，过滤常见泛化前缀；结合匹配事实句中的活动/状态或全文词汇匹配判断。无需针对具体稿件、城市或炼油厂名称硬编码。
4. 增加通用状态约束：损毁、碰撞、建成通车、选举结果、体育活动、逮捕、暴力。候选声称这些已发生状态时，必须在具有共同锚点的事实句中找到肯定依据。计划、未来、假设和否认不是已发生证据；失败返回 `unsupported_scene_state`，不能由标题相似度绕过。
5. 保留合理场景的完整构图描述，不再自动缩为短标题；明确无关的场景仍返回原有 fallback，不增加生成重试循环。
6. 场景审计版本从 `scene-anchor-v1` 升为 `scene-anchor-v2`。保留原有 writer/rewrite 等字段，增加 `supported_anchors`、`evidence_sentence` 和 `state_checks`，记录接受或拒绝依据。图片提示模板 `single-scene-v3` 与此版本独立，不作修改。

边界：这是确定性场景依据检查，不是完整语义事实核验。已发生的破坏性事件可支持受损设施的编辑性示意，但不能据此确认任意具体画面细节。细节真实性和成图质量仍交由现有视觉审查；未降低评分或放宽重绘上限。

## 测试与证据

新增 21 项参数化测试，禁止网络连接。覆盖两篇真实稿件的三段 writer/rewrite 场景、替换城市/设施名称、长构图、未来/否认/未开工、同主体不同动作、另一设施的损毁不能证明目标损毁、评价不能提供事实依据。

- 首轮失败复现：11 failed / 6 passed。
- 初步修复及相关回归：56 passed。
- 追加未来状态与街头枪击反例：3 failed / 18 passed；补齐状态判断后重新运行。
- 最终专项回归：60 passed，2.20 秒（场景 grounding、场景契约、图片提示完整性及智能体图片锚点四个测试文件）。
- 最终完整 tools 回归：317 passed / 5 skipped，35.98 秒。显式设置 `REDBOOK_TEST_POSTGRES=0`；此结果不代表生产数据库、模型或平台实机验证。两项警告为既有 LangGraph 配置未来变更和 Pillow `getdata` 弃用。
- 对真实 post 中保存的三段场景作只读归一化复核：3/3 完整保留，理由均为 `body_supported`；炼油厂锚点为“炼油厂”，街景锚点为“耶路撒冷”。没有修改原稿，也没有生成新图片。

临时产物仅位于独占目录 `E:/AI/codex/redbook_runtime/data/runs/tests/scene-anchor-20261001-c925`；完整回归报告为 `resumed-results.xml`。源码和测试落盘后由主代理决定何时恢复原 checkpoint 加载新代码，本修复不干预运行中的 worker。
