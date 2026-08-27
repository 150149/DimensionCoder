# DimensionCoding — REST API 契约（API.md）

> **版本**: v3.0｜ **来源**: WP2-3-基础后端契约-API与配置.md §3（逐字固化）
> **实现**: `dc_server/rest_api.py`（端点 1-29，当前实现）｜ **配套**: `dc_server/config.py`（端口/HOST/DB/工作区/CORS 环境变量入口）
> **跨包端点**: 端点 30 由 SWP2-C 新增；端点 31/32 由 SWP3-B1 新增（rest_api.py 跨包追加点）。

## 1. 错误约定

- 请求体一律 JSON。响应一律 `{"status": "ok", ...}` 风格（与旧版一致）。
- 错误：404 用 `HTTPException(404, detail=...)`；校验失败 400。
- **错误文案约定（第 8 轮 J5 修订）**：状态敏感端点（`advance/approve/reject/resume/delete/pause/start`）校验失败或并发冲突时，`detail`/`error` 必须用**用户可读中文文案**（如 409 → `"该操作已被其他人处理，请刷新查看最新状态"`、状态机拒绝
  → `"当前状态不允许此操作，请刷新查看最新状态"`），禁止直接透出英文状态机消息（`invalid transition` 等仅可追加在括号内作调试信息）。
- **工具错误约定**：`POST /api/tool/invoke` 的工具错误包在 `result` 内，前缀 `[Error] ...`；越界路径类错误由 SWP2-B 安全强化后返回 `[Security] ...`。

## 2. REST 端点表（32 行；当前实现 29 行）

| # | 端点 | 请求体（必填/可选） | 响应关键字段 |
|---|------|---------------------|-------------|
| 1 | `GET /api/health` | — | `{status:"ok", version:"0.2.0"}` |
| 2 | `GET /api/prompt/{name}` | — | `{name, content}`；缺失 404 |
| 3 | `POST /api/task` | `task_type`(默认 dev-full-flow)/`title`/`description`/`epic_id?`/`assignee?`/`auto_start?`(**L4 修订：auto_start 为废弃参数——旧代码读取但忽略，保留兼容，不产生行为**)；**custom
时另有 `steps`（WP2-4 T2.5）** | **`{task_id, task_type, title, steps?}`（第 7 轮 B6 修订：与 WP3 planner/WP4 createTask 响应统一；custom 时 `steps` 非空——前端确认框数据源，无需二次 GET）** |
| 4 | `GET /api/tasks` | — | `{epics, tasks, task_count, status_distribution, available_task_types}`（**M6 修订：available_task_types 含 7 预设 + `"custom"`**） |
| 5 | `GET /api/task/{id}` | — | `{task, artifacts, monitor_conversations, step_messages, recent_events}`；**`task.steps` 结构（问题
17）**：`[{step_id,title,status,required,parallel_with,human_attention,model_tier,sort_order}]`——执行循环与前端 FlowOverview/ProgressRail 的唯一步骤来源 |
| 6 | `DELETE /api/task/{id}` | — | `{status:"deleted", task_id}` |
| 7 | `GET /api/task/{id}/next-step` | — | `{step_id, title, model_tier}` 或 `{}` |
| 8 | `POST /api/step/prepare` | `task_id`, `step_id` | `{system_message, system_prompt, step_context, temp_dir, model_tier, step_title, step_id}`；`_` 前缀元步骤返回最小响应。**字段补齐（B5
修订）**：`system_prompt` = 纯规则提示词（executor.md 等，按步骤角色）；`step_context` = 任务背景+前序产物；`system_message` = 两者拼装后完整值；`temp_dir` = `PROJECT_ROOT/.dc_tmp/<task_id>/<step_id>/`（T2.6 双粒度） |
| 9 | `POST /api/step/submit` | `task_id`, `step_id`, `conversation?`(废弃) | `{status:"step_completed", task_id, step_id}`（幂等） |
| 10 | `POST /api/step/save-conversation` | `task_id`, `step_id`, `conversation` | `{status:"ok", ...}` |
| 11 | `POST /api/step/message/append` | `task_id`, `step_id`, `message:{role,content,toolName?,input?,output?,round_num?,tool_call_id?,tool_calls?}`（tool_call_id: tool 消息的关联 id；tool_calls: assistant 消息的 OpenAI tool_calls JSON 数组文本，B1 方案②） | `{status:"ok", task_id, step_id, seq}` |
| 12 | `GET /api/step/{sid}/messages?task_id=X&after_seq=N` | — | `{task_id, step_id, messages, max_seq, after_seq}` |
| 13 | `POST /api/step/chunk/save` | `task_id`, `step_id`, `chunk:{chunk_type:"text"\|"tool_call_start"\|"tool_call_result", content, call_id?}` | `{status:"ok", seq}` |
| 14 | `GET /api/step/{sid}/chunks?task_id=X&after_seq=N` | — | `{task_id, step_id, chunks, max_seq, after_seq}` |
| 15 | `POST /api/step/messages/clear` | `task_id`, `step_id` | `{status:"ok"}` |
| 16 | `GET /api/artifact/{tid}/{sid}/intervention` | — | `{content}` 或 `{}` |
| 17 | `POST /api/step/messages/clear-intervention` | `task_id`, `step_id` | `{status:"ok"}` |
| 18 | `POST /api/artifact/save` | `task_id`, `step_id`, `artifact_type`, `content`, `content_format?` | `{status:"ok"}` |
| 19 | `POST /api/tool/invoke` | `name`, `args` | `{result: str}`（工具错误也包在 result 内，`[Error] ...`） |
| 20 | `POST /api/step/advance` | `task_id`, `step_id`, 无 decision 时 `new_status`；有 decision 时 `decision:"approved"\|"rejected"`（**无 changes_requested**——旧代码 handle_gate 只支持 approved/rejected，传其他值 500，P0-4 修订）, `reason?` | `{status:"ok", step_id, decision?}` 或 `{status:"ok", step_id, new_status}` |
| 21 | `POST /api/step/compress` | `task_id`, `step_id` | 消息 ≤6：`{status:"skipped", reason:"too_few_messages", count}`；否则 `{status:"ok", original_count, compressed_count}`（保留最近 6 条，早期合并为一条 system 摘要，摘要前缀 `[早期对话已压缩]`；**

seq 语义（问题 21 + 第 9 轮 B2）：合并摘要的 seq = 被合并消息的最小 seq，其余保留消息 seq 不变——after_seq 增量拉取与前端去重不受影响；此语义为 Web 版刻意变更（V9，旧代码 clear+重新 append 导致 seq 全从 0 重排），由 SWP2-A T2.1 适配项实现（`append_message`
支持显式 seq 或 compress 直接 SQL 更新），T2.3 `test_compress_ok` 补 seq 断言**） | | 22 | `POST /api/step/resume` | `after_intervention`(默认 false)；false 时另需 `step_id`, `message?`
| `{status:"resumed", step_id}` 或 `{status:"ok", task_id}` | | 23 | `POST /api/intervene/step` | `task_id`, `step_id`, `intervention_type:"send"\|"force_inject"\|"stop"`, `message`
| `{status:"ok", intervention_type, step_id}` | | 24 | `POST /api/intervene/flow` | `task_id`, `reason?`, `mode?("pending"默认"immediate")`
| `{status:"queued"\|"ok", task_id, reason}` | | 25 | `POST /api/monitor/export` | `task_id`, `step_id?`, `final_review?`
| `{system_message, task_id, step_states:[{step_id,title,status}]}`（对话摘要内联进 system_message，无临时文件夹） | | 26 | `POST /api/monitor/cleanup` | `temp_dir?` | `{status:"ok"}` | | 27
| `POST /api/monitor/trigger` | `task_id`, `trigger_step_id?`, `reason?` | `{system_message, task_id, trigger_step_id, step_states}` | | 28 | `POST /api/monitor/save-conversation`
| `task_id`, `trigger_step_id?`(默认 `_monitor`), `conversation` | `{status:"ok", task_id, trigger_step_id, message_count}` | | 29 | `GET /api/task/{id}/monitor-conversations` | —
| `{task_id, monitor_conversations:{sid:[msgs]}}` | | 30 | `POST /api/task/{id}/pause` | —（可选 `pause_level`，默认 `"gate"`——**M2 注明：不扩展 PauseLevel 枚举（旧 data_models.py 只有
step/flow），`"gate"` 为字符串约定**） | `{status:"ok", task_id}`；**H11 修复的唯一新增端点**（gate 暂停置 task paused；state_machine 新增 `pause_task`，实现与 `reject_gate` 的 task 更新段相同）。**SWP2-C 新增（本包未实现）** |
| 31 | `POST /api/task/{id}/start` | — | `{status:"ok", task_id}`；**第 7 轮 A1 新增——V2「立即启动」交互的后端通道（旧代码走 VS Code 内部 `scheduleAndRun`，无 REST）**。语义：校验 task 存在且 `status="active"`（未启动任务 =
status active + 步骤全 pending；**第 8 轮 J3：`status="paused"`
亦允许**）→ 调 `brain.orchestrator.start_task(task_id)`（幂等，已 running 则直接返回 ok）→ 触发执行循环。**SWP3-B1 新增（本包未实现）** | | 32 | `GET /api/step/{sid}?task_id=X` | — | **第 8 轮 J1 新增——StepDetail
数据源聚合端点**。响应逐字段固化：`{stepId, taskId, prep, conversation, messages, max_seq, step}`——`prep`=端点 8 prepare 结果（幂等，入 `_prepCache`），`messages`=step_messages 全量（按 seq），`conversation`
=旧协议兼容别名（=messages），`max_seq`=最新 seq，`step`=task_steps 行。**
SWP3-B1 新增（本包未实现）** |

## 3. 工具表（10 个工具，`POST /api/tool/invoke` 的 name 值，args 字段逐字固化）

| 工具 | args | 行为要点（与旧一致） |
|------|------|---------------------|
| `dcflow_list_dir` | `dir_path?` | 返回 `[DIR]`/`[FILE]` 前缀排序行，空目录 `(空目录)` |
| `dcflow_read_file` | `file_path`, `start_line?`, `end_line?` | **一律
safe_resolve**：相对 root 解析，绝对路径也必须在 root 内（V3 修订，与旧"绝对路径优先"语义不同）；按 `start_line`/`end_line` 行范围读取（含，默认 L1 起到末尾），返回 `[L{start}-L{end}]` 前缀 + 内容；请求范围超 30000 字符不返回内容，返回 `读取文本超过限制，当前N字符，限制30000字符，请减少行数来读取。`；缺失返回 `(文件不存在: ...)` |
| `dcflow_write_file` | `file_path`, `content` | 自动建目录；返回 `✓ 已写入 ... (N bytes)` |
| `dcflow_edit_file` | `file_path`, `old_string`, `new_string`, `replace_all?` | 未匹配返回 `(未找到匹配文本: ...)`；返回 `✓ 已替换 N 处` |
| `dcflow_read_doc` | `filename` | 依次查 `.github/docs`、`.github/instructions`、`docs`；读前 30000 字符 |
| `dcflow_search_code` | `pattern`, `path_filter?` | 正则搜，最多 30 条，`path:line: content`；**非法正则（`re.error`）捕获返回 `{"result": "[Error] 正则无效: <pattern前80字符>"}`（问题 12，不 500）** |
| `dcflow_run_cmd` | `command`, `timeout_seconds?`(默认 60) | **安全包装（T2.2）**；输出前 10000 字符；**
编码提示**：Windows 中文环境下子进程（如 WinRAR/7z）输出为 GBK，在 `-X utf8` 下用 `subprocess.run(text=True)` 无 `encoding=` 会得到 � 乱码，必须显式 `encoding='gb18030'` |
| `dcflow_step_done` | `task_id`, `step_id`, `summary?` | summary 存 artifact（**artifact_type="summary"、content_format="text"，M9 修订**）；返回 `✓ 步骤 ... 确认完成` |
| `dcflow_list_steps` | `task_id` | JSON 数组 `[{step_id,title,status,type,human_attention,required,model_tier,sort_order,parallel_with}]`（不含 _ 前缀系统虚拟步骤；2026-08-22 字段扩展：补 type/human_attention 等供 Monitor 区分 gate/plan/code_review 类型与审批属性） |
| `dcflow_adjust_flow` | `task_id`, `action`(no_change/skip_steps/add_steps/remove_steps/reorder_steps/mark_complete), `step_ids?`, `steps_json?`, `order_json?`, `reasoning?` | 每个 action 落 event；返回 `action=X done`；**
steps_json 元素可带 `type`（executor/gate/plan/code_review，缺省 executor）；操作/创建 _ 前缀虚拟步骤（如产出报告）被拦截** |

## 4. 不变式

- **Web 版不新增 Python 端点**：除端点 30（pause，SWP2-C 新增）、端点 31（start，SWP3-B1 新增）、端点 32（step 聚合，SWP3-B1 新增）外，不再新增端点。当前实现（SWP2-A 完成态）为端点 1-29；SWP2-C 完成后 30 行；SWP3-B1 完成后 32 行。
- **custom 类型仅 T2.5 规定的一个扩展点**：`POST /api/task` 的 `task_type == "custom"` 分支（steps 校验/创建）由 SWP2-C 实现，不做其他扩展。
- **孤儿端点/表说明（审查三低项）**：`POST /api/monitor/cleanup`（无调用方）、`stream_chunks` 表（写不读）、`messages/chunks` 增量端点（前端未消费）、`recent_events`/`epics`（前端不渲染）——**均保留**（旧架构兼容与调试用途），不删除、不新增消费方。
