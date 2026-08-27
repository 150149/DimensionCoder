// ═══════════════════════════════════════════════════════════════
// 决策请求包解析（StepDetail 与 FlowOverview 共用）
// - gate 步骤 AI 输出 markdown 报告 + 尾部 ```json {...} ``` 数据块
//   （options/recommendation/questions），前端据此区分两类 gate 交互：
//   选项类（有 options → 选项按钮 + 自定义输入）vs 审批类（无 → 通过/拒绝）
// - 2026-08-20：报告正文在聊天流完整 markdown 展示（不渲染卡片）；
//   JSON 块仅用于操作区解析，对用户隐藏；why_human 已从格式移除
// ═══════════════════════════════════════════════════════════════

/** AI 决策请求包（gate 步骤尾部 JSON 数据块） */
export interface DecisionPackage {
    options?: { option: string; cost?: string; risk?: string; pros?: string }[]
    recommendation?: string
    questions?: string[]
}

/** 从文本提取 ```json {...} ``` 决策请求包；非 JSON 或缺 options 视为解析失败 */
export function parseDecisionPackage(text: string): DecisionPackage | null {
    if (!text) return null
    const m = text.match(/```json\s*(\{[\s\S]*?\})\s*```/)
    if (!m) return null
    try {
        const obj = JSON.parse(m[1])
        if (obj && typeof obj === 'object' && Array.isArray(obj.options)) return obj as DecisionPackage
        return null
    } catch {
        return null
    }
}

/** 拆分 assistant 文本：叙述部分 + 决策请求包（取首个含 options 的 JSON 块） */
export function extractDecisionPackage(
    content: string,
): { before: string; after: string; pkg: DecisionPackage } | null {
    if (!content) return null
    const m = content.match(/```json\s*(\{[\s\S]*?\})\s*```/)
    if (!m) return null
    let obj: unknown = null
    try {
        obj = JSON.parse(m[1])
    } catch {
        return null
    }
    if (!obj || typeof obj !== 'object' || !Array.isArray((obj as DecisionPackage).options)) return null
    return {
        before: content.slice(0, m.index ?? 0),
        after: content.slice((m.index ?? 0) + m[0].length),
        pkg: obj as DecisionPackage,
    }
}

/** 步骤消息流中是否已有 AI 输出的决策请求包（选项类判定；无 → 审批类） */
export function hasDecisionPackageInMessages(messages: { role?: string; content?: string }[]): boolean {
    for (const m of messages) {
        if (m.role === 'assistant' && parseDecisionPackage(String(m.content ?? ''))) return true
    }
    return false
}
