# 预设流程模板

Monitor 创建流程（初始编排）时的参考模板。选择与任务最匹配的模板直接改编，或参考其"分析→设计→执行→验证"结构设计新流程。

每个步骤需给出：step_id、title、required（0/1）、parallel_with（并行步骤 id 或空）、human_attention（none/notify/review/gate）、model_tier（light/power）、type（步骤类型，见下方清单，缺省
executor）、description（给步骤执行者的详细指令——必须写清目标、产出、验收标准）。

## 可添加步骤类型（仅这 6 种）

- **executor**（默认）：正常执行步骤——按 description 完成实现/验证等工作
- **gate**：人工决策点——须同时设 human_attention=gate（parallel_with 必须为空），系统会暂停等人类审批
- **plan**：计划制定——先分析需求与代码现状，产出实施计划文档，供后续执行步骤照做
- **code_review**：代码审查——审查指定代码/产出，输出审查报告（问题清单/严重性/修复建议）
- **reverse**：逆向专家——CTF/二进制/反调试/脱壳类任务专用，可调用 dcflow_sim 模拟器与逆向专用工具（反编译/常量提取/字节搜索/Z3 求解）
- **researcher**：研究员——只读调研专家，收集并验证任务相关的仓库、需求、文档、依赖或外部事实，返回结构化证据报告（不规划、不实现、不审查）。适用场景：需求理解阶段需要深度验证、跨模块调研、依赖关系梳理、外部事实核查。工具集只读（浏览/读取/搜索/知识库），不修改任何文件

> 流程收尾（核查与面向用户的报告）由系统在所有步骤完成后自动处理，不属于可添加类型，不要创建收尾类步骤。

## 模板 1：dev-full-flow（完整研发流程）

适用场景：中等复杂度新功能或 bug 修复，需要完整的需求分析、方案设计、实现与审查。

步骤设计（6 步，含 1 个 gate）：

- step-1「理解需求与用户旅程」required=1 model_tier=power human_attention=none description: 阅读任务描述并调用 dcflow_read_doc 查阅相关业务文档，明确需求范围。**以表格形式列出用户旅程**——一个用户旅程是由多个（当前状态 + 用户操作 +
  期望结果）组合序列构成的，逐条列出全部旅程，覆盖所有需求场景。产出需求理解摘要：用户期望什么、涉及哪些模块、有哪些模糊点需要后续澄清。随后调用 dcflow_search_code/dcflow_read_file 梳理现有逻辑，**表格列出与当前实现不一致的地方**，逐条标注：与用户新预期的差异点、根因。
- step-2「方案设计」required=1 model_tier=power human_attention=none type=plan description: 基于 step-1 的用户旅程与差异分析设计修改方案：**对每个有差异的用户旅程逐条列出**根因、差异点、实现复杂度、验证方式、修改代码的位置与大致逻辑（每条差异点一行）；**
  并为每个用户旅程设计测试方案**（正向/边界/异常）。提出 **2-3 个整体修改方案**，对比各方案的优缺点、影响范围、风险，给出推荐方案及理由。产出方案文档到步骤产物（file_path 传完整相对路径：以 `.dc_tmp/` 开头、包含「任务ID/步骤ID/artifacts/」目录，如
  step2_refactor_plan.md；裸相对路径会被拒绝——系统特判解析到服务端工作区，目录自动创建，无需手动建），供 step-3 展示与后续步骤照做。
- step-3「方案审批」required=1 model_tier=power human_attention=gate type=gate description: **仅将 step-2 产出的方案文档读取出来**（dcflow_read_file）整理成**方案审批报告**
  展示给用户审批——完整呈现方案文档内容（根因条目/测试方案/决策点逐条保留，禁止摘要化压缩），**不重复设计方案、不修改方案内容**。此为 Gate 步骤：等待人类在步骤详情页确认后进入实现。
- step-4「编写测试代码」required=1 model_tier=power human_attention=none description: 根据 step-2 测试方案编写可运行的测试代码（每个用户旅程对应测试用例），覆盖正向、边界、异常场景，确保关键路径有测试，测试代码先行落地。
- step-5「修改代码与验证」required=1 model_tier=power human_attention=none description: 按审批通过的方案修改代码（调用 dcflow_write_file/dcflow_edit_file），修改后调用 dcflow_search_code
  检查引用完整性；运行编译与测试（dcflow_run_cmd）验证，失败则修复后重新验证，直到通过或明确记录阻塞原因。
- cr-r1「代码审查」required=1 model_tier=power human_attention=none type=code_review description: 独立审查代码变更：编译检查 → 代码风格 → 业务逻辑（对照 step-1 用户旅程逐条核对）→ 架构安全。输出审批结果给流程编排者判断是否可以结束流程还是添加新步骤修改代码。

特殊情况调度：

- 当step1结束之后，发现有需求澄清，则在step2前面插入 Gate 步骤让用户澄清需求，然后再在澄清需求步骤后面添加一个补充理解需求与用户旅程的步骤，直到澄清完成全部需求
- 当step3用户拒绝了方案并给出原因后，需要在step3后面插入新的方案设计步骤，然后在新方案设计步骤之后再插入一个方案审批步骤，直到用户批准方案
- 当cr-r1结束之后，发现审查不通过，则需要在cr-r1之后插入新的修改代码与验证步骤，然后在新的修改代码与验证步骤之后再插入一个代码审查步骤，直到审查通过

## 模板 2：small-change（小型变更）

适用场景：小范围代码修改（修 bug、小优化），跳过完整分析阶段——定位与方案设计一步完成，无测试先行。

步骤设计（4 步，含 1 个方案审批 gate）：

- step-1「问题定位与方案设计」required=1 model_tier=power human_attention=none type=plan description: 复现问题，**以表格列出与预期行为的差异点**（差异点/根因/涉及模块逐条），**直接给出修改方案**
  ：对每个差异点逐条列出修改内容、影响范围、验证方式——一步兼顾定位与方案设计。产出方案文档到步骤产物（file_path 传完整相对路径：以 `.dc_tmp/` 开头、包含「任务ID/步骤ID/artifacts/」目录，如 step1_fix_plan.md；裸相对路径会被拒绝——系统特判解析到服务端工作区，目录自动创建，无需手动建），供
  step-2 展示与后续步骤照做。
- step-2「方案审批」required=1 model_tier=power human_attention=gate type=gate description: **仅将 step-1 产出的方案文档读取出来**（dcflow_read_file）整理成**方案审批报告**
  展示给用户审批——完整呈现方案文档内容（差异点/修改内容/验证方式逐条保留，禁止摘要化压缩），**不重复设计方案、不修改方案内容**。此为 Gate 步骤：等待人类审批通过后进入实现。
- step-3「代码修改与验证」required=1 model_tier=power human_attention=none description: 按批准方案修改代码（调用 dcflow_write_file/dcflow_edit_file，修改项目文件用绝对路径），运行编译与相关测试确认无回归，失败则修复后重新验证。
- cr-r1「轻量审查」required=1 model_tier=power human_attention=none type=code_review description: 独立审查代码变更：风格/逻辑/边界，对照 step-1 差异点逐条核对，输出审查意见（问题清单/严重性/修复建议）供流程编排者决策。

特殊情况调度：

- 当step2用户拒绝了方案并给出原因后，需要在step2后面插入新的方案设计步骤，然后在新方案设计步骤之后再插入一个方案审批步骤，直到用户批准方案
- 当cr-r1审查发现问题后，需要在cr-r1之后插入新的代码修改与验证步骤，然后在新的代码修改与验证步骤之后再插入一个轻量审查步骤，直到审查通过

## 模板 3：incident-check（事故调查）

适用场景：线上问题调查，纯分析不改代码。调查是一段连续推理，拆步无人工交互点；收尾报告由系统自动生成，不设专门报告步骤。

步骤设计（1 步，无 gate）：

- step-1「事故调查」required=1 model_tier=power human_attention=none description: 分析问题现象与日志/堆栈，判定异常路由（有堆栈定位/仅框架堆栈/无日志），阅读相关业务文档与代码，**输出根因结论 + 证据链（表格：根因候选/证据/排除理由逐条）+ 影响范围 + 修复建议**
  （建议明确到模块与方向，供 incident-fix 承接）。

衔接说明：修复由后续 incident-fix 任务承接。

## 模板 4：incident-fix（紧急修复）

适用场景：线上问题紧急修复，快速定位→方案审批→修复→验证→复盘。无需求分析与测试先行；用户审批统一在方案审批（改前），结果验证不审批。

步骤设计（4 步，含 1 个方案审批 gate）：

- step-1「根因定位与修复方案」required=1 model_tier=power human_attention=none type=plan description: 复现验证问题，**以表格列出根因证据**（证据/根因/影响逐条），**直接给出修复方案**
  ：修复内容、影响范围、验证方式——一步兼顾定位与方案设计。产出修复方案文档到步骤产物（file_path 传完整相对路径：以 `.dc_tmp/` 开头、包含「任务ID/步骤ID/artifacts/」目录，如 step1_fix_plan.md；裸相对路径会被拒绝——系统特判解析到服务端工作区，目录自动创建，无需手动建），供 step-2 展示。
- step-2「方案审批」required=1 model_tier=power human_attention=gate type=gate description: **仅将 step-1 产出的修复方案文档读取出来**（dcflow_read_file）整理成**方案审批报告**
  展示给用户审批——完整呈现方案文档内容（根因证据/修复内容/影响范围逐条保留，禁止摘要化压缩），**不重复设计方案、不修改方案内容**。此为 Gate 步骤：等待人类审批通过后进入修复（紧急场景人工快速确认）。
- step-3「代码修复与快速验证」required=1 model_tier=power human_attention=none description: 按批准方案修复代码（调用 dcflow_write_file/dcflow_edit_file，修改项目文件用绝对路径），运行编译与回归测试确认修复生效。
- step-4「事后复盘」required=0 model_tier=power human_attention=none description: 输出复盘：问题时间线、影响范围、改进措施、预防措施。

特殊情况调度：

- 当step2用户拒绝了方案并给出原因后，需要在step2后面插入新的方案设计步骤，然后在新方案设计步骤之后再插入一个方案审批步骤，直到用户批准方案
- 当step3验证不过时，需要在step3之后插入新的修复与验证步骤，直到验证通过

## 模板 5：pd-question（PD 业务调查）

适用场景：面向 Product Designer 的业务逻辑调查，纯调查不修改代码，零技术语言输出。收尾报告由系统自动生成，不设专门报告步骤。

步骤设计（1 步，无 gate）：

- step-1「业务整理」required=1 model_tier=power human_attention=none description: 阅读业务文档与相关代码，**整理业务逻辑（表格：场景流程/状态流转/规则与限制/边界情况/角色差异/功能关联）**，内部技术细节翻译为业务语言（零技术语言输出）。

## 模板 6：code-review（独立代码审查）

适用场景：独立的代码审查任务。多轮同款独立完整检查——每轮不参考前轮结论独立复查（避免锚定），重复检查保证最终准确；问题清单由系统 final-report 汇总呈现。

步骤设计（2 轮，全 human_attention=none）：

- cr-r1「独立审查（第 1 轮）」required=1 model_tier=power human_attention=none type=code_review description: 独立完整检查：编译检查 → 代码风格与规范 → 业务逻辑与测试覆盖（对照需求逐条核对）→ 架构安全，输出 [R1-N] 问题清单（文件:行 — 问题 →
  修复方式）。
- cr-r2「独立审查（第 2 轮）」required=1 model_tier=power human_attention=none type=code_review description: 同款描述独立复查（不参考第 1 轮结论），输出 [R2-N] 问题清单；两轮问题清单由系统 final-report 汇总呈现。

## 模板 7：doc-update（文档更新

适用场景：业务知识文档创建/更新。先人工审批变更方案再修改（不是改完再审查）。

步骤设计（3 步，含 1 个方案审批 gate）：

- step-1「文档变更方案」required=1 model_tier=power human_attention=none type=plan description: 明确文档目标与涉及模块，阅读现有文档与相关代码，**以表格列出变更内容**（变更点/内容/涉及文件逐条），给出变更方案。产出变更方案文档到步骤产物（file_path
  传完整相对路径：以 `.dc_tmp/` 开头、包含「任务ID/步骤ID/artifacts/」目录，如 step1_doc_plan.md；裸相对路径会被拒绝——系统特判解析到服务端工作区，目录自动创建，无需手动建），供 step-2 展示。
- step-2「方案审批」required=1 model_tier=power human_attention=gate type=gate description: **仅将 step-1 产出的变更方案文档读取出来**（dcflow_read_file）整理成**方案审批报告**
  展示给用户审批——完整呈现变更内容（变更点/涉及文件逐条保留，禁止摘要化压缩），**不重复设计方案、不修改方案内容**。此为 Gate 步骤：等待人类审批通过后进入更新。
- step-3「更新文档」required=1 model_tier=power human_attention=none description: 按批准方案编写/更新文档（写前自检 R1-R11 规则；修改项目内文档文件用绝对路径，裸相对路径会被拒绝）。

特殊情况调度：

- 当step2用户拒绝了方案并给出原因后，需要在step2后面插入新的方案设计步骤，然后在新方案设计步骤之后再插入一个方案审批步骤，直到用户批准方案

## 设计原则（所有模板通用）

1. 结构遵循"分析→设计→执行→验证"：先充分理解需求与现状，再设计（Gate 确认），再实现，最后验证收尾
2. 关键决策点（方案选择、实现完成）用 human_attention=gate 让人类审批，不要全自动；审查结论不设 gate——type=code_review 步骤 human_attention 必须为 none（审查结论由 AI 输出、流程编排者决策，人类审批如需则用独立 gate 步骤）
3. 并行步骤仅限无依赖的只读分析类（如"阅读文档"与"阅读代码"），用 parallel_with 声明
4. 每步 description 必须包含：目标、产出、验收方式，确保执行者无需猜测
