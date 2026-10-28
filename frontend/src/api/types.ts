export interface StepDef {
    step_id: string
    title: string
    status: string
    required: boolean
    parallel_with?: string[]
    human_attention?: string
    model_tier?: string
    sort_order?: number
    type?: string
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
    auto_approve?: boolean
    mode?: 'flow' | 'single'
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
    mode?: 'flow' | 'single'
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

export interface StepLive {
    seq: number
    streaming: boolean
    roundUid?: string | null
    thinking: string
    text: string
    tools?: Array<{
        callId: string
        name: string
        input: string
        output?: string | null
        status: 'streaming' | 'executing' | 'done'
    }> | null
}

export interface GateApproval {
    options: { option: string; cost?: string; risk?: string; pros?: string }[]
    doc_path?: string
    recommendation?: string
    questions?: string[]
}

export interface GateDoc {
    path: string
    content: string
    truncated: boolean
    error?: string
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
    live?: StepLive | null
    decision?: GateApproval | null
    pendingQuestion?: { question: string; options?: string[] } | null
}


export interface McpServerConfig {
    name: string
    type: 'stdio' | 'http'
    command: string
    args: string[]
    env: Record<string, string>
    url: string
    enabled: boolean
}

export interface McpTestResult {
    ok: boolean
    tools?: string[]
    error?: string
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
    lightModel2?: string
    lightBaseUrl2?: string
    hasLightApiKey2?: boolean
    lightRouteEnabled?: boolean
    lightInputPrice?: number
    lightCachedPrice?: number
    lightOutputPrice?: number
    powerInputPrice?: number
    powerCachedPrice?: number
    powerOutputPrice?: number
    mcpServers?: McpServerConfig[]
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
    lightModel2?: string
    lightBaseUrl2?: string
    lightApiKey2?: string
    lightInputPrice?: number
    lightCachedPrice?: number
    lightOutputPrice?: number
    powerInputPrice?: number
    powerCachedPrice?: number
    powerOutputPrice?: number
    mcpServers?: McpServerConfig[]
}

export interface DailyTokenItem {
    date: string
    prompt: number
    cached: number
    completion: number
    total: number
}

export interface DailyTokenUsage {
    days: number
    items: DailyTokenItem[]
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
    root?: string
    workspace?: string
}

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
    entityId?: string
}

export interface StreamEndEvent extends SseEventBase {
    command: 'streamEnd'
    stepId: string
}

export interface ToolCallStartEvent extends SseEventBase {
    command: 'toolCallStart'
    stepId: string
    callId: string
    entityId?: string
    toolName: string
    input: string
}

export interface ToolCallResultEvent extends SseEventBase {
    command: 'toolCallResult'
    stepId: string
    callId: string
    entityId?: string
    toolName: string
    output: string
}

export interface ToolCallParamEvent extends SseEventBase {
    command: 'toolCallParam'
    stepId: string
    callId: string
    entityId?: string
    delta: string
}

export interface ToolExecutingEvent extends SseEventBase {
    command: 'toolExecuting'
    stepId: string
    callIds: string[]
    entityIds?: string[]
    toolNames: string[]
}

export interface UserMessageEvent extends SseEventBase {
    command: 'userMessage'
    stepId: string
    message: string
    msgSeq?: number
    midRound?: boolean
    entityId?: string
}


export interface ConfirmItem {
    seq: number
    entityId?: string | null
    role: 'system' | 'user' | 'assistant' | 'tool'
    content?: string
    toolName?: string | null
    input?: unknown
    output?: string | null
    roundNum?: number
}

export interface MessagesConfirmedEvent extends SseEventBase {
    command: 'messagesConfirmed'
    stepId: string
    items: ConfirmItem[]
}

export interface TaskUpdatedEvent extends SseEventBase {
    command: 'taskUpdated'
    taskId: string
    status?: string
    pauseLevel?: string | null
    updatedAt?: string
    title?: string
    autoApprove?: boolean
    steps: { stepId: string, status: string, sortOrder?: number, title?: string }[]
}

export interface StepStatsUpdatedEvent extends SseEventBase {
    command: 'stepStatsUpdated'
    stepId: string
    stats: StepStats & { token_completion?: number }
}

export interface GateApprovalReadyEvent extends SseEventBase {
    command: 'gateApprovalReady'
    stepId: string
    approval: GateApproval
}

export interface AskUserEvent extends SseEventBase {
    command: 'askUser'
    stepId: string
    callId: string
    question: string
    options?: string[]
}

export interface StepTokensUpdatedEvent extends SseEventBase {
    command: 'stepTokensUpdated'
    stepId: string
    prompt: number
    cached: number
    completion: number
    contextTokens?: number
}

export interface MessagesResetEvent extends SseEventBase {
    command: 'messagesReset'
    stepId: string
}

export interface StepStatusEvent extends SseEventBase {
    command: 'stepStatus'
    stepId: string
    status?: string
}

export interface RetryProgressEvent extends SseEventBase {
    command: 'retryProgress'
    stepId: string
    retryCount: number
    total: number
}

export interface RoundAbortedEvent extends SseEventBase {
    command: 'roundAborted'
    stepId: string
    roundUid?: string | null
    toolCallIds?: string[]
}

export interface ThinkingChunkEvent extends SseEventBase {
    command: 'thinkingChunk'
    stepId: string
    entityId?: string
    chunk: string
}

export interface LlmErrorEvent extends SseEventBase {
    command: 'llmError'
    stepId: string
    code: string
    message: string
    retryable: boolean
    retryCount: number
    model?: string | null
}

export interface HeartbeatEvent extends SseEventBase {
    command: 'heartbeat'
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
    | PlanChunkEvent
    | PlanDoneEvent
    | MessagesConfirmedEvent
    | TaskUpdatedEvent
    | StepTokensUpdatedEvent
    | MessagesResetEvent
    | StepStatusEvent
    | RetryProgressEvent
    | RoundAbortedEvent
    | StepStatsUpdatedEvent
    | GateApprovalReadyEvent
    | AskUserEvent
    | HeartbeatEvent
