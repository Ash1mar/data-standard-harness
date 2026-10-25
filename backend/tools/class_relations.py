"""
class_relations.py
==================
P3 · 对象类之间的关系发现（大模型 + 本体论模式），把「类与类之间的关系」补进图谱。

为什么需要这个模块
------------------
底座里的 `subClassOf` 只有一个来源：类表的「父类 / 父类ID」列。D0 类表该列全空、
D2 需求表本身没有层级列 → 建出来的图画布上 380 个类平铺，一条类间边都没有。
规则抽不出来，只能靠模型读「类名 + 定义 + 属性清单」判归属 —— 这就是本模块。

职责边界（重要）
----------------
- 本模块只产出**候选关系**，落到 `data/graph/class_relations_{mode}.json`，
  **不自动改图**。入图必须经 `apply()`，由人给阈值后触发。
- 不覆盖、不修改任何已有边。模型给出的关系与已有边重复的，标 `duplicate` 跳过。
- head/tail 必须是底座里真实存在的类（按 label 精确匹配），模型编的类一律丢弃。
- `subClassOf` / `partOf` 入图前做**成环检测**：加了会成环的直接跳过并计数
  （OWL 里 subClassOf 成环等于把两个类合并，partOf 成环会让推理机把 A 推成自己的部件）。

流程
----
    analyze(mode)  读图谱里的对象类 → 分批喂模型 → 汇总去重 → 写候选文件
    apply(mode)    按置信度阈值 + 类型白名单挑候选 → 防环 → graph_upsert → 重导 OWL
    clear(mode)    只删「模型推断」的边（source_ref = llm:class_relations），保留原有骨架
"""

from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import llm_client as llm  # noqa: E402
from tools import llm_tasks as lt  # noqa: E402
from tools import ontology_builder as ob  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
REL_DIR = DATA_ROOT / "graph"

SOURCE_REF = "llm:class_relations"      # 所有推断边的统一来源戳（撤销靠它）
REL_TYPES = lt.REL_TYPES                # 允许的关系类型（与 llm_tasks 单一来源）
REL_TYPE_CN = lt.REL_TYPE_CN
DEFAULT_TYPES = ("subClassOf", "partOf", "associates")   # sameAs 默认不入图（易误合并）
MIN_CONF_DEFAULT = 0.6
_CYCLE_TYPES = ("subClassOf", "partOf")


def candidate_path(mode: str = "B") -> Path:
    return REL_DIR / f"class_relations_{(mode or 'B').upper()}.json"


# ---------------------------------------------------------------------------
# 1) 给模型准备输入：类名 + 定义 + 属性清单 + 出处证据
# ---------------------------------------------------------------------------
def _evidence_text(n: dict) -> str:
    """把节点上的证据字段翻成模型能懂的一句话。**无证据就如实说无**。"""
    if n.get("origin") == "d0":
        return "D0 标准库（已人工确认）"
    try:
        from tools import confidence as cf
        t = cf.tier_for_node(n) or {}
        if t.get("label"):
            return str(t["label"])
    except Exception:
        pass
    kind = str(n.get("evidence_kind") or "").strip()
    return f"证据类型 {kind}" if kind else "证据未评估"


def class_payload(mode: str = "B", limit: int = 0) -> list[dict]:
    """从图谱里取对象类，附上定义、属性名清单、证据说明。

    排序：属性多的在前（信息量大，更可能是「父类」候选）→ 同属性数按名字排
    （中文按码点，同前缀的类会相邻，模型更容易看出「XX阀」同族）。
    """
    g = ob.get_graph(mode)
    attr_names: dict[str, list[str]] = defaultdict(list)
    for e in g["edges"]:
        if e.get("type") == "hasAttribute":
            attr_names[e["source"]].append(e["target"])
    by_id = {n["id"]: n for n in g["nodes"]}

    rows = []
    for n in g["nodes"]:
        if n.get("type") != "object_class":
            continue
        rows.append({
            "id": n["id"],
            "name": str(n.get("label") or n.get("code") or n["id"]).strip(),
            "definition": str(n.get("definition") or ""),
            "attributes": [str(by_id[t].get("label") or t) for t in attr_names.get(n["id"], [])[:12]
                           if t in by_id],
            "attr_count": int(n.get("field_count") or len(attr_names.get(n["id"], []))),
            "evidence": _evidence_text(n),
            "confidence": n.get("confidence"),
        })
    rows.sort(key=lambda r: (-r["attr_count"], r["name"]))
    return rows[:limit] if limit and limit > 0 else rows


def _name_index(rows: list[dict]) -> tuple[dict, list[str]]:
    """label → id。重名类只认第一个（按属性数排序后的），其余记进 notes。"""
    idx: dict[str, str] = {}
    dup: list[str] = []
    for r in rows:
        nm = r["name"]
        if not nm:
            continue
        if nm in idx:
            dup.append(nm)
            continue
        idx[nm] = r["id"]
    return idx, dup


# ---------------------------------------------------------------------------
# 2) 分析：分批并发调模型，汇总去重
# ---------------------------------------------------------------------------
def analyze(mode: str = "B", *, batch: int = 15, workers: int = 4, limit: int = 0,
            model: str | None = None, with_same_as: bool = False,
            timeout: float = 420, retry_failed: bool = False,
            progress=None) -> dict:
    """跑一遍类间关系分析，产出候选文件（**不入图**）。

    batch   : 每次调用喂多少个类。太大 → 单次 prompt 超 12000 字符会被截断；
              太小 → 调用次数变多、跨批关系变少。15~20 比较稳。
    workers : 并发调用数。实测串行 26 批要 15 分钟以上，4 并发 3~5 分钟。
    timeout : 单批超时。qwen3.8-max 这类慢模型给 420s；实测 240s 会有整批超时。
    retry_failed : 只重跑上次失败的批次，结果合并进已有候选（失败的类通常
              是定义长/属性多的，整批重跑最省）。**不覆盖**已成功的批次结果。
    progress: 可选回调 `fn(event: str, data: dict)`，供 SSE 推进度。
    """
    mode = (mode or "B").upper()
    st = llm.status()
    if not st["available"]:
        raise RuntimeError(st.get("reason") or "大模型未配置，无法做类间关系分析")

    # 全量取一次：id 映射与 name_pool 必须覆盖整张底座，否则模型找不到范围外的父类，
    # 只能把「前 N 个类」互相凑成关系，质量会塌。limit 只裁剪**要分析**的类。
    all_rows = class_payload(mode)
    limit = int(limit or 0)
    rows = all_rows[:limit] if limit > 0 else all_rows
    if len(rows) < 2:
        raise RuntimeError("底座里的对象类不足 2 个，没有「类与类之间的关系」可分析")

    idx, dup_names = _name_index(all_rows)
    pool = [r["name"] for r in all_rows if r["name"] in idx][:400]

    chunks = [rows[i:i + batch] for i in range(0, len(rows), batch)]
    base: list[dict] = []
    prev_failed: list[dict] = []
    if retry_failed:
        prev = load(mode)
        if not prev.get("exists"):
            raise RuntimeError("没有历史候选可补跑，请先完整跑一次分析")
        base = list(prev.get("relations") or [])
        prev_failed = list(prev.get("failed_batches") or [])
        # 批次是按「属性数降序 + 类名」切分的，顺序稳定 → 用下标即可复现
        target_idx = sorted({int(b.get("batch", 0)) - 1 for b in prev_failed
                             if 0 < int(b.get("batch", 0)) <= len(chunks)})
        if not target_idx:
            return {"mode": mode, "classes": len(rows), "batches": 0, "relations": base,
                    "counts": dict(Counter(x["type"] for x in base)),
                    "duplicates": sum(1 for x in base if x.get("duplicate")),
                    "dropped": {}, "notes": ["上次没有失败批次，无需补跑"],
                    "failed_batches": [], "elapsed_ms": 0, "model": ""}
    else:
        target_idx = list(range(len(chunks)))
        # 只跑一部分类时，必须把「这次没分析的类」的旧候选原样接过来，
        # 否则写盘就是整文件覆盖，前几次跑出来的关系会被悄悄清掉。
        if limit > 0:
            prev = load(mode)
            if prev.get("exists"):
                keep = {r["name"] for r in rows}
                base = [x for x in (prev.get("relations") or [])
                        if x.get("head_label") not in keep]
    total = len(target_idx)
    if progress:
        progress("rel_begin", {"mode": mode, "classes": len(rows),
                               "total_classes": len(all_rows), "limit": limit,
                               "batches": total, "model": st["model"],
                               "name_pool": len(pool), "retry_failed": retry_failed})

    # 每批的输入只保留模型需要的字段（id 在本地映射，不发给模型）
    payloads = [[{"name": r["name"], "definition": r["definition"],
                  "attributes": r["attributes"], "evidence": r["evidence"]}
                 for r in chunks[i]] for i in target_idx]

    t0 = time.time()
    merged: list[dict] = []
    failed: list[dict] = []
    dropped = Counter()
    models: Counter = Counter()
    done = 0
    # 补跑时把上次成功的批次也记为「已处理」，否则进度条看着像丢了数据
    done0 = (len(chunks) - total) if retry_failed else 0

    def run(k: int) -> dict:
        return lt.analyze_class_relations(payloads[k], name_pool=pool, model=model,
                                          timeout=timeout)

    with futures.ThreadPoolExecutor(max_workers=max(1, min(8, workers))) as ex:
        futs = {ex.submit(run, k): k for k in range(total)}
        for f in futures.as_completed(futs):
            k = futs[f]
            done += 1
            bi = target_idx[k] + 1
            try:
                res = f.result()
            except Exception as e:                      # 单批失败不拖垮整体，如实记录
                failed.append({"batch": bi, "error": f"{type(e).__name__}: {e}"[:200]})
                if progress:
                    progress("rel_batch", {"done": done + done0, "total": len(chunks),
                                           "batch": bi, "ok": False,
                                           "error": str(e)[:160], "found": 0,
                                           "relations": len(base) + len(merged)})
                continue
            models[res.get("model") or ""] += 1
            dropped["unknown_name"] += res.get("dropped_unknown_name", 0)
            dropped["no_basis"] += res.get("dropped_no_basis", 0)
            dropped["dup_in_batch"] += res.get("dropped_dup", 0)
            dropped["bad_type"] += res.get("dropped_type", 0)
            merged.extend(res.get("relations") or [])
            if progress:
                progress("rel_batch", {"done": done + done0, "total": len(chunks),
                                       "batch": bi, "ok": True,
                                       "found": len(res.get("relations") or []),
                                       "relations": len(base) + len(merged),
                                       "dropped_unknown": res.get("dropped_unknown_name", 0)})

    # ---- 汇总：名字 → id、去重、标记与已有边重复的项 ----
    # 补跑模式下把上次已成功的候选一并合进来（它们已经是 id 形态），一起参与去重。
    g = ob.get_graph(mode)
    existing = {(e.get("type"), e.get("source"), e.get("target")) for e in g["edges"]}
    seen: set = set()
    out: list[dict] = []
    for r in base:
        t, hid, tid = r["type"], r["head"], r["tail"]
        if t == "sameAs" and not with_same_as:
            continue
        if not hid or not tid:
            continue
        key = (t, hid, tid)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": r.get("id") or "r-" + hashlib.md5(f"{t}|{hid}|{tid}".encode("utf-8")).hexdigest()[:10],
            "type": t, "head": hid, "head_label": r.get("head_label", ""),
            "tail": tid, "tail_label": r.get("tail_label", ""),
            "basis": r.get("basis", ""),
            "confidence": round(float(r.get("confidence") or 0.6), 3),
            "duplicate": key in existing,
        })
    for r in merged:
        t, head, tail = r["type"], r["head"], r["tail"]
        if t == "sameAs" and not with_same_as:
            dropped["same_as_off"] += 1
            continue
        hid, tid = idx.get(head), idx.get(tail)
        if not hid or not tid:
            dropped["name_not_in_graph"] += 1
            continue
        if hid == tid:
            continue
        key = (t, hid, tid)
        if key in seen:
            dropped["dup_across_batches"] += 1
            continue
        seen.add(key)
        out.append({
            "id": "r-" + hashlib.md5(f"{t}|{hid}|{tid}".encode("utf-8")).hexdigest()[:10],
            "type": t,
            "head": hid, "head_label": head,
            "tail": tid, "tail_label": tail,
            "basis": r["basis"],
            "confidence": round(float(r.get("confidence") or 0.6), 3),
            "duplicate": key in existing,
        })
    out.sort(key=lambda x: (-x["confidence"], x["type"], x["head_label"]))

    counts = Counter(x["type"] for x in out)
    res = {
        "schema": "class-relations/v1",
        "mode": mode,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "model": (models.most_common(1)[0][0] if models else ""),
        "classes": len(rows),
        "total_classes": len(all_rows),
        "limit": limit,
        "name_pool": len(pool),
        "batches": len(chunks),
        "analyzed_batches": total,
        "retry_failed": retry_failed,
        "failed_batches": failed,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "relations": out,
        "counts": dict(counts),
        "duplicates": sum(1 for x in out if x["duplicate"]),
        "dropped": dict(dropped),
        "notes": ([f"本次只分析了前 {limit} 个类（共 {len(all_rows)} 个），其余类的旧候选已保留；"
                   f"要覆盖全部类请把范围选成「全部」"] if limit > 0 and limit < len(all_rows) else [])
                 + ([f"有 {len(dup_names)} 个类名重复，重名类只按第一个匹配："
                     + "、".join(sorted(set(dup_names))[:6])] if dup_names else []),
    }
    REL_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path(mode).write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    if progress:
        progress("rel_complete", {"mode": mode, "classes": len(rows),
                                 "batches": len(chunks), "analyzed": total,
                                 "failed": len(failed), "relations": len(out),
                                 "counts": dict(counts), "elapsed_ms": res["elapsed_ms"],
                                 "retry_failed": retry_failed,
                                 "path": str(candidate_path(mode).relative_to(DATA_ROOT.parent))})
    return res


# ---------------------------------------------------------------------------
# 3) 读候选（附带「是否已在图里」的实时状态）
# ---------------------------------------------------------------------------
def load(mode: str = "B") -> dict:
    mode = (mode or "B").upper()
    p = candidate_path(mode)
    if not p.exists():
        return {"mode": mode, "exists": False, "relations": [], "counts": {},
                "duplicates": 0, "dropped": {}, "notes": [],
                "hint": "还没有跑过类间关系分析：点「大模型分析类间关系」"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"mode": mode, "exists": False, "relations": [], "counts": {},
                "duplicates": 0, "dropped": {}, "notes": [], "hint": f"候选文件损坏：{e}"}
    existing = {(e.get("type"), e.get("source"), e.get("target"))
                for e in ob.get_graph(mode)["edges"]}
    in_graph = 0
    for r in d.get("relations") or []:
        r["in_graph"] = (r["type"], r["head"], r["tail"]) in existing
        if r["in_graph"]:
            in_graph += 1
    d.update({"exists": True, "in_graph": in_graph,
              "pending": sum(1 for r in d.get("relations") or []
                             if not r["in_graph"] and not r.get("duplicate"))})
    return d


# ---------------------------------------------------------------------------
# 4) 入图（人工阈值 → 防环 → 写边 → 重导 OWL）
# ---------------------------------------------------------------------------
def _edge_id(t: str, source: str, target: str) -> str:
    h = hashlib.md5(f"{t}|{source}|{target}".encode("utf-8")).hexdigest()[:10]
    return f"e-{t}-ai-{h}"


def _would_cycle(t: str, head: str, tail: str, parent: dict[tuple[str, str], str]) -> bool:
    """把 head→tail 加进 parent 映射后是否成环（沿 tail 往上走回 head 即成环）。

    parent 的键是 (关系类型, 子)，**不跨类型判环**：A subClassOf B 同时 B partOf C
    是合法的多重关系，不该被当成环误杀。
    """
    cur, hops = tail, 0
    while (t, cur) in parent and hops < 500:
        cur = parent[(t, cur)]
        hops += 1
        if cur == head:
            return True
    return False


def apply(mode: str = "B", *, min_confidence: float = MIN_CONF_DEFAULT,
          types: list[str] | None = None, dry_run: bool = False) -> dict:
    """把候选关系按阈值挑出来写进图谱。

    只做三件事：过滤 → 防环 → 写边。**不新建节点**（关系两端必须已存在于底座）。
    写完重导 OWL，否则 Turtle 里没有这些断言，面板上「类间关系」和 OWL 会对不上。
    """
    mode = (mode or "B").upper()
    keep_types = tuple(types) if types else DEFAULT_TYPES
    cand = load(mode)
    if not cand.get("exists"):
        return {"ok": False, "error": cand.get("hint") or "没有候选关系，请先分析"}

    g = ob.get_graph(mode)
    node_ids = {n["id"] for n in g["nodes"]}
    existing = {(e.get("type"), e.get("source"), e.get("target")) for e in g["edges"]}
    parent: dict[tuple[str, str], str] = {}
    for e in g["edges"]:
        if e.get("type") in _CYCLE_TYPES:
            parent[(e["type"], e["source"])] = e["target"]

    skip = Counter()
    new_edges: list[dict] = []
    for r in sorted(cand["relations"], key=lambda x: -float(x.get("confidence") or 0)):
        t = r.get("type")
        if t not in keep_types:
            skip["type_excluded"] += 1
            continue
        if float(r.get("confidence") or 0) < float(min_confidence):
            skip["low_confidence"] += 1
            continue
        if r.get("in_graph") or (t, r["head"], r["tail"]) in existing:
            skip["already_in_graph"] += 1
            continue
        if r["head"] not in node_ids or r["tail"] not in node_ids:
            skip["missing_node"] += 1
            continue
        if t in _CYCLE_TYPES and _would_cycle(t, r["head"], r["tail"], parent):
            skip["cycle"] += 1
            continue
        if t in _CYCLE_TYPES:
            parent[(t, r["head"])] = r["tail"]
        existing.add((t, r["head"], r["tail"]))
        new_edges.append({
            "id": _edge_id(t, r["head"], r["tail"]),
            "source": r["head"], "target": r["tail"], "type": t,
            "label": {"subClassOf": "子类（模型推断）", "partOf": "组成",
                      "associates": "业务关联", "sameAs": "同义"}.get(t, t),
            "source_ref": SOURCE_REF,
            "origin": "inferred",
            "confidence": r.get("confidence"),
            "confidence_source": "llm",
            "evidence": r.get("basis", ""),
        })

    if dry_run:
        return {"ok": True, "dry_run": True, "would_add": len(new_edges),
                "skipped": dict(skip),
                "by_type": dict(Counter(e["type"] for e in new_edges))}

    if not new_edges:
        return {"ok": True, "applied": 0, "skipped": dict(skip),
                "by_type": {}, "owl": None,
                "message": "没有满足阈值的新关系（可能已全部入图）"}

    wr = ob.graph_upsert([], new_edges, mode)
    owl = ob.json_to_owl(ob.get_graph(mode), mode, write=True)
    return {"ok": True, "applied": len(new_edges), "skipped": dict(skip),
            "by_type": dict(Counter(e["type"] for e in new_edges)),
            "upsert": wr,
            "owl_profile": owl["owl_profile"],
            "owl_path": owl["owl_path"]}


def clear(mode: str = "B") -> dict:
    """撤销模型推断的类间关系（只删这些边，不动任何原有骨架与节点）。"""
    mode = (mode or "B").upper()
    r = ob.remove_edges(lambda e: str(e.get("source_ref") or "") == SOURCE_REF, mode)
    owl = ob.json_to_owl(ob.get_graph(mode), mode, write=True) if r["removed_edges"] else None
    return {"ok": True, **r, "owl_path": (owl or {}).get("owl_path", "")}


def summary(mode: str = "B") -> dict:
    """P3 面板用的一行摘要：图里现有多少类间关系、其中多少是模型推断的。"""
    mode = (mode or "B").upper()
    g = ob.get_graph(mode)
    by_type = Counter(e.get("type") for e in g["edges"])
    ai = Counter(e.get("type") for e in g["edges"]
                 if str(e.get("source_ref") or "") == SOURCE_REF)
    cls = sum(1 for n in g["nodes"] if n.get("type") == "object_class")
    linked = {e["source"] for e in g["edges"]
              if e.get("type") in ("subClassOf", "partOf", "associates")}
    cand = load(mode)
    return {
        "mode": mode, "classes": cls,
        "edges_by_type": dict(by_type),
        "ai_by_type": dict(ai),
        "class_relations": sum(by_type.get(t, 0) for t in ("subClassOf", "partOf", "associates")),
        "hierarchy": by_type.get("subClassOf", 0),
        "classes_with_relation": len(linked),
        "candidates": {"exists": cand.get("exists"), "total": len(cand.get("relations") or []),
                       "pending": cand.get("pending", 0),
                       "in_graph": cand.get("in_graph", 0),
                       "built_at": cand.get("built_at", ""),
                       "model": cand.get("model", ""),
                       "hint": cand.get("hint", "")},
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="P3 类间关系分析（大模型）")
    ap.add_argument("cmd", choices=["analyze", "apply", "clear", "summary", "show"])
    ap.add_argument("--mode", default="B")
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-conf", type=float, default=MIN_CONF_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "analyze":
        r = analyze(a.mode, batch=a.batch, workers=a.workers, limit=a.limit,
                    progress=lambda ev, d: print(f"[{ev}] {json.dumps(d, ensure_ascii=False)}"))
        print(json.dumps({k: r[k] for k in ("classes", "batches", "counts", "duplicates",
                                           "dropped", "elapsed_ms", "failed_batches")},
                         ensure_ascii=False, indent=1))
    elif a.cmd == "apply":
        print(json.dumps(apply(a.mode, min_confidence=a.min_conf, dry_run=a.dry_run),
                         ensure_ascii=False, indent=1))
    elif a.cmd == "clear":
        print(json.dumps(clear(a.mode), ensure_ascii=False, indent=1))
    elif a.cmd == "summary":
        print(json.dumps(summary(a.mode), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(load(a.mode), ensure_ascii=False, indent=1)[:4000])
