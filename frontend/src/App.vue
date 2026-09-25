<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, toRaw } from 'vue'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { Handle, Position, VueFlow, type Edge, type Node } from '@vue-flow/core'
import {
  Activity,
  ArrowDownToLine,
  Bot,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Clock3,
  CloudCog,
  FileText,
  GitBranch,
  Layers3,
  LoaderCircle,
  KeyRound,
  Pause,
  Play,
  PlugZap,
  RotateCcw,
  ShieldCheck,
  Settings2,
  Sparkles,
  Square,
  Terminal,
  UserRoundCheck,
  X,
  Zap,
} from 'lucide-vue-next'

type Status = 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'SKIPPED' | 'WAITING_APPROVAL' | 'WAITING_CLARIFICATION'
type LogItem = { time: string; type: string; message: string; tone?: string; stepId?: string; detail?: string }
type AgentOutput = { id: string; name: string; role: string; status: Status; output: string; duration?: number; retryCount?: number; agentId?: string }
type Artifact = { id: string; name: string; mimeType: string; sizeBytes: number; contentUrl?: string; previewable?: boolean }
type ArtifactViewer = { artifact: Artifact; mode: 'preview' | 'source'; content: string; loading: boolean; error: string }
type ActiveRunConflict = { runId: string; status: string; requirement: string }
type Locale = 'en' | 'zh'
type WorkflowSummary = { id: string; name: string; stepCount?: number; concurrency?: number; detail?: string; active?: boolean }
type ProviderKey = 'qwen' | 'deepseek' | 'glm' | 'openai_compatible' | 'chatgpt_local'
type ThinkingMode = 'auto' | 'off' | 'low' | 'high' | 'max'
type BudgetMode = 'auto' | 'manual'
type RuntimeSettings = { maxTokens: number; retry: number; thinking: ThinkingMode }
type LocalCodexModel = { value: string; label: string }
type AgentTokenUsage = { stepId: string; agentId: string; status: string; inputTokens: number; outputTokens: number; totalTokens: number; retryCount: number }
type RepairMetrics = { firstPass: boolean; repairRounds: number; successfulRepairs: number; automaticRepairRate: number; averageRepairRounds: number; circuitBreaks: number }
type StageMetric = { stage: string; durationMs: number; inputTokens: number; outputTokens: number; totalTokens: number }
type RepairTrace = { key: string; stepId: string; attempt: number; location: string; owners: string[]; status: string; result: string; files: string[] }
type RunStats = { activeDurationMs: number; approvalDurationMs: number; inputTokens: number; outputTokens: number; totalTokens: number; byAgent: AgentTokenUsage[]; repair: RepairMetrics; stages: StageMetric[]; repairTrace: RepairTrace[] }
type ErrorSummary = { title: string; reason: string; suggestion: string }
type RunHistoryItem = { id: string; workflow_id: string; status: string; execution_status?: string; delivery_status?: string; requirement?: string; started_at?: string; finished_at?: string; duration_ms?: number; recovery_count?: number; error_message?: string }

const API_BASE = '/api'
function cloneValue<T>(value: T): T {
  return structuredClone(toRaw(value))
}
const demoRequirement = ''
const providerPresets: Record<ProviderKey, { baseUrl: string; modelName: string }> = {
  qwen: { baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', modelName: 'qwen-plus' },
  deepseek: { baseUrl: 'https://api.deepseek.com', modelName: 'deepseek-v4-pro' },
  glm: { baseUrl: 'https://open.bigmodel.cn/api/paas/v4', modelName: 'glm-5.2' },
  openai_compatible: { baseUrl: '', modelName: '' },
  chatgpt_local: { baseUrl: '', modelName: 'gpt-5.6-luna' },
}
const localCodexModels: LocalCodexModel[] = [
  { value: 'gpt-5.6-sol', label: 'GPT-5.6 Sol' },
  { value: 'gpt-5.6-terra', label: 'GPT-5.6 Terra' },
  { value: 'gpt-5.6-luna', label: 'GPT-5.6 Luna' },
  { value: 'gpt-5.5', label: 'GPT-5.5' },
]
const defaultLocalCodexModel = 'gpt-5.6-luna'
const localCodexModelValues = new Set(localCodexModels.map((model) => model.value))

function normalizeLocalCodexModel(value: unknown) {
  const model = String(value ?? '').trim().toLowerCase()
  return localCodexModelValues.has(model) ? model : defaultLocalCodexModel
}
const locale = ref<Locale>('zh')
const translations: Record<Locale, Record<string, string>> = {
  en: {
    workspace: 'Workspace', runs: 'Runs', workflows: 'Workflows', agentLibrary: 'Agent library', mockProvider: 'Mock provider', localDemoReady: 'Local demo is ready', builderAccess: 'Builder access',
    apiConnected: 'API connected', demoMode: 'Demo mode', ready: 'Ready', running: 'Running', waitingApproval: 'WAITING_APPROVAL', success: 'SUCCESS', failed: 'FAILED', stopped: 'STOPPED',
    runCanvas: 'Workflow run / live canvas', canvasDescription: 'One brief in. A reviewed implementation plan out.', stepsComplete: 'steps complete', runDuration: 'run duration', executionGraph: 'Execution graph', nodesLevels: '8 nodes · 7 levels', autoLayout: 'Auto layout', export: 'Export',
    pending: 'Pending', approval: 'Approval', runBrief: 'Run brief', input: 'INPUT', concurrency: 'Concurrency', maxParallel: 'max parallel agents', eventStream: 'Event stream', runActivity: 'Run activity', live: 'LIVE',
    inspector: 'Inspector', nodeDetail: 'Node detail', purpose: 'Purpose', agentContract: 'Agent contract', agentId: 'Agent ID', inputSources: 'Input sources', outputKey: 'Output key', agentOutput: 'Agent output', captured: 'captured', retryPolicy: 'Retry policy', attempts: '2 attempts', exponentialBackoff: 'exponential backoff', outputWillAppear: 'Output will appear here', selectAfterComplete: 'Select a node after it completes.',
    apiSettings: 'API settings', cloudApiDescription: 'Connect every Agent to your cloud model.', provider: 'Provider', baseUrl: 'Base URL', model: 'Model', apiKey: 'API key', apiKeyConfigured: 'Key configured', keyNotConfigured: 'Key not configured', keyPlaceholder: 'Paste your cloud API key', apiSecretNote: 'The key is sent only to this backend and stored in backend/.env.', testConnection: 'Test connection', saveConfiguration: 'Save configuration', testing: 'Testing…', connectionSuccess: 'Connection successful', testFailed: 'Connection failed', mockProviderNote: 'Mock mode is active until a real key is saved.', approving: 'Confirming…', approvalFailed: 'Approval failed', close: 'Close',
    architectureReady: 'Architecture is ready', unlockAgents: 'Your decision unlocks 2 parallel agents.', approveArchitecture: 'Approve architecture', reject: 'Reject', commandCenter: 'Command center', statePersisted: 'State is persisted per run', yamlSource: 'YAML is the source of truth', reset: 'Reset', stopRun: 'Stop run', runWorkflow: 'Run workflow', runAgain: 'Run again',
    budgetPlanning: 'Token budget planning', productAnalysis: 'Product analysis', systemDesign: 'System design', implementationPlan: 'Implementation plan', qualityGate: 'Quality gate', finalReview: 'Final review', humanGate: 'Human in the loop', skipped: 'Skipped',
    nodeTokenEstimator: 'Token estimator', nodeRequirement: 'Requirement', nodeArchitecture: 'Architect', nodeApproval: 'Architecture approval', nodeBackend: 'Backend', nodeFrontend: 'Frontend', nodeTester: 'Tester', nodeReviewer: 'Reviewer',
    workflowSoftware: 'Software development', workflowDetail: '7 agents · 1 approval', workflowProduct: 'Product discovery', workflowProductDetail: '4 agents · draft',
  },
  zh: {
    workspace: '工作区', runs: '运行记录', workflows: '工作流', agentLibrary: 'Agent 库', mockProvider: 'Mock Provider', localDemoReady: '本地演示已就绪', builderAccess: '构建者权限',
    apiConnected: 'API 已连接', demoMode: '演示模式', ready: '就绪', running: '运行中', waitingApproval: '等待确认', success: '已完成', failed: '失败', stopped: '已停止',
    runCanvas: '工作流运行 / 实时画布', canvasDescription: '输入一条需求，得到经过审查的实现方案。', stepsComplete: '步骤完成', runDuration: '运行耗时', executionGraph: '执行图', nodesLevels: '8 个节点 · 7 个层级', autoLayout: '自动布局', export: '导出',
    pending: '待运行', approval: '需确认', runBrief: '运行需求', input: '输入', concurrency: '并发数', maxParallel: '最大并行 Agent', eventStream: '事件流', runActivity: '运行动态', live: '实时',
    inspector: '检查器', nodeDetail: '节点详情', purpose: '职责', agentContract: 'Agent 合约', agentId: 'Agent ID', inputSources: '输入来源', outputKey: '输出键', agentOutput: 'Agent 输出', captured: '已捕获', retryPolicy: '重试策略', attempts: '最多 2 次', exponentialBackoff: '指数退避', outputWillAppear: '输出将在这里出现', selectAfterComplete: '节点完成后选择它查看结果。',
    apiSettings: 'API 配置', cloudApiDescription: '让所有 Agent 接入你的云端模型。', provider: 'Provider', baseUrl: '接口地址', model: '模型', apiKey: 'API Key', apiKeyConfigured: 'Key 已配置', keyNotConfigured: 'Key 未配置', keyPlaceholder: '粘贴云端 API Key', apiSecretNote: 'Key 只会发送到本地后端，并保存到 backend/.env。', testConnection: '测试连接', saveConfiguration: '保存配置', testing: '测试中…', connectionSuccess: '连接成功', testFailed: '连接失败', mockProviderNote: '保存真实 Key 后才会退出 Mock 模式。', approving: '确认中…', approvalFailed: '确认失败', close: '关闭',
    architectureReady: '架构方案已就绪', unlockAgents: '确认后将解锁 2 个并行 Agent。', approveArchitecture: '确认架构', reject: '拒绝', commandCenter: '命令中心', statePersisted: '状态按 Run 持久化', yamlSource: 'YAML 是唯一事实来源', reset: '重置', stopRun: '停止运行', runWorkflow: '运行工作流', runAgain: '再次运行',
    budgetPlanning: 'Token 预算评估', productAnalysis: '需求分析', systemDesign: '系统设计', implementationPlan: '实现方案', qualityGate: '质量门禁', finalReview: '最终审查', humanGate: '人工确认', skipped: '已跳过',
    nodeTokenEstimator: 'Token 预算评估', nodeRequirement: '需求分析', nodeArchitecture: '架构设计', nodeApproval: '架构确认', nodeBackend: '后端方案', nodeFrontend: '前端方案', nodeTester: '测试方案', nodeReviewer: '最终审查',
    workflowSoftware: '软件开发', workflowDetail: '7 个 Agent · 1 次确认', workflowProduct: '产品探索', workflowProductDetail: '4 个 Agent · 草稿',
  },
}

translations.en.liveLogs = 'Live logs'
translations.en.openLogs = 'Open logs'
translations.en.closeLogs = 'Close logs'
translations.en.clearLogs = 'Clear'
translations.en.existingRunAttached = 'Existing run attached · continue it or stop it before starting another.'
translations.en.differentActiveRun = 'Current request was not started · another request is still running. Stop that run before starting this one.'
translations.en.noLogs = 'No events yet'
translations.en.events = 'events'
translations.en.requestFailed = 'Request failed'
translations.en.backendUnavailable = 'Backend did not return a JSON response. Start FastAPI on port 3456.'
translations.en.agentLogs = 'Agent logs'
translations.en.openAgentLogs = 'View run logs'
translations.en.noAgentLogs = 'No run events for this Agent yet'
translations.en.deepseek = 'DeepSeek'
translations.en.glm = 'GLM (Zhipu AI)'
translations.en.openaiCompatible = 'OpenAI Compatible (custom)'
translations.en.chatgptLocal = 'ChatGPT (local Codex login)'
translations.en.localCodexNote = 'Uses the Codex CLI already logged in on this computer. No API key is required.'
translations.en.providerConfigurationWarning = 'The selected model and Base URL may not belong to the same provider.'
translations.en.runtimePolicy = 'Run policy'
translations.en.maxTokens = 'Max output tokens'
translations.en.retryCount = 'Retry count'
translations.en.thinkingLevel = 'Thinking level'
translations.en.effectiveLevel = 'Effective level'
translations.en.autoMode = 'Auto'
translations.en.thinkingOff = 'Off'
translations.en.unsupportedThinking = 'This model does not support that level; it will be downgraded automatically.'
translations.en.providerDefault = 'Provider default'
translations.en.qwen = 'Qwen'
translations.en.providerAuthFailed = 'API Key was rejected. Check the key, region, plan, and matching Base URL.'
translations.en.budgetMode = 'Token budget mode'
translations.en.budgetAuto = 'Auto (adaptive by task)'
translations.en.budgetManual = 'Manual per Agent'
translations.en.budgetAutoNote = 'Your value is the safety cap; the system allocates per Agent and complexity.'
translations.en.budgetManualNote = 'Each Agent uses its configured output limit.'
translations.en.downloadAllZip = 'Download all ZIP'
translations.en.downloadingZip = 'Preparing ZIP…'
translations.en.archiveDownloadFailed = 'ZIP download failed'
translations.en.runStats = 'Run stats'
translations.en.activeDuration = 'Active run time'
translations.en.approvalWait = 'Approval wait excluded'
translations.en.totalTokens = 'Total tokens'
translations.en.inputTokens = 'Input tokens'
translations.en.outputTokens = 'Output tokens'
translations.en.noTokenUsage = 'No token usage yet'
translations.en.perAgentUsage = 'Per-Agent usage'
translations.en.closeStats = 'Close run stats'
translations.en.stopExistingRun = 'Stop existing run'
translations.en.repairMetrics = 'Repair quality'
translations.en.firstPass = 'First pass'
translations.en.autoRepairRate = 'Auto-repair rate'
translations.en.repairRounds = 'Repair rounds'
translations.en.averageRepairRounds = 'Average rounds'
translations.en.stageMetrics = 'Stage cost'
translations.en.repairTrace = 'Failure and repair trace'
translations.zh.runStats = '运行统计'
translations.zh.activeDuration = '有效运行耗时'
translations.zh.approvalWait = '已排除架构确认时间'
translations.zh.totalTokens = 'Token 总量'
translations.zh.inputTokens = '输入 Token'
translations.zh.outputTokens = '输出 Token'
translations.zh.noTokenUsage = '暂无 Token 消耗记录'
translations.zh.perAgentUsage = '各 Agent 消耗'
translations.zh.closeStats = '关闭运行统计'
translations.zh.repairMetrics = '修复质量'
translations.zh.firstPass = '首次成功'
translations.zh.autoRepairRate = '自动修复率'
translations.zh.repairRounds = '修复轮次'
translations.zh.averageRepairRounds = '平均修复轮数'
translations.zh.stageMetrics = '阶段耗时与 Token'
translations.zh.repairTrace = '失败与修复链路'
translations.en.mainError = 'Main error'
translations.en.errorReason = 'Reason'
translations.en.errorSuggestion = 'Suggested action'
translations.en.noMainError = 'No main error'
translations.zh.mainError = '主要报错'
translations.zh.errorReason = '主要原因'
translations.zh.errorSuggestion = '处理建议'
translations.zh.noMainError = '暂无主要报错'
translations.zh.stopExistingRun = '\u505c\u6b62\u65e7\u8fd0\u884c'
translations.zh.liveLogs = '\u5b9e\u65f6\u65e5\u5fd7'
translations.zh.openLogs = '\u6253\u5f00\u65e5\u5fd7'
translations.zh.closeLogs = '\u5173\u95ed\u65e5\u5fd7'
translations.zh.clearLogs = '\u6e05\u7a7a'
translations.zh.existingRunAttached = '\u5df2\u63a5\u5165\u672a\u5b8c\u6210\u7684\u8fd0\u884c\uff0c\u8bf7\u7ee7\u7eed\u5f53\u524d\u8fd0\u884c\u6216\u5148\u505c\u6b62\u5b83\u3002'
translations.zh.noLogs = '\u6682\u65e0\u4e8b\u4ef6'
translations.zh.events = '\u4e8b\u4ef6'
translations.zh.requestFailed = '\u8bf7\u6c42\u5931\u8d25'
translations.zh.backendUnavailable = '\u540e\u7aef\u6ca1\u6709\u8fd4\u56de\u6709\u6548 JSON\uff0c\u8bf7\u5148\u542f\u52a8 3456 \u7aef\u53e3\u7684 FastAPI\u3002'
translations.zh.agentLogs = '\u8fd0\u884c\u65e5\u5fd7'
translations.zh.openAgentLogs = '\u67e5\u770b\u8fd0\u884c\u65e5\u5fd7'
translations.zh.noAgentLogs = '\u8be5 Agent \u6682\u65e0\u8fd0\u884c\u4e8b\u4ef6'
translations.zh.deepseek = 'DeepSeek'
translations.zh.glm = 'GLM（智谱 AI）'
translations.zh.openaiCompatible = 'OpenAI Compatible（自定义）'
translations.zh.chatgptLocal = 'ChatGPT（本地 Codex 登录）'
translations.zh.localCodexNote = '使用本机已登录的 Codex CLI，不需要填写 API Key。'
translations.zh.providerConfigurationWarning = '当前模型与 Base URL 可能不属于同一模型服务商。'
translations.zh.runtimePolicy = '\u8fd0\u884c\u7b56\u7565'
translations.zh.maxTokens = '\u6700\u5927\u8f93\u51fa Token'
translations.zh.retryCount = '\u91cd\u8bd5\u6b21\u6570'
translations.zh.thinkingLevel = '\u601d\u8003\u5f3a\u5ea6'
translations.zh.effectiveLevel = '\u5b9e\u9645\u5f3a\u5ea6'
translations.zh.autoMode = '\u81ea\u52a8'
translations.zh.thinkingOff = '\u5173\u95ed'
translations.zh.unsupportedThinking = '\u5f53\u524d\u6a21\u578b\u4e0d\u652f\u6301\u8be5\u5f3a\u5ea6\uff0c\u5c06\u81ea\u52a8\u964d\u7ea7\u3002'
translations.zh.providerDefault = '\u6a21\u578b\u9ed8\u8ba4'
translations.zh.qwen = '千问（Qwen）'
translations.zh.providerAuthFailed = 'API Key 被千问拒绝，请检查 Key 是否完整，以及地域、套餐和 Base URL 是否匹配。'
translations.zh.budgetMode = 'Token 预算模式'
translations.zh.budgetAuto = 'Auto（按任务自适应）'
translations.zh.budgetManual = '手动按 Agent 设置'
translations.zh.budgetAutoNote = '你填写的数值是整条 Run 的安全上限，系统会按 Agent 职责和复杂度分配。'
translations.zh.budgetManualNote = '每个 Agent 按当前配置的输出上限执行。'
translations.zh.downloadAllZip = '下载全部 ZIP'
translations.zh.downloadingZip = '正在打包…'
translations.zh.archiveDownloadFailed = 'ZIP 下载失败'
translations.en.workflowRequirement = 'Requirement analysis'
translations.en.workflowRequirementDetail = '1 Agent 路 focused analysis'
translations.en.workflowBackend = 'Backend implementation'
translations.en.workflowBackendDetail = '1 Agent 路 backend plan'
translations.en.workflowFrontend = 'Frontend implementation'
translations.en.workflowFrontendDetail = '1 Agent 路 frontend plan'
translations.en.retryFailedRun = 'Retry failed nodes'
translations.en.retryingRun = 'Retrying…'
translations.en.recoveryCount = 'recovery'
translations.zh.workflowRequirement = '\u9700\u6c42\u5206\u6790'
translations.zh.workflowRequirementDetail = '1 \u4e2a Agent 路 \u805a\u7126\u5206\u6790'
translations.zh.workflowBackend = '\u540e\u7aef\u5b9e\u73b0'
translations.zh.workflowBackendDetail = '1 \u4e2a Agent 路 \u540e\u7aef\u65b9\u6848'
translations.zh.workflowFrontend = '\u524d\u7aef\u5b9e\u73b0'
translations.zh.workflowFrontendDetail = '1 \u4e2a Agent 路 \u524d\u7aef\u65b9\u6848'
translations.zh.retryFailedRun = '\u91cd\u8bd5\u5931\u8d25\u8282\u70b9'
translations.zh.retryingRun = '\u6b63\u5728\u6062\u590d…'
translations.zh.recoveryCount = '\u6b21\u6062\u590d'

Object.assign(translations.en, {
  skillPolicy: 'Skill policy',
  skillAuto: 'Auto (by task)',
  skillOn: 'On (all candidates)',
  skillOff: 'Off',
  skillPolicyNote: 'Auto selects only declared Skills that match this task. Turning Skills off never disables validation or retry safeguards.',
  skillCandidates: 'Candidate Skills',
})
Object.assign(translations.zh, {
  skillPolicy: 'Skill 策略',
  skillAuto: 'Auto（按任务选择）',
  skillOn: '开启（加载候选项）',
  skillOff: '关闭',
  skillPolicyNote: 'Auto 只加载与当前任务匹配的已声明 Skill；关闭 Skill 不会关闭校验、重试或安全保护。',
  skillCandidates: '候选 Skill',
})

Object.assign(translations.zh, {
  workspace: '工作区', runs: '运行记录', workflows: '工作流', agentLibrary: 'Agent 库', mockProvider: 'Mock Provider', localDemoReady: '本地演示已就绪', builderAccess: '构建者权限',
  apiConnected: 'API 已连接', demoMode: '演示模式', ready: '就绪', running: '运行中', waitingApproval: '等待确认', success: '成功', failed: '失败', stopped: '已停止',
  runCanvas: '工作流运行 / 实时画布', canvasDescription: '输入一条需求，输出经过审核的实现方案', stepsComplete: '步骤已完成', runDuration: '运行耗时', executionGraph: '执行图', nodesLevels: '8 个节点 · 7 个层级', autoLayout: '自动布局', export: '导出',
  pending: '待运行', approval: '审批', runBrief: '运行需求', input: '输入', concurrency: '并发数', maxParallel: '最大并发 Agent', eventStream: '事件流', runActivity: '运行动态', live: '实时',
  inspector: '检查器', nodeDetail: '节点详情', purpose: '职责', agentContract: 'Agent 合约', agentId: 'Agent ID', inputSources: '输入来源', outputKey: '输出键', agentOutput: 'Agent 输出', captured: '已捕获', retryPolicy: '重试策略', attempts: '最多 2 次', exponentialBackoff: '指数退避', outputWillAppear: '输出将在这里显示', selectAfterComplete: '节点完成后选择它查看结果。',
  apiSettings: 'API 配置', cloudApiDescription: '让所有 Agent 接入你的云端模型。', provider: 'Provider', baseUrl: 'Base URL', model: '模型', apiKey: 'API Key', apiKeyConfigured: 'Key 已配置', keyNotConfigured: 'Key 未配置', keyPlaceholder: '粘贴云端 API Key', apiSecretNote: 'Key 仅发送到本地后端，并保存到 backend/.env。', testConnection: '测试连接', saveConfiguration: '保存配置', testing: '测试中…', connectionSuccess: '连接成功', testFailed: '连接失败', mockProviderNote: '保存真实 Key 后将退出 Mock 模式。', approving: '确认中…', approvalFailed: '确认失败', close: '关闭',
  architectureReady: '架构已就绪', unlockAgents: '确认后将解锁 2 个并行 Agent。', approveArchitecture: '确认架构', reject: '拒绝', commandCenter: '命令中心', statePersisted: '状态按 Run 持久化', yamlSource: 'YAML 是唯一事实来源', reset: '重置', stopRun: '停止运行', runWorkflow: '运行工作流', runAgain: '再次运行',
  budgetPlanning: 'Token 预算评估', productAnalysis: '需求分析', systemDesign: '系统设计', implementationPlan: '实现方案', qualityGate: '质量门禁', finalReview: '最终审查', humanGate: '人工确认', skipped: '已跳过',
  nodeTokenEstimator: 'Token 预算评估', nodeRequirement: '需求分析', nodeArchitecture: '架构设计', nodeApproval: '架构确认', nodeBackend: '后端方案', nodeFrontend: '前端方案', nodeTester: '测试方案', nodeReviewer: '最终审查',
  workflowSoftware: '软件开发', workflowDetail: '7 个 Agent · 1 次审批', workflowProduct: '产品探索', workflowProductDetail: '4 个 Agent · 草稿',
  liveLogs: '实时日志', openLogs: '打开日志', closeLogs: '关闭日志', clearLogs: '清空', noLogs: '暂无事件', events: '事件', requestFailed: '请求失败', backendUnavailable: '后端没有返回有效 JSON，请先启动 3456 端口的 FastAPI。', agentLogs: '运行日志', openAgentLogs: '查看运行日志', noAgentLogs: '该 Agent 暂无运行事件', deepseek: 'DeepSeek', runtimePolicy: '运行策略', maxTokens: '最大输出 Token', retryCount: '重试次数', thinkingLevel: '思考强度', effectiveLevel: '实际强度', autoMode: 'Auto', thinkingOff: '关闭', unsupportedThinking: '当前模型不支持该强度，将自动降级。', providerDefault: '模型默认',
  differentActiveRun: '当前需求没有启动 · 已有另一条需求正在运行，请先停止它再运行当前输入。',
  stopExistingRun: '停止旧运行',
  workflowRequirement: '需求分析', workflowRequirementDetail: '1 个 Agent · 重点分析', workflowBackend: '后端实现', workflowBackendDetail: '1 个 Agent · 后端方案', workflowFrontend: '前端实现', workflowFrontendDetail: '1 个 Agent · 前端方案',
  systemEvent: '系统', stepEvent: '步骤', workflowEvent: '工作流', approvalEvent: '审批',
})

function t(key: string) {
  return translations[locale.value][key] ?? key
}

function toggleLocale() {
  locale.value = locale.value === 'en' ? 'zh' : 'en'
}

function localizedRole(role: string | undefined) {
  const roleKeys: Record<string, string> = { 'Token budget planning': 'budgetPlanning', 'Product analysis': 'productAnalysis', 'System design': 'systemDesign', 'Implementation plan': 'implementationPlan', 'Quality gate': 'qualityGate', 'Final review': 'finalReview', 'Human in the loop': 'humanGate', token_budget_agent: 'budgetPlanning', requirement_agent: 'productAnalysis', architect_agent: 'systemDesign', backend_agent: 'implementationPlan', frontend_agent: 'implementationPlan', tester_agent: 'qualityGate', reviewer_agent: 'finalReview' }
  return t(roleKeys[role ?? ''] ?? role ?? '')
}

function workflowName(id: string) {
  const keys: Record<string, string> = { 'software-development': 'workflowSoftware', 'product-discovery': 'workflowProduct', 'requirement-analysis': 'workflowRequirement', 'backend-implementation': 'workflowBackend', 'frontend-implementation': 'workflowFrontend' }
  const key = keys[id]
  return key ? t(key) : workflowList.value.find((workflow) => workflow.id === id)?.name ?? id
}

function workflowDetail(id: string) {
  const keys: Record<string, string> = { 'software-development': 'workflowDetail', 'product-discovery': 'workflowProductDetail', 'requirement-analysis': 'workflowRequirementDetail', 'backend-implementation': 'workflowBackendDetail', 'frontend-implementation': 'workflowFrontendDetail' }
  const key = keys[id]
  if (key) return t(key)
  const workflow = workflowList.value.find((item) => item.id === id)
  return workflow ? `${workflow.stepCount ?? 0} steps 路 ${workflow.concurrency ?? 1} parallel` : ''
}

function localizedNodeLabel(id: string, fallback: string) {
  const keys: Record<string, string> = { token_estimator: 'nodeTokenEstimator', requirement: 'nodeRequirement', requirement_analysis: 'nodeRequirement', architecture: 'nodeArchitecture', architecture_approval: 'nodeApproval', backend: 'nodeBackend', backend_implementation: 'nodeBackend', frontend: 'nodeFrontend', frontend_implementation: 'nodeFrontend', tester: 'nodeTester', reviewer: 'nodeReviewer' }
  return t(keys[id] ?? fallback)
}

const activeWorkflowId = ref('software-development')
const workflowList = ref<WorkflowSummary[]>([
  { id: 'software-development', name: 'Software development', stepCount: 8, concurrency: 3, detail: '7 agents · 1 approval', active: true },
  { id: 'requirement-analysis', name: 'Requirement analysis', stepCount: 1, concurrency: 1, detail: '1 Agent · focused analysis', active: false },
  { id: 'backend-implementation', name: 'Backend implementation', stepCount: 1, concurrency: 1, detail: '1 Agent · backend plan', active: false },
  { id: 'frontend-implementation', name: 'Frontend implementation', stepCount: 1, concurrency: 1, detail: '1 Agent · frontend plan', active: false },
])

const baseNodes: Node[] = [
  { id: 'requirement', type: 'agent', position: { x: 36, y: 64 }, data: { label: 'Requirement', role: 'Product analysis', agentId: 'requirement_agent', status: 'PENDING', description: '先使用启动预算，把自然语言需求收敛成可执行的产品范围。' } },
  { id: 'token_estimator', type: 'agent', position: { x: 250, y: 64 }, data: { label: 'Token estimator', role: 'Token budget planning', agentId: 'token_budget_agent', status: 'PENDING', description: '依据已确认需求和模型能力，为后续 Agent 规划输出预算。' } },
  { id: 'architecture', type: 'agent', position: { x: 464, y: 64 }, data: { label: 'Architect', role: 'System design', agentId: 'architect_agent', status: 'PENDING', description: '定义模块边界、数据流和技术风险。' } },
  { id: 'architecture_approval', type: 'approval', position: { x: 678, y: 64 }, data: { label: 'Architecture approval', role: 'Human in the loop', status: 'PENDING', description: '架构确认后，开发分支才会启动。' } },
  { id: 'backend', type: 'agent', position: { x: 892, y: 16 }, data: { label: 'Backend', role: 'Implementation plan', agentId: 'backend_agent', status: 'PENDING', description: '拆解 API、数据模型、安全和可靠性。' } },
  { id: 'frontend', type: 'agent', position: { x: 892, y: 166 }, data: { label: 'Frontend', role: 'Implementation plan', agentId: 'frontend_agent', status: 'PENDING', description: '设计页面、交互、状态和错误体验。' } },
  { id: 'tester', type: 'agent', position: { x: 1110, y: 92 }, data: { label: 'Tester', role: 'Quality gate', agentId: 'tester_agent', status: 'PENDING', description: '从两条实现分支合并测试策略和风险。' } },
  { id: 'reviewer', type: 'agent', position: { x: 1328, y: 92 }, data: { label: 'Reviewer', role: 'Final review', agentId: 'reviewer_agent', status: 'PENDING', description: '输出最终的架构与方案审查结论。' } },
]

// Runtime planners are platform control nodes. Their own budget/retry policy
// comes from YAML and is intentionally not user-overridable.
const tokenEstimatorNode = baseNodes.find((node) => node.id === 'token_estimator')
if (tokenEstimatorNode) tokenEstimatorNode.data.runtimePlan = true

const baseEdges: Edge[] = [
  { id: 'requirement-token-estimator', source: 'requirement', target: 'token_estimator', animated: false },
  { id: 'token-estimator-architecture', source: 'token_estimator', target: 'architecture', animated: false },
  { id: 'architecture-approval', source: 'architecture', target: 'architecture_approval', animated: false },
  { id: 'approval-backend', source: 'architecture_approval', target: 'backend', animated: false },
  { id: 'approval-frontend', source: 'architecture_approval', target: 'frontend', animated: false },
  { id: 'backend-tester', source: 'backend', target: 'tester', animated: false },
  { id: 'frontend-tester', source: 'frontend', target: 'tester', animated: false },
  { id: 'tester-reviewer', source: 'tester', target: 'reviewer', animated: false },
]

// Vue Flow's generic Node type is intentionally wide; the canvas owns this UI metadata.
const graphTemplate = ref<any[]>(cloneValue(baseNodes))
const edgeTemplate = ref<any[]>(cloneValue(baseEdges))
const nodes = ref<any[]>(cloneValue(graphTemplate.value))
const edges = ref<any[]>(cloneValue(edgeTemplate.value))
const selectedNodeId = ref('architecture')
const requirement = ref(demoRequirement)
const showApiConfig = ref(false)
const isTestingConnection = ref(false)
const isSavingConfig = ref(false)
const connectionTest = ref<{ kind: 'idle' | 'success' | 'error'; message: string; latency?: number }>({ kind: 'idle', message: '' })
const apiConfig = reactive({ provider: 'qwen' as ProviderKey, baseUrl: providerPresets.qwen.baseUrl, modelName: providerPresets.qwen.modelName, apiKey: '', apiKeyConfigured: false, usingMock: true })
const apiKeyInput = ref<HTMLInputElement | null>(null)
const providerCapabilities = reactive({ supportsThinking: false, supportedLevels: ['off'] as ThinkingMode[], defaultLevel: 'off' as ThinkingMode, model: '', modelFamily: '', configurationWarning: '' })
const runtimeDefaults = reactive<RuntimeSettings>({ maxTokens: 6000, retry: 2, thinking: 'auto' })
const budgetMode = ref<BudgetMode>('auto')
const skillMode = ref<'auto' | 'on' | 'off'>('auto')
const runtimeByStep = reactive<Record<string, Partial<RuntimeSettings>>>({})
const thinkingModes: ThinkingMode[] = ['auto', 'off', 'low', 'high', 'max']
const runId = ref<string | null>(null)
const runStatus = ref<'IDLE' | 'RUNNING' | 'WAITING_APPROVAL' | 'WAITING_CLARIFICATION' | 'SUCCESS' | 'FAILED' | 'STOPPED'>('IDLE')
const isSubmittingApproval = ref(false)
const isRetryingRun = ref(false)
const conflictingRun = ref<ActiveRunConflict | null>(null)
const approvalError = ref('')
const clarificationError = ref('')
const clarificationRequest = ref<Record<string, any> | null>(null)
const clarificationOption = ref('full_stack_h2')
const clarificationEntityInput = ref('')
const clarificationCustomAnswers = ref<Record<string, string>>({})
const logsByStep = ref<Record<string, LogItem[]>>({})
const expandedLogNodeId = ref<string | null>(null)
const logs = ref<LogItem[]>([
  { time: '09:41:02', type: 'system', message: '画布已加载 · 执行图已准备' },
  { time: '09:41:02', type: 'system', message: 'Mock Provider 已连接 · 延迟 250ms' },
])
const outputs = ref<Record<string, AgentOutput>>({})
const errorSummaries = ref<Record<string, ErrorSummary>>({})
const errorSummaryNodeId = ref<string | null>(null)
const artifacts = ref<Artifact[]>([])
const artifactViewer = ref<ArtifactViewer | null>(null)
const isDownloadingArchive = ref(false)
const archiveDownloadError = ref('')
const usingDemo = ref(false)
const apiOnline = ref(false)
let eventSource: EventSource | null = null
const seenSseEventIds = new Set<string>()
const sseConnectionLogRuns = new Set<string>()
let demoTimers: number[] = []
let runStartedAt = 0
const showRunStats = ref(false)
const showRunHistory = ref(false)
const runHistory = ref<RunHistoryItem[]>([])
const runHistoryLoading = ref(false)
const runHistoryError = ref('')
const clockNow = ref(Date.now())
const runDurationBaseMs = ref(0)
const runDurationSyncedAt = ref(0)
const runStats = reactive<RunStats>({
  activeDurationMs: 0,
  approvalDurationMs: 0,
  inputTokens: 0,
  outputTokens: 0,
  totalTokens: 0,
  byAgent: [],
  repair: { firstPass: false, repairRounds: 0, successfulRepairs: 0, automaticRepairRate: 0, averageRepairRounds: 0, circuitBreaks: 0 },
  stages: [],
  repairTrace: [],
})
let durationTimer: number | null = null
const activeRunStorageKey = 'orbit.activeRun'
const selectedWorkflowStorageKey = 'orbit.selectedWorkflow'
const eventCursorByRun = new Map<string, number>()
let hiddenAt = 0
let refreshInFlight = false
let lastRefreshErrorAt = 0

const activeWorkflow = computed(() => workflowList.value.find((workflow) => workflow.id === activeWorkflowId.value))
const selectedNode = computed(() => nodes.value.find((node) => node.id === selectedNodeId.value) ?? nodes.value[0])
const selectedOutput = computed(() => outputs.value[selectedNodeId.value])
const selectedRuntime = computed<RuntimeSettings>(() => ({ maxTokens: runtimeByStep[selectedNodeId.value]?.maxTokens ?? runtimeDefaults.maxTokens, retry: runtimeByStep[selectedNodeId.value]?.retry ?? runtimeDefaults.retry, thinking: runtimeByStep[selectedNodeId.value]?.thinking ?? runtimeDefaults.thinking }))
const selectedRuntimeLocked = computed(() => Boolean(selectedNode.value?.data.runtimePlan))
const effectiveThinking = computed<ThinkingMode>(() => resolveThinkingForUi(selectedRuntime.value.thinking))
const thinkingNeedsFallback = computed(() => selectedRuntime.value.thinking !== 'auto' && effectiveThinking.value !== selectedRuntime.value.thinking)
const completedCount = computed(() => nodes.value.filter((node) => node.data.status === 'SUCCESS').length)
const progress = computed(() => Math.round((completedCount.value / nodes.value.length) * 100))
const liveDurationMs = computed(() => {
  if (!runDurationSyncedAt.value || runStatus.value !== 'RUNNING') return runDurationBaseMs.value
  return runDurationBaseMs.value + Math.max(0, clockNow.value - runDurationSyncedAt.value)
})
const liveDuration = computed(() => formatDuration(liveDurationMs.value))
const isBusy = computed(() => runStatus.value === 'RUNNING' || runStatus.value === 'WAITING_APPROVAL' || runStatus.value === 'WAITING_CLARIFICATION')
const isStartingRun = ref(false)
const workflowGraphMeta = computed(() => locale.value === 'zh' ? `${nodes.value.length} 个节点 路 ${edges.value.length} 条连接` : `${nodes.value.length} nodes 路 ${edges.value.length} edges`)

function resolveThinkingForUi(requested: ThinkingMode): ThinkingMode {
  const supported = providerCapabilities.supportedLevels
  if (!providerCapabilities.supportsThinking) return 'off'
  if (requested === 'auto') return supported.includes(providerCapabilities.defaultLevel) ? providerCapabilities.defaultLevel : 'off'
  if (supported.includes(requested)) return requested
  const fallback: Record<ThinkingMode, ThinkingMode[]> = { auto: ['off'], off: ['off'], low: ['off'], high: ['low', 'off'], max: ['high', 'low', 'off'] }
  return fallback[requested].find((level) => supported.includes(level)) ?? 'off'
}

function updateRuntimeField(field: keyof RuntimeSettings, value: string | number) {
  if (!selectedNodeId.value || selectedRuntimeLocked.value) return
  const current = runtimeByStep[selectedNodeId.value] ?? {}
  runtimeByStep[selectedNodeId.value] = { ...current, [field]: value }
  nodes.value = nodes.value.map((node) => node.id === selectedNodeId.value ? { ...node, data: { ...node.data, runtime: { ...node.data.runtime, [field]: value } } } : node)
}

function onMaxTokensInput(event: Event) {
  updateRuntimeField('maxTokens', Math.min(128000, Math.max(1, Number((event.target as HTMLInputElement).value) || 1)))
}

function onRetryChange(event: Event) {
  updateRuntimeField('retry', Math.min(10, Math.max(0, Number((event.target as HTMLSelectElement).value))))
}

function onBudgetModeChange(event: Event) {
  budgetMode.value = (event.target as HTMLSelectElement).value as BudgetMode
}

function onSkillModeChange(event: Event) {
  skillMode.value = (event.target as HTMLSelectElement).value as 'auto' | 'on' | 'off'
}

function onThinkingChange(event: Event) {
  updateRuntimeField('thinking', (event.target as HTMLSelectElement).value as ThinkingMode)
}

function buildRuntimePayload() {
  const steps: Record<string, Record<string, string | number>> = {}
  for (const node of nodes.value.filter((item) => item.type === 'agent' && !item.data?.runtimePlan)) {
    const runtime = runtimeByStep[node.id] ?? {}
    steps[node.id] = {
      max_tokens: runtime.maxTokens ?? runtimeDefaults.maxTokens,
      retry: runtime.retry ?? runtimeDefaults.retry,
      thinking: runtime.thinking ?? runtimeDefaults.thinking,
    }
  }
  return {
    budget_mode: budgetMode.value,
    defaults: { max_tokens: runtimeDefaults.maxTokens, retry: runtimeDefaults.retry, thinking: runtimeDefaults.thinking, skill_mode: skillMode.value },
    steps,
  }
}

function formatDuration(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function formatTokenCount(value: number) {
  const count = Math.max(0, Math.round(Number(value) || 0))
  return count.toLocaleString('en-US')
}

function formatPercent(value: number) {
  return `${Math.round(Math.max(0, Number(value) || 0) * 100)}%`
}

function stageLabel(stage: string) {
  const labels: Record<string, string> = {
    analysis: '需求与架构', implementation: '实现', validation: '验证', review: '审查',
    validation_preflight: '快速预检', validation_execution: '构建与运行',
  }
  return labels[stage] ?? stage
}

function numericValue(value: unknown) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : 0
}

function syncRunDuration(activeDurationMs: number) {
  const current = numericValue(activeDurationMs)
  runDurationBaseMs.value = current
  runDurationSyncedAt.value = Date.now()
  clockNow.value = Date.now()
  runStats.activeDurationMs = current
}

function resetRunStats() {
  runDurationBaseMs.value = 0
  runDurationSyncedAt.value = 0
  runStats.activeDurationMs = 0
  runStats.approvalDurationMs = 0
  runStats.inputTokens = 0
  runStats.outputTokens = 0
  runStats.totalTokens = 0
  runStats.byAgent = []
  runStats.repair = { firstPass: false, repairRounds: 0, successfulRepairs: 0, automaticRepairRate: 0, averageRepairRounds: 0, circuitBreaks: 0 }
  runStats.stages = []
  runStats.repairTrace = []
  showRunStats.value = false
}

function recalculateTokenTotals() {
  runStats.inputTokens = runStats.byAgent.reduce((total, item) => total + item.inputTokens, 0)
  runStats.outputTokens = runStats.byAgent.reduce((total, item) => total + item.outputTokens, 0)
  runStats.totalTokens = runStats.inputTokens + runStats.outputTokens
}

function updateAgentTokenUsage(stepId: string, tokens: unknown, status = 'SUCCESS', retryCount = 0) {
  if (!tokens || typeof tokens !== 'object') return
  const value = tokens as Record<string, any>
  const node = nodes.value.find((item) => item.id === stepId)
  const inputTokens = numericValue(value.input ?? value.input_tokens ?? value.prompt_tokens)
  const outputTokens = numericValue(value.output ?? value.output_tokens ?? value.completion_tokens)
  const existing = runStats.byAgent.find((item) => item.stepId === stepId)
  if (existing) {
    existing.inputTokens = inputTokens
    existing.outputTokens = outputTokens
    existing.totalTokens = inputTokens + outputTokens
    existing.status = status
    existing.retryCount = numericValue(retryCount)
  } else {
    runStats.byAgent.push({
      stepId,
      agentId: String(node?.data?.agentId ?? stepId),
      status,
      inputTokens,
      outputTokens,
      totalTokens: inputTokens + outputTokens,
      retryCount: numericValue(retryCount),
    })
  }
  recalculateTokenTotals()
}

function applyRunStats(data: Record<string, any>) {
  const activeDurationMs = numericValue(data.activeDurationMs ?? data.active_duration_ms ?? data.durationMs ?? data.duration_ms)
  const approvalDurationMs = numericValue(data.approvalDurationMs ?? data.approval_duration_ms)
  const usage = (data.tokenUsage ?? data.token_usage ?? {}) as Record<string, any>
  const rows = Array.isArray(usage.byAgent) ? usage.byAgent : Array.isArray(usage.by_agent) ? usage.by_agent : []
  runStats.approvalDurationMs = approvalDurationMs
  runStats.byAgent = rows.map((item: Record<string, any>) => {
    const inputTokens = numericValue(item.inputTokens ?? item.input_tokens)
    const outputTokens = numericValue(item.outputTokens ?? item.output_tokens)
    return {
      stepId: String(item.stepId ?? item.step_id ?? ''),
      agentId: String(item.agentId ?? item.agent_id ?? item.stepId ?? item.step_id ?? ''),
      status: String(item.status ?? ''),
      inputTokens,
      outputTokens,
      totalTokens: inputTokens + outputTokens,
      retryCount: numericValue(item.retryCount ?? item.retry_count),
    }
  }).filter((item: AgentTokenUsage) => item.stepId)
  const reportedInput = numericValue(usage.inputTokens ?? usage.input_tokens)
  const reportedOutput = numericValue(usage.outputTokens ?? usage.output_tokens)
  recalculateTokenTotals()
  if (!runStats.byAgent.length) {
    runStats.inputTokens = reportedInput
    runStats.outputTokens = reportedOutput
    runStats.totalTokens = numericValue(usage.totalTokens ?? usage.total_tokens) || reportedInput + reportedOutput
  }
  const repair = (data.repairMetrics ?? data.repair_metrics ?? {}) as Record<string, any>
  runStats.repair = {
    firstPass: Boolean(repair.firstPass ?? repair.first_pass),
    repairRounds: numericValue(repair.repairRounds ?? repair.repair_rounds),
    successfulRepairs: numericValue(repair.successfulRepairs ?? repair.successful_repairs),
    automaticRepairRate: Number(repair.automaticRepairRate ?? repair.automatic_repair_rate ?? 0),
    averageRepairRounds: Number(repair.averageRepairRounds ?? repair.average_repair_rounds ?? 0),
    circuitBreaks: numericValue(repair.circuitBreaks ?? repair.circuit_breaks),
  }
  const stages = Array.isArray(data.stageMetrics) ? data.stageMetrics : Array.isArray(data.stage_metrics) ? data.stage_metrics : []
  runStats.stages = stages.map((item: Record<string, any>) => ({
    stage: String(item.stage ?? ''),
    durationMs: numericValue(item.durationMs ?? item.duration_ms),
    inputTokens: numericValue(item.inputTokens ?? item.input_tokens),
    outputTokens: numericValue(item.outputTokens ?? item.output_tokens),
    totalTokens: numericValue(item.totalTokens ?? item.total_tokens),
  })).filter((item: StageMetric) => item.stage)
  const traceRows = Array.isArray(data.repairTrace) ? data.repairTrace : Array.isArray(data.repair_trace) ? data.repair_trace : []
  runStats.repairTrace = traceRows.map((item: Record<string, any>) => ({
    key: String(item.key ?? ''),
    stepId: String(item.stepId ?? item.step_id ?? ''),
    attempt: numericValue(item.attempt),
    location: String(item.location ?? ''),
    owners: Array.isArray(item.owners) ? item.owners.map(String) : [],
    status: String(item.status ?? ''),
    result: String(item.result ?? ''),
    files: Array.isArray(item.files) ? item.files.map(String) : [],
  })).filter((item: RepairTrace) => item.key && item.stepId)
  syncRunDuration(activeDurationMs)
}

function upsertRepairTrace(stepId: string, payload: Record<string, any>, status: string, result = '') {
  const plan = (payload.plan ?? {}) as Record<string, any>
  const fact = (payload.failureFact ?? {}) as Record<string, any>
  const attempt = numericValue(payload.repairAttempt ?? payload.attempt ?? payload.attempts)
  const key = `${stepId}:${attempt || 0}`
  const existing = runStats.repairTrace.find((item) => item.key === key)
  const owners = Array.isArray(payload.owners) ? payload.owners.map(String) : Array.isArray(plan.owners) ? plan.owners.map(String) : fact.owner ? [String(fact.owner)] : existing?.owners ?? []
  const checks = Array.isArray(plan.check_ids) ? plan.check_ids : Array.isArray(plan.checkIds) ? plan.checkIds : []
  const files = Array.isArray(payload.files) ? payload.files.map(String) : existing?.files ?? []
  const value: RepairTrace = {
    key,
    stepId,
    attempt,
    location: checks.map(String).join('、') || String(fact.code ?? '') || existing?.location || stepId,
    owners,
    status,
    result: result || existing?.result || '',
    files,
  }
  if (existing) Object.assign(existing, value)
  else runStats.repairTrace.push(value)
  recalculateLiveRepairMetrics()
}

function recalculateLiveRepairMetrics() {
  const attempted = runStats.repairTrace.filter((item) => item.attempt > 0)
  const successful = attempted.filter((item) => /通过|完成|已提交/.test(item.status))
  const circuits = runStats.repairTrace.filter((item) => item.status.includes('熔断'))
  runStats.repair.firstPass = runStats.repairTrace.length === 0 && runStatus.value === 'SUCCESS'
  runStats.repair.repairRounds = new Set(attempted.map((item) => item.key)).size
  runStats.repair.successfulRepairs = successful.length
  runStats.repair.circuitBreaks = circuits.length
  const completed = Math.max(successful.length + circuits.length, attempted.length ? 1 : 0)
  runStats.repair.automaticRepairRate = completed ? successful.length / completed : 0
  runStats.repair.averageRepairRounds = completed ? attempted.length / completed : 0
}

function now() {
  return new Date().toLocaleTimeString('en-GB', { hour12: false })
}

function addLog(message: string, type = 'system', tone = '', stepId?: string, detail?: string) {
  const item: LogItem = { time: now(), type, message, tone, stepId, detail }
  logs.value.unshift(item)
  if (stepId) logsByStep.value[stepId] = [item, ...(logsByStep.value[stepId] ?? [])]
}

function setClarificationRequest(request: Record<string, any> | null | undefined) {
  const previousId = String(clarificationRequest.value?.request_id ?? '')
  clarificationRequest.value = request ?? null
  if (!request) return
  if (String(request.request_id ?? '') !== previousId) {
    clarificationEntityInput.value = ''
    clarificationCustomAnswers.value = {}
    for (const prompt of request.field_prompts ?? []) {
      if (prompt?.field && prompt?.recommended != null) {
        clarificationCustomAnswers.value[String(prompt.field)] = String(prompt.recommended)
      }
    }
  }
  const options = Array.isArray(request.options) ? request.options : []
  const current = String(clarificationOption.value ?? '')
  const currentExists = options.some((option: any) => String(option?.value ?? '') === current)
  const first = options.find((option: any) => option && option.value != null)
  if ((!currentExists || String(request.request_id ?? '') !== previousId) && first) clarificationOption.value = String(first.value)
}

function openStepLogs(stepId: string) {
  selectedNodeId.value = stepId
  expandedLogNodeId.value = expandedLogNodeId.value === stepId ? null : stepId
}

function stepLogs(stepId: string) {
  return logsByStep.value[stepId] ?? []
}

function openArtifact(artifact: Artifact) {
  if (!artifact.contentUrl) return
  artifactViewer.value = { artifact, mode: 'preview', content: '', loading: false, error: '' }
}

async function viewArtifactSource(artifact: Artifact) {
  if (!artifact.contentUrl) return
  artifactViewer.value = { artifact, mode: 'source', content: '', loading: true, error: '' }
  try {
    const response = await fetch(artifact.contentUrl)
    const content = await response.text()
    if (!response.ok) throw new Error(content || `HTTP ${response.status}`)
    if (artifactViewer.value?.artifact.id === artifact.id) artifactViewer.value = { artifact, mode: 'source', content, loading: false, error: '' }
  } catch (error) {
    if (artifactViewer.value?.artifact.id === artifact.id) artifactViewer.value = { artifact, mode: 'source', content: '', loading: false, error: error instanceof Error ? error.message : '无法读取成果物' }
  }
}

function artifactDownloadUrl(artifact: Artifact) {
  if (!artifact.contentUrl) return '#'
  return `${artifact.contentUrl}${artifact.contentUrl.includes('?') ? '&' : '?'}download=1`
}

function artifactArchiveDownloadUrl() {
  if (!runId.value) return '#'
  return `${API_BASE}/runs/${encodeURIComponent(runId.value)}/artifacts/archive`
}

async function downloadArtifactArchive() {
  const url = artifactArchiveDownloadUrl()
  if (url === '#' || isDownloadingArchive.value) return
  isDownloadingArchive.value = true
  archiveDownloadError.value = ''
  try {
    const response = await fetch(url, { headers: { Accept: 'application/zip' } })
    const contentType = response.headers.get('content-type') ?? ''
    if (!response.ok || contentType.includes('application/json')) {
      const raw = await response.text()
      let data: Record<string, any> | null = null
      try {
        data = raw.trim() ? JSON.parse(raw) : null
      } catch {
        data = null
      }
      throw new Error(response.ok ? '服务器返回的不是 ZIP 文件，请重启后端后重试。' : responseError(response, data))
    }
    const blob = await response.blob()
    if (!blob.size) throw new Error('服务器返回了空的 ZIP 文件。')
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `${runId.value}-artifacts.zip`
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  } catch (error) {
    archiveDownloadError.value = error instanceof Error ? error.message : t('archiveDownloadFailed')
  } finally {
    isDownloadingArchive.value = false
  }
}

async function copyStepLogs(stepId: string) {
  const items = stepLogs(stepId)
  const text = items.map((log) => `${log.time} ${log.message}${log.detail ? `\n${log.detail}` : ''}`).join('\n')
  if (!text || !navigator.clipboard) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // Native text selection remains available when clipboard permissions are denied.
  }
}

function closeArtifactViewer() {
  artifactViewer.value = null
}

function hydrateArtifacts(data: Record<string, any>, replace = true) {
  const items = Array.isArray(data.artifacts) ? data.artifacts : []
  const next = items.map((item: Record<string, any>) => ({
    id: String(item.id ?? ''),
    name: String(item.name ?? 'artifact'),
    mimeType: String(item.mimeType ?? item.mime_type ?? 'application/octet-stream'),
    sizeBytes: Number(item.sizeBytes ?? item.size_bytes ?? 0),
    contentUrl: String(item.contentUrl ?? `${API_BASE}/runs/${runId.value}/artifacts/${encodeURIComponent(String(item.id ?? ''))}/content`),
    previewable: Boolean(item.previewable),
  })).filter((item: Artifact) => item.id)
  if (replace) artifacts.value = next
  else artifacts.value = [...artifacts.value.filter((item) => !next.some((incoming) => incoming.id === item.id)), ...next]
}

async function readApiJson(response: Response): Promise<Record<string, any> | null> {
  const raw = await response.text()
  if (!raw.trim()) return null
  try {
    return JSON.parse(raw) as Record<string, any>
  } catch {
    return { detail: t('backendUnavailable') }
  }
}

function responseError(response: Response, data: Record<string, any> | null) {
  const detail = localizeProviderMessage(data?.detail, '')
  return detail || `${t('requestFailed')} · HTTP ${response.status}`
}

function requestErrorMessage(error: unknown) {
  if (error instanceof TypeError && /fetch|network/i.test(error.message)) return t('backendUnavailable')
  return error instanceof Error ? localizeProviderMessage(error.message, t('requestFailed')) : t('requestFailed')
}

function nodeStatusClass(status: Status | string | undefined) {
  return `status-${(status ?? 'PENDING').toLowerCase()}`
}

function statusLabel(status: Status | string | undefined) {
  return { PENDING: t('pending'), RUNNING: t('running'), SUCCESS: t('success'), FAILED: t('failed'), SKIPPED: t('skipped'), WAITING_APPROVAL: t('approval'), WAITING_CLARIFICATION: '等待需求确认' }[status ?? 'PENDING'] ?? status ?? t('pending')
}

function readableError(value: unknown, fallback: string) {
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return readableError(record.message ?? record.error ?? record.detail, fallback)
  }
  const message = String(value ?? '').trim()
  return message || fallback
}

function compactLogValue(value: unknown) {
  return String(value ?? '').trim().slice(0, 260)
}

function localizeProviderMessage(value: unknown, fallback: string) {
  const message = readableError(value, fallback)
  const knownMessages: Record<string, string> = {
    'Agent returned an empty output': 'Agent 返回了空输出',
    'Provider returned no choices': 'Provider 未返回 choices',
    'Provider returned invalid JSON': 'Provider 返回了无效 JSON',
    'A real MODEL_API_KEY is required to test the cloud provider': '请先输入当前 Provider 的 API Key，再测试连接',
    'Architecture classified the project as static HTML; Backend is not required': 'Architecture 判断为静态 HTML，Backend 无需运行',
    'Provider returned an invalid response object': 'Provider 返回了无效响应对象',
    'Agent failed without a reported cause': 'Agent 未报告失败原因',
    'Workflow failed without a reported cause': '工作流未报告失败原因',
    'Request timed out': '请求超时',
    'retryable provider error': '可重试的 Provider 错误',
  }
  if (knownMessages[message]) return knownMessages[message]
  if (message.includes('HTTP 401') || message.includes('Incorrect API key provided')) return t('providerAuthFailed')
  if (message.startsWith('Requested thinking level')) return '当前模型不支持请求的思考强度，已自动降级。'
  if (message.startsWith('This model does not support thinking controls')) return '当前模型不支持思考控制，已自动关闭思考。'
  if (message.startsWith('Provider returned HTTP ')) return message.replace('Provider returned HTTP ', 'Provider 返回 HTTP ')
  if (message.startsWith('Provider network error: ')) return message.replace('Provider network error: ', 'Provider 网络错误：')
  if (message.startsWith('Provider connect timeout after ')) return message.replace('Provider connect timeout after ', 'Provider 连接超时，限制为 ')
  if (message.startsWith('Provider read timeout after ')) return message.replace('Provider read timeout after ', 'Provider 读取超时，限制为 ')
  return message
}

function thinkingLabel(value: unknown) {
  const level = String(value ?? '').trim().toLowerCase()
  return ({ auto: 'Auto', off: '关闭', low: 'Low', high: 'High', max: 'Max' } as Record<string, string>)[level] ?? String(value ?? '-')
}

function localizedLogType(type: string) {
  const keys: Record<string, string> = { system: 'systemEvent', step: 'stepEvent', workflow: 'workflowEvent', approval: 'approvalEvent' }
  return t(keys[type] ?? type)
}

function runtimeLogDetail(payload: Record<string, any>) {
  const parts: string[] = []
  if (payload.agentId) parts.push(`Agent：${payload.agentId}`)
  if (payload.durationMs != null) parts.push(`耗时：${payload.durationMs}ms`)
  if (payload.retryCount != null) parts.push(`重试：${payload.retryCount} 次`)
  const tokens = payload.tokens
  if (tokens && typeof tokens === 'object') {
    parts.push(`Token：输入 ${tokens.input ?? 0} / 输出 ${tokens.output ?? 0}`)
  }
  const runtime = payload.runtime
  if (runtime && typeof runtime === 'object') {
    const thinking = runtime.requestedThinking ?? runtime.thinking
    const effective = runtime.effectiveThinking ?? thinking
    if (runtime.budgetMode) parts.push(`Token 模式：${runtime.budgetMode === 'auto' ? 'Auto 自适应' : 'Manual'}`)
    parts.push(`上限：${runtime.maxTokens ?? '-'} Token · 思考：${thinkingLabel(thinking)} → ${thinkingLabel(effective)}`)
    if (runtime.generationMode === 'artifacts') parts.push(`生成：按成果物分块${Array.isArray(runtime.artifactFiles) && runtime.artifactFiles.length ? `（${runtime.artifactFiles.join('、')}）` : ''}`)
    if (runtime.artifactBudgetSource === 'file_estimate') parts.push(`成果物预算：按文件估算，最低 ${Number(runtime.artifactFileMinTokens ?? 0)} Token`)
    if (runtime.artifactSplitCount) parts.push(`任务细分：${runtime.artifactSplitCount} 个文件进入分块任务`)
    if (runtime.artifactContinuationCount) parts.push(`续写兜底：${runtime.artifactContinuationCount} 次`)
    if (runtime.artifactRepairCount) parts.push(`定点修复：${runtime.artifactRepairCount} 次`)
  }
  const providerDetail = providerLogDetail(payload.provider)
  if (providerDetail) parts.push(providerDetail)
  if (Array.isArray(payload.providerAttempts) && payload.providerAttempts.length > 1) {
    const attempts = payload.providerAttempts.map((attempt: Record<string, any>, index: number) => {
      const request = attempt.request_parameters ?? {}
      const details = attempt.usage?.completion_tokens_details ?? {}
      const controls = [
        request.enable_thinking != null ? `thinking=${request.enable_thinking}` : request.reasoning_effort ? `thinking=${request.reasoning_effort}` : 'thinking=未下发',
        `输出=${attempt.usage?.completion_tokens ?? 0}`,
        `推理=${details.reasoning_tokens ?? 0}`,
        `正文=${String(attempt.message_content ?? '').trim() ? '有' : '空'}`,
      ]
      return `第 ${index + 1} 次（${controls.join('、')}）`
    })
    parts.push(`请求明细：${attempts.join('；')}`)
  }
  return parts.join(' · ')
}

function providerLogDetail(provider: unknown) {
  if (!provider || typeof provider !== 'object') return ''
  const value = provider as Record<string, any>
  const parts: string[] = []
  const request = value.request_parameters ?? value.requestParameters
  if (request && typeof request === 'object') {
    const controls: string[] = []
    if (request.max_tokens != null) controls.push(`max_tokens=${request.max_tokens}`)
    if (request.enable_thinking != null) controls.push(`enable_thinking=${request.enable_thinking}`)
    if (request.reasoning_effort) controls.push(`reasoning_effort=${request.reasoning_effort}`)
    if (request.thinking?.type) controls.push(`thinking.type=${request.thinking.type}`)
    if (controls.length) parts.push(`云端实际参数：${controls.join('、')}`)
  }
  const finishReason = value.finish_reason ?? value.finishReason
  if (finishReason) parts.push(`finish_reason：${finishReason}`)
  const content = value.message_content ?? value.messageContent
  if (content !== undefined) parts.push(`message.content：${String(content).trim() ? '有内容' : '空'}`)
  const reasoning = value.reasoning_content ?? value.reasoningContent
  if (reasoning !== undefined) parts.push(`reasoning_content：${String(reasoning).trim() ? '有内容' : '空'}`)
  const usage = value.usage
  if (usage && typeof usage === 'object') {
    const input = usage.prompt_tokens ?? usage.input_tokens ?? usage.input ?? 0
    const output = usage.completion_tokens ?? usage.output_tokens ?? usage.output ?? 0
    parts.push(`usage：输入 ${input} / 输出 ${output}`)
  }
  return parts.join(' · ')
}

function failureLogDetail(payload: Record<string, any>) {
  const parts: string[] = []
  if (Array.isArray(payload.providerAttempts) && payload.providerAttempts.length > 1) {
    const attempts = payload.providerAttempts.map((attempt: Record<string, any>, index: number) => {
      const request = attempt.request_parameters ?? {}
      const details = attempt.usage?.completion_tokens_details ?? {}
      const thinking = request.enable_thinking != null ? String(request.enable_thinking) : String(request.reasoning_effort ?? '未下发')
      return `第 ${index + 1} 次（thinking=${thinking}、输出=${attempt.usage?.completion_tokens ?? 0}、推理=${details.reasoning_tokens ?? 0}、正文=${String(attempt.message_content ?? '').trim() ? '有' : '空'}）`
    })
    parts.push(`请求明细：${attempts.join('；')}`)
  }
  if (payload.errorType) parts.push(`类型：${payload.errorType}`)
  if (payload.durationMs != null) parts.push(`耗时：${payload.durationMs}ms`)
  if (payload.retryCount != null) parts.push(`重试：${payload.retryCount} 次`)
  if (payload.reason) parts.push(`原因：${localizeProviderMessage(compactLogValue(payload.reason), '未提供原因')}`)
  const providerDetail = providerLogDetail(payload.provider)
  if (providerDetail) parts.push(providerDetail)
  return parts.join(' · ')
}

function stepFailureMessage(payload: Record<string, any>) {
  const message = localizeProviderMessage(payload.error, 'Agent 未报告失败原因')
  const retryCount = Number(payload.retryCount ?? 0)
  return retryCount > 0 ? `${message} · 重试 ${retryCount} 次` : message
}

function summarizeAgentError(payload: Record<string, any>): ErrorSummary {
  const failureFact = payload.failureFact as Record<string, any> | undefined
  if (failureFact?.summary) {
    return {
      title: String(failureFact.summary),
      reason: String(failureFact.message ?? payload.error ?? '').slice(0, 180),
      suggestion: failureFact.repairable ? '已按责任范围进行有界修复；详细证据请查看运行日志。' : '责任尚不明确，已保留证据，请检查平台验证或运行环境。',
    }
  }
  const raw = String(payload.error ?? payload.reason ?? '').trim()
  const text = raw.toLowerCase()
  const reason = localizeProviderMessage(compactLogValue(raw), 'Agent 未返回明确错误信息').slice(0, 180)
  if (/timeout|timed out|超时/.test(text)) {
    return { title: '请求超时', reason, suggestion: '检查网络和 Provider 响应速度，必要时降低任务范围或提高该 Agent 超时限制。' }
  }
  if (/length|truncat|output limit|输出上限|输出被截断/.test(text)) {
    return { title: '输出达到上限', reason, suggestion: '缩小单次输出范围，启用文件拆分，或提高该 Agent 的输出 Token 上限。' }
  }
  if (/validation|pydantic|schema|契约|字段|query_parameters|类型未明确/.test(text)) {
    return { title: '输出不符合契约', reason, suggestion: '检查 Architecture 的字段、接口和类型定义，再让责任 Agent 按错误指纹整改。' }
  }
  if (/401|403|api key|api_key|authentication|unauthorized|鉴权|密钥/.test(text)) {
    return { title: 'Provider 鉴权失败', reason, suggestion: '检查 API Key、Provider、Base URL 和模型是否属于同一厂商。' }
  }
  if (/connect|connection|network|dns|httpx|连接|网络/.test(text)) {
    return { title: 'Provider 连接失败', reason, suggestion: '检查 Base URL、网络代理和服务状态，确认请求确实到达了 Provider。' }
  }
  if (/maven|compile|build|database|h2|sql|启动|编译|数据库/.test(text)) {
    return { title: '成果物构建或运行失败', reason, suggestion: '优先检查依赖、数据库配置和启动日志，修复后重新运行验证节点。' }
  }
  return { title: 'Agent 执行失败', reason, suggestion: '展开详细日志查看完整上下文，再根据错误指纹回退给对应责任 Agent。' }
}

function errorSummaryFor(stepId: string) {
  return errorSummaries.value[stepId] ?? null
}

function hasAgentError(stepId: string) {
  return Boolean(errorSummaries.value[stepId])
}

function toggleErrorSummary(stepId: string) {
  if (!hasAgentError(stepId)) return
  errorSummaryNodeId.value = errorSummaryNodeId.value === stepId ? null : stepId
  selectedNodeId.value = stepId
}

function selectNode(node: any) {
  selectedNodeId.value = node.id
}

function updateNode(id: string, status: Status, extra: Record<string, unknown> = {}) {
  nodes.value = nodes.value.map((node) => node.id === id ? { ...node, data: { ...node.data, status, ...extra } } : node)
  syncEdgeActivity()
}

function syncEdgeActivity() {
  const statusByNode = new Map(nodes.value.map((node) => [node.id, String(node.data?.status ?? 'PENDING')]))
  edges.value = edges.value.map((edge) => {
    const sourceStatus = statusByNode.get(edge.source)
    const targetStatus = statusByNode.get(edge.target)
    const active = sourceStatus === 'RUNNING' || targetStatus === 'RUNNING'
    const completed = sourceStatus === 'SUCCESS' && ['SUCCESS', 'SKIPPED'].includes(targetStatus ?? '')
    return { ...edge, animated: active, className: active ? 'edge-active' : completed ? 'edge-complete' : '' }
  })
}

function finishOutput(id: string, output: string, duration = 500, retryCount = 0) {
  const node = nodes.value.find((item) => item.id === id)
  if (!node) return
  outputs.value[id] = { id, name: String(node.data.label), role: String(node.data.role), status: 'SUCCESS', output, duration, retryCount, agentId: node.data.agentId }
}

function persistActiveRun() {
  if (!runId.value || runId.value.startsWith('demo_')) return
  try {
    localStorage.setItem(activeRunStorageKey, JSON.stringify({ runId: runId.value, workflowId: activeWorkflowId.value, startedAt: runStartedAt, lastEventId: eventCursorByRun.get(runId.value) ?? 0 }))
  } catch {
    // Storage can be unavailable in private browsing; the live stream still works.
  }
}

function clearActiveRun() {
  try {
    localStorage.removeItem(activeRunStorageKey)
  } catch {
    // Ignore storage failures.
  }
}

function persistSelectedWorkflow() {
  try {
    localStorage.setItem(selectedWorkflowStorageKey, activeWorkflowId.value)
  } catch {
    // Ignore storage failures.
  }
}

function readSelectedWorkflow() {
  try {
    return localStorage.getItem(selectedWorkflowStorageKey)
  } catch {
    return null
  }
}

function readActiveRun(): { runId: string; workflowId: string; startedAt?: number; lastEventId?: number } | null {
  try {
    const raw = localStorage.getItem(activeRunStorageKey)
    if (!raw) return null
    const saved = JSON.parse(raw)
    if (!saved?.runId || !saved?.workflowId || String(saved.runId).startsWith('demo_')) return null
    return { runId: String(saved.runId), workflowId: String(saved.workflowId), startedAt: Number(saved.startedAt) || undefined, lastEventId: Number(saved.lastEventId) || 0 }
  } catch {
    return null
  }
}

function parseEventMessage(message: MessageEvent) {
  try {
    const event = JSON.parse(String(message.data)) as Record<string, any>
    const eventId = String(event.id ?? message.lastEventId ?? '').trim()
    if (eventId) {
      const eventRunId = String(event.runId ?? runId.value ?? '').trim()
      const dedupeKey = `${eventRunId}:${eventId}`
      if (seenSseEventIds.has(dedupeKey)) return
      seenSseEventIds.add(dedupeKey)
      const numericId = Number(eventId)
      if (eventRunId && Number.isFinite(numericId)) {
        eventCursorByRun.set(eventRunId, Math.max(eventCursorByRun.get(eventRunId) ?? 0, numericId))
        if (eventRunId === runId.value) persistActiveRun()
      }
    }
    handleEvent(event)
  } catch {
    addLog('收到无法解析的后端事件', 'system', 'failed')
  }
}

function handleEvent(event: Record<string, any>) {
  const type = event.type as string
  const payload = event.payload ?? {}
  const stepId = payload.stepId as string | undefined
  if (type.startsWith('coding_loop.')) {
    const toolLabels: Record<string, string> = {
      'repo.tree': '查看目录', 'repo.search': '搜索代码', 'repo.read': '读取文件',
      'git.status': '查看 Git 状态', 'git.diff': '查看代码差异', 'git.log': '查看提交记录',
      'fs.create': '创建文件', 'fs.patch': '定点修改', 'fs.append': '分块追加',
    }
    const tool = toolLabels[String(payload.tool ?? '')] ?? String(payload.tool ?? '工具动作')
    const filePath = String(payload.metadata?.path ?? '')
    const actionNumber = Number(payload.sequence ?? 0)
    const actionSuffix = actionNumber ? ' · 动作 ' + actionNumber : ''
    if (type === 'coding_loop.iteration_started') {
      addLog((stepId ?? '编码 Agent') + ' 开始编码循环第 ' + Number(payload.iteration ?? 0) + ' 轮', 'step', 'running', stepId)
    } else if (type === 'coding_loop.action_started') {
      addLog((stepId ?? '编码 Agent') + ' 正在' + tool + actionSuffix, 'step', 'running', stepId, filePath)
    } else if (type === 'coding_loop.action_completed') {
      const success = Boolean(payload.success)
      addLog((stepId ?? '编码 Agent') + ' ' + tool + (success ? '已完成' : '失败') + actionSuffix, 'step', success ? 'success' : 'waiting', stepId, [filePath, payload.error].filter(Boolean).join(' · '))
    } else if (type === 'coding_loop.action_reconciled') {
      const complete = String(payload.status ?? '') === 'COMPLETED'
      addLog((stepId ?? '编码 Agent') + ' 已核对中断动作 · ' + tool + actionSuffix, 'step', complete ? 'success' : 'waiting', stepId, [filePath, payload.reason].filter(Boolean).join(' · '))
    } else if (type === 'coding_loop.action_rejected') {
      addLog((stepId ?? '编码 Agent') + ' 请求的' + tool + '被安全策略拒绝', 'step', 'waiting', stepId, String(payload.reason ?? ''))
    } else if (type === 'coding_loop.action_limit') {
      addLog((stepId ?? '编码 Agent') + ' 已达到工具动作上限', 'step', 'waiting', stepId, '上限：' + Number(payload.maxToolActions ?? 0) + ' 次')
    } else if (type === 'coding_loop.protocol_error') {
      addLog((stepId ?? '编码 Agent') + ' 输出格式未符合工具协议', 'step', 'waiting', stepId, String(payload.reason ?? ''))
    } else if (type === 'coding_loop.no_progress') {
      addLog((stepId ?? '编码 Agent') + ' 重复动作无进展 · 已熔断', 'step', 'failed', stepId, String(payload.reason ?? ''))
    } else if (type === 'coding_loop.final_pending_validation') {
      addLog((stepId ?? '编码 Agent') + ' 已提交候选结果 · 等待外层验证', 'step', 'waiting', stepId, String(payload.message ?? '模型自报完成不代表验证通过'))
    } else if (type === 'coding_loop.repair_candidate') {
      const files = Array.isArray(payload.files) ? payload.files.join('、') : ''
      addLog((stepId ?? '编码 Agent') + ' 已在隔离候选中修复 · 等待目标 Gate', 'step', 'waiting', stepId, files)
    } else if (type === 'coding_loop.repair_interrupted') {
      addLog((stepId ?? '编码 Agent') + ' 工具式修复未完成 · 将回退到受控修复流程', 'step', 'waiting', stepId, String(payload.reason ?? ''))
    }
  }
  if (stepId) {
    const status = (payload.status ?? type.split('.')[1]?.toUpperCase()) as Status
    if (['PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'WAITING_APPROVAL', 'WAITING_CLARIFICATION'].includes(status)) updateNode(stepId, status)
    if (type === 'step.completed') {
      delete errorSummaries.value[stepId]
      if (errorSummaryNodeId.value === stepId) errorSummaryNodeId.value = null
      updateAgentTokenUsage(stepId, payload.tokens, 'SUCCESS', Number(payload.retryCount ?? 0))
      finishOutput(stepId, String(payload.output ?? ''), Number(payload.durationMs ?? 0), Number(payload.retryCount ?? 0))
      const completionLabel = payload.fallback ? `${stepId} 已完成 · 已使用本地预算兜底` : `${stepId} 已完成 · ${payload.durationMs ?? 0}ms`
      const completionDetail = payload.fallback
        ? `原因：${localizeProviderMessage(compactLogValue(payload.fallbackReason), 'Runtime Plan 输出不完整')} · 工作流继续执行`
        : runtimeLogDetail(payload)
      addLog(completionLabel, 'step', 'success', stepId, completionDetail)
    } else if (type === 'step.started') {
      delete errorSummaries.value[stepId]
      if (errorSummaryNodeId.value === stepId) errorSummaryNodeId.value = null
      addLog(`${stepId} 已开始`, 'step', 'running', stepId, runtimeLogDetail(payload))
    } else if (type === 'step.failed') {
      updateAgentTokenUsage(stepId, payload.tokens, 'FAILED', Number(payload.retryCount ?? 0))
      const errorMessage = stepFailureMessage(payload)
      errorSummaries.value[stepId] = summarizeAgentError(payload)
      outputs.value[stepId] = { id: stepId, name: stepId, role: '执行错误', status: 'FAILED', output: errorMessage }
      addLog(`${stepId} 失败 · ${errorMessage}`, 'step', 'failed', stepId, failureLogDetail(payload))
    } else if (type === 'step.retrying') {
      const retryLabel = payload.automaticRecovery ? `${stepId} 正在自动恢复` : `${stepId} 正在重试`
      const attemptNumber = Number(payload.retryCount ?? 0) + 1
      addLog(`${retryLabel} · 思考强度 ${thinkingLabel(payload.reasoningEffort ?? 'low')} · 第 ${attemptNumber} 次请求`, 'step', 'waiting', stepId, `原因：${localizeProviderMessage(compactLogValue(payload.reason), '可重试的 Provider 错误')}`)
    } else if (type === 'step.continuing') {
      addLog(`${stepId} 正在续写 · 第 ${Number(payload.retryCount ?? 0) + 1} 次`, 'step', 'waiting', stepId, `已保存输出：${Number(payload.outputTokens ?? 0)} Token · 将从截断位置继续生成`)
    } else if (type === 'step.artifact_plan_recovering') {
      addLog(`${stepId} 的成果物规划正在自动恢复`, 'step', 'waiting', stepId, `原因：${String(payload.reason ?? '文件清单 JSON 不完整')} · 输出上限 ${Number(payload.previousMaxTokens ?? 0)} → ${Number(payload.nextMaxTokens ?? 0)} Token`)
    } else if (type === 'step.artifact_plan_fallback') {
      addLog(`${stepId} 已启用本地成果物规划兜底`, 'step', 'waiting', stepId, `${String(payload.reason ?? 'Provider 未返回完整文件清单')} · 已保留 ${Number(payload.fileCount ?? 0)} 个必要文件`)
    } else if (type === 'step.artifact_planned') {
      const files = Array.isArray(payload.files) ? payload.files : []
      const source = payload.planSource === 'deterministic_fallback' ? ' · 本地保守规划' : ''
      addLog(`${stepId} 已规划成果物 · ${Number(payload.fileCount ?? files.length)} 个文件${source}`, 'step', 'waiting', stepId, files.map((item: Record<string, any>) => `${item.name}（约 ${Number(item.estimatedTokens ?? 0)} Token）`).join('、'))
    } else if (type === 'step.artifact_split_planned') {
      const parts = Array.isArray(payload.parts) ? payload.parts : []
      addLog(`${stepId} 已细分 ${String(payload.fileName ?? '文件')} · ${parts.length} 个任务`, 'step', 'waiting', stepId, parts.map((item: Record<string, any>, index: number) => `${index + 1}. ${item.id}（约 ${Number(item.estimatedTokens ?? 0)} Token）`).join('、'))
    } else if (type === 'step.artifact_generating') {
      const budgetNote = payload.budgetRaised
        ? ` · Agent 规划 ${Number(payload.agentMaxTokens ?? 0)} Token，已按文件级预算提升`
        : ''
      const partNote = payload.partId ? ` · 片段 ${String(payload.partId)}（${Number(payload.partIndex ?? 0)} / ${Number(payload.partCount ?? 0)}）` : ''
      addLog(`${stepId} 正在生成 ${String(payload.fileName ?? '文件')}`, 'step', 'running', stepId, `第 ${Number(payload.fileIndex ?? 0)} / ${Number(payload.fileCount ?? 0)} 个文件${partNote} · 单次上限 ${Number(payload.maxTokens ?? 0)} Token${budgetNote}`)
    } else if (type === 'step.artifact_repairing') {
      const repairScope = payload.scope === 'suffix' ? '从截断位置补齐' : '定点修复'
      addLog(`${stepId} 正在${repairScope} ${String(payload.fileName ?? '文件')}`, 'step', 'waiting', stepId, `${payload.partId ? `片段 ${String(payload.partId)} · ` : ''}第 ${Number(payload.repairAttempt ?? 0)} 次 · ${String(payload.reason ?? '结构校验未通过')}`)
    } else if (type === 'step.artifact_continuing') {
      addLog(`${stepId} 正在执行最后一级续写兜底 ${String(payload.fileName ?? '文件')}`, 'step', 'waiting', stepId, `${payload.partId ? `片段 ${String(payload.partId)} · ` : ''}第 ${Number(payload.continuationAttempt ?? 0)} 次 · ${String(payload.reason ?? '当前内容仍不完整')}`)
    } else if (type === 'step.artifact_validated') {
      addLog(`${stepId} 已校验 ${String(payload.fileName ?? '文件')}`, 'step', 'success', stepId, `第 ${Number(payload.fileIndex ?? 0)} / ${Number(payload.fileCount ?? 0)} 个文件通过完整性检查`)
    } else if (type === 'step.artifact_dependency_normalized') {
      addLog(`${stepId} 已纠正依赖声明`, 'step', 'waiting', stepId, `${String(payload.fileName ?? '')} · ${String(payload.reason ?? '')}`)
    } else if (type === 'collaboration.started') {
      addLog(`${stepId} 开始跨 Agent 协商`, 'step', 'waiting', stepId, `参与方：${Array.isArray(payload.owners) ? payload.owners.join('、') : ''} · 最多 ${Number(payload.rounds ?? 0)} 轮`)
    } else if (type === 'collaboration.message') {
      const actNames: Record<string, string> = { query: '询问', propose: '提议', review: '复核', error: '回复失败', decision: '协商结论' }
      addLog(`${stepId} 协商 · ${actNames[String(payload.act)] ?? String(payload.act ?? '')}`, 'step', payload.act === 'error' ? 'waiting' : 'running', stepId, `${String(payload.sender ?? '')} → ${String(payload.recipient ?? '')} · ${String(payload.summary ?? '')}`)
    } else if (type === 'collaboration.completed') {
      const statusNames: Record<string, string> = { consensus: '已形成建议', disputed: '存在分歧', contract_change_requested: '提出合同变更', incomplete: '未形成完整建议', advisory: '已给出单轮建议' }
      addLog(`${stepId} 跨 Agent 协商${statusNames[String(payload.status)] ?? '结束'}`, 'step', 'waiting', stepId, '协商结果仅用于指导责任 Agent；冻结合同与真实验证门禁不变。')
    } else if (type === 'collaboration.unavailable') {
      addLog(`${stepId} 协商不可用，继续按确定性规则修复`, 'step', 'waiting', stepId, String(payload.reason ?? ''))
    } else if (type === 'architecture.contract_failed') {
      const fact = (payload.failureFact ?? {}) as Record<string, any>
      upsertRepairTrace(stepId || 'architecture', payload, '合同预检失败', String(fact.summary ?? '架构合同校验失败'))
      addLog(`architecture 合同预检失败 · ${String(fact.summary ?? '请查看详细证据')}`, 'step', 'waiting', stepId, String(fact.message ?? ''))
    } else if (type === 'architecture.repair_started') {
      upsertRepairTrace(stepId || 'architecture', payload, 'Architecture 定点修复中')
      addLog(`architecture 第 ${Number(payload.repairAttempt ?? 1)} 轮合同修复`, 'step', 'running', stepId)
    } else if (type === 'architecture.repair_rejected') {
      upsertRepairTrace(stepId || 'architecture', payload, '修复提案未通过验收', String(payload.reason ?? ''))
      addLog('architecture 修复提案未满足合同要求', 'step', 'waiting', stepId, String(payload.reason ?? ''))
    } else if (type === 'architecture.target_gate_completed') {
      if (Number(payload.repairAttempt ?? 0) > 0) {
        upsertRepairTrace(stepId || 'architecture', payload, '合同目标 Gate 通过', '合同重新校验通过，等待后续完整回归')
        addLog('architecture 合同目标 Gate 通过', 'step', 'success', stepId)
      }
    } else if (type === 'architecture.repair_circuit_open') {
      upsertRepairTrace(stepId || 'architecture', payload, '无进展熔断', String(payload.reason ?? ''))
      addLog('architecture 合同修复已熔断', 'step', 'failed', stepId, String(payload.reason ?? ''))
    } else if (type === 'repair.routed') {
      const plan = payload.plan ?? {}
      const owners = Array.isArray(plan.owners) ? plan.owners.join('、') : String(plan.owner_step ?? '')
      upsertRepairTrace(stepId, payload, '已定位', String(plan.reason ?? payload.reason ?? ''))
      addLog(`${stepId} 已定位失败责任`, 'step', plan.repairable === false ? 'waiting' : 'running', stepId, `责任 Agent：${owners || '平台'} · 处理：${String(plan.action ?? '等待处理')} · ${String(plan.reason ?? payload.reason ?? '')}`)
    } else if (type === 'repair.round_started') {
      upsertRepairTrace(stepId, payload, '责任 Agent 整改中')
      const plan = payload.plan ?? {}
      const owners = Array.isArray(plan.owners) ? plan.owners.join('、') : '待定位'
      addLog(`${stepId} 开始第 ${Number(payload.repairAttempt ?? 1)} 轮自动修复`, 'step', 'running', stepId, `责任 Agent：${owners} · 策略：${String(plan.strategy ?? '定点修复')}`)
    } else if (type === 'repair.target_gate_completed') {
      const summary = String(payload.result?.summary ?? (payload.passed ? '目标 Gate 已通过' : '目标 Gate 未通过'))
      upsertRepairTrace(stepId, payload, payload.passed ? '目标 Gate 通过' : payload.madeProgress ? '目标 Gate 已推进' : '目标 Gate 未推进', summary)
      addLog(`${stepId} 第 ${Number(payload.repairAttempt ?? 1)} 轮目标 Gate ${payload.passed ? '通过' : '未通过'}`, 'step', payload.passed ? 'success' : 'waiting', stepId, payload.stageTransition ? `${payload.stageTransition} · ${summary}` : summary)
    } else if (type === 'repair.full_regression_completed') {
      const regressed = Array.isArray(payload.regressedChecks) ? payload.regressedChecks.map(String) : []
      const unverified = Array.isArray(payload.unverifiedChecks) ? payload.unverifiedChecks.map(String) : []
      const summary = `${regressed.length ? `已通过检查出现回归：${regressed.join('、')} · ` : ''}${unverified.length && payload.result?.status === 'passed' ? `完整回归未重验：${unverified.join('、')} · ` : ''}${String(payload.result?.summary ?? (payload.passed ? '完整回归通过' : '完整回归失败'))}`
      upsertRepairTrace(stepId, payload, payload.passed ? '完整回归通过' : '完整回归失败', summary)
      addLog(`${stepId} 第 ${Number(payload.repairAttempt ?? 1)} 轮完整回归${payload.passed ? '通过' : '失败'}`, 'step', payload.passed ? 'success' : 'failed', stepId, summary)
    } else if (type === 'repair.completed') {
      upsertRepairTrace(stepId, payload, payload.passed ? '自动修复完成' : '自动修复停止', String(payload.reason ?? ''))
      runStats.repair.repairRounds = Math.max(runStats.repair.repairRounds, Number(payload.attempts ?? 0))
      if (payload.passed) runStats.repair.successfulRepairs += 1
    } else if (type === 'repair.candidate_created') {
      upsertRepairTrace(stepId, payload, '候选工作区已建立')
    } else if (type === 'step.validation_missing_declaration') {
      addLog(`${stepId} 编译缺少类 ${String(payload.symbol ?? '')}，交由后端补建`, 'step', 'waiting', stepId, `引用：${String(payload.sourceFile ?? '')} · 候选文件：${String(payload.fileName ?? '')} · 第 ${Number(payload.repairAttempt ?? 1)} 轮`)
    } else if (type === 'repair.candidate_promoted') {
      upsertRepairTrace(stepId, payload, '候选版本已提交')
    } else if (type === 'repair.candidate_discarded') {
      upsertRepairTrace(stepId, payload, '候选版本已撤回', String(payload.reason ?? ''))
    } else if (type === 'repair.circuit_open') {
      upsertRepairTrace(stepId, payload, '无进展熔断', String(payload.reason ?? ''))
      addLog(`${stepId} 已停止无效修复`, 'step', 'waiting', stepId, String(payload.reason ?? '连续整改没有推进验证阶段，已保留原成果物和完整诊断。'))
    } else if (type === 'repair.escalated') {
      addLog(`${stepId} 的失败需要人工处理`, 'step', 'failed', stepId, String(payload.reason ?? '当前失败无法安全映射到源码责任 Agent。'))
    } else if (type === 'step.validation_short_circuited') {
      const ids = Array.isArray(payload.failedCheckIds) ? payload.failedCheckIds.join('、') : ''
      addLog(`${stepId} 已快速停止本轮验证`, 'step', 'waiting', stepId, `${String(payload.reason ?? '预检失败，已跳过耗时检查。')}${ids ? ` · 失败项：${ids}` : ''}`)
    } else if (type === 'step.validation_started') {
      const profile = payload.profile ?? {}
      addLog(`${stepId} 开始成果物验证`, 'step', 'running', stepId, `目标：${Array.isArray(profile.targets) ? profile.targets.join('、') : '自动识别'} · 构建：${profile.build === false ? '关闭' : '开启'} · 启动检查：${profile.startup === false ? '关闭' : '开启'}`)
    } else if (type === 'step.validation_stage_started') {
      addLog(`${stepId} 开始${String(payload.stage ?? '当前')}阶段验证`, 'step', 'running', stepId, `范围：${String(payload.validationScope ?? payload.scope ?? 'full_regression')}`)
    } else if (type === 'step.validation_stage_completed') {
      addLog(`${stepId} 完成${String(payload.stage ?? '当前')}阶段验证`, 'step', payload.passed ? 'success' : 'failed', stepId, `耗时：${formatDuration(Number(payload.durationMs ?? 0))} · ${payload.passed ? '通过' : '未通过'}`)
    } else if (type === 'step.validation_check') {
      const check = payload.check ?? {}
      const tone = check.status === 'passed' ? 'success' : check.status === 'blocked' ? 'waiting' : 'failed'
      const evidence = check.evidence ?? {}
      const screenshots = Array.isArray(evidence.screenshotPaths) ? evidence.screenshotPaths.map(String) : evidence.screenshotPath ? [String(evidence.screenshotPath)] : []
      const evidenceDetail = screenshots.length ? `\n浏览器证据：${screenshots.join('、')}` : ''
      addLog(`${stepId} · ${String(check.label ?? '验证检查')} · ${check.status === 'passed' ? '通过' : check.status === 'blocked' ? '阻塞' : '失败'}`, 'step', tone, stepId, `${String(check.message ?? '')}${check.command ? ` · ${String(check.command)}` : ''}${check.output ? `\n${String(check.output)}` : ''}${evidenceDetail}`)
    } else if (type === 'step.validation_repairing') {
      addLog(`${stepId} 正在定点修复成果物`, 'step', 'waiting', stepId, `目标：${String(payload.target ?? '')} · 文件：${String(payload.fileName ?? '')} · 第 ${Number(payload.repairAttempt ?? 1)} 轮 · 上限：${Number(payload.maxTokens ?? 0)} Token`)
    } else if (type === 'step.validation_repair_dispatched') {
      addLog(`${stepId} 已将修复指令流转给 ${String(payload.agentId ?? payload.owner ?? '责任 Agent')}`, 'step', 'waiting', stepId, `责任域：${String(payload.owner ?? payload.target ?? '')} · 文件：${String(payload.fileName ?? '')} · 第 ${Number(payload.repairAttempt ?? 1)} 轮`)
    } else if (type === 'step.validation_repaired') {
      addLog(`${stepId} 已重新验证候选整改`, 'step', payload.passed ? 'success' : 'waiting', stepId, `第 ${Number(payload.repairAttempt ?? 1)} 轮 · 文件：${Array.isArray(payload.files) ? payload.files.join('、') : ''} · ${payload.passed ? '全部验证通过，提交新版本' : payload.madeProgress ? '验证阶段已推进，候选仍未交付' : '未推进验证阶段，撤回候选修改'}`)
    } else if (type === 'step.validation_owner_reexecuting') {
      addLog(`${stepId} 开始关联源码整改`, 'step', 'waiting', stepId, String(payload.reason ?? '根据当前真实源码修复关联文件，保留原有合同。'))
    } else if (type === 'step.validation_candidate_rejected' || type === 'step.validation_no_progress') {
      addLog(`${stepId} ${type === 'step.validation_no_progress' ? '已停止无效整改' : '已撤回候选版本'}`, 'step', 'waiting', stepId, String(payload.reason ?? '保留原成果物与诊断日志。'))
    } else if (type === 'step.validation_repair_failed' || type === 'step.validation_repair_skipped') {
      addLog(`${stepId} 成果物定点修复未生效`, 'step', 'failed', stepId, `${String(payload.fileName ?? payload.target ?? '')} · ${String(payload.error ?? payload.reason ?? '无法定位可修复文件')}`)
    } else if (type === 'step.validation_succeeded') {
      if (runStats.repairTrace.some((trace) => trace.stepId === 'architecture' && trace.attempt > 0)) {
        const latest = [...runStats.repairTrace].filter((trace) => trace.stepId === 'architecture').sort((a, b) => b.attempt - a.attempt)[0]
        upsertRepairTrace('architecture', { repairAttempt: latest.attempt }, '完整回归通过', 'Tester 成果物验证通过')
      }
      addLog(`${stepId} 成果物验证通过`, 'step', 'success', stepId, '结构、构建和启动检查均已通过，允许进入最终交付。')
    } else if (type === 'step.validation_failed') {
      if (runStats.repairTrace.some((trace) => trace.stepId === 'architecture' && trace.attempt > 0)) {
        const latest = [...runStats.repairTrace].filter((trace) => trace.stepId === 'architecture').sort((a, b) => b.attempt - a.attempt)[0]
        upsertRepairTrace('architecture', { repairAttempt: latest.attempt }, '完整回归失败', String(payload.result?.summary ?? ''))
      }
      const result = payload.result ?? {}
      addLog(`${stepId} 成果物验证未通过`, 'step', 'failed', stepId, String(result.summary ?? '请查看上方各项验证检查结果。'))
    } else if (type === 'step.budget_planned') {
      const difficulty = payload.difficulty === 'high' ? '高' : payload.difficulty === 'medium' ? '中' : payload.difficulty === 'low' ? '低' : '动态'
      const signals = Array.isArray(payload.difficultySignals) && payload.difficultySignals.length ? ` · 判断依据：${payload.difficultySignals.slice(0, 3).join('、')}` : ''
      addLog(`${stepId} 已按需求难度分配 Token`, 'step', 'waiting', stepId, `难度：${difficulty} · 建议上限：${Number(payload.recommendedMaxTokens ?? 0)} Token · 本次计划：${Number(payload.plannedMaxTokens ?? 0)} Token${signals}`)
    } else if (type === 'step.context_packed') {
      addLog(`${stepId} 已整理上下文`, 'step', 'waiting', stepId, `输入约 ${Number(payload.inputTokens ?? 0)} Token${Array.isArray(payload.truncatedKeys) && payload.truncatedKeys.length ? ` · 已压缩：${payload.truncatedKeys.join('、')}` : ''}`)
    } else if (type === 'step.budget_adjusted') {
      addLog(`${stepId} 已调整输出预算`, 'step', 'waiting', stepId, `请求 ${Number(payload.requestedMaxTokens ?? 0)} Token · 实际 ${Number(payload.effectiveMaxTokens ?? 0)} Token · 上下文窗口 ${Number(payload.contextWindow ?? 0)} Token`)
    } else if (type === 'step.skipped') {
      addLog(`${stepId} 已跳过`, 'step', 'waiting', stepId, localizeProviderMessage(compactLogValue(payload.reason), '已按 Architecture 策略跳过'))
    } else if (type === 'step.waiting_approval') {
      // Treat the durable step event as a complete pause signal.  The
      // workflow-level event normally follows immediately, but this fallback
      // keeps the approval UI usable across Worker hand-off or SSE reconnects.
      runStatus.value = 'WAITING_APPROVAL'
      selectedNodeId.value = stepId
      persistActiveRun()
      addLog('架构已完成，等待人工确认', 'approval', 'waiting', stepId, '工作流将在这里暂停，直到审批完成。')
    }
  }
  if (type === 'step.runtime_adjusted' && stepId) {
    addLog(`${stepId} · ${localizeProviderMessage(payload.warning, '运行策略已按模型能力调整')}`, 'step', 'waiting', stepId, `请求：${thinkingLabel(payload.requestedThinking)} · 实际：${thinkingLabel(payload.effectiveThinking)}`)
  }
  if (type === 'step.skills_resolved' && stepId) {
    const applied = Array.isArray(payload.applied) ? payload.applied : []
    const missing = Array.isArray(payload.missing) ? payload.missing : []
    const skipped = Array.isArray(payload.skipped) ? payload.skipped : []
    const label = applied.length ? applied.map((item: Record<string, any>) => String(item.id ?? '')).filter(Boolean).join('、') : '未加载'
    const detail = [
      `模式：${payload.requestedMode === 'on' ? '开启' : payload.requestedMode === 'off' ? '关闭' : 'Auto'}`,
      `难度：${payload.difficulty ?? 'low'}`,
      `注入：${Number(payload.injectionChars ?? 0)} 字符`,
      missing.length ? `缺失：${missing.join('、')}` : '',
      skipped.length ? `跳过：${skipped.map((item: Record<string, any>) => String(item.id ?? '')).filter(Boolean).join('、')}` : '',
    ].filter(Boolean).join(' · ')
    addLog(`${stepId} Skill 已解析 · ${label}`, 'step', applied.length ? 'success' : 'waiting', stepId, detail)
  }
  if (type === 'workflow.recovered') {
    const recoveredPauseStatus = payload.status === 'WAITING_CLARIFICATION' ? 'WAITING_CLARIFICATION' : 'WAITING_APPROVAL'
    runStatus.value = payload.paused ? recoveredPauseStatus : 'RUNNING'
    persistActiveRun()
    addLog(
      payload.paused ? '工作流暂停状态已恢复 · 等待人工操作' : `工作流已恢复 · 第 ${Number(payload.recoveryCount ?? 0)} 次`,
      'workflow',
      'waiting',
      undefined,
      `原状态：${String(payload.previousStatus ?? '未知')} · 已从持久化快照继续执行`,
    )
  }
  if (type === 'workflow.recovery_exhausted') {
    runStatus.value = 'FAILED'
    addLog(`自动恢复已熔断 · ${String(payload.reason ?? '已达到恢复次数上限')}`, 'workflow', 'failed')
    clearActiveRun()
  }
  if (type === 'workflow.started') {
    runStatus.value = 'RUNNING'
    if (!runDurationSyncedAt.value) syncRunDuration(0)
    persistActiveRun()
    addLog('工作流已开始 · 执行层级已解锁', 'workflow', 'running')
  }
  if (type === 'workflow.requirement_routed') {
    const requiredCapabilities = payload.requiredCapabilities ?? []
    const optionalCapabilities = payload.optionalCapabilities ?? []
    addLog('需求已完成能力路由', 'workflow', 'success', undefined, `必需能力：${requiredCapabilities.length ? requiredCapabilities.join('、') : '未识别'} · 可选能力：${optionalCapabilities.length ? optionalCapabilities.join('、') : '无'} · 置信度：${payload.confidence ?? 'low'}${payload.needsClarification ? ' · 需要二次确认' : ''}`)
  }
  if (type === 'workflow.budget_planned') {
    const plan = payload.plan ?? {}
    const difficulty = plan.difficulty === 'high' ? '高' : plan.difficulty === 'medium' ? '中' : '低'
    const strength = plan.model_strength === 'strong' ? '强' : plan.model_strength === 'standard' ? '标准' : plan.model_strength === 'basic' ? '基础' : '未判定'
    if (payload.fallback) {
      addLog('Token 预算评估 Agent 未产出有效计划 · 已启用本地预检', 'workflow', 'waiting', undefined, `本地难度：${difficulty} · 工作流不会被阻断`)
    } else {
      addLog('Token 预算评估已完成', 'workflow', 'success', undefined, `需求难度：${difficulty} · 模型强度：${strength} · 置信度：${Math.round(Number(plan.confidence ?? 0) * 100)}%`)
    }
  }
  if (type === 'workflow.policy_decided') {
    const decision = payload.decision ?? {}
    const routingGuard = decision.routing_guard ?? {}
    const requiredCapabilities = decision.required_capabilities ?? []
    const policy = payload.policy ?? {}
    addLog('需求能力已识别', 'workflow', 'success', undefined, `必需能力：${requiredCapabilities.length ? requiredCapabilities.join('、') : '未识别'}${decision.needs_clarification ? ' · 存在待确认边界' : ''}`)
    addLog('路由策略已确定', 'workflow', 'success', undefined, `${policy.route_mode === 'static_frontend' ? '静态前端路线' : '完整实现路线'}${decision.needs_clarification ? ' · 需求待确认但已保留实现分支' : ''}`)
    if (routingGuard.correction_applied) {
      addLog('路由保护已纠正 Architecture 判断', 'workflow', 'waiting', undefined, `检测到${(routingGuard.matched_signals ?? []).join('、')}，已保留 Backend 与 Frontend`)
    }
    if (payload.fallback) {
      addLog('Architecture 未生成可解析结果 · 已使用保守架构兜底', 'workflow', 'waiting', undefined, `原因：${localizeProviderMessage(compactLogValue(payload.fallbackReason), '结构化输出异常')} · 已进入人工确认`)
    }
    const projectType = decision.project_type === 'static_html' ? '静态 HTML' : decision.project_type === 'backend_service' ? '后端服务' : decision.project_type === 'web_app' ? 'Web 应用' : '待确认项目'
    const backendPlan = decision.backend_required === false ? 'Backend：跳过' : 'Backend：保留'
    addLog(`Architecture 已完成分类 · ${projectType}`, 'workflow', 'success', undefined, `${backendPlan} · 复杂度：${decision.complexity ?? '-'} · Token：Auto 自适应`)
  }
  if (type === 'workflow.waiting_approval') {
    const currentDuration = liveDurationMs.value
    runStatus.value = 'WAITING_APPROVAL'
    if (payload.durationMs != null) syncRunDuration(Number(payload.durationMs))
    else syncRunDuration(currentDuration)
    if (payload.approvalDurationMs != null) runStats.approvalDurationMs = numericValue(payload.approvalDurationMs)
    persistActiveRun()
    addLog('工作流已暂停 · 等待你的决定', 'workflow', 'waiting')
  }
  if (type === 'workflow.clarification_required') {
    runStatus.value = 'WAITING_CLARIFICATION'
    setClarificationRequest(payload.request ?? payload.clarificationRequest ?? null)
    selectedNodeId.value = 'architecture'
    updateNode('architecture', 'WAITING_CLARIFICATION')
    const fields = Array.isArray(clarificationRequest.value?.unresolved_fields) ? clarificationRequest.value.unresolved_fields.join('、') : '交付边界'
    addLog('需求需要二次确认', 'workflow', 'waiting', undefined, `${String(clarificationRequest.value?.reason ?? '需求边界未明确')} · 待确认：${fields}`)
  }
  if (type === 'step.waiting_clarification') {
    runStatus.value = 'WAITING_CLARIFICATION'
    setClarificationRequest(payload.request ?? clarificationRequest.value)
    if (stepId) updateNode(stepId, 'WAITING_CLARIFICATION')
  }
  if (type === 'workflow.clarification_answered') {
    runStatus.value = 'RUNNING'
    clarificationRequest.value = null
    addLog('需求确认已写回蓝图，工作流继续执行', 'workflow', 'success', undefined, JSON.stringify(payload.answers ?? {}, null, 2))
  }
  if (type === 'workflow.blueprint_created') {
    addLog('Project Blueprint 已生成', 'workflow', 'success', undefined, `状态：${String(payload.status ?? 'DRAFT')} · 作为后续 Agent 的统一契约`)
  }
  if (type === 'workflow.blueprint_frozen') {
    addLog('Project Blueprint 已冻结', 'workflow', 'success', undefined, '审批通过后，后续 Agent 必须遵守同一技术栈、API 和成果物归属契约')
  }
  if (type === 'validation.evidence' || type === 'integration.evidence') {
    const evidence = Array.isArray(payload.evidence) ? payload.evidence : []
    const failures = Array.isArray(payload.failureFacts) ? payload.failureFacts : []
    addLog('结构化证据已记录', 'workflow', failures.length ? 'failed' : 'success', undefined, `证据 ${evidence.length} 条${failures.length ? ` · 失败事实 ${failures.length} 条` : ''}`)
  }
  if (type === 'artifact.created') {
    const artifact = payload.artifact as Record<string, any> | undefined
    if (artifact?.id) {
      hydrateArtifacts({ artifacts: [artifact] }, false)
      addLog(`产物已生成 · ${String(artifact.name ?? 'artifact')}`, 'system', 'success', undefined, '可在下方打开预览')
    }
  }
  if (type === 'artifact.failed') {
    addLog(`产物生成失败 · ${localizeProviderMessage(payload.error, '未提供错误原因')}`, 'system', 'failed')
  }
  if (type === 'worker.lease_acquired') {
    addLog('后台 Worker 已接管运行', 'system', 'success', undefined, `Worker：${String(payload.workerId ?? '-')} · 动作：${String(payload.action ?? 'start')}`)
  }
  if (type === 'worker.lease_released') {
    addLog('后台 Worker 已释放运行租约', 'system', 'waiting', undefined, `原因：${String(payload.reason ?? '-')}`)
  }
  if (type === 'worker.lease_lost') {
    addLog('后台 Worker 租约已失效 · 等待其他 Worker 从 Checkpoint 接管', 'system', 'failed')
  }
  if (type === 'worker.execution_error') {
    addLog(`后台 Worker 异常 · ${localizeProviderMessage(payload.error, '未知错误')}`, 'system', 'failed')
  }
  if (type === 'worker.control_applied') {
    addLog(`后台已执行控制指令 · ${String(payload.action ?? '-')}`, 'system', 'success')
  }
  if (type === 'worker.control_rejected') {
    addLog(`后台拒绝过期控制指令 · ${String(payload.action ?? '-')}`, 'system', 'waiting', undefined, String(payload.reason ?? '租约或运行状态已变化'))
  }
  if (type === 'workflow.completed') {
    if (payload.durationMs != null) syncRunDuration(Number(payload.durationMs))
    else syncRunDuration(liveDurationMs.value)
    if (payload.approvalDurationMs != null) runStats.approvalDurationMs = numericValue(payload.approvalDurationMs)
    runStatus.value = 'SUCCESS'
    clearActiveRun()
    addLog(`工作流已完成 · ${liveDuration.value}${payload.deliverable ? ' · 已通过交付门禁' : ''}`, 'workflow', 'success')
  }
  if (type === 'workflow.contract_frozen') {
    addLog('交付合同已冻结 · 页面、接口和数据库约束已同步到各 Agent', 'workflow', 'success', undefined, JSON.stringify(payload.contract, null, 2))
  }
  if (type === 'workflow.contract_reopened') {
    runStatus.value = 'RUNNING'
    persistActiveRun()
    addLog(
      '冻结合同已重新打开 · 正在回退架构设计',
      'workflow',
      'waiting',
      undefined,
      `第 ${Number(payload.attempt ?? 1)} 轮 · ${String(payload.reason ?? '下游验证发现合同缺陷')} · 将重新校验并等待你确认`,
    )
  }
  if (type === 'workflow.contract_validated') {
    addLog('架构合同预检通过 · 等待人工确认后冻结', 'workflow', 'success')
  }
  if (type === 'workflow.delivery_checked') {
    addLog(payload.deliverable ? '交付门禁已通过 · 已验证源码与 ZIP 一致' : '交付门禁未通过', 'workflow', payload.deliverable ? 'success' : 'failed', undefined, JSON.stringify(payload.checks, null, 2))
  }
  if (type === 'delivery.repair_started') {
    upsertRepairTrace(String(payload.stepId ?? 'tester'), payload, '交付门禁整改中', String(payload.reason ?? ''))
    addLog('最终交付门禁正在回修', 'workflow', 'waiting', String(payload.stepId ?? 'tester'), `第 ${Number(payload.repairAttempt ?? 1)} 轮 · 缺失：${(payload.missing ?? []).join('、') || '验证证据'}`)
  }
  if (type === 'delivery.repair_completed') {
    upsertRepairTrace(String(payload.stepId ?? 'tester'), payload, payload.passed ? '交付门禁通过' : '交付门禁未通过', `${payload.madeProgress ? '验证状态已推进' : '验证状态未推进'}${Array.isArray(payload.missing) && payload.missing.length ? ` · 仍缺少：${payload.missing.join('、')}` : ''}`)
    addLog(payload.passed ? '最终交付门禁回修通过' : '最终交付门禁回修未通过', 'workflow', payload.passed ? 'success' : 'failed', String(payload.stepId ?? 'tester'), `第 ${Number(payload.repairAttempt ?? 1)} 轮 · ${payload.madeProgress ? '验证状态已推进' : '验证状态未推进'}`)
  }
  if (type === 'delivery.archive_rebuild_started') {
    upsertRepairTrace('platform', { ...payload, repairAttempt: 1 }, '交付压缩包重建中', '平台正在重新打包并核对文件清单')
    addLog('交付压缩包不一致 · 平台正在重新打包', 'workflow', 'waiting', undefined, `缺失：${(payload.missing ?? []).join('、') || '压缩包校验信息'}`)
  }
  if (type === 'delivery.archive_rebuild_completed') {
    upsertRepairTrace('platform', { ...payload, repairAttempt: 1 }, payload.passed ? '压缩包重建通过' : '压缩包重建未通过', Array.isArray(payload.missing) && payload.missing.length ? `仍缺少：${payload.missing.join('、')}` : '源码与 ZIP 清单一致')
    addLog(payload.passed ? '交付压缩包已重建并通过校验' : '交付压缩包重建后仍未通过', 'workflow', payload.passed ? 'success' : 'failed')
  }
  if (type === 'workflow.failed') {
    if (payload.durationMs != null) syncRunDuration(Number(payload.durationMs))
    else syncRunDuration(liveDurationMs.value)
    if (payload.approvalDurationMs != null) runStats.approvalDurationMs = numericValue(payload.approvalDurationMs)
    runStatus.value = 'FAILED'
    clearActiveRun()
    addLog(`工作流失败 · ${localizeProviderMessage(payload.error, '工作流未报告失败原因')}`, 'workflow', 'failed', undefined, `有效运行耗时：${liveDuration.value}`)
  }
  if (type === 'workflow.stopped') {
    if (payload.durationMs != null) syncRunDuration(Number(payload.durationMs))
    else syncRunDuration(liveDurationMs.value)
    if (payload.approvalDurationMs != null) runStats.approvalDurationMs = numericValue(payload.approvalDurationMs)
    runStatus.value = 'STOPPED'
    clearActiveRun()
    addLog(`工作流已由操作员停止 · ${liveDuration.value}`, 'workflow', 'failed')
  }
  if (type === 'workflow.cancelling') {
    addLog('正在安全停止工作流 · 等待底层请求退出', 'workflow', 'waiting')
  }
}

async function connectToRun(id: string) {
  closeEventSource()
  const cursor = eventCursorByRun.get(id) ?? 0
  const source = new EventSource(`${API_BASE}/runs/${id}/events?after=${cursor}`)
  eventSource = source
  let streamErrorLogged = false
  source.onopen = () => { streamErrorLogged = false }
  source.onerror = () => {
    if (isBusy.value && !streamErrorLogged) {
      streamErrorLogged = true
      addLog('实时 SSE 连接中断，页面将自动重连', 'system', 'waiting')
    }
  }
  source.onmessage = parseEventMessage
  const eventTypes = ['workflow.started', 'workflow.queued', 'workflow.recovered', 'workflow.recovery_exhausted', 'workflow.requirement_routed', 'workflow.budget_planned', 'workflow.policy_decided', 'step.started', 'step.completed', 'step.failed', 'step.retrying', 'step.continuing', 'step.repairing', 'step.repaired', 'step.coding_loop_completed', 'coding_loop.iteration_started', 'coding_loop.iteration_completed', 'coding_loop.tool_result', 'step.artifact_plan_recovering', 'step.artifact_plan_fallback', 'step.artifact_planned', 'step.artifact_split_planned', 'step.artifact_generating', 'step.artifact_repairing', 'step.artifact_continuing', 'step.artifact_validated', 'step.artifact_dependency_normalized', 'step.validation_started', 'step.validation_check', 'step.validation_repairing', 'step.validation_repair_dispatched', 'step.validation_repaired', 'step.validation_owner_reexecuting', 'step.validation_candidate_rejected', 'step.validation_no_progress', 'step.validation_repair_failed', 'step.validation_repair_skipped', 'step.validation_succeeded', 'step.validation_failed', 'step.budget_planned', 'step.context_packed', 'step.budget_adjusted', 'step.runtime_adjusted', 'step.skills_resolved', 'step.skipped', 'step.waiting_approval', 'step.waiting_clarification', 'workflow.waiting_approval', 'artifact.created', 'artifact.failed', 'workflow.completed', 'workflow.failed', 'workflow.cancelling', 'workflow.stopped']
  eventTypes.push('workflow.contract_validated', 'workflow.contract_frozen', 'workflow.contract_reopened', 'workflow.delivery_checked', 'workflow.clarification_required', 'workflow.clarification_answered', 'workflow.blueprint_created', 'workflow.blueprint_frozen', 'validation.evidence', 'integration.evidence')
  eventTypes.push('repair.routed', 'repair.round_started', 'repair.target_gate_completed', 'repair.full_regression_completed', 'repair.completed', 'repair.candidate_created', 'repair.candidate_promoted', 'repair.candidate_discarded', 'repair.circuit_open', 'repair.escalated', 'step.validation_stage_started', 'step.validation_stage_completed', 'step.validation_short_circuited', 'step.validation_missing_declaration')
  eventTypes.push('architecture.contract_failed', 'architecture.repair_started', 'architecture.repair_rejected', 'architecture.target_gate_completed', 'architecture.repair_circuit_open')
  eventTypes.push('delivery.repair_started', 'delivery.repair_completed', 'delivery.archive_rebuild_started', 'delivery.archive_rebuild_completed')
  eventTypes.push('collaboration.started', 'collaboration.message', 'collaboration.completed', 'collaboration.unavailable')
  eventTypes.push('worker.lease_acquired', 'worker.lease_released', 'worker.lease_lost', 'worker.execution_error', 'worker.control_applied', 'worker.control_rejected')
  const codingEventTypes = [
    'coding_loop.action_started', 'coding_loop.action_completed', 'coding_loop.action_reconciled',
    'coding_loop.action_rejected', 'coding_loop.action_limit', 'coding_loop.protocol_error',
    'coding_loop.no_progress', 'coding_loop.final_pending_validation',
    'coding_loop.repair_candidate', 'coding_loop.repair_interrupted',
  ]
  codingEventTypes.forEach((eventType) => source.addEventListener(eventType, (message) => {
    parseEventMessage(message as MessageEvent)
  }))
  eventTypes.forEach((eventType) => source.addEventListener(eventType, (message) => {
    parseEventMessage(message as MessageEvent)
  }))
  if (!sseConnectionLogRuns.has(id)) {
    sseConnectionLogRuns.add(id)
    addLog('已连接实时 SSE 事件流', 'system', 'success')
  }
}

async function startRun() {
  if (isBusy.value || isStartingRun.value) return
  const submittedRequirement = requirement.value.trim()
  if (!submittedRequirement) {
    addLog('请先填写运行需求', 'workflow', 'failed')
    return
  }
  clearDemoTimers()
  resetRun(false)
  runStartedAt = Date.now()
  isStartingRun.value = true
  addLog('正在创建 Run 并提交到后台队列', 'workflow', 'running')
  try {
    const response = await fetch(`${API_BASE}/workflows/${activeWorkflowId.value}/runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ requirement: submittedRequirement, runtime: buildRuntimePayload() }) })
    const data = await readApiJson(response)
    if (!response.ok || !data?.runId) {
      apiOnline.value = true
      usingDemo.value = false
      const activeRun = data?.detail
      if (response.status === 409 && activeRun && typeof activeRun === 'object' && activeRun.runId) {
        const activeRequirement = String(activeRun.requirement ?? '').trim()
        if (activeRequirement && activeRequirement !== submittedRequirement) {
          // Do not attach a different request to the current canvas. That
          // would make the new brief appear to produce the old Agent logs.
          runId.value = null
          runStatus.value = 'IDLE'
          clearActiveRun()
          conflictingRun.value = {
            runId: String(activeRun.runId),
            status: String(activeRun.status ?? 'RUNNING'),
            requirement: activeRequirement,
          }
          addLog(t('differentActiveRun'), 'workflow', 'failed', undefined, `当前输入：${submittedRequirement} · 旧需求：${activeRequirement} · Run：${activeRun.runId}`)
          return
        }
        runId.value = String(activeRun.runId)
        runStatus.value = String(activeRun.status) === 'WAITING_APPROVAL' ? 'WAITING_APPROVAL' : String(activeRun.status) === 'WAITING_CLARIFICATION' ? 'WAITING_CLARIFICATION' : 'RUNNING'
        runStartedAt = Date.now()
        persistActiveRun()
        await loadGraph(activeWorkflowId.value)
        await refreshRunSnapshot(runId.value)
        addLog(t('existingRunAttached'), 'workflow', runStatus.value === 'WAITING_APPROVAL' ? 'waiting' : 'running', undefined, `Run：${runId.value} · 状态：${statusLabel(activeRun.status)}`)
        return
      }
      runStatus.value = 'FAILED'
      addLog(responseError(response, data), 'workflow', 'failed')
      return
    }
    apiOnline.value = true
    usingDemo.value = false
    runId.value = data.runId
    runStatus.value = 'RUNNING'
    persistActiveRun()
    addLog('运行已提交 · 等待 Worker 接管', 'workflow', 'running', undefined, `Run：${data.runId}`)
    await connectToRun(data.runId)
  } catch {
    apiOnline.value = false
    usingDemo.value = true
    runId.value = `demo_${Date.now()}`
    addLog('后端离线 · 已切换到本地演示 Provider', 'system', 'waiting')
    runStatus.value = 'RUNNING'
    if (activeWorkflowId.value === 'software-development') demoSequence()
    else {
      runStatus.value = 'FAILED'
      addLog('该工作流需要后端在线后才能运行', 'workflow', 'failed')
    }
  } finally {
    isStartingRun.value = false
  }
}

function demoSequence() {
  const currentRequirement = requirement.value.trim() || '未填写需求'
  const sequence: Array<{ id: string; delay: number; output: string }> = [
    { id: 'requirement', delay: 450, output: `需求分析已接收当前任务：${currentRequirement}` },
    { id: 'token_estimator', delay: 900, output: `已根据确认后的需求难度和模型能力生成后续节点 Token 预算：${currentRequirement}` },
    { id: 'architecture', delay: 1550, output: `架构设计已根据当前任务生成：${currentRequirement}` },
  ]
  sequence.forEach(({ id, delay, output }) => {
    demoTimers.push(window.setTimeout(() => { updateNode(id, 'RUNNING'); handleEvent({ type: 'step.started', runId: runId.value, payload: { stepId: id, status: 'RUNNING' } }) }, delay - 260))
    demoTimers.push(window.setTimeout(() => { updateNode(id, 'SUCCESS'); finishOutput(id, output, 410); handleEvent({ type: 'step.completed', runId: runId.value, payload: { stepId: id, status: 'SUCCESS', output, durationMs: 410 } }) }, delay))
  })
  demoTimers.push(window.setTimeout(() => handleEvent({ type: 'workflow.budget_planned', runId: runId.value, payload: { stepId: 'token_estimator', fallback: false, plan: { difficulty: 'low', model_strength: 'standard', confidence: 0.8 } } }), 920))
  demoTimers.push(window.setTimeout(() => { updateNode('architecture_approval', 'WAITING_APPROVAL'); handleEvent({ type: 'step.waiting_approval', runId: runId.value, payload: { stepId: 'architecture_approval', status: 'WAITING_APPROVAL' } }); handleEvent({ type: 'workflow.waiting_approval', runId: runId.value, payload: { stepId: 'architecture_approval', status: 'WAITING_APPROVAL' } }) }, 1950))
}

async function resolveApproval(decision: 'approve' | 'reject') {
  if (runStatus.value !== 'WAITING_APPROVAL' || isSubmittingApproval.value) return
  const approvalNodeId = nodes.value.find((node) => node.type === 'approval')?.id ?? selectedNodeId.value
  const durationBeforeDecision = liveDurationMs.value
  approvalError.value = ''
  isSubmittingApproval.value = true
  try {
    if (!usingDemo.value && runId.value) {
      const response = await fetch(`${API_BASE}/runs/${runId.value}/approval`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision }) })
      const data = await readApiJson(response)
      if (!response.ok || !data) throw new Error(responseError(response, data))
      apiOnline.value = true
      updateNode(approvalNodeId, decision === 'approve' ? 'SUCCESS' : 'FAILED')
      syncRunDuration(durationBeforeDecision)
      if (decision === 'approve') {
        runStatus.value = 'RUNNING'
        addLog('架构已确认 · 并行分支已解锁', 'approval', 'success')
      } else {
        runStatus.value = 'FAILED'
        clearActiveRun()
        addLog('架构确认已拒绝', 'approval', 'failed', approvalNodeId)
      }
      return
    }

    updateNode(approvalNodeId, decision === 'approve' ? 'SUCCESS' : 'FAILED')
    if (decision === 'reject') {
      syncRunDuration(durationBeforeDecision)
      handleEvent({ type: 'workflow.failed', runId: runId.value, payload: { status: 'FAILED', error: 'Architecture approval rejected' } })
      clearDemoTimers()
      return
    }
    syncRunDuration(durationBeforeDecision)
    runStatus.value = 'RUNNING'
    addLog('架构已确认 · 并行分支已解锁', 'approval', 'success')
    demoBranchSequence()
  } catch (error) {
    updateNode(approvalNodeId, 'WAITING_APPROVAL')
    approvalError.value = requestErrorMessage(error)
    addLog(`${t('approvalFailed')} · ${approvalError.value}`, 'approval', 'failed', approvalNodeId)
  } finally {
    isSubmittingApproval.value = false
  }
}

function clarificationFieldPrompt(field: string) {
  return (clarificationRequest.value?.field_prompts ?? []).find((item: any) => String(item?.field ?? '') === field) ?? { field, label: field, type: 'text' }
}

function clarificationCustomComplete() {
  if (clarificationOption.value !== 'custom') return true
  return (clarificationRequest.value?.unresolved_fields ?? []).every((field: string) => {
    if (field === 'primary_entity') return Boolean(clarificationEntityInput.value.trim())
    return Boolean(String(clarificationCustomAnswers.value[field] ?? '').trim())
  })
}

async function submitClarification() {
  if (runStatus.value !== 'WAITING_CLARIFICATION' || !runId.value || !clarificationRequest.value) return
  clarificationError.value = ''
  const answers: Record<string, any> = {
    option: clarificationOption.value,
    request_id: String(clarificationRequest.value.request_id ?? ''),
  }
  if (clarificationOption.value === 'custom') {
    for (const field of clarificationRequest.value.unresolved_fields ?? []) {
      if (field === 'primary_entity') {
        answers.primary_entity = clarificationEntityInput.value.trim()
      } else {
        answers[field] = clarificationCustomAnswers.value[field]
      }
    }
    if (!clarificationCustomComplete()) {
      clarificationError.value = '请填写全部待确认项后再继续。'
      return
    }
  }
  try {
    const response = await fetch(`${API_BASE}/runs/${runId.value}/clarification`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    })
    const data = await readApiJson(response)
    if (!response.ok || !data) throw new Error(responseError(response, data))
    runStatus.value = 'RUNNING'
    addLog('已提交需求确认，正在从 Checkpoint 恢复', 'workflow', 'success')
  } catch (error) {
    clarificationError.value = requestErrorMessage(error)
    addLog(`需求确认提交失败 · ${clarificationError.value}`, 'workflow', 'failed')
  }
}

function demoBranchSequence() {
  const currentRequirement = requirement.value.trim() || '未填写需求'
  const branchOutputs: Record<string, string> = {
    backend: `后端方案已针对当前任务“${currentRequirement}”生成：补充 API、数据边界、错误处理和可观测性。`,
    frontend: `前端方案已针对当前任务“${currentRequirement}”生成：补充页面结构、交互状态、响应式布局和可访问性。`,
    tester: `测试方案已针对当前任务“${currentRequirement}”生成：覆盖主流程、异常路径、边界输入和回归验证。`,
    reviewer: `最终审查已针对当前任务“${currentRequirement}”完成：检查交付范围、验收标准、风险和后续产物。`,
  }
  ;['backend', 'frontend'].forEach((id, index) => {
    demoTimers.push(window.setTimeout(() => handleEvent({ type: 'step.started', runId: runId.value, payload: { stepId: id, status: 'RUNNING' } }), index * 80))
    demoTimers.push(window.setTimeout(() => handleEvent({ type: 'step.completed', runId: runId.value, payload: { stepId: id, status: 'SUCCESS', output: branchOutputs[id], durationMs: 560 + index * 44 } }), 800 + index * 280))
  })
  demoTimers.push(window.setTimeout(() => handleEvent({ type: 'step.started', runId: runId.value, payload: { stepId: 'tester', status: 'RUNNING' } }), 1450))
  demoTimers.push(window.setTimeout(() => handleEvent({ type: 'step.completed', runId: runId.value, payload: { stepId: 'tester', status: 'SUCCESS', output: branchOutputs.tester, durationMs: 610 } }), 2050))
  demoTimers.push(window.setTimeout(() => handleEvent({ type: 'step.started', runId: runId.value, payload: { stepId: 'reviewer', status: 'RUNNING' } }), 2200))
  demoTimers.push(window.setTimeout(() => { handleEvent({ type: 'step.completed', runId: runId.value, payload: { stepId: 'reviewer', status: 'SUCCESS', output: branchOutputs.reviewer, durationMs: 520 } }); handleEvent({ type: 'workflow.completed', runId: runId.value, payload: { status: 'SUCCESS', finalReport: branchOutputs.reviewer } }) }, 2800))
}

function closeEventSource() {
  eventSource?.close()
  eventSource = null
}

function clearDemoTimers() {
  demoTimers.forEach((timer) => window.clearTimeout(timer))
  demoTimers = []
}

function resetRun(keepLog = true) {
  closeEventSource()
  clearDemoTimers()
  nodes.value = cloneValue(graphTemplate.value)
  edges.value = cloneValue(edgeTemplate.value)
  outputs.value = {}
  errorSummaries.value = {}
  errorSummaryNodeId.value = null
  artifacts.value = []
  seenSseEventIds.clear()
  sseConnectionLogRuns.clear()
  runId.value = null
  runStatus.value = 'IDLE'
  conflictingRun.value = null
  runStartedAt = 0
  resetRunStats()
  clearActiveRun()
  expandedLogNodeId.value = null
  if (!keepLog) {
    logs.value = []
    logsByStep.value = {}
  }
}

async function stopRun() {
  if (!runId.value) return
  clearDemoTimers()
  if (!usingDemo.value) {
    try {
      const response = await fetch(`${API_BASE}/runs/${runId.value}/stop`, { method: 'POST' })
      const data = await readApiJson(response)
      if (!response.ok || !data) throw new Error(responseError(response, data))
    } catch (error) {
      addLog(`停止运行失败 · ${requestErrorMessage(error)}`, 'workflow', 'failed')
      await refreshRunSnapshot(runId.value)
      return
    }
  }
  handleEvent({ type: 'workflow.stopped', runId: runId.value, payload: { status: 'STOPPED' } })
  closeEventSource()
}

async function retryRun() {
  if (!runId.value || isBusy.value || usingDemo.value || isRetryingRun.value) return
  const retryingId = runId.value
  isRetryingRun.value = true
  try {
    const response = await fetch(`${API_BASE}/runs/${retryingId}/retry`, { method: 'POST' })
    const data = await readApiJson(response)
    if (!response.ok || !data) throw new Error(responseError(response, data))
    apiOnline.value = true
    const nextStatus = String(data.status ?? 'RUNNING')
    runStatus.value = nextStatus === 'WAITING_APPROVAL'
      ? 'WAITING_APPROVAL'
      : nextStatus === 'WAITING_CLARIFICATION'
        ? 'WAITING_CLARIFICATION'
        : 'RUNNING'
    persistActiveRun()
    addLog(
      runStatus.value === 'WAITING_APPROVAL' ? '已恢复架构审批，请确认后继续' : runStatus.value === 'WAITING_CLARIFICATION' ? '已恢复需求确认，请确认后继续' : `${t('retryFailedRun')} · ${t('recoveryCount')} ${Number(data.recoveryCount ?? 0)}`,
      'workflow',
      'waiting',
      undefined,
      runStatus.value === 'RUNNING' ? '仅重新执行失败节点及其下游节点，已完成的前置节点不会重复调用' : '已恢复到上次暂停位置，不会重复调用已完成的 Agent',
    )
    await refreshRunSnapshot(retryingId)
    await connectToRun(retryingId)
  } catch (error) {
    addLog(`${t('testFailed')} · ${requestErrorMessage(error)}`, 'workflow', 'failed')
  } finally {
    isRetryingRun.value = false
  }
}

async function stopConflictingRun() {
  const target = conflictingRun.value
  if (!target) return
  try {
    const response = await fetch(`${API_BASE}/runs/${encodeURIComponent(target.runId)}/stop`, { method: 'POST' })
    const data = await readApiJson(response)
    if (!response.ok || !data) throw new Error(responseError(response, data))
    conflictingRun.value = null
    addLog('旧需求运行已停止 · 可以运行当前新需求', 'workflow', 'success', undefined, `Run：${target.runId}`)
  } catch (error) {
    addLog(`停止旧运行失败 · ${requestErrorMessage(error)}`, 'workflow', 'failed', undefined, `Run：${target.runId}`)
  }
}

function markActiveWorkflow() {
  workflowList.value = workflowList.value.map((workflow) => ({ ...workflow, active: workflow.id === activeWorkflowId.value }))
  persistSelectedWorkflow()
}

async function loadWorkflows() {
  try {
    const response = await fetch(`${API_BASE}/workflows`)
    const data = await readApiJson(response)
    if (!response.ok || !Array.isArray(data)) throw new Error(responseError(response, data))
    const workflows = data.filter((item) => item && typeof item.id === 'string').map((item) => ({ id: item.id, name: String(item.name ?? item.id), stepCount: Number(item.stepCount ?? 0), concurrency: Number(item.concurrency ?? 1) }))
    if (workflows.length) {
      workflowList.value = workflows
      if (!workflowList.value.some((workflow) => workflow.id === activeWorkflowId.value)) activeWorkflowId.value = workflowList.value[0].id
      markActiveWorkflow()
    }
    apiOnline.value = true
  } catch {
    apiOnline.value = false
    markActiveWorkflow()
  }
}

async function selectWorkflow(workflowId: string) {
  if (isBusy.value || workflowId === activeWorkflowId.value) return
  activeWorkflowId.value = workflowId
  persistSelectedWorkflow()
  markActiveWorkflow()
  resetRun(false)
  await loadGraph(workflowId)
}

async function loadGraph(workflowId = activeWorkflowId.value) {
  try {
    const response = await fetch(`${API_BASE}/workflows/${workflowId}/graph`)
    if (!response.ok) throw new Error('offline')
    const graph = await readApiJson(response)
    if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) throw new Error('Invalid graph response')
    apiOnline.value = true
    const defaults = graph.runtimeDefaults ?? {}
    runtimeDefaults.maxTokens = Number(defaults.max_tokens ?? defaults.maxTokens ?? 6000)
    runtimeDefaults.retry = Number(defaults.retry ?? 2)
    runtimeDefaults.thinking = String(defaults.thinking ?? 'auto') as ThinkingMode
    skillMode.value = String(defaults.skill_mode ?? defaults.skillMode ?? 'auto') as 'auto' | 'on' | 'off'
    for (const stepId of Object.keys(runtimeByStep)) delete runtimeByStep[stepId]
    const mappedNodes = graph.nodes.map((node: any) => {
      const runtime = node.data?.runtime ?? {}
      runtimeByStep[node.id] = { maxTokens: Number(runtime.maxTokens ?? runtimeDefaults.maxTokens), retry: Number(runtime.retry ?? runtimeDefaults.retry), thinking: String(runtime.thinking ?? runtimeDefaults.thinking) as ThinkingMode }
      return { ...node, data: { ...node.data, role: node.type === 'approval' ? 'Human in the loop' : node.data.agentId, status: 'PENDING', description: node.data.description ?? '工作流步骤' } }
    })
    graphTemplate.value = structuredClone(mappedNodes)
    edgeTemplate.value = structuredClone(graph.edges)
    nodes.value = structuredClone(mappedNodes)
    edges.value = structuredClone(graph.edges)
    selectedNodeId.value = mappedNodes[0]?.id ?? ''
  } catch {
    apiOnline.value = false
  }
}

function snapshotLogTime(value: unknown) {
  const parsed = Date.parse(String(value ?? ''))
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleTimeString('en-GB', { hour12: false }) : now()
}

function hydrateStepLogsFromSnapshot(data: Record<string, any>) {
  const inputs = data.inputs?.runtime ?? {}
  for (const step of Array.isArray(data.steps) ? data.steps : []) {
    const stepId = String(step.step_id ?? '')
    if (!stepId || logsByStep.value[stepId]?.length) continue
    const runtime = inputs.steps?.[stepId] ?? inputs.defaults ?? {}
    const detailParts = [`已从历史 Run 恢复`, `上限：${runtime.max_tokens ?? runtime.maxTokens ?? '-'} Token`]
    const providerDetail = providerLogDetail(step.provider)
    if (providerDetail) detailParts.push(providerDetail)
    if (step.retry_count != null) detailParts.push(`重试：${step.retry_count} 次`)
    if (step.input_tokens != null || step.output_tokens != null) detailParts.push(`Token：输入 ${step.input_tokens ?? 0} / 输出 ${step.output_tokens ?? 0}`)
    if (step.duration_ms != null) detailParts.push(`耗时：${step.duration_ms}ms`)
    const status = String(step.status ?? 'PENDING')
    const items: LogItem[] = []
    if (step.started_at) items.push({ time: snapshotLogTime(step.started_at), type: 'step', message: `${stepId} 已开始`, tone: 'running', stepId, detail: detailParts.join(' · ') })
    if (status === 'SUCCESS') items.push({ time: snapshotLogTime(step.finished_at), type: 'step', message: `${stepId} 已完成`, tone: 'success', stepId, detail: detailParts.join(' · ') })
    if (status === 'FAILED') {
      const facts = Array.isArray(data.failureFacts) ? data.failureFacts : Array.isArray(data.failure_facts) ? data.failure_facts : []
      const matching = facts.filter((fact: Record<string, any>) => fact?.stage === stepId || fact?.stage === `${stepId}_contract` || fact?.evidence?.step_id === stepId)
      const preferred = [...matching].reverse().find((fact: Record<string, any>) => String(fact.code ?? '').startsWith('ARCH_')) ?? matching[matching.length - 1]
      errorSummaries.value[stepId] = summarizeAgentError({ error: step.error_message, errorType: step.error_type, retryCount: step.retry_count, failureFact: preferred })
      items.push({ time: snapshotLogTime(step.finished_at), type: 'step', message: `${stepId} 失败 · ${localizeProviderMessage(compactLogValue(step.error_message), '历史错误')}`, tone: 'failed', stepId, detail: `已从历史 Run 恢复 · 类型：历史错误 · ${detailParts.slice(1).join(' · ')}` })
    }
    if (status === 'WAITING_APPROVAL') items.push({ time: snapshotLogTime(step.started_at), type: 'approval', message: '架构已完成，等待人工确认', tone: 'waiting', stepId, detail: '已从历史 Run 恢复 · 工作流将在这里暂停，直到审批完成' })
    if (status === 'RUNNING' && !items.length) items.push({ time: snapshotLogTime(step.started_at), type: 'step', message: `${stepId} 仍在运行`, tone: 'running', stepId, detail: detailParts.join(' · ') })
    logsByStep.value[stepId] = items.reverse()
  }
}

function applyRunSnapshot(data: Record<string, any>) {
  const status = String(data.status ?? 'PENDING') as Status | 'STOPPED'
  runStatus.value = status === 'PENDING' ? 'RUNNING' : status as typeof runStatus.value
  if (status === 'WAITING_CLARIFICATION') setClarificationRequest(data.clarificationRequest ?? data.clarification ?? null)
  const startedAt = Date.parse(String(data.started_at ?? ''))
  if (Number.isFinite(startedAt)) runStartedAt = startedAt
  applyRunStats(data)
  hydrateStepLogsFromSnapshot(data)
  hydrateArtifacts(data)
  let waitingStepId = ''
  for (const step of Array.isArray(data.steps) ? data.steps : []) {
    const stepId = String(step.step_id ?? '')
    if (!stepId) continue
    const stepStatus = String(step.status ?? 'PENDING') as Status
    updateNode(stepId, stepStatus)
    if (stepStatus === 'WAITING_APPROVAL' || stepStatus === 'WAITING_CLARIFICATION') waitingStepId = stepId
    if (stepStatus === 'SUCCESS' && step.output) finishOutput(stepId, String(step.output), Number(step.duration_ms ?? 0), Number(step.retry_count ?? 0))
    if (stepStatus === 'FAILED' && step.error_message) outputs.value[stepId] = { id: stepId, name: stepId, role: '执行错误', status: 'FAILED', output: localizeProviderMessage(step.error_message, '历史错误') }
  }
  const recentEvents = Array.isArray(data.recentEvents) ? data.recentEvents : []
  for (const event of recentEvents) {
    const eventId = Number(event?.id ?? 0)
    const eventRunId = String(event?.runId ?? runId.value ?? '')
    const dedupeKey = `${eventRunId}:${eventId}`
    if (!eventId || seenSseEventIds.has(dedupeKey)) continue
    seenSseEventIds.add(dedupeKey)
    eventCursorByRun.set(eventRunId, Math.max(eventCursorByRun.get(eventRunId) ?? 0, eventId))
    handleEvent(event)
  }
  const snapshotCursor = Number(data.eventCursor ?? 0)
  if (runId.value && snapshotCursor > 0) eventCursorByRun.set(runId.value, Math.max(eventCursorByRun.get(runId.value) ?? 0, snapshotCursor))
  if (waitingStepId) selectedNodeId.value = waitingStepId
}

async function loadRunHistory() {
  runHistoryLoading.value = true
  runHistoryError.value = ''
  try {
    const response = await fetch(`${API_BASE}/runs?limit=100`)
    const data = await readApiJson(response)
    if (!response.ok || !Array.isArray(data)) throw new Error(responseError(response, data))
    runHistory.value = data as RunHistoryItem[]
  } catch (error) {
    runHistoryError.value = requestErrorMessage(error)
  } finally {
    runHistoryLoading.value = false
  }
}

async function openRunHistory() {
  showRunHistory.value = true
  await loadRunHistory()
}

function formatHistoryTime(value: unknown) {
  const parsed = Date.parse(String(value ?? ''))
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString('zh-CN', { hour12: false }) : '-'
}

async function openHistoricalRun(item: RunHistoryItem) {
  closeEventSource()
  clearDemoTimers()
  activeWorkflowId.value = item.workflow_id
  markActiveWorkflow()
  nodes.value = cloneValue(graphTemplate.value)
  edges.value = cloneValue(edgeTemplate.value)
  outputs.value = {}
  logsByStep.value = {}
  logs.value = []
  errorSummaries.value = {}
  artifacts.value = []
  seenSseEventIds.clear()
  eventCursorByRun.delete(item.id)
  runId.value = item.id
  runStartedAt = Date.parse(String(item.started_at ?? '')) || Date.now()
  await loadGraph(item.workflow_id)
  await refreshRunSnapshot(item.id)
  showRunHistory.value = false
}

async function refreshRunSnapshot(id: string) {
  if (refreshInFlight) return
  refreshInFlight = true
  try {
    const response = await fetch(`${API_BASE}/runs/${id}`)
    const data = await readApiJson(response)
    if (!response.ok || !data) throw new Error(responseError(response, data))
    apiOnline.value = true
    applyRunSnapshot(data)
    if (['SUCCESS', 'FAILED', 'STOPPED'].includes(String(data.status))) {
      clearActiveRun()
      closeEventSource()
      return
    }
    persistActiveRun()
    await connectToRun(id)
  } catch (error) {
    const nowMs = Date.now()
    if (nowMs - lastRefreshErrorAt > 3000) {
      lastRefreshErrorAt = nowMs
    addLog(`无法刷新当前 Run · ${requestErrorMessage(error)}`, 'system', 'waiting')
    }
  } finally {
    refreshInFlight = false
  }
}

async function refreshApplicationState(force = false) {
  if (document.visibilityState !== 'visible') return
  const elapsed = hiddenAt ? Date.now() - hiddenAt : 0
  if (!force && (!hiddenAt || elapsed < 2000)) return
  hiddenAt = 0
  await loadWorkflows()
  await loadGraph(activeWorkflowId.value)
  await loadProviderConfig()
  await loadProviderCapabilities()
  const saved = readActiveRun()
  if (saved && !runId.value) {
    activeWorkflowId.value = saved.workflowId
    markActiveWorkflow()
    await loadGraph(saved.workflowId)
    runId.value = saved.runId
    runStartedAt = saved.startedAt ?? Date.now()
  }
  if (runId.value && !runId.value.startsWith('demo_')) await refreshRunSnapshot(runId.value)
}

function handleVisibilityChange() {
  if (document.visibilityState === 'hidden') hiddenAt = Date.now()
  else void refreshApplicationState()
}

function handlePageShow(event: PageTransitionEvent) {
  void refreshApplicationState(Boolean(event.persisted))
}

function handleWindowFocus() {
  void refreshApplicationState()
}

function applyDraftProviderCapabilities() {
  if (apiConfig.provider === 'chatgpt_local') {
    providerCapabilities.supportsThinking = false
    providerCapabilities.supportedLevels = ['off']
    providerCapabilities.defaultLevel = 'off'
    providerCapabilities.modelFamily = 'codex'
    providerCapabilities.configurationWarning = ''
    return
  }
  const model = apiConfig.modelName.trim().toLowerCase()
  const isGlm = model.startsWith('glm-') || model.startsWith('chatglm-')
  const supportsThinking = isGlm || /^qwen3(?:[.-]|$)/.test(model) || model.startsWith('deepseek')
  providerCapabilities.supportsThinking = supportsThinking
  providerCapabilities.supportedLevels = supportsThinking ? ['off', 'low', 'high', 'max'] : ['off']
  providerCapabilities.defaultLevel = isGlm || model.startsWith('qwen3.8') ? 'max' : supportsThinking ? 'high' : 'off'
  providerCapabilities.modelFamily = isGlm ? 'glm' : model.startsWith('deepseek') ? 'deepseek' : model.startsWith('qwen3') ? 'qwen3' : 'unknown'
  providerCapabilities.configurationWarning = isGlm && (apiConfig.provider === 'qwen' || apiConfig.baseUrl.toLowerCase().includes('dashscope.aliyuncs.com'))
    ? t('providerConfigurationWarning')
    : ''
}

function onProviderChange(event: Event) {
  const provider = (event.target as HTMLSelectElement).value as ProviderKey
  const preset = providerPresets[provider] ?? providerPresets.qwen
  apiConfig.provider = provider
  apiConfig.baseUrl = preset.baseUrl
  apiConfig.modelName = preset.modelName
  apiConfig.apiKey = ''
  apiConfig.apiKeyConfigured = false
  apiConfig.usingMock = true
  connectionTest.value = { kind: 'idle', message: '' }
  applyDraftProviderCapabilities()
}

async function loadProviderConfig() {
  try {
    const response = await fetch(`${API_BASE}/provider/config`)
    if (!response.ok) throw new Error('offline')
    const data = await readApiJson(response)
    if (!data) throw new Error('empty response')
    const provider = String(data.provider ?? 'openai_compatible').trim().toLowerCase()
    apiConfig.provider = (['qwen', 'deepseek', 'glm', 'openai_compatible', 'chatgpt_local'].includes(provider) ? provider : 'openai_compatible') as ProviderKey
    apiConfig.baseUrl = data.baseUrl
    apiConfig.modelName = apiConfig.provider === 'chatgpt_local' ? normalizeLocalCodexModel(data.modelName) : data.modelName
    apiConfig.apiKeyConfigured = Boolean(data.apiKeyConfigured)
    apiConfig.usingMock = data.usingMock
    applyDraftProviderCapabilities()
  } catch {
    // Backend may be offline while the canvas is still usable in Demo Mode.
  }
}

async function loadProviderCapabilities() {
  try {
    const response = await fetch(`${API_BASE}/provider/capabilities`)
    const data = await readApiJson(response)
    if (!response.ok || !data) throw new Error(responseError(response, data))
    providerCapabilities.supportsThinking = Boolean(data.supportsThinking)
    providerCapabilities.supportedLevels = Array.isArray(data.supportedLevels) ? data.supportedLevels.filter((level: unknown): level is ThinkingMode => ['auto', 'off', 'low', 'high', 'max'].includes(String(level))) : ['off']
    if (!providerCapabilities.supportedLevels.includes('off')) providerCapabilities.supportedLevels.unshift('off')
    providerCapabilities.defaultLevel = (providerCapabilities.supportedLevels.includes(data.defaultLevel) ? data.defaultLevel : 'off') as ThinkingMode
    providerCapabilities.model = String(data.model ?? '')
    providerCapabilities.modelFamily = String(data.modelFamily ?? '')
    providerCapabilities.configurationWarning = String(data.configurationWarning ?? '')
  } catch {
    providerCapabilities.supportsThinking = false
    providerCapabilities.supportedLevels = ['off']
    providerCapabilities.defaultLevel = 'off'
    providerCapabilities.modelFamily = ''
    providerCapabilities.configurationWarning = ''
  }
}

function syncApiKeyFromInput() {
  const inputValue = apiKeyInput.value?.value ?? ''
  if (inputValue !== apiConfig.apiKey) apiConfig.apiKey = inputValue
  return inputValue.trim()
}

async function testConnection() {
  isTestingConnection.value = true
  connectionTest.value = { kind: 'idle', message: '' }
  try {
    const apiKey = syncApiKeyFromInput()
    const response = await fetch(`${API_BASE}/provider/test`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: apiConfig.provider, base_url: apiConfig.baseUrl, api_key: apiKey || undefined, model_name: apiConfig.modelName }) })
    const data = await readApiJson(response)
    if (!response.ok) throw new Error(responseError(response, data))
    if (!data) throw new Error(responseError(response, null))
    connectionTest.value = { kind: 'success', message: t('connectionSuccess'), latency: data.latencyMs }
  } catch (error) {
    connectionTest.value = { kind: 'error', message: requestErrorMessage(error) }
  } finally {
    isTestingConnection.value = false
  }
}

async function saveApiConfig() {
  isSavingConfig.value = true
  try {
    const apiKey = syncApiKeyFromInput()
    const response = await fetch(`${API_BASE}/provider/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: apiConfig.provider, base_url: apiConfig.baseUrl, api_key: apiKey || undefined, model_name: apiConfig.modelName }) })
    const data = await readApiJson(response)
    if (!response.ok) throw new Error(responseError(response, data))
    if (!data) throw new Error(responseError(response, null))
    apiConfig.usingMock = data.usingMock
    apiConfig.apiKeyConfigured = !data.usingMock
    apiOnline.value = !data.usingMock
    connectionTest.value = { kind: data.usingMock ? 'error' : 'success', message: data.usingMock ? t('mockProviderNote') : t('connectionSuccess') }
    addLog(data.usingMock ? 'Provider 已保存 · 仍处于 Mock 模式' : 'Provider 已保存 · 所有 Agent 已接入云端 LLM', 'system', data.usingMock ? 'waiting' : 'success')
    if (!data.usingMock) apiConfig.apiKey = ''
    await loadProviderCapabilities()
  } catch (error) {
    connectionTest.value = { kind: 'error', message: requestErrorMessage(error) }
  } finally {
    isSavingConfig.value = false
  }
}

async function initializeApp() {
  const saved = readActiveRun()
  const savedWorkflowId = saved?.workflowId ?? readSelectedWorkflow()
  if (savedWorkflowId) activeWorkflowId.value = savedWorkflowId
  if (saved) {
    runId.value = saved.runId
    runStartedAt = saved.startedAt ?? Date.now()
  }
  await loadWorkflows()
  markActiveWorkflow()
  await loadGraph(activeWorkflowId.value)
  persistSelectedWorkflow()
  await loadProviderConfig()
  await loadProviderCapabilities()
  if (saved) await refreshRunSnapshot(saved.runId)
}

onMounted(() => {
  durationTimer = window.setInterval(() => { clockNow.value = Date.now() }, 1000)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  window.addEventListener('pageshow', handlePageShow)
  window.addEventListener('focus', handleWindowFocus)
  void initializeApp()
})
onBeforeUnmount(() => {
  if (durationTimer !== null) window.clearInterval(durationTimer)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  window.removeEventListener('pageshow', handlePageShow)
  window.removeEventListener('focus', handleWindowFocus)
  closeEventSource()
  clearDemoTimers()
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-lockup">
        <div class="brand-mark"><span></span><span></span><span></span></div>
        <div><div class="brand-name">ORBIT</div><div class="brand-sub">agent team / 01</div></div>
      </div>

      <div class="workspace-switcher">
        <div class="workspace-avatar">AT</div>
        <div class="workspace-copy"><span>{{ t('workspace') }}</span><strong>Atlas kitchen</strong></div>
        <ChevronRight :size="15" />
      </div>

      <div class="side-label">{{ t('workspace') }}</div>
      <nav class="main-nav">
        <a class="nav-item active" href="#"><Activity :size="16" /> {{ t('runs') }} <span class="nav-count">01</span></a>
        <a class="nav-item" href="#"><GitBranch :size="16" /> {{ t('workflows') }}</a>
        <a class="nav-item" href="#"><Bot :size="16" /> {{ t('agentLibrary') }}</a>
      </nav>

      <div class="side-label workflow-label">{{ t('workflows') }} <button aria-label="添加工作流">+</button></div>
      <div class="workflow-list">
        <button v-for="workflow in workflowList" :key="workflow.id" class="workflow-item" :class="{ selected: workflow.active }" :disabled="isBusy" @click="selectWorkflow(workflow.id)">
          <span class="workflow-icon"><GitBranch :size="14" /></span>
          <span class="workflow-copy"><strong>{{ workflowName(workflow.id) }}</strong><small>{{ workflowDetail(workflow.id) }}</small></span>
          <span v-if="workflow.active" class="active-dot"></span>
        </button>
      </div>

      <div class="sidebar-spacer"></div>
      <div class="side-footer-card">
        <div class="footer-card-icon"><Sparkles :size="15" /></div>
        <div><strong>{{ t('mockProvider') }}</strong><span>{{ t('localDemoReady') }}</span></div>
        <span class="online-pulse"></span>
      </div>
      <div class="user-row"><div class="user-avatar">Y</div><div><strong>you@atlas.team</strong><span>{{ t('builderAccess') }}</span></div><CircleHelp :size="15" /></div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="breadcrumbs"><span>{{ t('runs') }}</span><ChevronRight :size="13" /><strong>{{ workflowName(activeWorkflowId) }}</strong><span class="run-chip">{{ runId ? runId : 'draft' }}</span></div>
        <div class="top-actions">
          <div class="connection-status"><span :class="['connection-dot', { live: apiOnline }]" />{{ apiOnline ? t('apiConnected') : t('demoMode') }}</div>
          <button class="settings-button" aria-label="运行记录" @click="openRunHistory"><Clock3 :size="14" /> <span>运行记录</span></button>
          <button class="settings-button" :aria-label="t('apiSettings')" @click="showApiConfig = true"><Settings2 :size="14" /> <span>{{ t('apiSettings') }}</span></button>
          <div class="stats-menu">
            <button type="button" class="stats-button" :class="{ active: showRunStats }" :aria-expanded="showRunStats" :aria-label="t('runStats')" @click="showRunStats = !showRunStats">
              <Activity :size="14" /> <span>{{ t('runStats') }}</span> <strong>{{ formatTokenCount(runStats.totalTokens) }}</strong>
            </button>
            <Transition name="stats-popover">
              <section v-if="showRunStats" class="stats-popover" role="dialog" :aria-label="t('runStats')" @click.stop>
                <div class="stats-popover-head">
                  <div><span class="stats-kicker">{{ t('runStats') }}</span><strong>{{ liveDuration }}</strong></div>
                  <button type="button" class="stats-close" :aria-label="t('closeStats')" @click="showRunStats = false"><X :size="13" /></button>
                </div>
                <div class="stats-summary">
                  <div><span>{{ t('activeDuration') }}</span><strong>{{ liveDuration }}</strong><small>{{ t('approvalWait') }} · {{ formatDuration(runStats.approvalDurationMs) }}</small></div>
                  <div><span>{{ t('totalTokens') }}</span><strong>{{ formatTokenCount(runStats.totalTokens) }}</strong><small>{{ t('inputTokens') }} {{ formatTokenCount(runStats.inputTokens) }} · {{ t('outputTokens') }} {{ formatTokenCount(runStats.outputTokens) }}</small></div>
                </div>
                <div class="stats-section-title">{{ t('repairMetrics') }}</div>
                <div class="stats-quality-grid">
                  <div><span>{{ t('firstPass') }}</span><strong>{{ runStats.repair.firstPass ? '是' : '否' }}</strong></div>
                  <div><span>{{ t('autoRepairRate') }}</span><strong>{{ formatPercent(runStats.repair.automaticRepairRate) }}</strong></div>
                  <div><span>{{ t('repairRounds') }}</span><strong>{{ runStats.repair.repairRounds }}</strong></div>
                  <div><span>{{ t('averageRepairRounds') }}</span><strong>{{ runStats.repair.averageRepairRounds.toFixed(1) }}</strong></div>
                  <div><span>熔断次数</span><strong>{{ runStats.repair.circuitBreaks }}</strong></div>
                </div>
                <template v-if="runStats.repairTrace.length">
                  <div class="stats-section-title">{{ t('repairTrace') }}</div>
                  <div v-for="trace in runStats.repairTrace" :key="trace.key" class="stats-repair-row">
                    <div class="stats-repair-index">{{ trace.attempt || '·' }}</div>
                    <div><strong>{{ trace.location }} → {{ trace.owners.join('、') || '平台' }}</strong><span>{{ trace.status }}</span><small v-if="trace.result">{{ trace.result }}</small></div>
                  </div>
                </template>
                <template v-if="runStats.stages.length">
                  <div class="stats-section-title">{{ t('stageMetrics') }}</div>
                  <div v-for="stage in runStats.stages" :key="stage.stage" class="stats-stage-row">
                    <strong>{{ stageLabel(stage.stage) }}</strong><span>{{ formatDuration(stage.durationMs) }}</span><small>{{ formatTokenCount(stage.totalTokens) }} Token</small>
                  </div>
                </template>
                <div class="stats-section-title">{{ t('perAgentUsage') }}</div>
                <div v-if="!runStats.byAgent.length" class="stats-empty">{{ t('noTokenUsage') }}</div>
                <div v-for="usage in runStats.byAgent" :key="usage.stepId" class="stats-agent-row">
                  <div class="stats-agent-copy"><strong>{{ localizedNodeLabel(usage.stepId, usage.stepId) }}</strong><small>{{ usage.agentId }}</small></div>
                  <div class="stats-agent-values"><strong>{{ formatTokenCount(usage.totalTokens) }}</strong><small>{{ t('inputTokens') }} {{ formatTokenCount(usage.inputTokens) }} · {{ t('outputTokens') }} {{ formatTokenCount(usage.outputTokens) }}</small></div>
                </div>
              </section>
            </Transition>
          </div>
          <div class="top-activity-indicator"><Activity :size="15" /><span>{{ t('runActivity') }}</span></div>
          <button class="avatar-button">Y</button>
        </div>
      </header>

      <section class="content-grid">
        <div class="canvas-column">
          <div class="page-heading">
            <div><div class="eyebrow"><span class="eyebrow-line"></span>{{ t('runCanvas') }}</div><h1>{{ workflowName(activeWorkflowId) }} <span class="heading-status" :class="runStatus.toLowerCase()">{{ runStatus === 'IDLE' ? t('ready') : runStatus === 'WAITING_APPROVAL' ? t('waitingApproval') : runStatus === 'WAITING_CLARIFICATION' ? '等待需求确认' : runStatus === 'RUNNING' ? t('running') : runStatus === 'SUCCESS' ? t('success') : runStatus === 'FAILED' ? t('failed') : t('stopped') }}</span></h1><p>{{ t('canvasDescription') }}</p></div>
            <div class="heading-metrics"><div><strong>{{ completedCount }}/{{ nodes.length }}</strong><span>{{ t('stepsComplete') }}</span></div><div><strong>{{ liveDuration }}</strong><span>{{ t('runDuration') }}</span></div></div>
          </div>

          <div class="canvas-toolbar">
            <div class="toolbar-left"><span class="canvas-title"><Layers3 :size="15" /> {{ t('executionGraph') }}</span><span class="level-pill">{{ workflowGraphMeta }}</span></div>
            <div class="toolbar-actions"><button class="tool-button"><Sparkles :size="14" /> {{ t('autoLayout') }}</button><button class="tool-button"><ArrowDownToLine :size="14" /> {{ t('export') }}</button></div>
          </div>

          <div class="canvas-wrap">
            <VueFlow v-model:nodes="nodes" v-model:edges="edges" fit-view-on-init :min-zoom="0.35" :max-zoom="1.35" @node-click="({ node }) => selectNode(node)">
              <Background pattern-color="#dfe9ee" :gap="22" :size="1" />
              <Controls position="bottom-left" />
              <MiniMap position="bottom-right" pannable zoomable />

              <template #node-agent="{ data, id }">
                <div class="flow-node" :class="nodeStatusClass(data.status)">
                  <Handle type="target" :position="Position.Left" />
                  <div class="node-accent"></div><div class="node-kicker"><Bot :size="12" /> {{ localizedRole(data.role) }}</div><div class="node-label">{{ localizedNodeLabel(id, data.label) }}</div>
                  <div class="node-bottom"><span class="node-status-dot"></span><span>{{ statusLabel(data.status) }}</span><span v-if="data.status === 'RUNNING'" class="node-spinner"></span><button class="node-log-button" :class="{ active: expandedLogNodeId === id }" :aria-label="t('openAgentLogs')" @click.stop="openStepLogs(id)"><Activity :size="10" /><span>{{ t('agentLogs') }}</span></button><button type="button" class="node-error-button" :class="{ active: errorSummaryNodeId === id }" :disabled="!hasAgentError(id)" :aria-label="t('mainError')" @click.stop="toggleErrorSummary(id)">主要报错</button></div>
                  <div v-if="expandedLogNodeId === id" class="inline-agent-log" @pointerdown.stop @mousedown.stop @touchstart.stop @click.stop><div class="inline-agent-log-head"><span><Activity :size="10" /> {{ t('agentLogs') }}</span><div><strong>{{ stepLogs(id).length }} {{ t('events') }}</strong><button type="button" class="inline-log-copy" :disabled="!stepLogs(id).length" @click.stop="copyStepLogs(id)">复制</button></div></div><div v-if="!stepLogs(id).length" class="inline-log-empty">{{ t('noAgentLogs') }}</div><div v-for="(log, logIndex) in stepLogs(id)" :key="`${log.time}-${logIndex}`" class="inline-log-row"><span>{{ log.time }}</span><i class="activity-marker" :class="log.tone"></i><p>{{ log.message }}<small v-if="log.detail">{{ log.detail }}</small></p></div></div>
                  <div v-if="errorSummaryNodeId === id && errorSummaryFor(id)" class="inline-error-summary" @pointerdown.stop @mousedown.stop @touchstart.stop @click.stop><div class="inline-error-summary-head"><strong>{{ t('mainError') }}</strong><button type="button" @click.stop="toggleErrorSummary(id)">{{ t('close') }}</button></div><strong class="inline-error-title">{{ errorSummaryFor(id)?.title }}</strong><p><span>{{ t('errorReason') }}</span>{{ errorSummaryFor(id)?.reason }}</p><p><span>{{ t('errorSuggestion') }}</span>{{ errorSummaryFor(id)?.suggestion }}</p></div>
                  <Handle type="source" :position="Position.Right" />
                </div>
              </template>
              <template #node-approval="{ data, id }">
                <div class="flow-node approval-node" :class="nodeStatusClass(data.status)">
                  <Handle type="target" :position="Position.Left" />
                  <div class="node-accent"></div><div class="node-kicker"><UserRoundCheck :size="12" /> {{ t('humanGate') }}</div><div class="node-label">{{ t('nodeApproval') }}</div>
                  <div class="node-bottom"><span class="node-status-dot"></span><span>{{ statusLabel(data.status) }}</span><Pause v-if="data.status === 'WAITING_APPROVAL'" :size="11" /><button class="node-log-button" :class="{ active: expandedLogNodeId === id }" :aria-label="t('openAgentLogs')" @click.stop="openStepLogs(id)"><Activity :size="10" /><span>{{ t('agentLogs') }}</span></button></div>
                  <div v-if="expandedLogNodeId === id" class="inline-agent-log" @pointerdown.stop @mousedown.stop @touchstart.stop @click.stop><div class="inline-agent-log-head"><span><Activity :size="10" /> {{ t('agentLogs') }}</span><div><strong>{{ stepLogs(id).length }} {{ t('events') }}</strong><button type="button" class="inline-log-copy" :disabled="!stepLogs(id).length" @click.stop="copyStepLogs(id)">复制</button></div></div><div v-if="!stepLogs(id).length" class="inline-log-empty">{{ t('noAgentLogs') }}</div><div v-for="(log, logIndex) in stepLogs(id)" :key="`${log.time}-${logIndex}`" class="inline-log-row"><span>{{ log.time }}</span><i class="activity-marker" :class="log.tone"></i><p>{{ log.message }}<small v-if="log.detail">{{ log.detail }}</small></p></div></div>
                  <Handle type="source" :position="Position.Right" />
                </div>
              </template>
            </VueFlow>
            <div class="canvas-legend"><span><i class="legend-dot pending"></i> {{ t('pending') }}</span><span><i class="legend-dot running"></i> {{ t('running') }}</span><span><i class="legend-dot success"></i> {{ t('success') }}</span><span><i class="legend-dot waiting"></i> {{ t('approval') }}</span></div>
          </div>

          <div class="brief-card">
            <div class="brief-icon"><FileText :size="17" /></div>
                  <div class="brief-copy"><div class="brief-label">{{ t('runBrief') }} <span>{{ t('input') }}</span></div><textarea v-model="requirement" :disabled="isBusy" placeholder="例如：开发一个简单的 HTML 广告网页" aria-label="运行需求"></textarea></div>
            <div class="brief-side"><span>{{ t('concurrency') }}</span><strong>{{ String(activeWorkflow?.concurrency ?? 1).padStart(2, '0') }}</strong><small>{{ t('maxParallel') }}</small></div>
          </div>

          <div class="activity-header"><div><div class="eyebrow"><span class="eyebrow-line"></span>{{ t('eventStream') }}</div><h2>{{ t('runActivity') }}</h2></div><span class="live-badge"><i></i> {{ t('live') }}</span></div>
          <div class="activity-feed">
            <div v-for="(log, index) in logs.slice(0, 6)" :key="`${log.time}-${index}`" class="activity-row"><span class="activity-time">{{ log.time }}</span><span class="activity-marker" :class="log.tone"></span><span class="activity-type">{{ localizedLogType(log.type) }}</span><span class="activity-message">{{ log.message }}<small v-if="log.detail">{{ log.detail }}</small></span></div>
          </div>
          <section v-if="artifacts.length" class="artifact-panel">
            <div class="artifact-panel-heading"><div><div class="eyebrow"><span class="eyebrow-line"></span>工作产物</div><h2>可预览文件</h2></div><div class="artifact-panel-heading-actions"><span>{{ artifacts.length }} 个文件</span><button type="button" class="artifact-action artifact-archive-action" :disabled="isDownloadingArchive" @click="downloadArtifactArchive">{{ isDownloadingArchive ? t('downloadingZip') : t('downloadAllZip') }}</button></div></div>
            <p v-if="archiveDownloadError" class="artifact-archive-error">{{ archiveDownloadError }}</p>
            <div v-for="artifact in artifacts" :key="artifact.id" class="artifact-row"><span class="artifact-icon"><Terminal :size="13" /></span><span class="artifact-copy"><strong>{{ artifact.name }}</strong><small>{{ artifact.previewable ? 'HTML 可预览' : '源码文件' }} · {{ artifact.sizeBytes }} bytes</small></span><div class="artifact-actions"><button v-if="artifact.previewable" type="button" class="artifact-action" @click="openArtifact(artifact)">预览</button><button type="button" class="artifact-action" @click="viewArtifactSource(artifact)">源码</button><a class="artifact-action" :href="artifactDownloadUrl(artifact)" download>下载</a></div></div>
            <section v-if="artifactViewer" class="artifact-viewer"><div class="artifact-viewer-head"><div><strong>{{ artifactViewer.artifact.name }}</strong><small>{{ artifactViewer.mode === 'preview' ? '页面预览' : '源码查看' }}</small></div><div class="artifact-viewer-tools"><button v-if="artifactViewer.mode === 'preview'" type="button" class="artifact-action" @click="viewArtifactSource(artifactViewer.artifact)">查看源码</button><button v-else-if="artifactViewer.artifact.previewable" type="button" class="artifact-action" @click="openArtifact(artifactViewer.artifact)">预览页面</button><a class="artifact-action" :href="artifactDownloadUrl(artifactViewer.artifact)" download>下载</a><button type="button" class="artifact-close" aria-label="关闭成果物查看器" @click="closeArtifactViewer"><X :size="13" /></button></div></div><div v-if="artifactViewer.mode === 'preview' && artifactViewer.artifact.previewable" class="artifact-preview-frame"><iframe :src="artifactViewer.artifact.contentUrl" title="HTML 成果物预览" sandbox="allow-scripts"></iframe></div><pre v-else-if="artifactViewer.mode === 'source' && !artifactViewer.loading && !artifactViewer.error" class="artifact-source"><code>{{ artifactViewer.content }}</code></pre><div v-else-if="artifactViewer.loading" class="artifact-viewer-empty">正在读取源码……</div><div v-else-if="artifactViewer.error" class="artifact-viewer-error">{{ artifactViewer.error }}</div><div v-else class="artifact-viewer-empty">该文件暂不支持页面预览，请查看源码或下载。</div></section>
          </section>
        </div>

        <aside class="inspector">
          <div class="inspector-top"><div><div class="eyebrow"><span class="eyebrow-line"></span>{{ t('inspector') }}</div><h2>{{ t('nodeDetail') }}</h2></div><button class="close-inspector" aria-label="关闭检查器"><X :size="16" /></button></div>
          <div class="inspector-node-title"><div class="inspector-node-icon" :class="selectedNode?.type === 'approval' ? 'approval' : ''"><UserRoundCheck v-if="selectedNode?.type === 'approval'" :size="18" /><Bot v-else :size="18" /></div><div><h3>{{ localizedNodeLabel(selectedNode?.id ?? '', selectedNode?.data.label ?? '') }}</h3><span>{{ localizedRole(selectedNode?.data.role) }}</span></div><span class="detail-status" :class="nodeStatusClass(selectedNode?.data.status)">{{ statusLabel(selectedNode?.data.status) }}</span></div>
          <div v-if="selectedNode?.type === 'approval' && runStatus === 'WAITING_APPROVAL'" class="approval-card"><div class="approval-card-top"><div class="approval-card-icon"><ShieldCheck :size="17" /></div><div><strong>{{ t('architectureReady') }}</strong><span>{{ t('unlockAgents') }}</span></div></div><div class="approval-actions"><button class="approve-button" :disabled="isSubmittingApproval" @click="resolveApproval('approve')"><Check :size="15" /> {{ isSubmittingApproval ? t('approving') : t('approveArchitecture') }}</button><button class="reject-button" :disabled="isSubmittingApproval" @click="resolveApproval('reject')">{{ isSubmittingApproval ? t('approving') : t('reject') }}</button></div><p v-if="approvalError" class="approval-error">{{ approvalError }}</p></div>
          <div v-if="runStatus === 'WAITING_CLARIFICATION' && clarificationRequest" class="clarification-card">
            <div class="approval-card-top"><div class="approval-card-icon"><CircleHelp :size="17" /></div><div><strong>需求需要二次确认</strong><span>{{ clarificationRequest.reason }}</span></div></div>
            <p class="clarification-impact">{{ clarificationRequest.impact }}</p>
            <select v-model="clarificationOption" class="clarification-select"><option v-for="option in clarificationRequest.options ?? []" :key="String(option.value)" :value="String(option.value)">{{ option.label }}</option></select>
            <button class="approve-button" :disabled="!runId" @click="submitClarification"><Check :size="15" /> 确认并继续</button>
            <p v-if="clarificationError" class="approval-error">{{ clarificationError }}</p>
          </div>
          <div v-if="selectedNode?.type === 'agent'" class="detail-section runtime-section">
            <div class="runtime-heading"><div class="detail-label">{{ t('runtimePolicy') }}</div><span class="runtime-effective">{{ t('effectiveLevel') }}: {{ effectiveThinking === 'auto' ? t('autoMode') : effectiveThinking }}</span></div>
            <p v-if="selectedRuntimeLocked" class="budget-note">系统控制 Agent 的 Token、重试和思考策略由工作流固定，普通运行配置不会覆盖。</p>
            <div class="runtime-grid">
              <label><span>{{ t('maxTokens') }}</span><input type="text" inputmode="numeric" pattern="[0-9]*" autocomplete="off" :disabled="selectedRuntimeLocked" :value="selectedRuntime.maxTokens" @input="onMaxTokensInput" /></label>
              <label><span>{{ t('retryCount') }}</span><select :disabled="selectedRuntimeLocked" :value="selectedRuntime.retry" @change="onRetryChange"><option v-for="count in [0, 1, 2, 3]" :key="count" :value="count">{{ count }}</option></select></label>
            </div>
            <div class="budget-mode-row">
              <label><span>{{ t('budgetMode') }}</span><select :value="budgetMode" @change="onBudgetModeChange"><option value="auto">{{ t('budgetAuto') }}</option><option value="manual">{{ t('budgetManual') }}</option></select></label>
              <p class="budget-note">{{ budgetMode === 'auto' ? t('budgetAutoNote') : t('budgetManualNote') }}</p>
            </div>
            <div class="budget-mode-row skill-mode-row">
              <label><span>{{ t('skillPolicy') }}</span><select :value="skillMode" @change="onSkillModeChange"><option value="auto">{{ t('skillAuto') }}</option><option value="on">{{ t('skillOn') }}</option><option value="off">{{ t('skillOff') }}</option></select></label>
              <p class="budget-note">{{ t('skillPolicyNote') }}</p>
            </div>
            <div v-if="selectedNode?.data.skills?.length" class="contract-row skill-candidates"><span>{{ t('skillCandidates') }}</span><span class="source-tags"><em v-for="skill in selectedNode.data.skills" :key="skill">{{ skill }}</em></span></div>
            <div class="thinking-picker"><button v-for="mode in thinkingModes" :key="mode" type="button" :class="{ selected: selectedRuntime.thinking === mode, unsupported: mode !== 'auto' && mode !== 'off' && !providerCapabilities.supportedLevels.includes(mode) }" :disabled="selectedRuntimeLocked || (mode !== 'auto' && mode !== 'off' && !providerCapabilities.supportedLevels.includes(mode))" @click="updateRuntimeField('thinking', mode)">{{ mode === 'auto' ? t('autoMode') : mode === 'off' ? t('thinkingOff') : mode === 'low' ? 'Low' : mode === 'high' ? 'High' : 'Max' }}</button></div>
            <p v-if="thinkingNeedsFallback" class="runtime-warning">{{ t('unsupportedThinking') }}</p>
          </div>
          <div class="detail-section"><div class="detail-label">{{ t('purpose') }}</div><p class="detail-description">{{ selectedNode?.data.description }}</p></div>
          <div class="detail-section"><div class="detail-label">{{ t('agentContract') }}</div><div class="contract-row"><span>{{ t('agentId') }}</span><code>{{ selectedNode?.data.agentId ?? 'human_approval' }}</code></div><div class="contract-row"><span>{{ t('inputSources') }}</span><span class="source-tags"><em v-for="edge in edges.filter((edge) => edge.target === selectedNode?.id)" :key="edge.id">{{ edge.source }}</em><em v-if="!edges.some((edge) => edge.target === selectedNode?.id)">run brief</em></span></div><div class="contract-row"><span>{{ t('outputKey') }}</span><code>{{ selectedNode?.type === 'approval' ? 'approval_decision' : `${selectedNode?.id}_result` }}</code></div></div>
          <div class="detail-section output-section"><div class="detail-label">{{ t('agentOutput') }} <span v-if="selectedOutput" class="output-live">{{ t('captured') }}</span></div><div v-if="selectedOutput" class="output-box"><div class="output-box-head"><span><Terminal :size="13" /> result.md</span><span>{{ selectedOutput.duration ?? 0 }}ms</span></div><p>{{ selectedOutput.output }}</p></div><div v-else class="empty-output"><span class="empty-orbit"><span></span></span><p>{{ t('outputWillAppear') }}<br /><small>{{ t('selectAfterComplete') }}</small></p></div></div>
          <div class="inspector-footer"><span><Clock3 :size="14" /> {{ t('retryPolicy') }}</span><strong>{{ t('attempts') }} <i>·</i> {{ t('exponentialBackoff') }}</strong></div>
        </aside>
      </section>

      <Transition name="clarification-modal">
        <div v-if="runStatus === 'WAITING_APPROVAL'" class="clarification-overlay" role="presentation">
          <section class="clarification-modal" role="dialog" aria-modal="true" aria-labelledby="architecture-approval-title">
            <div class="clarification-modal-top">
              <div class="approval-card-icon"><ShieldCheck :size="19" /></div>
              <div><span class="eyebrow">工作流暂停</span><h2 id="architecture-approval-title">请确认架构方案</h2></div>
            </div>
            <p class="clarification-modal-reason">需求分析和架构设计已经完成。批准后将冻结当前合同，并继续执行 Database、Backend、Frontend 和测试节点。</p>
            <p class="clarification-impact">如果方案与需求不符，请选择拒绝；系统会保留本次运行记录，不会继续生成错误成果物。</p>
            <p v-if="approvalError" class="approval-error">{{ approvalError }}</p>
            <div class="clarification-modal-actions approval-modal-actions">
              <button type="button" class="reject-button" :disabled="isSubmittingApproval" @click="resolveApproval('reject')">{{ isSubmittingApproval ? t('approving') : t('reject') }}</button>
              <button type="button" class="approve-button" :disabled="isSubmittingApproval" @click="resolveApproval('approve')"><Check :size="15" /> {{ isSubmittingApproval ? t('approving') : t('approveArchitecture') }}</button>
            </div>
          </section>
        </div>
      </Transition>

      <Transition name="clarification-modal">
        <div v-if="runStatus === 'WAITING_CLARIFICATION' && clarificationRequest" class="clarification-overlay" role="presentation">
          <section class="clarification-modal" role="dialog" aria-modal="true" aria-labelledby="clarification-title">
            <div class="clarification-modal-top">
              <div class="approval-card-icon"><CircleHelp :size="19" /></div>
              <div><span class="eyebrow">工作流暂停</span><h2 id="clarification-title">请确认需求边界</h2></div>
            </div>
            <p class="clarification-modal-reason">{{ clarificationRequest.reason || '当前需求存在会影响执行链路的未明确项。' }}</p>
            <p v-if="clarificationRequest.impact" class="clarification-impact">{{ clarificationRequest.impact }}</p>
            <p v-if="clarificationRequest.prompt_reference" class="clarification-prompt-reference">参考提问：{{ clarificationRequest.prompt_reference }}</p>
            <label class="clarification-modal-field"><span>请选择执行方案</span><select v-model="clarificationOption" class="clarification-select"><option v-for="option in clarificationRequest.options ?? []" :key="String(option.value)" :value="String(option.value)">{{ option.label }}</option></select></label>
            <p v-if="clarificationOption === 'use_recommended' && clarificationRequest.recommended_default?.primary_entity" class="clarification-suggestion">将按你确认的对象“{{ clarificationRequest.recommended_default.primary_entity }}”继续；可切换为自行填写。</p>
            <label v-if="clarificationOption === 'custom' && clarificationRequest.unresolved_fields?.includes('primary_entity')" class="clarification-modal-field"><span>你想管理的对象是什么？</span><input v-model.trim="clarificationEntityInput" class="clarification-input" type="text" autocomplete="off" placeholder="例如：客房、客房（Room）或预订（Booking）" /><small>常见对象可直接写中文；其他对象请附英文名称，例如“预订（Booking）”。</small></label>
            <template v-if="clarificationOption === 'custom'">
              <label v-for="field in (clarificationRequest.unresolved_fields ?? []).filter((item: string) => item !== 'primary_entity')" :key="field" class="clarification-modal-field">
                <span>{{ clarificationFieldPrompt(field).label }}</span>
                <select v-if="clarificationFieldPrompt(field).type === 'select'" v-model="clarificationCustomAnswers[field]" class="clarification-select">
                  <option value="" disabled>请选择</option>
                  <option v-for="option in clarificationFieldPrompt(field).options ?? []" :key="String(option.value)" :value="String(option.value)">{{ option.label }}</option>
                </select>
                <textarea v-else-if="clarificationFieldPrompt(field).type === 'textarea'" v-model.trim="clarificationCustomAnswers[field]" class="clarification-input" :placeholder="clarificationFieldPrompt(field).placeholder" rows="3"></textarea>
                <input v-else v-model.trim="clarificationCustomAnswers[field]" class="clarification-input" type="text" autocomplete="off" :placeholder="clarificationFieldPrompt(field).placeholder" />
                <small v-if="clarificationFieldPrompt(field).recommended != null">平台建议：{{ clarificationFieldPrompt(field).recommended }}</small>
              </label>
            </template>
            <p v-if="clarificationRequest.unresolved_fields?.length" class="clarification-modal-fields">待确认：{{ clarificationRequest.unresolved_fields.join('、') }}</p>
            <p v-if="clarificationError" class="approval-error">{{ clarificationError }}</p>
            <div class="clarification-modal-actions"><button type="button" class="approve-button" :disabled="!runId || !clarificationOption || !clarificationCustomComplete()" @click="submitClarification"><Check :size="15" /> 确认并继续</button></div>
          </section>
        </div>
      </Transition>

      <Transition name="drawer-fade">
        <div v-if="showRunHistory" class="settings-overlay" @click.self="showRunHistory = false">
          <section class="api-drawer run-history-drawer" role="dialog" aria-modal="true" aria-label="运行记录">
            <div class="drawer-header"><div><div class="eyebrow"><span class="eyebrow-line"></span>Run / audit</div><h2>运行记录</h2><p>历史状态、恢复次数和失败原因均来自后台持久化记录。</p></div><button class="close-inspector" aria-label="关闭" @click="showRunHistory = false"><X :size="16" /></button></div>
            <div class="run-history-toolbar"><span>最近 {{ runHistory.length }} 条</span><button type="button" class="artifact-action" :disabled="runHistoryLoading" @click="loadRunHistory"><RotateCcw :size="13" /> 刷新</button></div>
            <div v-if="runHistoryLoading" class="run-history-empty"><LoaderCircle class="spin-icon" :size="18" /> 正在读取运行记录……</div>
            <div v-else-if="runHistoryError" class="run-history-empty error">{{ runHistoryError }}</div>
            <div v-else-if="!runHistory.length" class="run-history-empty">暂无运行记录</div>
            <div v-else class="run-history-list">
              <button v-for="item in runHistory" :key="item.id" type="button" class="run-history-item" @click="openHistoricalRun(item)">
                <span class="run-history-status" :class="String(item.status).toLowerCase()">{{ item.status }}</span>
                <span class="run-history-copy"><strong>{{ item.requirement || '未记录需求' }}</strong><small>{{ item.id }} · {{ workflowName(item.workflow_id) }}</small><small>{{ formatHistoryTime(item.started_at) }} · 恢复 {{ item.recovery_count ?? 0 }} 次</small><em v-if="item.error_message">{{ item.error_message }}</em></span>
                <ChevronRight :size="15" />
              </button>
            </div>
          </section>
        </div>
      </Transition>

      <Transition name="drawer-fade">
        <div v-if="showApiConfig" class="settings-overlay" @click.self="showApiConfig = false">
          <section class="api-drawer" role="dialog" aria-modal="true" :aria-label="t('apiSettings')">
            <div class="drawer-header"><div><div class="eyebrow"><span class="eyebrow-line"></span>Orbit / provider</div><h2>{{ t('apiSettings') }}</h2><p>{{ t('cloudApiDescription') }}</p></div><button class="close-inspector" :aria-label="t('close')" @click="showApiConfig = false"><X :size="16" /></button></div>
            <div class="provider-status-row"><div class="provider-status-icon"><CloudCog :size="17" /></div><div><strong>{{ apiConfig.provider === 'chatgpt_local' ? t('chatgptLocal') : apiConfig.usingMock ? t('demoMode') : apiConfig.modelName }}</strong><span>{{ apiConfig.provider === 'chatgpt_local' ? t('localCodexNote') : apiConfig.usingMock ? t('mockProviderNote') : t('apiKeyConfigured') }}</span></div><span :class="['provider-status-dot', { live: !apiConfig.usingMock }]" /></div>
            <div class="api-form">
              <label><span>{{ t('provider') }}</span><select :value="apiConfig.provider" @change="onProviderChange"><option value="qwen">{{ t('qwen') }}</option><option value="deepseek">{{ t('deepseek') }}</option><option value="glm">{{ t('glm') }}</option><option value="openai_compatible">{{ t('openaiCompatible') }}</option><option value="chatgpt_local">{{ t('chatgptLocal') }}</option></select></label>
              <label v-if="apiConfig.provider !== 'chatgpt_local'"><span>{{ t('baseUrl') }}</span><input v-model="apiConfig.baseUrl" type="url" spellcheck="false" /></label>
              <label v-if="apiConfig.provider === 'chatgpt_local'"><span>{{ t('model') }}</span><select v-model="apiConfig.modelName" @change="applyDraftProviderCapabilities"><option v-for="model in localCodexModels" :key="model.value" :value="model.value">{{ model.label }}</option></select></label>
              <label v-else><span>{{ t('model') }}</span><input v-model="apiConfig.modelName" spellcheck="false" @input="applyDraftProviderCapabilities" /></label>
              <label v-if="apiConfig.provider !== 'chatgpt_local'"><span>{{ t('apiKey') }} <em v-if="apiConfig.apiKeyConfigured">{{ t('apiKeyConfigured') }}</em></span><div class="key-input"><KeyRound :size="14" /><input ref="apiKeyInput" v-model="apiConfig.apiKey" type="password" :placeholder="apiConfig.apiKeyConfigured ? '••••••••••••••••' : t('keyPlaceholder')" autocomplete="off" spellcheck="false" @focus="syncApiKeyFromInput" @change="syncApiKeyFromInput" /></div></label>
            </div>
            <div class="secret-note"><ShieldCheck :size="14" /><span>{{ apiConfig.provider === 'chatgpt_local' ? t('localCodexNote') : t('apiSecretNote') }}</span></div>
            <div v-if="providerCapabilities.configurationWarning" class="provider-config-warning"><span>!</span>{{ providerCapabilities.configurationWarning }}</div>
            <div v-if="connectionTest.kind !== 'idle'" class="connection-result" :class="connectionTest.kind"><CheckCircle2 v-if="connectionTest.kind === 'success'" :size="15" /><X v-else :size="15" /><span>{{ connectionTest.message }}</span><small v-if="connectionTest.latency">{{ connectionTest.latency }}ms</small></div>
            <div class="drawer-actions"><button class="test-button" :disabled="isTestingConnection || isSavingConfig" @click="testConnection"><LoaderCircle v-if="isTestingConnection" class="spin-icon" :size="14" /><PlugZap v-else :size="14" /> {{ isTestingConnection ? t('testing') : t('testConnection') }}</button><button class="save-button" :disabled="isTestingConnection || isSavingConfig" @click="saveApiConfig"><LoaderCircle v-if="isSavingConfig" class="spin-icon" :size="14" /><Check v-else :size="14" /> {{ t('saveConfiguration') }}</button></div>
          </section>
        </div>
      </Transition>

      <footer class="command-bar"><div class="command-hint"><span class="shortcut">⌘</span><span class="shortcut">K</span><span>{{ t('commandCenter') }}</span></div><div class="footer-note"><Zap :size="13" /> {{ t('statePersisted') }} <span class="footer-divider"></span> {{ t('yamlSource') }}</div><div class="run-actions"><button class="reset-button" @click="resetRun()" :disabled="isBusy || isStartingRun"><RotateCcw :size="14" /> {{ t('reset') }}</button><button v-if="isBusy" class="stop-button" @click="stopRun"><Square :size="13" fill="currentColor" /> {{ t('stopRun') }}</button><button v-else-if="conflictingRun" class="stop-button" @click="stopConflictingRun"><Square :size="13" fill="currentColor" /> {{ t('stopExistingRun') }}</button><button v-else-if="(runStatus === 'FAILED' || runStatus === 'STOPPED') && runId && !usingDemo" class="retry-button" :disabled="isRetryingRun" @click="retryRun"><RotateCcw :size="14" /> {{ isRetryingRun ? t('retryingRun') : t('retryFailedRun') }}</button><button v-else class="run-button" :disabled="isStartingRun" @click="startRun"><Play :size="14" fill="currentColor" /> {{ isStartingRun ? '正在提交…' : runStatus === 'SUCCESS' ? t('runAgain') : t('runWorkflow') }}</button><button class="language-button" :aria-label="locale === 'en' ? '切换为中文' : '切换为英文'" :title="locale === 'en' ? '切换为中文' : '切换为英文'" @click="toggleLocale"><span :class="{ active: locale === 'zh' }">中</span><i></i><span :class="{ active: locale === 'en' }">EN</span></button></div></footer>
    </main>
  </div>
</template>
