"""
llm_tasks.py
============
把「必须理解自然语言」的环节交给大模型（其余仍走规则）：

1. `extract_candidates`     P2 · 从文档正文抽候选对象类/属性，**必须附原文片段**
2. `normalize_terms`        P2 · 术语归一到规范名（词表之外的写法）
3. `judge_confidence`       P2 · 模糊区间的候选定「高/中/低」三档
4. `judge_novelty`          P4 · 与现有标准比对，判"可复用 / 需合并 / 确为新增"
5. `prioritize`             P4 · 按业务价值与覆盖缺口排优先级
6. `analyze_class_relations` P3 · **对象类之间**的关系发现（参考本体论模式：
   is-a / part-of / 业务关联 / 同义），补「表格里没写父类」的层级缺口

约定：
- 全部 JSON 输出，失败抛异常由调用方降级，**不允许规则冒充模型结果**；
- 每条结果带 `confidence` 与 `reason`，界面可解释、可驳回；
- 抽取类结果必须带 `evidence_span`，没有原文片段的一律不采纳（防止编造）；
- 关系类结果的 head/tail **必须在给定类名白名单内**，越界的直接丢弃（防止编类）。
"""

from __future__ import annotations

import json

from tools import llm_client as llm

SYS_EXTRACT = (
    "你是核电 EPC 数据标准专家。从给定的业务文档片段中抽取可纳入数据标准的"
    "**对象类**与**属性**。要求：\n"
    "1) 只抽文档中确有其文的内容，每条必须给出 evidence_span（原文摘录，20–60 字，逐字照抄）；\n"
    "2) 抽不到就少给，宁缺勿造；不要使用文档外的领域常识；\n"
    "3) 属性尽量带 class_hint（所属对象类）、unit、data_type_hint；\n"
    "4) 只输出 JSON：{\"items\":[{\"kind\":\"object_class|attribute\",\"name\":\"\","
    "\"class_hint\":\"\",\"unit\":\"\",\"data_type_hint\":\"\",\"evidence_span\":\"\","
    "\"confidence\":0.0-1.0}]}"
)

SYS_TERM = (
    "你是核电数据标准术语专家。给定候选术语清单，判断每个术语的**规范名**：\n"
    "1) 已是规范写法就 canonical 填自身；\n"
    "2) 是同义词/简称/别名（如“Ø”=“直径”“ELBO”=“弯头”），canonical 给最规范的中文名；\n"
    "3) 不同事物绝不合并；拿不准就 canonical 填自身、confidence 低于 0.6；\n"
    "4) 只输出 JSON：{\"items\":[{\"name\":\"\",\"canonical\":\"\",\"confidence\":0.0-1.0,"
    "\"reason\":\"\"}]}"
)

SYS_CONFIDENCE = (
    "你是核电数据标准评审专家。给定一批**已由规则初判、处于模糊区间**的候选条目，"
    "逐条判断它们「作为数据标准对象类/属性」的可信程度。要求：\n"
    "1) 只给三档，不给精确概率：高 = 文档里明确写出且形态就是标准条目；"
    "中 = 文档里有依据，但形态不完整或归属不明；低 = 证据太弱、大概率不是标准条目；\n"
    "2) 必须看 evidence（原文片段）与 fields（规格字段）下判断，**不得使用文档外的常识补全**；\n"
    "3) reason 用一句话说清依据，例如「表格第 3 行明确给出类型与单位」"
    "「仅标题出现，未见属性定义」「原文是流程描述，不是字段」；\n"
    "4) 规则分仅供参考，不要不加判断地照抄；\n"
    "5) 只输出 JSON：{\"items\":[{\"name\":\"\",\"level\":\"高|中|低\",\"reason\":\"\"}]}"
)

SYS_NOVELTY = (
    "你是数据标准治理专家。给定一批**候选标准条目**与**现有标准条目**，逐条判断：\n"
    "- 可复用：现有条目已完整覆盖，可直接引用；\n"
    "- 需合并：语义相同或高度重叠，但与现有条目口径/命名不一致，需要合并；\n"
    "- 确为新增：现有条目里没有等价项；\n"
    "必须给出 matched（命中的现有条目名，没有则空）与 reason（一句话说清依据）。\n"
    "只输出 JSON：{\"items\":[{\"name\":\"\",\"verdict\":\"可复用|需合并|确为新增\","
    "\"matched\":\"\",\"reason\":\"\",\"confidence\":0.0-1.0}]}"
)

SYS_PRIORITY = (
    "你是数据治理项目经理。给定候选标准条目与当前覆盖盲区，为每条给出建设优先级：\n"
    "- priority：1（最高）~5（最低），依据「业务影响面、是否卡住下游对接、是否落在盲区」；\n"
    "- score：0-100 的综合分；\n"
    "- reason：一句话说明为什么排这个位置。\n"
    "只输出 JSON：{\"items\":[{\"name\":\"\",\"priority\":1,\"score\":0,\"reason\":\"\"}]}"
)

# P3 · 类间关系（本体论视角）。关系类型**只允许四种**，多一个都不认。
SYS_CLASS_REL = (
    "你是本体工程师，为核电 EPC 数据标准构建**对象类之间**的关系网络，"
    "参照 OWL 本体的建模模式。允许的关系类型只有这四种：\n"
    "1) subClassOf —— 上下位（is-a）：head 是 tail 的一种/一类/子类，tail 更抽象。"
    "例：球阀 subClassOf 阀门；安全阀 subClassOf 阀门。\n"
    "2) partOf —— 组成（part-of）：head 是 tail 的组成部分/部件/零件。"
    "例：阀体 partOf 球阀；管件 partOf 管道系统。\n"
    "3) associates —— 业务关联：两者在业务上配合、引用或一一对应，但既不是上下位也不是组成。"
    "例：管道 associates 支吊架；焊缝 associates 无损检测报告。\n"
    "4) sameAs —— 同义：两个类名指同一事物（仅当不同来源写法不一致时给）。\n"
    "判断依据的优先级：定义文本 > 共享的属性集合 > 类名构词（如「XX阀」多为「阀门」子类）。\n"
    "输入 JSON 结构：name_pool = 全部可选类名（白名单）；classes = 本批要分析的类，"
    "每项含 name（类名）、definition（定义）、attributes（属性清单）、evidence（出处证据）。\n"
    "硬性要求：\n"
    "- head / tail 必须原样取自给定的类名清单，**不得发明新类名、不得改字**；\n"
    "- 禁止自环（head=tail）；同一对类只给一条最强的关系；\n"
    "- subClassOf 方向必须是「具体 → 抽象」，绝不允许 A→B 与 B→A 同时出现；\n"
    "- 每条必须给 basis（判断依据，≤40 字，指向定义/属性/构词里的具体线索）；\n"
    "- 没有依据就不要给，宁缺勿造；把握不大就把 confidence 压到 0.6 以下；\n"
    "- 只输出 JSON：{\"relations\":[{\"type\":\"subClassOf|partOf|associates|sameAs\","
    "\"head\":\"\",\"tail\":\"\",\"basis\":\"\",\"confidence\":0.0-1.0}]}"
)


def _clean_items(data, keys: tuple[str, ...]) -> list[dict]:
    got = (data or {}).get("items")
    if not isinstance(got, list):
        return []
    out = []
    for g in got:
        if not isinstance(g, dict) or not str(g.get("name") or "").strip():
            continue
        row = {"name": str(g["name"]).strip()}
        for k in keys:
            row[k] = g.get(k, "")
        out.append(row)
    return out


def _float(v, default=0.6) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 1) P2 · 正文抽取（带证据）
# ---------------------------------------------------------------------------
def extract_candidates(doc_title: str, text: str, *, max_items: int = 25,
                       model: str | None = None, timeout: float = 180) -> dict:
    text = (text or "").strip()
    if len(text) < 40:
        raise ValueError("文档无可用正文（扫描件需先 OCR），不能做正文抽取")
    msgs = [
        {"role": "system", "content": SYS_EXTRACT},
        {"role": "user", "content": "文档：%s\n正文片段：\n%s" % (doc_title, text[:6000])},
    ]
    out = llm.chat_json(msgs, tag="p2.extract", model=model, timeout=timeout,
                        max_tokens=2600)
    raw = _clean_items(out.get("data"), ("kind", "class_hint", "unit",
                                         "data_type_hint", "evidence_span", "confidence"))
    items = []
    for r in raw[:max_items]:
        ev = str(r.get("evidence_span") or "").strip()
        # 硬约束：没有原文证据的不采纳
        if len(ev) < 4:
            continue
        items.append({
            "kind": "attribute" if str(r.get("kind")).lower().startswith("attr") else "object_class",
            "name": r["name"],
            "class_hint": str(r.get("class_hint") or "").strip(),
            "unit": str(r.get("unit") or "").strip(),
            "data_type_hint": str(r.get("data_type_hint") or "").strip(),
            "evidence_span": ev,
            "confidence": _float(r.get("confidence"), 0.7),
        })
    return {"items": items, "model": out["model"], "elapsed_ms": out["elapsed_ms"],
            "dropped_no_evidence": len(raw) - len(items)}


# ---------------------------------------------------------------------------
# 2) P2 · 术语归一
# ---------------------------------------------------------------------------
def normalize_terms(names: list[str], *, model: str | None = None,
                    timeout: float = 120) -> dict:
    names = [str(n).strip() for n in names if str(n).strip()][:40]
    if not names:
        return {"items": [], "model": "", "elapsed_ms": 0}
    msgs = [
        {"role": "system", "content": SYS_TERM},
        {"role": "user", "content": json.dumps({"terms": names}, ensure_ascii=False)},
    ]
    out = llm.chat_json(msgs, tag="p2.term", model=model, timeout=timeout,
                        max_tokens=max(800, 90 * len(names)))
    items = [{"name": r["name"], "canonical": str(r.get("canonical") or r["name"]).strip(),
              "confidence": _float(r.get("confidence"), 0.6),
              "reason": str(r.get("reason") or "").strip()}
             for r in _clean_items(out.get("data"), ("canonical", "confidence", "reason"))]
    return {"items": items, "model": out["model"], "elapsed_ms": out["elapsed_ms"]}


# ---------------------------------------------------------------------------
# 2.5) P2 · 置信度裁决（只判模糊区间，输出三档而非概率）
# ---------------------------------------------------------------------------
def judge_confidence(cands: list[dict], *, model: str | None = None,
                     timeout: float = 150) -> dict:
    """给规则初判落在模糊区间的候选定「高/中/低」。

    刻意**不要**模型输出 0–1 的概率：模型对术语天生乐观，直接要分数会得到一片 0.9，
    失去区分度。这里只要离散等级与依据，由 confidence.py 融合成最终分。
    """
    cands = [c for c in (cands or []) if str(c.get("name") or "").strip()][:25]
    if not cands:
        return {"items": [], "model": "", "elapsed_ms": 0}
    payload = {"items": cands}
    msgs = [
        {"role": "system", "content": SYS_CONFIDENCE},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    out = llm.chat_json(msgs, tag="p2.confidence", model=model, timeout=timeout,
                        max_tokens=max(700, 90 * len(cands)))
    items = []
    for r in _clean_items(out.get("data"), ("level", "reason")):
        lvl = str(r.get("level") or "").strip()
        # 容错：模型偶尔会写成"高置信""中等"
        if lvl not in ("高", "中", "低"):
            if "高" in lvl:
                lvl = "高"
            elif "低" in lvl:
                lvl = "低"
            elif "中" in lvl:
                lvl = "中"
            else:
                continue
        items.append({"name": r["name"], "level": lvl,
                      "reason": str(r.get("reason") or "").strip()})
    return {"items": items, "model": out["model"], "elapsed_ms": out["elapsed_ms"]}


# ---------------------------------------------------------------------------
# 3) P4 · 新颖性判断
# ---------------------------------------------------------------------------
def judge_novelty(cands: list[dict], existing: list[str], *, model: str | None = None,
                  timeout: float = 150) -> dict:
    cands = cands[:15]
    if not cands:
        return {"items": [], "model": "", "elapsed_ms": 0}
    payload = {
        "candidates": [{"name": c.get("name", ""), "type": c.get("type", ""),
                        "definition": (c.get("definition") or "")[:80]} for c in cands],
        "existing": existing[:200],
    }
    msgs = [
        {"role": "system", "content": SYS_NOVELTY},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    out = llm.chat_json(msgs, tag="p4.novelty", model=model, timeout=timeout,
                        max_tokens=max(900, 110 * len(cands)))
    items = []
    for r in _clean_items(out.get("data"), ("verdict", "matched", "reason", "confidence")):
        v = str(r.get("verdict") or "").strip()
        if v not in ("可复用", "需合并", "确为新增"):
            v = "确为新增" if "新增" in v else ("需合并" if "合并" in v else "可复用")
        items.append({"name": r["name"], "verdict": v,
                      "matched": str(r.get("matched") or "").strip(),
                      "reason": str(r.get("reason") or "").strip(),
                      "confidence": _float(r.get("confidence"), 0.65)})
    return {"items": items, "model": out["model"], "elapsed_ms": out["elapsed_ms"]}


# ---------------------------------------------------------------------------
# 4) P4 · 立项优先级
# ---------------------------------------------------------------------------
def prioritize(cands: list[dict], blind_spots: list[dict] | None = None, *,
               model: str | None = None, timeout: float = 150) -> dict:
    cands = cands[:15]
    if not cands:
        return {"items": [], "model": "", "elapsed_ms": 0}
    payload = {
        "candidates": [{"name": c.get("name", ""), "type": c.get("type", ""),
                        "novelty": c.get("novelty", "")} for c in cands],
        "blind_spots": [{"domain": b.get("domain") or b.get("section") or "",
                         "missing": b.get("hint") or b.get("reason") or ""}
                        for b in (blind_spots or [])][:12],
    }
    msgs = [
        {"role": "system", "content": SYS_PRIORITY},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    out = llm.chat_json(msgs, tag="p4.priority", model=model, timeout=timeout,
                        max_tokens=max(800, 90 * len(cands)))
    items = []
    for r in _clean_items(out.get("data"), ("priority", "score", "reason")):
        try:
            pr = int(r.get("priority") or 3)
        except Exception:
            pr = 3
        try:
            sc = float(r.get("score") or 0)
        except Exception:
            sc = 0.0
        items.append({"name": r["name"], "priority": max(1, min(5, pr)),
                      "score": max(0.0, min(100.0, sc)),
                      "reason": str(r.get("reason") or "").strip()})
    return {"items": items, "model": out["model"], "elapsed_ms": out["elapsed_ms"]}


# ---------------------------------------------------------------------------
# 5) P3 · 对象类之间的关系发现（本体论模式：is-a / part-of / 关联 / 同义）
# ---------------------------------------------------------------------------
REL_TYPES = ("subClassOf", "partOf", "associates", "sameAs")
REL_TYPE_CN = {"subClassOf": "上下位", "partOf": "组成", "associates": "业务关联",
               "sameAs": "同义"}

# 模型写法五花八门（is-a / subclass_of / 组成 / 关联），统一归到四种正规类型。
# 归一不上的直接丢弃 —— 不允许把未知类型塞进图里，OWL 那边会不认识。
_REL_ALIAS = (
    ("subClassOf", ("subclassof", "subclass", "is-a", "is_a", "isa", "sub_class_of",
                    "上下位", "继承", "子类", "父类", "is a")),
    ("partOf", ("partof", "part_of", "part-of", "composition", "组成", "构成",
                "部分", "部件", "包含于")),
    ("associates", ("associates", "associate", "associated", "association",
                    "relates", "reference", "关联", "引用", "对应", "配合")),
    ("sameAs", ("sameas", "same_as", "same-as", "equivalentclass", "同义",
                "等同", "别名")),
)


def _norm_rel_type(t) -> str:
    s = str(t or "").strip()
    if not s:
        return ""
    low = s.lower().replace(" ", "")
    for canon, aliases in _REL_ALIAS:
        if low == canon.lower():
            return canon
        for a in aliases:
            if low == a.replace(" ", "") or low.startswith(a):
                return canon
    return ""


def _rel_name(v) -> str:
    """类名归一：只做空白清理，**不做模糊匹配**（白名单必须是原样的名字）。"""
    return str(v or "").replace("\u3000", " ").strip()


def analyze_class_relations(classes: list[dict], name_pool: list[str] | None = None, *,
                           model: str | None = None, timeout: float = 240,
                           max_relations: int = 60) -> dict:
    """让模型为一批对象类找出它们之间的关系（含与池中其他类的关系）。

    参数
    ----
    classes : [{name, definition, attributes: [str], evidence: str}]
        本批要分析的类（带定义与属性清单，模型据此判断归属）。
    name_pool : 可引用的**全部**类名（通常整张底座的类名）。
        作用有二：① 提供父类候选（避免模型凭空造父类）；② 作为硬白名单，
        返回的 head/tail 不在池内一律丢弃。
    model : 默认走配置的模型（批量任务可传 bulk_model）。

    返回
    ----
    {relations:[{type, head, tail, basis, confidence}], model, elapsed_ms,
     requested, dropped_unknown_name, dropped_no_basis, dropped_dup,
     dropped_type, note}

    注：这里只产出**候选关系**，不入图。入图由 class_relations.apply() 在
    人工确认阈值后执行 —— 生成环节不自动改底座。
    """
    classes = [c for c in (classes or []) if _rel_name(c.get("name"))][:40]
    if not classes:
        return {"relations": [], "model": "", "elapsed_ms": 0, "requested": 0,
                "dropped_unknown_name": 0, "dropped_no_basis": 0,
                "dropped_dup": 0, "dropped_type": 0, "note": "本批没有可分析的类"}

    pool = [_rel_name(x) for x in (name_pool or []) if _rel_name(x)]
    if not pool:
        pool = [_rel_name(c["name"]) for c in classes]
    # 池子太大时截断：prompt 有 12000 字符硬上限，超了会被 llm_client 掐掉内容，
    # 与其被静默截断，不如在这里按原顺序留前 400 个（调用方已按信息量排序）。
    pool = pool[:400]
    pool_set = set(pool)

    payload = {
        "name_pool": pool,
        "classes": [{
            "name": _rel_name(c["name"]),
            "definition": str(c.get("definition") or "")[:120],
            "attributes": [str(a) for a in (c.get("attributes") or [])][:10],
            "evidence": str(c.get("evidence") or ""),
        } for c in classes],
    }
    msgs = [
        {"role": "system", "content": SYS_CLASS_REL},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    out = llm.chat_json(msgs, tag="p3.class_rel", model=model, timeout=timeout,
                        max_tokens=max(1200, 55 * max(8, len(classes))))

    got = (out.get("data") or {}).get("relations")
    note = ""
    if not isinstance(got, list):
        got, note = [], "模型返回里没有 relations 数组"

    rels: list[dict] = []
    seen: set = set()
    d_unknown = d_basis = d_dup = d_type = 0
    for r in got:
        if not isinstance(r, dict):
            continue
        t = _norm_rel_type(r.get("type"))
        head, tail = _rel_name(r.get("head")), _rel_name(r.get("tail"))
        if not t:
            d_type += 1
            continue
        # 白名单硬校验：名字必须原样在池子里，否则丢弃（不许模型编类 / 改字）
        if head not in pool_set or tail not in pool_set or not head or not tail:
            d_unknown += 1
            continue
        if head == tail:
            continue
        basis = str(r.get("basis") or "").strip()
        if len(basis) < 4:
            d_basis += 1
            continue
        key = (t, head, tail)
        if key in seen:
            d_dup += 1
            continue
        seen.add(key)
        rels.append({"type": t, "head": head, "tail": tail, "basis": basis,
                     "confidence": _float(r.get("confidence"), 0.6)})
        if len(rels) >= max_relations:
            break

    return {"relations": rels, "model": out["model"], "elapsed_ms": out["elapsed_ms"],
            "requested": len(classes), "dropped_unknown_name": d_unknown,
            "dropped_no_basis": d_basis, "dropped_dup": d_dup,
            "dropped_type": d_type, "note": note}
