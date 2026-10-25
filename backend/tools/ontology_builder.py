"""
ontology_builder.py
====================
核电数据标准智能治理智能体（双模式）· 本体/图谱构建工具（P3 双知识底座）。

绑定工具（见 .dsh/roles/ontology-builder.md）：json_to_owl / graph_upsert / graph_query。

**2026-10-23 重大修正（解耦循环论证）**
- 此前底座经 `mode_isolation.dev_output_paths()` 取数，而那个函数明写「只看 D3 槽位的显式
  决策」—— 也就是说 P3 图谱底座 = D3 预期输出（评测答案）。P7 又拿 P6 生成结果去跟 D3 比，
  底座里本就装着答案 → 准确率虚高，属循环论证。
- 现在底座改由 `base_supply.load(mode)` 供给，只有两类**输入型**数据：
    · D0 已有标准库（存量标准，人工审核入库）
    · 本轮输入：A 模式 = D1 文档解析（md 标题层级 + 表格）；B 模式 = D2 需求表
  两者都取不到就抛 DataUnavailable，**绝不回落 D3**。
- 保留的硬约束：
    · A / B 图谱物理分离，各自落 `data/knowledge_graph_{mode}.json`，不共用节点；
    · 写入前过 `mode_isolation.assert_allowed()` 跨模式防泄漏校验；
    · 硬指标仍是「对象类 / 属性 / 关系」三维度，阈值见 `mode_isolation.METRIC_THRESHOLDS`。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import mode_isolation as mi  # noqa: E402
from tools import knowledge_extractor as ke  # noqa: E402
from tools import base_supply as bs  # noqa: E402
from tools import confidence as cf  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
DELIVERABLES = Path(__file__).resolve().parent.parent.parent / "deliverables"

NS = "http://example.org/nuclear-standard#"
XSD_MAP = {
    "string": "xsd:string", "str": "xsd:string", "varchar": "xsd:string", "text": "xsd:string",
    "float": "xsd:double", "double": "xsd:double", "decimal": "xsd:decimal", "number": "xsd:double",
    "int": "xsd:integer", "integer": "xsd:integer", "long": "xsd:integer",
    "boolean": "xsd:boolean", "bool": "xsd:boolean",
    "date": "xsd:date", "datetime": "xsd:dateTime", "time": "xsd:time",
}
ILLEGAL_IRI_CHARS = re.compile(r"[\s<>\"{}|\\^`]")

FIELD_REQUIRED = ["name", "data_type"]
CLASS_REQUIRED = ["name"]

_GRAPH_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 存储（按模式分离）
# ---------------------------------------------------------------------------
def store_path(mode: str = "A") -> Path:
    return DATA_ROOT / f"knowledge_graph_{mode}.json"


def owl_path(mode: str = "A") -> Path:
    return DELIVERABLES / "shared" / f"ontology-{mode}.ttl"


def _assert_no_test_source(payload: dict, mode: str = "A") -> None:
    """写入前硬校验：来源路径不得命中该模式的禁用项（跨模式泄漏）。"""
    for n in payload.get("nodes", []):
        mi.assert_allowed(mode, n.get("source", ""))
    for e in payload.get("edges", []):
        mi.assert_allowed(mode, e.get("source_ref", "") or "")


# ---------------------------------------------------------------------------
# 1) 建模集 → 图谱节点/边（唯一入口：mode_isolation）
# ---------------------------------------------------------------------------
def load_dev_model(mode: str = "A") -> dict:
    """（兼容旧签名）返回该模式的底座供给。实际由 base_supply 供数。

    2026-10-23：不再走 mode_isolation.load_mode_model（那是 D3 预期输出）。
    """
    return bs.load(mode)


def _iri_safe(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\s\u3000]+", "_", s)
    s = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", s)
    return s or "UNNAMED"


def _xsd_of(dt: str) -> str:
    return XSD_MAP.get((dt or "").strip().lower(), "xsd:string")


def _completeness(kind: str, data: dict) -> dict:
    missing, warnings = [], []
    for f in (FIELD_REQUIRED if kind == "field" else CLASS_REQUIRED):
        if not str(data.get(f, "") or "").strip():
            missing.append(f)
    if not str(data.get("definition", "") or "").strip():
        warnings.append("definition 为空")
    if kind == "field" and not str(data.get("unit", "") or "").strip():
        warnings.append("无量纲（unit 为空）")
    return {"status": "COMPLETE" if not missing else "INCOMPLETE",
            "missing": missing, "warnings": warnings}


def build_base_graph(mode: str = "A") -> dict:
    """从底座供给构建图谱。

    节点三层：
      object_class 对象类
      field        对象类属性（一条一行，不跨类合并）
      attribute    属性池（同一属性只一个节点，被多个类共用）

    边三类：
      subClassOf    子类 → 父类（父子层级：md 标题层级 / 类表父类列）
      hasAttribute  对象类 → 对象类属性（归属）
      refersTo      对象类属性 → 属性池（取用）

    2026-10-24 调整（用户要求）：
    · 去掉业务域 domain 分组节点 —— 不要业务域，它只是凭空多一层无关分组；
    · 补属性池节点 —— 「属性是公共池，任何对象类需要都可以取来用」在图上要能看出来；
    · 置信度：D0 已确认的类/属性不标（origin=d0，前端用中性色），
      本轮新增的沿用 P2 给的 confidence，**P3 不重算**。
    """
    m = bs.load(mode)
    src = m["source"]
    nodes: list[dict] = []
    edges: list[dict] = []

    for c in m["classes"]:
        is_d0 = c.get("origin") == "d0"
        nodes.append({
            "id": c["id"], "label": c["name"] or c["code"] or c["id"],
            "type": "object_class", "code": c["code"], "definition": c["definition"],
            "domain": c["domain"], "depth": 1, "status": c.get("status", ""),
            "parent_id": c["parent_id"] or None,
            "origin": c.get("origin", ""),
            "confidence": 1.0 if is_d0 else c.get("confidence"),
            "confidence_basis": c.get("confidence_basis", ""),
            # d0 = 已确认不标；其余透传 P2 的来源（rule / llm / unevaluated）
            "confidence_source": "d0" if is_d0 else (c.get("confidence_source") or ""),
            # 证据形态：图上的档位着色靠它（与 P2 置信度同源）
            "evidence_kind": ((c.get("evidence") or {}).get("kind") or ""),
            "section_level": ((c.get("evidence") or {}).get("section_level") or 0),
            "source_mode": f"reference-{mode}",
            "source": src,
            "owl": {"iri": f"{NS}{_iri_safe(c['id'])}", "entity": "owl:Class"},
            "completeness": _completeness("class", c),
        })

    for a in m["attr_instances"]:
        is_d0 = a.get("origin") == "d0"
        nodes.append({
            "id": a["id"], "label": a["name"] or a["code"] or a["attr_id"],
            "type": "field", "code": a["code"], "definition": a["definition"],
            "data_type": a["data_type"], "constraint": a["constraint"], "unit": a["unit"],
            "owner_class": a["class_id"], "owner_class_name": a["class_name"],
            "domain": "", "depth": 2,
            "origin": a.get("origin", ""),
            "confidence": 1.0 if is_d0 else a.get("confidence"),
            "confidence_basis": a.get("confidence_basis", ""),
            "confidence_source": "d0" if is_d0 else (a.get("confidence_source") or ""),
            "evidence_kind": ((a.get("evidence") or {}).get("kind") or ""),
            "section_level": ((a.get("evidence") or {}).get("section_level") or 0),
            "source_mode": f"reference-{mode}",
            "source": src,
            "owl": {"iri": f"{NS}{_iri_safe(a['class_id'])}_{_iri_safe(a['code'] or a['attr_id'])}",
                    "entity": "owl:DatatypeProperty",
                    "domain_class": a["class_id"], "range": _xsd_of(a["data_type"])},
            "completeness": _completeness("field", a),
        })

    # ③ 属性池节点：同一属性被 N 个类取用，图上只一个节点
    for p in m.get("attr_pool") or []:
        is_d0 = p.get("origin") == "d0"
        nodes.append({
            "id": p["id"], "label": p["name"], "type": "attribute",
            "code": p.get("key", ""), "definition": p["definition"],
            "data_type": p["data_type"], "constraint": p["constraint"], "unit": p["unit"],
            "domain": "", "depth": 3,
            "origin": p.get("origin", ""),
            "confidence": 1.0 if is_d0 else p.get("confidence"),
            "confidence_basis": p.get("confidence_basis", ""),
            "confidence_source": "d0" if is_d0 else (p.get("confidence_source") or ""),
            "evidence_kind": ((p.get("evidence") or {}).get("kind") or ""),
            "ref_count": p.get("ref_count", 0),
            "class_names": p.get("class_names") or [],
            "source_mode": f"reference-{mode}", "source": src,
            "owl": {"iri": None, "entity": None,
                    "note": "属性池节点（图内复用视图）；OWL 里由 DatatypeProperty 表达，不单独建实体"},
            "completeness": _completeness("field", p),
        })

    node_ids = {n["id"] for n in nodes}
    edge_label = {"subClassOf": "子类", "hasAttribute": "具有属性",
                  "refersTo": "取用属性池", "associates": "关联"}
    for i, r in enumerate(m["relations"]):
        if r["source"] in node_ids and r["target"] in node_ids:
            edges.append({
                "id": f"e-{r['type']}-{i}", "source": r["source"], "target": r["target"],
                "type": r["type"],
                "label": edge_label.get(r["type"], r["type"]),
                "source_ref": src,
            })

    payload = {"nodes": nodes, "edges": edges}
    _assert_no_test_source(payload, mode)
    return {**payload,
            "mode": mode,
            "source": src,
            "counts": {**m["counts"],
                       "nodes": len(nodes), "edges": len(edges)},
            "supply": m.get("supply"),
            "confidence_summary": m.get("confidence_summary"),
            "data_caveat": m["data_caveat"]}


# ---------------------------------------------------------------------------
# 2) json_to_owl
# ---------------------------------------------------------------------------
def json_to_owl(knowledge: dict, mode: str = "A", write: bool = True) -> dict:
    nodes = knowledge.get("nodes", [])
    edges = knowledge.get("edges", [])
    classes = [n for n in nodes if n.get("type") == "object_class"]
    fields = [n for n in nodes if n.get("type") == "field"]
    subs = [e for e in edges if e.get("type") == "subClassOf"]

    L = [f"@prefix : <{NS}> .",
         "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
         "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
         "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
         "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
         "",
         f"<{NS}> a owl:Ontology ;",
         '    rdfs:label "核电数据标准本体（模式 %s 建模集转换）" ;' % mode,
         '    owl:versionInfo "%s" .' % datetime.now().strftime("%Y-%m-%d"),
         "",
         "###  Annotation properties"]
    for ap, lbl in (("domainName", "业务域"), ("fieldConstraint", "字段约束"),
                    ("fieldUnit", "量纲"), ("confidence", "置信度"), ("sourceMode", "来源模式")):
        L.append(f":{ap} a owl:AnnotationProperty ; rdfs:label \"{lbl}\" .")
    L += ["", "###  Object classes (owl:Class)"]
    for c in classes:
        label = (c.get("label") or "").replace("\\", "\\\\").replace('"', '\\"')
        L.append(f":{_iri_safe(c['id'])} a owl:Class ;")
        L.append(f'    rdfs:label "{label}"@zh ;')
        if c.get("code"):
            L.append(f'    rdfs:label "{c["code"]}" ;')
        if c.get("definition"):
            L.append(f'    rdfs:comment "{c["definition"].replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"@zh ;')
        if c.get("domain"):
            L.append(f'    :domainName "{c["domain"]}" ;')
        # 置信度：只有真评过的才写。None 若直接插值会变成 :confidence "None"^^xsd:double，
        # 那是非法字面量（校验器/推理机会直接报错），所以宁可整行不写。
        _cv = cf.as_float(c.get("confidence"))
        if _cv is not None:
            L.append(f'    :confidence "{_cv}"^^xsd:double ;')
        L.append(f'    :sourceMode "{c.get("source_mode", "")}" .')
    L += ["", "###  Class hierarchy (rdfs:subClassOf)"]
    for e in subs:
        L.append(f":{_iri_safe(e['source'])} rdfs:subClassOf :{_iri_safe(e['target'])} .")

    # 类间关系（P3 大模型发现的 is-a 之外的语义关系）。
    # partOf / associates / sameAs 不是 subClassOf，必须落成 owl:ObjectProperty，
    # 否则「类与类之间的关系」在 OWL 里根本不存在，只剩一张层级表。
    obj_rels = [e for e in edges if e.get("type") in ("partOf", "associates", "sameAs")]
    L += ["", "###  Object properties (类间语义关系：组成 / 业务关联 / 同义)",
          ":partOf a owl:ObjectProperty, owl:TransitiveProperty ;",
          '    rdfs:label "组成于"@zh ;',
          "    owl:inverseOf :hasPart .",
          ':hasPart a owl:ObjectProperty ; rdfs:label "由…组成"@zh .',
          ':associates a owl:ObjectProperty, owl:SymmetricProperty ; rdfs:label "业务关联"@zh .',
          ':sameAs a owl:ObjectProperty, owl:SymmetricProperty, owl:TransitiveProperty ;',
          '    rdfs:label "同义"@zh .',
          ""]
    for e in obj_rels:
        _basis = re.sub(r"[\r\n]+", " ", str(e.get("evidence") or e.get("basis") or ""))
        _cmt = f"  # {_basis[:60]}" if _basis else ""
        L.append(f":{_iri_safe(e['source'])} :{e['type']} :{_iri_safe(e['target'])} .{_cmt}")
    L += ["", "###  Datatype properties (字段级)"]
    for f in fields:
        local = _iri_safe(f["id"].split("::")[0]) + "_" + _iri_safe(f.get("code") or f["id"])
        label = (f.get("label") or "").replace("\\", "\\\\").replace('"', '\\"')
        L.append(f":{local} a owl:DatatypeProperty ;")
        L.append(f'    rdfs:label "{label}"@zh ;')
        L.append(f"    rdfs:domain :{_iri_safe(f['owl'].get('domain_class') or f['owner_class'])} ;")
        L.append(f"    rdfs:range {f['owl'].get('range', 'xsd:string')} ;")
        if f.get("definition"):
            L.append(f'    rdfs:comment "{f["definition"].replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"@zh ;')
        if f.get("constraint"):
            L.append(f'    :fieldConstraint "{f["constraint"]}" ;')
        if f.get("unit"):
            L.append(f'    :fieldUnit "{f["unit"]}" ;')
        _fv = cf.as_float(f.get("confidence"))
        if _fv is not None:
            L.append(f'    :confidence "{_fv}"^^xsd:double ;')
        L.append(f'    :sourceMode "{f.get("source_mode", "")}" .')
    turtle = "\n".join(L) + "\n"

    checks = []
    iri_bad = [n["id"] for n in nodes if n.get("owl", {}).get("iri")
               and ILLEGAL_IRI_CHARS.search(n["owl"]["iri"])]
    checks.append({"name": "IRI 字符集合法", "passed": not iri_bad,
                   "detail": f"{len(nodes)} 个 IRI 均合法" if not iri_bad else f"{len(iri_bad)} 个非法"})
    class_ids = {c["id"] for c in classes}
    field_ids = {f["id"] for f in fields}
    checks.append({"name": "类与属性命名空间不相交",
                   "passed": not (class_ids & field_ids),
                   "detail": f"类 {len(class_ids)} / 属性 {len(field_ids)} 无交集"})
    bad_range = [f["id"] for f in fields if not str(f.get("owl", {}).get("range", "")).startswith("xsd:")]
    checks.append({"name": "DatatypeProperty 的 range 必须是 XSD 类型", "passed": not bad_range,
                   "detail": f"{len(fields)} 个 range 均为 XSD" if not bad_range else f"{len(bad_range)} 个越界"})
    parent = {e["source"]: e["target"] for e in subs}
    cyc, seen = [], set()
    for s in parent:
        if s in seen:
            continue
        path, cur = [], s
        while cur in parent:
            if cur in path:
                cyc.append(s)
                break
            path.append(cur)
            cur = parent[cur]
        seen.update(path)
    checks.append({"name": "subClassOf 无环", "passed": not cyc,
                   "detail": f"{len(subs)} 条层次边无环" if not cyc else f"{len(cyc)} 处环"})

    # 类间语义关系（partOf / associates / sameAs）：两端必须是类，且 partOf 不能成环
    # （partOf 声明为 TransitiveProperty，一旦成环推理机会把 A 推成自己的部件）。
    obj_rel_types = ("partOf", "associates", "sameAs")
    obj_edges = [e for e in edges if e.get("type") in obj_rel_types]
    bad_end = [e["id"] for e in obj_edges
               if e["source"] not in class_ids or e["target"] not in class_ids]
    checks.append({"name": "类间关系两端均为 owl:Class", "passed": not bad_end,
                   "detail": f"{len(obj_edges)} 条类间关系端点合法" if not bad_end
                             else f"{len(bad_end)} 条端点不是类"})
    _pp = {e["source"]: e["target"] for e in obj_edges if e["type"] == "partOf"}
    _pcyc, _pseen = [], set()
    for s in _pp:
        if s in _pseen:
            continue
        _path, _cur = [], s
        while _cur in _pp:
            if _cur in _path:
                _pcyc.append(s)
                break
            _path.append(_cur)
            _cur = _pp[_cur]
        _pseen.update(_path)
    checks.append({"name": "partOf 无环", "passed": not _pcyc,
                   "detail": f"{len(_pp)} 条组成边无环" if not _pcyc else f"{len(_pcyc)} 处环"})
    # 性能：节点集必须**先算一次**。原写法在边循环里重建 set → O(E×N)，
    # 底座扩到 2 万节点后单这一项就要几分钟（实测卡死）。
    node_id_set = {n["id"] for n in nodes}
    dangling = [e["id"] for e in edges
                if e["source"] not in node_id_set or e["target"] not in node_id_set]
    checks.append({"name": "边的两端节点均存在", "passed": not dangling,
                   "detail": f"{len(edges)} 条边均可解析" if not dangling else f"{len(dangling)} 条悬空边"})
    no_label = [n["id"] for n in nodes if n.get("owl", {}).get("entity") and not str(n.get("label", "")).strip()]
    checks.append({"name": "OWL 实体均有 rdfs:label", "passed": not no_label,
                   "detail": "全部实体已标注" if not no_label else f"{len(no_label)} 个缺 label"})

    profile = {
        "profile": "OWL 2 DL",
        "checker": "builtin-structural-profile（结构化剖面校验，非完整推理机：环境无 HermiT/Pellet）",
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "counts": {"classes": len(classes), "datatype_properties": len(fields),
                   "subClassOf_axioms": len(subs), "object_property_axioms": len(obj_edges),
                   "turtle_bytes": len(turtle.encode("utf-8"))},
    }
    if write:
        p = owl_path(mode)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(turtle, encoding="utf-8")

    return {"owl_turtle": turtle, "owl_profile": profile,
            "graph_nodes": nodes, "graph_edges": edges, "owl_path": str(owl_path(mode))}


# ---------------------------------------------------------------------------
# 3) graph_upsert / 读取（按模式）
# ---------------------------------------------------------------------------
def graph_upsert(nodes: list[dict], edges: list[dict], mode: str = "A", reset: bool = False) -> dict:
    payload = {"nodes": nodes, "edges": edges}
    _assert_no_test_source(payload, mode)
    store = {"nodes": {}, "edges": {}} if reset else _load_store_raw(mode)
    an = un = ae = 0
    for n in nodes:
        if n["id"] in store["nodes"]:
            store["nodes"][n["id"]].update(n)
            un += 1
        else:
            store["nodes"][n["id"]] = n
            an += 1
    for e in edges:
        eid = e.get("id") or f"{e['source']}->{e['target']}:{e['type']}"
        if eid not in store["edges"]:
            store["edges"][eid] = e
            ae += 1
    _save_store(store, mode)
    _GRAPH_CACHE.pop(mode, None)
    # 索引与图谱必须同步失效：否则重建后查询仍在旧邻接/旧倒排上跑
    _TOKEN_INDEX.pop(mode, None)
    _CLASS_VEC.pop(mode, None)
    _ADJ_INDEX.pop(mode, None)
    return {"added_nodes": an, "updated_nodes": un, "added_edges": ae,
            "total_nodes": len(store["nodes"]), "total_edges": len(store["edges"])}


def remove_edges(match, mode: str = "A") -> dict:
    """按条件删边，用于撤销「大模型推断的类间关系」。

    `match` 是 `callable(edge) -> bool`。只删边、不动节点（推断出来的关系删掉后，
    类本身还在底座里，属于本轮输入/标准库的资产，不能被顺手清掉）。
    """
    store = _load_store_raw(mode)
    gone = [eid for eid, e in store["edges"].items() if match(e)]
    for eid in gone:
        store["edges"].pop(eid, None)
    if gone:
        _save_store(store, mode)
    _GRAPH_CACHE.pop(mode, None)
    _TOKEN_INDEX.pop(mode, None)
    _CLASS_VEC.pop(mode, None)
    _ADJ_INDEX.pop(mode, None)
    return {"removed_edges": len(gone), "total_edges": len(store["edges"])}


def _load_store_raw(mode: str) -> dict:
    p = store_path(mode)
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"nodes": d.get("nodes", {}), "edges": d.get("edges", {})}
    return {"nodes": {}, "edges": {}}


def _save_store(store: dict, mode: str) -> None:
    p = store_path(mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema": "nuclear-knowledge-graph/v2", "mode": mode,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "nodes": store["nodes"], "edges": store["edges"],
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def get_graph(mode: str = "A") -> dict:
    if mode not in _GRAPH_CACHE:
        st = _load_store_raw(mode)
        _GRAPH_CACHE[mode] = {"nodes": list(st["nodes"].values()), "edges": list(st["edges"].values())}
    return _GRAPH_CACHE[mode]


# ---------------------------------------------------------------------------
# 3.5) purge：清空双知识底座（本体 + OWL + 解析缓存 + 进程内缓存）
# ---------------------------------------------------------------------------
PURGE_DIR = DATA_ROOT / "_purge"
_PARSE_CACHE_DIR = DATA_ROOT / ".parse_cache"

# 清空时要一并失效的模块内缓存：(模块名, 缓存变量名)
_PEER_CACHES = (
    ("tools.local_embedding", "_INDEX_CACHE"),
    ("tools.doc_parser", "_PARSE_CACHE"),
    ("tools.knowledge_extractor", "_CACHE"),
    ("tools.landscape_analyst", "_CACHE"),
    ("tools.requirement_analyst", "_CACHE"),
    ("tools.standard_evolver", "_ANALYSIS"),
    ("tools.standard_generator", "_STANDARDS"),
    ("tools.standard_library", "_CACHE"),
    ("tools.standard_library", "_SCAN_CACHE"),
)


def purge(mode: str = "both", *, archive: bool = True,
          include_parse_cache: bool = True) -> dict:
    """清空 P3 双知识底座及其历史缓存。

    **只删「可重建的派生物」**，绝不碰原始数据：
      · 删：`data/knowledge_graph_{A,B}.json`、`deliverables/shared/ontology-{A,B}.ttl`、
            `data/.parse_cache/*.json`、各模块进程内缓存。
      · 不碰：`data/library/`（D0 上传原件与入库结果）、`data/modeA|modeB/`（D1–D3 槽位）、
              `data/uploaded/`（上传原文件）、`deliverables/shared/standards/`（P5 产物）。

    archive=True 时移到 `data/_purge/<时间戳>/` 而非直接删除，便于事后核对；
    目录本身不在任何读取路径上，不影响「已清空」的效果。
    """
    modes = ["A", "B"] if mode == "both" else [mode]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = PURGE_DIR / stamp if archive else None
    if dest:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "graph").mkdir(exist_ok=True)
        (dest / "owl").mkdir(exist_ok=True)
        if include_parse_cache:
            (dest / "parse_cache").mkdir(exist_ok=True)

    removed, missing = [], []
    moved: list[tuple[Path, Path]] = []

    def _take(p: Path, sub: str = "") -> None:
        if not p.exists():
            missing.append(str(p.relative_to(DATA_ROOT.parent)).replace("\\", "/"))
            return
        rec = {"path": str(p.relative_to(DATA_ROOT.parent)).replace("\\", "/"),
               "bytes": p.stat().st_size}
        if dest:
            tgt = (dest / sub / p.name) if sub else (dest / p.name)
            p.replace(tgt)
            moved.append((p, tgt))
        else:
            p.unlink()
        removed.append(rec)

    for m in modes:
        _take(store_path(m), "graph")
        _take(owl_path(m), "owl")
        # 旧备份（含 D3 泄漏口径的历史版本）一并清掉
        for bak in DATA_ROOT.glob(f"knowledge_graph_{m}.json.bak*"):
            _take(bak, "graph")

    if include_parse_cache and _PARSE_CACHE_DIR.exists():
        for f in sorted(_PARSE_CACHE_DIR.glob("*.json")):
            _take(f, "parse_cache")
        try:
            if not any(_PARSE_CACHE_DIR.iterdir()):
                _PARSE_CACHE_DIR.rmdir()
        except OSError:
            pass

    # 进程内缓存全部失效（否则界面仍显示清空前读进内存的旧图）
    _GRAPH_CACHE.clear()
    _TOKEN_INDEX.clear()
    _CLASS_VEC.clear()
    _ADJ_INDEX.clear()
    cleared = ["tools.ontology_builder._GRAPH_CACHE", "tools.ontology_builder._TOKEN_INDEX",
               "tools.ontology_builder._CLASS_VEC", "tools.ontology_builder._ADJ_INDEX"]
    for mod_name, var in _PEER_CACHES:
        try:
            mod = __import__(mod_name, fromlist=[var])
            obj = getattr(mod, var, None)
            if isinstance(obj, dict):
                obj.clear()
                cleared.append(f"{mod_name}.{var}")
        except Exception:
            continue

    return {"ok": True, "modes": modes, "archived": archive,
            "archive_dir": (str(dest.relative_to(DATA_ROOT.parent)).replace("\\", "/")
                            if dest else ""),
            "removed": removed, "not_found": missing,
            "removed_count": len(removed),
            "removed_bytes": sum(r["bytes"] for r in removed),
            "caches_cleared": cleared,
            "graph_now": {m: graph_counts(m) for m in modes}}


def graph_counts(mode: str = "A") -> dict:
    """当前磁盘上的图谱规模（不读缓存，用于清理后自证）。"""
    st = _load_store_raw(mode)
    return {"exists": store_path(mode).exists(),
            "nodes": len(st["nodes"]), "edges": len(st["edges"])}


# ---------------------------------------------------------------------------
# 4) 候选入图（P2 抽取结果，带 confidence / source_mode）
# ---------------------------------------------------------------------------
def upsert_candidates(mode: str = "A", llm: bool = False) -> tuple[dict, int]:
    """P2 候选入图。llm=False（默认）：P3 不复跑大模型增强，只挂已有候选。"""
    res = ke.extract(mode, llm=llm)
    nodes, edges = [], []
    # 性能：图节点只取一次（get_graph 每次都重建 list，候选多时是 O(cand × N)）
    g_nodes = get_graph(mode)["nodes"]
    label_index = {(n.get("label") or ""): n["id"] for n in g_nodes
                   if n.get("type") in ("object_class", "field")}
    for c in res.get("candidates", []):
        nid = f"CAND::{mode}::{c['id']}"
        nodes.append({
            "id": nid, "label": c.get("name", ""), "type": "candidate",
            "code": c.get("id", ""), "definition": c.get("definition", ""),
            "cand_type": c.get("type", ""), "novelty": c.get("novelty", ""),
            "domain": "", "depth": 0, "confidence": c.get("confidence", 0.0),
            "source_mode": f"P2-extract-{mode}",
            "source": f"knowledge_extractor.extract(mode={mode})",
            "evidence": c.get("evidence_span", {}),
            "owl": {"iri": None, "entity": None, "note": "候选节点，未经人工门控，暂不入 OWL 本体"},
            "completeness": {"status": "INCOMPLETE" if c.get("confidence", 0) < cf.LOW_CONF else "COMPLETE",
                             "missing": [] if c.get("confidence", 0) >= cf.LOW_CONF else ["人工门控确认"],
                             "warnings": [f"confidence={c.get('confidence')}"]},
        })
        term = (c.get("term_norm") or {}).get("original") or c.get("name")
        hit = label_index.get(term)
        if hit:
            edges.append({
                "id": f"e-cand-{mode}-{c['id']}", "source": nid, "target": hit,
                "type": "candidateMatch", "label": f"候选匹配（conf {c.get('confidence')}）",
                "source_ref": f"knowledge_extractor.extract(mode={mode})",
            })
    return graph_upsert(nodes, edges, mode), len(nodes)


# ---------------------------------------------------------------------------
# 5) 模式 B 需求相关性
# ---------------------------------------------------------------------------
def compute_requirement_relevance(mode: str = "B") -> dict:
    rel: dict[str, float] = {}
    # 2026-10-23：改走 requirement_analyst 的公开取数口（多份合并 + 去重）。
    # 原来调 ke._read_requirement()（只读单份文件），P2 与 P5/P7 因此口径不一致。
    try:
        from tools import requirement_analyst as ra  # 延迟导入：ra 模块级 import 本模块
        ds = ra.load_requirement_dataset()
    except Exception:
        return rel
    names = {c.get("name") for c in ds.get("classes") or [] if c.get("name")}
    attrs = {r.get("name") for r in ds.get("rows") or [] if r.get("name")}
    for n in get_graph(mode)["nodes"]:
        label = (n.get("label") or "").strip()
        if n.get("type") == "object_class":
            if label in names:
                rel[n["id"]] = 1.0
            elif any(x and (x in label or label in x) for x in names):
                rel[n["id"]] = 0.6
        elif n.get("type") == "field":
            owner = (n.get("owner_class_name") or "").strip()
            owner_hit = owner in names
            attr_hit = label in attrs
            attr_sub = any(x and (x in label or label in x) for x in attrs)
            if owner_hit and attr_hit:
                rel[n["id"]] = 1.0
            elif attr_hit:
                rel[n["id"]] = 0.6
            elif owner_hit and attr_sub:
                rel[n["id"]] = 0.5
            elif owner_hit:
                rel[n["id"]] = 0.35
    return rel


# ---------------------------------------------------------------------------
# 6) graph_query（向量实体匹配 + 多跳路径传播）
# ---------------------------------------------------------------------------
# 多跳传播参数（变更需在面板说明）
HOP_DECAY = 0.55
EDGE_WEIGHT = {"subClassOf": 0.95, "hasAttribute": 0.85,
               "belongsToDomain": 0.6, "candidateMatch": 0.7,
               # 大模型发现的类间语义关系（P3 class_relations）：比父子弱一档
               "partOf": 0.9, "associates": 0.55, "sameAs": 0.88}
# 反向遍历（属性 → 所属类、父类 → 子类）权重打折：语义上是弱关联
REVERSE_W = {"subClassOf": 0.8, "hasAttribute": 0.45,
             "belongsToDomain": 0.7, "candidateMatch": 0.6,
             "partOf": 0.9, "associates": 0.6, "sameAs": 0.95}
_TOKEN_INDEX: dict[str, dict] = {}
_CLASS_VEC: dict[str, object] = {}
_ADJ_INDEX: dict[str, dict] = {}


def _adj_index(mode: str) -> dict:
    """邻接表 + 节点/边引用，按模式缓存。

    原来每个查询都重建邻接表（2.6 万边）再扫一遍节点找子图 —— 单次查询就要
    几百万次操作，P6 演化 855 个字段逐个查会跑成几分钟。这里建一次复用。
    """
    if mode in _ADJ_INDEX:
        return _ADJ_INDEX[mode]
    g = get_graph(mode)
    nodes, edges = g["nodes"], g["edges"]
    adj: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    # 边 → 端点反查：子图裁剪时不扫全量边（2.6 万条 × 每次查询）
    incident: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        incident[e["source"]].append(e)
        if e["target"] != e["source"]:
            incident[e["target"]].append(e)
        et = e.get("type", "")
        if et not in EDGE_WEIGHT:
            continue
        adj[e["source"]].append((e["target"], et, EDGE_WEIGHT[et]))
        adj[e["target"]].append((e["source"], et, REVERSE_W.get(et, 0.7)))
    _ADJ_INDEX[mode] = {"adj": adj, "nodes": nodes, "edges": edges,
                        "by_id": {n["id"]: n for n in nodes},
                        "incident": incident}
    return _ADJ_INDEX[mode]


def _token_index(mode: str) -> dict:
    """全量节点的稀疏倒排索引（token → 节点 id 集合）。

    2.4 万节点逐个做稠密编码太慢（十几秒），属性节点改用倒排 token 精确召回，
    对象类节点才走本地向量——两者取 max 作为实体分。
    """
    if mode in _TOKEN_INDEX:
        return _TOKEN_INDEX[mode]
    from tools import local_embedding as le
    post: dict[str, set] = defaultdict(set)
    df: Counter = Counter()
    for n in get_graph(mode)["nodes"]:
        toks = set(le._tokens(f"{n.get('label', '')} {n.get('definition', '')}"))
        for t in toks:
            post[t].add(n["id"])
        df.update(toks)
    _TOKEN_INDEX[mode] = {"post": post, "df": df, "n": max(len(get_graph(mode)["nodes"]), 1),
                          "by_id": {n["id"]: n for n in get_graph(mode)["nodes"]}}
    return _TOKEN_INDEX[mode]


def _class_vector_index(mode: str):
    """对象类节点的本地向量索引（几十到几百条，成本低）。

    2026-10-24：业务域节点已从底座移除，这里不再收 domain 类型。
    """
    if mode in _CLASS_VEC:
        return _CLASS_VEC[mode]
    from tools import local_embedding as le
    nodes = [n for n in get_graph(mode)["nodes"]
             if n.get("type") == "object_class"]
    idx = le.Index().build(
        [{"name": n.get("label", ""), "definition": n.get("definition", ""), "id": n["id"],
          "node": n} for n in nodes],
        text_fn=lambda r: f"{r['name']} {r.get('definition', '')}")
    _CLASS_VEC[mode] = idx
    return idx


def _entity_match(query: str, mode: str, topk: int) -> tuple[list[dict], dict]:
    import math
    ti = _token_index(mode)
    post, df, N = ti["post"], ti["df"], ti["n"]
    from tools import local_embedding as le
    toks = set(le._tokens(query))
    if not toks:
        return [], {"sparse": 0, "vector": 0}

    # ① 稀疏 TF-IDF 累积（只遍历有共现 token 的节点）
    acc: dict[str, float] = defaultdict(float)
    for t in toks:
        ids = post.get(t)
        if not ids:
            continue
        idf = math.log((1 + N) / (1 + len(ids))) + 1.0
        for nid in ids:
            acc[nid] += idf
    sparse_norm = math.sqrt(sum(v * v for v in acc.values())) or 1.0
    hits = {nid: v / sparse_norm for nid, v in acc.items()}

    # ② 对象类向量召回（补语义相近，如「管径」→「公称直径」）
    vi = _class_vector_index(mode)
    vec_hits = 0
    for h in (vi.search(query, topk=topk * 2) if vi.items else []):
        nid = h["payload"]["node"]["id"]
        if h["score"] > hits.get(nid, 0):
            hits[nid] = h["score"]
            vec_hits += 1

    scored = []
    qn = query.strip()
    for nid, s in hits.items():
        n = ti["by_id"].get(nid)
        if not n:
            continue
        label = str(n.get("label", ""))
        # 精确/子串命中是强信号，直接压过 TF-IDF 的归一化得分
        if qn and label == qn:
            s, reason = 1.0, "名称精确命中"
        elif qn and qn in label:
            s, reason = max(0.85, s), "名称子串命中"
        elif qn and label and label in qn:
            s, reason = max(0.75, s), "名称被包含"
        else:
            reason = "语义/词项近似"
        scored.append({"node": n, "score": min(round(s, 4), 1.0), "reason": reason})
    scored.sort(key=lambda x: (-x["score"], x["node"]["id"]))
    return scored[:topk], {"sparse": len(acc), "vector": vec_hits}


def graph_query(query: str, mode: str = "A", topk: int = 12, hop: int = 2) -> dict:
    """语义查询：向量实体匹配 → 多跳路径传播 → 子图裁剪。

    2026-10-23 重写。原来叫 rag_graph，实际只是「同名节点校验 + 一层邻域扩展」：
    命中靠字面、扩展不带权重、结果里没有路径，等于字典查找。
    现在：
      · 实体匹配 = 稀疏 TF-IDF 倒排 + 对象类本地向量（取 max）；
      · 多跳 = 从种子按边权重 × 衰减 BFS，保留每个节点的最优路径；
      · 输出 `paths`，说明「为什么这个节点被召回」。
    """
    g = get_graph(mode)
    nodes, edges = g["nodes"], g["edges"]
    q = (query or "").strip()
    if not q:
        return {"query": q, "mode": mode, "matched": [], "paths": [],
                "subgraph": {"nodes": [], "edges": []},
                "trace": [{"step": "输入校验", "detail": "查询为空，未执行检索"}]}

    syn2can, _ = ke.load_domain_vocab()
    norm = ke.term_normalize([q] + re.split(r"[\s,，、;；]+", q), syn2can)
    canon_terms = sorted({n["canonical"] for n in norm if n.get("canonical")})
    trace = [{"step": "术语归一 term_normalize",
              "detail": " → ".join(f"{n['original']}→{n['canonical']}[{n['method']}]" for n in norm[:6])
                        or "无命中，按原词检索"}]

    seeds, match_stat = _entity_match(q, mode, topk)
    trace.append({"step": "实体匹配（稀疏 TF-IDF 倒排 + 对象类本地向量）",
                  "detail": f"候选 {match_stat['sparse']} 个（向量补入 {match_stat['vector']}），"
                            f"取 top {len(seeds)}"})

    # ---- 多跳路径传播 ----
    # 有向边：subClassOf 子→父、hasAttribute 类→属性。反向遍历权重打折
    # （属性 → 类 → 兄弟属性 这种两跳在图上合法，但语义弱，不能和白拿一样计分）
    adj = _adj_index(mode)["adj"]

    best: dict[str, dict] = {}
    for s in seeds:
        nid = s["node"]["id"]
        best[nid] = {"score": s["score"], "hop": 0,
                     "path": [nid], "edges": [], "reason": s["reason"]}
    frontier = [s["node"]["id"] for s in seeds]
    for h in range(1, max(0, hop) + 1):
        nxt = []
        for nid in frontier:
            cur = best.get(nid)
            if not cur:
                continue
            for nb, et, ew in adj.get(nid, []):
                cand = cur["score"] * ew * HOP_DECAY
                if cand < 0.02:
                    continue
                if cand > best.get(nb, {}).get("score", 0.0):
                    best[nb] = {"score": cand, "hop": h,
                                "path": cur["path"] + [nb],
                                "edges": cur["edges"] + [et],
                                "reason": f"{h} 跳经 {et}"}
                    nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    trace.append({"step": f"多跳路径传播（{hop} 跳，衰减 {HOP_DECAY}，按边类型加权）",
                  "detail": f"召回 {len(best)} 个节点，其中 {sum(1 for v in best.values() if v['hop'] > 0)} 个来自多跳"})

    by_id = _adj_index(mode)["by_id"]
    TYPE_CN = {"object_class": "对象类", "field": "对象类属性",
               "attribute": "属性", "candidate": "候选"}

    def _path_labels(ids: list[str]) -> list[str]:
        """路径标签：同名不同节点（候选 vs 字段 vs 类）加类型后缀，否则看起来像重复。"""
        labels = [by_id.get(x, {}).get("label", x) for x in ids]
        dup = {lb for lb in labels if labels.count(lb) > 1}
        if not dup:
            return labels
        out = []
        for x in ids:
            n = by_id.get(x, {})
            lb = n.get("label", x)
            if lb in dup:
                lb = f"{lb}（{TYPE_CN.get(n.get('type'), '节点')}）"
            out.append(lb)
        return out

    # 子图 = 直命中的种子 + 传播得分最高的若干跳节点
    # 注意：D0 里同一字段名会挂在多个对象类下（「公称直径-公制」在 12 个类下都有），
    # 种子本身就有一批同 label 不同 id 的节点；不能拿它们去挤占路径名额。
    seed_ids = [s["node"]["id"] for s in seeds]
    prop = sorted(((nid, v) for nid, v in best.items() if v["hop"] > 0),
                  key=lambda kv: (-kv[1]["score"], kv[1]["hop"], kv[0]))
    keep_prop = [nid for nid, _ in prop[:max(topk * 4, 40)]]
    sub_ids = set(seed_ids) | set(keep_prop)
    # 路径多样性：先按 (跳数, 边序列) 分组，再轮转取。
    # 不轮转的话，12 条会全是同一种 1 跳（字段→候选 / 字段→所属类），看不出"多跳"。
    groups: dict[tuple, list[str]] = {}
    for nid in keep_prop:
        v = best[nid]
        groups.setdefault((v["hop"], tuple(v["edges"])), []).append(nid)
    sigs = sorted(groups, key=lambda sg: (-best[groups[sg][0]]["score"], groups[sg][0]))
    paths: list[dict] = []
    depth = 0
    while len(paths) < 12 and any(depth < len(groups[sg]) for sg in sigs):
        for sg in sigs:
            if depth >= len(groups[sg]) or len(paths) >= 12:
                continue
            nid = groups[sg][depth]
            v = best[nid]
            paths.append({"target": by_id[nid].get("label", nid),
                          "target_id": nid, "hops": v["hop"],
                          "score": round(v["score"], 4),
                          "via": v["edges"],
                          "path_labels": _path_labels(v["path"])})
        depth += 1

    gi = _adj_index(mode)
    sub_nodes = [n for n in nodes if n["id"] in sub_ids]
    seen_e: set = set()
    sub_edges = []
    for nid in sub_ids:
        for e in gi["incident"].get(nid, []):
            eid = e.get("id") or f"{e['source']}->{e['target']}:{e.get('type', '')}"
            if eid in seen_e or e["source"] not in sub_ids or e["target"] not in sub_ids:
                continue
            seen_e.add(eid)
            sub_edges.append(e)
    trace.append({"step": "子图裁剪",
                  "detail": f"节点 {len(sub_ids)} / 边 {len(sub_edges)}"
                            f"（种子 {len(seed_ids)} + 多跳保留 {len(keep_prop)}）"
                            + ("；展示路径 " + "、".join(f"{h} 跳×{c}"
                               for h, c in sorted(Counter(p['hops'] for p in paths).items()))
                               if paths else "")})
    label_cnt = Counter(s["node"].get("label", "") for s in seeds)
    dup_label = [f"{k}×{v}" for k, v in label_cnt.most_common(3) if v > 1]
    if dup_label:
        trace.append({"step": "同名多挂载",
                      "detail": "命中名称重复出现：" + "、".join(dup_label) +
                                "（同一字段名挂在多个对象类下，均按独立节点召回）"})

    return {
        "query": q, "mode": mode, "hop": hop, "normalized": canon_terms,
        "matched": [{"id": x["node"]["id"], "label": x["node"]["label"], "type": x["node"]["type"],
                     "score": round(x["score"], 4), "reason": x["reason"],
                     "owner": x["node"].get("owner_class") or x["node"].get("domain") or "",
                     "hop": best.get(x["node"]["id"], {}).get("hop", 0),
                     "path": _path_labels(best.get(x["node"]["id"], {}).get("path", [])),
                     "confidence": x["node"].get("confidence", 1.0)} for x in seeds],
        "paths": paths,
        "subgraph": {"nodes": sub_nodes, "edges": sub_edges},
        "trace": trace,
    }


# ---------------------------------------------------------------------------
# 7) 统计（三维度 + 阈值对照）
# ---------------------------------------------------------------------------
def graph_stats(mode: str = "A") -> dict:
    g = get_graph(mode)
    nodes, edges = g["nodes"], g["edges"]
    by_type = Counter(n.get("type") for n in nodes)
    counts = {
        "object_class": by_type.get("object_class", 0),
        # 语义保持：metric_counts.attribute 指「对象类属性」条数（原先记在 field 上），
        # 属性池另记 attribute_pool，避免把两层混成一个数
        "attribute": by_type.get("field", 0),
        "relation": len([e for e in edges if e.get("type") in ("subClassOf", "hasAttribute")]),
    }
    chk = mi.assert_metrics(mode, counts)
    # 低置信分界统一取 cf.LOW_CONF（当前 0.5），与 P2 候选筛选同口径；
    # 桶名同步带出阈值，前端按 LOW_BUCKET 常量取值，避免两边写死不同。
    # 没有置信度的节点（D2 需求表无置信度列 / 兜底补建的类）单独归「未评估」桶，
    # **既不进低置信也不进高置信** —— 否则要么满屏标红，要么凭空背书。
    hi_key = f"high(>={cf.LOW_CONF})"
    lo_key = f"low(<{cf.LOW_CONF})"
    conf = Counter()
    unevaluated = 0
    for n in nodes:
        v = cf.as_float(n.get("confidence"))
        if v is None:
            conf[cf.UNKNOWN_BUCKET] += 1
            unevaluated += 1
        else:
            conf[hi_key if v >= cf.LOW_CONF else lo_key] += 1
    try:
        m = bs.load(mode)
    except Exception:
        m = {"source": "（底座不可用）", "source_human": "（底座不可用）",
             "data_caveat": "", "supply": None}
    return {
        "mode": mode,
        "total_nodes": len(nodes), "total_edges": len(edges),
        "nodes_by_type": dict(by_type),
        "edges_by_type": dict(Counter(e.get("type") for e in edges)),
        "metric_counts": counts,
        "metric_check": chk,
        "confidence_dist": dict(conf),
        "unevaluated_nodes": unevaluated,
        "incomplete_nodes": sum(1 for n in nodes if n.get("completeness", {}).get("status") == "INCOMPLETE"),
        "source": m["source"],
        "source_human": m.get("source_human", m["source"]),
        "supply": m.get("supply"),
        "data_caveat": m["data_caveat"],
        "sources": sorted({n.get("source_mode", "") for n in nodes}),
    }


def node_detail(node_id: str, mode: str = "A") -> dict | None:
    g = get_graph(mode)
    for n in g["nodes"]:
        if n["id"] == node_id:
            rel = [e for e in g["edges"] if e["source"] == node_id or e["target"] == node_id]
            return {"node": n, "edges": rel[:40], "edge_count": len(rel)}
    return None


# ---------------------------------------------------------------------------
# 8) 一键构建
# ---------------------------------------------------------------------------
def build(mode: str = "A", with_candidates: bool = True) -> dict:
    base = build_base_graph(mode)
    _assert_no_test_source(base, mode)
    r1 = graph_upsert(base["nodes"], base["edges"], mode, reset=True)
    r2, n_cand = ({"added_nodes": 0}, 0)
    if with_candidates:
        r2, n_cand = upsert_candidates(mode)
    owl = json_to_owl(get_graph(mode), mode)
    return {"mode": mode, "upsert_base": r1, "upsert_candidates": r2,
            "candidate_count": n_cand,
            "owl_profile": owl["owl_profile"], "owl_path": owl["owl_path"],
            "stats": graph_stats(mode)}


if __name__ == "__main__":
    for m in ("A", "B"):
        r = build(m)
        s = r["stats"]
        print(f"===== 模式 {m} =====")
        print("  来源:", s["source_human"], "|", s["source"])
        print("  图谱: 节点", s["total_nodes"], "/ 边", s["total_edges"], "| 类型", s["nodes_by_type"])
        print("  三维度:", s["metric_counts"])
        for it in s["metric_check"]["items"]:
            print(f"     {it['metric']:<13} {it['value']:>5} / 阈值 {it['threshold']:>4}  "
                  f"{'PASS' if it['pass'] else 'FAIL'}")
        print("  全达标:", s["metric_check"]["all_pass"])
        print("  OWL 剖面:", r["owl_profile"]["passed"])
        if s["data_caveat"] != "数据完整":
            print("  数据残缺提示:", s["data_caveat"])
        print()
    print("模式 A 语义查询:", len(graph_query("管道", mode="A", topk=8)["matched"]), "命中")
    print("模式 B 语义查询:", len(graph_query("弯头", mode="B", topk=8)["matched"]), "命中")
