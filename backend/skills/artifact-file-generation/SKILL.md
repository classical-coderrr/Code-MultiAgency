---
id: artifact-file-generation
version: "1.0"
description: Generate complete, independently checkable source artifacts with minimal continuation.
applies_to: [backend, frontend, backend_implementation, frontend_implementation]
min_complexity: medium
max_injection_tokens: 360
---

先输出最小文件清单和依赖关系，再逐文件生成完整内容。一个文件只承担一个清晰职责；不要为了压缩篇幅省略 import、配置、类型、闭合标签或引用的实现。优先拆分模块而不是续写同一大文件。每个文件完成后自检：语法闭合、引用可解析、功能与需求对应。
