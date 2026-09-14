import {memo} from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const REMARK_PLUGINS = [remarkGfm]

export const Markdown = memo(function Markdown({children}: { children: string }) {
    return <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{children}</ReactMarkdown>
})
