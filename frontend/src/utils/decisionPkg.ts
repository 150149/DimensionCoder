import type {GateApproval} from '../api/types'

export interface DecisionPackage {
    options?: { option: string; cost?: string; risk?: string; pros?: string }[]
    recommendation?: string
    questions?: string[]
}

export function toGateApproval(pkg: DecisionPackage): GateApproval {
    return {
        options: (pkg.options ?? []).map((o) => ({
            option: o.option, cost: o.cost, risk: o.risk, pros: o.pros,
        })),
        doc_path: '',
        recommendation: pkg.recommendation,
        questions: pkg.questions,
    }
}

export interface AskUserPackage {
    question: string
    options?: string[]
}

const JSON_BLOCK_RE = /```json\s*(\{[\s\S]*?\})\s*```/

function _parseJsonBlock(text: string): Record<string, unknown> | null {
    if (!text) return null
    const m = text.match(JSON_BLOCK_RE)
    if (!m) return null
    try {
        const obj = JSON.parse(m[1])
        return obj && typeof obj === 'object' ? (obj as Record<string, unknown>) : null
    } catch {
        return null
    }
}

export function parseDecisionPackage(text: string): DecisionPackage | null {
    const obj = _parseJsonBlock(text)
    if (obj && Array.isArray((obj as DecisionPackage).options)) return obj as DecisionPackage
    return null
}

export function parseAskUserPackage(text: string): AskUserPackage | null {
    const obj = _parseJsonBlock(text)
    const ask = obj?.ask_user as AskUserPackage | undefined
    if (ask && typeof ask === 'object' && typeof ask.question === 'string' && ask.question.trim()) {
        return {question: ask.question, options: Array.isArray(ask.options) ? ask.options : undefined}
    }
    return null
}

export function extractDecisionPackage(
    content: string,
): { before: string; after: string; pkg: DecisionPackage } | null {
    if (!content) return null
    const m = content.match(JSON_BLOCK_RE)
    if (!m) return null
    const pkg = parseDecisionPackage(content)
    if (!pkg) return null
    return {
        before: content.slice(0, m.index ?? 0),
        after: content.slice((m.index ?? 0) + m[0].length),
        pkg,
    }
}

export function extractAskUserPackage(
    content: string,
): { before: string; after: string; pkg: AskUserPackage } | null {
    if (!content) return null
    const pkg = parseAskUserPackage(content)
    if (!pkg) return null
    const m = content.match(JSON_BLOCK_RE)!
    return {
        before: content.slice(0, m.index ?? 0),
        after: content.slice((m.index ?? 0) + m[0].length),
        pkg,
    }
}

export function hasDecisionPackageInMessages(messages: { role?: string; content?: string }[]): boolean {
    for (const m of messages) {
        if (m.role === 'assistant' && parseDecisionPackage(String(m.content ?? ''))) return true
    }
    return false
}
