---
id: architecture-routing
version: "1.0"
description: Select capability branches conservatively and preserve required implementation paths.
applies_to: [architecture, architect_agent]
min_complexity: medium
max_injection_tokens: 380
---

先按能力拆分架构：前端、后端、持久化、鉴权、外部集成、异步、成果物。对每条能力给出“必需 / 可选 / 不需要”的证据。业务管理、数据实体或 CRUD 需求默认保留前后端分支；只有用户明确要求纯前端、无后端或 localStorage 时才移除后端。输出必须与工作流的 JSON 契约兼容。
