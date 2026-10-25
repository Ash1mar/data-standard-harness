"""
standard_evolver.py
====================
核电数据标准智能治理智能体（双模式）· 标准演化匹配（增量 6 · P6）。

绑定工具（见 .dsh/roles/standard-evolver.md）：rag_vector / rag_graph / diff_compare。

流程：rag_vector（语义召回）→ rag_graph（图谱口径校验）→ diff_compare（字段级比对）
→ 判定 可复用 / 需合并 / 确为新增 → 「需合并」出合并建议并触发人工门控。

诚实、可追溯的设计原则：
- 检索源为参考底座模拟库与 P3 图谱（真实已有标准库缺失时顶替），不含任何预期输出。
- 合并建议必须给 `rationale` 与 `alternatives`；业务未确认前不改写任何标准。
- 决策写入 deliverables/shared/evolution-log/；`manual` 决策进 P8 门控。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import landscape_analyst as la  # noqa: E402
from tools import ontology_builder as ob   # noqa: E402
from tools import standard_generator as sg # noqa: E402

DELIVERABLES = Path(__file__).resolve().parent.parent.parent / "deliverables"
EVOLVE_DIR = DELIVERABLES / "shared" / "evolution-log"

_ANALYSIS: dict[str, dict] = {}


def _graph_field_index(mode: str = "A") -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    for n in ob.get_graph(mode)["nodes"]:
        if n.get("type") == "field":
            idx[n.get("label", "")].append(n)
    return idx


def diff_compare(draft: dict, existing: dict) -> list[dict]:
    """字段级 diff：逐项比对 data_type / constraint / unit。"""
    rows = []
    for k in ("data_type", "constraint", "unit"):
        d = str(draft.get(k, "") or "").strip() or "（空）"
        e = str(existing.get(k, "") or "").strip() or "（空）"
        if d != e:
            rows.append({"field": k, "draft": d, "existing": e})
    return rows


def analyze(standard_id: str) -> dict:
    std = sg.get_standard(standard_id)
    if not std:
        raise KeyError(f"未找到标准 {standard_id}，请先生成标准")
    lib = la.load_mock_library(std["source_path"])
    gidx = _graph_field_index(std["source_path"])
    from tools import local_embedding as le
    idx = le.library_index(std["source_path"])

    items, merges = [], []
    for spec in std["field_specs"]:
        name = spec["name"]
        # rag_vector：本地向量召回（2026-10-23 从 Dice 字面相似换成本地嵌入）
        hit = idx.best(name) if idx.items else None
        best, best_name = (hit["score"], hit["key"]) if hit else (0.0, "")

        # rag_graph：多跳图谱检索（原先是同名字段直接查表，没有跳数/路径）
        gq = ob.graph_query(name, std["source_path"], topk=3, hop=2)
        direct = [m for m in gq.get("matched", [])
                  if m.get("hop") == 0 and m.get("type") == "field"]
        near = [p for p in (gq.get("paths") or []) if p.get("target") != name]
        gn = [gidx[m["id"]] for m in direct if m["id"] in gidx] if direct else []
        if gn:
            gspecs = {(x.get("data_type"), x.get("constraint"), x.get("unit")) for x in gn}
            gcheck = f"图谱中 {len(gn)} 个同名字段节点，{len(gspecs)} 种口径"
        elif near:
            gcheck = (f"图谱无同名节点；最近关联「{near[0]['target']}」"
                      f"（{near[0]['hops']} 跳，经 {'→'.join(near[0]['via'])}）")
        else:
            gcheck = "图谱中无同名节点"

        existing = None
        # 阈值门槛：向量检索总能给出"最近邻"，但最近邻≠匹配。
        # 低于 VECTOR_THRESHOLD 一律按"确为新增"处理（原来直接把最近邻当同名词条，
        # 会让相似度 0.13 的条目被判成「需合并」，判定全乱）。
        if best >= la.VECTOR_THRESHOLD and best_name in lib["field_index"]:
            rows = lib["field_index"][best_name]
            r0 = rows[0]
            existing = {"name": best_name, "code": r0.get("attr_id") or r0.get("std_id") or "",
                        "data_type": r0.get("data_type", ""), "constraint": r0.get("constraint", ""),
                        "unit": r0.get("unit", ""), "owner_count": len(rows),
                        "spec_variants": len({(x.get("data_type"), x.get("constraint"), x.get("unit"))
                                              for x in rows})}

        diffs = diff_compare(spec, existing) if existing else []
        if existing is None:
            verdict = "确为新增"
            reason = (f"参考库中最高向量相似度仅 {best}（阈值 {la.VECTOR_THRESHOLD}），"
                      f"无同名条目" if best < la.VECTOR_THRESHOLD
                      else f"相似度 {best} 命中的是对象类「{best_name}」，非字段条目")
        elif not diffs:
            verdict = "可复用"
            reason = f"与「{best_name}」口径完全一致，可直接复用"
        else:
            verdict = "需合并"
            reason = f"与「{best_name}」同名但 {len(diffs)} 项口径不同，需合并裁定"

        item = {
            "field": name, "similarity": best, "matched": best_name or None,
            "verdict": verdict, "reason": reason,
            "graph_check": gcheck, "graph_node_count": len(gn),
            "diff": diffs, "existing": existing,
            "draft_spec": {"data_type": spec.get("data_type", ""),
                           "constraint": spec.get("constraint", ""),
                           "unit": spec.get("unit", "")},
        }
        items.append(item)

        if verdict == "需合并":
            merges.append({
                "field": name,
                "merge_preview": {"draft": item["draft_spec"],
                                  "existing": {k: existing.get(k) for k in ("data_type", "constraint", "unit")},
                                  "diff": diffs},
                "recommended": "adopt_existing" if existing.get("spec_variants", 1) == 1 else "manual",
                "rationale": ("已有标准口径唯一，沿用可保持全库一致"
                              if existing.get("spec_variants", 1) == 1
                              else f"已有标准自身存在 {existing.get('spec_variants')} 种口径，"
                                   f"无法机械裁定，需业务确认"),
                "alternatives": [
                    {"action": "adopt_existing", "note": "沿用已有标准口径"},
                    {"action": "adopt_draft", "note": "采用本次草案口径"},
                    {"action": "manual", "note": "转专家裁定（入 P8 门控）"},
                ],
            })

    res = {
        "standard_id": standard_id,
        "source_path": std["source_path"],
        "items": items,
        "merge_suggestions": merges,
        "verdict_dist": dict(Counter(i["verdict"] for i in items)),
        "retrieval": ("rag_vector（本地向量：字 n-gram TF-IDF + 随机投影，余弦召回）"
                      "+ rag_graph（多跳路径传播）+ diff_compare（字段级比对）"),
        "retrieval_impl": le.describe(),
        "library": {"source": lib["source"], "note": lib["source_note"]},
    }
    _ANALYSIS[standard_id] = res
    return res


def commit(standard_id: str, decisions: dict, note: str = "") -> dict:
    """decisions: {field: {action: adopt_existing|adopt_draft|manual}}"""
    std = sg.get_standard(standard_id)
    if not std:
        raise KeyError(f"未找到标准 {standard_id}")
    ana = _ANALYSIS.get(standard_id) or analyze(standard_id)
    applied, manuals = [], []

    for fname, d in (decisions or {}).items():
        act = d.get("action")
        if act == "manual":
            manuals.append(fname)
            continue
        spec = next((s for s in std["field_specs"] if s["name"] == fname), None)
        item = next((i for i in ana["items"] if i["field"] == fname), None)
        if not spec or not item or not item.get("existing"):
            continue
        ex = item["existing"]
        if act == "adopt_existing":
            spec["data_type"] = ex.get("data_type", spec["data_type"])
            spec["constraint"] = ex.get("constraint", spec["constraint"])
            spec["unit"] = ex.get("unit", spec["unit"])
        spec["evolved"] = act
        spec["missing"] = [f for f in sg.REQUIRED_SPEC_FIELDS if not str(spec.get(f, "") or "").strip()]
        spec["status"] = "COMPLETE" if not spec["missing"] else "INCOMPLETE"
        applied.append(fname)

    std["quality"] = sg.quality_score(std["field_specs"])
    std["usability"] = sg.usability(std["field_specs"])
    sg.save_standard(std)

    EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
    log = {
        "schema": "shared/evolution-log/v1",
        "committed_at": datetime.now().isoformat(timespec="seconds"),
        "standard_id": standard_id, "note": note,
        "applied": applied, "manual": manuals,
        "verdict_dist": ana["verdict_dist"],
        "decisions": [{"field": k, "action": v.get("action")} for k, v in (decisions or {}).items()],
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = EVOLVE_DIR / f"evolution-{ts}.json"
    p.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    (EVOLVE_DIR / "latest.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    log["path"] = str(p)
    log["manual_gates"] = manuals
    return log


if __name__ == "__main__":
    a = sg.draft_from_mode_a()
    r = analyze(a["standard_id"])
    print("[入口 A] 演化判定:", r["verdict_dist"])
    for m in r["merge_suggestions"][:3]:
        print("   需合并:", m["field"], "| 建议", m["recommended"], "|", m["rationale"][:50])
        for d in (m["merge_preview"]["diff"] or [])[:2]:
            print("       diff:", d["field"], "草案", d["draft"], "vs 已有", d["existing"])
    b = sg.draft_from_mode_b()
    r2 = analyze(b["standard_id"])
    print("[入口 B] 演化判定:", r2["verdict_dist"], "| 需合并", len(r2["merge_suggestions"]))
