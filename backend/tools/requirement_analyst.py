"""
requirement_analyst.py
=======================
核电数据标准智能治理智能体（双模式）· 模式 B 需求分析（增量 5 · P5）。

绑定工具（见 .dsh/roles/requirement-analyst.md）：
parse_requirement / gap_analyze / generate_clarification / rag_vector / rag_graph。

诚实、可追溯的设计原则：
- 全部基于真实文件：
  · D2 需求文件（模式 B 业务人员填写的需求表，多份合并）
  · D4   完整数据模型模板 data/templates/complete-model-template.json（41 字段）
  · 参考底座模拟库（mode_isolation.load_mode_model，真实已有标准库缺失时顶替）
- 完备性评分公式可审计（`FORMULA` 常量随结果返回）。
- AI 推断必附 `basis`（来自参考底座模拟库或内置字段基线，说明具体出处）与 `confidence`；
  confidence < 0.6 明确标注「仅供参考，不得直接采纳」。
- 冲突判定不编造：需求文件的「数据格式/约束」与 D4 模板的 `constraint` 逐字比对，
  不一致即为真实口径冲突。
- 检索源不含 test_model / full_mode。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import knowledge_extractor as ke   # noqa: E402
from tools import ontology_builder as ob      # noqa: E402
from tools import landscape_analyst as la     # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

REQ_XLSX = (DATA_ROOT / "modeB" / "dev" / "input" /
            "数据标准需求-业务人员填写-EPC互联-大宗材料类-EM4管道管件-建模用数据集.xlsx")

def _req_files() -> list[str]:
    """D2 的全部需求表（1–N 份）：**只看 b_in 槽位的显式决策**。

    模式 B 的输入本来就是「多份业务人员填写的表」。**所有 Px 必须用同一批文件**，
    否则 P5 的规模会和 P7 / 导出对不上（历史问题：P5 只读了「管道管件」那一份）。
    未上传且未选择「采用内置数据」时抛 DataUnavailable，绝不静默回落内置目录。
    """
    from tools import upload_manager as um
    fs = [f for f in um.active_files("b_in")
          if str(f).lower().endswith((".xlsx", ".xls"))]
    if not fs:
        raise um.DataUnavailable("b_in")
    return sorted(fs)


def _req_path() -> str:
    """兼容旧调用：只取第一份。新代码请用 `_req_files()` 拿全量。"""
    return _req_files()[0]


# 注：需求表的逐文件读取已统一到 dataset_registry.load_tables（见 `_req_dataset`），
# 这里不再保留第二套 xlsx 解析，避免「P5 与 P7 读到不同结果」。


D4_JSON = DATA_ROOT / "templates" / "complete-model-template.json"

# 完备性评分公式（随结果返回，保证可审计）
FORMULA = ("completeness_score = Σ(字段权重) / 字段总数 × 100；"
           "权重：filled=1.0，ambiguous=0.5，conflict=0.3，missing=0")

CONFIDENCE_REFERENCE_ONLY = 0.6   # SKILL 5.4：低于此值仅作参考

_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 读取 D2 需求文件
# ---------------------------------------------------------------------------
# 注：`_req_files` / `_sheet_rows` / `_read_rows` 已在文件上方定义（多份合并版），
# 此处不再重复定义单文件版本，避免后面的定义覆盖前面的合并逻辑。


def _col(hdr: list[str], name: str) -> int:
    return hdr.index(name) if name in hdr else -1


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _num(s: str):
    """兜底：抽取约束里的首个数字（仅当已确认两侧都是长度口径时使用）。"""
    import re
    m = re.findall(r"\d+", s or "")
    return int(m[0]) if m else None


def _len_req(s: str):
    """需求侧「长度口径」：仅接受形如「字符型：50位」的显式位数声明。

    刻意不用「首个数字」兜底 —— 需求里常见「1、前2位为项目代号」这类编号列表，
    用首个数字会得到 1 而误判冲突。
    """
    import re
    m = re.search(r"(\d+)\s*位", s or "")
    return int(m.group(1)) if m else None


def _len_d4(s: str):
    """D4 侧「长度口径」：仅接受 VARCHAR(n)/CHAR(n)/NVARCHAR(n)。"""
    import re
    m = re.search(r"(?:VAR)?(?:N)?CHAR(?:ACTER)?\s*\(\s*(\d+)\s*\)", s or "", re.I)
    return int(m.group(1)) if m else None


def _spec(s: str):
    """把口径描述归一化成 (kind, key)，用于机械比对。

    kind: len（长度）/ md（主数据引用）/ enum（枚举域）/ num（数值型）/ None（无法归类）
    刻意不做「取首个数字」这类兜底 —— 需求里大量编号列表（如「1、前2位为…」）会被误读成长度。
    """
    import re
    s = (s or "").strip()
    if not s:
        return (None, None)
    m = re.search(r"(?:VAR)?(?:N)?CHAR(?:ACTER)?\s*\(\s*(\d+)\s*\)", s, re.I)
    if m:
        return ("len", int(m.group(1)))
    m = re.search(r"(\d+)\s*位", s)
    if m and re.search(r"字符|字符串|文本|位数", s):
        return ("len", int(m.group(1)))
    # 注意：Python 的 \w 含中日韩字符，\b 在中文前后不成立，故用前后瞻排除字母数字
    m = (re.search(r"MD\s*\(\s*([A-Za-z]{2}\d+)", s)
         or re.search(r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{6})(?![A-Za-z0-9])", s))
    if m:
        return ("md", m.group(1).upper())
    if re.search(r"ENUM\s*\(", s, re.I):
        return ("enum", tuple(sorted(re.findall(r"'([^']*)'", s))))
    if re.search(r"FLOAT|DOUBLE|DECIMAL|数值|数字|实数", s, re.I):
        return ("num", None)
    return (None, None)


def _req_dataset(force: bool = False) -> dict:
    """D2 的唯一取数口：与 P7 生成侧共用 `dataset_registry` 的解析与去重。

    返回 `{rows, classes, files, sheets}`。P5 的解析 / 完备性 / 匹配 / 合成**必须**都走这里，
    否则会出现「P5 只看到一个对象类、P7 却是几十类」的口径分裂。
    """
    if not force and "ds" in _CACHE:
        return _CACHE["ds"]
    from tools import dataset_registry as reg
    from tools import eval_pipeline as ep
    ds = reg.resolve("B")
    t = reg.load_tables(ds["input_files"])
    classes = ep._dedup_classes([dict(c) for c in t["classes"]])
    valid = {ep._norm(c["name"]) for c in classes}
    attrs = ep._dedup_attrs([dict(a) for a in t["attrs"]], valid)

    rows = []
    for a in attrs:
        f = a.get("file", "")
        rows.append({
            "row": a.get("row", 0), "file": f,
            "object_class": a.get("class_name", ""), "object_code": a.get("class_code", ""),
            "attr_id": a.get("attr_id", ""), "attr_code": a.get("code", ""),
            "name": a.get("name", ""), "unit": a.get("unit", ""),
            "definition": a.get("definition", ""),
            # fmt 优先取「数据类型」（数值型（12,2）这类），空才退回「数据格式/约束」；
            # 否则 P5 会把所有数值字段判成 String（P7 对照时 87 行从 exact 掉到 partial 的根因）
            "fmt": (str(a.get("data_type") or "").strip()
                    or str(a.get("constraint") or "").strip()),
            "data_type_raw": str(a.get("data_type") or "").strip(),
            "constraint_raw": str(a.get("constraint") or "").strip(),
            "value_domain": a.get("value_domain", ""),
            "constraint_note": a.get("constraint_note", ""),
            "required": a.get("required", ""), "nullable": a.get("nullable", ""),
            "unique": a.get("unique", ""), "example": a.get("example", ""),
            "status": "", "source_span": f"{f} 行 {a.get('row', 0)}",
        })

    # 属性里出现、但对象类表没登记的分类补进来（否则导出时成孤儿）
    known = {ep._norm(c["name"]) for c in classes}
    for r in rows:
        cn = r["object_class"]
        if cn and ep._norm(cn) not in known:
            classes.append({"name": cn, "code": r["object_code"], "synthesized": True,
                            "file": r["file"]})
            known.add(ep._norm(cn))

    # 名称归一：属性行的类名可能与对象类表只是写法不同（如带括号补充说明），
    # 统一回对象类表的规范名，避免同一类被算成两个类（67 vs 68 那种错位）。
    canon = {ep._norm(c["name"]): c["name"] for c in classes if c.get("name")}
    for r in rows:
        cn = r["object_class"]
        if cn:
            r["object_class"] = canon.get(ep._norm(cn), cn)

    out = {"rows": rows, "classes": classes,
           "files": sorted({r["file"] for r in rows if r["file"]}),
           "sheets": t.get("sheets", [])}
    _CACHE["ds"] = out
    return out


def load_requirement_dataset(force: bool = False) -> dict:
    """公开取数口：`{rows, classes, files, sheets}` —— 多份合并 + (类,属性) 去重。

    P5 全流程与 P2 模式 B 抽取都走这里，保证「P2 候选数 = P5 规模 = P7 生成规模」。
    以前 P2 自己写了第二套单文件解析，导致 17 类 vs 67 类的口径分裂。
    """
    return _req_dataset(force)


def load_requirement_rows(force: bool = False) -> list[dict]:
    """拍平成逐行记录（多份合并、按 (对象类, 属性) 去重，与 P7 同口径）。"""
    if force:
        _CACHE.pop("rows", None)
        _CACHE.pop("ds", None)
    if "rows" not in _CACHE:
        _CACHE["rows"] = _req_dataset(force)["rows"]
    return _CACHE["rows"]


def load_d4_template() -> dict:
    """D4 字段基线：**只看 model_spec 槽位的显式决策**。

    - uploaded：读用户上传的 JSON（含 `fields` 或 `field_baseline` 才生效）；
    - builtin ：读内置基线 `data/templates/complete-model-template.json`；
    - off / 未选择：直接抛 DataUnavailable，不再悄悄读内置基线。
    """
    from tools import upload_manager as um
    state = um.decision("model_spec")
    if state == "uploaded":
        for f in um.active_files("model_spec"):
            if str(f).lower().endswith(".json"):
                d = json.loads(Path(f).read_text(encoding="utf-8"))
                fields = d.get("fields") or d.get("field_baseline") or []
                if fields:
                    return {x["name"]: x for x in fields if x.get("name")}
        return {}
    if state == "builtin":
        d = json.loads(D4_JSON.read_text(encoding="utf-8"))
        return {f["name"]: f for f in d.get("fields", []) if f.get("name")}
    raise um.DataUnavailable("model_spec")


def _degraded_notes() -> dict:
    """P5 顶部的降级提示。只报**真实发生**的降级，不写死。

    - 只报「确实缺数据」的降级，不把「参考库是模拟的」当警示挂在顶部：
      匹配用的参考库口径在区 3 以普通说明文字交代（见前端），不占告警位；
    - D3 预期输出是可选数据：只有确实没有时才提示「未评分」，有就不提示。
    """
    out = {}
    try:
        from tools import dataset_registry as reg
        has_gold = bool(reg.resolve("B").get("gold_files"))
    except Exception:
        has_gold = False
    if not has_gold:
        out["预期输出"] = "未评分：未提供 D3 预期输出（可选），P7 只生成不评分"
    return out


# ---------------------------------------------------------------------------
# 1) parse_requirement
# ---------------------------------------------------------------------------
def parse_requirement(force: bool = False) -> dict:
    """输出结构化需求：目标对象类、用途范围、预期属性（含空缺标记）、约束、模糊项、来源。"""
    if not force and "parsed" in _CACHE:
        return _CACHE["parsed"]
    ds = _req_dataset(force)
    rows = ds["rows"]
    classes = [c["name"] for c in ds["classes"] if c.get("name")]

    # 预期属性（按属性名聚合，保留首次出现行号作为 source_span）
    attrs: dict[str, dict] = {}
    for r in rows:
        a = attrs.setdefault(r["name"], {
            "name": r["name"], "code": r["attr_code"],
            "unit": r["unit"], "definition": r["definition"],
            "fmt": r["fmt"], "value_domain": r["value_domain"],
            "example": r["example"], "required": r["required"],
            "nullable": r["nullable"], "unique": r["unique"],
            "source_span": r["source_span"], "rows": [], "object_classes": [],
        })
        a["rows"].append(r["row"])
        if r["object_class"] and r["object_class"] not in a["object_classes"]:
            a["object_classes"].append(r["object_class"])
        # 空缺项按“任一行为空即视为空缺”（保守）
        for k in ("unit", "definition", "example", "value_domain"):
            if not a[k] and r[k]:
                a[k] = r[k]

    expected = []
    for a in attrs.values():
        empty_keys = [k for k in ("unit", "definition", "example") if not a[k]]
        expected.append({
            **a,
            "status": "missing" if empty_keys else "filled",
            "missing_fields": empty_keys,
            "row_count": len(a["rows"]),
            "class_count": len(a["object_classes"]),
        })

    constraints = [{"field": a["name"], "fmt": a["fmt"], "value_domain": a["value_domain"]}
                   for a in expected if a["fmt"] or a["value_domain"]]
    ambiguities = [{"field": a["name"], "reason": f"空缺：{'、'.join(a['missing_fields'])}",
                    "source_span": a["source_span"]}
                   for a in expected if a["missing_fields"]]

    # 逐对象类统计（P5 头部与合成模型都按类展开，口径与 P7 生成侧一致）
    files = sorted({r.get("file", "") for r in rows if r.get("file")})
    by_class: dict[str, dict] = {}
    for r in rows:
        cn = r["object_class"] or "（未标注对象类）"
        b = by_class.setdefault(cn, {"name": cn, "code": r["object_code"],
                                     "attr_rows": 0, "attrs": set(), "files": set()})
        b["attr_rows"] += 1
        b["attrs"].add(r["name"])
        if r.get("file"):
            b["files"].add(r["file"])
    by_class_list = [{"name": b["name"], "code": b["code"], "attr_rows": b["attr_rows"],
                      "attr_count": len(b["attrs"]), "files": sorted(b["files"])}
                     for b in by_class.values()]

    multi = len(classes) > 1
    res = {
        "file": files[0] if len(files) == 1 else f"{len(files)} 份需求表（合并）",
        "files": files,
        "file_count": len(files),
        "sheet": "对象类属性",
        "target_object": "" if multi else (classes[0] if classes else ""),
        "target_object_label": (f"多对象类（{len(classes)} 类）" if multi
                                else (classes[0] if classes else "")),
        "target_objects": classes,
        "target_object_code": next((r["object_code"] for r in rows if r["object_code"]), ""),
        "purpose": (f"把业务人员填写的 {len(classes)} 个对象类需求转化为可导入中台的数据标准"
                    if multi else
                    (f"把业务人员填写的「{classes[0]}」需求转化为可导入中台的数据标准"
                     if classes else "把业务人员填写的需求转化为可导入中台的数据标准")),
        "scope": {"object_classes": classes, "class_count": len(classes),
                  "attr_row_count": len(rows), "unique_attr_count": len(expected),
                  "file_count": len(files), "by_class": by_class_list},
        "expected_attributes": expected,
        "constraints": constraints,
        "ambiguities": ambiguities,
        "source_spans": ([f"{files[0]} 行 2–{rows[-1]['row']}（共 {len(rows)} 条属性行）"]
                         if len(files) == 1 and rows
                         else [f"{len(files)} 份需求表合并，共 {len(rows)} 条属性行"]),
        "degraded": _degraded_notes(),
    }
    _CACHE["parsed"] = res
    return res


# ---------------------------------------------------------------------------
# 2) gap_analyze
# ---------------------------------------------------------------------------
def gap_analyze(force: bool = False) -> dict:
    """对照内置字段基线（41 字段）做完备性分析；冲突为需求约束与基线约束逐字比对所得。"""
    if not force and "gap" in _CACHE:
        return _CACHE["gap"]
    req = parse_requirement(force)
    d4 = load_d4_template()
    by_name = {a["name"]: a for a in req["expected_attributes"]}

    gaps, dist = [], Counter()
    for fname, f in d4.items():
        a = by_name.get(fname)
        if a is None:
            dist["missing"] += 1
            gaps.append({"field": fname, "code": f.get("code", ""), "status": "missing",
                         "current_value": None,
                         "reason": "内置字段基线要求该字段，但需求文件未提供",
                         "d4_spec": f"{f.get('data_type','')} / {f.get('constraint','')}"})
            continue
        # 冲突判定：需求「数据格式/约束」与 D4 约束长度/取值逐项比对。
        # 只比对可机械比较的“数值口径”（如 字符型：50位 vs VARCHAR(256)），
        # 无法机械比对的一律降级为 ambiguous，不做主观判定。
        d4c = (f.get("constraint") or "").strip()
        rq = (a["fmt"] or "").strip() or (a["value_domain"] or "").strip()
        k1, v1 = _spec(rq)
        k2, v2 = _spec(d4c)
        warns = [f"{k} 为空" for k in ("definition", "unit") if not a[k]]
        if k1 and k1 == k2:
            if v1 == v2:
                dist["filled"] += 1
                gaps.append({"field": fname, "code": f.get("code", ""), "status": "filled",
                             "current_value": rq,
                             "reason": f"需求「{rq}」与 D4「{d4c}」的口径一致（{k1}）",
                             "warnings": warns, "source_span": a["source_span"]})
                continue
            dist["conflict"] += 1
            gaps.append({"field": fname, "code": f.get("code", ""), "status": "conflict",
                         "current_value": rq,
                         "reason": f"需求「{rq[:40]}」与 D4「{d4c}」的 {k1} 口径不一致（{v1} vs {v2}）",
                         "conflict_detail": {"requirement": rq, "existing": d4c,
                                             "kind": k1, "requirement_value": str(v1),
                                             "existing_value": str(v2),
                                             "field": fname, "source_span": a["source_span"]},
                         "warnings": warns, "source_span": a["source_span"]})
            continue
        dist["ambiguous"] += 1
        gaps.append({"field": fname, "code": f.get("code", ""), "status": "ambiguous",
                     "current_value": rq or None,
                     "reason": ("需求未给出可机械比对的数据格式/值域" if not rq
                                else f"需求「{rq}」与 D4「{d4c}」无法机械比对（缺数值口径），需人工确认"),
                     "warnings": warns, "source_span": a["source_span"],
                     "d4_spec": f"{f.get('data_type','')} / {d4c}"})

    total = len(d4)
    w = {"filled": 1.0, "ambiguous": 0.5, "conflict": 0.3, "missing": 0.0}
    score = round(sum(w[g["status"]] for g in gaps) / total * 100, 1)

    res = {
        "completeness_score": score,
        "formula": FORMULA,
        "weights": w,
        "template_field_total": total,
        "gaps": gaps,
        "status_dist": dict(dist),
        "blocking": [g["field"] for g in gaps if g["status"] in ("missing", "conflict")],
        "note": "未处理（missing/conflict）的字段不得进入模型合成，需先澄清或消解。",
    }
    _CACHE["gap"] = res
    return res


# ---------------------------------------------------------------------------
# 3) generate_clarification
# ---------------------------------------------------------------------------
def generate_clarification(force: bool = False, use_llm: bool = True) -> dict:
    """对缺失/模糊项生成澄清问题 + AI 自动推断（必附 basis 与 confidence）。"""
    if not force and "clar" in _CACHE:
        return _CACHE["clar"]
    gap = gap_analyze(force)
    req = parse_requirement(force)
    d4 = load_d4_template()
    lib = la.load_mock_library("B")

    # 词表归一（同义词 → 规范名）
    syn2can, _ = ke.load_domain_vocab()

    questions, inferences = [], []
    for g in gap["gaps"]:
        if g["status"] not in ("missing", "ambiguous", "conflict"):
            continue
        f = d4.get(g["field"], {})
        questions.append({
            "field": g["field"],
            "question": f"「{g['field']}」的口径应如何确定？D4 模板建议为 "
                        f"{f.get('data_type','')} / {f.get('constraint','')}，请确认或给出业务口径。",
            "options": [f.get("constraint", ""), "维持业务现有口径", "待定（转专家）"],
        })

        # 推断来源优先级：参考底座模拟库 > 内置字段基线
        basis, conf, value = None, 0.0, None
        canon = syn2can.get(g["field"], g["field"])
        for term in (g["field"], canon):
            if term in lib["field_index"]:
                rows = lib["field_index"][term]
                specs = {(r["data_type"], r["constraint"], r["unit"]) for r in rows}
                if len(specs) == 1:
                    dt, cons, unit = next(iter(specs))
                    value = {"data_type": dt, "constraint": cons, "unit": unit}
                    basis = f"rag_graph/rag_vector：参考底座模拟库「{term}」在 {len(rows)} 个对象类下口径唯一（{dt} / {cons}）"
                    conf = 0.9
                else:
                    value = {"data_type": sorted(specs)[0][0], "constraint": sorted(specs)[0][1],
                             "unit": sorted(specs)[0][2]}
                    basis = (f"rag_graph：参考底座模拟库「{term}」存在 {len(specs)} 种口径"
                             f"（需人工裁定），此处取其一作为建议")
                    conf = 0.55
                break
        if basis is None and f:
            value = {"data_type": f.get("data_type", ""), "constraint": f.get("constraint", ""),
                     "unit": f.get("unit", "")}
            basis = f"内置字段基线（data/templates/complete-model-template.json）字段 {g['field']}"
            conf = 0.7

        inferences.append({
            "field": g["field"],
            "inferred_value": value,
            "confidence": conf,
            "basis": basis or "无可用依据",
            "reference_only": conf < CONFIDENCE_REFERENCE_ONLY,
            "source": "inference",
        })

    # ------------------------------------------------------------------
    # 大模型补写（有配置才跑；无配置保持纯规则，并在返回值里说明）
    # 只把「字段名 + 已有格式 + 已有单位」发给模型，不发原文，避免文档外泄。
    # ------------------------------------------------------------------
    llm_meta = {"available": False, "used": False, "reason": "", "model": ""}
    try:
        from tools import llm_client as llm
        st = llm.status()
        llm_meta.update(available=st["available"], reason=st.get("reason", ""))
        if use_llm and st["available"] and inferences:
            targets = []
            for inf in inferences[:12]:
                row = next((r for r in req["expected_attributes"] if r["name"] == inf["field"]), None)
                targets.append({"name": inf["field"],
                                "fmt": (row or {}).get("fmt", ""),
                                "unit": (row or {}).get("unit", ""),
                                "definition": (row or {}).get("definition", ""),
                                "class_name": ((row or {}).get("object_classes") or [""])[0]})
            try:
                got = llm.suggest_definitions(targets, max_items=12, tag="p5.clarify")
                by_name = {g["name"]: g for g in got["items"]}
                for inf in inferences:
                    g = by_name.get(inf["field"])
                    if not g:
                        continue
                    inf["llm_definition"] = g["definition"]
                    inf["llm_unit"] = g["unit"]
                    inf["llm_data_type"] = g["data_type"]
                    inf["llm_reason"] = g["reason"]
                    if g["definition"]:
                        inf["basis"] = (f"{inf['basis']}；另由大模型 {got['model']} 补写定义"
                                        f"（置信 {g['confidence']:.2f}）")
                        inf["llm_confidence"] = g["confidence"]
                llm_meta.update(used=True, model=got["model"],
                                elapsed_ms=got["elapsed_ms"], trace_id=got["trace_id"],
                                suggestions=len(got["items"]))
            except Exception as e:
                llm_meta.update(used=False, reason=f"调用失败：{e}")
    except Exception as e:
        llm_meta.update(available=False, reason=f"接入层不可用：{e}")

    res = {
        "questions": questions, "inferences": inferences,
        "confidence_reference_only_threshold": CONFIDENCE_REFERENCE_ONLY,
        "note": f"confidence < {CONFIDENCE_REFERENCE_ONLY} 的推断仅作参考，不得直接采纳；"
                f"所有推断标 source: inference，可逐项驳回。",
        "term_normalized": True,
        "llm": llm_meta,
    }
    _CACHE["clar"] = res
    return res


# ---------------------------------------------------------------------------
# 4) 已有标准匹配（rag_vector + rag_graph）
# ---------------------------------------------------------------------------
def match_existing(force: bool = False) -> dict:
    """区 3：调 rag_vector（语义召回）+ rag_graph（图谱校验），给出可复用/需合并/确为新增。"""
    if not force and "match" in _CACHE:
        return _CACHE["match"]
    req = parse_requirement(force)
    lib = la.load_mock_library("B")
    # D0 存量字段索引：字段名 → [{class_name, data_type, constraint, unit}]
    # 这是「是否已存在于标准库」的唯一判据（图谱里混了本轮输入，不能用）
    lib_fields = lib.get("field_index") or {}
    g = ob.get_graph("B")
    graph_by_label = defaultdict(list)
    for n in g["nodes"]:
        if n.get("type") == "field":
            graph_by_label[n.get("label", "")].append(n)

    items = []
    # rag_vector：本地向量召回（2026-10-23 从 Dice 字面相似换成本地嵌入）
    from tools import local_embedding as le
    idx = le.library_index("B")

    for a in req["expected_attributes"]:
        name = a["name"]
        hit = idx.best(name) if idx.items else None
        best, best_name = (hit["score"], hit["key"]) if hit else (0.0, "")
        # rag_graph：多跳图谱检索（原来只是「同名节点查一遍」，没有跳数、没有路径）
        gq = ob.graph_query(name, "B", topk=3, hop=2)
        direct = [m for m in gq.get("matched", [])
                  if m.get("hop") == 0 and m.get("type") in ("field", "candidate")]
        # 路径里排除"同名节点"——同名不算关联证据，只说明底座里有同名候选
        near = [p for p in (gq.get("paths") or []) if p.get("target") != name]
        # 「是否已在标准库」只能由 D0 存量库回答。
        # 图谱是 D0 + 本轮输入，同名节点很可能就来自本轮需求表自己——
        # 拿图谱判「可复用」等于自证，正是第 4 条要防的循环论证。
        d0_same = lib_fields.get(name, [])
        graph_same = graph_by_label.get(name, [])
        if d0_same:
            gn = d0_same
            specs = {(x.get("data_type"), x.get("constraint"), x.get("unit")) for x in d0_same}
            if len(specs) > 1:
                verdict = "需合并"
                gcheck = (f"D0 已有标准库中该字段挂在 {len(d0_same)} 个对象类下，"
                          f"存在 {len(specs)} 种口径，需统一")
            else:
                verdict = "可复用"
                gcheck = f"D0 已有标准库中 {len(d0_same)} 处同名定义，口径一致"
        else:
            gn = graph_same
            verdict = "确为新增"
            if graph_same:
                gcheck = (f"D0 存量库无此字段；底座图谱里的 {len(graph_same)} 个同名节点"
                          f"来自本轮输入自身（口径未审，不作复用依据）")
            elif direct:
                gcheck = (f"D0 存量库无此字段；图谱仅有 P2 抽取候选节点「{name}」"
                          f"（{len(direct)} 个），口径未定")
            else:
                gcheck = ("图谱中无同名字段节点" if not near else
                          f"图谱无同名节点；最近关联「{near[0]['target']}」（{near[0]['hops']} 跳，"
                          f"经 {'→'.join(near[0]['via'])}，得分 {near[0]['score']}）")

        items.append({
            "field": name,
            "similarity": best,
            # 低于阈值只作"最近邻参考"，不算匹配到已有标准
            "matched_standard": (best_name or None) if best >= la.VECTOR_THRESHOLD else None,
            "nearest_hint": (f"{best_name}（余弦 {best}，低于阈值 {la.VECTOR_THRESHOLD}，仅作参考）"
                             if best_name and best < la.VECTOR_THRESHOLD else None),
            "matched_standard_id": ((lib["field_index"].get(best_name) or [{}])[0].get("attr_id")
                                    or (lib["field_index"].get(best_name) or [{}])[0].get("std_id"))
                                   if (best >= la.VECTOR_THRESHOLD and best_name in lib["field_index"]) else
                                   ((lib["classes"].get(best_name, {}).get("id"))
                                    if (best >= la.VECTOR_THRESHOLD and best_name in lib["classes"]) else None),
            "verdict": verdict,
            "graph_check": gcheck,
            "graph_node_count": len(gn),
            "graph_hops": (near[0]["hops"] if (not direct and near) else (0 if direct else None)),
            "graph_path": ([x for x in (near[0]["path_labels"] if near else [])] or None),
            "merge_preview": (f"统一为参考库口径：{sorted(specs)[0]}"
                              if (verdict == "需合并" and gn) else None),
            "source_span": a["source_span"],
        })

    dist = Counter(i["verdict"] for i in items)
    res = {
        "items": items, "verdict_dist": dict(dist),
        "retrieval": ("rag_vector（本地向量：字 n-gram TF-IDF + 随机投影，余弦召回）"
                      "+ rag_graph（多跳路径传播，2 跳，按边类型加权）"),
        "retrieval_impl": le.describe(),
        "library": {"source": lib["source"],
                    "source_short": lib.get("source_short") or lib["source"],
                    "note": lib["source_note"],
                    "class_count": lib["class_count"], "field_count": lib["field_count"]},
    }
    _CACHE["match"] = res
    return res


if __name__ == "__main__":
    r = parse_requirement()
    print("目标对象类:", r["target_object"], "| 对象类数:", r["scope"]["class_count"],
          "| 属性行:", r["scope"]["attr_row_count"], "| 唯一属性:", r["scope"]["unique_attr_count"])
    g = gap_analyze()
    print("完备性评分:", g["completeness_score"], "| 分布:", g["status_dist"])
    print("   blocking:", g["blocking"][:6])
    c = generate_clarification()
    print("澄清问题:", len(c["questions"]), "| 推断:", len(c["inferences"]))
    for x in c["inferences"][:3]:
        print("   ", x["field"], "conf", x["confidence"], "|", x["basis"][:70])
    m = match_existing()
    print("匹配判定分布:", m["verdict_dist"])
    for x in m["items"][:4]:
        print("   ", x["field"], "→", x["verdict"], "| sim", x["similarity"], "|", x["graph_check"][:40])
