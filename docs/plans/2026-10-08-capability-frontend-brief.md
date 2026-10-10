# 前端实施要求

先读配套设计文档的 UI 章节及全部界面验收要求。范围为 frontend/src/capabilities/、frontend/src/App.tsx、对应样式和 tests/test_capability_browser.py；不要改后端或工具包。

新增能力中心六页签和对话本次能力，复用现有 api、Lucide、样式、同源会话。URL hash 保留页签与详情。符合 1440/1024/390 视口、抽屉 focus/ESC/未保存提醒、空态、局部错误、分页、敏感数据不显示。控件对应真实接口，不使用假数据。

## 与后端统一契约

- GET /api/capabilities: {rows, database, environment, issues, recent_calls}；rows 含 id,name,description,kind,group,enabled,revision,version,health:{status,observed_at,error,probe},binding,stages,dependencies,effects,input_schema,output_schema,concurrency,active_runs。工具默认最多50，支持 query,kind,group,status,limit,cursor。
- GET /api/capabilities/{id}: 单个对象含 versions,recent_calls,active_runs。PATCH body {expected_revision,enabled?,timeout_seconds?,reason?}；POST /{id}/revoke body {expected_revision,reason}。
- POST /api/capabilities/checks body {resource_ids} 返回 {operation_id,status}；GET checks/{id} 返回 {status,stages,results,error}，只有检测中轮询。
- GET/POST /api/mcp/connections，PATCH/DELETE /{id}；连接 body {name,transport:stdio|streamable_http,command,args:[],cwd,url,environment:{},headers:{},startup_timeout_seconds,timeout_seconds,enabled,expected_revision?}；敏感值只写，GET 返回 configured 标记和密钥引用，不回显。POST /api/mcp/import-preview body {config:{mcpServers:{...}}} 填表预览，POST /connections/{id}/discover 启动 operation；POST /{id}/tool-policy body {expected_revision,tool_name,schema_hash,enabled,stages:[preparation|evidence],purpose}。
- GET /api/skills: {rows,default_mode,directories}；GET /{id}: 含 body,files,versions,recent_calls；POST import-preview body {source_path} （文件夹/ZIP）返回 {preview_id,hash,valid,issues,name,files,total_bytes,collision}；POST import-commit body {preview_id,hash,allow_new_version}；PATCH /{id} body {expected_revision,enabled?,version?,body?,default_mode?}；DELETE /{id}?expected_revision=N 退役；GET /{id}/resources?path= 返回 {content}。
- GET/POST /api/memory/items，PATCH /{id}，POST /{id}/forget；字段 content,key,scope:workspace|account|column|conversation,scope_id,origin:manual|inferred,source_ref,active,expires_at,expected_revision,reason。
- GET /api/knowledge/documents: {rows,next_cursor,namespaces,index_progress} query namespace,type,purpose,query,index_status,limit,cursor；GET /documents/{id}?namespace= 返回正文,chunks,versions,policy；PATCH policy body {namespace,expected_revision,excluded_purposes:[],annotation,source_ref}；POST search body {query,namespace,purpose,limit} 返回 {rows,elapsed_ms,ranking,embedding_model,embedding_dimensions}；POST index-jobs body {namespace} 返回 operation。
- GET /api/conversations/{id}/context 返回 {status,context,policy,model,snapshots,actual_usage,tokens_estimate,raw_message_count,policy_revision}；POST compact body {} 返回 operation；PUT context-policy body {expected_revision,mode:auto|manual,soft_threshold,keep_recent}。
- GET /api/conversations/{cid}/plans/{pid}/capabilities 返回 {tools,skills,skill_mode,skill_names,memory,models,profile,readiness,frozen,snapshot_id,version}；PUT 同路径 body {version,skill_mode,skill_names,disabled_tools?} 返回 {plan,...当前能力}，须同步上层 plan.version。确认请求保留 skill_mode,skill_names（若未编辑按读取默认）。GET /api/runs/{id}/capabilities 同类结构加 calls,resume_diff；无旧记录标 not_recorded。
- GET /api/capability-calls 与 /api/resource-changes 返回 {rows,next_cursor}；calls 行含 id,resource_id,resource_name,run_id,stage,status,origin,input_summary,result_summary,error,queue_ms,connection_ms,execution_ms,wall_ms,retries,parent_call_id,started_at,version,evidence_refs。
- 所有长操作共用 GET /api/capabilities/checks/{operation_id}。
- 错误：{code,message,resource_id,next_action,retryable,revision}，现有 api 需兼容 message。

先写有意义的浏览器测试并观察缺失界面导致失败，再实现。不得自动生成/上传/开浏览器，管理读取无副作用。可用已安装依赖，不安装C盘。将实现/测试/待补后端契约记录到同目录 2026-10-08-capability-frontend-report.md。不要提交、不要部署、不要创建子agent。
