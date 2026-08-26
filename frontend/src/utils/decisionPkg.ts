export interface DecisionPackage {
    options?: { option: string; cost?: string; risk?: string; pros?: string }[]
    recommendation?: string
    questions?: string[]
}

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

export function hasDecisionPackageInMessages(messages: { role?: string; content?: string }[]): boolean {
    for (const m of messages) {
        if (m.role === 'assistant' && parseDecisionPackage(String(m.content ?? ''))) return true
    }
    return false
}
