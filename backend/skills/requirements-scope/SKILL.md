---
id: requirements-scope
version: "1.0"
description: Keep requirement scope testable and prevent optional ideas becoming commitments.
applies_to: [requirement, requirement_analysis, requirement_agent]
min_complexity: medium
max_injection_tokens: 360
---

将原始需求、明确约束、可选建议和待确认事项分开表达。每项必做能力都要有可验收结果；不要把“可选建议”变成实现承诺。遇到范围不清时，保留最小可用范围，并输出不超过 5 个能改变实现路径的澄清问题。
