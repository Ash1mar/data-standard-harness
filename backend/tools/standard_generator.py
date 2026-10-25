"""
standard_generator.py
======================
核电数据标准智能治理智能体（双模式）· 标准草案生成（增量 6 · P6）。

绑定工具（见 .dsh/roles/standard-generator.md）：standard_draft / rag_vector / rag_graph。

双入口（SKILL 2.8）：
- 入口 A：从 P4 立项清单（deliverables/mode-a/approved-candidates/latest.json）批量生成草案，
  标注「源自发现式梳理，需业务确认立项」。
- 入口 B：从 P5 合成模型（deliverables/mode-b/final-models/latest.json）生成正式条目，
  标注「源自需求转化，已通过完备性校验」。

诚实、可追溯的设计原则：
- `field_specs` 缺任一必填字段（name / code / data_type / constraint）即标 `INCOMPLETE`，
  不静默补默认值。
- `quality_score` 的 accuracy 维度：D7（人工标注）缺失时**返回 None 并注明「未验证」**，
  不得用模型自评分数冒充（SKILL 1.3.4 / 2.9）。
- 标准可使用率 = COMPLETE 字段数 / 字段总数，实时重算。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import knowledge_extractor as ke   # noqa: E402
from tools import landscape_analyst as la     # noqa: E402
from tools import ontology_builder as ob      # noqa: E402

DELIVERABLES = Path(__file__).resolve().parent.parent.parent / "deliverables"
APPROVED = DELIVERABLES / "mode-a" / "approved-candidates" / "latest.json"
FINAL_MODEL = DELIVERABLES / "mode-b" / "final-models" / "latest.json"
STANDARD_DIR = DELIVERABLES / "shared" / "standards"

# field_specs 必填字段（缺任一 → INCOMPLETE）
# 说明：`code` 不再作为硬性必填 —— 业务人员填的需求表本来就不给编码，
# 编码由 D4 规则在导出时分配（对象类 = 大类缩写+6 位，属性 = Attr+6 位）。
# 把 code 当必填会让「可使用率」恒为 0，掩盖真正的缺口（定义/单位）。
REQUIRED_SPEC_FIELDS = ["name", "data_type", "constraint"]
# 规范性维度检查项（缺失计 warning，不直接判 INCOMPLETE）
NORMATIVE_FIELDS = ["definition", "unit", "code"]

_STANDARDS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 读入上游产物
# ---------------------------------------------------------------------------
def load_approved() -> dict:
    if not APPROVED.exists():
        raise FileNotFoundError("缺少 P4 立项清单：请先在 P4 勾选候选并提交立项清单")
    return json.loads(APPROVED.read_text(encoding="utf-8"))


def load_final_model() -> dict:
    if not FINAL_MODEL.exists():
        raise FileNotFoundError("缺少 P5 最终模型：请先在 P5 完成合成并导出最终数据模型")
    return json.loads(FINAL_MODEL.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 质量评价（SKILL 5.1 quality_score）
# ---------------------------------------------------------------------------
# 可机检的口径枚举（准确性维度的判定依据，全部来自 D4 建模规则 + 通用量纲）
_VALID_TYPES = {
    "string", "str", "varchar", "char", "text", "varchar2",
    "int", "integer", "bigint", "smallint", "long",
    "float", "double", "decimal", "number", "numeric", "real",
    "boolean", "bool", "date", "datetime", "timestamp", "time",
}
_UNIT_WHITELIST = {
    "mm", "cm", "m", "km", "in", "inch", "μm", "um", "nm",
    "kg", "g", "t", "mg", "lb", "ton",
    "mpa", "kpa", "pa", "bar", "kgf/cm2", "n", "kn",
    "℃", "°c", "k", "℉",
    "%", "‰", "ppm",
    "dn", "pn", "class", "级", "度", "s", "min", "h", "d", "a",
    "v", "kv", "a", "ma", "kw", "w", "hz", "m3", "m2", "l", "ml",
    "元", "万元", "个", "件", "套", "台", "根", "张", "米", "吨", "千克",
}
_PLACEHOLDER = re.compile(r"^[（(].*[)）]$|未填写|待补|N/?A|TBD|^-+$")
_CONSTRAINT_OK = re.compile(
    r"^(varchar|char|nvarchar|string|text)\s*[（(]?\s*\d*\s*[)）]?$"
    r"|^(decimal|numeric|number)\s*[（(]\s*\d+\s*(,\s*\d+\s*)?[)）]$"
    r"|^(int|integer|bigint|smallint|float|double|date|datetime|timestamp|boolean|bool)$"
    r"|^[\[(]\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*[\])]$"     # 数值区间
    r"|^[（(].*[)）]$"                                          # 括号枚举
    r"|^[\u4e00-\u9fffA-Za-z0-9_]+(\s*[,，、/]\s*[\u4e00-\u9fffA-Za-z0-9_]+)+$",  # 枚举列举
    re.I)


def _cons_parseable(cons: str) -> bool:
    """约束能否被解析成「技术格式」。

    只认结构化写法（VARCHAR(50)/DECIMAL(12,2)/区间/枚举列举/基本类型）。
    像「按 GB/T 706 执行」这类业务规则文本算**不可解析**——不是错，但不能直接入库，
    这正是准确性维度要区分的东西。原来这里有个 `len(c) >= 2` 兜底，
    等于让所有非空约束一律通过（实测 100%），指标失去区分度，已删除。
    """
    c = (cons or "").strip()
    if not c:
        return False
    return bool(_CONSTRAINT_OK.match(c))


def _type_norm(dt: str) -> str:
    d = (dt or "").strip().lower()
    d = re.sub(r"[（(].*?[)）]", "", d).strip()
    return d


def _is_numeric_type(dt: str) -> bool:
    return _type_norm(dt) in {"int", "integer", "bigint", "smallint", "long",
                              "float", "double", "decimal", "number", "numeric", "real"}


def _machine_checks(spec: dict) -> list[tuple[str, bool]]:
    """单条字段的可机检准确性检查项（只做能客观判定的，不做语义自评）。

    返回 [(检查项, 通过与否)]；无法判定的项**不下发**（不计入分母，避免用空值刷分）。
    """
    items: list[tuple[str, bool]] = []
    dt = _type_norm(spec.get("data_type", ""))
    cons = str(spec.get("constraint", "") or "").strip()
    unit = str(spec.get("unit", "") or "").strip()
    name = str(spec.get("name", "") or "").strip()
    example = str(spec.get("example", "") or "").strip()

    # 类型 / 约束对每个字段都要判（空值 = 不可判定 = 不通过，不能靠"没填就不检查"刷分）
    items.append(("数据类型在 D4 类型表内", dt in _VALID_TYPES))
    items.append(("约束可被解析为技术格式", _cons_parseable(cons)))
    if unit:
        items.append(("量纲在标准量纲表内", unit.lower() in _UNIT_WHITELIST
                      or unit.replace(" ", "").lower() in _UNIT_WHITELIST))
        # 数值型带单位、文本型带单位都正常，这里只查「数值型缺单位」的相反面：
        # 字符型却标了物理量纲（如 mm）→ 口径可疑
        if not _is_numeric_type(dt) and unit.lower() in _UNIT_WHITELIST and dt:
            items.append(("量纲与数据类型不矛盾", unit.lower() in {"级", "度", "个", "件", "套", "台", "元"}))
    if example:
        if _is_numeric_type(dt):
            items.append(("示例值符合数据类型", bool(re.fullmatch(r"-?\d+(\.\d+)?", example.replace(",", "")))))
        else:
            items.append(("示例值非占位符", not _PLACEHOLDER.match(example)))
    if name:
        items.append(("名称非占位符", not _PLACEHOLDER.match(name)))
    return items


def quality_score(specs: list[dict], manual_flags: list[str] | None = None,
                  human_accuracy: float | None = None) -> dict:
    """三维质量评价 {completeness, normativity, accuracy, manual_review_required}。

    2026-10-23 修正：原来 `accuracy` 恒为 None、只有两维，且 note 误引 D7
    （D7 是业务域，不是人工标注）。现在第三维落地为**可机检口径准确率**：
    逐字段检查「类型是否在 D4 类型表内 / 约束是否可解析 / 量纲是否规范 /
    示例值是否符合类型 / 名称是否占位符」，通过项 ÷ 判定项。
    有人工标注（human_accuracy）时优先采用人工口径，并在 note 里标明来源。
    与答案比对的准确率仍在 P7，不在这里冒名顶替。
    """
    n = len(specs) or 1
    complete = sum(1 for s in specs if s.get("status") == "COMPLETE")
    completeness = round(complete / n * 100, 1)

    norm_hits = 0
    for s in specs:
        for f in NORMATIVE_FIELDS:
            if str(s.get(f, "") or "").strip():
                norm_hits += 1
    normativity = round(norm_hits / (n * len(NORMATIVE_FIELDS)) * 100, 1)

    # 准确性维度：可机检项
    ok = total = 0
    fail_counter: Counter = Counter()
    for s in specs:
        for label, passed in _machine_checks(s):
            total += 1
            if passed:
                ok += 1
            else:
                fail_counter[label] += 1
    machine_acc = round(ok / total * 100, 1) if total else None
    if human_accuracy is not None:
        accuracy = round(float(human_accuracy), 1)
        accuracy_basis = "human"
        accuracy_note = "采用人工标注口径（human_accuracy 传入）。"
    else:
        accuracy = machine_acc
        accuracy_basis = "machine"
        accuracy_note = (f"可机检口径准确率：{ok}/{total} 项通过（类型合法 / 约束可解析 / "
                         f"量纲规范 / 示例值合类型 / 名称非占位符）。"
                         "与预期输出比对的准确率见 P7，不使用模型自评分。")

    manual = bool(manual_flags) or completeness < 100 or normativity < 70 or (
        accuracy is not None and accuracy < 80)
    return {
        "completeness": completeness,
        "normativity": normativity,
        "accuracy": accuracy,
        "accuracy_basis": accuracy_basis,
        "accuracy_note": accuracy_note,
        "accuracy_checked": {"passed": ok, "total": total,
                             "failed_by_item": dict(fail_counter)},
        "manual_review_required": manual,
        "manual_reasons": (manual_flags or []) + (
            [f"{complete}/{n} 字段 COMPLETE"] if completeness < 100 else []
        ) + ([f"规范性 {normativity} < 70"] if normativity < 70 else [])
        + ([f"口径准确率 {accuracy} < 80"] if (accuracy is not None and accuracy < 80) else []),
    }


def usability(specs: list[dict]) -> dict:
    """两档口径，防止只用「完全合规率」看低整体：

    - rate：完全合规（名称 + 类型 + 约束齐全），业务表通常很低，因为约束多是业务规则文本；
    - structure_rate：结构可入库（名称 + 类型齐全），反映"至少能导入中台"的比例。
    """
    n = len(specs)
    c = sum(1 for s in specs if s.get("status") == "COMPLETE")
    st = sum(1 for s in specs
             if str(s.get("name") or "").strip() and str(s.get("data_type") or "").strip())
    return {"complete_fields": c, "total_fields": n,
            "rate": round(c / n * 100, 1) if n else 0.0,
            "structure_complete": st,
            "structure_rate": round(st / n * 100, 1) if n else 0.0,
            "gaps": {
                "missing_constraint": sum(1 for s in specs if not str(s.get("constraint") or "").strip()),
                "missing_definition": sum(1 for s in specs if not str(s.get("definition") or "").strip()),
                "missing_unit": sum(1 for s in specs if not str(s.get("unit") or "").strip()),
                "missing_code": sum(1 for s in specs if not str(s.get("code") or "").strip()),
            }}


def _spec_from(name: str, code: str, data_type: str, constraint: str,
               unit: str, definition: str, source: str, extra: dict | None = None) -> dict:
    fields = {"name": name, "code": code, "data_type": data_type,
              "constraint": constraint, "unit": unit, "definition": definition}
    missing = [f for f in REQUIRED_SPEC_FIELDS if not str(fields.get(f) or "").strip()]
    spec = {
        "name": name, "code": code, "data_type": data_type, "constraint": constraint,
        "unit": unit or "", "definition": definition or "",
        "source": source,
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "missing": missing,
        "warnings": [f"{f} 为空" for f in NORMATIVE_FIELDS
                     if not str(fields.get(f) or "").strip()],
    }
    if extra:
        spec.update(extra)
    return spec


# ---------------------------------------------------------------------------
# 入口 A：P4 立项清单 → 标准草案
# ---------------------------------------------------------------------------
def draft_from_mode_a() -> dict:
    appr = load_approved()
    lib = la.load_mock_library()
    syn2can, _ = ke.load_domain_vocab()
    specs, mapping = [], []

    for c in appr.get("candidates", []):
        term = c.get("term") or c.get("name")
        nv = c.get("novelty") or {}
        # 模拟库里查同名条目，尽量给出 code / data_type / constraint
        code, dt, cons, unit, defn = "", "", "", "", c.get("definition") or ""
        src_extra = {}
        if term in lib["field_index"]:
            rows = lib["field_index"][term]
            r0 = rows[0]
            code, dt, cons, unit = r0["attr_id"], r0["data_type"], r0["constraint"], r0["unit"]
            if not defn:
                defn = f"（取自参考底座模拟库「{term}」）"
            if len({(x["data_type"], x["constraint"]) for x in rows}) > 1:
                src_extra["note"] = f"参考底座模拟库中「{term}」有 {len(rows)} 处且口径不一致，需人工裁定"
        elif term in lib["classes"]:
            k = lib["classes"][term]
            code = k["code"]
            defn = defn or k.get("definition", "")
            src_extra["note"] = "命中模拟库对象类，字段级口径待补"

        canon = syn2can.get(term, term)
        mapping.append({"from": term, "to": canon, "kind": "term_normalize",
                        "confidence": 0.95 if canon == term else 0.7,
                        "basis": "内置同义词归一" if canon != term else "无需归一"})
        if nv.get("matched_standard_id"):
            mapping.append({"from": term, "to": nv["matched_standard_id"],
                            "kind": "existing_standard", "confidence": nv.get("similarity", 0),
                            "basis": f"novelty_check 判定 {nv.get('status')}"})

        specs.append(_spec_from(
            term, code, dt, cons, unit, defn,
            "P4-立项候选",
            {"novelty": nv.get("status"), "confidence": c.get("confidence"),
             "priority": c.get("priority"), "candidate_id": c.get("id"), **src_extra},
        ))

    std = _assemble("A", appr.get("candidates", [])[0].get("term", "") if appr.get("candidates") else "",
                    specs, mapping,
                    "源自发现式梳理，需业务确认立项")
    return std


# ---------------------------------------------------------------------------
# 入口 B：P5 合成模型 → 正式条目
# ---------------------------------------------------------------------------
def draft_from_mode_b() -> dict:
    """P5 最终模型 → 标准条目。**按对象类展开**（多类时每条字段带 class_name）。"""
    m = load_final_model()
    models = m.get("models") or []
    if not models:      # 兼容旧产物（单类模型）
        models = [{"object_class": m.get("object_class", ""),
                   "object_class_code": m.get("object_class_code", ""),
                   "attributes": m.get("attributes", [])}]

    specs, mapping = [], []
    for mo in models:
        cn = mo.get("object_class", "")
        cc = mo.get("object_class_code", "")
        for a in mo.get("attributes", []) or []:
            spec = _spec_from(a.get("name", ""), a.get("code", ""), a.get("data_type", ""),
                              a.get("constraint", ""), a.get("unit", ""), a.get("definition", ""),
                              a.get("source", ""),
                              {"synthesized_value": a.get("synthesized_value"),
                               "confidence": a.get("confidence"),
                               "biz_constraint": a.get("biz_constraint", ""),
                               "warnings": a.get("warnings") or []})
            if a.get("source") in ("inference", "answered", "resolved"):
                spec["note"] = a.get("source_note", "")
            spec["class_name"] = cn
            spec["class_code"] = cc
            specs.append(spec)
            mapping.append({"from": a.get("name", ""), "to": a.get("code", ""),
                            "kind": "field_code", "confidence": 1.0,
                            "basis": "D4 规则（属性ID = Attr+6 位；业务未填则待分配）"})

    n_cls = len([mo for mo in models if mo.get("attributes")])
    label = (f"多对象类（{len(models)} 类，{n_cls} 类有属性行）" if len(models) > 1
             else (models[0].get("object_class", "") if models else ""))
    no_req = m.get("field_completeness", {}).get("classes_without_requirement", 0)
    note = "源自需求转化（D2 全部需求表合并）"
    if no_req:
        note += f"；{no_req} 个对象类在需求表中无属性行，待业务补填"

    std = _assemble("B", label, specs, mapping, note)
    std["object_class_code"] = "" if len(models) > 1 else m.get("object_class_code", "")
    std["scope"] = m.get("scope", {})
    # 记录本轮输入快照：P7「对照 P6 产物」时用它可以判断产物是不是同一批数据生成的
    try:
        from tools import dataset_registry as _reg
        _files = [str(f).replace("\\", "/").split("/")[-1]
                  for f in _reg.resolve("B").get("input_files", [])]
        std["scope"] = {**std["scope"], "input_files": sorted(_files),
                        "snapshot_at": datetime.now().isoformat(timespec="seconds")}
    except Exception:
        pass
    std["class_count"] = len([mo for mo in models if mo.get("attributes")])
    std["classes"] = [{"name": mo.get("object_class", ""),
                       "code": mo.get("object_class_code", ""),
                       "parent": mo.get("parent", ""),
                       "attr_count": len(mo.get("attributes") or [])} for mo in models]
    # 关系随产物一起出：P7 的关系层要拿它对 D3 评（原来 P6 只出字段，关系全丢）
    rels = m.get("relations") or []
    if not rels:      # 兼容旧 final-model（无 relations）：至少把字段归属补成关系
        rels = [{"type": "hasAttribute", "source": s.get("class_name", ""), "target": s["name"]}
                for s in specs if s.get("class_name") and s.get("name")]
    std["relations"] = rels
    std["relation_counts"] = dict(Counter(r["type"] for r in rels))
    return std


def _assemble(path: str, object_name: str, specs: list[dict], mapping: list[dict], note: str) -> dict:
    sid = f"STD-{path}-{datetime.now().strftime('%H%M%S')}"
    std = {
        "standard_id": sid,
        "source_path": path,
        "source_note": note,
        "object_class_name": object_name,
        "definition": "",
        "attribute_list": [s["name"] for s in specs],
        "field_specs": specs,
        "mapping_suggestions": mapping,
        "quality": quality_score(specs),
        "usability": usability(specs),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _STANDARDS[sid] = std
    return std


# ---------------------------------------------------------------------------
# 人工补全 INCOMPLETE
# ---------------------------------------------------------------------------
def complete_standard(standard_id: str, patches: dict) -> dict:
    """patches: {field_name: {data_type:..., constraint:..., code:..., definition:..., unit:...}}"""
    std = _STANDARDS.get(standard_id)
    if not std:
        if not STANDARD_DIR.exists():
            raise KeyError(f"未找到标准 {standard_id}（进程内无缓存）")
        p = STANDARD_DIR / f"{standard_id}.json"
        if not p.exists():
            raise KeyError(f"未找到标准 {standard_id}")
        std = json.loads(p.read_text(encoding="utf-8"))
        _STANDARDS[standard_id] = std

    applied = []
    for s in std["field_specs"]:
        patch = patches.get(s["name"])
        if not patch:
            continue
        for k, v in patch.items():
            if k in ("name", "code", "data_type", "constraint", "definition", "unit"):
                s[k] = v
        s["missing"] = [f for f in REQUIRED_SPEC_FIELDS if not str(s.get(f, "") or "").strip()]
        s["status"] = "COMPLETE" if not s["missing"] else "INCOMPLETE"
        s["warnings"] = [f"{f} 为空" for f in NORMATIVE_FIELDS if not str(s.get(f, "") or "").strip()]
        s["human_completed"] = True
        applied.append(s["name"])

    std["quality"] = quality_score(std["field_specs"])
    std["usability"] = usability(std["field_specs"])
    std["completed_at"] = datetime.now().isoformat(timespec="seconds")
    _STANDARDS[standard_id] = std
    save_standard(std)

    return {"standard_id": standard_id, "applied": applied,
            "quality": std["quality"], "usability": std["usability"],
            "remaining_incomplete": [s["name"] for s in std["field_specs"] if s["status"] == "INCOMPLETE"]}


# ---------------------------------------------------------------------------
# 大模型补全（P6）：给缺失定义 / 单位的字段生成内容
# ---------------------------------------------------------------------------
def enrich_with_llm(standard_id: str, limit: int = 60, batch: int = 12,
                    workers: int = 4) -> dict:
    """并发分批调用大模型补 definition / unit。**只填空值，不覆盖业务已填内容**。

    批量任务默认用 `bulk_model`（如 qwen3.8-flash）：实测单批 12 条约 28s，
    855 条串行要 30+ 分钟，4 并发约 8 分钟；`limit` 控制单次规模，避免一次跑爆。
    """
    import concurrent.futures as cf
    from tools import llm_client as llm
    if not llm.available():
        raise RuntimeError(llm.status().get("reason") or "大模型未配置")

    std = get_standard(standard_id)
    if std is None:
        p = STANDARD_DIR / f"{standard_id}.json"
        if not p.exists():
            raise FileNotFoundError(f"未找到标准 {standard_id}")
        std = json.loads(p.read_text(encoding="utf-8"))

    bulk_model = (llm.config().get("bulk_model") or "").strip() or None
    specs = std.get("field_specs") or []

    def _no_def(s): return not str(s.get("definition") or "").strip()
    def _no_unit(s): return not str(s.get("unit") or "").strip()

    # 优先补「定义」（真正的规范缺口），再补「单位」；
    # 很多字段本来就没有单位（名称类），模型返回空是对的，不该算失败。
    targets = ([s for s in specs if _no_def(s)] + [s for s in specs if not _no_def(s) and _no_unit(s)])
    targets = targets[:max(1, limit)]

    chunks = [targets[i:i + batch] for i in range(0, len(targets), batch)]

    def run_chunk(idx: int):
        chunk = chunks[idx]
        items = [{"name": s.get("name", ""), "fmt": s.get("constraint", ""),
                  "unit": s.get("unit", ""), "definition": s.get("definition", ""),
                  "class_name": s.get("class_name", "")} for s in chunk]
        got = llm.suggest_definitions(items, max_items=len(chunk),
                                      tag=f"p6.define.{standard_id}.{idx}",
                                      model=bulk_model, timeout=180)
        return idx, got

    filled_def, filled_unit, batches, errors, model = 0, 0, 0, [], ""
    if chunks:
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(run_chunk, i): i for i in range(len(chunks))}
            for fut in cf.as_completed(futs):
                i = futs[fut]
                try:
                    _, got = fut.result()
                except Exception as e:
                    errors.append(str(e))
                    continue
                batches += 1
                model = got.get("model") or model
                by = {g["name"]: g for g in got.get("items") or []}
                for s in chunks[i]:
                    g = by.get(s.get("name", ""))
                    if not g:
                        continue
                    if g.get("definition") and not str(s.get("definition") or "").strip():
                        s["definition"] = g["definition"]
                        s["source"] = "llm"
                        s["llm_confidence"] = g.get("confidence")
                        s["llm_reason"] = g.get("reason", "")
                        filled_def += 1
                    if g.get("unit") and not str(s.get("unit") or "").strip():
                        s["unit"] = g["unit"]
                        filled_unit += 1

    # 重算状态（模型只补了 definition/unit，必填项没变，但 warnings 要更新）
    for s in specs:
        fields = {"name": s.get("name"), "data_type": s.get("data_type"),
                  "constraint": s.get("constraint"), "unit": s.get("unit"),
                  "definition": s.get("definition"), "code": s.get("code")}
        s["missing"] = [f for f in REQUIRED_SPEC_FIELDS
                        if not str(fields.get(f) or "").strip()]
        s["status"] = "COMPLETE" if not s["missing"] else "INCOMPLETE"
        s["warnings"] = [f"{f} 为空" for f in NORMATIVE_FIELDS
                         if not str(fields.get(f) or "").strip()]

    std["quality"] = quality_score(specs)
    std["usability"] = usability(specs)
    std["llm"] = {"model": model, "batches": batches,
                  "filled_definition": filled_def, "filled_unit": filled_unit,
                  "errors": errors[:3],
                  "at": datetime.now().isoformat(timespec="seconds")}
    _STANDARDS[standard_id] = std
    save_standard(std)
    return {"ok": True, "standard_id": standard_id, "model": model,
            "requested": len(targets), "filled_definition": filled_def,
            "filled_unit": filled_unit, "batches": batches, "errors": errors,
            "quality": std["quality"], "usability": std["usability"]}


def save_standard(std: dict) -> str:
    STANDARD_DIR.mkdir(parents=True, exist_ok=True)
    p = STANDARD_DIR / f"{std['standard_id']}.json"
    p.write_text(json.dumps(std, ensure_ascii=False, indent=1), encoding="utf-8")
    (STANDARD_DIR / f"latest-{std['source_path']}.json").write_text(
        json.dumps(std, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(p)


def get_standard(sid: str) -> dict | None:
    """取标准：先内存，再磁盘（进程重启后仍能读到已生成的标准）。"""
    s = _STANDARDS.get(sid)
    if s:
        return s
    p = STANDARD_DIR / f"{sid}.json"
    if p.exists():
        try:
            s = json.loads(p.read_text(encoding="utf-8-sig"))
            _STANDARDS[sid] = s
            return s
        except Exception:
            return None
    return None


def list_standards() -> list[dict]:
    """列出标准：**以磁盘为准**（进程重启后内存缓存会空，不能只看 _STANDARDS）。"""
    seen: dict[str, dict] = {}
    for s in _STANDARDS.values():
        seen[s["standard_id"]] = s
    try:
        if STANDARD_DIR.exists():
            for p in sorted(STANDARD_DIR.glob("STD-*.json")):
                sid = p.stem
                if sid in seen:
                    continue
                try:
                    seen[sid] = json.loads(p.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
    except Exception:
        pass
    out = [{"standard_id": s.get("standard_id", ""), "source_path": s.get("source_path", ""),
            "source_note": s.get("source_note", ""),
            "object_class_name": s.get("object_class_name", ""),
            "class_count": s.get("class_count", 1),
            "usability": s.get("usability", {}), "quality": s.get("quality", {})}
           for s in seen.values() if s.get("standard_id")]
    out.sort(key=lambda x: x["standard_id"], reverse=True)
    return out


if __name__ == "__main__":
    a = draft_from_mode_a()
    print("[入口 A]", a["standard_id"], "|", a["source_note"])
    print("   字段", a["usability"], "| 质量", {k: a["quality"][k] for k in
                                              ("completeness", "normativity", "accuracy", "manual_review_required")})
    inc = [s["name"] for s in a["field_specs"] if s["status"] == "INCOMPLETE"]
    print("   INCOMPLETE:", inc)
    b = draft_from_mode_b()
    print("[入口 B]", b["standard_id"], "|", b["source_note"])
    print("   字段", b["usability"], "| 质量", {k: b["quality"][k] for k in
                                              ("completeness", "normativity", "accuracy", "manual_review_required")})
    inc2 = [s["name"] for s in b["field_specs"] if s["status"] == "INCOMPLETE"]
    print("   INCOMPLETE:", inc2[:8], "| 规范性维度缺 definition 的字段数:",
          sum(1 for s in b["field_specs"] if not s.get("definition")))
