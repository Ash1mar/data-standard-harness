"""
model_synthesizer.py
=====================
核电数据标准智能治理智能体（双模式）· 模式 B 模型合成与冲突消解（增量 5 · P5）。

绑定工具（见 .dsh/roles/model-synthesizer.md）：synthesize_model / resolve_conflict / rag_graph。

诚实、可追溯的设计原则：
- 每个合成字段必须标 `source`（requirement / inference / resolved / template / pending），
  便于逐项追溯；未标来源的字段视为无效。
- 冲突消解建议必须给 `rationale` 与 `alternatives`；无法机械裁定的一律 `manual` 并触发门控，
  不替业务做决定。
- 最终模型**全字段 COMPLETE 才允许导出**（SKILL 2.7 / 5.4）。
- 决策写入冲突日志 deliverables/mode-b/conflict-log/。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import requirement_analyst as ra  # noqa: E402

DELIVERABLES = Path(__file__).resolve().parent.parent.parent / "deliverables"
CONFLICT_DIR = DELIVERABLES / "mode-b" / "conflict-log"
FINAL_DIR = DELIVERABLES / "mode-b" / "final-models"


# ---------------------------------------------------------------------------
# 1) resolve_conflict
# ---------------------------------------------------------------------------
def resolve_conflict(gaps: list[dict]) -> dict:
    """对 gap_analyze 判出的 conflict 项给出消解建议。

    recommended_action: adopt_max_len（取较大长度）/ adopt_requirement / adopt_template / manual
    """
    items = []
    for g in gaps:
        if g.get("status") != "conflict":
            continue
        d = g.get("conflict_detail", {})
        kind = d.get("kind")
        rv, ev = d.get("requirement_value"), d.get("existing_value")

        if kind == "len":
            try:
                n_req, n_d4 = int(rv), int(ev)
            except (TypeError, ValueError):
                n_req = n_d4 = None
            if n_req is not None and n_d4 is not None:
                bigger = max(n_req, n_d4)
                items.append({
                    "field": g["field"], "recommended_action": "adopt_max_len",
                    "recommended_value": f"长度 {bigger}",
                    "rationale": (f"取两侧较大长度（需求 {n_req} vs D4 {n_d4} → {bigger}）："
                                  f"取较小值会造成已有数据截断，取大值可同时容纳两侧历史数据，"
                                  f"且不引入新的口径约束。"),
                    "alternatives": [
                        {"action": "adopt_requirement", "value": f"长度 {n_req}", "note": "严格遵循业务方填写口径"},
                        {"action": "adopt_template", "value": f"长度 {n_d4}", "note": "严格遵循 D4 完整模型模板"},
                        {"action": "manual", "value": None, "note": "转专家裁定（触发 P8 门控）"},
                    ],
                    "auto_resolvable": True, "conflict_detail": d, "reason": g.get("reason"),
                })
                continue

        # 主数据引用 / 枚举域 / 其他：引用对象不同，不能机械取大，必须业务裁定
        if kind == "md":
            rationale = (f"两侧引用的是不同主数据对象（需求 {rv} vs D4 {ev}），"
                         f"取值域语义不同，无法机械合并，必须由业务确认以哪一份主数据为准。")
        elif kind == "enum":
            rationale = (f"两侧枚举域不同（需求 {rv} vs D4 {ev}），需业务确认枚举取值与编码映射。")
        else:
            rationale = "两侧口径无法机械比对，不能自动裁定，需业务确认。"
        items.append({
            "field": g["field"], "recommended_action": "manual",
            "rationale": rationale,
            "alternatives": [
                {"action": "adopt_requirement", "value": str(rv), "note": "采用需求侧口径"},
                {"action": "adopt_template", "value": str(ev), "note": "采用 D4 模板口径"},
                {"action": "manual", "value": None, "note": "转专家裁定（触发 P8 门控）"},
            ],
            "auto_resolvable": False, "conflict_detail": d, "reason": g.get("reason"),
        })

    return {
        "conflicts": items,
        "count": len(items),
        "auto_resolvable_count": sum(1 for i in items if i["auto_resolvable"]),
        "note": "recommended_action=manual 的项会进入 P8 门控，不得静默覆盖。",
    }


def commit_conflict_decisions(decisions: dict, note: str = "") -> dict:
    """写入冲突消解日志。decisions: {field: {action, value?}}"""
    CONFLICT_DIR.mkdir(parents=True, exist_ok=True)
    rc = resolve_conflict(ra.gap_analyze()["gaps"])
    log = {
        "schema": "mode-b/conflict-log/v1",
        "committed_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "decisions": [],
    }
    for c in rc["conflicts"]:
        d = decisions.get(c["field"])
        log["decisions"].append({
            "field": c["field"],
            "recommended": c["recommended_action"],
            "decided": (d or {}).get("action"),
            "decided_value": (d or {}).get("value"),
            "conflict_detail": c["conflict_detail"],
            "rationale": c["rationale"],
        })
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = CONFLICT_DIR / f"conflict-log-{ts}.json"
    p.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    (CONFLICT_DIR / "latest.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    log["path"] = str(p)
    return log


# ---------------------------------------------------------------------------
# 2) synthesize_model
# ---------------------------------------------------------------------------
def synthesize_model(clarifications: dict | None = None,
                     conflict_decisions: dict | None = None) -> dict:
    """合成最终模型（**按对象类分组**，与 P7 生成侧同源）。

    数据来源统一为 `requirement_analyst.load_requirement_rows()` —— D2 的全部需求表
    合并、按 (对象类, 属性) 去重后的结果。**不再用「41 字段基线」硬套单个对象类**：
    那是旧口径，会和 P7 / 三表导出对不上（历史问题：P5 只有「管道」一个类）。

    clarifications: {field: {'action':'adopt'|'answer'|'pending', 'value': {...}}}
    conflict_decisions: {field: {'action': 'adopt_max_len'|'adopt_requirement'|'manual', ...}}
    """
    from tools import eval_pipeline as ep

    req = ra.parse_requirement()
    rows = ra.load_requirement_rows()
    clar = clarifications or {}
    cdec = conflict_decisions or {}

    rc = resolve_conflict(ra.gap_analyze()["gaps"])
    rec = {c["field"]: c for c in rc["conflicts"]}
    inf_by_field: dict[str, dict] = {}
    try:
        for i in ra.generate_clarification()["inferences"]:
            inf_by_field[i["field"]] = i
    except Exception:
        pass

    def _spec(r: dict) -> dict:
        """一行需求 → 一个字段规格。类型按 D4 规则从自然语言转换，业务原文另存。"""
        name = (r.get("name") or "").strip()
        raw = (r.get("fmt") or "").strip()
        dt, ln, pr = ep.parse_type(raw) if raw else ("String", None, None)
        cons = ep.tech_constraint(dt, ln, pr, raw) if raw else ""
        definition = (r.get("definition") or "").strip()
        unit = (r.get("unit") or "").strip()
        source, basis, conf = "requirement", "", 1.0

        c = clar.get(name)
        if c and c.get("action") == "answer" and isinstance(c.get("value"), dict):
            v = c["value"]
            dt = v.get("data_type", dt) or dt
            cons = v.get("constraint", cons) or cons
            unit = v.get("unit", unit) or unit
            definition = v.get("definition", definition) or definition
            source, basis, conf = "answered", "按人工澄清结果覆盖", 1.0
        elif not definition and name in inf_by_field:
            inf = inf_by_field[name]
            definition = inf.get("value") or definition
            source, basis = "inference", inf.get("basis", "")
            conf = inf.get("confidence", 0.6)
        if r.get("object_class") in rec and rec[r["object_class"]].get("field") == name:
            source, basis = "resolved", rec[name].get("rationale", "")

        warnings = []
        if not definition:
            warnings.append("定义为空")
        if not unit:
            warnings.append("单位为空")
        if not r.get("attr_code"):
            warnings.append("编码为空（导入前需按 D4 规则分配）")

        return {
            "name": name,
            "code": (r.get("attr_code") or "").strip(),
            "data_type": dt,
            "data_type_raw": raw,
            "constraint": cons,
            "biz_constraint": raw,
            "unit": unit,
            "definition": definition,
            "value_domain": (r.get("value_domain") or "").strip(),
            "example": (r.get("example") or "").strip(),
            "required": (r.get("required") or "").strip(),
            "nullable": (r.get("nullable") or "").strip(),
            "unique": (r.get("unique") or "").strip(),
            "source": source,
            "source_note": basis,
            "confidence": conf,
            "field_completeness": "COMPLETE" if (name and dt) else "INCOMPLETE",
            "warnings": warnings,
        }

    # 按对象类分组：先按「对象类清单」铺满（含尚无属性行的类），再补属性表里出现的类，
    # 保证这里的类数 = P5 解析 = P7 生成侧，不出现「P6 少几个类」。
    ds = ra._req_dataset()
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for c in ds["classes"]:
        cn = (c.get("name") or "").strip()
        if cn and cn not in grouped:
            grouped[cn] = []
            order.append(cn)
    for r in rows:
        cn = r.get("object_class") or "（未标注对象类）"
        if cn not in grouped:
            grouped[cn] = []
            order.append(cn)
        grouped[cn].append(r)

    models, attributes = [], []
    # 类层级：对象类表的「父类ID」→ 父类名。2026-10-23 新增，
    # 否则 subClassOf 只在 P3 图谱里凭空出现，P5/P6 产物一条关系都带不出去。
    id2name = {str(c.get("id") or "").strip(): (c.get("name") or "").strip()
               for c in ds["classes"] if c.get("id")}
    parent_of: dict[str, str] = {}
    for c in ds["classes"]:
        nm, pid = (c.get("name") or "").strip(), str(c.get("parent_id") or "").strip()
        if nm and pid and id2name.get(pid):
            parent_of[nm] = id2name[pid]
    sub_rels = [{"type": "subClassOf", "source": p, "target": c}
                for c, p in parent_of.items()]
    for cn in order:
        items = grouped[cn]
        specs = [_spec(r) for r in items]
        cls_code = items[0].get("object_code", "") if items else ""
        for s in specs:
            attributes.append({**s, "class_name": cn, "class_code": cls_code})
        incomplete = [s["name"] for s in specs if s["field_completeness"] != "COMPLETE"]
        no_req = not specs
        models.append({
            "object_class": cn,
            "object_class_code": cls_code,
            "parent": parent_of.get(cn, ""),
            "attributes": specs,
            "no_requirement_rows": no_req,
            "relations": ([{"type": "hasAttribute", "source": cn, "target": s["name"]}
                           for s in specs if s["field_completeness"] == "COMPLETE"]
                          + [r for r in sub_rels if r["target"] == cn]),
            "field_completeness": {"total": len(specs), "complete": len(specs) - len(incomplete),
                                   "incomplete": len(incomplete)},
            "missing_fields": (["（该类在需求表中没有属性行，待业务补填）"] if no_req else incomplete),
            "complete": (not incomplete) and not no_req,
            "warnings": {
                "missing_definition": sum(1 for s in specs if not s["definition"]),
                "missing_unit": sum(1 for s in specs if not s["unit"]),
                "missing_code": sum(1 for s in specs if not s["code"]),
            },
        })

    incomplete_all = [a["name"] for a in attributes if a["field_completeness"] != "COMPLETE"]
    no_req_classes = [m["object_class"] for m in models if m.get("no_requirement_rows")]
    multi = len(models) > 1
    gate = "已满足导出条件"
    if incomplete_all:
        gate = f"存在 {len(incomplete_all)} 个属性缺类型，不允许导出（SKILL 2.7）"
    elif no_req_classes:
        gate = (f"可导出：{len(no_req_classes)} 个对象类在需求表中没有属性行，"
                f"导出时只出对象类行，需业务补填后重跑")
    model = {
        "object_class": "" if multi else (order[0] if order else ""),
        "object_class_label": (f"多对象类（{len(models)} 类）" if multi
                               else (order[0] if order else "")),
        "object_class_code": ("" if multi else (models[0]["object_class_code"] if models else "")),
        "models": models,
        "attributes": attributes,
        "scope": req["scope"],
        "relations": [rel for m in models for rel in m["relations"]],
        "relation_counts": dict(__import__("collections").Counter(
            r["type"] for m in models for r in m["relations"])),
        "class_hierarchy": {"with_parent": len(parent_of), "total": len(order),
                            "note": ("类层级取自 D2 对象类表「父类ID」；"
                                     "父类不在本次需求表内则不建边")},
        "field_completeness": {
            "total": len(attributes),
            "complete": len(attributes) - len(incomplete_all),
            "incomplete": len(incomplete_all),
            "missing_definition": sum(1 for a in attributes if not a["definition"]),
            "missing_unit": sum(1 for a in attributes if not a["unit"]),
            "missing_code": sum(1 for a in attributes if not a["code"]),
            "classes_without_requirement": len(no_req_classes),
        },
        "classes_without_requirement": no_req_classes,
        "missing_fields": incomplete_all[:200],
        "complete": not incomplete_all,
        "export_allowed": not incomplete_all,
        "gate": gate,
        "source_dist": dict(__import__("collections").Counter(a["source"] for a in attributes)),
        "synthesized_at": datetime.now().isoformat(timespec="seconds"),
    }
    return model


def commit_final_model(model: dict, note: str = "") -> dict:
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    if not model.get("complete"):
        return {"ok": False, "error": f"存在 {len(model['missing_fields'])} 个字段未 COMPLETE，不允许导出",
                "missing_fields": model["missing_fields"][:20]}
    payload = {**model, "note": note, "schema": "mode-b/final-model/v1"}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = FINAL_DIR / f"final-model-{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (FINAL_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "path": str(p), "complete": True,
            "attributes": len(model["attributes"])}


if __name__ == "__main__":
    rc = resolve_conflict(ra.gap_analyze()["gaps"])
    print("冲突项:", rc["count"], "| 可自动裁定:", rc["auto_resolvable_count"])
    for c in rc["conflicts"][:3]:
        print("  ", c["field"], "→", c["recommended_action"], "|", c["rationale"][:70])

    # 演示：全部采纳推断 + 冲突取较大长度
    clar = {i["field"]: {"action": "adopt"} for i in ra.generate_clarification()["inferences"]}
    cdec = {c["field"]: {"action": "adopt_max_len"} for c in rc["conflicts"]}
    m = synthesize_model(clar, cdec)
    print("\n合成模型:", m["object_class"], "| 字段", m["field_completeness"])
    print("   来源分布:", m["source_dist"])
    print("   complete:", m["complete"], "| 未完成:", m["missing_fields"][:6])
