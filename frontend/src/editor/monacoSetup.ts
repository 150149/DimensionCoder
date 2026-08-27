// ═══════════════════════════════════════════════════════════════
// Monaco 本地化（SWP4-D / WP4-4 §3.9，P1-6 硬性——内网部署无 CDN）
// - 唯一实现：本地 worker（?worker 由 Vite 打包为独立 chunk）+
//   loader.config({monaco}) 覆盖 @monaco-editor/react 默认 loader
// - 禁止任何从 CDN 加载 monaco 的路径
// - CodeEditor.tsx 首行 import "./monacoSetup"
// ═══════════════════════════════════════════════════════════════

import * as monaco from 'monaco-editor'
import {loader} from '@monaco-editor/react'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker';

(self as unknown as {
  MonacoEnvironment: { getWorker: (_moduleId: string, label: string) => Worker }
}).MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    if (label === 'json') return new jsonWorker()
    if (label === 'css') return new cssWorker()
    if (label === 'html') return new htmlWorker()
    if (label === 'typescript' || label === 'javascript') return new tsWorker()
    return new editorWorker()
  },
}

loader.config({monaco})

// §3.9：「monaco 从 monacoSetup 导入」——CodeEditor 的 Ctrl+S 命令
// 与 worker 配置使用同一实例（ESM 单例，行为等价于直接 import）
export {monaco}
