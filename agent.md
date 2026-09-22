# Agent Team 项目协作约定

## Bug 与错误记录

后续处理本项目时，只要遇到 Bug、异常、接口错误、构建失败、测试失败、端口问题或运行时故障，都必须在问题处理完成后追加记录到：

`D:\Agent-Team\错误分析.txt`

记录必须先总结错误要点，再记录解决步骤。每条记录必须包含以下内容：

1. 日期和问题标题
2. `错误要点`：用 1～3 句话提炼“哪里出错、影响了什么、在什么条件下出现”，不要直接复制整段终端或浏览器日志
3. `关键证据`：只保留能帮助定位的状态码、异常名称、进程/文件/接口和关键时间；长日志只做摘要
4. `根因分析`：说明导致问题的直接原因和必要的上下文；如果根因尚未确认，必须明确写“待确认”，不能猜测成结论
5. `解决步骤`：按实际执行顺序编号，写清每一步检查了什么、修改了什么、为什么这样处理
6. `验证结果`：记录测试、构建、接口、端口或用户操作的结果，以及是否仍有遗留风险

如果问题需要多轮对话、反复复现或多次排查后才能解决，在问题标题最前面加 `*`，并在记录中说明关键排查过程。单轮即可解决的问题不加 `*`。

## 记录规则

- 只追加，不覆盖历史记录；使用 UTF-8 编码。
- 一次问题形成一条独立记录，避免把多个无关问题混在一起。
- 先提炼错误要点，再写解决步骤；错误要点必须让没有上下文的人也能快速理解问题。
- 解决步骤必须对应实际处理过程，不能只写“已修复”；要写明用户如何复现和验证。
- 记录真实证据、命令结果和验证结果；不确定的内容标记为待确认。
- 原始错误仅在有助于搜索或核对时保留一小段，禁止把重复日志、无关堆栈和整屏输出直接粘贴进去。
- 绝不记录 API Key、密码、Token、Cookie 或完整的敏感配置；统一使用 `[已脱敏]`。
- 如果问题最终没有解决，也要记录当前结论、已排查范围和下一步建议。

## 统一记录模板

追加到 `D:\Agent-Team\错误分析.txt` 时使用以下结构：

```text
【YYYY-MM-DD】[可选 *] 问题标题
错误要点：用简短语言总结错误、影响和触发条件。
关键证据：记录最小必要证据，敏感信息必须脱敏。
根因分析：说明已确认原因；未确认内容标记“待确认”。
解决步骤：
1. 检查/复现了什么。
2. 修改/处理了什么，以及原因。
3. 做了哪些防止复发的处理。
验证结果：测试或实际操作结果，附遗留风险。
```

标题前的 `*` 只用于需要多轮对话、反复复现或多次排查后才解决的问题，并在“解决步骤”中保留关键排查转折点。单轮即可解决的问题不加 `*`。

## 工作完成标准

修复 Bug 后，先完成错误要点总结，再按顺序记录解决步骤，最后运行相关测试、构建或接口健康检查并写入错误分析记录。最终回复用户时，简要说明修复结果和对应的错误记录文件。

## 项目最终目标：LangGraph Agent 公司平台

本项目的最终方向是建设一个基于 Python 和 LangGraph 的企业级 Agent 公司平台，而不是只维护一条软件开发工作流。

首期只实现三家公司：

1. 代码开发公司
2. 画展策划公司
3. 电商运营公司

这三家公司是首期业务模板，不是核心代码中的硬编码分支。它们共用同一套 Agent Runtime、LangGraph Orchestrator、工具系统、RAG、权限、审计、并发调度和 Artifact 产物系统。首期不开发金融、法律等其他公司，也不开发公司模板商城。

### 一、总体架构

```text
前端 Studio
  ├─ Company / Workspace / Team 管理
  ├─ Agent 配置
  ├─ Workflow 画布与 YAML
  ├─ 运行队列、日志和 Artifact
  └─ Tool / RAG / 权限配置

平台控制层
  ├─ 身份认证与 RBAC
  ├─ 版本、发布、回滚
  ├─ 全局并发调度与配额
  ├─ 审计和可观测性
  └─ 密钥与策略管理

LangGraph 执行层
  ├─ StateGraph
  ├─ Agent Node
  ├─ Tool Node
  ├─ RAG Node
  ├─ Approval / interrupt
  ├─ Checkpoint / resume
  └─ 条件分支、并行和有限循环

基础适配层
  ├─ Qwen / DeepSeek / OpenAI-compatible Provider
  ├─ MCP / HTTP Tool
  ├─ 本地文档解析与检索
  └─ Workspace 文件和 Artifact
```

### 二、技术基线

- 后端主语言为 Python，使用 FastAPI、LangGraph、asyncio 和 httpx。
- 前端使用 Vue + TypeScript。
- Agent 和 Workflow 使用 YAML/JSON，必须支持输入 Schema、输出 Schema、模型策略、工具权限、知识库权限、超时、重试和 Token 策略。
- 本地默认使用 SQLite；通过 Repository 接口预留 PostgreSQL、Redis 和向量数据库。
- LangGraph 是 Workflow 执行内核；平台自身负责 Company、Workspace、Team、Agent、Tool、RAG、Artifact、权限、审计和全局调度。
- 不同时引入 LangChain、CrewAI 等重复的核心编排框架，除非经过明确评估并记录原因。

### 三、模块职责边界

#### WorkflowExecutor 门面约束

`WorkflowExecutor` 只保留 Run 控制、LangGraph 协调入口和领域服务编排，不再直接拥有 Provider 请求生命周期、Token/Thinking 策略、Artifact 物化、Workspace 文件收集、Architecture 合同编译或 FailureFact 持久化。上述能力必须通过独立服务调用。禁止在同一个类中定义重名方法；结构回归测试会阻止执行器重新增长到历史规模。后续拆分采用渐进委托方式，必须保持现有 API、事件顺序、Checkpoint 和历史 Run 快照兼容。

#### Agent Runtime

负责 Agent 定义、Prompt 组装、输入输出 Schema、模型调用、输出校验、Tool 调用和 RAG 上下文注入。

不负责 Company 管理、用户权限和全局队列，不直接修改其他 Agent 的状态。

#### LangGraph Orchestrator

负责把 Workflow 编译为 StateGraph，处理节点状态、依赖、并行、条件路由、Checkpoint、审批、恢复和节点级重试。

每个 Run 使用独立的 `thread_id`。所有节点输出必须可安全持久化，所有副作用必须可追踪或具备幂等保护。

#### 平台控制层

负责 Company、Workspace、Team、用户、RBAC、版本发布、运行队列、配额、审计和跨 Run 调度。

平台控制层可以限制 Run 启动，但不能绕过 LangGraph 直接修改运行中的 Graph 状态。

#### Provider、Tool、RAG 和 Artifact

- Provider 只负责模型请求和能力适配。
- Tool 只负责外部能力调用。
- RAG 只负责知识导入、索引、检索和引用。
- Artifact 只负责受控 Workspace 中的文件、报告和预览产物。
- 实现 Agent 返回带文件名的源码代码块时，由 Artifact Service 解析为独立文件；前端提供 HTML 沙箱预览、源码查看和下载。查看产物不应再次调用 LLM。

以上模块不得直接改变 Workflow 拓扑。

### 四、核心领域模型

```text
Company
  └─ Workspace
      ├─ Team
      │   └─ Agent
      ├─ Workflow / WorkflowVersion
      ├─ Tool / ToolVersion
      ├─ KnowledgeBase / KnowledgeVersion
      ├─ Run / Checkpoint
      └─ Artifact
```

每个 Agent 至少包含：ID、名称、角色、System Prompt、输入/输出 Schema、Provider、Model、思考强度、Token 策略、超时、重试、Tool 权限、知识库权限和版本信息。

### 五、Workflow 约束

- 可视化画布和 YAML 必须表达同一份 Workflow 定义。
- 支持串行、并行、条件分支、人工审批和有上限的循环。
- 禁止无上限循环和无法判断终点的回路。
- 发布前校验节点 ID、Agent 引用、变量引用、边、环路、Schema 和权限。
- 草稿不能正式运行；Run 必须绑定已发布版本。
- 修改 Workflow 不得改变历史 Run 的版本快照。
- Agent 之间只能通过 Graph State 或明确 Context 传递数据，不允许隐式共享全局变量。

### 六、并发边界

并发必须分为四层：

1. 单个 Run 内的 LangGraph 节点并行。
2. 多个 Run 的平台队列。
3. Company、Workspace、Workflow 的并发配额。
4. Provider/Model 和 Tool 的外部限流。

默认策略：全局最多 8 个活动 Run；单个 Company 最多 3 个；单个 Workspace 最多 2 个；单个 Workflow 默认最多 2 个；单个 Workflow 内默认最多 3 个并行 Agent Node。配额必须可配置，资源不足时进入 `QUEUED`，不能把排队误报为失败。

### 七、运行状态与异常边界

Run 状态至少包括：

```text
QUEUED → RUNNING → WAITING_APPROVAL → RUNNING
                         ├→ SUCCESS
                         ├→ FAILED
                         ├→ STOPPED
                         └→ CANCELLED
```

必须区分配置错误、权限错误、Provider 错误、Tool 错误、RAG 错误、执行错误和资源错误。

- 只有可重试错误才允许自动重试。
- 重试前确认底层异步 HTTP 请求已经真正结束。
- 非幂等 Tool 和文件写入必须有幂等键或恢复保护。
- 取消不能只改变数据库状态，必须确认实际任务已停止。
- Worker 或后端重启后，优先从 Checkpoint 恢复，不重复执行已完成节点。
- 任意异常都必须保留 Run、Node、Attempt 和 Trace 信息。

### 八、Tool、RAG 与代码执行边界

- Tool 首期支持 MCP 和 HTTP/OpenAPI，必须包含输入 Schema、Agent 权限、认证引用、超时、取消、重试、幂等、审计和敏感信息脱敏。
- 高风险 Tool 调用必须经过人工确认。
- RAG 首期支持本地文件导入、解析、切片、索引、检索、引用、权限隔离、版本管理和重新索引。
- Agent 只能检索已授权的知识范围。
- 代码写入和命令执行只能发生在受控 Workspace，默认禁止任意本机脚本和无限制 Shell。

### 九、三家公司首期边界

- 代码开发公司：需求、架构、前端、后端、测试、审查、代码生成、检查和 HTML 预览。
- 代码开发工作流在 Backend、Frontend 前可按需启用 Database Agent；它只负责数据模型、表结构、约束、迁移和初始化数据文件，后续 Agent 必须读取其成果物，不能各自猜测字段。
- 画展策划公司：艺术研究、策展、展览结构、视觉设计、文案和宣传运营。
- 电商运营公司：市场分析、选品、竞品分析、商品详情、视觉营销、广告文案和运营方案。

三家公司不得复制三套执行器，只能通过 Agent、Workflow、Tool、Knowledge Base 和 Artifact 模板体现差异。

### 十、Vibe Coding 判断规则

每次修改前先判断：

1. 需求属于哪个架构层和模块边界。
2. 是否破坏 LangGraph State、Checkpoint、恢复、取消或并发隔离。
3. 是否把行业逻辑错误地写死到某家公司。
4. 是否需要新的版本、权限、审计、Schema 或数据迁移。
5. 是否会产生重复调用、无限循环、数据泄露或不可恢复副作用。

实现时优先扩展公共接口和配置，不复制三套业务逻辑；优先让失败可观察、可恢复、可重试，而不是静默吞错。新增功能必须有测试、构建或明确的手工验收步骤。

### 十一、完成标准

只有同时满足以下条件，功能才算完成：

- 符合本文架构分层和职责边界。
- 不破坏三家公司共用的 LangGraph 执行内核。
- 具备必要的输入校验、异常处理、并发限制和取消恢复能力。
- 版本、权限、日志、审计和敏感信息边界清晰。
- 有测试、构建或实际操作验证。
- 相关 Bug 已按本文错误记录规范写入 `D:\Agent-Team\错误分析.txt`。

### 十二、通用动态 Workflow 原则

本项目不能把某一种行业或某一种任务的 Agent 顺序写死。简单 HTML 的流程只能作为测试模板，不得成为平台默认制度。核心执行器固定的是运行规则，业务 Workflow 的节点、连线、工具和产物类型必须可配置、可扩展、可动态规划。

Workflow 支持以下三种形态：

1. 固定 Workflow：节点和边由已发布的 YAML/JSON 定义，适合标准化、可审计流程。
2. 自适应 Workflow：Planner 根据当前需求，从已注册的 Agent、Tool、RAG 和权限范围中生成候选执行计划，再经过平台校验后编译为本次 Run 的 StateGraph。
3. 递归子 Workflow：复杂任务可以拆成有限深度的子任务或子 Workflow；必须限制最大深度、最大节点数、最大循环次数和总预算。

Planner 的边界：

- Planner 只能引用已注册且当前用户有权限使用的 Agent、Tool 和 Knowledge Base。
- Planner 只能生成候选计划，不能绕过 Schema、权限、版本、预算、环路和安全校验。
- 动态计划必须保存为当前 Run 的不可变快照，历史 Run 不得被重新规划或修改。
- Planner 属于平台控制层或 Orchestrator 能力；Provider、Tool、RAG 和 Artifact 不得直接改变 Workflow 拓扑。
- 用户可以在权限范围内添加、删除和调整 Agent 连线，但发布前必须通过拓扑、依赖、变量、Schema 和权限校验。

### 十三、通用 Token 与上下文制度

增加统一的 `TokenBudgetManager` 和 `ContextPacker`，不得把每个 Agent 的 Token 上限简单当作固定目标。

`TokenBudgetManager` 负责：

- 请求前统计输入 Token、系统 Prompt、历史上下文和预留思考空间。
- 根据 Provider 的 context window、max output、模型能力、Agent 角色和 Run 剩余预算计算实际可用输出。
- Auto 模式按任务复杂度和 Agent 职责动态分配；Manual 模式尊重用户设置，但仍不能超过模型和 Run 的安全上限。
- 分别记录请求上限、实际使用量、剩余 Run 预算、重试消耗和累计费用。失败节点恢复成功后，总量必须继续累计此前失败尝试，不能只展示覆盖后的最终 Step 行；持久化终态事件或 Attempt Ledger 是计费事实来源。
- 预算不足时优先压缩上下文、拆分产物或安排续写，不得静默截断关键结果。Runtime Plan Agent 的低置信度建议不能把普通 Agent 压到不可用的极小预算；源码成果物还要使用文件级预算。

Workflow 可以在需求分析与必要的人工确认后声明可选的 `Runtime Plan Agent`。需求分析先使用确定性的启动预算；Runtime Plan 只规划尚未执行的后续节点，并遵守：

- 只输出经过 Schema 校验的固定 JSON，建议需求难度、模型强度、每个 Agent 的最大输出预算和思考强度。
- Runtime Plan Agent 只能提出建议；最终预算仍由 `TokenBudgetManager` 与 Provider context window、max output、用户单 Agent 上限共同裁决。
- Runtime Plan 不得反向覆盖已经执行完成的 Requirement 预算；架构冻结并形成成果物文件清单后，代码生成还要按文件或片段重新细化预算。
- 评估节点默认使用小预算、关闭或降低思考、不进行重复重试；失败时必须切换到确定性的本地预检，不得阻断业务 Workflow。
- Runtime Plan Agent 的预算、重试和思考配置属于平台硬限制，普通 Run 参数不得覆盖。
- Provider 能力必须按传输接口与具体模型族共同识别；界面显示的实际档位必须对应真正发送到云端的脱敏参数，不能只显示执行器意图。
- 若一次响应将输出额度耗尽在 reasoning 且没有正文，下一次重试必须真正降低思考参数；无法降低或参数未变化时立即熔断。
- Requirement 不得把行业常见功能擅自升级为用户必做范围，必须区分确认范围、最小假设、可选建议和待确认问题。
- 评估建议与 Architecture 的结构化复杂度判断冲突时，优先保留能避免输出截断的安全预算，并记录合并依据。
- 该能力属于通用 Runtime 接口，通过 Workflow 配置启用，不得在核心执行器中写死代码公司、画展公司或电商公司的 Agent 名称。

`ContextPacker` 负责：

- 将 Agent 结果转换为结构化字段、摘要、引用和 Artifact，而不是无条件传递全部历史文本。
- 按下游 Agent 的 `input_schema` 选择必要上下文。
- 始终优先保留原始需求、关键决策、约束、验收标准、权限信息和错误状态。
- 对超长内容进行可追踪的压缩，保留原始 Artifact 和摘要来源，禁止不可追溯地覆盖原文。
- 防止 Agent 通过隐式全局变量共享状态；所有上下文必须来自 Graph State、Context Packet 或明确的 Artifact 引用。

### 十四、通用输出完整性与续写制度

Agent 输出不能只通过“content 非空”判断成功。Runtime 必须综合检查：

- `finish_reason`
- `message.content`
- `reasoning_content`
- usage 和实际 Token
- 输出 Schema
- JSON、HTML、代码或文档的结构完整性
- 任务所需的必填字段和完成条件

统一输出状态至少支持：

```text
GENERATING → VALIDATING → SUCCESS
                  ├→ CONTINUING → VALIDATING
                  ├→ REPAIRING → VALIDATING
                  └→ FAILED
```

执行规则：

- `finish_reason=stop` 且输出校验通过，才允许标记 Agent 为 SUCCESS。
- `finish_reason=length` 即使有 content，也必须标记为“可能截断”，不能直接视为完整成功。
- 已经产生部分内容时，优先从明确边界继续生成并合并，不得无条件从头重试。
- JSON、代码、HTML 和长文档应优先按结构、章节或文件 Artifact 分块生成，再由服务端合并和校验。
- 空输出、格式错误和不可安全续写的结果，必须在确认底层请求结束后再进行可控重试。
- 前端必须区分生成中、续写中、校验中、可能截断、修复中、成功和失败，不得用一个成功状态掩盖不完整产物。

### 十四点一、成果物优先生成

当 Agent 需要输出代码、文件、长文档或其他可下载产物时，优先通过 Workflow 的 `generation_mode` 选择生成策略：

- `single`：适合短文本、判断、需求分析和总结；单次响应完成，达到长度上限就明确标记不完整。
- `artifacts`：先生成受 Schema 约束的文件清单，再为每个文件单独生成；文件之间通过文件名合同关联。
- `auto`：由通用运行时根据输出格式、用户是否明确要求代码成果物和当前任务上下文选择 `single` 或 `artifacts`，不得绑定某个行业或固定文件名。

分块成果物模式不得把上一轮完整回复作为“续写上下文”重复发送。每个文件必须单独校验，并根据文件估算和 Provider 上限独立申请输出空间；较大的文件还必须优先拆成有明确顺序的源码片段任务，逐段生成、合并和校验。文件遇到 `finish_reason=length` 时，先保留已生成正文，只请求从截断位置开始的缺失后缀并在服务端合并，禁止把整个文件再次作为修复结果生成。Runtime Plan Agent 给整步的建议预算只是规划参考，不能成为每个文件或片段的过小硬上限。失败时先只对当前文件尾部或当前片段做一次有边界的定点修复；分块和修复仍无法完成时，才允许进入最后一级 continuation 兜底，并记录续写次数和原因。中间文件写入 Run State 的结构化 Artifact 列表，最终由 Artifact Service 安全落盘，前端提供预览、源码和下载。

### 成果物归档与日志交互

- Run 完成后，Artifact Service 必须把当前 Run 的全部已登记成果物统一提供为一个 ZIP 下载；压缩包只允许包含经过路径校验的成果物文件，不把 ZIP 自身登记为成果物，也不能因此再次调用 LLM。存在多个成果物 Owner 时，归档必须按 `owner_step` 建立项目目录，已带 Owner 前缀的路径不得重复加前缀；禁止把旧下载 ZIP 递归打入新 ZIP。
- Agent 运行日志必须跟随对应节点展开，支持原生文本选中复制，并提供复制当前 Agent 全部日志的快捷操作；不得依赖固定侧栏或弹窗才能查看日志。

### 成果物真实验证门禁

- 测试 Agent 不得仅凭 LLM 文本判断成果物可运行。代码成果物必须经过通用 `ArtifactValidator` 的确定性检查。
- 验证器从 Run 的结构化成果物识别技术栈，执行受控的目录/入口/依赖检查、前端构建、后端编译和启动 HTTP 探测；命令必须使用参数数组执行，禁止把模型返回文本当作 Shell 命令。
- 验证任务必须在临时隔离 Workspace 中执行，使用随机端口，继承环境时移除 API Key、Token、Secret、Password、Cookie 和 Authorization 等敏感变量，并设置命令超时、启动超时、取消和子进程清理。
- Tester Agent 可以读取验证器的结构化检查结果并负责解释失败、提出修复建议；验证器的 `passed/failed/blocked` 结果才是最终门禁依据。
- `Agent SUCCESS`、`Artifact 已生成`、`Artifact 已验证` 和 `可交付` 必须在日志和状态上区分。只要存在失败或阻塞检查，Tester 和最终 Workflow 不得显示 SUCCESS。
- 验证失败不等于删除现场：若 Run 已产生结构化文件，失败收尾前仍要物化并保留当前 Artifact，方便用户下载失败现场、Tester 解释问题和后续定点修复；保存失败不能覆盖原始验证错误。
- Workflow 配置允许声明有限次数的自动修复。每次修复必须以真实构建输出为依据，优先使用文件名和行号选择最小目标；模块导入/导出不一致时优先只修复导出方，不得无依据地同时重写相互依赖的多个文件。修复后的 Artifact 以 `(owner_step, path)` 为唯一身份覆盖旧版本，并重新执行全部确定性门禁；只有复验通过才能进入 Reviewer。
- 前后端单独构建通过后，还应继续增加 API 契约、服务健康检查和关键用户流程的集成验证；不能把单端构建通过等同于前后端集成通过。
- 有数据库的 Spring Boot 成果物默认使用隔离 H2 内存数据库测试，保留生产数据库配置但不能依赖用户本机数据库；检查 H2 和 JPA 源码/依赖一致性，执行 Maven test、启动及 CRUD 实际数据闭环。
- 整改必须基于现有真实源码与冻结合同，不能整套重生成导致目录、API、依赖或公开方法漂移。单文件修复未通过后，可升级为有依据的关联源码整改，禁止删除测试或弱化合同。
- 候选整改在独立上下文和临时工作区暂存，全部门禁通过后才提交；失败、阻塞、取消或异常不得覆盖已提交成果物。验证进展按结构/依赖、构建/测试、启动、CRUD 阶段判断，不把错误指纹变化等同于进展。
- HTTP 500 缺陷包必须附带后端异常根因日志；长日志保留关键根因链。已验证 Artifact 按 owner revision 合并，过期 Checkpoint 和并行状态不得恢复旧版本文件。
- H2 初始化路线必须按实际依赖选择：存在迁移脚本且有 Flyway 执行依赖才启用迁移；没有执行器但有 schema.sql 时走 Spring SQL init，先建表再进行 JPA validate。仅有迁移脚本而无执行器时必须报依赖合同错误，不能禁用初始化后继续校验空库。禁止生成 flyway-database-h2，H2 的 Flyway 支持属于 flyway-core。
- 启动检查必须优先从真实 Controller 提取可探测的 GET 路由，而非只猜测默认健康地址；记录各探测端点的 HTTP 状态，区分无响应、进程退出、404 路由缺失及权限/接口错误。服务健康不等于页面可交付，Thymeleaf 模板必须有渲染依赖与 MVC 视图处理，纯 HTML+REST 应交付 static 页面。CRUD 样例需遵守 JsonProperty 的真实 JSON 字段名，不得因验证器字段错误误报业务失败。

### 模型 Provider 与能力边界

- Provider 只描述账户/传输协议，不能单独决定模型能力；所有云端模型必须经过统一的 `ModelProfile` 能力层，按模型族和 Base URL 解析思考能力、支持档位、最大输出和请求参数协议。
- 平台内部只使用可移植的 `off / low / high / max`，由协议适配器映射为目标服务商字段；未知模型默认关闭思考控制，不得猜测或发送其他厂商私有参数。
- 前端必须支持内置 Provider 和 `OpenAI Compatible（自定义）`；模型名称可以动态输入。若模型族与 Base URL 明显不匹配，必须在测试/保存前给出配置警告，并把实际配置写入日志。
- GLM、Qwen、DeepSeek 等适配逻辑必须集中在模型能力/协议边界，不能散落在 Executor 或业务 Workflow 中；新增模型应通过能力注册扩展，不修改核心调度器。
- ChatGPT 本地登录必须作为独立的 Local Codex Runner Provider：通过本机 codex exec --json --ephemeral --sandbox read-only 调用，不读取或上传 Session/OAuth 文件，不把本地登录态伪装成云端 API Key。该 Provider 的 Token 上限和思考档位只能标记为软控制，日志必须说明实际由本地 Codex 默认策略决定。

### 十四点二、Runtime Plan 控制节点容错

- `token_estimator` 是可选的控制平面优化节点，不是业务 Agent 的前置依赖；它失败时不得阻断后续工作流。
- Runtime Plan 必须先尝试解析完整 JSON；若 Provider 只截断了 JSON 后的尾部说明，但 JSON 合同本身完整，可以直接采用，不得触发续写。
- JSON 合同无法解析、输出为空或被推理内容占满时，必须使用本地需求难度预检生成安全预算，并将原因、预算来源和置信度记录到 Run 日志。
- 本地兜底不得再次调用 LLM，也不得把 `token_estimator` 显示为红色失败；控制节点应以“已使用本地预算兜底”完成，同时保留 Provider 实际响应明细供审计。
- Runtime Plan 的 800 Token 小预算只用于控制 JSON；它不能成为下游文件或长文本 Agent 的硬上限。下游预算仍需结合难度、模型上限、上下文窗口和用户配置计算。
- 对 `output_format=json` 的关键结构化节点，若第一次响应只有推理没有正文，允许一次不计入用户配置的自动降级恢复（例如 `low` → `off`）；仍无正文时，只有声明 `failure_policy: fallback` 的节点才能使用确定性保守结果。
- 结构化兜底只适用于 Provider 已返回响应但正文不完整/不可解析的情况；HTTP 鉴权、连接、模型不存在等传输或配置错误必须继续失败并提示用户。

### 十五、通用实现取向

后续 Vibe Coding 必须优先遵循以下取向：

1. 固定平台能力，动态业务流程；不为某个 HTML、代码、画展或电商示例添加核心特判。
2. 通过 Agent、Workflow、Tool、RAG、Artifact 和策略配置扩展公司能力，不复制多套执行器。
3. 复杂度由输入和运行时状态决定，不预设所有任务都使用相同节点、相同顺序或相同 Token。
4. 所有动态行为必须可观察、可审计、可恢复、可取消，并能还原本次 Run 的实际计划。
5. 任何新增能力都要同时考虑输入 Schema、输出 Schema、上下文预算、并发、权限、版本、失败重试和产物完整性。

### 十五点一、动态 Skill 编排边界

Skill 是 Agent Runtime 的可配置增强层，不是替代 Agent 职责、Tool 权限或 RAG 的另一套执行器。后续 Vibe Coding 必须遵守：

- Agent 的稳定角色、输入输出契约保留在 `agents/*.yaml`；Workflow Step 只声明候选 Skill，不能把某一个行业或客户任务硬编码进核心执行器。
- Skill 以本地 `skills/<skill-id>/SKILL.md` 注册，正文只作为本次模型请求的受限系统提示词附加内容，不能执行代码、读取密钥、绕过 Tool/RAG 权限或直接修改 Workflow 拓扑。
- 每次 Run 允许使用 `Auto / On / Off` 全局策略；`Auto` 必须以 RequirementSpec、能力标签和确定性难度规则选择，不能额外调用 LLM 来决定是否启用，也不能让简单任务无故增加上下文和 Token。
- `On` 仅加载该 Step 已声明且已注册的候选项，必须设置每 Step 数量上限和每项注入上限；缺失、跳过、版本、注入量和解析模式必须记录到 Run 审计与节点日志。
- `Off` 只能关闭提示词增强，不能关闭输入输出 Schema、能力路由、Artifact 完整性校验、超时、取消、重试、权限或安全保护。
- 新增 Skill 必须有适用 Step / 能力 / 最低复杂度说明和回归测试；Skill 的配置与选择结果要随 Run 快照持久化，使恢复、复盘和审计能重现实际执行计划。

### 十六、需求路由保护

Architecture 负责设计项目类型，但不能独自决定是否删除实现分支。执行器必须同时读取用户原始需求并执行确定性路由校验：

- 学生、用户、订单、商品、库存等业务管理系统，只要出现 CRUD、增删改查或业务数据管理，默认保留 Backend 与 Frontend。
- “简单”“Demo”“MVP”只影响复杂度、Token 预算和任务拆分，不得单独成为跳过 Backend 的理由。
- 只有用户明确说明纯前端、无需后端、浏览器本地存储或 `localStorage` 时，才允许走 `static_html` 并跳过 Backend。
- Architecture 返回的项目类型、`backend_required` 与原始需求冲突时，必须校正为安全路线，并在事件日志中记录原始判断、命中信号、校正原因和最终策略。
- 需求不明确时保留可能需要的实现分支，不得因为“没有写 API/数据库”就推断为无需 Backend；不确定性应记录为风险，交给人工确认。

这条规则是通用平台能力，不属于代码、画展或电商公司的业务硬编码。新增公司模板必须复用同一套路由保护、Schema 校验、日志和审计机制。

路由实现应以能力而不是行业为中心，至少支持 `frontend`、`backend`、`persistence`、`authentication`、`external_api`、`tool`、`rag`、`artifact`、`human_approval` 和 `async`。Architecture 可以提出 `required_capabilities`、`optional_capabilities`、`evidence`、`needs_clarification` 和 `clarification_questions`，但 Executor 必须合并原始需求证据后再决定是否跳过步骤。

企业级实现必须保持以下代码边界：`workflow/requirements.py` 定义 `RequirementSpec`，`workflow/capability_router.py` 负责需求能力路由，`workflow/architecture_validator.py` 负责 Architecture 决策校验；Executor 只消费校验后的结果。旧版 `adaptive.py` 函数仅作为兼容层，新增功能不得继续把路由规则堆回 Executor。

## 可靠交付闭环（代码公司当前必守准则）

- 新 Run 必须使用本次原始需求、独立状态和工作流快照；不得借用历史成功状态证明新需求成功。
- 在 Architecture 完成、人工审批前冻结 `DeliveryContract` 与 SHA-256。合同统一技术栈、入口、API 方法/JSON 字段/合法样例、实体 Java/SQL 类型、数据库初始化及浏览器验收约定。具体能力通过 Workflow `meta.delivery_contract` 启用，不将合同模块绑定某个行业。
- 原始需求与冻结合同禁止上下文截断；输入窗口不足要明确报错，不能删掉约束后继续。常见等价 Schema（字段类型映射与字段名数组、集合与标准 /{id} 路由）可以确定性归一化；不可解析的类型必须确认，禁止猜测后报绿。
- 普通 Spring Boot + HTML 默认 static 资源 + 同源 REST；只有明确要求模板引擎时采用模板。联调默认 H2；数据库 Java/SQL 类型与接口字段必须一致。Vue 代理支持通过环境变量注入隔离后端地址。
- 测试节点执行真实构建/测试、启动、HTML/资源 HTTP、H2 CRUD 与浏览器渲染；CRUD 页面使用稳定 data-testid，真实操作新增、编辑、删除并检查列表。API 健康响应不能替代页面验收；缺工具、禁用或缺失必要验证应 blocked/FAILED，不能 SUCCESS。
- 失败回到责任 Agent 定点/关联源码整改，不整套重新设计。候选隔离、有限轮数、复验与进度判定；只有全部通过才提交；无进展回滚并保留根因、原文件和失败现场。允许补充被合同明确要求的缺失文件，不得删除测试绕过门禁。
- 交付前核对冻结合同哈希、被验证的源码指纹、完整 ZIP 内容与 CRC。Reviewer 文本不构成验证证据，测试后源文件变更必须重新验证。源码按原始 UTF-8 字节写入，不在打包时裁剪空白或混入未登记历史文件。
- ZIP 含全部本次源码及 `delivery-report.json`，报告记录合同、验证明细和门禁结果。“执行完成”与“可交付”明确分离；必需门禁失败则 Run FAILED。
- 发布变更必须跑平台回归，并用隔离工作区从新需求进行 Spring Boot + HTML、Spring Boot + Vue、静态 HTML 的真实模型回归。支持重复轮次，记录首次成功率、最终可交付率、耗时、Token、整改次数及失败证据；不得只复测人工修复副本。
- 当前受控命令/临时目录不等于 Docker 安全沙箱。多租户权限、资源配额、完整安全隔离及生产并发压测仍是最终目标，不能因本地通过就宣称已全部企业级。
## Enterprise Architecture Baseline (2026-09-19)

- Keep the existing YAML Workflow + LangGraph executor; extend through platform contracts instead of rewriting the runtime.
- Requirement Clarification is a single gate before Architecture for delivery-enabled workflows. It persists Requirement State, ClarificationRequest, Assumption Log and resumes from the same checkpoint.
- Project Blueprint is the single source of truth for stack, database, API, entrypoints, ports, ownership and delivery requirements. Freeze it after approval; later changes require an explicit change request.
- Deterministic validators are the evidence authority. Persist EvidenceRecord and FailureFact, route repair to the owning Agent/file, re-run targeted gates, then full regression.
- Run data separates execution_status from delivery_status and retains Artifact Candidate/Stable/Rejected version history.
- Platform Runtime owns model budgets, provider limits, retry/timeout policy and future queue/rate-limit/model escalation. Agents remain industry-neutral YAML definitions.
- Long-running Runs must not depend on browser visibility or the FastAPI process lifetime. SQLite/PostgreSQL is the durable source of truth; Redis Streams may provide queueing, live fan-out, fenced leases, controls, and Worker heartbeats, but Redis must not become the sole store for Run, Artifact, Checkpoint, audit, or secrets.
- API and Worker responsibilities stay separated in distributed mode. A Worker must hold a renewable per-Run fencing token before model or Tool execution; lease loss cancels the underlying operation, persists recoverable state, and permits bounded takeover without marking the Run successful or failed prematurely.
- Frontend recovery persists the active Run and last event cursor across reload/minimize. It reloads the authoritative Run snapshot before reconnecting SSE, deduplicates by event ID, and exposes paginated historical Runs instead of treating browser memory as the execution record.
- Provider attempts and Worker lease transitions are append-only audit facts. Health checks must distinguish “API reachable” from “Redis reachable and at least one Worker active”; startup scripts and production probes fail visibly when the queue has no consumer.
- New company templates must supply Agents, Workflows, Tools, RAG and Artifact rules only; they must not add company-specific branches to the core executor.

## 受控 Agent 协商边界

- 跨 Agent 讨论只在多责任域故障或合同冲突等确有必要时触发；正常工作流仍按冻结合同并行执行。
- 消息必须关联 Run、错误指纹、发送方、接收方、轮次和类型，持久化并防重；由平台计数限制轮次、耗时和 Token，不能依赖模型自报跳数。
- Agent 可以提出、复核修复建议，不能自行改冻结 Blueprint、其他 Agent 的文件或 Workflow 拓扑。提出合同变更时只记录提议，不隐式采纳。
- 协商完成不等于交付成功；责任 Agent 的候选修改仍须通过目标 Gate 与完整回归，失败继续保留现场并按原策略熔断。
- 这层能力应保持通用，由 Workflow 配置启用，不绑定代码公司的特定 Agent 名称。

## 代码公司契约驱动实施准则

- `RequirementSpec` 记录明确业务实体、技术栈、能力和缺口；高影响缺口先澄清，低影响缺口写入 Blueprint 假设。用户明确约束优先于 Architecture 草案。
- 审批后冻结 ProjectBlueprint；ContractCompiler 从它编译 Entity/API、Dependency Manifest、Artifact Ownership、File Dependency DAG 与角色合同。多实体 API 必须显式给出 `entity_id`，禁止靠 URL 猜测。
- Database、Backend、Frontend 独立持有自己的可写文件范围和角色合同；生成前补齐计划中缺失的文件并按依赖拓扑排序。平台生成基础依赖文件，Agent 不得自行改核心依赖。
- Tester 必须等待三个实现分支（含被明确跳过的分支）结束。IntegrationGate 先检查合同哈希、文件归属、文件依赖、前端 API、后端路由、表单字段、导入及 SQL 表字段，再执行真实构建/启动/浏览器验收。门禁失败要标责任 Agent、保留证据并走有限轮定点修复。
- 本地 H2 验证只是可重复联调；外部数据库、生产部署、多租户隔离、任意技术栈和真实 Provider 全场景成功率均需另行验证，不得把单元测试通过写成这些能力已完成。
