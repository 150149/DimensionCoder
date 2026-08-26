# 测试编写规范（参考 junit-rule / mockito-rule / spring-service-test-guide / spring-controller-test-guide / stream-bridge-test-guide / rabbitmq-contract-test-guide）

## 通用测试原则（JUnit）

- 测试目录镜像生产代码结构；测试类命名 `*Test.java`/`*Tests.java`；测试方法命名 `given条件_when动作_then期望结果`
- AAA 模式（Arrange-Act-Assert）；@BeforeEach/@AfterEach 准备/清理
- 测试独立隔离（不共享状态）；快执行（毫秒级）；不测实现细节（测行为）；不忽略失败
- 边界条件/空集合/无效输入全覆盖；参数化测试；覆盖率目标 ~80%
- 断言库 AssertJ/Hamcrest 更可读；测试资源放 src/test/resources
- 反模式：硬编码值、复杂测试逻辑、过度 mock、慢测试、忽略失败

## 单元测试（Service 层）

- 测试类必须 `@ExtendWith(MockitoExtension.class)`，**不应使用 @SpringBootTest**
- 方法命名：`void 应该返回什么_当做什么的时候_给了什么的时候()`（下划线后小写开头，中间驼峰）
- 三大步骤用 `// before` / `// when` / `// then` 注释分隔（斜杠后有空格）
- before 准备各种 DTO；when 配置 `when(bean.method(入参)).thenReturn(返回值)`（用 any()/anyString() 限定入参）；then 执行被测代码 + 验证
- 验证：`Assertions.assertAll(...)` 分组；`Mockito.verify(mock对象, times(N)).method(参数)`；`assertEquals(期望,实际)`；`assertThrows(异常.class, () -> 方法(), 消息)`
- 用例设计：白盒测试走完所有逻辑分支；必须包含正常 Case；覆盖所有 if/else 路径
- 私有方法测试用反射 `invokePrivateMethod`（BaseMockTest 提供）；Mock 静态方法用 `MockedStatic`（try-with-resource）；验证调用参数用 `ArgumentCaptor`；Mock 构造器用 `MockedConstruction`
- 测试代码允许重复，**禁止抽取 helper 方法**（每个测试方法自包含数据准备与断言，独立可读）

## Controller 集成测试

- 目的：通过 Controller 暴露的 API 测试背后 Service 代码（不是测 Controller 本身）——通过修改 API 入参走完 Service 全部分支；不能直接调用 Service 层方法
- 注解：`@WireMockTest/@AutoConfigureMockMvc/@AutoConfigureWireMock(port=8880)/@SpringBootTest(classes=FsXxxApplication.class)`（无 stubFor
  时用 `@AutoConfigureMockMvc/@SpringBootTest/@DirtiesContext(BEFORE_CLASS)` 三注解）
- 方法命名：`void should返回什么_when什么的时候_given什么的时候()`（不能缺 should/when）
- Repository 注入用 @Autowired + @BeforeEach cleanData() 清理
- 入参/出参大时外置 json（mockData 入参、expectData 出参，放 /test/resources），微差用 replaceAll 替代；简单 DTO 直接创建
- when：WireMock `stubFor(post/get(urlMatching(".*/xxx")))` + willReturn（header/status/body）模拟其他服务 API
- then：`mockMvc.perform(...)` 内用 `andExpect(status().isOk())` / `jsonPath("$.路径").value(期望)` / `content().json(期望json)` / `result -> verify(mock, times(N)).method(参数)`；验证只能在
  perform 后的 andExpect 内
- 必须验证：实体最终状态（repository.findAll() 后逐字段 assertEquals）；StreamBridge 消息（binding name/调用次数/payload 关键字段）；反向排除（`verify(streamBridge, never()).send(eq("other-binding"), any())`）
- 编写测试前先列出所有测试用例（每个用例覆盖的分支、准备的数据、预期的副作用）让用户确认后再编码——审查时检查测试是否覆盖了设计确认的用例
- 不能修改非测试代码、不能添加不存在的 API 接口

## StreamBridge 测试要点

- 验证发送的 binding name 正确、调用次数正确（`Mockito.verify(streamBridge, times(N)).send(eq("binding-name"), captor.capture())`）
- 验证 payload 内容包含关键字段；验证不该被调用的 binding 未被调用（never()）
- Listener（消费者）测试：@Autowired `Consumer<Message<String>>` 注入 listener bean，直接 `listenerBean.accept(new GenericMessage<>(JSON.toJSONString(dto)))` 发消息，`SpringUtils.sleep(1000)`
  等异步完成，再验证数据库最终状态与 StreamBridge 消息

## RabbitMQ 契约测试（7 层防御体系）

- L1 基础设施：exchange 硬编码、publish 方法签名；L2 序列化格式：FastJson/Jackson 时间格式基线；L3 HTTP 透传：透传消息字段结构不变；L4 DTO 快照：DTO JSON 字段树、枚举值集、BigDecimal 格式；L5 发送点：routing key + payload + headers；L6
  消费端：new String(body) 编码；L7 端到端
- 必须使用 JsonContractAssert：`assertFieldTree`（字段名+类型树）/`assertTimeFormat`/`assertNullFields`/`assertEnumValues`/`assertBigDecimalFormat`/`assertHeaderValue`/`assertEncoding`
- 时间格式基线：FastJson 2.x Instant → ISO-8601（`\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*`）；FastJson 2.x + Date/Timestamp → epoch millis（`\d{10,}`）；FastJson 2.x **不识别** Jackson
  @JsonFormat/@JsonValue/@JsonIgnore；Jackson bare ObjectMapper → {nano, epochSecond}；Jackson + JavaTimeModule(WRITE_DATES_AS_TIMESTAMPS=true) → "epochSec.nanos"
- 枚举序列化基线：FastJson 2.x 默认 `enum.name()`（不用 toString()）；Jackson 默认 `enum.name()`
- 发送点测试：@Mock RabbitPublisher + @InjectMocks Service，ArgumentCaptor 捕获 topic + payload，断言 routing key 与 payload 结构
- @Value 字段用反射设置；私有发送方法用反射调用（setAccessible(true)）
- Maven 模块间不共享 test 类，每个模块需复制 JsonContractAssert 与 RabbitTestConstant

## Mockito 用法要点

- @Mock 创建 mock；@InjectMocks 注入；`when(...).thenReturn(...)` 打桩；`verify(...)` 验证调用；ArgumentCaptor 捕获参数
- `doReturn(...).when(...)` 用于 void 方法/多次调用；`thenAnswer(...)` 自定义逻辑；`thenThrow(...)` 模拟异常
- 反模式：过度 mock（能直接实例化的值对象不用 mock）、测实现细节、频繁 reset()、滥用 spy、长链 when()
- Mock 静态方法用 `MockedStatic`（用完 close 或 try-with-resource）；Mock 构造器用 `MockedConstruction`（非 PowerMock）
- 参数匹配器混用注意：要么全部精确值、要么全部 matcher
- 每次测试创建全新 mock（@BeforeEach），避免静态/共享可变状态
