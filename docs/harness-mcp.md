# DeepSeek Harness MCP 接入

## 已实现的结构

`Harness → stdio MCP → 本机 HTTP/SSE → 原 FastAPI → 原业务模块`。
MCP 进程不导入 `tools`，缓存、决策和产物继续由 FastAPI 进程持有。
使用 MCP Python SDK 1.x，部署依赖固定在 `requirements.txt`；测试依赖见 `requirements-dev.txt`。

## Windows 安装与启动

要求 Python 3.11+。在项目目录运行：

```powershell
powershell -NoProfile -File scripts/start.ps1 setup
powershell -NoProfile -File scripts/start.ps1 app
```

浏览器打开 `http://127.0.0.1:8123`，沿用原界面上传/选择数据。
新环境为 `.venv-local`，不复用旧机器的 `.venv`。
实时启动设置 `STANDARD_EXECUTION_MODE=live`：不装载演示快照，不返回历史响应；原演示文件不被改写。

业务模型仍通过现有 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` 或本机私有配置指定。
主模型、批量模型和 fallback 都需与当前网关匹配。真实密钥不得写入提交文件。

## 配置 Harness

原生 Windows Harness：

```powershell
.\.venv-local\Scripts\python.exe scripts/configure_harness.py
```

Harness 在 WSL、业务后端在 Windows 时：

```powershell
.\.venv-local\Scripts\python.exe scripts/configure_harness.py --wsl
```

生成 `.dsh/mcp.local.yml`（本机绝对路径，Git 忽略）。
在 Harness CLI 使用 `dsh web --patch /absolute/path/to/.dsh/mcp.local.yml`，或将该 insert 配置合并至目标 profile。
WSL 配置通过 interop 启动 Windows Python，MCP HTTP 请求仍在 Windows 本机发出，无需开放后端到局域网。
桌面、Web、headless 的 profile 不应混淆。已有 profile 中的模型配置保持其自身设置。

会出现 9 个 `mcp__standards__*` 工具：inspect_project、analyze_requirements、build_knowledge_base、
propose_clarifications、match_standards、generate_standard、enrich_standard、evaluate_standard、export_standard。
运行 Skill 位于 `.dsh/skills/data-standard-mode-b/SKILL.md`。

推荐请求：

> 检查已选模式 B 数据，解析并分析完备性，构建知识底座，匹配已有标准，生成草案。使用返回的同一标准 ID 补写定义、评测和导出。明确说明未修复的审批和产物关联限制。

模型需支持工具调用，工具超时配置为 360 秒。遇到超时先检查后端，不要直接重复生成。
界面事件总线已兼容后端命名 SSE；各中间面板仍沿用原交互，不保证全部自动刷新。

## 合成数据验证

以下步骤创建全新隔离副本，完全不复制原业务数据、密钥和历史产物：

```powershell
.\.venv-local\Scripts\python.exe scripts/prepare_synthetic.py
.\.venv-local\Scripts\python.exe scripts/run_validation_backend.py --port 8125
```

第二个命令保持运行。它默认调用 Windows Ollama `gemma4:31b-cloud`；仅合成字段发给模型。
另开终端进行真实 MCP 协议测试：

```powershell
.\.venv-local\Scripts\python.exe scripts/verify_mcp.py --base-url http://127.0.0.1:8125 --enrich
.\.venv-local\Scripts\python.exe scripts/configure_harness.py --wsl --base-url http://127.0.0.1:8125
```

本机现有 WSL 安装的真实 Harness 验证：

```powershell
wsl -d Ubuntu-24.04 -u vimdr -- python3 /absolute/wsl/project/path/scripts/verify_harness.py --base-url http://127.0.0.1:8125
```

`--installation` 可覆盖 Harness 安装目录。脚本使用项目 `.runtime/harness-home` 独立 headless 配置，
只读取原 profile 的 Ollama 端点及能力描述，不复制凭据、不修改原服务。
当原 WSL Docker 模型转发桥未运行，可增加 `--windows-ollama-relay`：测试期间通过临时回环 HTTP 服务和
Windows Python interop 调用现有 Ollama，结束时关闭。它仍是真实云模型调用，不是模拟模型。

报告和原始日志在 `.runtime/`，均不提交。`mcp-verification.json` 是直接 MCP 客户端测试；
`harness-verification.json` 才是 Harness 真实模型工具调用验证。合成数据评测不代表真实业务准确率。

## 脱敏与提交范围

源码基线标签：`baseline-original-sanitized`。`data/`、`deliverables/`、私有配置、日志、虚拟环境、浏览器缓存、
历史迁移记录均不进入 Git。合成测试数据由生成器创建，不提交原始 xlsx/PDF。
每次提交前运行 `python scripts/check_repository_secrets.py`；它检查 Git 暂存内容并只报告文件/规则，不打印密钥。

本轮明确暂缓的业务修复和验收方法见 [后续修复计划](deferred-fixes.md)。
