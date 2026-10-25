"""
knowledge_extractor.py
=======================
核电数据标准智能治理智能体（双模式）· 知识抽取工具（增量 2 · P2）。

设计原则（诚实、可追溯）：
- 模式 A：两份 D1 文档为扫描件，pdfplumber 未抽取出文本层/可用表格
  （parse_quality=degraded，text_chars=0）。因此从「文档主题（文件名）+ 参考底座
  （已有标准模拟库 = E板块162 + EM4 57 + XMind 分类树）」做弱匹配生成候选，
  全部低置信度，并显式标注 extraction_basis。这是 D1 degraded 的真实后果，
  不做任何正文编造。
- 模式 B：需求文件可解析，直接做目标导向抽取，并对 D4 完整模型模板做
  「已覆盖 / 部分覆盖 / 未覆盖」对齐。
- term_normalize 的同义词表已下线（D6 删除），统一返回 method="none"。
- novelty 检测使用参考底座模拟库（真实已有标准库缺失时的顶替规则），检索源不含任何预期输出。

所有候选均带 evidence_span（原文/来源出处）与 confidence，低置信 < 0.5 置顶标红。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# 允许以 backend/ 为根导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools import confidence as cf  # noqa: E402
from tools import doc_parser as dp  # noqa: E402
from tools import relation_extractor as rx  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

# 缓存，避免每次演示重复计算
_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 参考库 / 词表加载
# ---------------------------------------------------------------------------
def load_domain_vocab() -> tuple[dict[str, str], set[str]]:
    """D6 领域词表已下线（用户决定彻底删除）。

    保留函数签名只为兼容调用方：返回空映射，术语归一退化为 method="none"。
    """
    return {}, set()

def load_d0_reference_names(mode: str = "A") -> set[str]:
    """已有标准库里的名称集合 —— **只读 D0**（不再拿建模集/预期输出当参考库）。

    以前这里读 `mode_isolation.load_mode_model()`，等于用 D3 答案判断
    "已覆盖/全新"；现在改成 D0（用户上传的存量标准）。D0 为空时返回空集合，
    所有候选按"未在已有标准库命中"处理，不会去偷偷读任何内置答案。
    """
    try:
        from tools import landscape_analyst as _la
        lib = _la.load_mock_library(mode)
        names: set[str] = set(lib.get("classes") or {})
        names |= set((lib.get("field_index") or {}).keys())
        if mode == "B":
            names |= _xmind_names()
        return {n for n in names if n}
    except Exception:
        return set()


def _xmind_names() -> set[str]:
    """XMind 大宗材料分类树（建模用，属模式 B 的 dev 输入）的中类名称。"""
    import openpyxl
    names: set[str] = set()
    p = DATA_ROOT / "modeB" / "dev" / "input" / "大宗材料类分类树-建模用.xlsx"
    if not p.exists():
        return names
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    if "分类树" in wb.sheetnames:
        for r in wb["分类树"].iter_rows(values_only=True):
            if r and r[0] == "中类" and len(r) > 2 and r[2]:
                names.add(str(r[2]).strip())
    wb.close()
    return names


def _legacy_load_d0_reference_names() -> set[str]:
    """（废弃）旧的一锅烩实现，仅保留作对照，不再被调用。"""
    names: set[str] = set()

    def sheet_names(path: str, sheet: str, col: str) -> set[str]:
        import openpyxl
        out: set[str] = set()
        if not Path(path).exists():
            return out
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if sheet not in wb.sheetnames:
            wb.close()
            return out
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            wb.close()
            return out
        hdr = [str(c).strip() if c is not None else "" for c in rows[0]]
        if col not in hdr:
            wb.close()
            return out
        ci = hdr.index(col)
        for r in rows[1:]:
            if r[ci] is not None and str(r[ci]).strip():
                out.add(str(r[ci]).strip())
        wb.close()
        return out

    # 2026-10-22 目录重排：参考名从数据集注册表取（group1 对象类表）
    try:
        from tools import dataset_registry as registry
        t = registry.load_gold_tables("A", "group1")
        names |= {c["name"] for c in t["classes"] if c.get("name")}
    except Exception:
        pass
    return names


def term_normalize(terms: list[str], syn2can: dict[str, str]) -> list[dict]:
    out = []
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        if t in syn2can:
            out.append({"original": t, "canonical": syn2can[t], "method": "exact", "confidence": 0.95})
            continue
        hit = None
        for s, c in syn2can.items():
            if s and (s in t or t in s):
                hit = c
                break
        if hit:
            out.append({"original": t, "canonical": hit, "method": "substring", "confidence": 0.7})
        else:
            out.append({"original": t, "canonical": t, "method": "none", "confidence": 0.0})
    return out


# ---------------------------------------------------------------------------
# 模式 A · 广度抽取（参考驱动，因扫描 OCR 缺失）
# ---------------------------------------------------------------------------
def _s(v) -> str:
    return str(v).strip() if v is not None else ""


# 扫描件兜底用的领域词（**不绑文件名**）
# 2026-10-24 改造：原来这里是「文件名 → 词表」的硬编码字典（_TOPICAL_SEEDS），
# 上传的 .md 文件名只要不叫那两个 PDF 名，seeds 就是空列表，候选只剩硬编码的
# 3 条广度补充项 —— 所谓"抽取"与文档内容完全无关。现在的路径是：
#   能读到标题/表格/正文 → _extract_structured()（真抽取）
#   只有扫描件（无文本层）→ _seed_fallback()（领域词兜底，明确标注无正文证据）
_DOMAIN_TERMS = {
    "object_class": ["物项", "物项分级", "物项清单", "大宗材料", "管道", "管道等级",
                     "管件", "弯头", "三通", "四通", "法兰", "阀门"],
    "attribute": ["压力等级", "公称直径", "壁厚", "材质", "材料牌号", "物资设计码",
                  "物资中类", "连接形式", "材料标准"],
}

# 正文「术语：定义」句式。只认冒号句式，避免把普通句子当条目。
# 前缀容错：`> ` 引用块、`- ` 列表、`1. ` 编号 —— markdown 里这些都常见。
_DEF_LINE = re.compile(
    r"^\s*(?:>\s*)?(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?([^：:\n]{2,20})(?:\*\*|__)?\s*[：:]\s*(\S.{5,})$")
_SKIP_DEF_PREFIX = ("注", "说明", "备注", "提示", "示例", "例", "图", "表",
                    "注意", "参见", "来源", "参考", "版本", "日期", "范围", "用途")


def _page_text(pg: dict) -> str:
    """取一个节/页的文本。

    **必须读 text_blocks** —— 所有解析器（pdf/docx/xlsx/md）产出的 page 里
    只有 `text_blocks`，从来没有 `text` 字段。旧代码这里读 `p.get("text")`，
    导致文本永远为空，大模型正文抽取分支永远进不去（还被写成"扫描件无文本层"）。
    """
    blocks = pg.get("text_blocks") or []
    return "\n".join(_s(b.get("text")) for b in blocks
                     if isinstance(b, dict) and _s(b.get("text")))


def _doc_text(d: dict) -> str:
    return "\n".join(_page_text(p) for p in (d.get("pages") or []))


def _split_title(name: str) -> list[str]:
    """文件名 → 主题词（仅兜底用）。只分词，不做语义猜测。"""
    stem = Path(name or "").stem
    parts = re.split(r"[-_—、\s（）()【】\[\]]+", stem)
    return [p for p in parts if 2 <= len(p) <= 20 and not p.isdigit()]


def _extract_structured(d: dict) -> dict:
    """从结构化解析结果（标题 + 表格 + 正文）抽候选 —— A 模式的**主路径**。

    三类证据，逐条带出处：
      heading    标题层级 → 对象类；parent_hint = 最近的上级标题（P3 建 subClassOf 用）
      table_row  类表行 → 对象类（含父类列）；属性表行 → 对象类属性（owner = 当前节标题）
      text_line  正文「术语：定义」句式 → 低置信候选

    不做无证据的猜测：抽不到就返回空，由调用方如实报告。
    """
    fname = _s(d.get("file"))
    stem = Path(fname).stem
    cands: list[dict] = []
    seen: dict[tuple, dict] = {}
    stats = {"headings": 0, "class_rows": 0, "attr_rows": 0, "def_lines": 0,
             "tables": 0, "skipped_tables": 0}

    def add(name, ctype, *, owner="", **kw):
        name = _s(name)
        if not name or len(name) > 40 or name.startswith("（无标题段"):
            return None
        key = (ctype, owner if ctype == "attribute" else "", name)
        cur = seen.get(key)
        if cur is not None:
            # 同一实体多条证据：保留最强的一条（先来的），缺失字段补齐
            for f in ("parent_hint", "definition", "owner"):
                if not cur.get(f) and kw.get(f):
                    cur[f] = kw[f]
            for f in ("data_type", "constraint", "unit", "code"):
                if not cur.get(f) and kw.get(f):
                    cur[f] = kw[f]
            return cur
        c = {
            "id": f"a-{len(cands) + 1:03d}",
            "name": name, "type": ctype, "owner": owner,
            "definition": _s(kw.get("definition")),
            "data_type": _s(kw.get("data_type")),
            "constraint": _s(kw.get("constraint")),
            "unit": _s(kw.get("unit")),
            "code": _s(kw.get("code")),
            "parent_hint": _s(kw.get("parent_hint")),
            "_evidence_kind": kw.get("_evidence_kind", "none"),
            "_section_level": kw.get("_section_level") or 0,
            "confidence": 0.0,             # 由 confidence.calibrate 填，这里不写死
            "evidence_span": kw.get("evidence_span") or {},
        }
        cands.append(c)
        seen[key] = c
        return c

    stack: list[tuple[int, str]] = []      # [(标题层级, 类名)] —— 靠 section_level 建真实的树
    for pg in d.get("pages") or []:
        sec = _s(pg.get("section"))
        try:
            lvl = int(pg.get("section_level") or 0)
        except Exception:
            lvl = 0
        placeholder = (not sec) or sec.startswith("（无标题段")
        owner = "" if placeholder else sec

        if not placeholder and lvl > 0:
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            add(sec, "object_class", parent_hint=parent, _section_level=lvl,
                _evidence_kind="heading",
                evidence_span={"doc": fname, "section": sec, "section_level": lvl,
                               "kind": "heading", "span": "#" * lvl + " " + sec,
                               "basis": f"文档标题（{lvl} 级）"})
            stack.append((lvl, sec))
            stats["headings"] += 1

        for ti, grid in enumerate(pg.get("tables") or [], 1):
            if not grid or len(grid) < 2:
                continue
            stats["tables"] += 1
            hdr = [_s(x) for x in grid[0]]
            kind = dp.classify_table(hdr)
            cols = dp.table_columns(hdr)

            if kind == "class_table":
                for ri, row in enumerate(grid[1:], 2):
                    nm = dp.cell(row, cols["class_name"])
                    if not nm:
                        continue
                    add(nm, "object_class",
                        definition=dp.cell(row, cols["definition"]),
                        code=dp.cell(row, cols["code"]),
                        parent_hint=(dp.cell(row, cols["parent"])
                                     or dp.cell(row, cols["parent_id"])
                                     or owner),
                        _evidence_kind="table_row",
                        evidence_span={"doc": fname, "section": sec, "kind": "table_row",
                                       "table": ti, "row": ri,
                                       "span": " | ".join(_s(x) for x in row)[:120],
                                       "basis": "类表行"})
                    stats["class_rows"] += 1
            elif kind == "attr_table":
                own = owner or stem
                for ri, row in enumerate(grid[1:], 2):
                    nm = dp.cell(row, cols["attr_name"])
                    if not nm or len(nm) > 40:
                        continue
                    add(nm, "attribute", owner=own,
                        data_type=dp.cell(row, cols["data_type"]),
                        unit=dp.cell(row, cols["unit"]),
                        constraint=dp.cell(row, cols["constraint"]),
                        definition=dp.cell(row, cols["definition"]),
                        code=dp.cell(row, cols["code"]),
                        _evidence_kind="table_row",
                        evidence_span={"doc": fname, "section": sec, "kind": "table_row",
                                       "table": ti, "row": ri,
                                       "span": " | ".join(_s(x) for x in row)[:120],
                                       "basis": f"属性表行（归属 {own}）"})
                    stats["attr_rows"] += 1
            else:
                stats["skipped_tables"] += 1

        # 正文「术语：定义」：最弱的一类证据，交给规则基线压分
        for b in pg.get("text_blocks") or []:
            txt = _s(b.get("text")) if isinstance(b, dict) else ""
            if not txt:
                continue
            for line in txt.splitlines():
                m = _DEF_LINE.match(line.strip())
                if not m:
                    continue
                nm = _s(m.group(1)).strip("*_ ")
                dfn = _s(m.group(2))
                if len(nm) < 2 or nm.startswith(_SKIP_DEF_PREFIX):
                    continue
                if any(ch.isdigit() for ch in nm):
                    continue
                add(nm, "attribute", owner=owner or stem, definition=dfn,
                    _evidence_kind="text_line",
                    evidence_span={"doc": fname, "section": sec, "kind": "text_line",
                                   "span": line.strip()[:120],
                                   "basis": "正文「术语：定义」句式"})
                stats["def_lines"] += 1

    return {"candidates": cands, "stats": stats}


def _seed_fallback(d: dict) -> dict:
    """扫描件兜底：**无文本层**时才走，明说这是降级，不冒充抽取结果。"""
    fname = _s(d.get("file"))
    cands: list[dict] = []
    seen: set[str] = set()

    def put(name: str, ctype: str, why: str):
        if not name or name in seen:
            return
        seen.add(name)
        cands.append({
            "id": f"a-{len(cands) + 1:03d}", "name": name, "type": ctype, "owner": "",
            "definition": "", "data_type": "", "constraint": "", "unit": "", "code": "",
            "parent_hint": "", "_evidence_kind": "seed", "_section_level": 0,
            "confidence": 0.0,
            "evidence_span": {"doc": fname, "section": "", "kind": "seed",
                              "span": "", "basis": why},
        })

    for t in _split_title(fname):
        put(t, "object_class", "文件名主题词（文档无文本层，未从正文抽取）")
    for ctype, terms in _DOMAIN_TERMS.items():
        for t in terms:
            put(t, ctype, "领域常识兜底（文档无文本层，未从正文抽取）")

    return {"candidates": cands,
            "stats": {"headings": 0, "class_rows": 0, "attr_rows": 0,
                      "def_lines": 0, "tables": 0, "skipped_tables": 0},
            "fallback": True}



def _build_mode_a(nollm: bool = False) -> dict:
    """模式 A · 结构化抽取（2026-10-24 重写）。

    路径：
      md / docx / xlsx 等**有文本层或表格**的文档 → `_extract_structured()` 真读标题 + 表格 + 正文；
      扫描件（无文本层、无表格）→ `_seed_fallback()` 领域词兜底，明确标注"未从正文抽取"。

    置信度不再写死：交给 `confidence.calibrate()`（规则基线 + 模型裁决 + 缓存）。
    """
    syn2can, _ = load_domain_vocab()
    ref = load_d0_reference_names()
    docs = dp.parse_mode_a().get("documents", [])
    candidates: list[dict] = []
    per_doc: list[dict] = []
    term_norm_all: list[dict] = []
    structured_docs = 0
    fallback_docs = 0

    for d in docs:
        fname = _s(d.get("file"))
        has_text = len(_doc_text(d).strip()) >= 40
        has_tables = any((pg.get("tables") or []) for pg in (d.get("pages") or []))

        if has_text or has_tables:
            one = _extract_structured(d)
            basis = "structured"
            structured_docs += 1
        else:
            one = _seed_fallback(d)
            basis = "filename_topic_fallback"
            fallback_docs += 1

        doc_cands = one["candidates"]
        for c in doc_cands:
            is_cov = c["name"] in ref
            c["novelty"] = "covered" if is_cov else "novel"
            c["term_norm"] = {
                "original": c["name"],
                "canonical": syn2can.get(c["name"], c["name"]),
                "method": "exact" if c["name"] in syn2can else "none",
                "confidence": 0.95 if c["name"] in syn2can else 0.0,
            }
            if not c.get("definition"):
                c["definition"] = "（参考库命中，文档未另写定义）" if is_cov else ""
        term_norm_all.extend(c["term_norm"] for c in doc_cands)

        st = one["stats"]
        direct = (st.get("headings", 0) + st.get("class_rows", 0)
                  + st.get("attr_rows", 0) + st.get("def_lines", 0))
        per_doc.append({
            "file": fname,
            "fmt": d.get("fmt", ""),
            "parse_quality": d.get("parse_quality", ""),
            "extract_basis": basis,
            "section_count": len(d.get("pages") or []),
            "table_count": st.get("tables", 0),
            "skipped_tables": st.get("skipped_tables", 0),
            "headings": st.get("headings", 0),
            "class_rows": st.get("class_rows", 0),
            "attr_rows": st.get("attr_rows", 0),
            "def_lines": st.get("def_lines", 0),
            "candidate_count": len(doc_cands),
            "direct_extracted": direct,
            "sparse": basis == "filename_topic_fallback",
            "candidates": doc_cands,
        })
        candidates.extend(doc_cands)

    # 关系候选（subClassOf / hasAttribute / associates）：有出处才抽
    rel_res = rx.from_documents(docs)

    if not docs:
        basis_note = "D1 槽位当前没有可用文档。"
    elif fallback_docs and not structured_docs:
        basis_note = (f"{fallback_docs} 份 D1 文档均无文本层/可用表格（扫描件），"
                      f"候选改由「文件名主题 + 领域常识」兜底生成，全部按最低档置信度处理，"
                      f"需人工确认；未做任何正文编造。")
    elif fallback_docs:
        basis_note = (f"{structured_docs} 份文档按结构化内容抽取（标题层级 / 表格 / 正文），"
                      f"另有 {fallback_docs} 份无文本层、仅用领域词兜底；"
                      f"置信度由「证据形态基线 + 大模型裁决」给出，每条带依据与原文出处。")
    else:
        basis_note = (f"{structured_docs} 份 D1 文档按结构化内容抽取：md 标题层级 → 对象类，"
                      f"表格 → 对象类 / 属性，正文「术语：定义」→ 低置信候选；"
                      f"置信度由「证据形态基线 + 大模型裁决」给出，每条带依据与原文出处。")

    res = {
        "mode": "A",
        "basis": "structured_extract" if structured_docs else "filename_topic_fallback",
        "basis_note": basis_note,
        "documents": per_doc,
        "candidates": candidates,
        "term_norm": term_norm_all,
        "relations": rel_res["relations"],
        "relation_summary": rx.summarize(rel_res),
        "extract_stats": {
            "docs": len(docs), "structured": structured_docs, "fallback": fallback_docs,
            "headings": sum(pd["headings"] for pd in per_doc),
            "class_rows": sum(pd["class_rows"] for pd in per_doc),
            "attr_rows": sum(pd["attr_rows"] for pd in per_doc),
            "def_lines": sum(pd["def_lines"] for pd in per_doc),
            "tables": sum(pd["table_count"] for pd in per_doc),
            "skipped_tables": sum(pd["skipped_tables"] for pd in per_doc),
        },
    }

    if nollm:
        # P3 复用候选入图：不触发模型，只用规则基线
        res["confidence"] = cf.calibrate(res["candidates"], llm_allowed=False)
        res["llm"] = {"available": None, "used": False, "reason": "",
                      "note": "本次为 P3 候选入图复用规则抽取结果，未触发大模型增强"}
        return _finalize_a(res)

    res = _llm_enrich_a(res, docs)
    # 模型增强会追加/改名/补定义 → 置信度放在最后统一算，避免与计数错位
    res["confidence"] = cf.calibrate(res["candidates"], llm_allowed=True)
    return _finalize_a(res)


def _finalize_a(res: dict) -> dict:
    """统一重算派生统计。**必须在候选最终确定之后调用**。

    旧代码在 `_llm_enrich_a` 追加候选之前就算 `low_confidence_count` 与
    `candidate_count`，导致大模型抽出来的条目没被计入，界面数字对不上。
    """
    cands = res.get("candidates") or []
    res["low_confidence_count"] = sum(1 for c in cands
                                      if float(c.get("confidence") or 0) < cf.LOW_CONF)
    nov: dict[str, int] = {}
    for c in cands:
        k = c.get("novelty", "unknown")
        nov[k] = nov.get(k, 0) + 1
    res["novelty_dist"] = nov
    res["type_dist"] = _type_dist(cands)
    res["confidence_dist"] = cf.dist(cands)
    res["candidate_count"] = len(cands)
    res["llm_extracted"] = sum(1 for c in cands if c.get("extracted_by") == "llm")

    # 文档级计数按证据里的 doc 归属重算
    by_doc: dict[str, int] = {}
    by_doc_llm: dict[str, int] = {}
    for c in cands:
        fn = _s((c.get("evidence_span") or {}).get("doc"))
        if not fn:
            continue
        by_doc[fn] = by_doc.get(fn, 0) + 1
        if c.get("extracted_by") == "llm":
            by_doc_llm[fn] = by_doc_llm.get(fn, 0) + 1
    for pd in res.get("documents") or []:
        fn = pd.get("file", "")
        if fn in by_doc:
            pd["candidate_count"] = by_doc[fn]
        pd["llm_extracted"] = by_doc_llm.get(fn, 0)

    res["coverage_density"] = [
        {"section": pd["file"], "candidate_count": pd.get("candidate_count", 0),
         "direct_extracted": pd.get("direct_extracted", 0), "sparse": pd.get("sparse", False)}
        for pd in res.get("documents") or []
    ]
    return res



def _type_dist(cands: list[dict]) -> dict:
    d: dict[str, int] = {}
    for c in cands:
        d[c["type"]] = d.get(c["type"], 0) + 1
    return d


def _llm_enrich_a(res: dict, docs: list[dict]) -> dict:
    """P2 的大模型增强：术语归一 + 定义补写 +（有正文时）带证据抽取。

    三项任务**并发**发出（实测串行要 230s，并发约 80–100s）；
    扫描件没有文本层时不硬抽 —— 如实写进 `llm.skipped_extract`。
    无模型可用时原样返回，并在 `llm.reason` 说明，绝不假装跑过。
    """
    import concurrent.futures as futures
    from tools import llm_client as llm
    from tools import llm_tasks as lt

    meta = {"available": False, "used": False, "reason": "", "model": "",
            "term_normalized": 0, "definitions_written": 0, "extracted": 0,
            "skipped_extract": "", "elapsed_ms": 0}
    t0 = time.time()
    try:
        st = llm.status()
        meta["available"] = st["available"]
        meta["reason"] = st.get("reason", "")
        if not st["available"]:
            res["llm"] = meta
            return res

        bulk = (llm.config().get("bulk_model") or "").strip() or None
        cands = res["candidates"]

        # (1) 术语归一：词表已下线，改为按常见写法差异纠偏，限 20 条
        targets = [c for c in cands
                   if (c.get("term_norm") or {}).get("method") in ("none", None)]
        names = [c["name"] for c in targets][:20]

        # (2) 定义补写：原候选只有「（参考库命中）」这类占位，限 12 条
        holes = [c for c in cands
                 if not c.get("definition") or str(c["definition"]).startswith("（")][:12]
        def_items = [{"name": c["name"], "fmt": "", "unit": "", "definition": "",
                      "class_name": ""} for c in holes]

        # (3) 正文抽取：只有真拿到文本才做
        # bugfix 2026-10-24：原实现读 `p.get("text")`，但所有解析器的 page 只有
        # `text_blocks`，导致文本恒为空 —— 即便 D1 换成有正文的 .md，正文抽取也永远
        # 进不来，还会被写成"扫描件无文本层"。改用 _page_text/ _doc_text。
        doc_text = []
        for d in docs:
            txt = _doc_text(d)
            if len(txt.strip()) >= 40:
                doc_text.append((d["file"], txt))
        if not doc_text:
            meta["skipped_extract"] = "无可用正文（扫描件需先 OCR，或文档正文过短）"

        got_term = got_def = None
        got_ext = []
        with futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_term = ex.submit(lt.normalize_terms, names, model=bulk, timeout=180) if names else None
            f_def = (ex.submit(llm.suggest_definitions, def_items, max_items=len(def_items),
                               tag="p2.define", model=bulk, timeout=180) if def_items else None)
            f_ext = [ex.submit(lt.extract_candidates, fn, tx, model=bulk)
                     for fn, tx in doc_text[:2]]
            if f_term:
                try:
                    got_term = f_term.result()
                except Exception as e:
                    meta["reason"] = (meta["reason"] + f"；术语归一失败：{e}").strip("；")
            if f_def:
                try:
                    got_def = f_def.result()
                except Exception as e:
                    meta["reason"] = (meta["reason"] + f"；定义补写失败：{e}").strip("；")
            for f in f_ext:
                try:
                    got_ext.append(f.result())
                except Exception as e:
                    meta["reason"] = (meta["reason"] + f"；正文抽取失败：{e}").strip("；")

        # 应用术语归一
        if got_term:
            by = {g["name"]: g for g in got_term["items"]}
            for c in targets:
                g = by.get(c["name"])
                if not g:
                    continue
                canon = g["canonical"] or c["name"]
                if canon != c["name"] and g["confidence"] >= 0.7:
                    c["term_norm"] = {"original": c["name"], "canonical": canon,
                                      "method": "llm", "confidence": g["confidence"],
                                      "reason": g["reason"]}
                    res["term_norm"].append({"original": c["name"], "canonical": canon,
                                             "method": "llm", "confidence": g["confidence"]})
                    c["name"] = canon
                else:
                    c["term_norm"] = {**(c.get("term_norm") or {}),
                                      "method": "llm_checked",
                                      "confidence": g["confidence"]}
                meta["term_normalized"] += 1
            meta["model"] = got_term["model"] or meta["model"]
            meta["used"] = meta["used"] or bool(got_term["items"])

        # 应用定义补写
        if got_def:
            by = {g["name"]: g for g in got_def["items"]}
            for c in holes:
                g = by.get(c["name"])
                if not g or not g.get("definition"):
                    continue
                c["definition"] = g["definition"]
                c["llm_definition"] = True
                c["evidence_span"]["basis"] = (
                    str(c["evidence_span"].get("basis", ""))
                    + f"；定义由 {got_def['model']} 补写（置信 {g['confidence']:.2f}）")
                if g.get("unit") and not c.get("unit"):
                    c["unit"] = g["unit"]
                meta["definitions_written"] += 1
            meta["model"] = got_def["model"] or meta["model"]
            meta["used"] = True

        # 应用正文抽取
        for (fn, _tx), got in zip(doc_text[:2], got_ext):
            for it in got["items"]:
                res["candidates"].append({
                    "id": f"a-llm-{len(res['candidates']) + 1:03d}",
                    "name": it["name"],
                    "type": "object_class" if it["kind"] == "object_class" else "attribute",
                    "owner": (it.get("class_hint") or "").strip(),
                    "definition": "",
                    "data_type": it.get("data_type_hint", ""),
                    "constraint": "",
                    "unit": it.get("unit", ""),
                    # 模型自评分只作参考留档；最终分由 confidence 统一算（不采信裸概率）
                    "confidence_model_raw": it["confidence"],
                    "confidence": it["confidence"],
                    "_evidence_kind": "text_line",     # 有原文片段，但形态不固定
                    "_section_level": 0,
                    "evidence_span": {"doc": fn, "section": "",
                                      "kind": "text_line",
                                      "basis": f"大模型 {got['model']} 正文抽取",
                                      "span": it["evidence_span"]},
                    "novelty": "unknown",
                    "term_norm": {"original": it["name"], "canonical": it["name"],
                                  "method": "none", "confidence": 0.0},
                    "class_hint": it.get("class_hint", ""),
                    "extracted_by": "llm",
                })
                meta["extracted"] += 1
            meta["used"] = meta["used"] or bool(got["items"])
            meta["model"] = got["model"] or meta["model"]
    except Exception as e:
        meta["reason"] = (meta["reason"] + f"；{e}").strip("；")
        res["llm"] = meta
        return res

    meta["elapsed_ms"] = int((time.time() - t0) * 1000)
    res["low_confidence_count"] = sum(1 for c in res["candidates"] if c["confidence"] < cf.LOW_CONF)
    nov: dict[str, int] = {}
    for c in res["candidates"]:
        nov[c.get("novelty", "unknown")] = nov.get(c.get("novelty", "unknown"), 0) + 1
    res["novelty_dist"] = nov
    res["type_dist"] = _type_dist(res["candidates"])
    if meta["used"]:
        res["basis_note"] += (
            f" 另：大模型 {meta['model']} 完成术语归一 {meta['term_normalized']} 条、"
            f"定义补写 {meta['definitions_written']} 条"
            + (f"、正文抽取 {meta['extracted']} 条" if meta["extracted"] else "")
            + "。")
    res["llm"] = meta
    return res


# ---------------------------------------------------------------------------
# 模式 B · 目标导向抽取（只汇报抽到了什么，不与标准库/模板比对）
# ---------------------------------------------------------------------------
def _build_mode_b(nollm: bool = False) -> dict:
    """需求驱动的目标导向抽取：对象类 + 属性，与 P5 / P7 用同一份合并数据。

    2026-10-23 修正：原实现只读 `sorted(active_files("b_in"))[0]` 一份文件，且只产出
    对象类候选（属性只出现在「D4 字段对齐」里），于是 P2 显示「17 个对象类、0 个属性」，
    与 P5/P7 的「多份合并、按 (类, 属性) 去重」口径分裂。现在统一取
    `requirement_analyst.load_requirement_dataset()` —— 它既是 P5 解析/合成的取数口，
    也与 P7 生成侧共用 `dataset_registry` 的解析与去重，三处规模天然一致。
    """
    from tools import requirement_analyst as ra  # 延迟导入：ra 在模块级 import 本模块

    ds = ra.load_requirement_dataset()
    rows = ds["rows"]
    classes = [c for c in ds["classes"] if c.get("name")]
    files = ds["files"]

    # 2026-10-23 二次收敛：P2 只回答「从需求文件里抽出了什么、有多确定、证据在哪」。
    # 原先这里还会 (1) 拿 D0 标准库判「已覆盖/全新」、(2) 拿 D4 那份 41 字段基线判
    # 「需求是否漏了某字段」——两者都是比对类工作，归属应在 P4（新颖性）/ P5（匹配）。
    # D4 的定义是「数据标准建模规则」，本身不含「必须存在哪些字段」的语义，因此
    # 那份「模板要求但需求未提供」的结论从概念上就不成立，已整体移除。

    # 每类的属性行数（0 属性的类也要出现，不能凭空消失）
    per_class: dict[str, int] = {}
    for r in rows:
        cn = r.get("object_class") or ""
        per_class[cn] = per_class.get(cn, 0) + 1

    syn2can, _ = load_domain_vocab()
    candidates: list[dict] = []
    seq = 0

    # ① 对象类候选
    for c in sorted(classes, key=lambda x: x.get("name", "")):
        name = c["name"]
        seq += 1
        candidates.append({
            "id": f"b-{seq:03d}",
            "name": name,
            "type": "object_class",
            "owner": "",
            "code": c.get("code", ""),
            "definition": c.get("definition") or "（需求文件指定对象类）",
            # 证据形态：需求表「对象类」sheet 的一行。分数由 confidence 模块统一算，
            # 这里**不再写死 0.82** —— 同样的候选必须有区分度。
            "_evidence_kind": "table_row",
            "evidence_span": {"doc": c.get("file", ""), "sheet": "对象类",
                              "kind": "table_row",
                              "span": f"对象类中文名称={name}", "row": None},
            "mentioned_in_requirement": True,
            "attr_count": per_class.get(name, 0),
            "term_norm": {"original": name, "canonical": syn2can.get(name, name),
                          "method": "exact" if name in syn2can else "none",
                          "confidence": 0.95 if name in syn2can else 0.0},
        })

    # ② 属性候选：一行一条（rows 已按 (对象类, 属性) 去重）
    for r in rows:
        nm = r.get("name") or ""
        if not nm:
            continue
        cn = r.get("object_class") or ""
        dt = r.get("data_type_raw") or ""
        cons = (r.get("fmt") or "").strip()
        unit = r.get("unit") or ""
        definition = r.get("definition") or ""
        seq += 1
        candidates.append({
            "id": f"b-{seq:03d}",
            "name": nm,
            "type": "attribute",
            "owner": cn,
            "definition": definition or "（需求文件未填写属性定义，待业务补写）",
            "data_type": dt, "constraint": cons, "unit": unit,
            # 证据形态：需求表「对象类属性」sheet 的一行。规格字段填了几项，
            # 由 confidence 的规则基线自动体现（不再 "0.88/0.62" 二值切分）。
            "_evidence_kind": "table_row",
            "evidence_span": {"doc": r.get("file", ""), "sheet": "对象类属性",
                              "kind": "table_row",
                              "span": f"{cn}·{nm}", "row": r.get("row")},
            "mentioned_in_requirement": True,
            "term_norm": {"original": nm, "canonical": syn2can.get(nm, nm),
                          "method": "exact" if nm in syn2can else "none",
                          "confidence": 0.95 if nm in syn2can else 0.0},
        })

    # 术语归一：只对属性名做同义词归一，不掺任何标准库/模板比对
    attr_names: list[str] = []
    seen_attr: set[str] = set()
    for r in rows:
        nm = r.get("name") or ""
        if nm and nm not in seen_attr:
            seen_attr.add(nm)
            attr_names.append(nm)
    term_norm = term_normalize(sorted(attr_names), syn2can)

    basis_note = (f"需求文件可解析（D2，合并 {len(files)} 份、按「对象类+属性」去重后 "
                  f"{len(classes)} 类 / {len(rows)} 属性）：逐个抽取对象类与属性候选，"
                  f"属性缺失项标低置信交人工门控；本环节不与标准库或模板做覆盖比对"
                  f"（新颖性见 P4，已有标准匹配见 P5）")

    # 关系候选：父类ID → subClassOf；属性归属 → hasAttribute；均带出处
    rel_res = rx.from_requirement(ds)

    # 置信度：规则基线 + 模型裁决（模糊区间）。必须在候选全部生成之后算。
    conf_meta = cf.calibrate(candidates, llm_allowed=not nollm)

    return {
        "mode": "B",
        "document": {"file": files[0] if files else "", "files": files,
                     "file_count": len(files), "fmt": "xlsx",
                     "class_count": len(classes), "attr_row_count": len(rows)},
        "basis": "requirement_driven",
        "basis_note": basis_note,
        "candidates": candidates,
        "term_norm": term_norm,
        "low_confidence_count": sum(1 for c in candidates
                                    if float(c.get("confidence") or 0) < cf.LOW_CONF),
        "type_dist": _type_dist(candidates),
        "confidence": conf_meta,
        "confidence_dist": cf.dist(candidates),
        "relations": rel_res["relations"],
        "relation_summary": rx.summarize(rel_res),
    }


# ---------------------------------------------------------------------------
# 统一入口（带缓存）
# ---------------------------------------------------------------------------
def extract_mode_a(force: bool = False, llm: bool = True) -> dict:
    """模式 A 抽取。`llm=False` 用于 P3 复用候选入图：不触发大模型调用。

    背景：_llm_enrich_a 是真实网络调用（并发 3 个任务，实测 46s），
    属于 P2 环节的能力。P3 只是把已有候选挂到图谱上，不该再跑一遍 LLM。
    """
    ck = "A" if llm else "A:nollm"
    if not force and ck in _CACHE:
        return _CACHE[ck]
    if llm:
        res = _build_mode_a()
    else:
        # 规则抽取结果：跳过 LLM 增强，但保留同一套 basis/字段结构
        res = _build_mode_a(nollm=True)
    _CACHE[ck] = res
    return res


def extract_mode_b(force: bool = False, llm: bool = True) -> dict:
    ck = "B" if llm else "B:nollm"
    if not force and ck in _CACHE:
        return _CACHE[ck]
    res = _build_mode_b(nollm=not llm)
    _CACHE[ck] = res
    return res


def extract(mode: str, force: bool = False, llm: bool = True) -> dict:
    return (extract_mode_a(force, llm) if mode == "A"
            else extract_mode_b(force, llm))


if __name__ == "__main__":
    import pprint
    ra = extract_mode_a()
    print("MODE A basis:", ra["basis"], "| candidates:", len(ra["candidates"]),
          "| low<0.5:", ra["low_confidence_count"])
    print("type_dist:", ra["type_dist"], "| novelty_dist:", ra["novelty_dist"])
    print("coverage_density:", ra["coverage_density"])
    print("term_norm sample:", ra["term_norm"][:3])
    rb = extract_mode_b()
    print("\nMODE B classes:", rb["document"]["class_count"], "| candidates:", len(rb["candidates"]),
          "| type_dist:", rb["type_dist"], "| low<0.5:", rb["low_confidence_count"])
