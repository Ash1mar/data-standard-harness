"""
confidence.py
=============
P2 候选置信度：**规则基线 + 大模型裁决 + 落盘缓存**。

背景（2026-10-24 改造）
-----------------------
改造前，候选置信度是写死的常数：
    A 模式候选 0.5、A 模式广度补充 0.45、B 模式对象类 0.82、B 模式属性 0.88 / 0.62。
那不是置信度，是手工分档 —— 候选形态一样分数就一样，跟证据强弱无关。
用户明确要求：「confidence 不能固定为一个数字，可以借助大模型的能力判断」。

现在的做法分三段：

① `baseline()` 规则基线（不调模型，毫秒级，几万条也能跑）
   只看**证据形态**：证据来自表格行还是标题、规格字段（类型/单位/约束/定义）填了几项、
   有没有原文片段。给出分数 + 一句人话依据（`confidence_basis`），可解释、可复现。

② `adjudicate()` 模型裁决（只对模糊区间，批量）
   只把基线落在 [0.40, 0.80] 的候选交模型，且**要它给「高 / 中 / 低 + 一句依据」，
   不要裸概率**。原因：模型对术语天生乐观，直接要 0–1 分会得到一片 0.9，
   颜色就失去区分度，等于换个方式写死。

③ `fuse()` 融合（带限幅，模型不能为所欲为）
   final = 0.4·base + 0.6·mapped，然后
     · 上限 base + 0.12 —— 字段全空的候选，模型不能把它抬成高分
     · 下限 base − 0.25 —— 表格行规格齐全的候选，模型说"低"也砍不动
   这样分数既有模型判断，又不会脱离证据。

缓存
----
按 (名称, 类型, 基线分, 证据摘要) 哈希落盘 `data/.llm_cache/confidence.json`。
重复点 P2 秒回，不让模型为同一批候选反复烧钱。模型不可用时只用基线，
并在返回的 meta 里如实写 `used=False` 与原因，**不假装跑过模型**。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_PATH = DATA_ROOT / ".llm_cache" / "confidence.json"

# 模糊区间：只有落在这里面的才值得问模型。
# 太高（证据确凿）问了也是浪费，太低（连名字都不可信）模型也救不回来。
AMBIGUOUS = (0.40, 0.80)

# P2「低置信」阈值（2026-10-24 用户要求 0.7 → 0.5）。
# 0.7 会把「表格行·仅名称」0.68、「标题层级」0.58 这类正常证据也判成低置信，
# 导致几乎全屏标红、P8 门控被灌满，失去提示意义；0.5 只留下真正站不住的候选。
LOW_CONF = 0.5

# 「未评估」桶：节点压根没有置信度（不是低置信）。
# 典型来源：D2 需求表本身就没有置信度列，base_supply 兜底补建的类也没有证据可评。
# **不能把它当成 0 或 1**：当 0 会被满屏标红误导成"低质量"，当 1 等于凭空背书。
UNKNOWN_BUCKET = "unknown(未评估)"
UNKNOWN_SOURCE = "unevaluated"


def as_float(v, default=None):
    """宽松转 float：None / '' / 'N/A' / bool 一律返回 default。

    存在的理由：`dict.get(k, default)` 只在**键不存在**时给默认值，键存在但值是 null
    时照样返回 None，直接拿去比较会抛 TypeError（P3 统计就这么崩过）。
    """
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def is_evaluated(v) -> bool:
    """这个置信度值是否真的评过（数值才算）。"""
    return as_float(v) is not None

# 模型给的离散等级 → 分数。刻意用三档，不用连续概率。
LEVEL_SCORE = {"高": 0.88, "中": 0.62, "低": 0.35}

# 融合权重与限幅
W_BASE, W_MODEL = 0.4, 0.6
UP_CAP, DOWN_CAP = 0.12, 0.25

# 调用规模控制：一次批量 25 条，单次 P2 最多问 100 条
BATCH = 25
MAX_JUDGE = 100


# ---------------------------------------------------------------------------
# 规则基线
# ---------------------------------------------------------------------------
# 证据形态 → (基线分, 依据标签)。按优先级从高到低排，第一个命中的生效。
_KIND_BASE = {
    # 表格行：文档里真实存在的结构化记录，最可信
    "table_row_full": (0.90, "表格行·类型/单位/约束/定义齐全"),
    "table_row_part": (0.80, "表格行·规格字段部分填写"),
    "table_row_bare": (0.68, "表格行·仅名称，无规格字段"),
    # 文档标题：结构明确，但"标题是类"仍需语义判断
    "heading_top": (0.66, "文档标题（一级/二级）"),
    "heading_sub": (0.58, "文档子标题（三级及以下）"),
    # 正文陈述句：有原文可查，但形态不固定
    "text_line": (0.52, "正文陈述句（有原文片段）"),
    # 文件名/主题词兜底：没有正文证据
    "seed": (0.32, "仅文件名/领域主题弱匹配，无正文证据"),
    # 什么都没有
    "none": (0.25, "无任何证据"),
}

# 证据档位：给图着色用。**颜色/描边由证据类型决定，不是把分数映射成色带** ——
# 0.58 和 0.61 肉眼分不出，但「表格行」和「正文一句话」一眼就能分开。
TIERS = [
    {"id": "T1", "label": "表格行·规格齐全", "keys": ["table_row_full"],
     "color": "#1f7a5c", "border": "solid", "width": 1.4},
    {"id": "T2", "label": "表格行·部分/仅名", "keys": ["table_row_part", "table_row_bare"],
     "color": "#4f9b74", "border": "solid", "width": 1.1},
    {"id": "T3", "label": "标题层级", "keys": ["heading_top", "heading_sub"],
     "color": "#c9a227", "border": "dashed", "width": 1.1},
    {"id": "T4", "label": "正文陈述", "keys": ["text_line"],
     "color": "#d9772b", "border": "dotted", "width": 1.1},
    {"id": "T5", "label": "弱兜底（无正文证据）", "keys": ["seed", "none"],
     "color": "#b8483f", "border": "dotted", "width": 0.9},
]
_KEY2TIER = {k: t for t in TIERS for k in t["keys"]}

# 「未评估」不是证据档位，是"根本没有证据可评"（D2 需求表无置信度列、兜底补建的类）。
# 单独给一档，避免被硬塞进 T5（弱兜底）而显得像"抽取质量差"。
TIER_UNKNOWN = {"id": "TX", "label": "未评估（无置信度）", "keys": [],
                "color": "#b9b2a6", "border": "dashed", "width": 0.9}


def tier_of_key(key: str) -> dict:
    """证据 key（table_row_full 之类）→ 档位。未知归 T5。"""
    return _KEY2TIER.get(key or "", TIERS[-1])


def tier_for(cand: dict) -> dict:
    """候选 → 档位（不调模型，纯规则）。"""
    return tier_of_key(evidence_key(cand))


def tier_for_node(n: dict) -> dict:
    """图谱节点 → 档位。

    节点上只留了 evidence_kind 与规格字段（图里不存整段原文），
    这里用同一条规则重建 key，保证**图上的分档与 P2 的置信度同源**。

    没有数值置信度的节点直接归「未评估」，不参与档位推断 ——
    它们本来就没有证据可评，推出来的档位是假的。
    """
    if not is_evaluated(n.get("confidence")):
        return TIER_UNKNOWN
    pseudo = {
        "_evidence_kind": n.get("evidence_kind") or "",
        "_section_level": n.get("section_level") or 3,
        "name": n.get("label") or "",
        "data_type": n.get("data_type") or "",
        "constraint": n.get("constraint") or "",
        "unit": n.get("unit") or "",
        "definition": n.get("definition") or "",
        "class_name": n.get("owner_class") or "",
        "evidence_span": n.get("evidence_span") or {},
    }
    if not pseudo["_evidence_kind"]:
        # 旧图没有 evidence_kind：从依据文字反推，不让老数据掉成 T5
        pseudo["evidence_span"] = {"basis": n.get("confidence_basis") or ""}
    return tier_of_key(evidence_key(pseudo))


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _spec_filled(cand: dict) -> int:
    """规格字段填了几项（类型 / 约束 / 单位 / 定义）。定义若是占位不算。"""
    n = 0
    for k in ("data_type", "constraint", "unit"):
        if _s(cand.get(k)):
            n += 1
    d = _s(cand.get("definition"))
    if d and not d.startswith("（"):
        n += 1
    return n


def _looks_unlike_name(name: str) -> bool:
    """名字太长、含编号/标点、像整句话 —— 不像规范术语，扣分。"""
    if not name:
        return True
    if len(name) > 24:
        return True
    if any(ch in name for ch in "。；;，,：:（）()[]{}\n\t"):
        return True
    return False


def evidence_kind(cand: dict) -> str:
    """判定证据形态。优先用调用方显式标注的 `_evidence_kind`。"""
    k = _s(cand.get("_evidence_kind"))
    if k:
        return k
    ev = cand.get("evidence_span") or {}
    k = _s(ev.get("kind"))
    if k:
        return k
    # 旧结构兜底：从 basis 文字反推
    basis = _s(ev.get("basis"))
    row = ev.get("row")
    if row is not None or "表格" in basis:
        return "table_row_part"
    if "标题" in basis:
        return "heading_sub"
    if _s(ev.get("span")):
        return "text_line"
    return "none"


def evidence_key(cand: dict) -> str:
    """证据形态 → `_KIND_BASE` 的 key（分档与打分共用这一个判定）。"""
    kind = evidence_kind(cand)
    if kind == "table_row":
        spec = _spec_filled(cand)
        return "table_row_full" if spec >= 3 else ("table_row_part" if spec >= 1 else "table_row_bare")
    if kind == "heading":
        # 标题层级：1/2 级算顶层
        lvl = cand.get("_section_level") or (cand.get("evidence_span") or {}).get("section_level") or 3
        try:
            lvl = int(lvl)
        except Exception:
            lvl = 3
        return "heading_top" if lvl <= 2 else "heading_sub"
    if kind in _KIND_BASE:
        return kind
    return "none"


def baseline(cand: dict) -> tuple[float, str]:
    """规则基线：返回 (分数, 依据标签)。不调模型。"""
    kind = evidence_kind(cand)
    key = evidence_key(cand)

    score, label = _KIND_BASE[key]

    # 修正项（都在小范围内，不改变量级）
    adj = []
    if _looks_unlike_name(_s(cand.get("name"))):
        score -= 0.10
        adj.append("名称不像规范术语 −0.10")
    ev = cand.get("evidence_span") or {}
    span = _s(ev.get("span"))
    if kind in ("table_row", "text_line") and len(span) >= 10:
        score += 0.04
        adj.append("原文片段充分 +0.04")
    if kind == "table_row" and _s(cand.get("class_name")):
        # 表格行且已明确归属某个对象类
        score += 0.03
        adj.append("已明确归属对象类 +0.03")

    score = max(0.05, min(0.95, score))
    if adj:
        label = f"{label}；{'，'.join(adj)}"
    return round(score, 3), label


# ---------------------------------------------------------------------------
# 模型裁决
# ---------------------------------------------------------------------------
def _cache_key(cand: dict) -> str:
    raw = "|".join([
        _s(cand.get("name")),
        _s(cand.get("type")),
        f"{cand.get('confidence_base', '')}",
        _s((cand.get("evidence_span") or {}).get("span"))[:60],
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_save(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 只保留最近 4000 条，避免无限膨胀
        if len(cache) > 4000:
            keep = list(cache.items())[-4000:]
            cache = dict(keep)
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _fuse(base: float, level: str) -> float:
    mapped = LEVEL_SCORE.get(level)
    if mapped is None:
        return base
    final = W_BASE * base + W_MODEL * mapped
    final = min(final, base + UP_CAP)
    final = max(final, base - DOWN_CAP)
    return round(max(0.05, min(0.95, final)), 3)


def _payload_one(c: dict) -> dict:
    ev = c.get("evidence_span") or {}
    fields = []
    for k, cn in (("data_type", "类型"), ("constraint", "约束"),
                  ("unit", "单位"), ("definition", "定义")):
        v = _s(c.get(k))
        if v and not v.startswith("（"):
            fields.append(f"{cn}={v[:20]}")
    return {
        "name": _s(c.get("name")),
        "type": "对象类" if c.get("type") == "object_class" else "属性",
        "rule_score": c.get("confidence_base"),
        "rule_basis": c.get("confidence_basis", ""),
        "fields": "；".join(fields) or "（无规格字段）",
        "evidence": _s(ev.get("span"))[:120] or "（无原文片段）",
    }


def adjudicate(cands: list[dict], *, model: str | None = None,
               timeout: float = 150) -> dict:
    """把模糊区间的候选批量交给模型裁决。返回 meta（含判了多少条）。"""
    meta = {"used": False, "reason": "", "ambiguous": 0, "judged": 0,
            "cached": 0, "model": "", "elapsed_ms": 0, "truncated": 0}
    if not cands:
        meta["reason"] = "无候选"
        return meta

    amb = [c for c in cands if AMBIGUOUS[0] <= float(c.get("confidence_base") or 0) <= AMBIGUOUS[1]]
    meta["ambiguous"] = len(amb)
    if not amb:
        meta["reason"] = "全部候选由规则判定（无一落在模糊区间）"
        return meta

    cache = _cache_load()
    picked, cache_hits = [], 0
    for c in amb:
        k = _cache_key(c)
        hit = cache.get(k)
        if hit and hit.get("level") in LEVEL_SCORE:
            c["confidence"] = _fuse(float(c["confidence_base"]), hit["level"])
            c["confidence_level"] = hit["level"]
            c["confidence_reason"] = hit.get("reason", "")
            c["confidence_source"] = "llm-cache"
            cache_hits += 1
        else:
            picked.append((k, c))
    meta["cached"] = cache_hits

    if len(picked) > MAX_JUDGE:
        meta["truncated"] = len(picked) - MAX_JUDGE
        picked = picked[:MAX_JUDGE]
    if not picked:
        meta["used"] = True
        meta["reason"] = "全部命中缓存"
        return meta

    try:
        from tools import llm_client as llm
        from tools import llm_tasks as lt
        st = llm.status()
        if not st.get("available"):
            meta["reason"] = f"模型不可用：{st.get('reason', '')}；仅用规则基线"
            return meta
        if not model:
            model = (llm.config().get("bulk_model") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        meta["reason"] = f"模型不可用：{e}；仅用规则基线"
        return meta

    t0 = time.time()
    judged_total = 0
    try:
        for i in range(0, len(picked), BATCH):
            batch = picked[i:i + BATCH]
            got = lt.judge_confidence([_payload_one(c) for _, c in batch],
                                      model=model, timeout=timeout)
            meta["model"] = got.get("model") or meta["model"]
            by_name = {}
            for it in got.get("items") or []:
                by_name[it["name"]] = it
            for k, c in batch:
                it = by_name.get(_s(c.get("name")))
                if not it:
                    continue
                lvl = it.get("level") or ""
                if lvl not in LEVEL_SCORE:
                    continue
                c["confidence"] = _fuse(float(c["confidence_base"]), lvl)
                c["confidence_level"] = lvl
                c["confidence_reason"] = it.get("reason", "")
                c["confidence_source"] = "llm"
                cache[k] = {"level": lvl, "reason": it.get("reason", "")}
                judged_total += 1
            meta["used"] = True
    except Exception as e:  # noqa: BLE001
        meta["reason"] = f"模型裁决失败：{type(e).__name__}: {e}；未判的候选保留规则基线"
    finally:
        meta["elapsed_ms"] = int((time.time() - t0) * 1000)
        if judged_total:
            _cache_save(cache)

    meta["judged"] = judged_total
    if not meta["reason"]:
        meta["reason"] = f"模糊区间 {meta['ambiguous']} 条，模型裁决 {judged_total} 条，缓存命中 {cache_hits} 条"
    return meta


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def calibrate(cands: list[dict], *, llm_allowed: bool = True,
              model: str | None = None) -> dict:
    """给每个候选算最终置信度，原地写回这三个字段：

        confidence         最终分（0–1，连续，不再是一个常数）
        confidence_base    规则基线分
        confidence_basis   规则依据（人话）
        confidence_source  rule | llm | llm-cache

    `llm_allowed=False` 时只用规则基线（P3 复用候选入图时不烧模型）。
    """
    for c in cands:
        s, label = baseline(c)
        c["confidence_base"] = s
        c["confidence_basis"] = label
        c["confidence"] = s
        c["confidence_source"] = "rule"
        t = tier_of_key(evidence_key(c))
        c["confidence_tier"] = t["id"]
        c["confidence_tier_label"] = t["label"]

    if not llm_allowed:
        return {"used": False, "reason": "本次未启用模型（仅规则基线）",
                "ambiguous": sum(1 for c in cands if AMBIGUOUS[0] <= c["confidence_base"] <= AMBIGUOUS[1]),
                "judged": 0, "cached": 0, "model": "", "elapsed_ms": 0, "truncated": 0}
    return adjudicate(cands, model=model)


def dist(cands: list[dict]) -> dict:
    """置信度分布 + 证据档位分布，供前端画图例。"""
    buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    tiers = {t["id"]: 0 for t in TIERS}
    for c in cands:
        v = float(c.get("confidence") or 0)
        if v < 0.2:
            buckets["0.0-0.2"] += 1
        elif v < 0.4:
            buckets["0.2-0.4"] += 1
        elif v < 0.6:
            buckets["0.4-0.6"] += 1
        elif v < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets["0.8-1.0"] += 1
        tid = c.get("confidence_tier") or tier_for(c)["id"]
        tiers[tid] = tiers.get(tid, 0) + 1
    vals = [float(c.get("confidence") or 0) for c in cands]
    return {"buckets": buckets,
            "tiers": [{"id": t["id"], "label": t["label"], "color": t["color"],
                       "count": tiers.get(t["id"], 0)} for t in TIERS],
            "min": round(min(vals), 3) if vals else 0,
            "max": round(max(vals), 3) if vals else 0,
            "avg": round(sum(vals) / len(vals), 3) if vals else 0,
            "distinct": len({round(v, 3) for v in vals})}
