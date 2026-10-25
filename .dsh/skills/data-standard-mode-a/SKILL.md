---
name: data-standard-mode-a
description: 用同一个 standards MCP 对模式 A 文本进行解析、证据抽取、知识底座构建、新颖性分析，选择候选生成草案并导出。
---

先 inspect_project 确认 live 模式。依次调用 parse_documents(mode="A")、extract_knowledge(mode="A")、
build_knowledge_base(mode="A")、analyze_discovery、prioritize_discovery。
从 analyze_discovery 返回的 candidate_id 中明确选择候选，调用 generate_standard(mode="A", candidate_ids=[...])。
该选择仅为草案编排；旧接口的 approve 名称不代表人工批准。人工决策执行和上游关联继续暂缓修复。
所有输出必须标明草案；新颖性、完整性或低置信度问题须如实报告。
使用返回的同一个 STD-A-* ID 调用 enrich_standard、evaluate_standard、export_standard。
没有模式 A 的 D3 答案时，应报告不可评测，而不是 0% 准确率。不要读取模式 B 的答案作为模式 A 参考。
若 artifact_binding_verified=false，不得将后端当前模型的分数归给这份草案。
同一个 MCP 同时支持 B；共享工具省略 mode 时仍默认 B。不得入库或声称业务已经验收。
