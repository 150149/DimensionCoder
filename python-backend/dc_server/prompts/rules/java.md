# Java 编写规范（参考 java-rule / controller-rule / dto-guide / entity-guide / enum-guide / constants-guide / repository-guide / web-client-guide / java-code-smell-rule）

## 组织与命名

- 使用 Maven 标准目录结构（src/main/java、src/test/java）；包按功能（feature）组织而非按层
- 类/接口 `PascalCase`，方法/变量 `camelCase`，常量 `UPPER_SNAKE_CASE`，包全小写
- 避免缩写，使用有意义且描述性的名称；命名必须清晰准确
- 依赖注入（DI）+ 控制反转（IoC）；分层架构（表现/业务/数据）
- `module-info.java` 显式声明模块依赖，最小化导出包

## 模式与反模式

- Singleton 慎用（优先 DI）；善用 Factory/Strategy/Observer/Template Method/Builder
- 资源管理必须 try-with-resources（流、连接）
- 循环中字符串拼接用 `StringBuilder`（禁止 `+` 反复拼接）
- 优先 Java Collections + 泛型
- 反模式：God Class、Long Method、Shotgun Surgery、Data Clumps、Primitive Obsession
- 大 switch 考虑用多态/Strategy 替代；空 catch 块必须处理（记录/重抛/补救）
- 状态管理：优先不可变对象；Service 设计为无状态

## 错误处理

- 异常只用于异常场景（不用于正常控制流）
- 捕获具体异常类型（禁止裸 catch Exception）
- 日志记录异常需含堆栈与上下文；创建自定义异常表达业务错误
- 绝不吞异常（不记录不处理）

## 性能

- 缓存频繁访问数据（Caffeine/Guava）；连接池复用；惰性初始化；减少对象创建
- 大对象谨慎（碎片化/GC 压力）；渲染大对象时缓冲+压缩
- 依赖树摇树/死代码消除（bundle 优化）

## 安全（Java 侧）

- SQL 注入：参数化查询/prepared statement，禁止拼接用户输入
- XSS：展示前对用户输入编码；CSRF：anti-CSRF token
- 认证/授权正确实现；限流防 DoS；避免反序列化不可信数据；依赖漏洞扫描
- 输入验证白名单优先；正则/长度限制/编码
- 密码强策略 + 加盐哈希（bcrypt/Argon2）；RBAC；OAuth 2.0
- 敏感数据静态/传输加密；日志与错误消息脱敏（masking）
- API 通信 HTTPS/TLS；API Key 认证；限流

## Controller 规范

- 类注解：`@RestController` + `@Validated` + `@RequiredArgsConstructor` + `@RequestMapping`（基础路径）+ `@Slf4j`（建议 `@Valid`），注解按长度排序（长的在下）
- 类名以 Controller 结尾（大驼峰）；方法动词开头（get/post/put/delete），内部接口以 internal 开头
- RESTful：GET 查询 / POST 创建 / PUT 更新 / DELETE 删除 / PATCH 部分更新
- 路径小写、单词连字符 `-` 分隔、内部接口路径以 `/internal` 开头、复数表示资源集合、嵌套路径表示关系
- 参数校验：`@Validated`/`@Pattern`/`@RequestBody`/`@RequestParam`/`@PathVariable`/`@RequestHeader`/`@Valid`
- 权限：内部接口放 controller/internal 包（切面拦截）；用 UserContextHolder 获取用户信息；未授权抛 BusinessException
- 参数非空校验；无效参数抛 BusinessException；用 StringUtils 校验字符串
- 异常：统一 BusinessException + CoreErrorResponse 错误码；异常信息清晰；敏感信息不暴露给前端
- DI：构造器注入 + final 字段（避免字段注入）
- 日志：`@Slf4j`；记录关键业务操作与异常；避免敏感信息；日志脱敏（password 等字段 → `******`）；级别 ERROR/INFO
- 响应：非 200 用 ResponseEntity；不用 JSONObject/JSONArray 作响应体（用结构化类）；空值/集合判空合理；日期时间格式化；响应脱敏
- 注释：类注释含作者/创建日期/功能说明（建议版本/修改历史，Javadoc）；复杂方法注释含功能/参数/返回值/异常
- 质量：单个方法不超过 18 行、复杂度不超过 15；Controller 只含权限校验类代码，不能含业务逻辑，只能调用 service 层

## DTO 规范

- class 必须用 `@Getter/@Setter/@NoArgsConstructor/@AllArgsConstructor` 四注解
- 成员对象必须 private
- 不能使用包装类（不用 Integer/Long，用 int/long，其他类似，包装类能不用就不用）
- 成员对象不能在 DTO 内初始化
- 时间类型使用 Instant 或 LocalDateTime

## Entity 规范（MySQL）

- 四注解：`@Data/@Entity/@NoArgsConstructor/@AllArgsConstructor`；表名 = 类名小写下划线
- 审计信息继承 AuditableEntity（@CreatedBy/@CreatedDate/@LastModifiedBy/@LastModifiedDate/@Version）
- 必须定义主键：UUID 类型（**禁止自增**），`@Id + @GeneratedValue(generator="uuid-generator") + @Column(nullable=false, columnDefinition="char(32)")`
- 普通字段 `@Column(nullable=false, length=30)`；枚举 `@Enumerated(EnumType.STRING)`；超长字符串 `@Lob + @Column(columnDefinition="CLOB")`
- 外键 `@OneToOne(targetEntity=xxx.class)` 或 `@ManyToOne(targetEntity=xxx.class)`，**不能使用 @OneToMany**
- 所有字段 private；成员对象不能在实体内初始化

## Entity 规范（Mongo）

- 五注解：`@Document/@Getter/@Setter/@NoArgsConstructor/@AllArgsConstructor`；@Document 表名小写下划线且与类名转换一致
- **不可以使用 @Field 修饰字段**（保证实体与表结构一致性）；@Id 修饰主键（有且只有一个）；其他字段不需要注解
- 审计字段继承 MongoAuditable

## Enum 规范

- 枚举变量名纯大写加下划线，一行一个
- 需要 value 时：注解只能用 `@Getter/@RequiredArgsConstructor`（不用其他）；value 用 `private final` 修饰 + `@JsonValue`；变量行末加注释；最后一行单独分号
- 序列化到数据库：添加 @ReadingConverter/@WritingConverter（Read 查字符串对应枚举，Write 取 value）
- 多参数时 @JsonValue 放在要序列化的第 n 个成员变量头上（顺序与构造器一致）

## Constants 规范

- 只放跨类多次使用的常量（单类内部使用不放）
- 必须创建 private 且参数为空的构造方法（防实例化）
- 成员必须 `public static final` + 大写加下划线命名

## Repository 规范

- 按业务模块分子包；命名按业务领域（避免通用名）
- 查询方法命名含查询条件：单结果 `findBy` 前缀、集合 `findAllBy`、计数 `countBy`、存在 `existsBy`
- `@Query` 建议同时指定 value/fields 属性，**不能使用 nativeQuery**
- 泛型明确指定 ID 类型（不用 Object）；分页用 `Page<T>`；排序用 `Sort`；投影用 `Projection`
- 参数验证 `@Valid`；日期 `@DateTimeFormat`；数值 `@Min/@Max`；参数命名有意义（startDate/endDate，不用 param1/param2）
- 常用查询/排序字段建索引；投影减少传输；大数据量分页；避免 `$regex` 等性能差操作
- 复杂查询方法加完整 JavaDoc（功能/参数/返回值/异常/示例）

## WebClient 规范

- Client 类建在 bean/client 包下，继承 BaseClient，加 `@Component`
- 访问其他项目服务需内部 Token 类（@Value 配置 + @Cacheable 缓存 token）；请求头 REQUEST_CLIENT 需按项目修改
- 构造器第一行必须调 super；baseUri：调用自己项目 `config.getSelf()`、其他项目 `config.getExternal().getXxxUrl()`
- 自己项目服务用 `inheritHeaderNames(Set.of(X_AUTH_TOKEN))`；其他项目用 `headersSupplier` 加 token
- 请求方法：重试用 `@Retryable(value={期望重试异常}, maxAttempts=2, backoff=@Backoff(delay=2000, multiplier=1))`；响应日志用 `onResponseLog(xxxLogMethod)`（方法写在类顶部，`private static final`
  修饰，只打印提取字段不打印整个响应体）

## Java 代码味道检查（java-code-smell-rule）

1. 单方法 ≤40 行；2. 单方法复杂度 ≤15；3. 单一职责；4. 不为效率牺牲可读性；5. 复杂逻辑拆分；6. 命名清晰；7. 一般不写注释（靠命名理解）；8. 重复 Magic Number/字符串 >3 次才提取常量（没超过不提取）；9. 重复代码 >5 行提取方法；10. 用 Wrapper 封装相近逻辑；11. 能 Stream 的地方必须
   Stream（禁止 for 遍历）；12. 能 Lambda 必须 Lambda；13. 能 Optional 必须 Optional；14. 函数式风格（不修改传入参数）；15. Service 层多个 set 修改同一对象建议构造器/Builder；16. 禁止 `if(!xxx){b}else{a}`；17. Repository
   返回单个对象必须 Optional 包装；18. Controller REST 风格（小写+中杠路径）；19. 构建 Stream 必须用 `StreamUtil.of`（防空指针）；20. 字符串判空必须 `StringUtils.isEmpty/isNotEmpty`（禁止字符串的 isEmpty()）；21.
   返回容器必须 `Collections.emptyList()/emptyMap()`（禁止返回 null）；22. 禁止 null 作为方法参数；23. 可能为空的对象必须 Optional 包装；24. 禁止 `JSONObject.parseObject()`（必须 `JSON.parseObject()`）；25.
   禁止 `@Resource/@Autowired` 字段注入（必须 private final，防循环依赖）；26. List 取第一个元素用 `getFirst()`（不用 `get(0)`）；27. 仅单方法内使用的简单逻辑提取为局部变量（不定义私有方法）；28. Oracle 建表需加权限 SQL（GRANT 增删改查给
   FS_JAVA/FS_JAVA_ROLE、SELECT 给 FS_SUPP、CREATE PUBLIC SYNONYM）
