---
id: test-acceptance
version: "1.0"
description: Convert requirements and generated artifacts into executable acceptance checks.
applies_to: [tester, tester_agent]
min_complexity: medium
max_injection_tokens: 320
---

从用户目标倒推验收标准，而不是只罗列技术名词。覆盖主流程、输入校验、失败路径、空数据与关键边界。将测试分为可自动化检查和需要人工确认的体验检查；明确每项检查依赖哪个成果物或接口。
