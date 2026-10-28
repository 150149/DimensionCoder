# DimensionCoder

[English](./README.md) | **简体中文**

> 由人类给出思路，完全由 AI 实现的编程工具

DimensionCoder **本身的代码完全由 AI 编写**——人类只给出点子、思路与方向，全部实现交由 AI 完成。这既是一次「人类出思路、AI 来编程」的自我实践，也是一款以同样方式工作的 AI 编码平台：面向用户的开发任务，由人类给出思路与拍板方案，AI
自动把任务拆解为结构化的多步骤流程（DAG），逐步调研、规划、编码、审查、报告，并在关键节点等待人工审批。它以任务卡片方式管理 AI 代码修改，提供实时状态流与文件变更管理。

采用**单进程架构**：浏览器 → Python 单进程，由 Python 同时托管前端静态资源（`frontend/dist`）、REST API（`/api/*`）与 SSE 事件流（`/sse`），服务器运行时零 Node、无独立前端服务。

```
┌──────────┐   HTTP / SSE    ┌──────────────────────────────────────┐
│  浏览器    │ ──────────────▶ │        Python 单进程（默认 8501）        │
│ 桌面/移动  │ ◀────────────── │  · 托管前端 frontend/dist              │
│  双端      │                 │  · REST API（/api/*）                 │
└──────────┘                 │  · SSE 事件流（/sse）                  │
                             └──────────────────────────────────────┘
```

![](./image/1.png)

## 工作流程

一次任务从「人类给出思路」到「AI 交付并报告」的完整闭环：

```
创建任务 ──▶ AI 编排 DAG 流程 ──▶ 执行循环 ──▶ 最终审查 ──▶ 流程报告
 (选类型/                        │
  自定义)                        ▼
                    next-step（选下一步）
                        → prepare（注入角色提示词 + 任务背景 + 前序产物 + 关键发现）
                        → LLM 流式执行（多轮工具调用，SSE 实时推送）
                        → step_done（显式完成，需二次确认自检）
                        → Monitor 编排（跳过/追加/重排/标记完成，动态调整 DAG）
                        → 命中 gate 则暂停等待人工审批；被拒则回退上一步
```

1. **创建任务**：选择预设任务类型或 `custom` 自定义流程；任务以卡片形式管理。
2. **动态编排**：Monitor Agent 依据任务与「人类拍板方案」自主设计多步骤 DAG，步骤间含依赖、排序与并行关系，并可在执行中随时调整流程。
3. **步骤执行**：每步按其角色加载专属系统提示词与工具集，流式调用 LLM 与工具；步骤状态流转 `pending → active → completed / skipped / stopped`，产出物持久化供后续步骤读取。
4. **人工审批（Gate）**：关键决策点插入 Gate 步骤，用户可批准 / 拒绝 / 发送消息引导 / 强制注入指令 / 停止执行；被拒消息自动回退到上一步进行调整。
5. **质量循环**：方案审批、代码审查、最终审查层层把关，任一环节被拒都会把结果回送对应步骤修正。
6. **收尾**：最终审查（review）通过后产出结构化流程报告（report）。

## 特色功能

- **动态 DAG 编排**：结构化多步骤，内嵌默认工作流，AI 根据人类拍板方案自主推进任务，执行中可跳过/追加/重排步骤。
- **双模型 LLM 分层调度**：light / power 两档模型按步骤复杂度自动路由，兼顾质量与开销；支持 OpenAI 兼容 API 与 Gemini 原生协议，并可配置第二路 light 通道。
- **记忆系统**：
  - 流程级：关键发现自动捕获并持久注入后续步骤、前序步骤完整聊天记录可回溯、流程报告沉淀。
  - 项目级：代码流程图可视化、项目背景与编码约定（`prompts/rules/`）随步骤注入。
- **随时可介入**：支持流程级介入与步骤级介入，可中断、暂停、恢复；介入方式含发送消息、强制注入、停止执行。
- **多重循环确保质量**：
  - 方案被拒时，自动将被拒消息发回上一个步骤调整方案。
  - 执行修改时需两次调用「任务完成」才算结束，确保自检一遍。
  - 独立代码审查被拒时，自动将审查结果发回上一个步骤调整代码。
- **自动上下文管理**：上下文不超 400k，自动摘要前序工具调用轮次；Plan 文件不丢弃；实在超限则开新步骤继续完成（token 由 tiktoken 确定性计数）。
- **可开关自动审批模式**：开启后自动同意推荐方案，减少人工等待。
- **内置多种角色**（11 种步骤类型，各配专属提示词与工具集）：
  - 方案设计优化：根因一处解决优先，多方案自动对比，前序方案盘点（`architect` / `plan`）。
  - 方案审批优化：六大节审批报告——计划盘点表、现状与根因表、方案列表（影响分析 + 实施步骤）、方案对比、验收表，并展示代码流程图，方便审批（`gate`）。
  - 代码审查优化：固定审查方向、审查分类、自动打回（`code_review` / `review`）。
  - 另有需求分析（`requirement`）、调研（`researcher`）、逆向（`reverse`）、执行（`executor`）、报告（`report`）等角色。
- **电脑端 / 移动端双端互通**：响应式界面，桌面与移动端均可查看流程、审批与介入。

### 更多能力

- **逆向工程工具集**：加密常量提取（匹配 MD5/SHA/AES/DES/CRC/RC4/TEA 等 S-Box）、通配符字节搜索、基于 angr 的反编译、Z3 约束求解。
- **二进制模拟器**：Unicorn 引擎驱动的 x86 PE 模拟，支持反混淆、符号执行与 Windows API 桩。
- **MCP 工具扩展**：通过 `mcpServers` 接入 stdio / http 型 MCP 服务器，动态扩展工具能力。
- **安全与传输**：可选 HTTPS/TLS；可选「SSH 式密钥登录」（ECDSA P-521 公钥挑战验签 + 会话）。
- **实时流式交互 + 内置编辑器**：SSE 推送流式文本、工具调用与思考过程；内置 Monaco 代码编辑器、文件树浏览与代码流程图渲染。

## 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| 后端框架 | FastAPI | 0.115.0 |
| ASGI 服务器 | Uvicorn (standard) | 0.32.0 |
| LLM SDK | OpenAI Python SDK | 1.40.0 |
| SSE | sse-starlette | 2.1.0 |
| Token 计数 | tiktoken | 0.7.0 |
| 加密 / 密钥登录 | cryptography | 45.0.4 |
| HTTP 客户端 | httpx | 0.27.0 |
| 数据库 | SQLite（内置） | — |
| 前端框架 | React | 18.3.1 |
| 类型系统 | TypeScript | 5.5.3 |
| 构建工具 | Vite | 5.3.3 |
| 代码编辑器 | Monaco Editor（@monaco-editor/react） | 0.49.0 |
| 前端路由 | react-router-dom | 6.24.0 |
| Markdown / 图表 | react-markdown + remark-gfm / mermaid | 9.0.1 / 11.x |
| 测试 | pytest / Vitest / Playwright | 8.2 / 2.0 / 1.44 |
| Python | > = 3.9 | — |

## 快速开始

### 环境要求

- **操作系统**：Windows 10/11 或 Server 2019+（随附 `.bat` 启动脚本）。
- **Python**：>= 3.9（服务器运行时仅需 Python）。推荐官方安装包（含 `py` launcher）。
- **Node.js**：仅用于构建前端；服务器运行期不需要。
- **网络**：需能访问 LLM 服务端点（DeepSeek / 通义等 OpenAI 兼容 API）；首次安装依赖需联网。

### 安装步骤

1. **安装后端依赖**：双击 `install.bat`（等价于 `cd python-backend && pip install -r requirements.txt`）。
  - 内网或下载慢时可加镜像源：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. **构建前端**（从源码仓库运行时必需；交付包已含 `frontend/dist/` 可跳过）：
   ```bash
   cd frontend
   npm install
   npm run build      # 产出 frontend/dist/，由 Python 静态托管
   ```
3. **准备配置**：复制 `config.example.json` 为 `config.json`，填入模型与 API Key（详见「配置说明」）。

### 启动与停止

- 双击 `start.bat` 启动（会依次预检前端产物、Python 依赖、端口占用），打印 `DIMENSIONCODING_PORT:8501` 即成功；也可直接运行 `python-backend/start_python.bat`。
- 浏览器访问 `http://localhost:8501`（本机）或 `http://服务器IP:8501`（同内网其他设备）。
- 停止：在运行窗口按 `Ctrl+C`，服务优雅退出（进行中的任务会先安全中止，重启后可恢复）。

## 配置说明

复制 `config.example.json` 为 `config.json` 后按需修改：

```json
{
  "lightModel": "deepseek-v4-flash",
  "powerModel": "glm-5.2",
  "projectRoot": "E:\\workspace",
  "contextWindow": 1048576,
  "lightBaseUrl": "https://api.deepseek.com/v1",
  "powerBaseUrl": "https://api.deepseek.com/v1",
  "lightApiKey": "sk-123456",
  "powerApiKey": "sk-123456",
  "lightInputPrice": 3,
  "lightCachedPrice": 0.1,
  "lightOutputPrice": 9,
  "powerInputPrice": 8,
  "powerCachedPrice": 2,
  "powerOutputPrice": 28,
  "https": {
    "enabled": true,
    "certFile": "certs/server.crt",
    "keyFile": "certs/server.key"
  },
  "auth": {
    "enabled": false,
    "sessionHours": 8,
    "challengeTtlSec": 300,
    "publicKeys": []
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `lightModel` / `powerModel` | 双模型名称（light 轻量任务 / power 复杂任务） | — |
| `lightBaseUrl` / `powerBaseUrl` | 各模型 API 基础 URL（OpenAI 兼容） | — |
| `lightApiKey` / `powerApiKey` | 各模型 API Key | 需替换为真实 Key |
| `lightModel2` / `lightBaseUrl2` / `lightApiKey2` | 第二路 light 通道（三项齐备时启用） | 空（不启用） |
| `projectRoot` | 工作区根路径（AI 读写代码的目录） | `python-backend/workspace` |
| `contextWindow` | 上下文窗口大小（token） | `1048576` |
| `*InputPrice` / `*CachedPrice` / `*OutputPrice` | 各模型计价（元/百万 token），用于成本统计 | 见示例 |
| `llmChannel` | New API 通道 JSON（`newapi_channel_conn`），粘贴即自动填充 URL/Key | 空 |
| `https` | HTTPS/TLS：`enabled` 开关、`certFile` / `keyFile` 证书与私钥路径 | `enabled=false` |
| `auth` | 密钥登录：`enabled` 开关、`sessionHours` 会话时长、`challengeTtlSec` 挑战有效期、`publicKeys` 允许的 ECDSA 公钥列表 | `enabled=false` |
| `mcpServers` | MCP 服务器数组：`{name, type: stdio\|http, command, args, env, url, enabled}` | 空 |

> **安全提示**：`config.json` 含明文 API Key，已被 `.gitignore` 排除，切勿提交。`auth.enabled=false` 时服务无鉴权，默认绑定 `0.0.0.0` 仅供内网使用；如需暴露到更大网络，请开启 `auth` 与 `https`。

## 环境变量

环境变量优先级高于 `config.json`，可用于覆盖配置：

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `DIMENSIONCODING_PORT` | 服务端口 | `8501` |
| `DIMENSIONCODING_HOST` | 监听地址 | `0.0.0.0` |
| `DIMENSIONCODING_DB_PATH` | SQLite 数据库路径 | `python-backend/data/dimensioncoding.db` |
| `DIMENSIONCODING_PROJECT_ROOT` | 工作区根路径 | `python-backend/workspace` |
| `DC_CORS_ORIGINS` | CORS 允许来源（逗号分隔） | `http://localhost:8501,http://127.0.0.1:8501` |
| `LLM_BASE_URL` / `LLM_API_KEY` | LLM 基础 URL / Key（回退用） | 取 `config.json` |
| `LLM_LIGHT_BASE_URL` / `LLM_LIGHT_API_KEY` / `LLM_LIGHT_MODEL` | light 模型 URL / Key / 名称 | 取 `config.json` |
| `LLM_POWER_BASE_URL` / `LLM_POWER_API_KEY` / `LLM_POWER_MODEL` | power 模型 URL / Key / 名称 | 取 `config.json` |
| `LLM_LIGHT_BASE_URL2` / `LLM_LIGHT_API_KEY2` / `LLM_LIGHT_MODEL2` | 第二路 light 通道 | 空 |
| `DC_AUTH_ENABLED` | 是否启用密钥登录（`true`/`false`） | 取 `config.json` `auth.enabled` |
| `DC_AUTH_SESSION_HOURS` | 会话有效时长（小时） | `8` |
| `DC_HTTPS_ENABLED` | 是否启用 HTTPS（`true`/`false`） | 取 `config.json` `https.enabled` |

## 项目结构

```
DimensionCoder/
├── install.bat                  # 一键安装 Python 依赖
├── start.bat                    # 启动服务（预检前端产物/依赖/端口）
├── config.example.json          # 配置示例（复制为 config.json）
├── README.md / README.zh-CN.md / LICENSE
├── image/                       # 文档配图
├── python-backend/
│   ├── requirements.txt         # Python 依赖清单
│   ├── pyproject.toml           # Python 项目配置
│   ├── start_python.bat         # 直接以 Python 启动服务
│   └── dc_server/               # 核心服务包
│       ├── server.py            # ASGI 入口（python -m dc_server.server）
│       ├── rest_api.py          # REST API 端点
│       ├── config.py            # 配置 / 环境变量入口
│       ├── auth.py              # SSH 式密钥登录（ECDSA P-521 挑战验签）
│       ├── tls.py               # HTTPS / TLS 支持
│       ├── observability.py     # 运行指标 / 可观测性
│       ├── step_context.py      # 步骤上下文（背景 + 前序产物注入）
│       ├── monitor_context.py   # Monitor 编排上下文
│       ├── graceful.py          # 优雅关闭 / 重启恢复
│       ├── tool_security.py     # 工具路径安全（越界防护）
│       ├── brain/               # LLM 与编排：llm_client / orchestrator / mcp_client / sse_hub
│       ├── state_machine/       # 任务与步骤状态机、任务类型
│       ├── storage/             # SQLite 持久化（adapter / sqlite_adapter / 迁移）
│       ├── models/              # 数据模型
│       ├── prompts/             # 角色提示词（planner/architect/code-reviewer/... + rules/）
│       ├── ctf_tool/            # CTF/逆向工具（常量提取/字节搜索/反编译/Z3）
│       └── simulator/           # 二进制模拟器（PE 模拟/反混淆/符号执行/API 桩）
└── frontend/                    # React + TypeScript 前端
    ├── package.json / vite.config.ts / tsconfig.json / index.html
    └── src/
        ├── App.tsx / main.tsx
        ├── api/                 # REST / SSE 调用层
        ├── auth/                # 登录与密钥（前端）
        ├── panels/              # FlowOverview / StepDetail / MonitorDetail / Sidebar / LoginPage
        ├── components/          # ChatMessage / ToolCard / CodeDiagram / Markdown / InterveneBar ...
        ├── editor/              # Monaco 编辑器 + 文件树/浏览（CodeEditor/CodeTab/FileBrowser/FileTree）
        ├── hooks/               # 流式/轮询/消息/媒体查询等 Hooks
        ├── config/              # 设置页
        ├── theme/               # 全局样式
        └── utils/               # 工具函数
```

> 运行时生成、已被 `.gitignore` 排除的目录/文件：`python-backend/data/`（SQLite）、`python-backend/workspace/`（默认工作区）、`frontend/dist/`（前端构建产物）、`frontend/node_modules/`、`config.json`、`certs/`。
