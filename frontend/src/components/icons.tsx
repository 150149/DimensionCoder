// ═══════════════════════════════════════════════════════════════
// icons.tsx — 自建 SVG 图标集（视觉产品化改版 v2）
// - 零 npm 依赖：内联 SVG，stroke=currentColor，颜色跟随 CSS color
// - 断网可用（P1-6）：随 bundle 打包，无外部资源
// - 用法：<Icon name="play" size={14} />；可传标准 svg 属性（className 等）
// - 风格：Linear 系 24x24 线性图标（strokeWidth 2，圆角端点）
// ═══════════════════════════════════════════════════════════════

import type {CSSProperties, ReactNode, SVGProps} from 'react'

export type IconName =
    | 'play'
    | 'pause'
    | 'settings'
    | 'refresh'
    | 'folder'
    | 'file'
    | 'wrench'
    | 'alert'
    | 'eye'
    | 'send'
    | 'zap'
    | 'book'
    | 'check'
    | 'list'
    | 'compass'
    | 'trash'
    | 'close'
    | 'edit'
    | 'search'
    | 'compress'
    | 'stop'
    | 'inject'
    | 'ban'
    | 'chevronDown'
    | 'chevronRight'
    | 'arrowLeft'
    | 'arrowRight'
    | 'rocket'
    | 'shield'
    | 'message'
    | 'fileText'
    | 'bookmark'
    | 'error'
    | 'lock'
    | 'unlock'

const PATHS: Record<IconName, ReactNode> = {
    play: <path d="M7 4.5v15l12-7.5-12-7.5z" fill="currentColor" stroke="none"/>,
    pause: (
        <>
            <rect x="6" y="4" width="4" height="16" rx="1.2" fill="currentColor" stroke="none"/>
            <rect x="14" y="4" width="4" height="16" rx="1.2" fill="currentColor" stroke="none"/>
        </>
    ),
    settings: (
        <>
            <line x1="4" y1="21" x2="4" y2="14"/>
            <line x1="4" y1="10" x2="4" y2="3"/>
            <line x1="12" y1="21" x2="12" y2="12"/>
            <line x1="12" y1="8" x2="12" y2="3"/>
            <line x1="20" y1="21" x2="20" y2="16"/>
            <line x1="20" y1="12" x2="20" y2="3"/>
            <line x1="1" y1="14" x2="7" y2="14"/>
            <line x1="9" y1="8" x2="15" y2="8"/>
            <line x1="17" y1="16" x2="23" y2="16"/>
        </>
    ),
    refresh: (
        <>
            <path d="M21 12a9 9 0 1 1-2.64-6.36"/>
            <polyline points="21 3 21 9 15 9"/>
        </>
    ),
    folder: <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>,
    file: (
        <>
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
        </>
    ),
    wrench: (
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
    ),
    alert: (
        <>
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
            <line x1="12" y1="9" x2="12" y2="13"/>
            <line x1="12" y1="17" x2="12.01" y2="17"/>
        </>
    ),
    eye: (
        <>
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
        </>
    ),
    send: (
        <>
            <line x1="22" y1="2" x2="11" y2="13"/>
            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </>
    ),
    zap: <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>,
    book: (
        <>
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </>
    ),
    check: <polyline points="20 6 9 17 4 12"/>,
    list: (
        <>
            <line x1="8" y1="6" x2="21" y2="6"/>
            <line x1="8" y1="12" x2="21" y2="12"/>
            <line x1="8" y1="18" x2="21" y2="18"/>
            <line x1="3" y1="6" x2="3.01" y2="6"/>
            <line x1="3" y1="12" x2="3.01" y2="12"/>
            <line x1="3" y1="18" x2="3.01" y2="18"/>
        </>
    ),
    compass: (
        <>
            <circle cx="12" cy="12" r="10"/>
            <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>
        </>
    ),
    trash: (
        <>
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            <line x1="10" y1="11" x2="10" y2="17"/>
            <line x1="14" y1="11" x2="14" y2="17"/>
        </>
    ),
    close: (
        <>
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
        </>
    ),
    edit: <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>,
    search: (
        <>
            <circle cx="11" cy="11" r="8"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </>
    ),
    compress: (
        <>
            <polyline points="21 8 21 21 3 21 3 8"/>
            <rect x="1" y="3" width="22" height="5"/>
            <line x1="10" y1="12" x2="14" y2="12"/>
        </>
    ),
    stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
    inject: <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>,
    ban: (
        <>
            <circle cx="12" cy="12" r="10"/>
            <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
        </>
    ),
    chevronDown: <polyline points="6 9 12 15 18 9"/>,
    chevronRight: <polyline points="9 18 15 12 9 6"/>,
    arrowLeft: (
        <>
            <line x1="19" y1="12" x2="5" y2="12"/>
            <polyline points="12 19 5 12 12 5"/>
        </>
    ),
    arrowRight: (
        <>
            <line x1="5" y1="12" x2="19" y2="12"/>
            <polyline points="12 5 19 12 12 19"/>
        </>
    ),
    rocket: (
        <>
            <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/>
            <path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>
            <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/>
            <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>
        </>
    ),
    shield: <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>,
    message: <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>,
    fileText: (
        <>
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
            <line x1="16" y1="13" x2="8" y2="13"/>
            <line x1="16" y1="17" x2="8" y2="17"/>
        </>
    ),
    bookmark: <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>,
    error: (
        <>
            <circle cx="12" cy="12" r="10"/>
            <line x1="15" y1="9" x2="9" y2="15"/>
            <line x1="9" y1="9" x2="15" y2="15"/>
        </>
    ),
    lock: (
        <>
            <rect x="4" y="11" width="16" height="10" rx="2"/>
            <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
        </>
    ),
    unlock: (
        <>
            <rect x="4" y="11" width="16" height="10" rx="2"/>
            <path d="M8 11V7a4 4 0 0 1 7.6-1.6"/>
        </>
    ),
}

interface IconProps extends SVGProps<SVGSVGElement> {
    name: IconName
    size?: number
    /** 图标与相邻文本的间距（默认 5px） */
    gap?: number
    style?: CSSProperties
}

export function Icon({name, size = 14, gap = 5, style, ...rest}: IconProps) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            style={{flexShrink: 0, marginRight: gap, verticalAlign: '-2px', ...style}}
            {...rest}
        >
            {PATHS[name]}
        </svg>
    )
}

/** 任务类型 → 图标名（Sidebar typeIcon 的 SVG 版） */
export const TYPE_ICON_NAMES: Record<string, IconName> = {
    'dev-full-flow': 'rocket',
    'small-change': 'wrench',
    'incident-check': 'search',
    'incident-fix': 'shield',
    'pd-question': 'message',
    'code-review': 'eye',
    'doc-update': 'fileText',
    custom: 'edit',
}

export function typeIconName(type: string): IconName {
    return TYPE_ICON_NAMES[type] ?? 'bookmark'
}

/** 工具名 → 图标名（ToolCard 的 emoji 映射表 SVG 版） */
export const TOOL_ICON_NAMES: Record<string, IconName> = {
    list_dir: 'folder',
    read_file: 'file',
    write_file: 'edit',
    edit_file: 'wrench',
    search_code: 'search',
    run_cmd: 'zap',
    read_doc: 'book',
    step_done: 'check',
    list_steps: 'list',
    adjust_flow: 'compass',
    sim: 'play',
}

export function toolIconName(name: string): IconName {
    // 兼容带/不带 dcflow_ 前缀（真实 DB tool_name 带前缀，映射表为无前缀键）
    const key = name.startsWith('dcflow_') ? name.slice(7) : name
    return TOOL_ICON_NAMES[key] ?? 'wrench'
}
