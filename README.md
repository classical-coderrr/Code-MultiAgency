# Orbit / Agent Team

一个面向软件开发分析场景的「多 Agent 协作平台」MVP。它把一条软件开发需求拆成可观察的 DAG：Requirement → Architect → Human Approval → Backend + Frontend → Tester → Reviewer。

本版本的重点是验证编排链路，而不是修改真实代码仓库。Agent 之间不互相调用，所有产物都通过 Workflow Context 传递；同层节点使用 asyncio 并发，运行状态通过 SSE 推给画布。

## 当前能力

- YAML Workflow DSL，支持 agent / approval 两类 Step
- Step ID、依赖引用、自依赖、循环、并发范围校验
- Kahn DAG 拓扑排序与 Execution Level 计算
- 同层并发执行，默认最大并发数 3，范围 1～10
- 严格的 `{{variable}}` 模板渲染，变量不存在会报 `Undefined workflow variable: xxx`
- Agent 与 Workflow 解耦，Agent 定义位于 `backend/agents/*.yaml`
- `LLMProvider` 抽象、Mock Provider、OpenAI-compatible Provider
- timeout、429、5xx、网络错误的指数退避重试
- Agent 输出写入 Context，并持久化到 SQLite
- Human Approval 暂停、Approve 继续、Reject 失败
- REST API + SSE 运行事件流
- Vue Flow 画布、节点详情、输出查看、全局日志和 Agent 独立运行日志（在各节点内点击展开）
- 中文系统字体栈，避免依赖外部英文字体服务
- Agent 运行策略可视化配置：最大输出 Token、重试次数、`Auto / Off / Low / High / Max` 思考强度
- Provider 能力适配：`Auto` 使用当前模型默认能力，不支持的思考强度自动安全降级并写入 Agent 日志
- 右下角中英语言切换，业务需求与 Agent 输出保持原文
- ChatGPT 本地登录 Provider：通过本机 Codex CLI 的非交互模式运行，不读取或上传 Session/OAuth 文件
- 后端未启动时，前端自动切换本地 Demo Mode，仍可演示完整链路

## 目录结构

```text
.
├── backend/
│   ├── agents/                    # AgentDefinition YAML
│   ├── app/
│   │   ├── api/                   # REST / SSE 适配层
│   │   ├── agents/                # Agent Registry
│   │   ├── llm/                   # Provider 抽象与实现
│   │   ├── repositories/          # SQLite 持久化
│   │   ├── schemas/               # API 请求模型
│   │   ├── services/              # Workflow 加载与 GraphDTO
│   │   ├── workflow/              # models / parser / validator / dag / executor / context / events
│   │   └── workflows/             # Workflow YAML
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/App.vue                # MVP 控制台
│   ├── src/style.css              # 视觉 token 与布局
│   ├── package.json
│   └── vite.config.ts
└── README.md
```

## 快速启动

日常本地测试可直接双击根目录的
[启动开发环境.bat](启动开发环境.bat)。它会检查并释放属于本项目的 3456/2199 旧进程，
启动 Redis、API、独立 Worker 和前端后自动打开浏览器；后台日志写入 `backend/data/logs/`。如果端口被其他程序占用，会提示并跳过，不会强制结束外部进程。

### 1. 启动后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 3456
```

如果 Windows 提示端口被占用，可双击项目根目录的
[release-port-8000.bat](release-port-8000.bat) 释放 8000 端口。脚本会请求管理员权限，
只处理 TCP `LISTENING` 状态且本地端口为 8000 的进程；如果你的服务使用的是其他端口，
请不要直接使用此脚本。

也可以双击项目根目录的 `启动开发环境.bat` 自动释放项目自身的 3456 和 2199 端口，
然后启动后端和前端。该入口由同目录的 `start-dev.ps1` 执行实际逻辑；批处理文件保持纯英文命令，
避免 Windows 命令行编码影响启动。端口查询遇到已经退出的残留 PID 时会自动忽略，不会误判为外部占用。

项目已包含 [backend/.env](backend/.env) 配置模板。请把 `MODEL_API_KEY` 的占位符替换成真实 Key；占位符状态下会安全地使用确定性的 `MockProvider`，不会把无效字符串发送到云端。

所有 `agent` 类型的 Step 都由同一个 `LLMProvider` 调用，Agent 的 `system_prompt` 来自 `backend/agents/*.yaml`，Step 的 `task` 模板由 `WorkflowContext` 渲染后作为 user prompt。`approval` 类型是人工节点，不调用 LLM。

云端 Agent 请求默认使用 120 秒总任务超时、10 秒连接超时、120 秒读取超时和 30 秒写入超时，单次最多重试 2 次。重试会按当前实际档位严格降级到 `low` 或 `off`，并在重试前等待被取消的异步 HTTP 请求完成清理。若模型已把全部输出额度用于推理、正文为空且下一次请求无法进一步降低思考强度，Executor 会立即停止无效重试。

运行时策略可以在每个 Agent 的 Inspector 中单独设置。工作流 YAML 支持在顶层使用 `defaults`（或 `runtime`）设置默认值，也支持在 Step 上使用 `max_tokens`、`retry`、`thinking` 覆盖：

```yaml
defaults:
  max_tokens: 6000
  retry: 2
  thinking: auto

steps:
  - id: architecture
    agent: architect_agent
    max_tokens: 5000
    retry: 1
    thinking: high
```

`auto` 是平台语义，不会直接作为陌生参数发送给云端模型。后端会通过统一的模型能力层按“OpenAI-compatible 传输层 + 模型族”计算 `effectiveThinking`，不能只看 Provider 名称。Qwen、DeepSeek、GLM 等模型族由各自的协议适配器转换思考参数；未知模型默认关闭思考控制，不凭空发送厂商私有字段。日志会记录脱敏后的云端实际参数，避免把执行器意图误当成已经生效的参数。

软件开发工作流还支持自适应调度。运行需求会先由 Architecture Agent 输出固定 JSON，包含 `project_type`、`backend_required` 和 `complexity`。Executor 根据这个结果决定后续路线：简单广告页、落地页或单文件 HTML 会跳过 Backend；需要接口、数据库、登录或支付的项目保留完整后端分支。JSON 不合法时不会猜测，系统会回退到完整工作流。

为防止需求歧义导致错误跳过 Agent，Executor 还会对用户原始需求执行路由保护校验。学生、用户、订单、商品等业务管理系统，只要包含 CRUD、增删改查或业务数据管理，默认保留 Backend 与 Frontend，即使需求写了“简单”“Demo”或没有主动提到 API/数据库。只有用户明确说明纯前端、无需后端、浏览器本地存储或 `localStorage` 时，才允许采用静态前端路线。若 Architecture 与该规则冲突，系统会自动校正决策，并在 `workflow.policy_decided` 日志中记录校正原因。

路由保护采用通用能力模型，不依赖某个行业关键词。系统会从原始需求提取 `frontend`、`backend`、`persistence`、`authentication`、`external_api`、`tool`、`rag`、`artifact`、`human_approval` 和 `async` 等能力，再与 Architecture 返回的 `required_capabilities`、`optional_capabilities`、`evidence` 和 `needs_clarification` 合并校验。明确不需要的能力才允许跳过；范围不明确时保留可能需要的实现分支，并记录待确认问题，不会静默删除 Agent。

这套能力路由已经按企业级边界拆分：`backend/app/workflow/requirements.py` 提供版本化 `RequirementSpec`，`capability_router.py` 提供 `CapabilityRouter`，`architecture_validator.py` 提供类型化 `ArchitectureDecision` 和 `ArchitectureValidator`。Run 启动时会持久化需求规格，Architecture 决策通过校验后才会影响 Workflow 路由。

前端每个 Agent 的 Inspector 中可以选择 `Auto（按任务自适应）` 或 `手动按 Agent 设置`。Auto 模式会先根据原始需求的文本长度、多模块、数据库/API、权限、安全、实时能力、部署和验收约束估算难度，再为 Requirement/Architecture 动态规划低、中、高三档启动预算；Architecture 完成后，后续 Agent 再根据项目类型和复杂度分配预算。界面填写的最大 Token 是本次 Agent 的安全上限，Manual 模式才按每个 Agent 当前配置的输出上限执行。这样既保留用户控制，也避免简单任务无意义地消耗大量 Token。

接入 OpenAI-compatible 服务时配置：

```powershell
$env:MODEL_PROVIDER="qwen" # qwen、deepseek、glm 或 openai_compatible
$env:MODEL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:MODEL_API_KEY="your-key"
$env:MODEL_NAME="qwen-plus"
```

前端 API 配置中的 Provider 可以选择千问、DeepSeek、GLM 或 `OpenAI Compatible（自定义）`。GLM-5.2 应使用智谱的 `https://open.bigmodel.cn/api/paas/v4`、GLM API Key 和 `glm-5.2`；不要把 GLM 模型填在千问 DashScope Base URL 下。GLM 的 `thinking.type` 与 `reasoning_effort` 按官方接口格式发送。[GLM-5.2 官方接口文档](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2)、[智谱 OpenAI-compatible 文档](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)

Provider 是传输和账户标识，模型名称决定模型族能力；用户可以通过自定义 OpenAI-compatible 选项接入未内置名称的模型。平台内部统一使用 `off / low / high / max`，再由模型能力层映射到目标服务商参数；未知模型默认采用关闭思考的安全策略，不会把 Qwen 或 DeepSeek 的参数误发给其他模型。

Executor 不读取这些配置，ProviderFactory 负责创建具体 Provider。

### 2. 启动前端

```powershell
cd frontend
npm install
npm run dev
```

打开 <http://localhost:2199>，输入需求后点击 `Run workflow`。当 Architecture 节点完成时，在右侧 Inspector 点击 `Approve architecture`，即可解锁 Backend 和 Frontend 并行分支。

同一工作流已有未结束 Run 时，后端会返回 409 并附带旧 Run 的 ID 与状态；前端会自动接入该 Run，恢复画布和审批状态。需要新建 Run 时，先在当前运行中点击 `Stop run`。

顶部右侧的 `API settings` 可以配置 Provider、Base URL、Model 和 API Key。点击 `Test connection` 会由后端用当前表单配置发送一次最小连通性请求；点击 `Save configuration` 后会立即替换运行时 Provider，并写入 `backend/.env`。API Key 不会返回给前端，也不会写入运行日志或 SSE 事件。

## Workflow files and frontend recovery

在软件开发主工作流中，`database_agent` 是一个按能力路由的数据库设计节点：当原始需求包含 CRUD、业务数据、数据库或持久化能力时，它会在审批后、Backend 和 Frontend 之前运行，独立生成 `schema.sql`、迁移脚本或初始化数据。Backend 与 Frontend 会读取它生成的文件级上下文，避免实体字段、接口和页面表单不一致。纯展示 HTML 或明确使用 `localStorage` 的需求会自动跳过该节点，不增加无效的 Token 和耗时。

Each Agent has its own contract under `backend/agents/*.yaml`; backend and frontend Agents can evolve independently without changing the executor. The workflow layer is also split into independent YAML files:

- `backend/app/workflows/software-development.yaml` — complete reviewed development chain
- `backend/app/workflows/requirement-analysis.yaml` — requirement analysis only
- `backend/app/workflows/backend-implementation.yaml` — backend plan only
- `backend/app/workflows/frontend-implementation.yaml` — frontend plan only

The frontend loads `GET /api/workflows` and `GET /api/workflows/{workflow_id}/graph`, so the sidebar and canvas are driven by the YAML files instead of a hard-coded workflow. Running a workflow posts to the selected workflow ID.

When the page is minimized, suspended, closed, or reopened, the frontend keeps the active Run ID, workflow ID, and last consumed event cursor in `localStorage`. On return it reloads the durable Run snapshot, restores the graph and approval/clarification state, then reconnects SSE from that cursor; duplicate events are ignored. A completed or stopped run is removed from the active recovery record but remains available from the top-bar **运行记录** drawer. If a new start collides with an existing Run, the frontend adopts the 409 response's active Run instead of showing an unexplained failure.

## Workflow DSL

默认 Workflow 在 [backend/app/workflows/software-development.yaml](backend/app/workflows/software-development.yaml)。核心结构如下：

```yaml
name: software-development
concurrency: 3
inputs:
  requirement: ""
steps:
  - id: architecture
    agent: architect_agent
    depends_on: [requirement]
    task: |
      根据需求设计架构：
      {{requirement_doc}}
    output: architecture_doc

  - id: architecture_approval
    type: approval
    depends_on: [architecture]
```

`meta.layout` 只保存画布位置，GraphDTO 负责把 Workflow 转成 Vue Flow 节点和边，Executor 忽略布局信息。第一版不实现 optional dependency；依赖失败时，下游统一变为 `SKIPPED`。

## 受控跨 Agent 协商（试点）

代码公司工作流在多个责任 Agent 同时涉及同一验证失败时，默认进行一次有限协商：各责任 Agent 提出本责任域的最小修复建议，再复核其他 Agent 的建议。协商只在 Tester 验证失败后的自动整改阶段触发，不增加正常成功路径的模型调用。

配置位于 `backend/app/workflows/software-development.yaml` 的 `meta.collaboration`。将 `mode` 改为 `off` 即可关闭；`max_rounds` 最多 2，`timeout_seconds` 和 `max_tokens_per_message` 控制额外耗时与 Token。消息按 Run 和错误指纹写入 SQLite，Run 详情的 `collaboration` 字段及事件流可供复盘；相同协商恢复时不会重复调用模型。

协商结论仅是修复提示，不得修改冻结合同、跨 Agent 文件归属或判定交付成功。若协商超时、输出无效或提出合同变更，执行器继续使用原有确定性责任路由；候选源码仍须通过目标 Gate 和完整回归才能提交。

## API

```text
GET  /api/health
GET  /api/provider/config
GET  /api/provider/capabilities
GET  /api/skills
POST /api/provider/test
POST /api/provider/config
GET  /api/workflows
GET  /api/workflows/{workflow_id}
GET  /api/workflows/{workflow_id}/graph
POST /api/workflows/{workflow_id}/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
POST /api/runs/{run_id}/approval       {"decision":"approve|reject"}
POST /api/runs/{run_id}/stop
```

创建 Run：

```json
POST /api/workflows/software-development/runs
{
  "requirement": "开发一个支持用户登录、菜谱搜索、AI 推荐菜谱的 Web 应用。"
}
```

SSE 事件包含 `workflow.started`、`workflow.policy_decided`、`step.started`、`step.completed`、`step.failed`、`step.skipped`、`step.waiting_approval`、`step.runtime_adjusted`、`step.skills_resolved`、`step.validation_started`、`step.validation_check`、`step.validation_succeeded`、`step.validation_failed`、`workflow.waiting_approval`、`workflow.completed`、`workflow.failed` 和 `workflow.stopped`。每个事件都带 `runId`、时间戳和 payload。

## 动态 Skill 编排

Skill 是运行时可选的提示词方法包，不是写死在 Agent YAML 中的角色职责，也不会执行本地代码。Agent 的稳定职责仍在 `backend/agents/*.yaml`；Workflow Step 只声明“候选 Skill”，由框架在真正请求模型前决定是否附加到该 Step 的系统提示词。

- 内置 Skill 位于 `backend/skills/<skill-id>/SKILL.md`，支持 YAML front matter：`id`、`version`、`applies_to`、`capabilities`、`min_complexity`、`max_injection_tokens`。
- Workflow Step 支持 `skill`（单个）或 `skills`（列表），以及 `skill_mode: auto | on | off`。Run 请求的 `runtime.defaults.skill_mode` 可覆盖 Workflow 默认值，前端 Inspector 的 **Skill 策略** 会发送这个全局开关。
- `Auto` 不额外调用 LLM：它根据已有 `RequirementSpec` 的能力和本地难度评估选择候选项；简单任务不会因 Skill 自动膨胀上下文。`开启` 会加载该 Step 的全部可用候选项（最多两个），`关闭` 则不注入任何 Skill。
- `step.skills_resolved` 日志记录模式、难度、已注入/缺失/跳过的 Skill 与注入字符数；这些审计信息随 Run 状态快照持久化。关闭 Skill 不会关闭 Schema 校验、成果物完整性、超时、取消或重试保护。

示例：

```yaml
defaults:
  skill_mode: auto

steps:
  - id: backend
    agent: backend_agent
    skills: [backend-api-contract, artifact-file-generation]
    skill_mode: auto
```

本地开发时可设置 `AGENT_TEAM_SKILLS_DIR` 指向一个覆盖目录；同名 Skill 以该目录优先。`GET /api/skills` 只返回元信息，Skill 正文不会下发给浏览器。

## 数据模型与职责边界

- `WorkflowDefinition` / `StepDefinition`：声明式流程模型
- `WorkflowContext`：run 范围的状态容器，只有 Executor 在 Step 成功后写入 output
- `WorkflowDAG`：依赖关系、拓扑顺序和分层
- `WorkflowExecutor`：只调度，不负责 HTTP、UI 和具体模型 SDK
- `AgentRegistry`：按 agent id 加载 AgentDefinition
- `LLMProvider`：统一的 `generate(system_prompt, user_prompt, config)` 合约
- `SQLiteRepository`：Run、StepRun、Approval 的历史记录
- `WorkflowEventBus`：把执行事件广播给 SSE 客户端

## 测试

后端测试覆盖 DAG topology、并发 Level、环检测、缺失依赖、重复 ID、Context 变量、Agent 缺失、重试、超时、下游跳过、Approval 暂停/继续和完整 E2E 链路。

```powershell
cd backend
pytest -q
```

## 后续迭代

1. 运行恢复与服务器重启后的未完成任务处理
2. Workflow 编辑 API：新增节点、删除节点、拖线、保存 YAML
3. 更细粒度的失败分支和人工回退
4. Provider 配置管理与 token 成本报表
5. Agent 版本、权限和运行隔离
6. 真实代码修改、Git Branch、Sandbox、PR
## 本轮实现：LangGraph 与 HTML 产物

- `WorkflowExecutor` 现在通过 LangGraph `StateGraph` 执行 Workflow；前端仍然使用原有 Vue Flow GraphDTO，因此执行内核和可视化层保持解耦。
- LangGraph 使用 `thread_id=run_id` 建立 Checkpoint；审批继续使用 `interrupt` / `Command(resume=...)`。
- 并行节点只合并 `context` 与 `results`，不会竞争写入 `run_id`；后端重启且原 Checkpoint 不存在时，会根据 SQLite 步骤状态安全重放，已完成的 Agent 不会重复调用。
- 成功 Run 会把 `final-report.md` 和实现 Agent 明确返回的源码文件写入 `backend/data/workspaces/<run_id>/`。带文件名的 Markdown 代码块会被识别为独立 Artifact，例如 `html index.html`、`css style.css`、`python main.py`。
- 前端“可预览文件”区域会直接展示产物；HTML 支持沙箱预览，任意源码支持在线查看和下载，不需要再次调用 LLM。

新增接口：

```text
GET /api/runs/{run_id}/artifacts
GET /api/runs/{run_id}/artifacts/{artifact_id}/content
GET /api/runs/{run_id}/artifacts/archive
```

`/artifacts/archive` 会把当前 Run 的全部已登记成果物统一打包为 ZIP，前端“工作产物”区域提供“下载全部 ZIP”。每个 Agent 的运行日志继续跟随节点展开，日志正文支持鼠标选中复制，并提供“复制”按钮；复制操作只读取当前页面日志，不会再次调用 LLM。

本轮验证：后端测试覆盖 Artifact 源码提取、路径隔离和 ZIP 完整性，前端生产构建覆盖成果物查看器、ZIP 下载入口和 Agent 日志复制交互。

## 通用 Agent 运行底座

代码公司 Demo 现在使用通用的运行时能力，不把执行链路限定为某一种项目类型：

- `TokenBudgetManager` 在请求前根据 Provider 能力、上下文窗口、输入 Token 和安全余量计算实际输出预算；Runtime Plan Agent 的建议不会低于普通 Agent 的可用最低预算。
- Workflow 可以声明可选的 `runtime_plan: true` Agent。代码公司先让 Requirement 使用原始需求的默认启动预算完成分析与必要的人工确认，再运行 `token_budget_agent`；它使用最多 800 Token 的固定 JSON，根据已确认需求评估难度、模型强度、后续 Agent 输出预算和建议思考强度。
- Runtime Plan Agent 默认最多使用 800 Token、关闭思考且不重试；这些是平台硬限制，不能被普通前端运行参数覆盖。它不会反向覆盖已经完成的 Requirement 预算。如果评估失败，`failure_policy: continue` 会切换到基于已确认需求的本地难度预检，不阻断 Architecture 及后续流程。评估结果还会与 Architecture 策略取更安全的预算，再受用户单 Agent 上限和 Provider 上限约束；架构与成果物文件清单确定后，代码生成继续按文件或片段细化预算。
- Requirement 必须保留用户原始范围，将最小假设、可选建议和待确认项分开；Architecture、Backend 和 Frontend 都会同时接收原始需求，不能把可选建议自动升级为本次必做范围。
- `ContextPacker` 只渲染 Workflow 模板引用的上下文；超长字段会被可追踪地压缩，原始值仍保留在 Run 状态中。
- `OutputInspector` 检查空输出、`finish_reason=length`、JSON 和 HTML 完整性。
- 输出被长度截断时，Executor 会保存已生成部分并请求 Provider 从断点续写，再合并和校验结果。
- SSE 增加上下文整理、预算调整和续写事件，前端 Agent 日志会显示实际处理过程。
- Workflow Step 支持 `generation_mode: single | artifacts | auto`。`artifacts` 会先生成小型文件清单，再按文件独立生成并校验；较大的文件会继续拆成有顺序的源码片段任务，优先逐段生成和合并。单个文件在 `finish_reason=length` 时，先保存已生成内容，只请求从截断位置开始的缺失后缀并在服务端合并，不重新生成整个文件；当前文件或片段仍失败才做定点修复，只有分块和修复仍无法完成时才进入最后一级 continuation，不把整个回答重新塞回 Prompt。成果物模式按文件/片段估算独立申请输出空间，不会把 Runtime Plan Agent 的小额整步建议当成每个文件的硬上限；实际提升和兜底过程会通过 Agent 日志记录。`auto` 会根据输出格式和用户是否明确要求代码成果物选择策略。
- 代码公司的 Backend / Frontend Step 默认使用 `generation_mode: auto`。生成源码时，前端日志会显示“规划成果物、生成文件、定点修复、文件校验”，并把文件写入 Artifact Workspace；分析/评审需求仍使用单次文本模式。

这些能力属于 Agent Runtime / Orchestrator，不属于 HTML 特判。未来复杂代码、画展策划或电商 Workflow 都可以复用同一套预算、上下文、分块成果物和输出校验机制。
### ChatGPT 本地登录模式

前端 Provider 可以选择“ChatGPT（本地 Codex 登录）”。后端不会读取或上传 Codex 的 Session/OAuth 文件，而是调用本机已登录的 Codex CLI 非交互模式。测试连接只检查本机登录状态，不额外消耗模型额度；实际 Agent 请求通过本地子进程执行。

该模式不需要 MODEL_API_KEY。可以设置 MODEL_PROVIDER=chatgpt_local、MODEL_NAME=codex-default；如果找不到 codex，可通过 CODEX_EXECUTABLE 配置可执行文件路径。由于本地 Codex CLI 不提供本项目 HTTP 层的硬 Token/思考参数，界面中的相关设置在该模式下属于软提示，日志会标明实际由本地 Codex 默认策略决定。
## Run、Artifact、Checkpoint 与失败恢复

当前版本已把一次工作流运行拆成四层可追踪状态：

- Run：SQLite 保存状态、输入、工作流快照、心跳、恢复次数和最终结果；`GET /api/runs` 提供可分页的历史摘要，正常 API 不会返回内部快照正文。
- Artifact：每个 Run 使用独立工作区，文件通过临时文件 + 原子替换写入，并保存 SHA-256；下载全部 ZIP 时只打包当前 Run 的可校验文件。多 Agent 共同产出项目时，ZIP 按 `owner_step` 分目录，例如完整前后端项目会得到 `frontend/` 与 `backend/`，避免同名 `src/`、README 或构建文件混在一起。
- Checkpoint：LangGraph 使用 `AsyncSqliteSaver` 保存执行节点状态，审批暂停和进程重启可以继续执行。
- 失败恢复：后端启动会扫描 `PENDING`、`RUNNING`、`WAITING_APPROVAL` 的未完成 Run；运行中断的节点会回收到待执行状态，已成功的前置节点不会重复调用。失败 Run 可以调用 `POST /api/runs/{run_id}/retry`，只重试失败节点及其下游节点。
- Token 计量：右上角统计从持久化的 `step.completed` / `step.failed` 事件累计所有真实尝试；失败后恢复成功不会用最后一次 Step 记录覆盖此前已经消耗的 Token。

运行事件也会写入 `run_events` 表。前端重新连接 SSE 时携带最后事件游标，后端先从 SQLite 补齐漏掉的事件，再通过 Redis Streams 接收实时事件，因此页面刷新、最小化或 API 进程重启不会丢失运行日志。恢复事件使用 `workflow.recovered` 标记，便于区分首次执行和恢复执行。

### Redis 高可用运行模式

默认的 [backend/.env.runtime](backend/.env.runtime) 开启 `AGENT_TEAM_EXECUTION_MODE=redis`。SQLite 继续是 Run、Step、Artifact、Checkpoint 和审计记录的最终事实来源；Redis 只承担任务队列、实时事件、控制命令、Worker 存活和可续租执行权，不保存模型密钥或不可替代的业务数据。

- API 进程只创建 Run、查询历史和投递命令，不再用进程内 `asyncio.create_task` 承担长任务。
- 独立 `python -m app.worker` 进程执行 LangGraph，并按 `REDIS_WORKER_CONCURRENCY` 并发托管多个 Run。等待审批或需求确认的 Run 会继续保有可恢复执行权，但不会堵塞后续队列。每个 Run 需要取得带随机 fencing token 的租约；租约丢失会先取消底层执行并保存状态，不会把 Run 错误标记为失败，其他 Worker 可在租约过期后接管。
- Provider 每次真实尝试写入 Attempt Ledger；Worker 的取得、续租、释放与接管记录写入 Worker Lease Ledger。Run 详情可还原“由谁执行、消耗多少 Token、从哪里恢复”。
- 等待审批/澄清的 Run 重启后只重新挂载暂停状态，不重复调用模型；其他非终态 Run 的自动接管受 `REDIS_MAX_AUTO_RECOVERIES` 限制，达到上限后明确熔断并等待人工重试。
- Redis 暂时不可用时，已经落入 SQLite 的事件和业务状态不会丢失；SSE 会退回 SQLite 轮询。Redis 恢复后实时分发继续工作。
- `GET /api/health` 同时检查 Redis 与已注册 Worker；Redis 模式下没有活动 Worker 时返回 `degraded`，避免 API 看似正常但任务无人消费。

根目录 [docker-compose.redis.yml](docker-compose.redis.yml) 提供本地 Redis 7（AOF 持久化）。双击 [启动开发环境.bat](启动开发环境.bat) 时会优先连接已有 Redis；本地 Docker Engine 未运行时会尝试启动 Docker Desktop，再启动 Redis、API、Worker 和前端，并把诊断日志写入 `backend/data/logs/`。

如果 Docker Desktop 不存在、启动超时或 Redis 暂时不可用，快捷启动器会仅对本次启动自动降级为 `local`：Run、Step、Artifact 和 LangGraph Checkpoint 仍由 SQLite 持久化并可在重启后恢复，但 Redis 多 Worker 队列、跨进程事件转发和租约协调会暂时关闭；启动器不会改写 `.env.runtime`。生产环境或必须验证 Redis 的场景可设置 `AGENT_TEAM_REDIS_REQUIRED=1`，此时 Redis 不可用仍会立即停止。也可以将 `REDIS_URL` 指向已有 Redis，或把 `.env.runtime` 中的 `AGENT_TEAM_EXECUTION_MODE` 明确改为 `local`。

## Tester Agent 成果物验证门禁

Spring Boot 数据库联调默认使用隔离 H2 内存数据库，不要求用户本机安装数据库。除 Maven test、启动、API 合同外，CRUD 探测会确认新增可查询、修改实际生效、删除后记录消失；HTTP 500 检查携带后端异常根因日志。

初始化路线按实际依赖选择：迁移 SQL 只有配合 Flyway 执行依赖才启用；若只有 schema.sql 执行能力，则先运行 Spring SQL init 再进行 JPA 校验，不能因目录中存在迁移备份而禁用建表。H2 支持属于 flyway-core；生成和整改阶段会确定性纠正错误的 flyway-database-h2 声明，同时保留其他依赖与版本配置。

启动探测同时从前端请求和后端 Controller 发现真实 GET 路由，收到 HTTP 404/401/500 时报告实际端点状态，不再笼统归为“无响应启动超时”。模板页面另检查 Thymeleaf 依赖和 MVC 视图处理，避免 REST API 健康但页面未交付；CRUD 数据样例遵守实体的 JsonProperty 字段别名。

自动整改先基于现有源码修改最小文件，必要时升级关联文件整改，不再重生成整个责任模块。候选只在独立上下文和临时工作区验证，全部检查通过后提交新版；失败或验证倒退保留原产物。新版使用 owner revision 合并，避免旧文件从 Checkpoint 回流。

自动整改现已由独立的 LangGraph `RepairCoordinator` 子图负责。所有节点和验证器错误统一转换为 `FailureFact`，再由 `FailureClassifier` 判断失败阶段、责任 Agent、可重试性和修复动作；修复过程按“候选工作区 → 责任域 Target Gate → 完整回归 → 提交 Stable”的顺序执行。Target Gate 未推进时撤回本轮候选，连续相同错误会按错误指纹触发熔断，避免无限循环。Backend 与 Frontend 使用独立 Agent Workspace 并行工作，不共享可写目录。右上角运行统计展示首次成功、自动修复率、平均修复轮数、各阶段耗时和 Token，并可查看“失败位置 → 责任 Agent → 修复轮次 → 验证结果”的证据链。

代码公司 Workflow 的 Tester Step 现在同时使用两层校验：`ArtifactValidator` 负责确定性检查，Tester Agent 负责解释结果、归纳风险并给出修复建议。Tester 不能仅凭模型回复把运行标记为成功。

验证器会在隔离临时目录中检查：

- 成果物路径是否安全、是否为空、是否重复
- 前端是否有标准 `index.html`、`package.json`、`src/main.*`、`src/App.vue` 和有效依赖声明
- Spring Boot 后端是否有标准 `pom.xml`、`src/main/java`、匹配 package 的 Java 路径以及 `src/main/resources`
- 配置允许时执行依赖安装、前端构建、后端编译，并使用随机端口启动服务检查首页或健康接口

执行命令使用参数数组而不是 Shell 拼接；子进程会继承超时和取消清理，环境变量中的 API Key、Token、密码等敏感信息会被移除。验证失败时，Tester 会根据编译器/构建器给出的文件与行号选择最小修复目标，调用对应产物 Agent 定点修复并重新执行整套确定性校验；导入/导出错误优先修复导出方，避免同时重写两端再次制造契约漂移。只有复验通过才显示最终绿灯；重试用尽仍失败时，当前 Artifact 仍会保留供下载排查。

YAML 可以按公司模板调整门禁策略：

```yaml
- id: tester
  agent_id: tester_agent
  validation:
    enabled: true
    targets: auto
    build: true
    startup: true
    install_dependencies: true
    timeout_seconds: 120
    startup_timeout_seconds: 30
    repair_attempts: 2
```

## 冻结合同、浏览器验收与可交付门禁

代码公司 Workflow 默认启用 `meta.delivery_contract: true`。Architecture 的紧凑 JSON 同时提供实体/API 定义，平台依据用户明确技术栈冻结合同，随后 Database、Backend、Frontend、Tester、Reviewer 共用同一合同。源码及合同指纹与验证绑定；页面、资源、数据库或打包不完整时不允许成功。

代码公司采用契约驱动链路：`RequirementSpec → 缺口分析/必要时澄清 → DeliveryContract → 冻结 ProjectBlueprint → ContractCompiler → Database/Backend/Frontend → IntegrationGate → 构建/启动/修复`。高影响歧义（例如“购物网站 + CRUD”但未说明主要对象）在运行前向用户确认；包名、分页大小、未指定的本地测试数据库等低影响缺口使用可追溯的安全默认值。用户明确的实体和数据库优先于模型建议。

Compiler 产生明确的 Entity/API、依赖清单、文件归属和文件依赖 DAG。`pom.xml`、`package.json` 由平台确定性生成；每个 Agent 只获得自己的角色合同，计划中缺文件或依赖环在生成前报错。Tester 等待三条实现分支都结束，并在构建前检查文件归属、冻结文件计划、API 路由、表单字段、相对导入和 SQL 表字段。失败记录责任 Agent，现有修复协调器在隔离候选工作区定点整改、目标复验、再完整回归。MySQL 生产配置仍用隔离 H2 做本地兼容性联调；这不等于已验证真实 MySQL 部署。

Spring Boot + HTML：页面放在 `src/main/resources/static/index.html`，默认后端端口 2198，接口同源。Spring Boot + Vue：前后端源码分别打包；Vite `/api` 代理读取 `process.env.VITE_API_PROXY`，默认 `http://127.0.0.1:2198`。验证器通过环境变量使用本次隔离端口，同时运行前端、后端和 H2。

浏览器门禁使用 Playwright/Chromium。首次准备平台前端依赖后安装浏览器（在项目根目录）：

```powershell
npm --prefix frontend install
npm --prefix frontend exec -- playwright install chromium
```

测试会检查实际 HTML、同源资源、JavaScript 异常、可见正文，并在 CRUD 页面通过稳定 `data-testid` 操作新增/修改/删除及查询列表。缺少 Node/浏览器时明确 blocked，不会误报成功。复杂页面业务仍应补充项目专属测试，不代表所有业务语义已经覆盖。

最终 ZIP 只包含登记的本次源码和交付元数据，不附带 Maven/Node 缓存。`delivery-report.json` 记录合同、验证、源码指纹和门禁结果。下载失败 Run 的 ZIP 仅用于排查，不能视为可交付版本。

### 从新需求进行真实回归

以下脚本默认只输出计划，不调用模型；只有显式 `--live` 才使用当前 `.env` 的真实 Provider。测试 Run 使用独立 SQLite、异步 LangGraph Checkpoint 和工作区，不修改生产历史。`--auto-approve` 仅自动确认隔离测试审批，不修改正常产品审批策略。

```powershell
python backend/scripts/regression_suite.py --cases backend/scripts/regression_cases.json
python backend/scripts/regression_suite.py --live --auto-approve --cases backend/scripts/regression_cases.json --repeat 2 --timeout 1800
```

报告位于 `backend/data/regression/<时间-随机ID>/report.json`；各用例保留现场、工作流快照、事件、验证、源码和 ZIP。可统计首次成功率、最终可交付率、实际耗时、Token 与整改次数；单 Run 具有总超时及进程树取消清理。仍受 Provider 真实输出/上下文限制，不承诺无限 Token 或任意需求必然成功。
