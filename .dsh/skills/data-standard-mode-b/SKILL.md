---
name: data-standard-mode-b
description: 使用已连接的 standards MCP 工具，将模式 B 需求表解析、构建知识底座、匹配并生成标准草案，再评测和导出。
---

先调用 `mcp__standards__inspect_project`，要求后端为 live 模式且回放关闭。
输入文件、D4 规则、D0 标准库和可选 D3 答案由用户在现有网页中选择。
依次调用 parse_documents(mode="B")、extract_knowledge(mode="B")、analyze_requirements、build_knowledge_base(mode="B")、match_standards。
需要澄清时调用 propose_clarifications 并说明建议；不得声称已获得人工批准。
用户要求生成时调用 generate_standard。所有结果都是草案。
如需模型补写定义，调用 enrich_standard，单次 limit 不超过 24。
把 generate_standard 返回的同一个 standard_id 传给 evaluate_standard 和 export_standard。
无 D3 时说明不可评测；不得将其说成准确率为零。
评测若 artifact_binding_verified=false，说明返回结果的标准 ID 不完整，分数仅作参考，不得当作验收结论。类数须区分完整类清单和有属性的类。
超时后可能仍在计算，先检查网页和产物，勿直接重复生成。
本版本暂未修复人工决策执行和全局 latest 上游关联问题，必须在报告中如实说明。
不要修改或写回已有标准库。详细设计和待办见 docs/deferred-fixes.md。
