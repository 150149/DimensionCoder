# Python 后端编写规范

- **代码必须包含类型注解**（函数参数、返回值；变量注解在复杂场景）——项目硬性要求
- 命名：函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`，私有成员 `_` 前缀
- 导包顺序：标准库 → 第三方 → 项目内部，每组按字母序，组间空行
- 异常处理：不吞异常（`except: pass` 必须带理由注释）、异常类型具体化（不裸 `except Exception`）、业务错误用 ValueError/自定义异常语义化，不返回 `-1/None` 表示错误
- 异步正确性：阻塞操作不得直接放 async 函数（需 `asyncio.to_thread`）、`await` 不可遗漏、事件循环不跨线程使用
- 数据库：SQLite 一律参数化（禁止 f-string 拼接 SQL）、连接复用、事务边界清晰、写操作后及时 commit
- 文件操作：`with open(...)` 上下文管理、编码显式（`encoding="utf-8"`）、路径拼接用 `os.path.join`/`pathlib.Path`
- 测试：pytest 命名 `test_*`、断言用具体值不用 `assert True`、fixture 合理复用、测试可独立运行
- 日志：用项目统一 logger，不用 `print` 调试（遗留 print 需删除）
