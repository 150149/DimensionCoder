// ═══════════════════════════════════════════════════════════════
// 测试 setup（WP4-1 §3 文件清单：test-setup.ts）
// 注意：@testing-library/jest-dom v6 主入口依赖 @jest/globals（jest 专用），
// vitest 运行环境无此模块——使用官方 vitest 入口（等效扩展 expect matcher，
// 与本包测试只使用 vitest 内置断言一致；SWP4-B/C/D 的测试亦经本入口生效）。
// ═══════════════════════════════════════════════════════════════

import '@testing-library/jest-dom/vitest'