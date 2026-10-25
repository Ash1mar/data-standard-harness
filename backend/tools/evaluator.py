"""
evaluator.py
============
D3 预期输出（标准评测集）· 数据标准生成准确率（SKILL 1.3.4 · v3.1）

口径（与 SKILL 1.3.4 一致）：
  ① 实体对齐：编码精确 → 归一名称精确 → 语义相似（≥0.85）→ 匈牙利一对一
     · 精确匹配先吃掉大部分行，残差才进语义匹配，避免 O(n^3) 在 891 行上跑不动
     · 残差规模超限时降级为贪心，并在报告的 notes 里如实标注
  ② 行内判定：exact / partial / missing / extra
     关键字段 = name / data_type / constraint / unit；编码单列统计
  ③ 计分：行级严格、行级宽松、P/R/F1（严格口径）、字段级、micro/macro、分对象类
  ④ 错误归因：漏抽 / 多抽 / 名称不一致 / 类型错 / 约束错 / 单位错 / 疑似粒度错

安全边界：
  - `load_gold()` 是**唯一**允许读取 `*/test/output/` 的入口，只读；
  - 结果只写 `deliverables/evaluation/`，不写图谱 / RAG / 缓存 / few-shot；
  - 本模块不调用 `assert_allowed`（test/output 对建模是硬禁，对评测是合法输入），
    因此严禁把本模块的返回值喂给建模链路。

CLI：
    python backend/tools/evaluator.py B
    python backend/tools/evaluator.py A --standard-id STD-A-123456
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import upload_manager as um  # noqa: E402
from tools import eval_pipeline as ep  # noqa: E402
from tools import dataset_registry as registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = ROOT / "data"
DELIVERABLES = ROOT / "deliverables"
STD_DIR = DELIVERABLES / "shared" / "standards"
EVAL_DIR = DELIVERABLES / "evaluation"

KEY_FIELDS = ("name", "data_type", "constraint", "unit")
# 判定 exact 只用"输入侧与答案侧口径一致"的字段。
# constraint 在 test/input 里是业务规则文本（如"仅允许 SCH+数字"），
# 在 test/output 里是技术格式（如 VARCHAR(256)），两者不可直接比，单列参考、不计入 exact。
STRICT_FIELDS = ("name", "data_type", "unit")
FIELD_LABEL = {"name": "名称", "data_type": "数据类型", "constraint": "约束（参考）", "unit": "单位"}
MATCH_THRESHOLD = 0.85      # 判为同一行的相似度下限
SUSPECT_THRESHOLD = 0.75    # 0.75–0.85 记为"疑似"，不配对
HUNGARIAN_MAX_CELLS = 40000 # 残差配对矩阵上限（约 200×200）
DETAIL_LIMIT = 600          # 返回前端的明细条数（CSV 始终全量）


# ---------------------------------------------------------------------------
# 1) 归一化
# ---------------------------------------------------------------------------
_FULL2HALF = str.maketrans("（）［］｛｝，。；：", "()[]{},.;:")


def _norm_text(s) -> str:
    """名称归一：全角转半角 + 去括号内容（多为单位）+ 去空白 + 去主/支后缀 + 小写。

    `-主` / `-支` 是本项目测试集用来区分主管/支管的示例后缀，
    输入侧（业务需求）不区分，匹配时必须剥离，否则会被误判为名称不一致。
    """
    s = str(s or "").strip().translate(_FULL2HALF)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[-－—_](主|支|副)$", "", s)
    return s.lower()


_TYPE_MAP = {
    "string": "string", "str": "string", "字符串": "string", "文本": "string",
    "char": "string", "varchar": "string", "nvarchar": "string", "text": "string",
    "int": "int", "integer": "int", "整型": "int", "整数": "int", "long": "int", "bigint": "int",
    "decimal": "decimal", "float": "decimal", "double": "decimal", "number": "decimal",
    "numeric": "decimal", "数值": "decimal", "小数": "decimal",
    "date": "date", "日期": "date",
    "datetime": "datetime", "日期时间": "datetime", "timestamp": "datetime",
    "bool": "boolean", "boolean": "boolean", "布尔": "boolean",
    "enum": "enum", "枚举": "enum",
}


def _norm_type(s) -> str:
    t = str(s or "").strip().lower()
    return _TYPE_MAP.get(t, t)


_UNIT_MAP = {
    "毫米": "mm", "mm": "mm", "厘米": "cm", "cm": "cm", "米": "m", "m": "m",
    "千克": "kg", "kg": "kg", "克": "g", "g": "g",
    "兆帕": "mpa", "mpa": "mpa", "千帕": "kpa", "kpa": "kpa",
    "摄氏度": "℃", "℃": "℃", "度": "℃",
}


def _norm_unit(s) -> str:
    u = str(s or "").strip().lower().replace(" ", "")
    if u in ("", "1", "-", "—", "无", "none", "null", "无量纲"):
        return ""
    return _UNIT_MAP.get(u, u)


def _norm_constraint(s) -> str:
    c = str(s or "").strip().lower().replace(" ", "")
    if c in ("", "-", "—", "无", "none", "null"):
        return ""
    m = {"必填": "非空", "notnull": "非空", "required": "非空",
         "unique": "唯一", "主键": "唯一", "primarykey": "唯一"}
    return m.get(c, c)


_NORM = {"name": _norm_text, "data_type": _norm_type,
         "constraint": _norm_constraint, "unit": _norm_unit}


# ---------------------------------------------------------------------------
# 2) 相似度（3-gram Jaccard + 归一编辑距离）
# ---------------------------------------------------------------------------
def _grams(s: str, n: int = 3) -> set[str]:
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: str, b: str) -> float:
    A, B = _grams(a), _grams(b)
    if not A and not B:
        return 1.0
    return len(A & B) / len(A | B)


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la or not lb:
        return max(la, lb)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ai != b[j - 1]))
        prev = cur
    return prev[lb]


def similarity(a: str, b: str) -> float:
    """SKILL 权重为 0.5/0.3/0.2（含向量项）；本实现未启用向量，按 0.6/0.4 重分配。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    j = _jaccard(a, b)
    l = 1 - _lev(a, b) / max(len(a), len(b))
    return round(0.6 * j + 0.4 * l, 4)


# ---------------------------------------------------------------------------
# 3) 匈牙利算法（纯 Python，最小代价）
# ---------------------------------------------------------------------------
def _hungarian(cost: list[list[float]]) -> list[tuple[int, int]]:
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    size = max(n, m)
    BIG = 10.0
    a = [[cost[i][j] if i < n and j < m else BIG for j in range(size)] for i in range(size)]
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            row = a[i0 - 1]
            for j in range(1, size + 1):
                if not used[j]:
                    cur = row[j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    out = []
    for j in range(1, size + 1):
        i = p[j]
        if i and i <= n and j <= m:
            out.append((i - 1, j - 1))
    return out


# ---------------------------------------------------------------------------
# 4) 读取评测集（D3 预期输出）与模型生成结果
# ---------------------------------------------------------------------------
def _hdr(rows) -> list[str]:
    return [str(c).strip() if c is not None else "" for c in rows[0]]


def _col(hdr: list[str], *names: str) -> int:
    for n in names:
        if n in hdr:
            return hdr.index(n)
    return -1


def load_gold(mode: str) -> dict:
    """读取该模式的评测集（D3 预期输出；上传槽位优先，否则内置预期输出目录）。

    **这是全项目唯一允许读 `*/test/output/` 的地方。** 只读，不落图谱/RAG/缓存。
    """
    slot = {"A": "test_a", "B": "test_b"}.get(mode)
    if slot is None:
        raise ValueError(f"未知模式：{mode}")
    path = um.active_path(slot)
    uploaded = um.is_uploaded(slot)

    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    classes: list[dict] = []
    if "对象类" in wb.sheetnames:
        ws = wb["对象类"]
        rows = list(ws.iter_rows(values_only=True))
        if rows:
            h = _hdr(rows)
            c_id = _col(h, "对象类ID")
            c_name = _col(h, "对象类名称", "中文名称")
            c_code = _col(h, "对象类编码")
            for r in rows[1:]:
                if c_id < 0 or not r[c_id]:
                    continue
                classes.append({
                    "id": str(r[c_id]).strip(),
                    "name": str(r[c_name]).strip() if c_name >= 0 and r[c_name] else "",
                    "code": str(r[c_code]).strip() if c_code >= 0 and r[c_code] else "",
                })

    attrs: list[dict] = []
    sheet = "对象类属性" if "对象类属性" in wb.sheetnames else None
    if sheet:
        ws2 = wb[sheet]
        rows2 = list(ws2.iter_rows(values_only=True))
        if rows2:
            h2 = _hdr(rows2)
            a_cid = _col(h2, "对象类ID")
            a_cname = _col(h2, "对象类名称", "中文名称")
            a_aid = _col(h2, "属性ID")
            a_code = _col(h2, "属性编码")
            a_name = _col(h2, "属性中文名称", "属性名称")
            a_dt = _col(h2, "数据类型")
            a_cons = _col(h2, "数据格式/约束", "约束")
            a_unit = _col(h2, "属性单位", "单位", "量纲")
            a_def = _col(h2, "属性定义", "中文定义", "定义")
            cls_name = {c["id"]: c["name"] for c in classes}
            for r in rows2[1:]:
                if a_cid < 0 or not r[a_cid] or (a_name >= 0 and not r[a_name]):
                    continue
                cid = str(r[a_cid]).strip()
                attrs.append({
                    "gold_id": str(r[a_aid]).strip() if a_aid >= 0 and r[a_aid] else "",
                    "class_id": cid,
                    "class_name": (str(r[a_cname]).strip() if a_cname >= 0 and r[a_cname]
                                   else cls_name.get(cid, "")),
                    "code": str(r[a_code]).strip() if a_code >= 0 and r[a_code] else "",
                    "name": str(r[a_name]).strip() if a_name >= 0 and r[a_name] else "",
                    "data_type": str(r[a_dt]).strip() if a_dt >= 0 and r[a_dt] else "",
                    "constraint": str(r[a_cons]).strip() if a_cons >= 0 and r[a_cons] else "",
                    "unit": str(r[a_unit]).strip() if a_unit >= 0 and r[a_unit] else "",
                    "definition": str(r[a_def]).strip() if a_def >= 0 and r[a_def] else "",
                })
    wb.close()

    if not attrs:
        raise ValueError(f"D3 预期输出「{path}」中未找到可用属性行（需含 `对象类属性` 表与名称列）")

    return {"path": str(path).replace("\\", "/"), "uploaded": uploaded,
            "classes": classes, "attrs": attrs}


def load_pred(mode: str, standard_id: str | None = None) -> dict:
    """读取模型生成的标准（P6 产物）。standard_id 为空时取 latest-{mode}.json。"""
    if standard_id:
        p = STD_DIR / f"{standard_id}.json"
    else:
        p = STD_DIR / f"latest-{mode}.json"
    if not p.exists():
        raise FileNotFoundError(
            f"未找到标准产物 {p.name}：请先在 P6 生成标准（入口 {mode}），再运行评测")
    d = json.loads(p.read_text(encoding="utf-8"))
    rows = d.get("field_specs") or []
    if not rows:
        raise ValueError(f"标准 {d.get('standard_id', p.name)} 没有 field_specs 行")
    # 类清单：优先标准里存的 classes（含 0 属性类），退回用 field_specs 的 class_name 去重
    classes = d.get("classes") or []
    if not classes:
        seen: list[dict] = []
        got: set[str] = set()
        for r in rows:
            cn = str(r.get("class_name") or "").strip()
            if cn and cn not in got:
                got.add(cn)
                seen.append({"name": cn, "code": r.get("class_code", ""), "attr_count": 0})
        classes = seen
    return {
        "standard_id": d.get("standard_id", ""),
        "object_class_name": d.get("object_class_name", ""),
        "source_path": d.get("source_path", mode),
        "source_note": d.get("source_note", ""),
        "classes": classes,
        "scope": d.get("scope") or {},
        "generated_at": d.get("generated_at", ""),
        "rows": rows,
        # 关系随产物带出（2026-10-23）。旧产物没有该字段 → 返回 None，由调用方
        # 从 classes/rows 兜底推导，不假装有关系。
        "relations": d.get("relations"),
        "relation_counts": d.get("relation_counts") or {},
        "path": str(p).replace("\\", "/"),
    }


# ---------------------------------------------------------------------------
# 5) 对齐
# ---------------------------------------------------------------------------
def _align_block(preds: list[dict], golds: list[dict], pis: list[int], gis: list[int],
                 pred_class: str = "") -> tuple[list, list, list]:
    """在给定索引子集内做一对一匹配，返回 (pairs, suspects, notes)。"""
    used_p: set[int] = set()
    used_g: set[int] = set()
    pairs: list[tuple[int, int, str, float]] = []
    notes: list[str] = []
    pc = _norm_text(pred_class)

    # ① 编码精确（名称相似度 ≥ 0.6 作为护栏，防编码复用错配）
    code_map: dict[str, list[int]] = defaultdict(list)
    for gi in gis:
        if golds[gi]["code"]:
            code_map[golds[gi]["code"]].append(gi)
    for pi in pis:
        p = preds[pi]
        if not p["code"]:
            continue
        for gi in code_map.get(p["code"], []):
            if gi in used_g:
                continue
            if similarity(_norm_text(p["name"]), _norm_text(golds[gi]["name"])) >= 0.6:
                pairs.append((pi, gi, "code", 1.0))
                used_p.add(pi)
                used_g.add(gi)
                break

    # ② 归一名称精确（同对象类优先）
    name_map: dict[str, list[int]] = defaultdict(list)
    for gi in gis:
        if gi not in used_g:
            name_map[_norm_text(golds[gi]["name"])].append(gi)
    for pi in pis:
        if pi in used_p:
            continue
        p = preds[pi]
        cands = name_map.get(_norm_text(p["name"]), [])
        if not cands:
            continue
        pc_i = _norm_text(p.get("class_name", "")) or pc
        cands = sorted(cands, key=lambda gi: 0 if _norm_text(golds[gi]["class_name"]) == pc_i else 1)
        gi = cands[0]
        pairs.append((pi, gi, "name", 1.0))
        used_p.add(pi)
        used_g.add(gi)

    # ③ 残差：语义相似 + 一对一
    rp = [i for i in pis if i not in used_p]
    rg = [j for j in gis if j not in used_g]
    suspects: list[dict] = []
    if rp and rg:
        sim_mat = [[similarity(_norm_text(preds[i]["name"]), _norm_text(golds[j]["name"]))
                    for j in rg] for i in rp]
        for a, i in enumerate(rp):
            for b, j in enumerate(rg):
                s = sim_mat[a][b]
                if SUSPECT_THRESHOLD <= s < MATCH_THRESHOLD:
                    suspects.append({"pred_index": i, "gold_index": j, "similarity": s})

        cells = len(rp) * len(rg)
        chosen: list[tuple[int, int]] = []
        if cells <= HUNGARIAN_MAX_CELLS:
            cost = [[(1 - s) if s >= MATCH_THRESHOLD else 10.0 for s in row] for row in sim_mat]
            chosen = _hungarian(cost)
            notes.append(f"残差 {len(rp)}×{len(rg)} 使用匈牙利一对一匹配")
        else:
            cand = sorted(((sim_mat[a][b], a, b) for a in range(len(rp)) for b in range(len(rg))
                           if sim_mat[a][b] >= MATCH_THRESHOLD), reverse=True)
            up, ug = set(), set()
            for _, a, b in cand:
                if a in up or b in ug:
                    continue
                chosen.append((a, b))
                up.add(a)
                ug.add(b)
            notes.append(f"残差 {len(rp)}×{len(rg)} 超过匈牙利上限，**降级为贪心匹配**（结果偏乐观，报告须标注）")

        for a, b in chosen:
            s = sim_mat[a][b]
            if s < MATCH_THRESHOLD:
                continue
            pairs.append((rp[a], rg[b], "semantic", s))

    return pairs, suspects, notes


def _align(preds: list[dict], golds: list[dict], pred_class: str = "") -> tuple[list, list, list]:
    """按对象类分组后做一对一匹配。

    为什么必须分组：测试集的属性名在多个对象类间大量重复（如「壁厚」在 4 个类里都有），
    全局名称匹配会让不同类的同名属性互相抢，导致真实匹配被挤成 missing/extra。
    """
    p_cls = [_norm_text(p.get("class_name", "")) for p in preds]
    g_cls = [_norm_text(g.get("class_name", "")) for g in golds]
    if not any(p_cls) or not any(g_cls):
        return _align_block(preds, golds, list(range(len(preds))),
                           list(range(len(golds))), pred_class)

    pairs: list[tuple[int, int, str, float]] = []
    suspects: list[dict] = []
    notes: list[str] = []
    matched = 0
    for c in sorted(set(x for x in p_cls if x) & set(x for x in g_cls if x)):
        pis = [i for i, x in enumerate(p_cls) if x == c]
        gis = [j for j, x in enumerate(g_cls) if x == c]
        pr, su, nt = _align_block(preds, golds, pis, gis, pred_class)
        pairs += pr
        suspects += su
        notes += nt
        matched += 1

    # 无类名的一侧（如模式 A 弱抽取）走全局，避免漏评
    no_cls_p = [i for i, x in enumerate(p_cls) if not x]
    no_cls_g = [j for j, x in enumerate(g_cls) if not x]
    if no_cls_p and no_cls_g:
        pr, su, nt = _align_block(preds, golds, no_cls_p, no_cls_g, pred_class)
        pairs += pr
        suspects += su
        notes += nt

    head = f"按对象类分组匹配：{matched} 个类"
    if no_cls_p and no_cls_g:
        head += f"；另有 {len(no_cls_p)}×{len(no_cls_g)} 行无类名，走全局匹配"
    return pairs, suspects, [head] + notes[:2]


# ---------------------------------------------------------------------------
# 6) 评测主流程
# ---------------------------------------------------------------------------
def _detail_row(mode: str, j: str, pred: dict | None, gold: dict | None,
                matched_by: str = "", sim: float = 0.0,
                diffs: list[str] | None = None, reason: str = "") -> dict:
    return {
        "mode": mode,
        "judgement": j,
        "gold_class": (gold or {}).get("class_name", ""),
        "gold_id": (gold or {}).get("gold_id", ""),
        "gold_name": (gold or {}).get("name", ""),
        "gold_code": (gold or {}).get("code", ""),
        "gold_type": (gold or {}).get("data_type", ""),
        "gold_constraint": (gold or {}).get("constraint", ""),
        "gold_unit": (gold or {}).get("unit", ""),
        "pred_name": (pred or {}).get("name", ""),
        "pred_class": (pred or {}).get("class_name", ""),
        "pred_code": (pred or {}).get("code", ""),
        "pred_type": (pred or {}).get("data_type", ""),
        "pred_constraint": (pred or {}).get("constraint", ""),
        "pred_unit": (pred or {}).get("unit", ""),
        "matched_by": matched_by,
        "similarity": round(sim, 4),
        "diff_fields": "|".join(diffs or []),
        "reason": reason,
    }




def _align_classes(preds: list[dict], golds: list[dict]) -> tuple[list, list]:
    """对象类对齐：编码精确 → 归一名称精确 → 语义相似。返回 (pairs, notes)。"""
    used_p: set[int] = set()
    used_g: set[int] = set()
    pairs: list[tuple[int, int, str, float]] = []

    gcode = {g["code"]: j for j, g in enumerate(golds) if g.get("code")}
    for i, p in enumerate(preds):
        c = p.get("code")
        if not c or c not in gcode:
            continue
        j = gcode[c]
        if j in used_g:
            continue
        if similarity(_norm_text(p["name"]), _norm_text(golds[j]["name"])) >= 0.6:
            pairs.append((i, j, "code", 1.0))
            used_p.add(i)
            used_g.add(j)

    gname: dict[str, list[int]] = defaultdict(list)
    for j, g in enumerate(golds):
        if j not in used_g:
            gname[_norm_text(g["name"])].append(j)
    for i, p in enumerate(preds):
        if i in used_p:
            continue
        for j in gname.get(_norm_text(p["name"]), []):
            if j in used_g:
                continue
            pairs.append((i, j, "name", 1.0))
            used_p.add(i)
            used_g.add(j)
            break

    rp = [i for i in range(len(preds)) if i not in used_p]
    rg = [j for j in range(len(golds)) if j not in used_g]
    notes: list[str] = []
    if rp and rg and len(rp) * len(rg) <= HUNGARIAN_MAX_CELLS:
        sim = [[similarity(_norm_text(preds[i]["name"]), _norm_text(golds[j]["name"]))
                for j in rg] for i in rp]
        cost = [[(1 - s) if s >= MATCH_THRESHOLD else 10.0 for s in row] for row in sim]
        for a, b in _hungarian(cost):
            if sim[a][b] >= MATCH_THRESHOLD:
                pairs.append((rp[a], rg[b], "semantic", sim[a][b]))
        notes.append(f"对象类残差 {len(rp)}×{len(rg)} 使用匈牙利匹配")
    return pairs, notes


def _eval_class_layer(preds: list[dict], golds: list[dict]) -> dict:
    pairs, notes = _align_classes(preds, golds)
    mp = {p for p, _, _, _ in pairs}
    mg = {g for _, g, _, _ in pairs}
    ec, mc, pg = len(pairs), len(golds), len(preds)
    counts = {"exact": ec, "partial": 0, "missing": mc - ec, "extra": pg - ec}

    gen = ec / pg if pg else 0.0
    cov = ec / mc if mc else 0.0
    prec = ec / (ec + counts["extra"]) if (ec + counts["extra"]) else 0.0
    f1 = 2 * prec * cov / (prec + cov) if (prec + cov) else 0.0

    details = []
    gmap = {g: (p, by, s) for p, g, by, s in pairs}
    for j, g in enumerate(golds):
        if j in gmap:
            p, by, s = gmap[j]
            details.append({
                "layer": "class", "judgement": "exact",
                "gold_class": "", "gold_id": g.get("id", ""), "gold_name": g["name"],
                "gold_code": g.get("code", ""), "gold_type": "", "gold_constraint": "",
                "gold_unit": "", "pred_name": preds[p]["name"], "pred_code": preds[p].get("code", ""),
                "pred_type": "", "pred_constraint": "", "pred_unit": "",
                "matched_by": by, "similarity": round(s, 4), "diff_fields": "", "reason": "",
            })
        else:
            details.append({
                "layer": "class", "judgement": "missing",
                "gold_class": "", "gold_id": g.get("id", ""), "gold_name": g["name"],
                "gold_code": g.get("code", ""), "gold_type": "", "gold_constraint": "",
                "gold_unit": "", "pred_name": "", "pred_code": "", "pred_type": "",
                "pred_constraint": "", "pred_unit": "", "matched_by": "", "similarity": 0,
                "diff_fields": "", "reason": "标准有、生成无",
            })
    for i, p in enumerate(preds):
        if i not in mp:
            details.append({
                "layer": "class", "judgement": "extra",
                "gold_class": "", "gold_id": "", "gold_name": "", "gold_code": "",
                "gold_type": "", "gold_constraint": "", "gold_unit": "",
                "pred_name": p["name"], "pred_code": p.get("code", ""), "pred_type": "",
                "pred_constraint": "", "pred_unit": "", "matched_by": "", "similarity": 0,
                "diff_fields": "", "reason": "生成有、标准无",
            })

    return {
        "counts": counts,
        "metrics": {
            "gen_accuracy": round(gen * 100, 2),
            "coverage": round(cov * 100, 2),
            "precision": round(prec * 100, 2),
            "recall": round(cov * 100, 2),
            "f1": round(f1 * 100, 2),
        },
        "details": details,
        "notes": notes,
    }


def _eval_attr_layer(preds: list[dict], golds: list[dict]) -> dict:
    pairs, suspects, notes = _align(preds, golds)
    counts = {"exact": 0, "partial": 0, "missing": 0, "extra": 0}
    per_field = {f: {"ok": 0, "total": 0} for f in KEY_FIELDS}
    reasons = {"漏抽": 0, "多抽": 0, "名称不一致": 0, "类型错": 0,
               "约束错": 0, "单位错": 0, "疑似粒度错": 0}
    details: list[dict] = []

    pairs_sorted = sorted(pairs, key=lambda x: x[1])
    matched_g = {gi: (pi, by, s) for pi, gi, by, s in pairs_sorted}
    matched_p = {pi for pi, _, _, _ in pairs_sorted}
    suspect_p = {x["pred_index"] for x in suspects}

    for gi, g in enumerate(golds):
        if gi not in matched_g:
            counts["missing"] += 1
            reasons["漏抽"] += 1
            d = _detail_row("", "missing", None, g, reason="标准有、生成无")
            d["layer"] = "attr"
            details.append(d)
            continue
        pi, by, s = matched_g[gi]
        p = preds[pi]
        diffs_all = [f for f in KEY_FIELDS
                     if _NORM[f](p.get(f, "")) != _NORM[f](g.get(f, ""))]
        hard = [f for f in diffs_all if f in STRICT_FIELDS]
        soft = [f for f in diffs_all if f not in STRICT_FIELDS]
        j = "exact" if not hard else "partial"
        counts[j] += 1
        for f in hard:
            reasons[{"name": "名称不一致", "data_type": "类型错", "unit": "单位错"}[f]] += 1
        for f in KEY_FIELDS:
            per_field[f]["total"] += 1
            if f not in diffs_all:
                per_field[f]["ok"] += 1
        diff_note = "、".join(FIELD_LABEL[f] for f in hard)
        if soft:
            diff_note = (diff_note + "；" if diff_note else "") + "约束口径不同（参考）"
        d = _detail_row("", j, p, g, by, s, hard + [f + "(soft)" for f in soft],
                        ("字段差异：" + diff_note) if (hard or soft) else "")
        d["layer"] = "attr"
        details.append(d)

    for pi, p in enumerate(preds):
        if pi in matched_p:
            continue
        counts["extra"] += 1
        reasons["多抽"] += 1
        reason = "生成有、标准无"
        if pi in suspect_p:
            reasons["疑似粒度错"] += 1
            reason += "（与某条 missing 相似度 0.75–0.85，疑似粒度拆分）"
        d = _detail_row("", "extra", p, None, reason=reason)
        d["layer"] = "attr"
        details.append(d)

    total, pred_total = len(golds), len(preds)
    ec, pa = counts["exact"], counts["partial"]
    strict = ec / total if total else 0.0
    loose = (ec + 0.5 * pa) / total if total else 0.0
    gen = ec / pred_total if pred_total else 0.0
    prec = ec / (ec + counts["extra"]) if (ec + counts["extra"]) else 0.0
    rec = ec / (ec + counts["missing"]) if (ec + counts["missing"]) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    f_total = sum(per_field[f]["total"] for f in STRICT_FIELDS)
    f_ok = sum(per_field[f]["ok"] for f in STRICT_FIELDS)
    field_acc = f_ok / f_total if f_total else 0.0

    by_class: dict[str, dict] = defaultdict(lambda: {"gold": 0, "exact": 0, "partial": 0, "missing": 0})
    for d in details:
        if d["judgement"] == "extra":
            continue
        c = d.get("gold_class") or "（未标注）"
        by_class[c]["gold"] += 1
        by_class[c][d["judgement"]] += 1
    per_class = [{
        "class_name": c, "gold": v["gold"], "exact": v["exact"], "partial": v["partial"],
        "missing": v["missing"],
        "strict": round(v["exact"] / v["gold"] * 100, 1) if v["gold"] else 0.0,
    } for c, v in sorted(by_class.items(), key=lambda x: -x[1]["gold"])]
    macro = round(sum(x["strict"] for x in per_class) / len(per_class), 2) if per_class else 0.0

    return {
        "counts": counts,
        "metrics": {
            "gen_accuracy": round(gen * 100, 2),
            "coverage": round(strict * 100, 2),
            "precision": round(prec * 100, 2),
            "recall": round(rec * 100, 2),
            "f1": round(f1 * 100, 2),
            "field_accuracy": round(field_acc * 100, 2),
            "macro_strict": macro,
            "loose_coverage": round(loose * 100, 2),
        },
        "per_field": {f: {"label": FIELD_LABEL[f], "ok": v["ok"], "total": v["total"],
                          "accuracy": round(v["ok"] / v["total"] * 100, 2) if v["total"] else 0.0}
                      for f, v in per_field.items()},
        "per_class": per_class,
        "reasons": reasons,
        "suspects": len(suspects),
        "details": details,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 5.5) 关系层（subClassOf / hasAttribute）
# ---------------------------------------------------------------------------
REL_TYPES = ("subClassOf", "hasAttribute")
REL_LABEL = {"subClassOf": "上下位（subClassOf）", "hasAttribute": "归属（hasAttribute）"}


def _rel_key(rel: dict) -> tuple[str, str, str]:
    from tools import text_norm as tn
    return (rel.get("type", ""),
            tn.canonical_name(rel.get("head") or rel.get("source") or ""),
            tn.canonical_name(rel.get("tail") or rel.get("target") or ""))


def gold_relations(classes: list[dict], attrs: list[dict]) -> dict:
    """从 D3 预期输出推导 gold 关系（不额外依赖任何标注文件）。

    - subClassOf：对象类表「父类ID」→ 父类名称（父类不在表内则丢弃并计数）
    - hasAttribute：属性行的 (对象类, 属性) 归属
    """
    id2name = {str(c.get("id") or "").strip(): (c.get("name") or "").strip()
               for c in classes if c.get("id")}
    rels, seen = [], set()
    orphan_parent = 0
    for c in classes:
        pid = str(c.get("parent_id") or "").strip()
        nm = (c.get("name") or "").strip()
        if not pid or not nm:
            continue
        sup = id2name.get(pid)
        if not sup:
            orphan_parent += 1
            continue
        k = ("subClassOf", sup, nm)
        if k in seen:
            continue
        seen.add(k)
        rels.append({"type": "subClassOf", "head": sup, "tail": nm})
    for a in attrs:
        cn = (a.get("class_name") or "").strip()
        an = (a.get("name") or "").strip()
        if not cn or not an:
            continue
        k = ("hasAttribute", cn, an)
        if k in seen:
            continue
        seen.add(k)
        rels.append({"type": "hasAttribute", "head": cn, "tail": an})
    return {"relations": rels, "orphan_parent": orphan_parent,
            "counts": dict(Counter(r["type"] for r in rels))}


def pred_relations(classes: list[dict], attrs: list[dict],
                   rels: list[dict] | None = None) -> dict:
    """生成侧关系：优先用产物自带的 relations；没有则从 classes/attrs 兜底推导。"""
    src = "product"
    if not rels:
        src = "derived"
        rels = []
        for c in classes:
            p = (c.get("parent") or c.get("parent_name") or "").strip()
            if p and c.get("name"):
                rels.append({"type": "subClassOf", "head": p, "tail": c["name"]})
        for a in attrs:
            cn = (a.get("class_name") or "").strip()
            an = (a.get("name") or "").strip()
            if cn and an:
                rels.append({"type": "hasAttribute", "head": cn, "tail": an})
    else:
        rels = [{"type": r.get("type", ""),
                 "head": r.get("head") or r.get("source") or "",
                 "tail": r.get("tail") or r.get("target") or ""} for r in rels]
    seen, out = set(), []
    for r in rels:
        k = _rel_key(r)
        if k[1] and k[2] and k not in seen:
            seen.add(k)
            out.append(r)
    return {"relations": out, "origin": src,
            "counts": dict(Counter(r["type"] for r in out))}


def _eval_relation_layer(pred: list[dict], gold: list[dict]) -> dict:
    """关系层评测：按 (类型, 头, 尾) 三元组集合比对。

    gen_accuracy = 命中 / 生成关系数（生成的关系有多少是对的）
    coverage     = 命中 / 预期关系数（答案的关系被找回了多少）
    """
    pk = {_rel_key(r) for r in pred}
    gk = {_rel_key(r) for r in gold}
    exact = len(pk & gk)
    pp, pg = len(pk), len(gk)
    prec = exact / pp if pp else 0.0
    rec = exact / pg if pg else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    per_type = {}
    for t in REL_TYPES:
        ps = {k for k in pk if k[0] == t}
        gs = {k for k in gk if k[0] == t}
        e = len(ps & gs)
        per_type[t] = {
            "label": REL_LABEL[t], "pred": len(ps), "gold": len(gs), "exact": e,
            "missing": len(gs - ps), "extra": len(ps - gs),
            "gen_accuracy": round(e / len(ps) * 100, 2) if ps else None,
            "coverage": round(e / len(gs) * 100, 2) if gs else None,
        }
    # 明细样本：漏掉的与多余的（各取前 40 条，够看模式）
    miss = [{"type": k[0], "head": k[1], "tail": k[2]} for k in sorted(gk - pk)][:40]
    extra = [{"type": k[0], "head": k[1], "tail": k[2]} for k in sorted(pk - gk)][:40]

    return {
        "counts": {"exact": exact, "missing": pg - exact, "extra": pp - exact,
                   "pred_total": pp, "gold_total": pg},
        "metrics": {"gen_accuracy": round(prec * 100, 2),
                    "coverage": round(rec * 100, 2),
                    "precision": round(prec * 100, 2),
                    "recall": round(rec * 100, 2),
                    "f1": round(f1 * 100, 2)},
        "per_type": per_type,
        "missing_samples": miss,
        "extra_samples": extra,
    }


# ---------------------------------------------------------------------------
# 6) 评测主流程
# ---------------------------------------------------------------------------
def evaluate(mode: str, group: str | None = None, pred: dict | list | None = None,
             standard_id: str | None = None, persist: bool = True,
             scope_to_class: bool | None = None,
             pred_source: str = "standard") -> dict:
    """按 (mode, group) 评测：生成侧取该组 `输入/`，答案侧取该组 `预期输出/`。"""
    ds = registry.resolve(mode, group)
    m, g = ds["mode"], ds["group"]

    # ---- P7 硬门槛：D3 必须由用户提供（上传或显式采用内置）----
    # off / 未选择 一律拒绝评测，且不产出任何指标 —— 不能拿内置答案悄悄打分。
    d3_slot = ds.get("gold_slot")
    d3_decision = None
    if d3_slot:
        try:
            from tools import upload_manager as um
            d3_decision = um.decision(d3_slot)
        except Exception:
            d3_decision = None
    if d3_slot and d3_decision not in ("uploaded", "builtin"):
        empty_metric = {"gen_accuracy": 0.0, "coverage": 0.0, "precision": 0.0,
                        "recall": 0.0, "f1": 0.0, "field_accuracy": 0.0,
                        "macro_strict": 0.0, "loose_coverage": 0.0}
        reason = ("该槽位已关闭（本轮不参与）" if d3_decision == "off"
                  else "该槽位未上传、也未选择采用内置数据")
        return {
            "mode": m, "group": g, "group_label": ds["label"],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "evaluable": False, "has_gold": False,
            "unevaluable_reason": "no_gold_provided",
            "pred_source": pred_source,
            "pred_input_note": f"未提供预期输出（D3）：{reason}，无法评测。请先在 P0 上传 D3 或将其设为「采用内置数据」。",
            "pred_input_files": [], "pred_input_quality": "missing",
            "gold_source": [], "gold_dir": "", "gold_uploaded": False,
            "gold_counts": {"classes": 0, "attrs": 0, "attr_dict": 0},
            "pred_counts": {"classes": 0, "attrs": 0},
            "overall": {"pred_total": 0, "gold_total": 0, "exact": 0,
                        "gen_accuracy": 0.0, "coverage": 0.0},
            "class_layer": None, "attr_layer": None, "relation_layer": None,
            "counts": {"exact": 0, "partial": 0, "missing": 0, "extra": 0},
            "metrics": empty_metric, "per_field": {}, "per_class": [], "reasons": {},
            "details": [], "detail_total": 0,
            "notes": [f"D3 槽位 {d3_slot} 的当前状态：{d3_decision or '未选择'}。"
                      "P7 不再回落到内置预期输出。"],
            "standard_id": standard_id or "",
        }

    gold = registry.load_gold_tables(m, g)
    gold_classes, gold_attrs = gold["classes"], gold["attrs"]

    if pred is None:
        if pred_source == "standard":
            sp = load_pred(m, standard_id)
            pred_doc = {"classes": sp.get("classes", []), "attrs": sp.get("rows", []),
                        "relations": sp.get("relations"),
                        "input_files": [], "input_quality": "ready",
                        "input_note": "P6 当前数据集产物（对照口径，不得当验收数字）",
                        "path": sp.get("path", "")}
        else:
            pred_doc = ep.build_pred(m, g)
    elif isinstance(pred, list):
        pred_doc = {"classes": [], "attrs": pred, "input_files": [],
                    "input_quality": "ready", "input_note": "调用方直接传入"}
    else:
        pred_doc = pred

    pred_classes = pred_doc.get("classes") or []
    pred_attrs = pred_doc.get("attrs") or []
    has_gold = bool(gold_classes or gold_attrs)
    has_pred = bool(pred_classes or pred_attrs)
    # 预期输出（D3）是可选资源：没有它只是不评分，不影响生成
    evaluable = has_gold and has_pred
    unevaluable_reason = "" if evaluable else ("no_gold" if not has_gold else "no_pred")

    class_layer = _eval_class_layer(pred_classes, gold_classes) if gold_classes else None
    attr_layer = _eval_attr_layer(pred_attrs, gold_attrs) if gold_attrs else None

    # 关系层（2026-10-23 新增）：3.1 指标原来止步于对象类层/属性层，关系没有指标
    g_rel = gold_relations(gold_classes, gold_attrs)
    p_rel = pred_relations(pred_classes, pred_attrs, pred_doc.get("relations"))
    relation_layer = (_eval_relation_layer(p_rel["relations"], g_rel["relations"])
                      if g_rel["relations"] or p_rel["relations"] else None)
    if relation_layer:
        relation_layer["pred_origin"] = p_rel["origin"]
        relation_layer["pred_counts"] = p_rel["counts"]
        relation_layer["gold_counts"] = g_rel["counts"]
        relation_layer["gold_orphan_parent"] = g_rel["orphan_parent"]

    te = (class_layer["counts"]["exact"] if class_layer else 0) + \
         (attr_layer["counts"]["exact"] if attr_layer else 0)
    tp = len(pred_classes) + len(pred_attrs)
    tg = len(gold_classes) + len(gold_attrs)
    overall = {
        "pred_total": tp, "gold_total": tg, "exact": te,
        "gen_accuracy": round(te / tp * 100, 2) if tp else 0.0,
        "coverage": round(te / tg * 100, 2) if tg else 0.0,
    }
    if relation_layer:
        overall["relation"] = {
            "pred_total": relation_layer["counts"]["pred_total"],
            "gold_total": relation_layer["counts"]["gold_total"],
            "exact": relation_layer["counts"]["exact"],
            "gen_accuracy": relation_layer["metrics"]["gen_accuracy"],
            "coverage": relation_layer["metrics"]["coverage"],
        }

    details = []
    if class_layer:
        details += class_layer["details"]
    if attr_layer:
        details += attr_layer["details"]

    notes: list[str] = []
    if not has_gold:
        notes.append("未提供预期输出（D3 为可选）：本次只生成、不评分；产物仍可导出，如需准确率请补齐预期输出后重跑")
    elif not has_pred:
        notes.append("生成侧 0 行：输入不可解析或未生成任何标准，本次无法计算准确率（这不是 0% 准确率）")
    if pred_doc.get("input_note"):
        notes.append(pred_doc["input_note"])
    if attr_layer:
        notes += attr_layer.get("notes") or []
    if has_gold:
        notes += [
            "对象类层只比名称（编码由系统按规则生成，单列展示，不计入对错）",
            "属性层 exact = 名称 + 数据类型 + 单位一致；`constraint` 在需求侧是业务规则、答案侧是技术格式，只作参考",
            "三个层分别报「生成准确率（分母=生成数）」与「覆盖率（分母=预期输出数）」",
        ]
        if relation_layer:
            notes.append(
                "关系层按 (类型, 头, 尾) 三元组比对，只算 subClassOf 与 hasAttribute；"
                "associates 属线索型关系，不参与准确率")
            if relation_layer["pred_origin"] == "derived":
                notes.append("生成侧未带 relations 字段（旧产物），关系由 classes/attrs 兜底推导")
        if not gold_attrs:
            notes.append("该组预期输出只有对象类表，属性层不适用（N/A）")

    # 去重保序：对象类残差与属性残差规模相同时（如都是 2×1），
    # 两层各自的匹配说明会一字不差地重复出现
    notes = list(dict.fromkeys(notes))

    empty_metrics = {"gen_accuracy": 0.0, "coverage": 0.0, "precision": 0.0,
                     "recall": 0.0, "f1": 0.0, "field_accuracy": 0.0,
                     "macro_strict": 0.0, "loose_coverage": 0.0}
    result = {
        "mode": m,
        "group": g,
        "group_label": ds["label"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "evaluable": evaluable,
        "has_gold": has_gold,
        "unevaluable_reason": unevaluable_reason,
        "pred_source": pred_source,
        "pred_input_files": pred_doc.get("input_files", []),
        "pred_input_quality": pred_doc.get("input_quality", "ready"),
        "pred_input_note": pred_doc.get("input_note", ""),
        "gold_source": [str(f).replace("\\", "/") for f in ds["gold_files"]],
        "gold_dir": str(ds["gold_dir"]).replace("\\", "/"),
        "gold_uploaded": False,
        "gold_counts": gold["counts"],
        "pred_counts": {"classes": len(pred_classes), "attrs": len(pred_attrs)},
        "overall": overall,
        "class_layer": class_layer,
        "attr_layer": attr_layer,
        "relation_layer": relation_layer,
        "details": details,
        "detail_total": len(details),
        "notes": notes,
    }

    # 兼容旧前端字段：顶层 metrics/counts 指向属性层（无属性层时给零值）
    result["counts"] = (attr_layer["counts"] if attr_layer
                        else {"exact": 0, "partial": 0,
                              "missing": len(gold_attrs), "extra": len(pred_attrs)})
    result["metrics"] = (attr_layer["metrics"] if attr_layer else empty_metrics)
    result["per_field"] = attr_layer["per_field"] if attr_layer else {}
    result["per_class"] = attr_layer["per_class"] if attr_layer else []
    result["reasons"] = attr_layer["reasons"] if attr_layer else {}
    result["standard_id"] = pred_doc.get("standard_id", "")

    if persist:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        report_p, csv_p = _write_outputs(result)
        result["report_path"] = str(report_p).replace("\\", "/")
        result["csv_path"] = str(csv_p).replace("\\", "/")
        (EVAL_DIR / f"latest-{m}-{g}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    # 「对照 P6 产物」时判断是否是同一批输入生成的：能确认才提示，不猜
    if pred_source == "standard" and pred is None:
        try:
            cur_files = sorted(str(f).replace("\\", "/").split("/")[-1]
                               for f in (ds.get("input_files") or []))
            std_files = sorted((sp.get("scope") or {}).get("input_files") or [])
            result["pred_same_dataset"] = (set(std_files) == set(cur_files)) if std_files else None
            result["pred_dataset_files"] = {"standard": std_files, "current": cur_files}
        except Exception:
            result["pred_same_dataset"] = None

    return result


# ---------------------------------------------------------------------------
# 7) 落盘：Markdown 报告 + CSV 明细
# ---------------------------------------------------------------------------
CSV_COLS = ["mode", "group", "layer", "judgement", "gold_class", "gold_id", "gold_name",
            "gold_code", "gold_type", "gold_constraint", "gold_unit", "pred_name",
            "pred_code", "pred_type", "pred_constraint", "pred_unit", "matched_by",
            "similarity", "diff_fields", "reason"]


def _write_outputs(r: dict) -> tuple[Path, Path]:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    csv_p = EVAL_DIR / "match_detail.csv"
    report_p = EVAL_DIR / "test_report.md"

    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for d in (r["details"] if r.get("evaluable", True) else []):
            d = dict(d)
            d["mode"] = r["mode"]
            d["group"] = r["group"]
            w.writerow(d)

    c_at = r.get("attr_layer") or {}
    c_cl = r.get("class_layer") or {}
    ov = r.get("overall") or {}
    gc = r.get("gold_counts") or {}

    L = []
    L.append(f"# 数据标准生成准确率报告 · 模式 {r['mode']} / {r['group']}")
    L.append("")
    L.append(f"- 生成时间：{r['generated_at']}")
    L.append(f"- 数据集：{r.get('group_label', '')}")
    L.append(f"- **生成输入（输入/）**：{'、'.join(r.get('pred_input_files') or []) or '—'}"
             f"（{r.get('pred_input_quality')}）")
    if r.get("pred_input_note"):
        L.append(f"- 输入说明：{r['pred_input_note']}")
    L.append(f"- **评测答案（预期输出/）**：{'、'.join(os.path.basename(x) for x in (r.get('gold_source') or [])) or '—'}")
    L.append(f"- 规模：预期输出 {gc.get('classes', 0)} 类 / {gc.get('attrs', 0)} 属性"
             f"；生成 {r['pred_counts']['classes']} 类 / {r['pred_counts']['attrs']} 属性")
    L.append("")

    if not r.get("evaluable", True):
        if r.get("unevaluable_reason") == "no_gold":
            L.append("> **本次不评分**：未提供预期输出（D3 为可选）。已完成生成，产物可直接导出；"
                     "如需准确率，补齐预期输出后重跑。")
        else:
            L.append("> **本次不可评测**：生成侧 0 行，准确率不适用（不是 0%）。请检查输入是否可解析。")
        L.append("")

    L.append("## 总览")
    L.append("")
    L.append("| 层 | 生成数 | 预期数 | exact | 生成准确率 | 覆盖率 |")
    L.append("|---|---|---|---|---|---|")
    for name, layer, key in (("对象类", c_cl, "class"), ("对象类属性", c_at, "attr")):
        if not layer:
            L.append(f"| {name} | — | — | — | N/A | N/A |")
            continue
        L.append(f"| {name} | {layer['counts']['exact'] + layer['counts']['partial'] + layer['counts']['extra']}"
                 f" | {layer['counts']['exact'] + layer['counts']['partial'] + layer['counts']['missing']}"
                 f" | {layer['counts']['exact']}"
                 f" | {layer['metrics']['gen_accuracy']}% | {layer['metrics']['coverage']}% |")
    L.append(f"| **合计** | {ov.get('pred_total', 0)} | {ov.get('gold_total', 0)} | {ov.get('exact', 0)}"
             f" | **{ov.get('gen_accuracy', 0)}%** | {ov.get('coverage', 0)}% |")
    # 关系层单独一行：关系条数与「类/属性」不同量纲，不并入合计
    c_rl = r.get("relation_layer")
    if c_rl:
        L.append(f"| 关系（subClassOf + hasAttribute） | {c_rl['counts']['pred_total']}"
                 f" | {c_rl['counts']['gold_total']} | {c_rl['counts']['exact']}"
                 f" | {c_rl['metrics']['gen_accuracy']}% | {c_rl['metrics']['coverage']}% |")
    L.append("")

    if c_cl:
        L.append("## 对象类层")
        L.append("")
        L.append(f"判定：exact {c_cl['counts']['exact']} · missing {c_cl['counts']['missing']}"
                 f" · extra {c_cl['counts']['extra']}")
        L.append("")
        L.append(f"- 生成准确率 **{c_cl['metrics']['gen_accuracy']}%**（exact / 生成类数）")
        L.append(f"- 覆盖率 {c_cl['metrics']['coverage']}%（exact / 预期类数）")
        L.append(f"- Precision {c_cl['metrics']['precision']}% · Recall {c_cl['metrics']['recall']}%"
                 f" · F1 {c_cl['metrics']['f1']}%")
        L.append("")

    if c_at:
        m, c = c_at["metrics"], c_at["counts"]
        L.append("## 对象类属性层")
        L.append("")
        L.append("| 指标 | 数值 | 口径 |")
        L.append("|---|---|---|")
        L.append(f"| **生成准确率（严格）** | **{m['gen_accuracy']}%** | exact / 生成属性数 |")
        L.append(f"| 标准覆盖率（严格） | {m['coverage']}% | exact / 预期属性数 |")
        L.append(f"| 覆盖率（宽松） | {m['loose_coverage']}% | (exact+0.5×partial) / 预期属性数 |")
        L.append(f"| Precision | {m['precision']}% | exact / (exact + extra) |")
        L.append(f"| Recall | {m['recall']}% | exact / (exact + missing) |")
        L.append(f"| F1 | {m['f1']}% | 严格口径 |")
        L.append(f"| 字段级准确率 | {m['field_accuracy']}% | 已对齐行 × 3 个硬字段（名称/类型/单位）|")
        L.append(f"| macro（按对象类） | {m['macro_strict']}% | 各类严格覆盖率的算术平均 |")
        L.append("")
        L.append(f"判定分布：exact {c['exact']} · partial {c['partial']}"
                 f" · missing {c['missing']} · extra {c['extra']}")
        L.append("")
        L.append("### 字段级")
        L.append("")
        L.append("| 字段 | 一致 | 应有 | 准确率 |")
        L.append("|---|---|---|---|")
        for _f, v in (c_at.get("per_field") or {}).items():
            L.append(f"| {v['label']} | {v['ok']} | {v['total']} | {v['accuracy']}% |")
        L.append("")
        L.append("> 「约束（参考）」在需求侧是业务规则文本、答案侧是技术格式（如 VARCHAR(256)），"
                 "口径不同，不计入 exact 与字段级准确率。")
        L.append("")
        L.append("### 分对象类")
        L.append("")
        L.append("| 对象类 | 预期行 | exact | partial | missing | 严格覆盖率 |")
        L.append("|---|---|---|---|---|---|")
        for x in (c_at.get("per_class") or []):
            L.append(f"| {x['class_name']} | {x['gold']} | {x['exact']} | {x['partial']}"
                     f" | {x['missing']} | {x['strict']}% |")
        L.append("")
        L.append("### 错误归因")
        L.append("")
        for k, v in (c_at.get("reasons") or {}).items():
            L.append(f"- {k}：{v}")
        L.append("")

    if c_rl:
        L.append("## 关系层")
        L.append("")
        L.append(f"生成侧关系来源：{'产物自带 relations' if c_rl.get('pred_origin') == 'product' else '由 classes/attrs 兜底推导'}；"
                 f"比对象为 (关系类型, 头实体, 尾实体) 三元组。")
        L.append("")
        L.append("| 关系类型 | 生成数 | 预期数 | 命中 | 漏 | 多 | 生成准确率 | 覆盖率 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for _t, v in (c_rl.get("per_type") or {}).items():
            ga = "N/A" if v["gen_accuracy"] is None else f"{v['gen_accuracy']}%"
            cv = "N/A" if v["coverage"] is None else f"{v['coverage']}%"
            L.append(f"| {v['label']} | {v['pred']} | {v['gold']} | {v['exact']}"
                     f" | {v['missing']} | {v['extra']} | {ga} | {cv} |")
        L.append("")
        if c_rl.get("missing_samples"):
            L.append("漏抽样例（预期有、生成无）：")
            L.append("")
            for x in c_rl["missing_samples"][:10]:
                L.append(f"- `{x['type']}` {x['head']} → {x['tail']}")
            L.append("")
        if c_rl.get("extra_samples"):
            L.append("多出样例（生成有、预期无）：")
            L.append("")
            for x in c_rl["extra_samples"][:10]:
                L.append(f"- `{x['type']}` {x['head']} → {x['tail']}")
            L.append("")

    L.append("## 口径与限制")
    L.append("")
    for n in r["notes"]:
        L.append(f"- {n}")
    L.append("")
    L.append(f"> 逐行明细见 `{csv_p.name}`（{r['detail_total']} 行，UTF-8-SIG）。")
    L.append("> 预期输出只用于评测，不得回流到图谱 / RAG / 缓存 / few-shot。")
    report_p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return report_p, csv_p


# ---------------------------------------------------------------------------
# 8) CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="按 (mode, group) 评测数据标准生成准确率")
    ap.add_argument("mode", nargs="?", default="B", choices=["A", "B", "a", "b"])
    ap.add_argument("--group", default=None)
    ap.add_argument("--standard-id", default=None)
    args = ap.parse_args()
    res = evaluate(args.mode.upper(), args.group, standard_id=args.standard_id)
    print(f"[{res['mode']}/{res['group']}] {res['group_label']}")
    print(f"  evaluable={res['evaluable']} | 输入={res['pred_input_files']} ({res['pred_input_quality']})")
    print(f"  规模: 预期 {res['gold_counts']} | 生成 {res['pred_counts']}")
    for name in ("class_layer", "attr_layer"):
        l = res.get(name)
        if l:
            print(f"  {name}: {l['counts']} | gen={l['metrics']['gen_accuracy']}% cov={l['metrics']['coverage']}%")
        else:
            print(f"  {name}: N/A")
    print(f"  overall: {res['overall']}")
    print(f"  报告：{res.get('report_path')}")
    print(f"  明细：{res.get('csv_path')}")
