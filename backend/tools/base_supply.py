"""
base_supply.py
==============
P3 知识底座的数据供给层 —— **只由「非答案」数据供给**。

背景（2026-10-23 修正）
- 原实现 `mode_isolation.dev_output_paths()` 明写「只看 D3 槽位的显式决策」，
  即 P3 图谱底座 = D3 预期输出（评测答案）；而 P7 又拿 P6 生成结果去跟 D3 比，
  于是「底座里本来就装着答案」→ 准确率必然虚高，属循环论证。
- 现在底座改由两类**输入型**数据供给：
    ① D0 已有标准库（存量标准，人工审核入库，`data/library/`）
    ② 本轮输入：A 模式 = D1 文档解析结果（含 .md 标题层级与表格）；
                 B 模式 = D2 业务需求表（对象类 + 对象类属性）
- 两者都取不到 → 抛 `DataUnavailable`，**绝不回落 D3**。宁可拒绝执行，不造假。

对外形状与 `mode_isolation.load_mode_model()` 保持同构（classes / attr_instances /
relations / counts / data_caveat），另外多一个 `supply` 明细块，供面板如实展示
「这一版底座到底由哪些数据拼出来的」。
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import confidence as cf  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

UNASSIGNED = "（未归属）"

# md 文档里「属性表」的表头特征：必须有名称列，且带类型/单位/约束之一
_ATTR_NAME_COLS = ("属性中文名称", "属性名称", "字段名称", "字段中文名", "中文名称", "名称", "数据项名称")
_ATTR_SPEC_COLS = ("数据类型", "数据格式/约束", "约束", "属性单位", "单位", "值域", "量纲")
_CLASS_NAME_COLS = ("对象类名称", "对象类中文名称", "对象类", "类别名称", "中文名称", "名称")
_CLASS_SPEC_COLS = ("中文定义", "定义", "说明", "父类", "所属专业", "对象类ID")


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _find(hdr: list[str], names: tuple[str, ...]) -> int:
    for n in names:
        if n in hdr:
            return hdr.index(n)
    return -1


def _mk_class(name: str, *, code="", definition="", domain="", parent="",
              status="", source="", origin="") -> dict:
    return {"id": "", "code": code, "name": name, "definition": definition,
            "parent_id": parent, "domain": domain, "status": status,
            "source": source, "origin": origin}


# ---------------------------------------------------------------------------
# ① D0 已有标准库
# ---------------------------------------------------------------------------
def from_d0() -> dict:
    """从 D0 存量标准库取类/属性。

    D0 的 classes 是「名称 → {code, domain, ...}」，field_index 是「属性名 → [行]」。
    注意：D0 里很多属性 `class_name` 为「（未归属）」（上传表未给归属列），
    这类属性**保留为属性节点但不建 hasAttribute 边**，并在 caveat 里如实计数。
    """
    try:
        from tools import landscape_analyst as la
        lib = la.load_mock_library("A")     # D0 跨模式共用一份
    except Exception:
        lib = None
    if not lib or not lib.get("class_count"):
        return {"classes": [], "attrs": [], "relations": [], "note": "D0 未提供",
                "counts": {"classes": 0, "attrs": 0, "relation": 0}}

    src = lib.get("source_short") or lib.get("source") or "D0 已有标准库"
    classes: list[dict] = []
    for name, c in (lib.get("classes") or {}).items():
        if not name:
            continue
        classes.append(_mk_class(
            name, code=_s(c.get("code")), definition=_s(c.get("definition")),
            domain=_s(c.get("domain")), parent=_s(c.get("parent_id") or c.get("parent")),
            status=_s(c.get("status")), source=src, origin="d0"))
    class_names = {c["name"] for c in classes}

    attrs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for nm, rows in (lib.get("field_index") or {}).items():
        if not nm:
            continue
        for r in rows:
            cn = _s(r.get("class_name"))
            if cn == UNASSIGNED:
                cn = ""
            if cn and cn not in class_names:
                # D0 属性挂的类不在 D0 类表里：补一个类节点，避免属性凭空消失
                classes.append(_mk_class(cn, source=src, origin="d0"))
                class_names.add(cn)
            key = (cn, nm)
            if key in seen:
                continue
            seen.add(key)
            attrs.append({
                "id": "", "class_id": "", "class_name": cn, "attr_id": _s(r.get("std_id") or r.get("attr_id")),
                "code": _s(r.get("code")), "name": nm, "unit": _s(r.get("unit")),
                "definition": _s(r.get("definition")), "data_type": _s(r.get("data_type")),
                "constraint": _s(r.get("constraint")), "source": src, "origin": "d0",
            })
    return {"classes": classes, "attrs": attrs, "relations": [],
            "counts": {"classes": len(classes), "attrs": len(attrs),
                       "unassigned": sum(1 for a in attrs if not a["class_name"])},
            "note": f"D0 已有标准库（{lib.get('version') and 'v' + str(lib.get('version')) or 'v?'}）",
            "source": src}


# ---------------------------------------------------------------------------
# ② 本轮输入 · 模式 B（D2 需求表）
# ---------------------------------------------------------------------------
def from_d2() -> dict:
    """从 D2 需求表取类/属性（与 P5/P7 共用 load_requirement_dataset 取数口）。"""
    try:
        from tools import requirement_analyst as ra
        ds = ra.load_requirement_dataset()
    except Exception as e:
        return {"classes": [], "attrs": [], "relations": [], "files": [],
                "counts": {"classes": 0, "attrs": 0}, "note": f"D2 不可用：{e}"}

    files = ds.get("files") or []
    classes = [{"id": "", "code": c.get("code", ""), "name": c["name"],
                "definition": c.get("definition", ""), "parent_id": c.get("parent_id", ""),
                "domain": "", "status": "", "source": "、".join(Path(f).name for f in files),
                "origin": "input"}
               for c in ds.get("classes") or [] if c.get("name")]
    attrs = []
    for r in ds.get("rows") or []:
        nm = r.get("name") or ""
        if not nm:
            continue
        attrs.append({
            "id": "", "class_id": "", "class_name": r.get("object_class") or "",
            "attr_id": r.get("attr_id") or "", "code": r.get("code") or "", "name": nm,
            "unit": r.get("unit") or "", "definition": r.get("definition") or "",
            "data_type": r.get("data_type_raw") or "", "constraint": (r.get("fmt") or "").strip(),
            "source": "、".join(Path(f).name for f in files), "origin": "input",
        })
    return {"classes": classes, "attrs": attrs, "relations": [], "files": files,
            "counts": {"classes": len(classes), "attrs": len(attrs)},
            "note": f"D2 需求表（{len(files)} 份）"}


# ---------------------------------------------------------------------------
# ③ 本轮输入 · 模式 A（D1 文档：标题层级 + 表格）
# ---------------------------------------------------------------------------
def _looks_like_attr_table(hdr: list[str]) -> bool:
    if _find(hdr, _ATTR_NAME_COLS) < 0:
        return False
    return _find(hdr, _ATTR_SPEC_COLS) >= 0


def _looks_like_class_table(hdr: list[str]) -> bool:
    return _find(hdr, _CLASS_NAME_COLS) >= 0 and _find(hdr, _CLASS_SPEC_COLS) >= 0


def from_d1() -> dict:
    """从 D1 文档取类/属性 —— **消费 P2 的抽取结果**，P3 不再自己解析文档。

    2026-10-24 改造。原来这里自己遍历 `doc_parser` 的 pages，维护一套自己的表头特征
    和一段错误的层级逻辑（`lvl` 写死 1、每节用 `stack = [(lvl, sec)]` 覆盖整个栈，
    等价于把连续标题串成一条链），与 P2 的抽取各说各话 —— 同一次上传，P2 说是属性表、
    P3 说是类表的情况迟早会发生。

    现在单一链路「P2 抽取 → P3 建模」：
      · 对象类 = P2 的 object_class 候选，父类取 `parent_hint`（标题层级 / 父类列）
      · 对象类属性 = P2 的 attribute 候选，归属取 `owner`
      · 置信度原样带过来 —— **P3 不重算**（用户明确要求）
    """
    empty = {"classes": [], "attrs": [], "relations": [], "files": [],
             "counts": {"classes": 0, "attrs": 0}}
    try:
        from tools import knowledge_extractor as ke
        res = ke.extract("A", llm=False)          # 复用 P2 缓存，绝不触发模型
    except Exception as e:  # noqa: BLE001
        return {**empty, "note": f"D1 不可用：{e}"}

    cands = res.get("candidates") or []
    # 旧进程内缓存是改造前抽的（没有 _evidence_kind / parent_hint），强制重抽一次
    if cands and not any("_evidence_kind" in c for c in cands):
        try:
            res = ke.extract("A", force=True, llm=False)
            cands = res.get("candidates") or []
        except Exception:  # noqa: BLE001
            pass
    if not cands:
        return {**empty, "note": "D1 文档未抽到任何对象类/属性"}

    classes: list[dict] = []
    attrs: list[dict] = []
    for c in cands:
        ev = c.get("evidence_span") or {}
        ctype = c.get("type")
        if ctype == "object_class":
            classes.append({
                "id": "", "code": _s(c.get("code")), "name": _s(c.get("name")),
                "definition": _s(c.get("definition")),
                # 父类先存名称，_materialize 再转成 ID
                "parent_id": _s(c.get("parent_hint")),
                "domain": "", "status": "",
                "source": _s(ev.get("doc")), "origin": "input",
                "confidence": c.get("confidence"),
                "confidence_basis": _s(c.get("confidence_basis")),
                "evidence": ev,
                "parent_source": "heading" if _s(ev.get("kind")) == "heading" else "table",
            })
        elif ctype == "attribute":
            attrs.append({
                "id": "", "class_id": "", "class_name": _s(c.get("owner")),
                "attr_id": _s(c.get("code")), "code": _s(c.get("code")),
                "name": _s(c.get("name")),
                "unit": _s(c.get("unit")), "definition": _s(c.get("definition")),
                "data_type": _s(c.get("data_type")), "constraint": _s(c.get("constraint")),
                "source": _s(ev.get("doc")), "origin": "input",
                "confidence": c.get("confidence"),
                "confidence_basis": _s(c.get("confidence_basis")),
                "evidence": ev,
            })

    n_parent = sum(1 for c in classes if c["parent_id"])
    by_heading = sum(1 for c in classes if c["parent_id"] and c["parent_source"] == "heading")
    files = [d.get("file", "") for d in (res.get("documents") or [])]
    return {"classes": classes, "attrs": attrs, "relations": [], "files": files,
            "counts": {"classes": len(classes), "attrs": len(attrs),
                       "with_parent": n_parent, "parent_by_heading": by_heading},
            "note": (f"D1 文档（P2 抽取：{len(classes)} 类 / {len(attrs)} 属性，"
                     f"{n_parent} 个类有父类，其中 {by_heading} 个来自标题层级）"),
            "parse_quality": Counter(d.get("parse_quality", "") for d in res.get("documents") or []),
            "extract_basis": res.get("basis", ""),
            "confidence": res.get("confidence")}


# ---------------------------------------------------------------------------
# ④ 合并
# ---------------------------------------------------------------------------
def _mark_unevaluated(rows: list[dict]) -> int:
    """把没有数值置信度的行统一标成「未评估」，返回条数。

    为什么不填 0 或 1：
      · 填 0 → 会被当成"低质量"，满屏标红，且 P8 门控被灌满；
      · 填 1 → 等于给没有证据的东西凭空背书。
    真实语义就是"没评过"（D2 需求表本身没有置信度列、兜底补建的类没有证据可评），
    所以 `confidence` 保持 None，另用 `confidence_source="unevaluated"` 让下游能识别；
    P3 统计把它单独归「未评估」桶，既不进高置信也不进低置信。
    """
    n = 0
    for r in rows:
        if cf.is_evaluated(r.get("confidence")):
            r["confidence"] = cf.as_float(r["confidence"])
            if not r.get("confidence_source"):
                r["confidence_source"] = "rule"
        else:
            r["confidence"] = None
            r["confidence_source"] = cf.UNKNOWN_SOURCE
            if not _s(r.get("confidence_basis")):
                r["confidence_basis"] = "无置信度来源（需求表未给 / 兜底补建），未做证据评估"
            n += 1
    return n


def _materialize(classes: list[dict], attrs: list[dict]
                 ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """补齐 ID、建关系、归并属性池。

    三层结构（2026-10-24 补第三层）：
      对象类 object_class  按名称去重
      对象类属性 field     按 (类名, 属性名) 去重 —— 一条一行，**不做跨类合并**：
                           同一属性出现在不同对象类下就是不同的对象类属性记录
      属性池 attribute     按 `属性ID`（缺则 名称+数据类型）归并 —— 同一属性只存一份，
                           **不记录归属**：属性是公共池，多个类各自取用

    关系：
      subClassOf    子类 → 父类（父子层级）
      hasAttribute  对象类 → 对象类属性（归属）
      refersTo      对象类属性 → 属性池（取用）
    """
    out_c: list[dict] = []
    seen_c: set[str] = set()
    used_ids: set[str] = set()
    for i, c in enumerate(classes, 1):
        nm = _s(c.get("name"))
        if not nm or nm in seen_c:
            continue
        seen_c.add(nm)
        c = dict(c)
        cid = _s(c.get("code")) or f"OC-{i:04d}"
        if cid in used_ids:                  # 编码重名：加后缀，避免图节点 ID 冲突
            cid = f"{cid}--{i:04d}"
        used_ids.add(cid)
        c["id"] = cid
        out_c.append(c)

    name2id = {c["name"]: c["id"] for c in out_c}

    # 父类：允许按名称指定；指不到的留空（不猜）
    for c in out_c:
        p = _s(c.get("parent_id"))
        if p and p in name2id and p != c["name"]:
            c["parent_id"] = name2id[p]
        elif p not in name2id:
            c["parent_id"] = ""

    out_a: list[dict] = []
    seen_a: set[tuple[str, str]] = set()
    for i, a in enumerate(attrs, 1):
        nm = _s(a.get("name"))
        if not nm:
            continue
        cn = _s(a.get("class_name"))
        if cn and cn not in name2id:
            # 属性挂的类没登记：补类节点（宁可多一个类，不让属性凭空消失）
            tmp = {"id": f"OC-X{i:04d}", "code": "", "name": cn, "definition": "",
                   "parent_id": "", "domain": "", "status": "",
                   "source": a.get("source", ""), "origin": a.get("origin", ""),
                   # 兜底补建：只因属性挂靠才补出来的类，没有独立证据可评，不硬给分数
                   "confidence": None, "confidence_source": cf.UNKNOWN_SOURCE,
                   "confidence_basis": "属性挂靠的类未在标准库/本轮输入中登记，兜底补建，无独立证据"}
            out_c.append(tmp)
            name2id[cn] = tmp["id"]
        key = (cn, nm)
        if key in seen_a:
            continue
        seen_a.add(key)
        a = dict(a)
        cid = name2id.get(cn, "")
        aid = _s(a.get("attr_id")) or f"AT-{i:04d}"
        a["class_id"] = cid
        a["attr_id"] = aid
        a["id"] = f"{cid}::{aid}" if cid else aid
        out_a.append(a)

    # ③ 属性池：同一属性被 N 个类取用，池里只留一份
    pool: dict[str, dict] = {}
    field2pool: dict[str, str] = {}
    for a in out_a:
        pk = _s(a.get("attr_id")) or f"{a['name']}::{_s(a.get('data_type'))}"
        p = pool.get(pk)
        if p is None:
            p = {"id": f"AP-{len(pool) + 1:04d}", "key": pk,
                 "name": a["name"], "data_type": _s(a.get("data_type")),
                 "unit": _s(a.get("unit")), "definition": _s(a.get("definition")),
                 "constraint": _s(a.get("constraint")),
                 "origin": _s(a.get("origin")),
                 "ref_count": 0, "class_names": [],
                 "evidence": a.get("evidence") or {},
                 "confidence_basis": _s(a.get("confidence_basis")),
                 "confidence": a.get("confidence")}
            pool[pk] = p
        p["ref_count"] += 1
        cn = _s(a.get("class_name"))
        if cn and cn not in p["class_names"]:
            p["class_names"].append(cn)
        v = a.get("confidence")
        if isinstance(v, (int, float)):
            cur = p.get("confidence")
            p["confidence"] = round(float(v), 3) if cur is None else max(round(float(cur), 3), round(float(v), 3))
        field2pool[a["id"]] = p["id"]

    class_ids = {c["id"] for c in out_c}
    relations: list[dict] = []
    for c in out_c:
        if c.get("parent_id") and c["parent_id"] in class_ids:
            relations.append({"type": "subClassOf", "source": c["id"], "target": c["parent_id"]})
    for a in out_a:
        if a["class_id"] in class_ids:
            relations.append({"type": "hasAttribute", "source": a["class_id"], "target": a["id"]})
        pid = field2pool.get(a["id"])
        if pid:
            relations.append({"type": "refersTo", "source": a["id"], "target": pid})

    # 置信度规范化：数值化的统一成 float，没有的标「未评估」（同一处收口，避免各分支各写一套）
    _mark_unevaluated(out_c)
    _mark_unevaluated(out_a)
    pool_rows = list(pool.values())
    _mark_unevaluated(pool_rows)

    return out_c, out_a, pool_rows, relations


def load(mode: str = "A") -> dict:
    """P3 底座供给唯一入口。"""
    from tools import upload_manager as um

    d0 = from_d0()
    inp = from_d2() if mode == "B" else from_d1()

    if not d0["classes"] and not inp["classes"]:
        raise um.DataUnavailable(
            "a_in_g1" if mode == "A" else "b_in",
            f"模式 {mode} 的知识底座需 D0 已有标准库或本轮输入（A=D1 文档 / B=D2 需求表），"
            f"当前两者都没有可用数据；底座不再取 D3 预期输出")

    classes = list(d0["classes"]) + list(inp["classes"])
    attrs = list(d0["attrs"]) + list(inp["attrs"])
    out_c, out_a, attr_pool, relations = _materialize(classes, attrs)

    class_ids = {c["id"] for c in out_c}
    attached = sum(1 for a in out_a if a["class_id"] in class_ids)
    caveat = []
    un_attr = len(out_a) - attached
    if un_attr:
        caveat.append(f"{un_attr}/{len(out_a)} 条对象类属性无归属对象类，未建 hasAttribute 边")

    has_parent = sum(1 for c in out_c if c.get("parent_id"))
    no_parent = len(out_c) - has_parent
    if no_parent:
        # 只描述"供给数据里没有层级"，不说成"图上就没有 subClassOf" ——
        # AI 类间关系入图后骨架同样会有 subClassOf，旧措辞会自相矛盾。
        caveat.append(f"{no_parent}/{len(out_c)} 个对象类无父类（D0 缺父类列、本轮输入也未给）："
                      f"骨架本身不含 subClassOf，层级可由 P3「类间关系」大模型分析补齐")

    # 属性池复用度：一个属性被多少类取用
    multi = [p for p in attr_pool if p["ref_count"] > 1]
    if multi:
        top = max(multi, key=lambda p: p["ref_count"])
        caveat.append(f"属性池 {len(attr_pool)} 条，其中 {len(multi)} 条被多个对象类复用"
                      f"（最多「{top['name']}」被 {top['ref_count']} 个类取用）")

    sources = []
    if d0["classes"] or d0["attrs"]:
        sources.append(d0["note"])
    if inp["classes"] or inp["attrs"]:
        sources.append(inp["note"])
    origin_dist = Counter(f"{x.get('origin', '')}" for x in out_c)
    conf_vals = [float(c["confidence"]) for c in out_c
                 if isinstance(c.get("confidence"), (int, float))]

    return {
        "mode": mode,
        "source": " + ".join(sources) or "（无）",
        "source_human": f"K3 知识底座供给（模式 {mode}）：" + " + ".join(sources),
        "classes": out_c,
        "attr_instances": out_a,
        "attr_pool": attr_pool,
        "relations": relations,
        "counts": {"object_class": len(out_c), "attribute": len(out_a),
                   "attribute_pool": len(attr_pool),
                   "attribute_attached": attached,
                   "attribute_reused": len(multi),
                   "subClassOf": sum(1 for r in relations if r["type"] == "subClassOf"),
                   "relation": len(relations)},
        "data_caveat": "；".join(caveat) if caveat else "数据完整",
        "confidence_summary": {
            "count": len(conf_vals),
            "min": round(min(conf_vals), 3) if conf_vals else None,
            "max": round(max(conf_vals), 3) if conf_vals else None,
            "avg": round(sum(conf_vals) / len(conf_vals), 3) if conf_vals else None,
        },
        "supply": {
            "d0": {**d0["counts"], "note": d0["note"]},
            "input": {**inp["counts"], "note": inp["note"],
                      "files": [Path(f).name for f in inp.get("files") or []]},
            "origin_dist": dict(origin_dist),
            "policy": "底座 = D0 已有标准库 + 本轮输入；不含 D3 预期输出（评测答案）",
        },
    }


if __name__ == "__main__":
    for m in ("A", "B"):
        try:
            d = load(m)
            print(f"===== 模式 {m} =====")
            print("  来源:", d["source"])
            print("  规模:", d["counts"])
            print("  供给:", d["supply"]["d0"], "|", d["supply"]["input"])
            print("  残缺:", d["data_caveat"])
        except Exception as e:
            print(f"===== 模式 {m} ===== 不可用：{e}")
