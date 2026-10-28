export interface CompressResult {
    status: string
    reason?: string
    rounds?: number
    tokens_before?: number | null
    tokens_after?: number | null
}

export function compressResultText(r: CompressResult): string {
    switch (r.status) {
        case 'ok':
            return `已压缩 ${r.rounds ?? 0} 轮早期工具调用（仅影响 AI 上下文，历史消息保留）`
        case 'queued':
            return '已请求压缩，将在下一轮生效'
        case 'exhausted':
            return '已压到保留最近 20 轮的下限，仍超 400K'
        case 'skipped':
            return r.reason === 'no_messages' ? '暂无对话可压缩' : '当前上下文在预算内，无需压缩'
        default:
            return '压缩完成'
    }
}
