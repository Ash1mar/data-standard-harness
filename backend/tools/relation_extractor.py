"""
relation_extractor.py
=====================
关系抽取（P2 第三类候选：对象类 / 属性 / **关系**）。

背景（2026-10-23）
- P2 原来只产「对象类」「属性」两类候选，关系一条都没有；P7 的 3.1 指标也止步于
  对象类层、属性层。结果：图谱里的 subClassOf / hasAttribute 是构建期硬拼出来的，
  没有任何抽取环节对它负责，也没人评测它对不对。
- 本模块把关系显式抽成候选，三种类型：
    · subClassOf  —— 上下位（「X 是 Y 的一种」「X 属于 Y」、md 标题层级、父类ID 列）
    · hasAttribute—— 归属（「对象类属性」表的一行 = 类拥有该属性）
    · associates  —— 弱关联（「X 与 Y 连接/组成/配合」，仅作线索，不参与评测）

抽取原则：**每条关系都要有出处**（文件 + 行号/列/句式），没有出处的宁可不抽。
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 中文上下位句式（保守：必须有明确的关系词，避免把并列句当层级）
_SUB_PATTERNS = [
    (re.compile(r"^(?P<sub>[^，。；、\s]{2,20})(?:类)?(?:属于|是)(?P<sup>[^，。；、\s]{2,20})(?:的)?(?:一种|一类|子类|下级|类型)"), 0.82),
    (re.compile(r"^(?P<sup>[^，。；、\s]{2,20})(?:包括|包含|分为|划分为)(?P<sub>[^，。；、\s]{2,20})"), 0.72),
    (re.compile(r"^(?P<sub>[^，。；、\s]{2,20})[（(](?P<sup>[^，。；、）)]{2,20})[）)]$"), 0.55),
]
_ASSOC_PATTERNS = [
    re.compile(r"^(?P<a>[^，。；、\s]{2,20})(?:与|和|同)(?P<b>[^，。；、\s]{2,20})(?:相连|连接|配合|组成|构成|搭接)"),
    re.compile(r"^(?P<a>[^，。；、\s]{2,20})(?:由)(?P<b>[^，。；、\s]{2,20})(?:组成|构成)"),
]
_SENT_SPLIT = re.compile(r"[。；;！!\n]+")


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _clean_name(s: str) -> str:
    s = _s(s)
    s = re.sub(r"^[（(]?\d+[)）.、]\s*", "", s)   # 去掉「1.」「（2）」这类编号
    return s.strip(" 　:：-")


def _add(bucket: list[dict], seen: set, rel: dict) -> None:
    key = (rel["type"], rel["head"], rel["tail"])
    if not rel["head"] or not rel["tail"] or rel["head"] == rel["tail"]:
        return
    if key in seen:
        return
    seen.add(key)
    bucket.append(rel)


# ---------------------------------------------------------------------------
# ① 需求表（D2）：父类ID → subClassOf；属性归属 → hasAttribute
# ---------------------------------------------------------------------------
def from_requirement(ds: dict) -> dict:
    """D2 需求表的关系抽取。

    - 对象类表的「父类ID」→ subClassOf（父类不在本次表内则丢弃并计数，不臆猜父类名）
    - 「对象类属性」表每行 → hasAttribute（类拥有该属性，带行号作证据）
    """
    rels: list[dict] = []
    seen: set = set()
    classes = ds.get("classes") or []
    id2name = {_s(c.get("id")): _s(c.get("name")) for c in classes if c.get("id") and c.get("name")}
    missing_parent = 0
    for c in classes:
        pid = _s(c.get("parent_id"))
        nm = _s(c.get("name"))
        if not pid or not nm:
            continue
        sup = id2name.get(pid)
        if not sup:
            missing_parent += 1
            continue
        _add(rels, seen, {
            "type": "subClassOf", "head": sup, "tail": nm,
            "head_id": pid, "tail_id": _s(c.get("id")),
            "confidence": 0.9, "pattern": "父类ID 列",
            "evidence": {"file": _s(c.get("file")), "column": "父类ID"},
        })

    for r in ds.get("rows") or []:
        cn, an = _s(r.get("object_class")), _s(r.get("name"))
        if not cn or not an:
            continue
        _add(rels, seen, {
            "type": "hasAttribute", "head": cn, "tail": an,
            "confidence": 0.9 if (r.get("data_type_raw") or r.get("fmt")) else 0.62,
            "pattern": "对象类属性表行",
            "evidence": {"file": _s(r.get("file")), "sheet": "对象类属性", "row": r.get("row"),
                         "span": f"{cn}·{an}"},
        })

    notes = []
    if missing_parent:
        notes.append(f"{missing_parent} 个类的父类ID 不在本次需求表内，subClassOf 未建")
    return {"relations": rels, "counts": dict(Counter(r["type"] for r in rels)),
            "note": "；".join(notes) or "父类ID 与属性归属齐备"}


# ---------------------------------------------------------------------------
# ② 文档（D1）：md 标题层级 → subClassOf；表格 → hasAttribute；正文句式 → subClassOf/associates
# ---------------------------------------------------------------------------
_ATTR_NAME_COLS = ("属性中文名称", "属性名称", "字段名称", "字段中文名", "中文名称", "名称", "数据项名称")
_ATTR_SPEC_COLS = ("数据类型", "数据格式/约束", "约束", "属性单位", "单位", "值域", "量纲")


def _find(hdr: list[str], names: tuple[str, ...]) -> int:
    for n in names:
        if n in hdr:
            return hdr.index(n)
    return -1


def from_documents(docs: list[dict]) -> dict:
    """从 D1 文档抽关系。只接受有明确结构/句式的证据。"""
    rels: list[dict] = []
    seen: set = set()
    sect = 0

    for d in docs:
        fname = _s(d.get("file"))
        stack: list[tuple[int, str]] = []
        for pg in d.get("pages") or []:
            # ① md 标题层级 → subClassOf（## 管道 / ### 管件 ⇒ 管件 subClassOf 管道）
            if d.get("fmt") == "md" and pg.get("section_level"):
                lvl, title = int(pg["section_level"]), _clean_name(pg.get("section"))
                while stack and stack[-1][0] >= lvl:
                    stack.pop()
                if title and stack:
                    _add(rels, seen, {
                        "type": "subClassOf", "head": stack[-1][1], "tail": title,
                        "confidence": 0.78, "pattern": "md 标题层级",
                        "evidence": {"file": fname, "section": pg.get("section"),
                                     "span": f"{'#' * lvl} {title} ⊂ {stack[-1][1]}"},
                    })
                if title:
                    stack.append((lvl, title))
                sect += 1
                continue

            owner = _clean_name(pg.get("section")) or Path(fname).stem
            # ② 表格：属性表 → hasAttribute（归属当前节）
            for grid in pg.get("tables") or []:
                if not grid or len(grid) < 2:
                    continue
                hdr = [_s(c) for c in grid[0]]
                if _find(hdr, _ATTR_NAME_COLS) < 0 or _find(hdr, _ATTR_SPEC_COLS) < 0:
                    continue
                ni = _find(hdr, _ATTR_NAME_COLS)
                for ri, row in enumerate(grid[1:], start=2):
                    nm = _clean_name(row[ni]) if 0 <= ni < len(row) else ""
                    if not nm or len(nm) > 40:
                        continue
                    _add(rels, seen, {
                        "type": "hasAttribute", "head": owner, "tail": nm,
                        "confidence": 0.75, "pattern": "文档属性表行",
                        "evidence": {"file": fname, "section": pg.get("section"), "row": ri,
                                     "span": f"{owner}·{nm}"},
                    })

            # ③ 正文句式：逐句匹配（保守，只认关系词）
            text = "\n".join(_s(b.get("text")) for b in (pg.get("text_blocks") or []))
            for sent in _SENT_SPLIT.split(text):
                sent = _clean_name(sent.replace("|", " "))
                if not sent or len(sent) > 60:
                    continue
                for pat, conf in _SUB_PATTERNS:
                    m = pat.match(sent)
                    if m:
                        _add(rels, seen, {
                            "type": "subClassOf",
                            "head": _clean_name(m.group("sup")), "tail": _clean_name(m.group("sub")),
                            "confidence": conf, "pattern": "正文上下位句式",
                            "evidence": {"file": fname, "span": sent[:80]},
                        })
                        break
                else:
                    for pat in _ASSOC_PATTERNS:
                        m = pat.match(sent)
                        if m:
                            _add(rels, seen, {
                                "type": "associates",
                                "head": _clean_name(m.group("a")), "tail": _clean_name(m.group("b")),
                                "confidence": 0.55, "pattern": "正文关联句式",
                                "evidence": {"file": fname, "span": sent[:80]},
                            })
                            break

    counts = dict(Counter(r["type"] for r in rels))
    return {"relations": rels, "counts": counts,
            "note": ("文档结构/句式抽取："
                     f"subClassOf {counts.get('subClassOf', 0)}、"
                     f"hasAttribute {counts.get('hasAttribute', 0)}、"
                     f"associates {counts.get('associates', 0)}")}


def summarize(rel_res: dict) -> dict:
    """给前端用的分布摘要（按类型 + 按置信度）。"""
    rels = rel_res.get("relations") or []
    return {
        "total": len(rels),
        "type_dist": dict(Counter(r["type"] for r in rels)),
        "low_confidence": sum(1 for r in rels if r.get("confidence", 0) < 0.7),
        "pattern_dist": dict(Counter(r.get("pattern", "") for r in rels)),
        "note": rel_res.get("note", ""),
    }
