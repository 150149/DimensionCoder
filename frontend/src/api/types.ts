export interface StepDef {
    step_id: string
    title: string
    status: string
    required: boolean
    parallel_with?: string[]
    human_attention?: string
    model_tier?: string
    sort_order?: number

    has_decision_pkg?: boolean

    token_prompt?: number
    token_cached?: number
    token_completion?: number

    context_tokens?: number

    requests?: number
    ttft_total_ms?: number
    ttft_samples?: number
    output_duration_ms?: number
    run_duration_ms?: number
}

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

export interface TaskOverview {
    epics: unknown[]
    tasks: TaskSummary[]
    task_count: number
    status_distribution: Record<string, number>
    available_task_types: { type: string; name: string; description?: string }[]
}

export interface CreateTaskResult {
    task_id: string
    task_type: string
    title: string
    steps?: StepDef[]
}

export interface TaskDetail {
    task: TaskSummary
    artifacts: unknown[]
    monitor_conversations: Record<string, Message[]>
    step_messages: Record<string, Message[]>
    recent_events: unknown[]
}

export interface StepTokens {
    token_prompt: number
    token_cached: number
    token_completion: number
}

export interface StepStats {
    run_duration_ms: number
    output_duration_ms: number
    ttft_total_ms: number
    ttft_samples: number
    requests: number
}

export interface MonitorConversations {
    task_id: string
    monitor_conversations: Record<string, Message[]>

    step_tokens?: Record<string, StepTokens>

    monitor_steps?: Record<string, string>

    monitor_order?: Record<string, number>

    monitor_anchors?: Record<string, string>

    step_stats?: Record<string, StepStats>
}

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

export interface StepPrep {
    system_message: string
    system_prompt: string
    step_context: string
    temp_dir: string
    model_tier: string
    step_title: string
    step_id: string
}

export interface StepData {
    stepId: string
    taskId: string
    prep: StepPrep
    conversation: Message[]
    messages: Message[]
    max_seq: number
    step: StepDef

    total?: number
    truncated?: boolean
}

export interface ConfigView {
    baseUrl: string
    lightModel: string
    powerModel: string
    projectRoot: string
    port: number
    host: string
    hasApiKey: boolean
    contextWindow: number
    channelType?: string
    hasChannel?: boolean

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
    llmChannel?: string

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

export interface FsFile {
    path: string
    content: string
    mtime: number
    size: number
}

export interface ToolCardState {
    toolName: string
    input: unknown
    output?: string
    status: 'running' | 'done'
}

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

export interface ToolCallParamEvent extends SseEventBase {
    command: 'toolCallParam'
    stepId: string
    callId: string
    delta: string
}

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

export const DC_FULL_MARKER = '__DC_FULL__'
export const DC_RETRY_PATTERN = /__DC_RETRY__(\d+)\/10__DC_RETRY__/
