# TypeScript / React 前端编写规范

- 命名：组件/类型 `PascalCase`，函数/变量 `camelCase`，常量 `UPPER_SNAKE_CASE`，回调前缀 `handle/on`
- 组件职责单一；不写超长组件（>200 行拆分）；重复 UI 提取公共组件
- Hooks：依赖数组完整（`useEffect/useMemo/useCallback` 缺依赖是高频 bug 源）、不在渲染中修改状态
- 类型安全：禁止无理由 `any`（用 unknown + 收窄）、props 用 interface 定义、接口字段与后端模型对齐
- 状态管理：局部状态优先，全局状态（Context/Store）不滥用
- 样式：与项目主题/设计规范一致（按钮用项目 `.btn` 规范、不内联魔法颜色值）
- 测试：核心逻辑/状态流转写 vitest 单测（`renderHook`/`userEvent`），关键交互有断言
- 构建：改完需确认 `npm run build` / `tsc --noEmit` 通过
