---
id: frontend-vue-implementation
version: "1.0"
description: Build maintainable frontend flows with explicit state, errors and API boundaries.
applies_to: [frontend, frontend_implementation, frontend_agent]
capabilities: [frontend]
min_complexity: medium
max_injection_tokens: 440
---

按照页面、组件、状态和 API 客户端划分职责。涉及 CRUD 时明确列表、加载、空态、表单校验、提交中、失败提示和刷新路径。不要把后端接口假设藏在组件里；用集中 API 模块声明请求与响应。仅生成用户确认的功能，不为“可能需要”的能力引入额外依赖。
