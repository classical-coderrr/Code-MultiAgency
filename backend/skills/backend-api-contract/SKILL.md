---
id: backend-api-contract
version: "1.0"
description: Keep backend output coherent around API contracts, validation, persistence and errors.
applies_to: [backend, backend_implementation, backend_agent]
capabilities: [backend]
min_complexity: medium
max_injection_tokens: 440
---

先明确资源边界、数据模型和 API 契约，再生成实现。每个写操作需要输入校验和明确错误响应；每个读取操作要定义空结果和分页（如适用）。仅实现确认范围内的数据层和接口，不臆造第三方服务。生成文件时让每个文件职责单一，并确保引用的接口、实体和配置存在。
