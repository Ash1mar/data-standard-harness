---
name: nuclear-standard-agent-dual-mode
description: 在 DeepSeek Harness 上构建“AI+核电全生命周期数据标准梳理”智能体，支持发现式梳理与需求转化双模式。启动时先向用户输出数据准备清单并等待数据就绪，再按「输入/预期输出」隔离原则构建与评测；P2 术语归一与带证据抽取、P4 新颖性与立项优先级、P5 字段推断、P6 定义补全四处真实调用大模型（无 key 自动回落规则）。交付统一 Web 演示界面，用于智能体应用开发大赛现场演示。
whenToUse: 当用户要求开发、配置或演示“核电数据标准智能治理”智能体，且需要覆盖“AI 主动发现标准”与“需求文件转化为数据模型”两类业务场景时使用。
version: 3.4.0
---

> **v3.3 修订说明（2026-10-22 晚，用户重排数据 + 规则.md）**
> 1. **数据目录改为「输入 / 预期输出」成组结构**（见 1.5）：A 两组、B 一组；旧 `dev/test` 四段式废弃。
> 2. **评测必须按 `(mode, group)` 绑定**：生成侧只读该组 `输入/`，答案侧只读同组 `预期输出/`；由 `dataset_registry.py` 唯一定义，禁止跨组/跨模式混用。
> 3. **导出按 `规则.md` 三表规范**：`对象类`（13 列）/ `对象类属性`（24 列）/ `属性`（20 列）；ID 规则见 1.3.4。
> 4. **业务需求是自然语言**，需转换：`字符型（50）`→`String`+`VARCHAR(50)`、`数值型（12,2）`→`Float`+`FLOAT`；业务规则原文只进备注，不当技术格式。
> 5. 评测分**对象类层 / 属性层**，每层同时报「生成准确率」与「覆盖率」；`constraint` 口径不同，只作参考不计入 exact。

> **v3.1 修订说明（2026-10-22）**
> 1. 1.3.4 新增 **D10 数据建模规则（元元数据）**：规定字段可填写项、对象类/属性/关系的编码生成规则、命名规则与禁止项，作为 P5 模型合成与 P6 标准生成的**唯一字段规则源**。
> 2. （v3.3 起 D10 已重编号为 D4）D10 缺失时按「旧 D4 模板字段键 + 通用规则」兜底，编码留空标 `INCOMPLETE`。
> 3. **D7 重新定义为「标准评测集」**（即该模式 `test/output/` 的规范标准三表），用于计算**数据标准生成准确率**：模型生成的每一行标准与评测集逐行比对。评测口径见 1.3.4。

> **v3.0 修订说明（2026-10-21，用户确认）**
> 1. 数据目录改为**四段式**（`mode{A,B}/{dev,test}/{input,output}`），dev/test 由**用户手工切分**，不再由 `split_model` 自动切分。
> 2. 新增 **1.8 模式隔离**：A / B 知识底座**不共用节点**，并新增**跨模式防泄漏**。
> 3. 硬指标从「图谱 ≥ 200/500 节点」改为**三维度**：对象类 / 属性 / 关系，且 A、B 分别设阈值。
> 4. **D7 原为「人工标注抽样集」** —— 模式 A 文档是扫描件、无文本层，做不出片段级标注；**v3.1 已将其重新定义为「标准评测集」**（不再需要片段级人工标注），准确率按 `*/test/output/` 三表逐行比对，见 1.3.4。
> 5. P0 新增 **1.9 现场数据接入（上传入口）**：比赛演示必须从上传文件开始，不得默认读固定路径。
> 6. 新增 **第 11 章 落地记录**：已交付增量、实测规模、降级项与已知问题。

# 核电数据标准智能治理智能体 · 双模式构建规格

## 0. 双模式系统架构

```
                     ┌──────────────────────────┐
                     │  P0 模式选择与系统总览     │
                     └────────────┬─────────────┘
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
┌──────────────────────────┐           ┌──────────────────────────┐
│ 模式 A · 发现式标准梳理    │           │ 模式 B · 需求转化          │
│ 输入：原始业务文档集       │           │ 输入：模板初填需求文件      │
│ 业务未知：需要什么标准     │           │ 业务已知：大致要什么        │
│ 输出：候选标准全景图       │           │ 输出：最终可用数据模型      │
│      + 数据标准建议       │           │      + 冲突消解记录        │
└────────────┬─────────────┘           └────────────┬─────────────┘
             └──────────────┬───────────────────────┘
                            ▼
              ┌─────────────────────────────┐
              │  共享核心                    │
              │  · 双知识底座（JSON+OWL/图谱）│
              │  · 已有标准库                 │
              │  · 标准生成与演化             │
              │  · 质量评价                   │
              │  · 人机门控                   │
              └─────────────────────────────┘
```

| 维度 | 模式 A · 发现式梳理 | 模式 B · 需求转化 |
|---|---|---|
| 业务起点 | 不知道需要什么标准 | 知道自己要什么，但需求文件不完整 |
| 输入 | 原始业务文档集（多份、多格式） | 一份模板初填的需求文件 |
| 核心动作 | 解析→抽取→新颖性检测→候选全景→立项 | 解析需求→完备性分析→澄清/推断→匹配→合成→冲突消解 |
| 关键产出 | 候选标准全景图（含新颖性标签和优先级）
 | 最终数据模型（字段完备、口径统一、可导入 EDM） |
| 业务决策点 | 在全景图上勾选作为新增数据标准的条目 | 确认澄清结果、接受或调整冲突消解建议 |

**硬约束：** UI 上每个结果必须可追溯到 Harness 的真实工具调用链。评委现场指定进入任一模式，系统必须走通对应全流程。

## 1. 数据准备与就绪（阶段 0）

### 1.1 核心原则：先告知，再等待，后校验

本系统的所有验收指标都建立在**用户提供的真实数据**之上。Harness 启动后的**第一个动作不是检查文件，而是向用户输出《数据准备清单》**，明确告知需要准备什么、放到哪个文件夹、格式与规模要求，然后**暂停等待**。

流程固定为：

```
智能体启动
  → 输出《数据准备清单》（对话 + 写入 data/DATA_REQUEST.md）
  → 暂停，等待用户回复“数据已就绪”
  → 执行 data_readiness_check（逐项校验）
  → 输出《数据就绪报告》（ready / missing / degraded）
  → 对缺失项询问用户：补充数据，还是确认降级
  → 用户确认后，进入阶段 1（数据接入与防泄漏检查）
  → 通过后，进入增量 0 开发
```

**三条禁令：**
1. 未输出清单前，不得开始任何解析、抽取、生成或开发动作。
2. 用户未回复"已就绪"前，不得读取 `data/` 下任何业务文件。
3. 数据缺失时不得编造、抓取或静默用示例数据替代，只能进入显式降级并记录。

### 1.2 智能体启动时必须输出的《数据准备清单》

清单以 Markdown 表格 + 逐项说明的形式渲染在对话中，同时写入 `data/DATA_REQUEST.md`。清单必须包含以下字段：

| 列 | 说明 |
|---|---|
| 编号 | D1、D2、D3……（v3.3 新编号，见 1.3.0）|
| 数据项 | 简短名称 |
| 用途 | 说明它支撑哪个模式、哪个面板、哪个指标 |
| 格式 | 文件类型与内部结构要求 |
| 规模下限 | 最少数量/页数/条数 |
| 放置路径 | 精确到文件夹，用户按路径放置 |
| 是否阻断 | 阻断 / 建议 / 可选 |
| 缺失后果 | 哪项指标会失效 |

**清单输出后，智能体必须附一句明确的行动指引：**

> 请将上述文件按“放置路径”放入项目目录，完成后回复“数据已就绪”。如需先看某个模式的演示，可只准备该模式所需数据（必填：D2、D4；建议：D1、D3、D5），并回复"仅准备模式 X 数据"。

### 1.3 数据项明细

#### 1.3.0 编号变更对照（v3.3，必须按新编号沟通）

| 旧编号 | 新编号 | 处理 |
|---|---|---|
| D1-dev / D1-test | **D1** | 合并，按组（A/group1、A/group2） |
| D3-dev / D3-test | **D2** | 合并，按组（B/default，1–N 份） |
| D7 | **D3** | 改名「预期输出」，按组 |
| D10 | **D4** | 改名「数据标准建模规则」，唯一规则源 |
| D5 | **D5** | 保留 |
| D8 / D9 | **D7 / D8** | 本次不要求上传，列为未来开发方向 |
| D0 / D2（原）/ D4（原） | — | **删除** |

#### 1.3.1 输入数据

| 编号 | 数据项 | 用途 | 格式 | 规模下限 | 放置路径 | 阻断 |
|---|---|---|---|---|---|---|
| **D1** | 模式 A 业务文档（按组） | 发现式梳理的输入，生成对象类 / 属性 | PDF / DOCX / XLSX / MD，**必须有文本层** | 每组 ≥ 1 份，文本 ≥ 500 字符 | `data/modeA/输入 group1/`、`data/modeA/输入 group2/` | 阻断（A） |
| **D2** | 模式 B 业务需求表 | 需求转化的输入，生成对象类 / 属性 | XLSX，含「对象类属性」表与「属性中文名称」列；值域 sheet 建议保留 | 1–N 份（现 5 份） | `data/modeB/输入/` | 阻断（B） |

**D1 硬要求：** 扫描件（文本层 0 字符）判 `degraded`，P7 标 `evaluable=false`，**不得**用文件名硬凑候选。本项目两份输入均为扫描件，是当前唯一阻断项。

**D2 数据质量要求：** 对象类编码按 `两位大写_英文缩写`（如 `PP_PIPE`），现在存在 `Q` / `QB` / `PE_SIT` 混用；不规范时导出保留原值并备注「待修正」，不编造。

#### 1.3.2 预期输出（评测答案）

| 编号 | 数据项 | 用途 | 格式 | 规模下限 | 放置路径 | 阻断 |
|---|---|---|---|---|---|---|
| **D3** | 预期输出（按组，**可选**） | P7 评测答案；用户梳理新数据时通常没有，缺席不影响生成，只是不评分 | XLSX：`对象类`（13 列）/ `对象类属性`（24 列）/ `属性`（20 列） | 有则评分，无则只生成 | `data/modeA/预期输出 group1|2/`、`data/modeB/预期输出/` | 可选（仅评测） |

**组与规模（本项目实测）：**

| 组 | 输入（D1 / D2） | 预期输出（D3） | 评测维度 |
|---|---|---|---|
| A/group1 | 物项分级原则（1 份 PDF） | E板块标准（160 类 / 0 属性） | 只评对象类层 |
| A/group2 | 管道等级表（1 份 PDF） | 管道管件模型（21 类 / 417 属性） | 对象类 + 属性 |
| B/default | 5 份业务需求表 | EM4 模型（57 类 / 891 属性 / 46 属性字典） | 对象类 + 属性 |

#### 1.3.3 规则与支撑数据

| 编号 | 数据项 | 用途 | 格式 | 规模下限 | 放置路径 | 阻断 |
|---|---|---|---|---|---|---|
| **D4** | **数据标准建模规则** | 三表列定义、ID / 编码规则、类型映射、留空规则；P5 / P6 / 导出 / 评测的**唯一规则源** | JSON（`schema: dsh.model_spec/v1`） | 3 张表 + 5 条 ID 规则 + 9 类类型映射 | `data/modeling_rules.json` | 阻断（B / 导出） |
| **D5** | 需求空白模板（可选） | 模板结构识别；不传则按列自动定位 | XLSX / JSON | 1 份 | `data/templates/` | 可选 |

#### 1.3.4 未来开发方向（本次不要求上传）

| 编号 | 数据项 | 用途 | 格式 | 放置路径 | 状态 |
|---|---|---|---|---|---|
| **D7** | 业务域分类清单 | P4 全景图行维度；当前从 D3 的「所属专业」自动聚合（group1 已覆盖仪控/电气/工艺/设备） | JSON，含 `domains[]` | `data/taxonomy.json` | 未来 |
| **D8** | 数据安全分级说明 | 字段外传管控 | MD / JSON | `data/security_policy.*` | 未来 |


**D4 数据标准建模规则（`data/modeling_rules.json`）：**

D4 描述的不是业务数据，而是**字段本身的规范**（元元数据），是 P5 合成、P6 生成、导出、评测的**唯一规则源**，杜绝模型自造字段名 / 编码 / 类型。

实际结构（`schema: dsh.model_spec/v1`）：

| 键 | 内容 |
|---|---|
| `tables[]` | 三张表的列定义：`对象类` 13 列 / `对象类属性` 24 列 / `属性` 20 列，每列含 key / cn / req / rule |
| `id_rules` | `object_class_id`（前缀+6 位）、`object_class_code`、`attribute_id`（Attr+6 位）、`attribute_code`、`relation_id`（对象类ID+属性ID） |
| `type_map` | 自然语言 → 类型：字符型→String、参照型→String、数值型→Float、整型→Int、日期型→Date… |
| `constraint_rules` | String 有长度→VARCHAR(n)；Float→FLOAT；参照型→留空 |
| `empty_rules` | 未填且无规则→留空；不补默认值、不编造编码 |
| `status_values` | 草稿 / 有效 / 预留 / 删除 |
| `conflict_rules` | 高相似归并（≥0.85）/ 中段人工（0.62–0.85）/ 低相似新增（<0.62） |

> 旧版 `dsh.modeling_rules/v1`（`object_code` / `fields[]`）已被上述结构取代；代码只读 `model_spec/v1`。

### 1.4 数据就绪校验与降级

用户回复"数据已就绪"后，Harness 执行 `data_readiness_check`，对每个数据项返回 `ready / missing / degraded`，写入 `data/readiness_status.json`（机器读）与 `data/readiness-report.md`（人读）：

- **上传接入后（v3）**：就绪面板不再是静态快照 —— 每个数据项若存在对应上传槽位，用**上传文件的真实校验结果**覆盖静态状态，并标 `source: uploaded | builtin`。

- **D1 / D2**：逐份试解析 1 页；D1 额外统计文本层字符数（< 500 判 degraded）。
- **D3**：按组检查三表（对象类 / 对象类属性 / 属性），group1 允许只有对象类表。
- **D4 / D5**：可解析，字段数/章节数统计。
- **D7**：三表均可解析，统计对象类 / 属性 / 关系行数，并与待评测对象类集合比对覆盖率（≥ 90%）。
- **D4**：可解析，`tables[]` = 3 张表、`id_rules` 5 条、`type_map` ≥ 8 类。
- **D2 / D8 / D9**：存在则校验，不存在标 `missing`（非阻断）。
- **D4 缺失**：标 `missing` 并**阻断导出**（可生成草稿，不可出正式表），P5 顶部横幅提示。

**缺失处理：** 对每个 `missing` 项，Harness 必须明确告知"影响哪个模式、哪个面板、哪个指标"，并给出三个选项：`补充数据` / `确认降级继续` / `仅演示另一模式`。用户选择后写入 `data/readiness-report.md` 的 `degradation_decisions` 字段。**未确认前不得继续。**

### 1.5 目录结构（数据部分）

```
data/
├── modeA/                          # 模式 A · 发现式梳理
│   ├── 输入 group1/                # 物项分级原则和物项清单（扫描 PDF）
│   ├── 预期输出 group1/            # E板块部分标准（仅「对象类」表，160 类）
│   ├── 输入 group2/                # 管道等级表（扫描 PDF）
│   └── 预期输出 group2/            # 管道管件模型（21 类 / 417 属性）
├── modeB/                          # 模式 B · 需求转化
│   ├── 输入/                       # 5 份业务人员填写的数据需求表（xlsx）
│   └── 预期输出/                   # EM4 模型（57 类 / 891 属性 / 46 属性字典）
├── templates/                      # D5 需求模板 + 字段基线（完备性对照用，非 D4）
├── taxonomy.json                   # D7 业务域分类
├── modeling_rules.json             # D4 数据标准建模规则（唯一规则源）
├── readiness_status.json           # D1–D8 就绪项静态定义
├── llm_config.json                 # 大模型配置（base_url / model / bulk_model / fallback）
├── llm_key.txt                     # 大模型 key（一行纯文本，优先级高于配置文件）
└── uploaded/                       # 现场上传区（见 1.9）
```

> **关于 A 的两组**：`group2` 只是开发期的第二份测试样本，**本质与 group1 相同**，用于验证"换一套数据也能跑通"。它**不进入 UI**，也不与 group1 并列；主界面每种模式只有一套「当前数据集」，跑完流程后归档，后续由独立的「历史梳理存档」入口查看。

**槽位决策（三态，不允许默认）：**
- `off`：本轮不参与（`POST /api/data/enable {enabled:false}`）。不显示详情、不提示上传、`validate_slot` 返回 `status=off`；必填项拒绝关闭。
- `pending`：参与但还没选来源 —— P1 解析**拦截并列出缺项**。
- `uploaded` / `builtin`：来源已定（上传文件，或 `POST /api/data/decision {choice:"builtin"}`；`POST /api/data/decision/all` 一键全内置）。

**必填 = 必须有可用数据**：只有 **D2、D4**（`b_in`、`model_spec`）——需求表与建模规则缺了生成不了标准。D1/D3/D5 为建议数据：D1 缺了 A 模式梳理没原料，D3 缺了 P7 无法评分，都不阻塞其他环节；`off` 不算缺项。槽位之间互不影响，可混用。

**硬规则（最高优先级）：**

1. **输入与预期输出成组绑定**，由 `backend/tools/dataset_registry.py` **唯一定义**（`(mode, group)` → 输入/预期输出），禁止再按旧 `dev/test` 路径拼。
2. 评测：生成侧**只读该组 `输入/`**，答案侧**只读同一组 `预期输出/`**；不得跨组、跨模式混用。
3. `预期输出/` 只用于评测，**不得**回流到生成链路 / 图谱 / RAG / 缓存 / few-shot。
4. 旧 `dev/test` 四段式已废弃；`raw/`、`full_mode/` 仅作历史追溯，不参与当前流程。

### 1.6 输入 / 预期输出 隔离与评测

**已废止 dev/test 四段式。** 现在按 **(mode, group) 成组绑定**：每组一个 `输入/`、一个 `预期输出/`（可选），由 `dataset_registry.py` 唯一定义。

**隔离约束（硬）：**

1. 生成侧**只读该组 `输入/`**；答案侧**只读同一组 `预期输出/`**；不得跨组、跨模式混用。
2. `预期输出/` 只用于评测，**不得**写入向量库、图谱、缓存、参考库或 few-shot 示例。
3. 同一批对象类在不同模式下角色不同（EM4 对 A 是答案、对 B 是建模集），所以**先看模式再谈隔离**，见 1.8。
4. 模式 B 的输入是 **1–N 份**需求表，**必须整批读**（`dataset_registry.load_tables` + 统一去重），不允许某个面板只读其中一份。
5. 数据变更后重跑交集校验，不能只校验"路径里有没有 test 字样"。

**评测口径：** `evaluate_against_test` 独立读数执行，读 `预期输出/`，输出报告前对原文片段脱敏；**P7 只有一个口径：评 P6 产物**（`pred_source="standard"`），不生成标准；没有产物时返回「请先在 P6 生成标准」。**D3 硬门槛**：`gold_slot` 的决策必须是 `uploaded` 或 `builtin`，否则 `evaluable=false / unevaluable_reason="no_gold_provided"`，指标全零、不写报告、不回落到内置答案。`test_input` 已从界面移除，只保留给离线脚本 `python -m tools.consistency_check` 做链路对账，**不得**再暴露成按钮（否则等于宣布“一个 P7 就够”，自毁 P1–P6 的流水线叙事）。同源性按**输入快照**判断：

- 标准 JSON 的 `scope.input_files` 记录生成时的输入文件清单；
- `evaluate(pred_source="standard")` 与当前数据集比对，输出 `pred_same_dataset`：`true`（同批输入）/ `false`（不同批，禁止当验收数字）/ `null`（旧产物无快照）；
- **同批输入时两个口径应基本一致**；若出现大差异，优先怀疑两条链路的类型/约束映射不一致（历史上出过：P5 读「数据格式」列而非「数据类型」列，导致 87 个数值字段被判成 String）。

### 1.7 防泄漏检查（阶段 1 硬门控）

`leakage_check` 检查五类泄漏：名称泄漏、定义泄漏（相似度 > 0.9）、文档泄漏、词表泄漏、RAG 库泄漏（存在 `source: test` 记录）。任一项命中，Harness 停下来报告具体命中项，由用户决定移出 dev、替换样本或书面确认风险。结果写入 `leakage_report.md`。

### 1.8 模式隔离（v3 新增 · 硬约束）

**背景（本项目真实踩坑）：** 阶段 -1 切分全量模型（当时的 D0）时未按模式区分，`dev_model` 装的是 EM4 对象类，而 EM4 正是**模式 A 的评测答案** —— 等于把考题答案建进了知识底座。实测重叠 34/34 = 100%。

**规则：**

1. **A / B 知识底座不共用节点**，两模式图谱节点集合交集必须为 0（实测已验证）。
2. 每个模式**只允许**读取下表左侧的建模源，右侧为**硬禁**（命中即抛错，不静默放行）：

| 模式 | 允许的建模源 | 禁止来源 | 禁止原因 |
|---|---|---|---|
| **A** | `modeA/dev/output`（E板块） | `modeA/test/output`、`modeB/dev/output`、`modeB/test/output`、`full_mode`、`dev_model` | EM4 这批对象类是**模式 A 的答案**（57/57 重叠） |
| **B** | `modeB/dev/output`（EM4） | `modeB/test/output`、`modeA/test/output`、`full_mode`、`dev_model` | 4 个测试类已剔除；其余 test 侧不得进建模库 |

3. 该映射**只在一处定义**（`backend/tools/mode_isolation.py`），所有工具必须经它取数并过 `assert_allowed(mode, source)`，不得各模块自己拼路径。
4. D2 缺失时的模拟库、参考库、推断依据、图谱、演化匹配，**全部按模式取**，不得一锅烩。
5. 现场上传的建模集若与**同模式** test 答案重合 ≥ 50%，必须显式告警（只比同模式，否则 EM4 对模式 B 合法却被误报）。

### 1.9 现场数据接入（比赛演示硬要求）

**原则：演示必须从"上传文件"开始，不得默认从某个固定路径读数据。**

- P0 提供**数据接入面板**，槽位与 D1–D8 一一对应，当前 9 个槽位：
  `a_in_g1` / `a_in_g2`（D1）、`b_in`（D2）、`a_gold_g1` / `a_gold_g2` / `b_gold`（D3）、`model_spec`（D4）、`req_template`（D5，可选）。
  D7 / D8 为未来项，暂不设槽位。
- **界面常驻只写编号（v3.3 硬规则）**：数据项标题只显示 `D1` / `D2` …，**不得**把具体文档名、组名（如"物项分级清单""A/group1"）写进常驻 UI —— 每次上传的文档都不同，内置数据只是示例。
- **每种模式只有一套「当前数据集」（v3.3 硬规则）**：未选模式时不显示任何数据项；选定后只显示该模式的 D1/D2/D3 + 共用 D4/D5，**同一编号只出现一次**。
  **不得**出现 A/B 并列，**也不得**出现"第 1 组 / 第 2 组"切换 —— A 的多组数据仅是开发期测试样本，本质相同，跑完流程即归档；后续由独立的「历史梳理存档」入口承载，不并列在主界面。
- 文件落盘到 `data/uploaded/<slot>/`，清单写 `data/uploaded/manifest.json`。
- **取数铁律（硬）：只看槽位决策，不做任何隐式回落。**
  `uploaded` → 上传文件；`builtin` → 内置文件；`off` / 未选择 → `active_files()` 返回空、`active_path()` 抛 `DataUnavailable`。
  各接口在开工前用 `_data_gate(*slots)` 检查，缺项直接 400（SSE 在开流前返回），前端把原因显示在面板上。
  **禁止**任何形如 `if not uploaded: 读内置目录` 的写法（历史 bug：D3 未提供也能拿内置答案出分）。
- 上传后立刻做**真实结构校验**（xlsx 查 sheet 与必需列并统计规模；PDF 用 pdfplumber 探文本层；JSON 查必需字段），**不得只看扩展名**。
- 上传后必须**清空各模块缓存**；上传建模集后**自动重建该模式图谱**，不清空上传也自动回到内置数据。
- 每个槽位校验结果并入 D1–D8 就绪面板（覆盖静态状态），SSE 流式广播。

### 1.10 D0 · 已有标准库（常驻底座，跨模式共用一份）

**定位**：D0 是「已经上线的存量标准」，**不属于 D1–D8**。D1–D8 是每轮可换的输入/答案，D0 是长期资产，上传一次反复复用。

| 维度 | 约定 |
|---|---|
| 归属 | **A / B 共用一份**（真实企业只有一个标准库）；读取时过 `mode_isolation` 校验 |
| 形态 | xlsx：`对象类` + `对象类属性`（列名与 D3 同构），可多份**追加** |
| 落盘 | `data/library/`：`uploaded/`（原始件，只增不改）、`merged.json`（索引）、`manifest.json`（版本/批次/文件名/时间）、`change_log.jsonl`（审计） |
| 去重键 | 对象类 = 归一化中文名（编码优先）；属性 = (类名, 属性名) |
| 合并策略 | 新键 → 追加；同键空值 → 补空；同键已有值不同 → 记 **conflict**，不静默覆盖 |
| 消费方 | P4 `novelty_check`、P5 `match_existing`、P6 `standard_evolve` —— `load_mock_library()` **只读 D0**；D0 为空则返回空库（`degraded=no_d0_library`），不再用参考底座顶替 |
| 与 D3 的边界 | D3 是评测答案，**不得**当库；上传 D0 时与 D3 类名做重合度校验，≥60% 记 `leak_warning`（疑似答案） |
| 回写 | 只有人**勾选「通过并入库」**并提交时才写；写前算 diff，写后 `manifest.version++` 且留 `change_log`；支持「清空还原为空库」 |

**工具**：`standard_library.load_library / status / ingest / commit_standard / reset`。
**接口**：`GET /api/library/status`、`POST /api/library/upload`、`POST /api/library/commit`、`POST /api/library/reset`。
**事件**：`library_updated`（入库后前端刷新计数，并清 P4/P5/P6 的库缓存）。

**验收**：① 上传后 P0 深色卡显示「N 类 / M 属性 / vX」；② P5「匹配参考库」文案变成 `D0 已有标准库（vX）`；③ 勾选「通过并入库」后再次匹配，命中项来自刚入库的条目；④ 点「清空」后 P4/P5 明确显示「未提供已有标准库（D0）」且不报错、不套用任何内置库。

## 2. 演示界面规格

技术选型（v3 修订 · 已落地形态）：**FastAPI + SSE 后端**，**Vue 3.5 单页前端（经典 `<script>`，无构建步骤）**，**Cytoscape.js** 图谱。图表用纯 CSS/SVG 自绘，不引入 ECharts。

> **依赖必须本地化**：Vue 与 Cytoscape 下载到 `frontend/vendor/`，**不得依赖 CDN / importmap / ESM**。理由：比赛现场网络与浏览器版本不可控，CDN 一断整个页面会退化成未编译的原始模板（`{{ }}` 裸露、`v-if` 失效、所有按钮失灵）。同时必须加 `v-cloak` 与启动失败兜底提示。

### 2.1 面板总览

| 面板 | 名称 | 模式 | 核心职责 |
|---|---|---|---|
| P0 | 模式选择 + 数据接入与系统总览 | 共享 | 双模式入口、**现场上传与校验**、逐槽位就绪状态、执行轨迹 |
| P1 | 文档解析工作台 | 共享 | 多格式解析、质量标记、门控入口 |
| P2 | 知识抽取审核台 | 共享 | 候选卡片、证据追溯、置信度门控 |
| P3 | 双知识底座视图 | 共享 | JSON 树 + OWL/图谱画布 + 语义推理查询 |
| P4 | 发现式梳理工作台 | 模式 A | 覆盖景观、新颖性检测、候选优先级、立项勾选 |
| P5 | 需求转化工作台 | 模式 B | 需求解析、完备性分析、澄清/推断、冲突消解、模型合成 |
| P6 | 标准生成与演化 | 共享 | 从 P4 立项候选或 P5 合成模型出发，生成标准、匹配演化 |
| P7 | 准确率评测与交付 | 共享 | **D7 逐行评测**（生成准确率 / 覆盖率 / 字段级 / 归因）、三维雷达图、导出成果包 |
| P8 | 人机门控中心 | 共享抽屉 | 汇总所有待处理门控项 |

### 2.2 P0 · 模式选择 + 数据接入与系统总览

**区 ① 模式选择**（置顶）：两张模式卡片（模式 A 发现式梳理 / 模式 B 需求转化）；选中即 `session/start`。此区不再放演示按钮与「刷新就绪校验」。

**区 ② 数据接入（现场上传）** —— 演示的第一步，规格见 1.9：

- 顶部进度条：`已确认 x / 8 个数据项` + 「待选择 / 采用内置 / 已上传」标记 + 缺失必需项红标。
- **全局忙碌条（长任务）**：sticky 深色条 + 旋转环 + 阶段标签 + 秒数（≥45s 提示"仍在调用大模型"）+ 流光进度 + **停止按钮**。
  停止 = `AbortController.abort()` 中断当前所有 fetch（`/api/events` 除外），并把各面板 busy 标志与提示复位；
  中断错误统一归一为「已停止（用户手动中断）」，不显示成"构建失败/解析失败"，并给 2.6s toast 回执。
  **注意**：服务端同步计算可能仍在收尾，停止后建议等 3–5 秒再重试（重复点会并发出第二个任务）。
- 每个槽位一行：槽位 ID、名称、模式标签（A / B / A·B）、必需标记、来源 chip（已上传 / 内置）、状态徽标（ready/degraded/missing，**标注来源是上传还是内置**）、已接入文件名、校验计数、issues 列表、泄漏告警红框。
- 操作：选择文件（多选槽位支持多选）、**上传并校验**、移除上传。顶部另有「重新校验全部」与「全部采用内置数据」。
- 上传成功后该槽位即接管对应环节的取数来源，并在执行轨迹流输出 `上传成功 / 图谱已重建 / 泄漏风险` 事件。

**区 ③ 执行轨迹流**：独占整行，宽度与上方模块框对齐；带 `[A]`/`[B]`/`[S]` 标签。原「数据就绪状态面板（D1–D8）」已移除，就绪状态改由区 ② 每个槽位行内的状态徽标与计数承载。

**验收锚点：** 上传一份建模集后，P3 显示的"实际取数路径"变成 `data/uploaded/<slot>/<file>` 且图谱自动重建；误传格式被拒绝；把某模式的答案传到该模式建模槽位会弹泄漏告警；点「清空上传」后槽位回到「待选择」，P3 直接报「数据未就绪」而不是读内置。

### 2.3 P1 · 文档解析工作台（共享）

**模式 A：** 批量解析（文档来自 P0 的 `d1_dev` 上传槽位，未上传则用内置示例），按文档分组，顶部"文档集概览"（文档数、页数、`ok/degraded/failed` 占比、格式分布）。
**模式 B：** 单份需求文件，额外高亮**模板结构识别结果**（章节标题、表格字段、复选框、空白待填项）。
**共享：** 三栏视图（原文/结构化文本/表格图表）、`parse_quality` 标签、`degraded/failed` 自动进入 P8。

**验收：** 模式 A 上传 2 种格式均产生非空输出且有 `degraded` 入队；模式 B 模板章节与空白字段被高亮。

### 2.4 P2 · 知识抽取审核台（共享）

**模式 A：** 广度抽取（对象类、属性、关系、取值域、计量单位、规则片段）；候选卡带**术语归一标签**；顶部**覆盖密度小图**，稀疏章节标黄。
**模式 B：** 目标导向抽取；候选卡带**"是否在需求文件中被提及"**标记。本环节只回答「抽出了什么 / 有多确定 / 证据在哪」，**不做任何与标准库或模板的比对**（2026-10-23 移除原「需求—候选对齐视图」与其「已覆盖/部分覆盖/未覆盖」三态：其基线来自 `data/templates/complete-model-template.json`，是 D0 管道管件子树的旧快照，与 D4「建模规则」的定义不符，且以旧域字段要求新域需求属误判）。
**共享：** 候选卡（名称、类型、定义、`confidence`、`evidence_span` 原文高亮）、筛选器、`confidence < 0.7` 置顶标红并自动进 P8 门控。

**大模型增强（`knowledge_extractor._llm_enrich_a`，可选、可降级）：**
- **术语归一**：D6 词表已删除，`term_normalize` 一律返回 `method: "none"`；模型只在名称写法明显不一致时给建议名（`method: "llm"`，带 `reason`），置信度 ≥ 0.7 才改写入库名。
- **定义补写**：原先只有「（参考库命中）」占位的候选，由模型补一句话定义，标 `llm_definition`，`evidence_span.basis` 追加"定义由 {模型} 补写（置信 x.xx）"。
- **正文抽取**：文档有文本层时，分块喂模型抽候选，**每条必须带 `evidence_span`（逐字原文摘录），无证据一律丢弃**（`dropped_no_evidence` 会回报条数）。
- **扫描件不硬抽**：文本层 < 40 字符时跳过，写 `llm.skipped_extract` 如实说明，不得编造正文。
- 三项任务**并发**（串行 231s → 并发约 70–100s）；结果进缓存，重复点击秒回。

**准确率口径：** 本页**不再有「刷新准确率抽样（D3）」按钮与抽样面板**（2026-10-23 移除：其数据源 `data/annotations` 已删，接口只回报 D3 是否就绪、三项准确率恒为 null，属误导）。逐行准确率统一在 P7 运行评测得出（口径见 1.3.4）。**禁止**用模型自评分数冒充。

**验收：** 模式 A 展示术语归一与覆盖密度图，至少一条低置信度候选经人工处置并进 P8；模式 B 展示对象类 + 属性候选，个数与 P5/P7 口径一致，无任何"已覆盖/未覆盖"判定；有模型时至少看到「AI 定义」标记与 `llm` 元信息（模型名 / 条数 / 耗时）。

### 2.5 P3 · 双知识底座视图（**A / B 各一套，不共用**）

左半区 JSON 树（对象类→属性→约束/量纲，结构完整性校验）；右半区 Cytoscape.js 图谱（三种布局，默认 concentric —— 大图跑 cose 会明显卡顿，节点点击弹 OWL 属性面板）；语义推理查询框（调 `graph_query`：实体匹配 → 多跳路径传播，高亮子图并流式返回推理链与 `paths`）；图谱统计面板。

**底座来源（v3.3 修订 —— 与评测答案解耦）：** 底座**唯一**入口是 `backend/tools/base_supply.py`，只装配 **D0 已有标准库 + 本轮输入**（A：D1 / B：D2），**不得**读 D3 预期输出。
理由：原来底座取自 D3（评测答案），模型等于"拿答案建库、再由答案打分"，是循环论证；评委一问就穿。
出口带 `supply` 块（D0 数量 / 输入数量 / 来源分布 / 政策原文），面板照此展示，另经 `ob._assert_no_test_source()` 防泄漏校验。

**图谱规模参考线（v3 修订 —— 三维度，A / B 分开；是"有没有空转"的参考线，不代表正确性）：**

| 模式 | 对象类 | 属性 | 关系 | 实测（D0 + 本轮输入） |
|---|---|---|---|---|
| **A** | ≥ 100 | ≥ 100 | ≥ 60 | 357 / 23944 / 23944 ✔（D1 两份 PDF 为扫描件，输入供给 0）|
| **B** | ≥ 20 | ≥ 200 | ≥ 500 | 420 / 24795 / 24795 ✔（D0 357 类 + D2 67 类）|

- 指标口径与 D2 的规模下限一致（对象类 / 属性 / 关系），**不再用"图谱节点数"**。
- 统计面板必须显示「底座来源（人类可读）+ 实际取数路径（相对路径，不暴露本机盘符）+ 上传/内置标记 + 三维度阈值 ✔/✘」。
- **数据残缺必须显式标注**：D0 类表未给父类列，`357/357 个对象类无父类` → A 模式建不出 `subClassOf`；属性和类的数量是 D0 存量库的真实规模，面板要注明"规模来自 D0"，不得用"节点数"糊过去。
- 画布是**子图视图**：全量 2.4 万节点直推 Cytoscape 会卡死，`/api/graph/data` 只渲染「域 + 全部对象类 + 每类前 3 个属性 + 候选」，返回 `view{shown,total,truncated}`，前端照实标注「画布展示 X / 全量 Y」；统计 / OWL / 查询仍用全量。

**双模式差异：** 模式 A 底座按置信度着色；模式 B 底座显示"需求相关性"热力（相关性需分层：类命中且字段命中 1.0 / 仅字段命中 0.6 / 仅类命中 0.35，否则会出现 77% 节点全红、毫无区分度）。

**空态纪律：** P3 **不得**在页面加载时自动载入历史图谱（`onMounted` 不调 `loadGraphData`）。未构建时画布显示「尚未构建知识底座」，只有用户点「构建双知识底座」后才出现节点。

**验收：** 两模式三维度均达标且节点集合交集为 0；一次语义查询返回高亮子图与可追溯推理链；两种模式着色/热力可见。

### 2.6 P4 · 发现式梳理工作台（模式 A 专属）

**上半区 · 候选标准全景图：** 业务域 × 文档章节矩阵热力图，单元格颜色表示候选数量与质量；点击单元格列出候选卡。候选卡带**新颖性四色标签**：🟢 `已覆盖`、🔵 `语义相近可复用`、🟠 `口径冲突`、🔴 `全新`；显示置信度、证据强度、业务域、优先级（`prioritize_candidates` 计算）。

**下半区 · 立项决策区：** 勾选候选 → 加入立项清单；清单显示数量、新颖性分布、工作量估算；按钮"对立项清单生成标准草案"→ P6。

**侧边 · 覆盖盲区提示：** 抽取密度低于阈值的单元格列表。

**交互：** 按新颖性/业务域/置信度筛选；点击 `口径冲突` 候选弹出与冲突标准的字段级 diff。

**`similar` 判定纪律（v3.3 修订）：** 判定改用**本地向量余弦**，阈值 `landscape_analyst.VECTOR_THRESHOLD = 0.35`（噪声底约 0.15）。
原来是 Dice 字符二元组 0.62 —— 那是字面重叠，同义/近义一律 0 分，等于"换个说法就查不到"。
**不得为了凑齐四色而调低阈值**：实测「弯头」在 D0 里没有对应词汇，最高只到 0.17，因此该查询下 `similar` 为 0，只出 `novel / covered / conflict` 三种标签——这满足"至少三种"的验收要求，是诚实结果，不许手动放水。
低于阈值的最近邻只作「参考提示」（`nearest_hint` 带余弦值），不算命中。

**大模型增强（`landscape_analyst._llm_enrich_p4`，可选、可降级）：**
- **新颖性语义判断**：规则只能算字符串相似度（"像不像"），模型判"业务上是不是同一件事"，输出 `可复用 / 需合并 / 确为新增` + **命中的现有条目 `matched`** + `reason`。
- **立项优先级**：`priority 1–5` + `score 0–100` + 理由（结合覆盖盲区）。
- 两个任务**并行**发出；只判前 8 条（15 条 + 200 条现有条目实测要 5 分钟），其余保留规则结论，界面注明"仅前 N 条经模型判断"。
- **模型结论不覆盖规则结论**：两者并存，`llm_agrees_rule=false` 时界面标「与规则不一致」，供人工复核。

**`conflict` 的 diff 必须真实：** 应来自"同一属性在不同对象类下口径不一致"这类**真实数据冲突**；不得凭空构造 diff。若某模式建模集本身没有这类冲突，`conflict` 就应为 0，改由另一模式承载该演示。

**验收：** 全景热力图可见；至少三种新颖性标签；勾选 2–3 个候选；至少一条盲区提示；点击冲突候选看到 diff；有模型时至少 5 条候选带「AI：判定（命中 X）」与「AI 优先级 N」。

### 2.7 P5 · 需求转化工作台（模式 B 专属）

四阶段从左到右流水线布局。

**区 1 · 需求解析结果：** 左侧原文件缩略图，右侧结构化结果（目标对象类、用途范围、预期属性列表含空缺标记、约束、每字段来源页/段）；顶部**需求完备性评分**。

**区 2 · 完备性分析与澄清/推断：** 表格（字段 × 当前状态/分析结果/建议动作）；状态 `已填/缺失/模糊/冲突`；对缺失和模糊项提供**生成澄清问题**与**AI 自动推断**（带置信度和依据）；业务方逐项选择"采纳推断/回答问题/标记待定"，**未处理项不得进入合成**。

**大模型补写（`requirement_analyst.generate_clarification`）：** 对缺失字段由模型补 `definition / unit / data_type`，写进 `inferences[].llm_*`，`basis` 注明"由大模型 {模型} 补写"；置信 < 0.6 标「仅作参考」，可逐项驳回。无模型时纯规则推断，界面显示「未配置（走规则兜底）」，不得假装调过。

**区 3 · 已有标准匹配：** 调 `rag_vector` + `rag_graph`，显示相似度、匹配对象、判定标签（可复用/需合并/确为新增）、图谱校验结果；"需合并"展示合并预览。

**区 4 · 模型合成与冲突消解：** 合成预览（左"需求意图"右"合成模型"，每字段标来源）；冲突消解面板（冲突描述、消解建议、理由、接受/替代/转专家）；最终模型预览全字段 `COMPLETE` 才允许导出；按钮"确认并生成最终数据模型"→ P7。

**验收：** 上传测试需求文件；完备性评分出现；至少一个缺失项经 AI 推断补齐（显示置信度和依据）；至少一个冲突项被处理；最终模型全 `COMPLETE`；导出成功。

### 2.8 P6 · 标准生成与演化（共享，两模式汇聚）

模式 A 入口：从 P4 立项清单批量生成草案；模式 B 入口：从 P5 合成模型生成正式条目。共享：可编辑标准表格、`INCOMPLETE` 补全、标准可使用率实时估算、演化匹配、合并人工门控。差异：A 标注"源自发现式梳理，需业务确认立项"，B 标注"源自需求转化，已通过完备性校验"。

**完整模型表格导出：** P6 不止产出"标准条目"，两条入口都可直接下载 **xlsx 完整数据模型表格**（`GET /api/standard/export?mode=A|B[&standard_id=...]`）：

- 四张表：`对象类`（13 列，与 D3 同构）/ `对象类属性`（24 列，与 D3 同构）/ `关系` / `说明`；
- **B**：对象类取 P5 `scope.object_classes`（当前数据集 **67 类**，其中 50 类有属性行），属性取 `field_specs`（**855 条**），关系取 P5 `relations`；标准按 `spec.class_name` 分组落表；
- **A**：候选按 `type` 分流 —— `object_class` 进对象类表，其余进属性表，关系按 `hasAttribute` 生成；
- **临时 ID 必须声明**：`OC-` / `AT-` / `RL-` 前缀是导出编号，**不是 EDM 正式编码**；`说明` 页与备注列必须写明，导入时重新分配；
- `INCOMPLETE` 字段照实保留、不补默认值，状态列标「草稿·待补全」。

**大模型补全（`standard_generator.enrich_with_llm` / `POST /api/standard/enrich`）：**
- 只填空值（`definition` / `unit`），**不覆盖业务已填**；补完重算质量分与可使用率并落盘。
- 12 条/批、4 并发（`qwen3.8-flash` 实测 12 条约 28s）；单批超时 180s，失败不中断其余批次，错误写 `std.llm.errors`。
- 全量补全走 CLI：`backend` 目录下 `python -m tools.enrich_all`（855 条约 8–13 分钟）。
- 两档口径都要显示：**完全合规率**（名称+类型+约束）与**结构可入库率**（名称+类型），并列出缺口（约束/定义/单位/编码各缺多少），防止一个 0.1% 掩盖真实情况。

**验收：** 两条路径各生成至少一个标准；出现 `INCOMPLETE` 与人工补全；完成一次演化匹配闭环；至少一次合并建议触发人工确认；**两条路径各能下载一份完整模型表格**。

### 2.9 P7 · 准确率评测与交付（共享）

**已落地形态（增量 7 部分）：**

- 顶部模式切换（A / B）+ **组切换**（A：group1 / group2；B：default）+「运行评测」「载入上次结果」+ 下载报告 / 明细 CSV；
- 三层指标卡：**对象类层 / 属性层 / 合计**，每层含「生成准确率 + 覆盖率」；
- 判定分布 chip（合并两层，exact / partial / missing / extra）可点击筛选；
- 字段级表、错误归因表、分对象类表、逐行明细表（含 `matched_by` / `similarity` / `diff_fields`）；
- 「口径与限制」折叠区展示 notes（作用域、匹配降级、未启用向量项等）。

其后段仍待补：三维雷达图、低分条目（含扣分原因）、人工复核清单、ZIP 导出（按模式分目录）、**硬指标验收面板**。

验收面板按模式分组（模式 A 组、模式 B 组、共享组），**每项指标旁标注"数据依据"**，例如"属性生成准确率 ← D7（modeA/test/output，逐行比对）"。测试模式下的指标数字必须来自 `deliverables/evaluation/test_report.md`，不得用建模集表现冒充。

**三维质量中的 accuracy：**
- 按 1.3.4 的 D7 口径计算，同屏展示**生成准确率**（分母 = 生成行数）与**标准覆盖率**（分母 = D7 行数），外加字段级准确率；标注依据为「D7 逐行比对」，**不得显示"未验证"**。
- 错因归类为漏抽 / 多抽 / 名称不一致 / 类型错 / 约束错 / 单位错 / 编码错 / 粒度错。
- 仅当 D3 预期输出缺失时才允许显示"未验证"，且**禁止**用模型自评分数填坑。

#### 2.9.1 历史梳理存档（v3.4 新增）

**入口：** 顶栏右侧「历史存档 · N」（全局，任何面板可开）+ P7 底部「归档本次梳理」。

**归档内容：** `deliverables/archive/{mode}-{YYYYMMDD-HHMMSS}/`
`meta.json`（模式、时间、输入/预期输出文件哈希、生成规模、评测指标、D4 规则版本、状态）+ `input/` + `gold/` + `output/`（三表 xlsx）+ `report.md` + `match_detail.csv`。

**状态：** `complete`（已评分）/ `unevaluable`（未评分，如无 D3 或输入不可解析）/ `generated`（只生成未评测）。

**边界（硬规则）：**

- 只读：归档写入后，任何生成 / 评测链路都不再从归档读数据；
- 不进图谱 / RAG / 缓存，不改变 P0–P7 的取数优先级（上传 > 内置）；
- 唯一回到主流程的通道是**显式「恢复为当前数据集」**（复制进上传槽位），原归档不变；
- 归档里的 `gold/` 只用于回看与恢复，不参与生成。

**界面：** 左列记录（模式 · 时间 / 编号 / 规模 / 状态），右侧详情（对象类层 / 属性层 / 合计三层指标、输入与预期输出、规则版本、产物下载、恢复与删除）。归档列表按时间倒序，可按模式筛选。

### 2.10 P8 · 人机门控中心（全局抽屉）

门控项带模式标签和来源面板。类型：共享（低置信度、`INCOMPLETE`、合并建议、解析失败、质量低分）、模式 A（`novelty_conflict`）、模式 B（`requirement_gap`、`model_conflict`）。操作：确认/拒绝/转专家，写入日志，P7 可查。

## 3. Harness 编排层

### 3.1 启动交互契约（最高优先级）

主编排 Agent 的 system prompt **必须**包含：

> 会话开始时，你的第一个动作是输出《数据准备清单》（模板见 SKILL 1.2），写入 `data/DATA_REQUEST.md`，然后暂停等待用户回复。在用户回复"数据已就绪"或"仅准备模式 X 数据"之前，不得读取 `data/` 下任何业务文件，不得开始任何开发动作。收到回复后执行 `data_readiness_check`，对缺失项逐一询问用户选择补充/降级/单模式，全部确认后才进入数据接入与防泄漏检查（**按模式隔离**，见 1.8）。

### 3.2 主编排 Agent 职责

根据模式选择加载编排路径，两路径共享子 Agent 与工具。

```
模式 A：doc-parser → knowledge-extractor → ontology-builder
        → landscape-analyst → [立项门控]
        → standard-generator → standard-evolver → quality-auditor

模式 B：doc-parser（需求文件）→ requirement-analyst
        → [澄清门控] → standard-evolver（已有标准匹配）
        → model-synthesizer → [冲突消解门控] → quality-auditor

两条路径的前置（v3）：upload_manager（现场接入）→ mode_isolation.assert_allowed（取数许可）
```

**硬规则：** 未通过门控不得进入下一阶段；状态机显式阻止跳步。

**v3 补充：** 两条路径各自持有**独立**的知识底座、参考库与模拟库（不共用节点）。任何数据进入参考库 / 图谱 / RAG 之前，必须过 `mode_isolation.assert_allowed(mode, source)`；数据来源走 `upload_manager.active_path()`（上传优先）。

### 3.3 子 Agent 清单

| 角色 ID | 模式 | 职责 | 绑定工具 |
|---|---|---|---|
| `doc-parser` | 共享 | 多格式解析与版面恢复 | `doc_parse`、`file_read`、`human-gate` |
| `knowledge-extractor` | 共享 | 对象类/属性/关系/取值域抽取；术语归一 | `llm_extract`、`schema_validate`、`term_normalize` |
| `ontology-builder` | 共享 | JSON→OWL+图谱 | `json_to_owl`、`graph_upsert`、`graph_query` |
| `landscape-analyst` | A | 新颖性检测、覆盖景观、优先级 | `novelty_check`、`landscape_map`、`prioritize_candidates`、`rag_vector`、`rag_graph` |
| `requirement-analyst` | B | 需求解析、完备性、澄清/推断 | `parse_requirement`、`gap_analyze`、`generate_clarification`、`rag_vector`、`rag_graph` |
| `model-synthesizer` | B | 模型合成、冲突消解 | `synthesize_model`、`resolve_conflict`、`rag_graph` |
| `standard-generator` | 共享 | 标准草案生成 | `rag_vector`、`rag_graph`、`standard_draft` |
| `standard-evolver` | 共享 | 匹配、去重、演化建议 | `rag_vector`、`rag_graph`、`diff_compare` |
| `quality-auditor` | 共享 | 三维质量评价 | `quality_score`、`rule_check` |

所有子 Agent 绑定 `emit_ui_event`。

### 3.4 角色文件模板（以 `landscape-analyst` 为例）

放入 `.dsh/roles/landscape-analyst.md`：

```yaml
---
provider: deepseek
model: deepseek-v4
reasoningEffort: medium
tools: [novelty_check, landscape_map, prioritize_candidates, rag_vector, rag_graph, emit_ui_event, human-gate]
maxToolCalls: 20
---
你是核电数据标准发现式梳理分析师，只做候选评估与全景构建，不生成最终标准。
你必须：
1. 每个候选的新颖性标签必须来自 novelty_check 的真实返回，不得凭经验编造。
2. 口径冲突必须返回字段级 diff；无 diff 的冲突判定视为无效。
3. 覆盖盲区必须基于配置的密度阈值计算，阈值变更需记录。
4. 任何时候不得跳过 novelty_check 直接输出 novel 标签。
5. 检索时不得使用 source 为 `test/output`（含跨模式的他方 dev/output）的任何内容。
```

其余角色按同模式编写，正文写清“做什么、不做什么、遇到什么必须停”。

## 4. 后端 API 契约

| 端点 | 方法 | 模式 | 用途 | 流式 |
|---|---|---|---|---|
| `/api/data/request` | GET | 共享 | 获取《数据准备清单》 | 否 |
| `/api/data/status` | GET | 共享 | 就绪快照（静态 + 上传槽位实时校验） | 否 |
| `/api/data/readiness` | POST | 共享 | 触发就绪校验 | SSE |
| `/api/data/upload` | POST | 共享 | 现场上传（multipart：slot + files） | 否 |
| `/api/data/validate` | POST | 共享 | 逐槽位结构校验 | SSE |
| `/api/data/upload/status` | GET | 共享 | 各槽位状态与校验结果 | 否 |
| `/api/data/upload/reset` | POST | 共享 | 清空上传（回到「待选择」，不自动改用内置） | 否 |
| `/api/data/degrade` | POST | 共享 | 提交降级决策 | 否 |
| `/api/session/start` | POST | 共享 | 创建会话，参数含 `mode` | 否 |
| `/api/pipeline/run` | POST | 共享 | 按模式触发流水线 | SSE |
| `/api/parse` | POST | 共享 | 文档解析（A 批量 / B 单份） | SSE |
| `/api/extract/review` | POST | 共享 | 候选确认/丢弃 | 否 |
| `/api/graph/build` | POST | 共享（按 mode 分别构建） | 构建该模式底座 → OWL + 图谱 | SSE |
| `/api/graph/data` | GET | 共享（?mode=A/B） | 图谱画布数据 | 否 |
| `/api/graph/stats` | GET | 共享（?mode=A/B） | 三维度统计 + 阈值对照 + 来源 | 否 |
| `/api/graph/owl` | GET | 共享（?mode=A/B） | OWL Turtle 全文 + 剖面校验 | 否 |
| `/api/graph/node/{id}` | GET | 共享（?mode=A/B） | 节点详情（OWL 属性面板） | 否 |
| `/api/graph/query` | POST | 共享（按 mode） | 语义推理查询 | SSE |
| `/api/landscape/build` | POST | A | 构建候选全景图 | SSE |
| `/api/landscape/prioritize` | POST | A | 计算优先级 | 否 |
| `/api/landscape/approve` | POST | A | 提交立项清单 | 否 |
| `/api/requirement/parse` | POST | B | 需求文件解析 | SSE |
| `/api/requirement/gap` | POST | B | 完备性分析 | SSE |
| `/api/requirement/clarify` | POST | B | 提交澄清/推断决策 | 否 |
| `/api/requirement/match` | POST | B | 已有标准匹配 | SSE |
| `/api/requirement/synthesize` | POST | B | 模型合成 | SSE |
| `/api/requirement/conflict/resolve` | POST | B | 提交冲突消解决策 | 否 |
| `/api/standard/generate` | POST | 共享 | 生成标准草案 | SSE |
| `/api/standard/complete` | POST | 共享 | 补全 INCOMPLETE | 否 |
| `/api/evolve/analyze` | POST | 共享 | 演化匹配 | SSE |
| `/api/evolve/commit` | POST | 共享 | 确认演化决策 | 否 |
| `/api/quality/evaluate` | POST | 共享 | 质量评价 | SSE |
| `/api/eval/run` | POST | 共享 | 对 P6 产物执行评测（`pred_source=standard`；无产物则要求先去 P6） | 否 |
| `/api/export` | GET | 共享 | 导出成果包 | 否 |
| `/api/gates` | GET | 共享 | 门控队列 | 否 |
| `/api/llm/status` | GET | 共享 | 模型是否可用 / 模型名 / key 末四位 / 失败原因 | 否 |
| `/api/llm/selftest` | POST | 共享 | 一次极小的真实调用，现场证明"不是规则引擎冒充" | 否 |
| `/api/standard/enrich` | POST | 共享 | P6 用大模型补 definition/unit（12 条/批、4 并发） | 否 |
| `/api/library/status` | GET | 共享 | D0 已有标准库状态（来源/类数/属性数/版本） | 否 |
| `/api/library/upload` | POST | 共享 | 上传/追加 D0（xlsx，含重合度校验） | 否 |
| `/api/library/commit` | POST | 共享 | 已审核标准写回 D0（仅在勾选「通过并入库」时调用） | 否 |
| `/api/library/reset` | POST | 共享 | 清空 D0（P4/P5 显示「未提供已有标准库」，不回落参考底座） | 否 |

### 4.1 SSE 事件格式

```
event: data_request
data: {"file":"data/DATA_REQUEST.md","items":["D1","D2","D3","D4","D5"]}

event: readiness_result
data: {"item":"D2","status":"ready","count":"5 份 / 895 条属性行"}
data: {"item":"D3","status":"missing","impact":"只生成不评分（D3 可选）"}

event: novelty_result
data: {"candidate_id":"c-042","status":"conflict","matched_standard":"S-1023","diff":[{"field":"pressure_rating","requirement":"Class 300","existing":"Class 150"}]}

event: requirement_gap
data: {"field":"pressure_rating","status":"missing","suggestion":{"type":"inference","value":"Class 150","confidence":0.82,"basis":"rag_graph: 同类阀门标准 S-1023"}}

event: gate_required
data: {"gate_id":"g-012","mode":"B","reason":"requirement_gap","item":{"field":"pressure_rating","status":"missing"}}

event: metric_update
data: {"metric":"attribute_accuracy","value":0.83,"source":"D0-test"}

event: stage_complete
data: {"stage":"parse","mode":"A","summary":{"parsed":3,"degraded":1,"failed":0}}

// v3 新增 · 现场上传与校验
event: upload_validate_result
data: {"slot":"d0_a","label":"模式 A 建模集","status":"ready","count":"162 类 / 120 属性实例","source":"uploaded","issues":["未提供关系表"],"leak_warning":null}

event: upload_validate_complete
data: {"missing_required":[],"uploaded_count":4}

// 大模型工作提示（P2/P4 各一条；前端据此显示"大模型处理中…"，避免干等）
event: llm_working
data: {"panel":"P4","model":"qwen3.8-flash","label":"大模型：新颖性判断 + 立项优先级（约 60–120 秒）"}
```

## 5. 工具契约

### 5.1 共享工具

**`doc_parse`**：输入 `file_path`、`format_hint`；输出 `{pages:[{text_blocks,tables,figures,parse_quality}]}`。支持 ≥ 3 格式；`degraded/failed` 显式标记。

**`llm_extract`**（实现：`llm_tasks.extract_candidates`）：输入 `chunks`、`task`、`domain_vocab_ref`；输出 `{candidates:[{name,definition,evidence_span,confidence}]}`。`evidence_span` 必填，**缺证据的候选必须丢弃并计数**（`dropped_no_evidence`）；`confidence < 0.7` 不得自动进入下一阶段。

**`term_normalize`**（实现：`llm_tasks.normalize_terms`，词表命中走规则）：输入 `terms`、`domain_vocab_ref`；输出 `{normalized:[{original,canonical,method,confidence}]}`。方法必须标注（`exact/substring/llm/none`）；`confidence < 0.6` 不得自动应用。

**`json_to_owl`**：输入 `json_knowledge`；输出 `{owl_turtle,graph_nodes,graph_edges}`。须通过 OWL 2 DL 检查。

**`rag_vector` / `rag_graph`**（v3.3 已换成真实现，不再是 Dice / 同名查表）：

| 工具 | 实现 | 说明 |
|---|---|---|
| `rag_vector` | `local_embedding.library_index(mode).best(name)` | 字 n-gram TF-IDF → 随机投影 1024 维（多哈希抑制碰撞）→ L2 归一化 → 余弦。**精确同名直接 1.0**（索引文本含定义与所属类，纯余弦会被稀释：`备注` vs `备注` 只有 0.28） |
| `rag_graph` | `ontology_builder.graph_query(q, mode, topk, hop=2)` | 稀疏 TF-IDF 倒排（24k 属性节点不走稠密编码，太慢）+ 对象类向量召回，取 max 作实体分；再从种子做**按边类型加权的多跳 BFS**（`HOP_DECAY 0.55`，正向子类 0.95 / 归属 0.85，反向 0.8 / 0.45），输出每个召回节点的 `hop`、`score`、`path_labels`、`via` |

- 必须协同调用：先 `rag_vector` 粗召回，再 `rag_graph` 细校验（同名是否真在同一类 / 口径是否一致）。
- **诚实标注**：这是本地随机投影嵌入（LSA 家族近似），**不是**预训练语言模型；`retrieval_impl` 字段照此返回，文案不得写"预训练 embedding"。
- 路径展示：同名不同节点（候选 / 字段 / 对象类）会在标签后加类型后缀；多条路径按 `(跳数, 边序列)` 限流，避免 12 条全是同一种 1 跳反向边。
- 「是否已在标准库」**只能由 D0 存量库（`lib["field_index"]`）回答**：图谱是 D0 + 本轮输入，同名节点可能就来自本轮需求表自己，拿图谱判"可复用"等于自证。

**`standard_draft`**：输出含 `object_class_name`、`definition`、`attribute_list`、`field_specs`、`mapping_suggestions`；`field_specs` 的字段键与编码必须符合 **D4**（`modeling_rules.json`），缺任一必填字段标 `INCOMPLETE`；D4 缺失时阻断导出并标 `rules_source: "missing"`。

**`quality_score`**：输出 `{completeness,normativity,accuracy,manual_review_required}`；任一维 < 70 触发门控。

**`emit_ui_event`**：仅推送 UI 事件，不改业务状态。

**`human-gate`**：输出 `{decision:"confirm"|"reject"|"escalate",note}`。低置信度、冲突、`INCOMPLETE` 必须经此，不得绕过。

### 5.2 数据阶段工具

**`emit_data_request`**：生成《数据准备清单》（Markdown + JSON），写入 `data/DATA_REQUEST.md`，返回清单结构供 UI 渲染。

**`data_readiness_check`**：对 D1–D8 逐项校验，输出 `{item,status,details,impact}` 数组，写入 `readiness-report.md`。

**`split_verify`（替代已废弃的 `split_model`）**：数据由用户按模式放入 `mode{A,B}/输入|预期输出`，不再自动切分。本工具只做校验：同一 (mode, group) 内输入与预期输出的对象类不得同源重叠、属性表无孤儿、隔离剔除项确已移除，输出 `test_isolation.json`。**校验不通过必须停下来报告，不得静默继续。**

**`leakage_check`**：五类泄漏检查 + **跨模式泄漏检查**（见 1.8），输出 `leakage_report.md`。

**`mode_isolation`（v3 新增 · 共享硬工具）**：`assert_allowed(mode, source)` —— 任何数据进入参考库 / 图谱 / RAG / 模拟库之前必须过此校验；`assert_metrics(mode, counts)` —— 三维度阈值对照。模式→数据源映射**只允许在此一处定义**。

**`upload_manager`（v3 新增 · 共享硬工具）**：槽位落盘、`validate_slot()` 真实结构校验、`active_path()` 上传优先取数、`invalidate_caches()`。

**`text_norm`（2026-10-23 新增 · 共享硬工具）**：两级名称归一，所有读取/评测/导出必须共用，不得各写一套。
- `canonical_name(s)`：只抹平排版差异（全角括号→半角、首尾空白、连续空格），用于**落库展示名**；在 `dataset_registry.parse_*` 入口统一施加，因此 P2 候选 / P5 合成 / P6 导出 / P7 评测看到的是同一个字符串。
- `norm_key(s)`：再做「抹掉括号内容 + 去空白 + 转小写」，**只用于查重/匹配**，不得用于展示。
- 事故背景：同一对象类在「对象类」表写 `空心型钢（方、圆、矩形钢）`、在「对象类属性」表写 `空心型钢(方、圆、矩形钢)`，被当成两个类，`consistency_check` 对账差 17 行。修复后 199/199 完全一致。

**`evaluate_against_test`（D7 评测）**：读取**该模式的 `test/output/`（D7）与 `test/input/`**，按 1.3.4 口径执行：三级对齐（编码 / 归一名称 / 语义，匈牙利一对一）→ 四类判定（exact / partial / missing / extra）→ 行级严格 / 宽松 + 字段级 + micro / macro + 分层指标 → 错误归因。输出 `deliverables/evaluation/test_report.md` 与逐行匹配明细 `match_detail.csv`（含 `matched_by`、`similarity`、`judgement`，供审计）。**该工具在独立测试进程运行，不得将 test/output 内容写入智能体可访问存储。**

### 5.3 模式 A 工具

**`novelty_check`**：输入 `candidate`；输出 `{status:"covered"|"similar"|"conflict"|"novel",matched_standard_id,similarity,diff?}`。`conflict` 必附字段级 `diff`；`similar` 必附映射建议；检索源只能是**本模式** dev/output（见 1.8）。

**`landscape_map`**：输入 `extraction_results`、`document_metadata`、`business_domain`；输出 `{matrix:[{domain,section,candidate_count,avg_confidence,candidates[]}],blind_spots:[{domain,section,reason}]}`。盲区基于配置阈值。

**`prioritize_candidates`**：输入 `candidates`；输出 `[{candidate_id,priority,rationale}]`。每项必须给 rationale。

### 5.4 模式 B 工具

**`knowledge_extractor`（模式 B · 2026-10-23 修正）**：必须走 `requirement_analyst.load_requirement_dataset()` 取数（多份 D2 合并 + 按 `(对象类, 属性)` 去重，与 P5/P7 同一份），产出 **对象类候选 + 属性候选**两类。历史问题：曾只读 `sorted(active_files("b_in"))[0]` 单份、且只产出对象类，导致 P2 显示 17 类 / 0 属性，而 P5/P7 是 67 类 / 855 属性。**红线：不得在 P2 里再写第二套需求表解析。** 模式 B 不调用大模型（候选来自结构化字段），接口也不得发 `llm_working`。
**红线（2026-10-23 二次收敛）**：模式 B 的返回体**不再含** `novelty` / `novelty_dist` / `alignment` / `alignment_summary` / `d4_field_total`，也**不得**在 P2 读 D0 标准库或 D4 字段基线做任何比对（比对属 P4 新颖性 / P5 已有标准匹配）。返回键收敛为：`document / basis / basis_note / candidates / term_norm / low_confidence_count / type_dist`。

**`parse_requirement`**：输出 `{target_object,purpose,scope,expected_attributes:[{name,expected_type?,expected_constraint?,source_span,status}],constraints,ambiguities,source_spans}`。每字段附 `source_span`；空缺显式标 `missing`。

**`gap_analyze`**：输入 `structured_requirement`、`complete_model_template`；输出 `{completeness_score,gaps:[{field,status,current_value?,reason}]}`。评分公式可审计；`conflicting` 附冲突描述。

**`generate_clarification`**：输出 `{questions:[{field,question,options?}],inferences:[{field,inferred_value,confidence,basis}]}`。推断必附 `basis`；`confidence < 0.6` 仅作参考。

**`synthesize_model`**：输出 `{object_class,attributes:[{name,definition,data_type,constraint,unit,source}],relations,field_completeness,missing_fields,rules_source}`。每字段标 `source`；字段键、编码与条件必填必须符合 **D4**（`rules_source: "D4"`），D4 缺失时阻断导出（`rules_source: "missing"`）；`complete` 才允许导出。

**`resolve_conflict`**：输出 `{recommended_action,rationale,alternatives}`。`manual` 触发门控；决策写入冲突日志。

### 5.5 大模型接入（`llm_client` / `llm_tasks`）

**定位：只做"必须理解/生成自然语言"的四件事，其余一律规则。** 规则能做的（对齐、计分、ID 生成、去重、阈值判定）**不许**交给模型——不可复现、不可审计。

| 环节 | 工具函数 | 模型任务 | 降级 |
|---|---|---|---|
| P2 | `llm_tasks.normalize_terms` | 术语归一（词表已删，仅按写法差异纠偏） | 保留原词，`method: "none"` |
| P2 | `llm_client.suggest_definitions` | 候选定义补写 | 保留「（参考库命中）」占位 |
| P2 | `llm_tasks.extract_candidates` | 正文抽取（**必须带 `evidence_span`**） | 无文本层 → `skipped_extract` |
| P4 | `llm_tasks.judge_novelty` | 新颖性语义判断（可复用/需合并/确为新增 + matched） | 保留规则四色标签 |
| P4 | `llm_tasks.prioritize` | 立项优先级 1–5 + 理由 | 保留 `prioritize_candidates` 分数 |
| P5 | `llm_client.suggest_definitions` | 缺失字段定义/单位/类型 | 纯规则推断 |
| P6 | `llm_client.suggest_definitions` | 855 条属性定义补全（CLI 全量） | 定义留空 + `INCOMPLETE` |

**配置（优先级：环境变量 > `data/llm_key.txt` > `data/llm_config.json`）：**

```jsonc
{
  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "api_key": "sk-...",              // 也可只写 data/llm_key.txt（一行，支持 "key: sk-..." 写法）
  "model": "qwen3.8-max",           // 精修用强模型（注意大小写，百炼是小写 qwen3.8-max）
  "bulk_model": "qwen3.8-flash",    // 批量用快模型
  "timeout": 120,
  "fallback_models": ["qwen3.7-max", "qwen-max", "qwen-plus"]
}
```

**必须守住的四条：**

1. **分数不许模型参与**：P7 的对齐、判定、计分全部确定性代码；模型最多写错因解释。
2. **无证据不入库**：模型产出的候选/关系必须附原文片段或已有标准 id，否则只进"待确认"。
3. **可降级**：任何调用失败都回落规则，界面标注「未配置（走规则兜底）」或具体错误；`available()=false` 时**不得**假装调过模型。
4. **可审计**：每次调用写 `deliverables/llm/trace.jsonl`（`model / prompt_hash / elapsed_ms / usage / ok`）；只发结构化字段，不发文档全文。

**已知时延与对策（实测，`qwen3.8-flash`）：**

| 任务 | 单批规模 | 耗时 | 对策 |
|---|---|---|---|
| 定义补写 | 12 条 | ~28s | 4 并发；全量走 `enrich_all` CLI |
| 术语归一 | 20 条 | ~30–40s | 与定义补写并发 |
| 新颖性/优先级 | 8 条 | ~40s | 两任务并行；只判前 8 条 |
| 单字段精修 | 1–6 条 | 20–40s | 用 `qwen3.8-max`，超时 120s |

> `qwen3.8-max` 生成 12 条 JSON 时 60s 会超时（实测），故默认 `timeout=120`、单批 180s。
> 进程内缓存：首次慢、重复点击秒回；**重启服务会清缓存**，现场演示前建议先跑一遍预热。

**接口：** `GET /api/llm/status`（不回传 key，只回 `key_tail` 末四位便于核对）、`POST /api/llm/selftest`（一次极小真实调用）、`POST /api/standard/enrich`（P6 补全）。

## 6. 目录约定

```
nuclear-standard-agent/
├── .dsh/
│   ├── skills/nuclear-standard-agent-dual-mode/SKILL.md
│   └── roles/
│       ├── doc-parser.md
│       ├── knowledge-extractor.md
│       ├── ontology-builder.md
│       ├── landscape-analyst.md
│       ├── requirement-analyst.md
│       ├── model-synthesizer.md
│       ├── standard-generator.md
│       ├── standard-evolver.md
│       └── quality-auditor.md
├── backend/
│   ├── main.py                  # FastAPI + SSE
│   ├── harness_bridge.py        # 事件总线 / 就绪状态 / 门控存储
│   └── tools/
│       ├── doc_parser.py            # P1
│       ├── knowledge_extractor.py   # P2
│       ├── ontology_builder.py      # P3  json_to_owl / graph_upsert / graph_query
│       ├── mode_isolation.py        # 【v3】模式→数据源唯一真相来源 + 防泄漏 + 三维度阈值
│       ├── upload_manager.py        # 【v3】现场上传槽位 / 结构校验 / 上传优先取数
│       ├── landscape_analyst.py     # P4
│       ├── requirement_analyst.py   # P5
│       ├── model_synthesizer.py     # P5
│       ├── standard_generator.py    # P6
│       └── standard_evolver.py      # P6
├── frontend/
│   ├── index.html               # Vue 3.5 单页（经典 script，无构建）
│   └── vendor/                  # 【v3】vue.global.prod.js / cytoscape.min.js（本地，禁 CDN）
├── data/                        # 见 1.5
└── deliverables/
    ├── mode-a/{landscape,approved-candidates,standards}/
    ├── mode-b/{requirement-analysis,clarification-log,conflict-log,final-models}/
    ├── shared/
    ├── evaluation/{test_report.md,test_cases/}
    ├── acceptance.md
    ├── demo-script-mode-a.md
    └── demo-script-mode-b.md
```

## 7. 开发顺序

> 状态图例：✅ 已交付并验收 ｜ ⬜ 待开发

**阶段 -2 · 数据需求告知（会话首动作）** ✅
- 输出《数据准备清单》，写入 `data/DATA_REQUEST.md` / `.json`。

**阶段 -1 · 数据就绪与切分** ✅
- `data_readiness_check` → `readiness_status.json`；缺失项逐一确认降级。
- **数据由用户按模式放置**（不走自动切分），系统只做 `split_verify` 与 `leakage_check`。
- 已剔除 `modeB/dev/output` 中误留的 4 个测试类（57 → 53 类）。

**阶段 0 · 共享骨架与 P0** ✅
- FastAPI + SSE 骨架、Vue 3.5 单页、P0 模式卡片、数据就绪面板、事件流。

**增量 1 · P1 + `doc-parser`** ✅
- 模式 A 批量解析、模式 B 模板结构识别；`degraded/failed` 显式标记并入 P8。

**增量 2 · P2 + `knowledge-extractor` + 术语归一** ✅
- 参考库**按模式加载**（不得把两个模式的 dev/output 一锅烩）。

**增量 3 · P3 + 双知识底座** ✅
- `json_to_owl`、`graph_upsert`、Cytoscape.js；**A / B 各一套，节点交集 0**；三维度指标达标。

**增量 4 · 模式 A：`landscape-analyst` + P4** ✅
- 模拟库**只用模式 A 建模集**；四色新颖性；立项清单 + 工作量估算。

**增量 5 · 模式 B：`requirement-analyst` + `model-synthesizer` + P5** ✅
- 四阶段流水线；低置信推断不进合成；冲突消解必附依据。

**增量 6 · P6 标准生成与演化（双入口）** ✅
- A：立项清单 → 草案；B：合成模型 → 正式条目；演化匹配 + 合并门控。
- **完整模型表格导出（xlsx 四表）**：A / B 各一个下载入口，临时 ID 在「说明」页声明。

**增量 6.5 · P0 现场数据接入（上传入口）** ✅
- 9 个槽位上传 + 真实结构校验 + 上传优先取数 + 自动重建图谱。
- **理由：比赛现场演示必须从上传文件开始。**

**增量 7 · P7 准确率评测 + P8 门控中心 + `evaluate_against_test`** 🟡 部分交付

- ✅ `backend/tools/eval_pipeline.py`：**仅离线对账用**（`python -m tools.consistency_check`）——同输入重算一遍与 P6 产物比对，抓两条链路的实现漂移；**不再进入 P7 界面**
- ✅ `backend/tools/evaluator.py`：D7 逐行比对（**按对象类分组** + 三级对齐 + 匈牙利 + 四类判定 + 双口径计分 + 归因），落盘 `test_report.md` / `match_detail.csv`
- ✅ API：`POST /api/eval/run`、`GET /api/eval/latest`、`GET /api/eval/file`
- ✅ P7 面板：指标卡、判定筛选、字段级 / 归因 / 分对象类 / 逐行明细、报告与 CSV 下载
- ⬜ P7 三维雷达图、ZIP 导出、验收面板分组；⬜ P8 独立门控中心
- 三维雷达图、低分条目、按模式分组验收面板、ZIP 导出。
- **准确率按 D7（`*/test/output/` 三表）逐行比对**（对象类 / 属性 / 关系分层），作用域由 `covers_test_objects` 限定，口径见 1.3.4。

**增量 8 · 全链路联调与双演示脚本** ⬜
- 两份 demo script（各 5–8 分钟），彩排 ≥ 3 次。

### 7.1 前端交付纪律（v3 新增 · 血泪教训）

每新增/改动一个面板，**交付前必须跑一次 jsdom 真实挂载测试**。已经踩过三次的坑：

1. **`v-for` 源是函数**：`v-for="c in filteredA"`（filteredA 是函数）会**静默渲染 0 条且不报错**；必须写成 `filteredA()`。
2. **`#app` 标签嵌套错位**：`#app` 少一个收尾 `</div>`、某面板多一个提前闭合 `</div>`，两者叠加会把后续面板**顶到 `#app` 外面** → Vue 根本编译不到它们 → `{{ }}` 裸露、`v-if` 失效（所有面板同时显示）、弹窗常驻关不掉、按钮全死。
   **注意：标签计数、模板编译、`node --check` 全部会通过**（多一个多一个正好抵消），**只有真实挂载能发现**。
3. **测试标识与 tab/徽标文案重叠**：用标题子串判断"某面板是否可见"会永远误判（导航 tab 本身就含该文案）。取标识前先全文确认唯一性。

挂载测试最低要求：残留 `{{ }}` = 0、控制台无 error/warn、逐个点击 tab 后**各自只显示自己**、模态框不常驻。

## 8. 参赛硬指标落点

### 8.1 共享指标

| 指标 | 面板 | 演示动作 | 数据依据 |
|---|---|---|---|
| **现场上传并校验数据** | P0 | 上传建模集/文档集/需求文件，看校验结果 | 现场上传文件 |
| 支持 ≥ 2 种文件格式解析 | P1 | 解析 PDF / XLSX / DOCX | D1 / D2 |
| **知识底座三维度达标（A/B 各一套）** | P3 | 切换模式看统计面板 | 该模式参考底座 |
| OWL 2 DL 剖面校验通过 | P3 | 查看 OWL 校验项 | 系统生成物 |
| 三维质量评价 | P7 | 查看雷达图与低分条目 | 运行时产物 |
| 标准可使用率 ≥ 80% | P6 | 生成标准并补全 INCOMPLETE | 运行时 + D3（有则评） |
| **D0 常驻标准库可上传、可回写** | P0 / P6 | 上传一次 → P5 匹配文案变 D0 → 勾选“通过并入库”再匹配 | D0 库（`data/library/`） |
| **大模型真实参与（P2/P4/P5/P6 四处）** | P2/P4/P5/P6 | 点「模型连通性自检」→ 再点 P2 抽取、P4 分析、P6 补写 | `/api/llm/status` + `deliverables/llm/trace.jsonl` |

### 8.2 模式 A 专属

| 指标 | 面板 | 演示动作 | 数据依据 |
|---|---|---|---|
| 候选全景图可生成 | P4 | 上传文档集后自动生成 | D1 |
| 新颖性四类标签可区分 | P4 | 展示至少三种标签 | **D0 已有标准库**（为空时全部判「全新」） |
| 覆盖盲区提示 | P4 | 侧栏展示盲区列表 | D1 |
| 立项勾选闭环 | P4→P6 | 勾选候选→生成草案 | 运行时 |
| **新颖性语义判断 + 立项优先级** | P4 | 查看「AI：需合并（命中 X）」「AI 优先级 N」与理由 | 大模型（`judge_novelty` / `prioritize`） |
| **数据标准生成准确率**（行级 + 字段级） | P2 / P7 | 准确率面板 | **D3 = 模式 A 预期输出** 三表，逐行比对（见 1.3.4） |

### 8.3 模式 B 专属

| 指标 | 面板 | 演示动作 | 数据依据 |
|---|---|---|---|
| 需求模板结构识别 | P1 | 上传需求文件 | D5 + D2 |
| 需求完备性评分 | P5 | 查看评分与 gap 列表 | D4 + D2 |
| AI 推断带置信度和依据 | P5 | 至少一项缺失被推断补齐 | 参考底座模拟库 / 内置字段基线 |
| 冲突消解记录 | P5 | 至少一项冲突被处理 | 运行时 |
| 最终模型字段完备 | P5 | 导出前检查 | **D4**（三表列定义 + ID / 类型 / 留空规则） |
| 可导入 EDM 的格式 | P5/P7 | 导出成果包 | D4 |
| **数据标准生成准确率**（行级 + 字段级） | P7 | 准确率面板 | **D3 = 模式 B 预期输出** 三表，逐行比对（见 1.3.4） |

## 9. 风险与对策

- **数据迟到或不全：** 阶段 -2 的清单与阶段 -1 的降级决策已覆盖；任何缺失都必须在 `readiness-report.md` 留痕，验收面板标"未验证"。
- **测试输入合成导致指标失真：** `synthetic_input: true` 必须在 `test_report.md` 与 P7 显著标注，不得混入"真实业务准确率"。
- **模式 A 候选过载：** 全景图默认按优先级降序，UI 默认只展示高优先级。
- **新颖性误判：** 术语归一再做新颖性检测；`conflict`/`similar` 进人工门控。
- **模式 B 推断越界：** `confidence < 0.6` 仅作参考；所有推断标 `source: inference`，可逐项驳回。
- **冲突静默覆盖：** `synthesize_model` 强制字段标来源；`resolve_conflict` 决策入日志。
- **底座污染：** 节点带 `confidence` 与 `source_mode`；模式 B 的 RAG 默认过滤 `source_mode=A && confidence<0.7`。
- **测试泄漏：** `leakage_check` 为硬门控；`evaluate_against_test` 在独立进程运行，输出脱敏。
- **SSE 断流：** 自动重连 + `Last-Event-ID` 续传；关键阶段写检查点。
- **Harness 接口变动：** 所有交互封装在 `harness_bridge.py`。
- **数据安全：** 不将核电文档原文发往外部服务；解析与抽取在内部受控环境执行。
- **建模规则漂移（v3.3 收敛）：** D4（`modeling_rules.json`）是唯一规则源；P5 对照的「内置字段基线」只是完备性检查用的字段清单，**不得用它的字段名去替换 D4 的列定义**，也不得为通过校验擅自改名。
- **编码自造（v3.3）：** 业务未填且 D4 无生成规则时，禁止模型编造编码；一律留空标 `INCOMPLETE`，由人工补全。
- **评测对象错用（教训）：** P7 只评 **P6 产物**，且必须校验产物与本轮输入的**快照一致性**（`pred_same_dataset`）；用旧产物比预期输出会得到假数字。**不允许**把「从输入现场重算」做成界面按钮——那等于宣称一个 P7 就能替代 P1–P6，自毁流水线叙事；该能力只留在离线脚本里做回归。
- **跨模式泄漏（v3 新增）：** 同一批对象类在不同模式下角色不同（EM4 对 A 是答案、对 B 是建模集）。只校验"路径里有没有 test 字样"**不够**，必须按 1.8 的模式白名单/黑名单校验，并在数据变更后重跑交集校验。
- **前端依赖 CDN（v3 新增）：** CDN 不可达会导致页面退化成未编译模板。依赖必须本地化 + `v-cloak` + 启动兜底提示。
- **面板未挂载（v3 新增）：** 静态检查通过 ≠ 能挂载。每次改前端必须跑真实挂载测试（见 7.1）。
- **建模数据规模不足（v3 新增）：** 某模式建模集可能撑不起阈值。优先补数据；不能补则**单独放宽该模式阈值并在验收面板写明依据**，不得跨模式共用节点来凑数。
- **现场上传改名（v3 新增）：** 扫描件无文本层时抽取依赖文件名主题，现场改名会显著改变候选数。上传面板必须提示"建议保留业务原名"。
- **大模型时延不可控：** 单批 12 条实测 20–40s，8 条判断 40s 起，网络差会更久。所有调用点必须：① 先推 `llm_working` 事件让前端显示进度；② 设 `timeout`（默认 120s、单批 180s）；③ 失败只降级该条/该批，不整页失败；④ 结果进缓存，重复点击秒回（**重启服务清缓存，演示前先预热**）。
- **模型名/接口变动：** 百炼模型名是小写 `qwen3.8-max`（写 `Qwen3.8-Max` 会 400）；已配 `fallback_models` 自动回退并在 `trace.jsonl` 标 `model_fallback`。换服务商只需改 `base_url/model/bulk_model`，代码不动。
- **余额/鉴权失败：** 401/402 后进入 300s 冷却，`/api/llm/status` 直接给原因（含 `key_tail` 末四位便于核对），**不要**让每个面板都去撞超时。
- **把模型当计分器：** 严禁。P7 的对齐与计分必须确定性；模型只写错因解释。分数一旦可随模型漂移，整个准确率就不可复现，直接失分。

## 10. 落地记录（v3 · 本项目实测）

> 本章记录 2026-10-21 阶段性 demo 的**真实实测数据**与**诚实标注的降级项**，作为后续开发与验收的基线。**不得用本章数字反推造假指标。**

### 11.1 实测数据规模

| 数据集 | 对象类 | 属性实例 | 关系 | 角色 |
|---|---|---|---|---|
| A / 当前数据集（`modeA/预期输出 group1`） | 160 | 0 | — | D3 预期输出（只评对象类层）|
| A / 备用样本（`modeA/预期输出 group2`） | 21 | 417 | 437 | 参考底座属性来源（不单独评测）|
| A / 输入（`modeA/输入 group1|2`） | — | — | — | D1，2 份扫描 PDF（文本层 0 字符，degraded）|
| B / 当前数据集（`modeB/预期输出`） | 57 | 891 | 943 | D3 预期输出（三表齐全）|
| B / 输入（`modeB/输入`） | 67 | 855 | — | D2，5 份业务需求表合并、按 (类, 属性) 去重 |
| D4 / D5 | 3 张表定义 / 1 份模板 | — | — | 支撑数据 |

**统一取数口径（硬约束）：** 模式 B 的 5 份需求表必须**全部读取**，由 `dataset_registry.load_tables` + `eval_pipeline._dedup_*` 统一解析去重，类名按 `_norm` 归一（如「空心型钢（方、圆、矩形钢）」归回「空心型钢」）。P5 解析 / P5 合成 / P7 生成侧 / 导出三表**四处必须同为 67 类 / 855 属性**；出现 17 类、53 类这类偏差，说明有人又去单独读某个文件了。

**知识底座（P3，底座 = D0 + 本轮输入，不读 D3）：** 模式 A 357 类 / 23944 属性 / 23944 关系（阈值 100/100/60 ✔，D1 两份 PDF 为扫描件、输入供给 0）；模式 B 420 类 / 24795 属性 / 24795 关系（阈值 20/200/500 ✔，D0 357 类 + D2 67 类 / 855 属性）；两模式节点合集**交集 0**。

**标准（P6，模式 B）：** 67 类（50 类有属性行）/ 855 条字段；完全合规率 ~0.1%（业务未填约束/定义/单位），**结构可入库率 100%**；大模型补全后定义缺口 855 → 24，规范性 3.4 → 36.8。

### 11.1.1 增量 7 准确率实测（2026-10-22）

**口径：按 `(mode, group)` 绑定 —— 生成侧只读该组 `输入/`，答案侧只读同组 `预期输出/`。**

| 组 | 输入 | 生成规模 | 预期规模 | 对象类层（生成率 / 覆盖率） | 属性层（生成率 / 覆盖率） | 合计 |
|---|---|---|---|---|---|---|
| **B/default** | 5 份需求 xlsx（ready） | 67 类 / 855 属性 | 57 类 / 891 属性 | **82.09% / 96.49%** | **90.76% / 87.09%** | **90.13% / 87.66%** |
| **A/group1** | 物项分级 PDF（扫描件） | 0 | 160 类 | 不可评测 | N/A（无属性表） | 不可评测 |
| **A/group2** | 管道等级表 PDF（扫描件） | 0 | 21 类 / 417 属性 | 不可评测 | 不可评测 | 不可评测 |

**如实说明：**

1. **B/default**：属性层 exact 776 / partial 6 / missing 109 / extra 73，字段级 **99.74%**；对象类层 exact 55 / missing 2 / extra 12（输入多了「其他」等大类节点，预期输出没有）。
2. **A 两组**：输入均为扫描件（文本层 0 字符），无法识别任何对象类/属性 → `evaluable=false`。这是「**输入不可解析**」，**不是 0% 准确率**；需提供可解析文档或接入 OCR。
3. **知识底座**（P3）：底座已与 D3 解耦，只装 D0 + 本轮输入 → A = 357 类 / 23944 属性 / 23944 关系（100/100/60 全 PASS）；B = 420 类 / 24795 属性 / 24795 关系（20/200/500 全 PASS）。
4. `constraint` 不计入 exact：需求侧是业务规则文本（"仅允许 SCH+数字"）、答案侧是技术格式（"VARCHAR(256)"），口径不同，只作参考项。

### 11.2 诚实标注的降级项

| 项 | 状态 | 说明 |
|---|---|---|
| D1-dev | **degraded** | 扫描件无文本层 → 候选只能弱匹配，**全部低置信**，真实演示"OCR 缺失 → 低置信 → 入 P8"链路 |
| D2 | **ready** | 5 份需求表合并 → 67 类 / 855 属性；P2 候选数、P5 规模、P7 生成规模三处必须相等（2026-10-23 已对齐） |
| **D7** | **ready** | 评测集 = 各模式 `test/output`：A **57 类 / 891 属性**；B **仅 4 类 / 112 属性**，样本偏小，准确率须报置信区间并标注覆盖不足 |
| D5 / D9 | missing | 模板识别降级为按列定位 |
| **D4** | **ready** | `data/modeling_rules.json`：3 张表列定义 + 5 条 ID 规则 + 9 类类型映射 + 留空规则 |
| 模式 A 建模集 | **残缺** | 43/120 属性指向未提供的类；162 个类父类不在子集内 → subClassOf 为 0，关系仅 77 条 |
| P6 入口 A 可使用率 | 33.3% | 隔离后管道管件候选在 E板块查不到编码/约束 → 更多 INCOMPLETE，**不粉饰** |
| P4 新颖性 | novel 23 / similar 5 / covered 1 | 隔离后 **conflict 归零**（E板块自身无同名不同口径属性），满足"至少三种标签" |

### 11.3 已知问题（待办）

1. `domain_vocab.json` 中 **`管道` 被登记为 `无法止回` 的同义词**（精确命中，置信 0.95），导致抽取候选名被改写。当前规避方式：新颖性判定改用**原始抽取词**，归一结果只在卡片展示。**建议单独修正该词表。**
2. 模式 A 需求文件 sheet 名会被重新整理（如 `Sheet1` → `对象类属性`），读取时**必须按列自动定位**，写死 `sheetnames[0]` 会读到只有 21 行的 `对象类` 表而返回空。
3. Python `re` 的 `\w` 含中日韩字符，中文语境下 `\b` 词边界不成立（如"见MD000070"），需改用前后瞻。

### 11.4 交付物

```
deliverables/
├── mode-a/{landscape,approved-candidates}/
├── mode-b/{requirement-analysis,clarification-log,conflict-log,final-models}/
└── shared/{ontology.ttl,evolution-log/}
```

服务：`python backend/main.py` → http://127.0.0.1:8123 （前端由后端托管，Vue/Cytoscape 走 `frontend/vendor/`，断网可演示）。

## 11. 启动指令

在 Harness 会话中直接说：

> 加载 `nuclear-standard-agent-dual-mode`。**第一步不是检查文件，而是向我输出《数据准备清单》**：逐项列出需要我准备的 **D1–D8** 数据，说明用途、格式、规模下限和放置路径（`data/mode{A,B}/{输入,预期输出}`），写入 `data/DATA_REQUEST.md`，然后在对话中等我回复。我回复"数据已就绪"或"仅准备模式 X 数据"后，你再执行就绪校验、逐项询问缺失降级决策，然后完成数据接入与防泄漏检查（**必须按模式隔离，见 1.8**）。三份报告发我确认后，才进入增量 0 开发。

**重新启动已有项目时**（数据已在 `data/` 下）：跳过清单等待，直接校验并按 1.8 / 1.9 建立模式隔离与上传接入，然后从下一个未完成增量继续。**演示前必须确认 P0 上传入口可用** —— 现场要从上传文件开始。