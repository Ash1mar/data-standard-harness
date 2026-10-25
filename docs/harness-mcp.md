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
本机当前使用已有 Ollama 登录和云模型，可启动 `scripts/start.ps1 app -UseLocalOllama`。
该参数同时设置主模型与 `LLM_BULK_MODEL`，避免批量调用沿用旧网关的模型名。
`gemma4:31b-cloud` 会经 Ollama 发送内容到云端，不是离线本地推理；真实数据需先确认外发范围。

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

同一个 `standards` MCP 提供 13 个 `mcp__standards__*` 工具，无需为 A/B 各部署一套：

| 工具 | 用途 |
| --- | --- |
| inspect_project | 检查后端、实时模式和数据槽位 |
| parse_documents / extract_knowledge / build_knowledge_base | 通过 `mode="A"/"B"` 复用解析、抽取和底座 |
| analyze_requirements / propose_clarifications / match_standards | B 需求分析、澄清建议和匹配 |
| analyze_discovery / prioritize_discovery | A 新颖性分析、候选 ID 和排序 |
| generate_standard | B 合成生成；A 根据明确的 candidate_ids 生成草案 |
| enrich_standard / evaluate_standard / export_standard | 根据 STD-A/STD-B ID 推断模式 |

共享工具省略 mode 时仍默认为 B，兼容原 B 调用。运行 Skill 位于 `.dsh/skills/data-standard-mode-{a,b}/SKILL.md`。
A 的 candidate_ids 必须来自本次发现结果；旧 approve 接口仅用于技术草案选择，不能视作人工批准。

推荐请求：

> 检查已选模式 B 数据，解析并分析完备性，构建知识底座，匹配已有标准，生成草案。使用返回的同一标准 ID 补写定义、评测和导出。明确说明未修复的审批和产物关联限制。

模型需支持工具调用，工具超时配置为 360 秒。遇到超时先检查后端，不要直接重复生成。
界面事件总线已兼容后端命名 SSE；各中间面板仍沿用原交互，不保证全部自动刷新。

评测会传入同一标准 ID。现有评测器读取该 P6 文件后，在组装报告时遗漏 standard_id；
`artifact_binding_verified=false` 表示响应元数据不足，分数仅供参考，不能作为验收结论。
本轮仅提示这个限制，没有改动评测器。完整类清单包括无属性的父类，勿与 P6 事件的有属性类数混淆。

## 本机日常 Harness Web 与模型桥

已将 MCP 合并到现有 Web profile，保留原模型凭据及其他插件。
日常配置恢复为后端 `http://127.0.0.1:8123`；Harness Web 使用原端口 3080 和原鉴权方式。

原 Docker Desktop 及 `dsh-ollama-bridge` 容器已恢复，继续使用原回环端点 `127.0.0.1:11435/v1`，
经原桥调用 Windows Ollama；凭据和模型路由保持原配置。原日常管理脚本 start/restart 会启动该容器。
联调期间的备用 systemd 桥已撤销，避免和原容器占用同一端口；最终运行不依赖临时转发进程。

跨机器重新安装时，先生成上述 `--wsl` 配置，再在 WSL 执行：

```bash
sudo python3 /absolute/project/path/scripts/install_local_harness.py --installation /opt/data/deepseek-harness
```

安装器先备份 Web profile 与运行 Skill 到安装目录 backups，再合并受管 MCP 块并重启 Harness。
它不安装模型服务、不修改凭据或其他插件。迁移机器时需先准备原模型端点；恢复配置可还原对应 profile 备份。

## 合成数据验证

以下步骤创建全新隔离副本，完全不复制原业务数据、密钥和历史产物：

```powershell
.\.venv-local\Scripts\python.exe scripts/prepare_synthetic.py
.\.venv-local\Scripts\python.exe scripts/run_validation_backend.py --port 8125
```

第二个命令保持运行。它默认调用 Windows Ollama `gemma4:31b-cloud`；仅合成字段发给模型。
测试 A 时改为 `prepare_synthetic.py --mode A`，后端可另用 8126 端口；创建后保存 latest-validation 所指向的隔离副本。
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
`harness-A-verification.json` / `harness-B-verification.json` 是 Harness headless 测试报告。
合成数据评测不代表真实业务准确率。

日常 Web 会话测试使用 `verify_web_harness.py --mode A/B`；它通过原服务的鉴权/RPC 创建可见会话，
不打印启动 token。`assess_web_harness.py --mode A/B` 从该会话的持久日志核对真实调用、ID 传递、批次和导出。
在 WSL 以能读取安装日志的账户运行；原始日志只落本机 `.runtime`。
脚本会按当前 Web MCP 后端执行，A 合成验证前必须指向隔离后端，结束后恢复原端口，勿向原 A 扫描资料误跑合成验证。

## 脱敏与提交范围

源码基线标签：`baseline-original-sanitized`。`data/`、`deliverables/`、私有配置、日志、虚拟环境、浏览器缓存、
历史迁移记录均不进入 Git。合成测试数据由生成器创建，不提交原始 xlsx/PDF。
每次提交前运行 `python scripts/check_repository_secrets.py`；它检查 Git 暂存内容并只报告文件/规则，不打印密钥。

本轮明确暂缓的业务修复和验收方法见 [后续修复计划](deferred-fixes.md)。
