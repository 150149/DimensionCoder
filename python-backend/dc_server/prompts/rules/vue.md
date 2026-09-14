# Vue 编写规范（参考 vue-code-smell-rule）

## 代码可读性要求

- 单个方法内的代码行数不能超过 40 行
- 单个方法复杂度不能超过 15
- 单个方法/单个类只做一件事（单一职责）
- 不能为效率而牺牲可读性，可以牺牲部分效率来提升可读性
- 复杂逻辑拆分成多个简单逻辑，巧用设计模式
- 命名清晰，能准确表达变量/方法/类的作用
- 一般不写注释，尽量只靠命名理解代码（注释密度与周围一致）
- 超过 3 次以上重复的 Magic Number 或字符串需要提取成常量（没超过的不要提取）
- 重复代码不能超过 5 行，超过需提取成方法
- 使用 Wrapper 将相近但不相同的代码逻辑封装
- 保持函数式编程风格，不允许修改传入的参数
- 禁止 `if(!xxx) {b} else {a}` 写法，必须用 `if(xxx) {a} else {b}` 正向顺序
- HTML 标签名 `<xxx-xxx>` 必须中杠加小写，不能出现大写字母
- HTML 标签 class 属性值必须中杠加小写（`abc-def`）
- Vue 组件 template 第一层不能包含 ElementUI 容器组件（如 `<el-dialog>`），便于容器内容复用
- 禁止将样式直接写在 HTML 标签中（style 属性），必须放 css 块
- I18n 翻译 Key 必须三层，按「业务.类型.英文意思」命名（如 `product.button.confirm`）
- HTML 代码块中不能出现中文，全部用 Vue-I18n 将中英文单独放一个文件
- HTML 标签中禁止 `<xx :visible="true">` 写法，应直接写 `<xxx visible>`
- 字符串拼接禁止 `+` 写法，只能用 `` `aaa${bbb}ccc` `` 模板字符串
- 禁止 `xxx === undefined` / `xxx === null` 写法，只能用 `_.isNil()` 判空
- 禁止 `.then()` 异步写法，只能用 `async/await`
- 禁止 `var`，只能用 `const/let`
- 不要出现不必要的中间变量（如 `const res = await getX(); store.a = res` 的 `res` 就是多余的）
- 连续二次以上 lodash 操作后需创建中间变量便于理解（如 `const filtered = _.filter(_.map(x, a), a)`）
- 禁止 `==`，应换成 `===` 或 lodash 判断函数（`_.isNil()/_ .isEmpty()/_ .isNumber()`）
- 不需要在换行时添加 `;`
- 禁止无用括号：`(xxx) => {}` 应写成 `xxx => {}`
- 定义方法时应清晰表明参数而不是只写一个 `param`（如 `param => param.a + param.b` 应写成 `(a, b) => a + b`）
- 方法只有一个操作时省略 return（`(a, b) => { return a+b }` 应写成 `(a, b) => a+b`）
- 代码中不应出现 `console.log`
- 禁止原生 `for(let i=0;i<xxx;i++)`，应使用 `_.forEach` 或 `_.map`
- 使用 `v-for` 时必须添加 `:key`
- class 名称必须取业务相关，能描述组件用途（如 `report-table`），不用 `flex`/`table-title` 通用写法
- 初始化非常大的数据对象（如 echarts 的 chartOptions）应单独抽取到 js 文件
- 禁止 `const map = {}; _.forEach(data, x => { map[x] = x })` 写法，应使用 `_.map()`
- HTML 标签 `<xxx-xxx :param-xxx>` 的 prop 名必须中杠加小写
- 方法不宜过小：小于等于 3 行的方法不应出现
- 复杂组件必须拆分成多个小组件，不能一个组件 1 千行
- 样式表使用 scss 嵌套写法，便于根据嵌套关系定位组件

## 代码健壮性要求

- 能使用 lodash 的必须使用（`_.forEach()/_ .filter()/_ .some()`），避免原生方法出现 undefined 异常
- 判断字符串是否为空必须使用 `_.isEmpty()`
- 返回数组时，必须用 `[]` 返回空容器，禁止返回 null/undefined
- 禁止使用 null/undefined 作为方法参数传入

## 组件与样式规则

- 组件复用优先：禁止手写日历/弹窗/下拉选择，应使用组件库对应组件（`<a-calendar>`/`<a-modal>`/`<a-popover>`/`<a-dropdown>`/`<a-select>`）并通过 slot/props 自定义，禁止从零实现
- 浮层/弹窗必须相对触发元素定位（父容器 `position: relative` + 弹窗 `position: absolute; top: 100%; right: 0`），禁止 viewport 固定坐标（如 `top: 200px; right: 60px`）
- 优先使用 `<a-popover>`/`<a-dropdown>` 替代手写 overlay + popup 组合
- 主题色：项目 `token.colorPrimary` 若为红色系（基于 `#cc3333`）则**不是蓝色**；需要蓝色时必须硬编码 `#1890ff`，禁止用 `token.colorPrimary` 当蓝色使用；`token.colorPrimaryBg` 用于选中行/周背景高亮；`token.colorError` 用于错误提示（与
  colorPrimary 视觉相似但不可互换）
