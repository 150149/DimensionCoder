// ═══════════════════════════════════════════════════════════════
// REST/SSE 类型契约（SWP4-A / WP4-2 T4.2）
// types.ts 与 WP3 §2.2 REST 契约 + API.md 端点表逐字段对齐（H1 修订：
// 原 §3.4 章节号不存在，逐字段对齐；字段名一字不改，D4）
// ═══════════════════════════════════════════════════════════════

// ── 任务 / 步骤（端点 3/4/5，API.md L20-22）──

/** task_steps 行（端点 5 问题 17 结构）——执行循环与 FlowOverview/ProgressRail 的唯一步骤来源 */
export interface StepDef {
    step_id: string
    title: string
    status: string
    required: boolean
    parallel_with?: string[]
    human_attention?: string
    model_tier?: string
    sort_order?: number
    // J5：gate 步骤 AI 是否已输出决策请求包（后端 get_task 判定）——选项类/审批类交互区分
    has_decision_pkg?: boolean
    // Token 展示：步骤累计用量（task_steps 三列，0 表示尚无数据）
    token_prompt?: number
    token_cached?: number
    token_completion?: number
    // 当前上下文长度（最近一次请求的输入 tokens，覆盖写非累加；上下文占用条数据源）
    context_tokens?: number
    // 运行统计（2026-08-21 步骤级落库，与 token 三列同表同位置）：
    // requests=LLM 请求数（每轮 LLM 流=1）；ttft_total_ms/ttft_samples=首字延迟
    // 累计（平均 = total/samples）；output_duration_ms=纯 API 输出时长累计；
    // run_duration_ms=最近一轮 LLM 流结束的运行时长定格值（active 时前端实时补差）
    requests?: number
    ttft_total_ms?: number
    ttft_samples?: number
    output_duration_ms?: number
    run_duration_ms?: number
}

/** tasks 行（端点 4 tasks 数组元素 / 端点 5 task） */
export interface TaskSummary {
    id: string
    type: string
    title: string
    description?: string
    status: string
    pause_level?: string
    assignee?: string
    best_effort?: boolean
    created_at: string
    updated_at: string
    steps: StepDef[]
}

/** 端点 4 GET /api/tasks 响应 */
export interface TaskOverview {
    epics: unknown[]
    tasks: TaskSummary[]
    task_count: number
    status_distribution: Record<string, number>
    available_task_types: { type: string; name: string; description?: string }[]
}

/** 端点 3 POST /api/task 响应（B6：{task_id, task_type, title, steps?}，custom 时 steps 非空） */
export interface CreateTaskResult {
    task_id: string
    task_type: string
    title: string
    steps?: StepDef[]
}

/** 端点 5 GET /api/task/{id} 响应 */
export interface TaskDetail {
    task: TaskSummary
    artifacts: unknown[]
    monitor_conversations: Record<string, Message[]>
    step_messages: Record<string, Message[]>
    recent_events: unknown[]
}

/** 审查/收尾步骤 token 用量（monitor 类 / review / report 实体行，Token 展示） */
export interface StepTokens {
    token_prompt: number
    token_cached: number
    token_completion: number
}

/** 审查/收尾步骤运行统计（实体行，orchestrator 每轮 LLM 流结束落库） */
export interface StepStats {
    run_duration_ms: number
    output_duration_ms: number
    ttft_total_ms: number
    ttft_samples: number
    requests: number
}

/** 端点 29 GET /api/task/{id}/monitor-conversations 响应（A2：MonitorDetail 数据源） */
export interface MonitorConversations {
    task_id: string
    monitor_conversations: Record<string, Message[]>
    /** Token 展示：虚拟步骤 token 用量映射（MonitorDetail 上下文占用条数据源） */
    step_tokens?: Record<string, StepTokens>
    /** 2026-08-20 多实例：虚拟实例状态映射 {实例id: active/completed/pending/stopped} */
    monitor_steps?: Record<string, string>
    /** 2026-08-21 实体化：审查/收尾步骤 sort_order 映射（FlowOverview 多图标区间归属） */
    monitor_order?: Record<string, number>
    /** 2026-08-23：monitor 锚点映射 {实例id: 触发步骤 id}（FlowOverview 眼睛归属——
     sort_order 被后续插入步骤挤压漂移，按锚点步骤归属） */
    monitor_anchors?: Record<string, string>
    /** 2026-08-21：审查/收尾步骤运行统计（输出速度/首字延迟/运行时长/请求数） */
    step_stats?: Record<string, StepStats>
}

// ── 消息（端点 11/12/29/32，step_messages 行 + B1 扩展列）──

/** 步骤消息（DB 行转换后：input=tool_input JSON 解析、output=tool_output、toolName=tool_name） */
export interface Message {
    id?: number
    task_id?: string
    step_id?: string
    seq?: number
    role: string
    content: string
    tool_call_id?: string | null
    tool_calls?: string | null
    toolName?: string
    tool_name?: string
    input?: unknown
    output?: string
    round_num?: number
    created_at?: string
}

// ── 步骤聚合（端点 32，J1）──

/** 端点 8 prepare 结果（B5 字段补齐；元步骤返回最小响应） */
export interface StepPrep {
    system_message: string
    system_prompt: string
    step_context: string
    temp_dir: string
    model_tier: string
    step_title: string
    step_id: string
}

/** 端点 32 GET /api/step/{sid}?task_id=X 响应（逐字段固化） */
/** 步骤实时状态快照（2026-08-27 详情页首屏进行中状态）：getStep 附带——
 * 进行中轮的思考/文本累积、当前执行中工具、streaming 与最后事件 seq。
 * 从总览进详情页时进行中事件已被 SSE 缓冲冲掉，快照保证首屏可渲染。 */
export interface StepLive {
    seq: number
    streaming: boolean
    thinking: string
    text: string
    tool: { callId: string; name: string; input: string } | null
    // 2026-08-27：整轮未落库期间已完成的工具（多工具轮窗口期刷新渲染用）
    completedTools?: Array<{ callId: string; name: string; input: string; output: string }> | null
}

export interface StepData {
    stepId: string
    taskId: string
    prep: StepPrep
    conversation: Message[]
    messages: Message[]
    max_seq: number
    step: StepDef
    // 分页：消息总数 / 是否还有更早消息（getStep 默认最近 200 条，历史折叠）
    total?: number
    truncated?: boolean
    // 2026-08-27：进行中状态快照（无则 null）
    live?: StepLive | null
}

// ── 配置 / 文件系统（WP3 §2.2 B3 修订，逐字段对齐）──

export interface ConfigView {
    baseUrl: string
    lightModel: string
    powerModel: string
    projectRoot: string
    port: number
    host: string
    hasApiKey: boolean
    contextWindow: number
    channelType?: string // 2026-08-19：llmChannel 通道类型（newapi_channel_conn 等），可选（旧后端无此字段）
    hasChannel?: boolean // llmChannel 已配置（key 不回传，同 apiKey 策略）
    // 2026-08-23：light/power 独立端点与 Key + 六项价格（可选——旧后端无此字段）
    lightBaseUrl?: string
    powerBaseUrl?: string
    hasLightApiKey?: boolean
    hasPowerApiKey?: boolean
    lightInputPrice?: number
    lightCachedPrice?: number
    lightOutputPrice?: number
    powerInputPrice?: number
    powerCachedPrice?: number
    powerOutputPrice?: number
}

export interface ConfigSave {
    baseUrl?: string
    apiKey?: string
    lightModel?: string
    powerModel?: string
    projectRoot?: string
    contextWindow?: number
    llmChannel?: string // 2026-08-19：New API 通道 JSON（可选；设置页一键导入走 baseUrl/apiKey，不发送）
    // 2026-08-23：light/power 独立端点与 Key + 六项价格
    lightBaseUrl?: string
    lightApiKey?: string
    powerBaseUrl?: string
    powerApiKey?: string
    lightInputPrice?: number
    lightCachedPrice?: number
    lightOutputPrice?: number
    powerInputPrice?: number
    powerCachedPrice?: number
    powerOutputPrice?: number
}

export interface FsEntry {
    name: string
    type: 'dir' | 'file'
    size?: number
}

export interface FsTree {
    path: string
    entries: FsEntry[]
    truncated?: boolean
}

/** 目录浏览（2026-08-26 创建配置弹窗「选择文件夹」）：空 path → 盘符列表 */
export interface FsBrowse {
    path: string
    entries: FsEntry[]
}

export interface FsFile {
    path: string
    content: string
    mtime: number
    size: number
}

// ── 工具状态（useLiveTools，WP4-2 §3.11 / WP4-3 §3.7 第 4 条）──

export interface ToolCardState {
    toolName: string
    input: unknown
    output?: string
    status: 'running' | 'done'
}

// ── SSE 事件（WP3 §2.1 事件表，第 7 轮 B2 固化，前后端唯一契约）──

interface SseEventBase {
    command: string
    taskId: string
    seq: number
}

export interface StepStartEvent extends SseEventBase {
    command: 'stepStart'
    stepId: string
}

export interface StreamChunkEvent extends SseEventBase {
    command: 'streamChunk'
    stepId: string
    chunk: string
}

export interface StreamEndEvent extends SseEventBase {
    command: 'streamEnd'
    stepId: string
}

export interface ToolCallStartEvent extends SseEventBase {
    command: 'toolCallStart'
    stepId: string
    callId: string
    toolName: string
    input: string
}

export interface ToolCallResultEvent extends SseEventBase {
    command: 'toolCallResult'
    stepId: string
    callId: string
    toolName: string
    output: string
}

/** V-18：工具参数流式增量（LLM 生成参数时逐片推送，前端逐字渲染动画） */
export interface ToolCallParamEvent extends SseEventBase {
    command: 'toolCallParam'
    stepId: string
    callId: string
    delta: string
}

/** V-18：工具开始执行（toolCallStart 参数流结束后、工具真正执行时推送） */
export interface ToolExecutingEvent extends SseEventBase {
    command: 'toolExecuting'
    stepId: string
    callIds: string[]
    toolNames: string[]
}

export interface UserMessageEvent extends SseEventBase {
    command: 'userMessage'
    stepId: string
    message: string
}

/** 思考过程流（deepseek reasoning_content，仅流式展示、不落盘） */
export interface ThinkingChunkEvent extends SseEventBase {
    command: 'thinkingChunk'
    stepId: string
    chunk: string
}

export interface LlmErrorEvent extends SseEventBase {
    command: 'llmError'
    stepId: string
    code: string
    message: string
    retryable: boolean
    retryCount: number
}

export interface RefreshDataEvent extends SseEventBase {
    command: 'refreshData'
}

/** v2：编排流事件（Sidebar 创建流程时 AI 思考实时展示） */
export interface PlanChunkEvent extends SseEventBase {
    command: 'planChunk'
    text: string
}

export interface PlanDoneEvent extends SseEventBase {
    command: 'planDone'
    ok: boolean
    task_id?: string
    error?: string
}

export type SseEvent =
    | StepStartEvent
    | StreamChunkEvent
    | StreamEndEvent
    | ToolCallStartEvent
    | ToolCallParamEvent
    | ToolExecutingEvent
    | ToolCallResultEvent
    | UserMessageEvent
    | ThinkingChunkEvent
    | LlmErrorEvent
    | RefreshDataEvent
    | PlanChunkEvent
    | PlanDoneEvent

/** 内联标记（WP3 §2.1：chunk 可含 __DC_FULL__ / __DC_RETRY__N/10__DC_RETRY__） */
export const DC_FULL_MARKER = '__DC_FULL__'
export const DC_RETRY_PATTERN = /__DC_RETRY__(\d+)\/10__DC_RETRY__/
