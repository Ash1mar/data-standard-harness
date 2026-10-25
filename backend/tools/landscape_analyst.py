"""
landscape_analyst.py
=====================
核电数据标准智能治理智能体（双模式）· 模式 A 发现式梳理分析（增量 4 · P4）。

绑定工具（见 .dsh/roles/landscape-analyst.md）：
novelty_check / landscape_map / prioritize_candidates / rag_vector / rag_graph。

诚实、可追溯的设计原则：
- **D2 是 degraded**（真实 E 板块已有标准库缺失：属性 <500、关系 <1000）。
  按 SKILL 1.3.4 顶替规则，用 **参考底座的 7 成** 充作"已有标准模拟库"进入匹配，
  UI 必须在 P4 顶部横幅提示"使用建模集模拟已有标准，新颖性判定仅供演示"。
- **conflict 必附字段级 diff**，且 diff 不是编造的：来自 dev_model 中同一属性
  在不同对象类下的 (data_type, constraint, unit) 不一致 —— 共 11 个真实口径冲突属性。
- 检索源不含 test_model / full_mode（沿用 ontology_builder 的防泄漏约定）。
- 新颖性标签必须由 novelty_check 真实返回，不得凭经验编造；
  相似度用字符二元组 Dice 系数（对中文短词稳定），阈值可配置并记录。
- 术语归一历史上有错误映射（如 管道→无法止回），
  因此 novelty_check 优先用**原始抽取词**匹配，归一结果只在卡片上展示，
  避免用错误的 canonical 污染新颖性判定。
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

from tools import knowledge_extractor as ke  # noqa: E402
from tools import ontology_builder as ob     # noqa: E402
from tools import mode_isolation as mi        # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
DELIVERABLES = Path(__file__).resolve().parent.parent.parent / "deliverables"
APPROVED_DIR = DELIVERABLES / "mode-a" / "approved-candidates"

# --- 可调阈值（变更需记录，SKILL 5.3 要求） ---
SIMILAR_THRESHOLD = 0.62     # （历史）Dice 系数口径，仅保留兼容；检索已改向量
VECTOR_THRESHOLD = 0.35      # 本地向量余弦 ≥ 此值判 similar（噪声底约 0.15）
COVERED_THRESHOLD = 0.92     # ≥ 此值且非精确命中，仍按 name 精确判定为主
MIN_DENSITY = 1              # 单元格候选数 < 此值 → 覆盖盲区

# 工作量基准（人日），公式在 rationale 中显式给出
EFFORT_BASE = {"object_class": 3.0, "attribute": 1.5, "relation": 2.0,
               "value_domain": 1.0, "unit": 0.5, "rule": 2.5}

_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 已有标准模拟库（真实已有标准库缺失时用参考底座顶替）
# ---------------------------------------------------------------------------
def load_mock_library(mode: str = "A", force: bool = False) -> dict:
    """"已有标准库"的统一入口：**只读 D0**（`data/library/`），库为空就返回空库。

    2026-10-21：模拟库按模式隔离，不再读已作废的 data/dev_model/model.json。
    2026-10-23：删掉「用该模式建模集的前 70% 顶替」这条回落路径 —— 那套底座取自
    D3 预期输出，等于拿评测答案当存量标准库，P4 新颖性 / P5 匹配都会被污染。
    现在 D0 为空时返回 `degraded=no_d0_library` 的空库，由各面板自己把话说清楚。

    返回形状（P4/P5/P6 共用）：{classes, field_index, conflicts, class_count, ...}
    """
    key = f"lib:{mode}"
    if not force and key in _CACHE:
        return _CACHE[key]

    # ① 只用 D0 已有标准库（跨模式共用一份）
    try:
        from tools import standard_library as sl
        d0 = sl.load_library()
        if d0 and d0.get("class_count"):
            _CACHE[key] = d0
            return d0
    except Exception:
        pass

    # ② D0 为空：**不再用「参考底座」顶替** —— 那套底座取自 D3 预期输出，
    #    等于是拿答案当库。现在明确返回空库，让 P4/P5 把"未提供已有标准库"说清楚。
    empty = {
        "mode": mode,
        "source": "未提供已有标准库（D0）",
        "source_short": "未提供已有标准库（D0）",
        "source_note": "本轮未上传已有标准库：新颖性无法与存量标准比对，候选一律按「全新」处理，"
                       "不套用任何内置/参考库。",
        "class_count": 0,
        "field_count": 0,
        "attr_count": 0,
        "kept_class_ids": [],
        "classes": {},
        "field_index": {},
        "conflicts": {},
        "conflict_count": 0,
        "is_builtin": False,
        "degraded": "no_d0_library",
    }
    _CACHE[key] = empty
    return empty


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------
def _bigrams(s: str) -> set[str]:
    s = (s or "").strip()
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dice(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return round(2 * len(A & B) / (len(A) + len(B)), 4)


# ---------------------------------------------------------------------------
# novelty_check
# ---------------------------------------------------------------------------
def novelty_check(cand: dict, mode: str = "A") -> dict:
    """返回 {candidate_id, term, status, matched_standard_id, similarity, diff?, mapping?, basis}。

    status: covered（模拟库中已有且口径一致）
            similar（语义相近可复用，附映射建议）
            conflict（口径冲突，必附字段级 diff）
            novel（全新）
    """
    lib = load_mock_library(mode)
    cid = cand.get("id", "")
    # 优先用「原始抽取词」，避免术语归一把判定带偏
    term = ((cand.get("term_norm") or {}).get("original") or cand.get("name") or "").strip()
    canonical = (cand.get("name") or "").strip()

    out = {"candidate_id": cid, "term": term, "canonical": canonical,
           "candidate_type": cand.get("type", ""),
           "similarity": 0.0, "matched_standard_id": None,
           "diff": None, "mapping": None, "status": "novel", "basis": ""}

    # 1) 对象类精确命中
    if term in lib["classes"]:
        c = lib["classes"][term]
        cid_ = c.get("id") or c.get("code") or term
        cdom = c.get("domain") or c.get("section") or "—"
        out.update({"status": "covered", "similarity": 1.0,
                    "matched_standard_id": cid_,
                    "basis": f"参考库对象类精确命中：{term}（{cid_}，业务域 {cdom}）"})
        return out

    # 2) 字段精确命中
    if term in lib["field_index"]:
        rows = lib["field_index"][term]
        if term in lib["conflicts"]:
            specs = lib["conflicts"][term]
            own = defaultdict(list)
            for r in rows:
                own[(r["data_type"], r["constraint"], r["unit"])].append(
                    r.get("owner_name") or r.get("class_name") or "")
            diff = []
            for sp in specs:
                spec = f"{sp[0]} / {sp[1] or '—'}" + (f" / {sp[2]}" if sp[2] else "")
                owners = sorted({n for n in own.get(sp, []) if n})
                diff.append({"field": term, "existing": spec, "requirement": spec,
                             "owner_classes": owners, "owner_count": len(owners),
                             "note": "同一属性在不同对象类下口径不一致，需人工裁定统一取值"})
            allowners = sorted({(r.get("owner_name") or r.get("class_name") or "") for r in rows
                                if (r.get("owner_name") or r.get("class_name"))})
            out.update({"status": "conflict", "similarity": 1.0,
                        "matched_standard_id": (rows[0].get("attr_id")
                                                or f"{rows[0].get('class_name', '')}·{term}"),
                        "diff": diff,
                        "basis": f"参考库中「{term}」存在 {len(specs)} 种不同口径"
                                 f"（分布于 {len(allowners)} 个对象类："
                                 f"{'、'.join(allowners[:4])}{'…' if len(allowners) > 4 else ''}），需人工裁定"})
            return out
        r0 = rows[0]
        out.update({"status": "covered", "similarity": 1.0,
                    "matched_standard_id": (r0.get("attr_id")
                                            or f"{r0.get('class_name', '')}·{term}"),
                    "basis": f"参考库字段精确命中：{term}（{r0.get('data_type') or '—'} / "
                             f"{r0.get('constraint') or '—'}），口径一致"})
        return out

    # 3) 近似匹配（对象类 + 字段）—— 本地向量召回，不再用 Dice 字面重叠
    from tools import local_embedding as le
    idx = le.library_index(mode)
    hit = idx.best(term) if idx.items else None
    best, best_kind, best_id, best_name = 0.0, "", None, ""
    if hit:
        best, best_name = hit["score"], hit["key"]
        best_kind = hit["payload"].get("kind", "field")
        if best_kind == "class":
            c = hit["payload"].get("ref") or {}
            best_id = c.get("id") or c.get("code") or best_name
        else:
            r0 = hit["payload"].get("ref") or {}
            best_id = r0.get("attr_id") or f"{r0.get('class_name', '')}·{best_name}"

    if best >= VECTOR_THRESHOLD:
        out.update({"status": "similar", "similarity": best,
                    "matched_standard_id": best_id,
                    "mapping": {"from": term, "to": best_name, "kind": best_kind,
                                "suggestion": f"建议复用「{best_name}」的口径，"
                                              f"并把「{term}」登记为其同义词"},
                    "basis": f"与参考库条目「{best_name}」本地向量余弦 {best}"
                             f" ≥ 阈值 {VECTOR_THRESHOLD}，判定语义相近可复用"})
        return out

    out["basis"] = (f"参考库（{lib['class_count']} 对象类 / {lib['field_count']} 字段）中"
                    f"最高向量相似度仅 {best} < 阈值 {VECTOR_THRESHOLD}，判定为全新")
    out["similarity"] = best
    return out


# ---------------------------------------------------------------------------
# landscape_map
# ---------------------------------------------------------------------------
def _load_domains(mode: str = "A") -> list[str]:
    # D7 业务域清单：未来方向、无 P0 槽位，属于内置支撑清单（不是用户可替换数据）
    p = DATA_ROOT / "taxonomy.json"
    if p.exists():
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            return [d.get("domain", "") for d in t.get("domains", []) if d.get("domain")]
        except Exception:
            pass
    m = mi.load_mode_model(mode)
    return sorted({c["domain"] for c in m["classes"] if c["domain"]})


def _domain_of(term: str, cand: dict) -> str:
    """用 D7 业务域分类清单（内置支撑清单，无 P0 槽位）给候选归类。"""
    p = DATA_ROOT / "taxonomy.json"
    if p.exists():
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            for d in t.get("domains", []):
                dn = d.get("domain", "")
                for sd in d.get("subdomains", []):
                    sn = (sd.get("name") or "").strip()
                    if sn and (sn == term or sn in term or term in sn):
                        return dn
        except Exception:
            pass
    # 退化：按对象类所属业务域
    return cand.get("_domain") or "未归属"


def landscape_map(min_density: int = MIN_DENSITY, force: bool = False, mode: str = "A") -> dict:
    """业务域 × 文档章节矩阵 + 覆盖盲区。"""
    if not force and "map" in _CACHE:
        return _CACHE["map"]

    res = ke.extract("A")
    docs = res.get("documents", [])
    # 章节：D1 为扫描件，pdfplumber 无文本层，章节粒度退化为「文档级」
    sections = [d["file"] for d in docs] or ["（无文档）"]
    domains = _load_domains(mode) + ["未归属"]

    # 逐候选做新颖性判定
    enriched = []
    for c in res.get("candidates", []):
        nv = novelty_check(c, mode)
        cand = dict(c)
        cand["term"] = nv["term"]
        cand["novelty"] = nv
        cand["novelty_status"] = nv["status"]
        enriched.append(cand)

    # 优先级 + 业务域归属（供 P4 候选卡直接展示，避免前端二次计算口径不一致）
    prio = {p["candidate_id"]: p for p in prioritize_candidates(enriched)}
    for c in enriched:
        c["domain"] = _domain_of(c["term"], c)
        c["priority"] = prio.get(c["id"], {}).get("priority")
        c["priority_level"] = prio.get(c["id"], {}).get("level")
        c["priority_rationale"] = prio.get(c["id"], {}).get("rationale")

    # 候选 → 章节（来源文档）
    cand_section = {}
    for d in docs:
        for c in d.get("candidates", []):
            cand_section[c["id"]] = d["file"]

    matrix, blind = [], []
    for dom in domains:
        for sec in sections:
            cs = [c for c in enriched
                  if cand_section.get(c["id"], sections[0]) == sec and c.get("domain") == dom]
            avg = round(sum(c.get("confidence", 0) for c in cs) / len(cs), 3) if cs else 0.0
            cell = {"domain": dom, "section": sec, "candidate_count": len(cs),
                    "avg_confidence": avg,
                    "novelty_dist": dict(Counter(c["novelty_status"] for c in cs)),
                    "candidates": [{"id": c["id"], "name": c["term"], "type": c.get("type"),
                                    "confidence": c.get("confidence"),
                                    "novelty": c["novelty_status"]} for c in cs]}
            matrix.append(cell)
            if len(cs) < min_density:
                blind.append({"domain": dom, "section": sec, "candidate_count": len(cs),
                              "reason": f"候选数 {len(cs)} < 密度阈值 {min_density}"
                                        + ("（扫描件无文本层，仅能做主题弱匹配）" if not cs else "")})

    lib_now = load_mock_library(mode)
    degraded = {
        "D1 输入": "degraded：扫描件无文本层，章节粒度退化为文档级",
        "D7 业务域": "degraded：业务域仅 5 个（要求 ≥10）",
    }
    # 只有 D0 真的为空时才挂这条降级说明；有库就不提示（不再写死"用参考底座顶替"）
    if not lib_now.get("class_count"):
        degraded["已有标准库"] = ("degraded：本轮未提供 D0 已有标准库，新颖性无法与存量标准比对，"
                                  "候选一律按「全新」处理（不套用任何内置/参考库）")

    out = {
        "mode": "A",
        "matrix": matrix,
        "blind_spots": blind,
        "domains": domains,
        "sections": sections,
        "candidates": enriched,
        "config": {"min_density": min_density, "similar_threshold": SIMILAR_THRESHOLD,
                   "covered_threshold": COVERED_THRESHOLD},
        "degraded": degraded,
        "novelty_dist": dict(Counter(c["novelty_status"] for c in enriched)),
        "library": {k: v for k, v in lib_now.items()
                    if k in ("source", "source_note", "class_count", "field_count", "conflict_count")},
    }
    _CACHE["map"] = _llm_enrich_p4(out)
    return _CACHE["map"]


def _llm_enrich_p4(out: dict) -> dict:
    """P4 的大模型增强：新颖性判断（业务上是不是同一件事）+ 立项优先级。

    规则只能算字符串相似度（"像不像"），判不了"是不是同一件事"——
    这一段由模型给判断和理由，且每条都附命中的现有条目，便于人工复核。
    模型与规则结论不一致时两者都保留，界面标分歧，不静默覆盖。
    """
    import concurrent.futures as cf
    from tools import llm_client as llm
    from tools import llm_tasks as lt

    meta = {"available": False, "used": False, "reason": "", "model": "",
            "novelty_judged": 0, "prioritized": 0}
    try:
        st = llm.status()
        meta["available"] = st["available"]
        meta["reason"] = st.get("reason", "")
        if not st["available"]:
            out["llm"] = meta
            return out

        bulk = (llm.config().get("bulk_model") or "").strip() or None
        lib = load_mock_library(out.get("mode", "A"))
        existing = sorted(set(list((lib.get("classes") or {}).keys())
                              + list((lib.get("field_index") or {}).keys())))[:80]
        cands = out.get("candidates") or []
        # 只对前 10 条做 LLM 判断，控制时延（15 条 + 200 条现有条目实测到 5 分钟以上）
        head = cands[:8]

        got_nov, got_pri = {"items": [], "model": ""}, {"items": [], "model": ""}
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_nov = ex.submit(lt.judge_novelty, head, existing, model=bulk, timeout=180)
            f_pri = ex.submit(lt.prioritize, head, out.get("blind_spots"),
                              model=bulk, timeout=180)
            try:
                got_nov = f_nov.result()
            except Exception as e:
                meta["reason"] = (meta["reason"] + f"；新颖性判断失败：{e}").strip("；")
            try:
                got_pri = f_pri.result()
            except Exception as e:
                meta["reason"] = (meta["reason"] + f"；优先级排序失败：{e}").strip("；")

        by = {g["name"]: g for g in got_nov["items"]}
        for c in head:
            g = by.get(c.get("term") or c.get("name") or "")
            if not g:
                continue
            c["llm_verdict"] = g["verdict"]
            c["llm_matched"] = g["matched"]
            c["llm_reason"] = g["reason"]
            c["llm_confidence"] = g["confidence"]
            c["llm_agrees_rule"] = (
                (g["verdict"] == "可复用" and c.get("novelty_status") == "covered")
                or (g["verdict"] == "确为新增" and c.get("novelty_status") == "novel")
                or g["verdict"] == "需合并")
            meta["novelty_judged"] += 1
        meta["used"] = meta["used"] or bool(got_nov["items"])
        meta["model"] = got_nov.get("model") or meta["model"]

        by = {g["name"]: g for g in got_pri["items"]}
        for c in head:
            g = by.get(c.get("term") or c.get("name") or "")
            if not g:
                continue
            c["llm_priority"] = g["priority"]
            c["llm_score"] = g["score"]
            c["llm_priority_reason"] = g["reason"]
            meta["prioritized"] += 1
        meta["used"] = meta["used"] or bool(got_pri["items"])
        meta["model"] = got_pri.get("model") or meta["model"]
    except Exception as e:
        meta["reason"] = (meta["reason"] + f"；{e}").strip("；")

    out["llm"] = meta
    return out




# ---------------------------------------------------------------------------
# prioritize_candidates
# ---------------------------------------------------------------------------
def prioritize_candidates(cands: list[dict]) -> list[dict]:
    """优先级 = 新颖性权重 × 0.4 + 置信度 × 0.3 + 证据强度 × 0.2 + 类型权重 × 0.1。"""
    nov_w = {"conflict": 1.0, "novel": 0.85, "similar": 0.5, "covered": 0.2}
    type_w = {"object_class": 1.0, "attribute": 0.9, "relation": 0.8,
              "value_domain": 0.5, "unit": 0.3, "rule": 0.7}
    out = []
    for c in cands:
        nv = c.get("novelty") or {}
        st = nv.get("status", "novel")
        conf = float(c.get("confidence", 0) or 0)
        ev = c.get("evidence_span") or {}
        # 证据强度：有明确出处（行号/跨度）> 仅文件名主题 > 领域常识
        if ev.get("row") or (ev.get("span") and ev.get("span") != f"文件名主题「{ev.get('doc','')}」"):
            evs = 1.0
        elif ev.get("doc"):
            evs = 0.5
        else:
            evs = 0.2
        tw = type_w.get(c.get("type", ""), 0.5)
        score = round(nov_w.get(st, 0.5) * 0.4 + conf * 0.3 + evs * 0.2 + tw * 0.1, 4)
        out.append({
            "candidate_id": c.get("id"),
            "name": c.get("term") or c.get("name"),
            "priority": score,
            "level": "高" if score >= 0.7 else ("中" if score >= 0.5 else "低"),
            "rationale": f"新颖性 {st}（权重 {nov_w.get(st, 0.5)}）×0.4 + 置信度 {conf}×0.3 "
                         f"+ 证据强度 {evs}×0.2 + 类型 {c.get('type','')}（权重 {tw}）×0.1 = {score}",
        })
    out.sort(key=lambda x: -x["priority"])
    return out


# ---------------------------------------------------------------------------
# 立项
# ---------------------------------------------------------------------------
def estimate_effort(cands: list[dict]) -> dict:
    """工作量估算：基准人日 × (1 + (1-置信度) × 0.5)，公式显式。"""
    total, items = 0.0, []
    for c in cands:
        base = EFFORT_BASE.get(c.get("type", ""), 1.0)
        conf = float(c.get("confidence", 0) or 0)
        e = round(base * (1 + (1 - conf) * 0.5), 2)
        total += e
        items.append({"candidate_id": c.get("id"), "name": c.get("term") or c.get("name"),
                      "type": c.get("type"), "base": base, "confidence": conf, "effort_days": e,
                      "formula": f"{base} × (1 + (1 - {conf}) × 0.5) = {e}"})
    return {"total_days": round(total, 2), "items": items,
            "formula": "人日 = 类型基准(object_class 3.0 / attribute 1.5 / relation 2.0 / "
                       "value_domain 1.0 / unit 0.5 / rule 2.5) × (1 + (1 - confidence) × 0.5)"}


def approve(candidate_ids: list[str], note: str = "") -> dict:
    """提交立项清单 → 写 deliverables/mode-a/approved-candidates/。"""
    m = landscape_map()
    idx = {c["id"]: c for c in m["candidates"]}
    picked = [idx[i] for i in candidate_ids if i in idx]
    prio = {p["candidate_id"]: p for p in prioritize_candidates(m["candidates"])}
    eff = estimate_effort(picked)
    payload = {
        "schema": "mode-a/approved-candidates/v1",
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "count": len(picked),
        "novelty_dist": dict(Counter(c["novelty_status"] for c in picked)),
        "type_dist": dict(Counter(c.get("type", "") for c in picked)),
        "effort": eff,
        "library_note": m["library"]["source_note"],
        "candidates": [{
            "id": c["id"], "term": c.get("term"), "type": c.get("type"),
            "definition": c.get("definition", ""),
            "confidence": c.get("confidence"),
            "novelty": c["novelty"],
            "priority": prio.get(c["id"], {}).get("priority"),
            "priority_level": prio.get(c["id"], {}).get("level"),
            "rationale": prio.get(c["id"], {}).get("rationale"),
            "evidence_span": c.get("evidence_span", {}),
        } for c in picked],
    }
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = APPROVED_DIR / f"approved-{ts}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (APPROVED_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    payload["path"] = str(p)
    return payload


def blind_spots(min_density: int = MIN_DENSITY) -> dict:
    m = landscape_map(min_density=min_density)
    return {"min_density": min_density, "count": len(m["blind_spots"]),
            "items": m["blind_spots"], "degraded": m["degraded"]}


if __name__ == "__main__":
    m = landscape_map(mode="A")
    lib = load_mock_library("A")
    print("模拟库:", lib["source"], "| 类", lib["class_count"], "| 字段", lib["field_count"],
          "| 真实口径冲突字段", lib["conflict_count"])
    print("新颖性分布:", m["novelty_dist"])
    print("矩阵单元格:", len(m["matrix"]), "| 盲区:", len(m["blind_spots"]))
    print("业务域:", m["domains"])
    print("章节:", m["sections"])
    print()
    for c in m["candidates"][:14]:
        nv = c["novelty"]
        print(f"  {c['term']:<12} {nv['status']:<9} sim={nv['similarity']:<6} {nv['basis'][:60]}")
    print()
    for p in prioritize_candidates(m["candidates"])[:5]:
        print("  优先级", p["priority"], p["level"], p["name"])
