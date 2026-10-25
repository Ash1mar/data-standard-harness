"""
standard_library.py
===================
D0 · 已有标准库（存量标准）——跨模式共用的一份常驻底座。

与 D1–D8 的区别：
- D1–D8 是「本轮数据」，每轮可换；D0 是长期资产，上传一次反复复用。
- D3 预期输出是**评测答案**，绝不能当库用；D0 必须与 D3 零交集（上传时校验）。

对外只暴露一个和 `landscape_analyst.load_mock_library` 同构的字典，
让 P4 / P5 / P6 的消费端感觉不到差别：{classes, field_index, conflicts, ...}。

目录：
    data/library/
    ├── uploaded/        每次上传的原始 xlsx（只增不改）
    ├── merged.json      规范化索引（类 → 属性）
    ├── manifest.json    版本/批次/来源/哈希
    └── change_log.jsonl 每次入库与修改的审计流水
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
DIR = DATA_ROOT / "library"
UPLOAD_DIR = DIR / "uploaded"
STAGING_DIR = DIR / "_staging"          # 派生副本（暂存属性表）落这里，绝不回写原文件
MERGED = DIR / "merged.json"
MANIFEST = DIR / "manifest.json"
CHANGELOG = DIR / "change_log.jsonl"

_INT_FIELDS = ("data_type", "constraint", "unit", "definition", "code")

# ---------------------------------------------------------------------------
# 状态口径（2026-10-24 用户确认）
# ---------------------------------------------------------------------------
# ① 「状态」列实际取值只有三种：有效 / 暂定 / 删除；
# ② 空值一律计入「暂定」（不确定 = 暂定，不当有效也不当删除）；
# ③ 双表形态下，去重出来的属性池条目**一律算有效**（属性本身没有归属，
#    它在某个对象类下被标记删除，不代表这个属性从属性池里消失）。
ST_VALID = "valid"
ST_PENDING = "pending"
ST_DELETED = "deleted"
_STATUS_BUCKETS = (ST_VALID, ST_PENDING, ST_DELETED)
_STATUS_LABEL = {ST_VALID: "有效", ST_PENDING: "暂定", ST_DELETED: "删除"}

# 双表形态 → 暂存属性表时删掉的列（只作用在副本上，原表一列不动）
STAGING_DROP_COLS = ("对象类编码", "对象类ID", "对象类中文名称", "确权平台", "所属专业")


# ---------------------------------------------------------------------------
# 读盘 / 写盘
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty() -> dict:
    return {"classes": {}, "attrs": []}


def _read_json(p: Path, default):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def manifest() -> dict:
    return _read_json(MANIFEST, {"version": 0, "batches": [], "updated_at": "",
                                 "source": "builtin", "files": []})


def _bump_manifest(patch: dict) -> dict:
    m = manifest()
    m.update(patch)
    m["version"] = int(m.get("version", 0)) + 1
    m["updated_at"] = _now()
    _write_json(MANIFEST, m)
    return m


def _log(entry: dict) -> None:
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    with CHANGELOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **entry}, ensure_ascii=False) + "\n")


def _invalidate_cache() -> None:
    """库变了要让 P4/P5/P6 的进程内缓存失效。"""
    try:
        _CACHE.clear()
    except NameError:
        pass
    try:
        from tools import landscape_analyst as la
        la._CACHE.pop("lib:A", None)
        la._CACHE.pop("lib:B", None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 状态与计数口径
# ---------------------------------------------------------------------------
def _norm_status(raw) -> str:
    """状态归一：有效 / 暂定 / 删除。空值 → 暂定。"""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip().replace(" ", "")
    if not s:
        return ST_PENDING
    if any(k in s for k in ("删除", "作废", "废弃", "失效", "停用")):
        return ST_DELETED
    if any(k in s for k in ("暂定", "待定", "草稿", "待审", "待确认")):
        return ST_PENDING
    if any(k in s for k in ("有效", "启用", "在用")):
        return ST_VALID
    return ST_PENDING


def _tally(statuses) -> dict:
    out = {"total": 0, ST_VALID: 0, ST_PENDING: 0, ST_DELETED: 0}
    for s in statuses:
        out["total"] += 1
        out[s if s in _STATUS_BUCKETS else ST_PENDING] += 1
    return out


def _mk_counts(n: int, *, valid_only: bool = False) -> dict:
    """没有状态列时的回落：全部计入有效。"""
    return {"total": n, ST_VALID: n if valid_only or n else 0,
            ST_PENDING: 0 if valid_only or n else 0, ST_DELETED: 0}


def _pool_key(attr_id: str, attr_name: str) -> tuple:
    """属性池去重键：**只看 属性ID**。

    属性是公共池，没有归属关系——同一个属性被 20 个对象类取用，池子里仍然只有 1 个。
    属性ID 为空时退回 属性中文名称。
    """
    aid = (attr_id or "").strip()
    return ("id", aid) if aid else ("name", (attr_name or "").strip())


def _cell(r, i: int) -> str:
    return str(r[i]).strip() if i >= 0 and i < len(r) and r[i] is not None else ""


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(s or "")).strip()[:80] or "标准库"


# ---------------------------------------------------------------------------
# 只读扫描：一份 xlsx 是「三表」还是「双表」，各自该报多少行
# ---------------------------------------------------------------------------
def scan_sheets(path: Path) -> dict:
    """**只读**统计三张表的行数口径，不改文件、不写库。

    三表形态（对象类 / 对象类属性 / 属性）：
        对象类数     = 对象类表数据行数
        对象类属性数 = 对象类属性表数据行数（原始行，**不去重**）
        属性数       = 属性表数据行数
    双表形态（对象类 / 对象类属性）：
        对象类数     = 对象类表数据行数
        对象类属性数 = 对象类属性表数据行数（原始行，**不去重**）
        属性数       = 对象类属性表按 (对象类ID, 属性ID) 去重后的条数

    三种计数都按「状态」列拆成 有效 / 暂定 / 删除。双表形态的属性池一律算有效。
    """
    import openpyxl
    from tools.evaluator import _col, _hdr

    out = {
        "file": path.name,
        "shape": "unknown",
        "sheets": [],
        "counts": {"classes": _mk_counts(0), "class_attrs": _mk_counts(0), "attrs": _mk_counts(0)},
        "dedupe": {"raw_rows": 0, "unique_rows": 0, "collapsed": 0},
        "pool": {"size": 0, "by": "", "mixed": False},
        "skipped_rows": 0,
        "has_status_col": False,
    }

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
        out["sheets"] = names
        has_cls = "对象类" in names
        has_ca = "对象类属性" in names
        has_attr = "属性" in names
        out["shape"] = "three" if (has_cls and has_ca and has_attr) else (
            "two" if (has_cls and has_ca) else ("partial" if has_ca else "unknown"))

        # ① 对象类表
        if has_cls:
            rows = list(wb["对象类"].iter_rows(values_only=True))
            if rows:
                h = _hdr(rows)
                c_nm = _col(h, "对象类名称", "对象类中文名称", "中文名称")
                c_st = _col(h, "状态")
                out["has_status_col"] = out["has_status_col"] or c_st >= 0
                st = []
                for r in rows[1:]:
                    if not _cell(r, c_nm):
                        continue
                    st.append(_norm_status(_cell(r, c_st)))
                out["counts"]["classes"] = _tally(st)

        # ② 对象类属性表（原始行数 —— 同一属性出现在不同对象类下就是不同行）
        ca_rows: list[dict] = []
        if has_ca:
            rows = list(wb["对象类属性"].iter_rows(values_only=True))
            if rows:
                h = _hdr(rows)
                a_cid = _col(h, "对象类ID")
                a_cn = _col(h, "对象类中文名称", "对象类名称", "中文名称")
                a_aid = _col(h, "属性ID")
                a_nm = _col(h, "属性中文名称", "属性名称")
                a_st = _col(h, "状态")
                out["has_status_col"] = out["has_status_col"] or a_st >= 0
                st = []
                for r in rows[1:]:
                    if not _cell(r, a_nm):
                        out["skipped_rows"] += 1
                        continue
                    st.append(_norm_status(_cell(r, a_st)))
                    ca_rows.append({"class_id": _cell(r, a_cid),
                                    "class_name": _cell(r, a_cn),
                                    "attr_id": _cell(r, a_aid),
                                    "attr_name": _cell(r, a_nm)})
                out["counts"]["class_attrs"] = _tally(st)

        # ③ 属性数量
        if has_attr and out["shape"] == "three":
            rows = list(wb["属性"].iter_rows(values_only=True))
            if rows:
                h = _hdr(rows)
                b_nm = _col(h, "属性中文名称", "属性名称", "中文名称")
                b_st = _col(h, "状态")
                out["has_status_col"] = out["has_status_col"] or b_st >= 0
                st = []
                for r in rows[1:]:
                    if not _cell(r, b_nm):
                        out["skipped_rows"] += 1
                        continue
                    st.append(_norm_status(_cell(r, b_st)))
                out["counts"]["attrs"] = _tally(st)
                out["pool"] = {"size": out["counts"]["attrs"]["total"], "by": "属性表行数",
                               "mixed": False, "note": "三表形态：属性数即属性表行数"}
        else:
            # 双表：属性池 = 按 属性ID 去重。属性是公共池、无归属，
            # 同一属性被多个对象类取用只算 1 个（若按 类ID+属性ID 去重会得到 16 倍虚高）。
            pool = _pool_of(ca_rows)
            n = pool["size"]
            out["counts"]["attrs"] = _mk_counts(n, valid_only=True)
            out["dedupe"] = {"raw_rows": len(ca_rows), "unique_rows": n,
                             "collapsed": len(ca_rows) - n}
            out["pool"] = pool
    finally:
        wb.close()
    return out


def _pool_of(ca_rows: list[dict] | None) -> dict:
    """属性池的真实规模（**跨对象类**按属性ID去重）——与「去重行数」是两个概念：

    - 去重行数（按 对象类ID+属性ID）= 暂存表有多少行，这是界面上显示的「属性」数；
    - 池子大小（只看属性ID，忽略对象类）= 真正有多少个不重复的属性。

    两者在真实数据里可能不等（同一属性被多个对象类取用时，行数 > 池子数）。
    """
    if ca_rows is None:
        return {"size": 0, "by": "", "note": "三表形态：属性数即属性表行数"}
    ids, names = set(), set()
    for x in ca_rows:
        aid = (x.get("attr_id") or "").strip()
        if aid:
            ids.add(aid)
        nm = (x.get("attr_name") or "").strip()
        if nm:
            names.add(nm)
    if ids:
        used = len(ids)
        by = "属性ID"
        mixed = bool(names) and used != len(names)
    else:
        used = len(names)
        by = "属性中文名称"
        mixed = False
    return {"size": used, "by": by, "mixed": mixed}


# ---------------------------------------------------------------------------
# 双表形态 → 另存一份「属性（暂存）」表
# ---------------------------------------------------------------------------
def build_staging_attrs(path: Path) -> dict:
    """把「对象类属性」去重后**另存**成属性池副本。

    铁律：原上传文件与它的「对象类属性」sheet **一个列都不删、一行都不改**；
    删列（对象类编码 / 对象类ID / 对象类中文名称 / 确权平台 / 所属专业）
    只作用于写出去的这份副本。`属性ID` 保留——它是属性池与对象类属性之间的
    唯一关联键（属性没有归属关系，不存 owner key）。
    """
    import openpyxl
    from tools.evaluator import _col, _hdr

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if "对象类属性" not in wb.sheetnames:
            return {"ok": False, "reason": "没有『对象类属性』表"}
        rows = list(wb["对象类属性"].iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return {"ok": False, "reason": "『对象类属性』表为空"}

    hdr = _hdr(rows)
    c_cid = _col(hdr, "对象类ID")
    c_cn = _col(hdr, "对象类中文名称", "对象类名称", "中文名称")
    c_aid = _col(hdr, "属性ID")
    c_an = _col(hdr, "属性中文名称", "属性名称")

    # 保留列 = 原表头去掉 5 列（属性ID 在保留之列）
    keep = [(i, x) for i, x in enumerate(hdr) if x and x not in STAGING_DROP_COLS]
    dropped = [x for x in STAGING_DROP_COLS if x in hdr]
    # 「状态」列在副本里统一写「有效」：属性池条目不因某个对象类的删除而失效
    st_j = next((j for j, (_, n) in enumerate(keep) if n == "状态"), -1)

    seen: set = set()
    out_rows: list[list] = []
    for r in rows[1:]:
        an = _cell(r, c_an)
        if not an:
            continue
        # 池子口径：一个属性ID只保留一行（不同对象类下的重复取用合并掉）
        k = _pool_key(_cell(r, c_aid), an)
        if k in seen:
            continue
        seen.add(k)
        row = [r[i] if i < len(r) else None for i, _ in keep]
        if st_j >= 0:
            row[st_j] = _STATUS_LABEL[ST_VALID]
        out_rows.append(row)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    dst = STAGING_DIR / f"{_safe_name(path.stem)}.属性暂存.xlsx"
    nwb = openpyxl.Workbook()
    nws = nwb.active
    nws.title = "属性（暂存）"
    nws.append([name for _, name in keep])
    for row in out_rows:
        nws.append(row)
    nws.freeze_panes = "A2"
    nwb.save(dst)

    try:
        rel = str(dst.relative_to(DATA_ROOT)).replace("\\", "/")
    except ValueError:                       # 测试时 STAGING_DIR 可能被指到别处
        rel = dst.name

    return {"ok": True, "path": str(dst), "rel": rel,
            "file": dst.name, "columns": [name for _, name in keep],
            "dropped_columns": dropped, "rows": len(out_rows),
            "status_column_filled": st_j >= 0,
            "source_file": path.name}


# ---------------------------------------------------------------------------
# 解析：把 1..N 份 xlsx 读成 {classes: {name: {...}}, attrs: [...]}
# ---------------------------------------------------------------------------
def _parse_file(path: Path) -> dict:
    import openpyxl
    from tools.evaluator import _col, _hdr      # 复用同一套列定位规则

    classes: dict[str, dict] = {}
    attrs: list[dict] = []

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        # ① 对象类表（可缺）
        if "对象类" in wb.sheetnames:
            rows = list(wb["对象类"].iter_rows(values_only=True))
            if rows:
                h = _hdr(rows)
                c_id, c_name = _col(h, "对象类ID"), _col(h, "对象类名称", "对象类中文名称", "中文名称")
                c_code, c_dom = _col(h, "对象类编码"), _col(h, "所属专业", "业务域")
                c_def, c_par = _col(h, "中文定义", "定义"), _col(h, "父类ID")
                c_st = _col(h, "状态")
                raw: list[dict] = []
                id2name: dict[str, str] = {}
                for r in rows[1:]:
                    nm = _cell(r, c_name)
                    if not nm:
                        continue
                    cid = _cell(r, c_id)
                    if cid:
                        id2name[cid] = nm
                    raw.append({
                        "name": nm, "id": cid,
                        "code": _cell(r, c_code),
                        "domain": _cell(r, c_dom),
                        "definition": _cell(r, c_def),
                        "status": _norm_status(_cell(r, c_st)),
                        "status_explicit": bool(_cell(r, c_st)),
                        "_parent_id": _cell(r, c_par),
                        "source_file": path.name,
                    })
                # 2026-10-23：父类ID → 父类名称（P3 底座的 subClassOf 边靠它）
                for c in raw:
                    pid = c.pop("_parent_id", "")
                    c["parent_name"] = id2name.get(pid, "")
                    classes[c["name"]] = c

        # ② 对象类属性表（必需，否则这份文件没意义）
        sheet = "对象类属性" if "对象类属性" in wb.sheetnames else None
        if sheet:
            rows = list(wb[sheet].iter_rows(values_only=True))
            if rows:
                h = _hdr(rows)
                # 2026-10-23 bugfix：真实列名是「对象类中文名称」，原来只找
                # 「对象类名称/中文名称」→ 定位失败 → 1523 条属性全部落到「（未归属）」，
                # P3 底座的 hasAttribute 边因此一条都建不出来。
                a_cn = _col(h, "对象类中文名称", "对象类名称", "中文名称")
                a_cid = _col(h, "对象类ID")
                a_nm = _col(h, "属性中文名称", "属性名称")
                a_code = _col(h, "属性编码")
                a_key = _col(h, "属性ID")
                a_dt = _col(h, "数据类型")
                a_cons = _col(h, "数据格式/约束", "约束")
                a_unit = _col(h, "属性单位", "单位", "量纲")
                a_def = _col(h, "属性定义", "中文定义", "定义")
                a_st = _col(h, "状态")
                for r in rows[1:]:
                    nm = _cell(r, a_nm)
                    if not nm:
                        continue
                    cn = _cell(r, a_cn)
                    attrs.append({
                        "class_name": cn or "（未归属）",
                        "class_id": _cell(r, a_cid),
                        "name": nm,
                        "code": _cell(r, a_code),
                        "std_id": _cell(r, a_key),
                        "data_type": _cell(r, a_dt),
                        "constraint": _cell(r, a_cons),
                        "unit": _cell(r, a_unit),
                        "definition": _cell(r, a_def),
                        "status": _norm_status(_cell(r, a_st)),
                        "status_explicit": bool(_cell(r, a_st)),
                        "source_file": path.name,
                    })
    finally:
        wb.close()

    # 有属性但类表缺这个类 → 用属性里的类名补类（「（未归属）」不补成类节点）
    # 这类类是**推断**出来的，必须如实标注，且默认落在「暂定」桶，不能冒充上传来的类。
    for a in attrs:
        if a["class_name"] and a["class_name"] != "（未归属）":
            classes.setdefault(a["class_name"], {
                "name": a["class_name"], "id": a.get("class_id", ""),
                "code": "", "domain": "", "definition": "",
                "parent_name": "", "status": ST_PENDING, "status_explicit": False,
                "inferred": True, "source_file": path.name})
    return {"classes": classes, "attrs": attrs}


# ---------------------------------------------------------------------------
# 合并：上传环节只做「放进库」，不做口径裁决
# ---------------------------------------------------------------------------
def _merge(base: dict, incoming: dict, *, origin: dict) -> dict:
    """按 (类名, 属性名) 放进库。

    **上传这一步不做口径冲突判定**——用户点上传就是要这份数据生效，
    同键以本次上传为准直接覆盖；「哪些值该统一、该听谁的」属于后续建模/合并环节的事，
    不在这里编造「待裁决」。
    """
    added_cls, added_attr, updated = [], [], []
    classes = base.setdefault("classes", {})
    index = {(a["class_name"], a["name"]): a for a in base.setdefault("attrs", [])}

    for nm, c in incoming["classes"].items():
        cur = classes.get(nm)
        if cur is None:
            classes[nm] = {**c, **origin}
            added_cls.append(nm)
            continue
        if c.get("status_explicit") and c.get("status") != cur.get("status"):
            cur["status"] = c["status"]
            cur["status_explicit"] = True
            updated.append(f"{nm}（状态 → {_STATUS_LABEL.get(c['status'], c['status'])}）")

    for a in incoming["attrs"]:
        key = (a["class_name"], a["name"])
        cur = index.get(key)
        if cur is None:
            row = {**a, **origin}
            base["attrs"].append(row)
            index[key] = row
            added_attr.append(f"{a['class_name']}·{a['name']}")
            continue
        if a.get("status_explicit") and a.get("status") != cur.get("status"):
            cur["status"] = a["status"]
            cur["status_explicit"] = True
            updated.append(f"{a['class_name']}·{a['name']}（状态 → {_STATUS_LABEL.get(a['status'], a['status'])}）")
        # 同键：本次上传的值直接生效（不覆盖空值、不做冲突记账）
        changed = [f for f in _INT_FIELDS
                   if (a.get(f) or "").strip() and (a.get(f) or "") != (cur.get(f) or "")]
        if changed:
            for f in changed:
                cur[f] = a[f]
            updated.append(f"{a['class_name']}·{a['name']}（更新 {len(changed)} 项）")

    return {"added_classes": added_cls, "added_attrs": added_attr, "updated": updated}


def _ensure_class_fields(lib: dict) -> None:
    """给每个类补 attr_count，方便界面直接显示。"""
    cnt: dict[str, int] = {}
    for a in lib.get("attrs", []):
        cnt[a["class_name"]] = cnt.get(a["class_name"], 0) + 1
    for nm, c in lib.get("classes", {}).items():
        c["attr_count"] = cnt.get(nm, 0)


# ---------------------------------------------------------------------------
# 对外：取库（供 P4/P5/P6 消费，形状与 load_mock_library 一致）
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def load_library(force: bool = False) -> dict:
    """给 P4/P5/P6 消费的底座视图。

    状态为「删除」的类/属性**不进底座**（原始行仍完整保留在 merged.json，
    计数由 sheet_stats 独立统计，两者不互相干扰）。
    """
    if not force:
        hit = _CACHE.get("lib")
        if hit is not None:
            return hit

    raw = _read_json(MERGED, _empty())
    if not raw.get("attrs") and not raw.get("classes"):
        _CACHE["lib"] = {}
        return {}

    classes = {k: v for k, v in (raw.get("classes") or {}).items()
               if v.get("status") != ST_DELETED}
    attrs = [a for a in (raw.get("attrs") or []) if a.get("status") != ST_DELETED]

    field_index: dict[str, list[dict]] = {}
    for a in attrs:
        field_index.setdefault(a["name"], []).append({
            "class_name": a["class_name"], "data_type": a.get("data_type", ""),
            "constraint": a.get("constraint", ""), "unit": a.get("unit", ""),
        })
    conflicts = {}
    for fname, rows in field_index.items():
        specs = {(r["data_type"], r["constraint"], r["unit"]) for r in rows}
        if len(specs) > 1:
            conflicts[fname] = sorted(specs)

    m = manifest()
    out = {
        "mode": "both",
        "source": "D0 已有标准库（已上传）",
        "source_short": f"D0 已有标准库（v{m.get('version', 0)}）",
        "source_note": "D0 常驻标准库；经人工审核通过的新增/修改会沉淀回这里。",
        "class_count": len(classes),
        "field_count": len(field_index),
        "attr_count": len(attrs),
        "classes": classes,
        "field_index": field_index,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "version": m.get("version", 0),
        "updated_at": m.get("updated_at", ""),
        "is_builtin": False,
        "sheet_stats": raw.get("sheet_stats") or {},
        "excluded": {
            "classes": len(raw.get("classes") or {}) - len(classes),
            "attrs": len(raw.get("attrs") or []) - len(attrs),
        },
        "_index_built": True,
    }
    _ensure_class_fields(out)
    _CACHE["lib"] = out
    return out


def _aggregate_counts(files: dict) -> dict:
    """跨文件累加三表计数（同名文件重传时按名称覆盖，不会重复累加）。"""
    tot = {"classes": _mk_counts(0), "class_attrs": _mk_counts(0), "attrs": _mk_counts(0)}
    for v in (files or {}).values():
        c = v.get("counts") or {}
        for k in tot:
            src = c.get(k) or {}
            for f in ("total",) + _STATUS_BUCKETS:
                tot[k][f] += int(src.get(f, 0) or 0)
    return tot


def _aggregate_pool(files: dict):
    ps = [v.get("pool") for v in (files or {}).values() if (v.get("pool") or {}).get("by")]
    if not ps:
        return None
    return {"size": sum(int(p.get("size", 0) or 0) for p in ps),
            "by": ps[0]["by"],
            "mixed": any(bool(p.get("mixed")) for p in ps)}


def _aggregate_dedupe(files: dict):
    ds = [v.get("dedupe") for v in (files or {}).values() if v.get("dedupe")]
    if not ds:
        return None
    return {"raw_rows": sum(int(d.get("raw_rows", 0) or 0) for d in ds),
            "unique_rows": sum(int(d.get("unique_rows", 0) or 0) for d in ds)}


def _apply_committed(counts, committed) -> dict:
    """上传带来的行数 + 人工审核入库的增量（入库的一律算有效）。"""
    out: dict = {}
    for k in ("classes", "class_attrs", "attrs"):
        c = dict((counts or {}).get(k) or _mk_counts(0))
        n = int(((committed or {}).get(k) or 0))
        if n:
            c["total"] = int(c.get("total", 0)) + n
            c[ST_VALID] = int(c.get(ST_VALID, 0)) + n
        out[k] = c
    return out


def _fallback_counts(raw: dict) -> dict:
    """最后回落（连 uploaded/ 原文件都没有）：没有状态信息就如实全部按有效计。"""
    classes = raw.get("classes") or {}
    attrs = raw.get("attrs") or []
    n_attr = len({a.get("name") for a in attrs if a.get("name")})
    return {"classes": _mk_counts(len(classes)),
            "class_attrs": _mk_counts(len(attrs)),
            "attrs": _mk_counts(n_attr)}


_SCAN_CACHE: dict = {}


def _scan_uploaded() -> dict | None:
    """库里没有 sheet_stats 时（旧版入库 / 手工恢复），直接扫 `uploaded/` 的原文件补出计数。

    **纯只读**：不写 merged.json、不改原文件。这样老库不用重传也能显示正确的三段计数。
    """
    names = manifest().get("files") or []
    per: dict[str, dict] = {}
    for n in names:
        p = UPLOAD_DIR / n
        if not p.exists():
            continue
        try:
            st = p.stat()
            key = (p.name, st.st_size, int(st.st_mtime))
            sc = _SCAN_CACHE.get(key)
            if sc is None:
                sc = scan_sheets(p)
                _SCAN_CACHE.clear()          # 只留最新一批，不无限涨
                _SCAN_CACHE[key] = sc
            per[p.name] = {"shape": sc["shape"], "counts": sc["counts"],
                           "pool": sc.get("pool"), "dedupe": sc.get("dedupe"),
                           "has_status_col": bool(sc.get("has_status_col"))}
        except Exception:
            continue
    if not per:
        return None
    shapes = {v.get("shape") for v in per.values()}
    return {"counts": _aggregate_counts(per),
            "pool": _aggregate_pool(per),
            "dedupe": _aggregate_dedupe(per),
            "shape": shapes.pop() if len(shapes) == 1 else "mixed"}


def status() -> dict:
    raw = _read_json(MERGED, _empty())
    lib = load_library()
    m = manifest()
    tail = {"version": m.get("version", 0), "updated_at": m.get("updated_at", ""),
            "batches": len(m.get("batches", [])), "files": m.get("files", []),
            "staging": m.get("staging") or None}
    if not lib:
        # 还没有上传过：明确告诉界面「库为空、不套用任何内置库」
        return {"source": "empty", "class_count": 0, "field_count": 0, "attr_count": 0,
                "counts": {"classes": _mk_counts(0), "class_attrs": _mk_counts(0),
                           "attrs": _mk_counts(0)},
                "shape": "", "note": "尚未上传已有标准库；新颖性一律判「全新」，不套用任何内置/参考库",
                **tail}
    ss = raw.get("sheet_stats") or {}
    if ss.get("counts"):
        counts = _apply_committed(ss.get("counts"), ss.get("committed"))
        shape, pool, dedupe = ss.get("shape", ""), ss.get("pool"), ss.get("dedupe")
        counts_from = "sheet"
    else:
        sc = _scan_uploaded()
        if sc:
            counts = _apply_committed(sc["counts"], (raw.get("sheet_stats") or {}).get("committed"))
            shape, pool, dedupe = sc["shape"], sc["pool"], sc["dedupe"]
            counts_from = "scan"
        else:
            counts = _fallback_counts(raw)
            shape, pool, dedupe = "", None, None
            counts_from = "fallback"
    return {"source": "uploaded", "class_count": lib["class_count"],
            "field_count": lib["field_count"], "attr_count": lib["attr_count"],
            "conflict_count": lib["conflict_count"],
            "counts": counts, "shape": shape, "counts_from": counts_from,
            "pool": pool or None,
            "dedupe": dedupe or None,
            "excluded": lib.get("excluded", {}),
            "version": lib["version"], "updated_at": lib["updated_at"],
            "batches": len(m.get("batches", [])), "files": m.get("files", []),
            "staging": m.get("staging") or None,
            "note": "",
            }


# ---------------------------------------------------------------------------
# 上传 / 追加
# ---------------------------------------------------------------------------
def _sha(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def overlap_with_answer(class_names: list[str]) -> dict:
    """与 D3 预期输出的类名重合度（防止把评测答案当库上传）。"""
    try:
        from tools import dataset_registry as reg
        gold_sets: dict[str, set[str]] = {}
        for mode in ("A", "B"):
            files = reg.resolve(mode).get("gold_files") or []
            names: set[str] = set()
            for f in files:
                try:
                    names |= set(_parse_file(Path(f))["classes"].keys())
                except Exception:
                    continue
            if names:
                gold_sets[mode] = names
        mine = {str(x).strip() for x in class_names if str(x).strip()}
        out = {}
        for mode, names in gold_sets.items():
            if not mine:
                continue
            ratio = round(len(mine & names) / max(1, len(mine)), 4)
            out[mode] = {"ratio": ratio, "overlap": len(mine & names), "gold": len(names)}
        return out
    except Exception:
        return {}


def ingest(paths: list[Path], *, note: str = "", append: bool = True,
           check_leak: bool = True, display_names: list[str] | None = None) -> dict:
    """上传/追加标准库。check_leak=True 时与 D3 做重合度校验。

    display_names 用于把临时文件名还原成用户上传时的原名（落盘文件要可追溯）。
    """
    paths = [Path(p) for p in paths if Path(p).suffix.lower() in (".xlsx", ".xls")]
    if not paths:
        raise ValueError("没有可解析的 xlsx 文件")

    parsed = {"classes": {}, "attrs": []}
    for p in paths:
        one = _parse_file(p)
        parsed["classes"].update(one["classes"])
        parsed["attrs"].extend(one["attrs"])
    if not parsed["attrs"]:
        raise ValueError("上传文件里没有『对象类属性』表或属性行为空")

    # ① 只读扫描：三表 / 双表各自的行数口径（原文件一个字都不改）
    scans = [scan_sheets(p) for p in paths]

    # ② 双表形态：另存一份「属性（暂存）」表（去重 + 删 5 列 + 保留属性ID）
    stagings: list[dict] = []
    for p, sc in zip(paths, scans):
        if sc["shape"] != "two":
            continue
        try:
            st = build_staging_attrs(p)
        except Exception as e:                       # 暂存表失败不能挡住入库
            st = {"ok": False, "reason": f"暂存表生成失败：{e}", "source_file": p.name}
        if st.get("ok"):
            stagings.append(st)

    leak = overlap_with_answer(list(parsed["classes"].keys())) if check_leak else {}
    suspect = {m: v for m, v in leak.items() if v["ratio"] >= 0.6}

    base = _read_json(MERGED, _empty()) if append else _empty()
    prev_stats = (base.get("sheet_stats") or {}) if append else {}
    prev_committed = prev_stats.get("committed") or {}

    # 按**上传时的原始文件名**记统计：同名重传覆盖，不同名累加（避免临时名无限堆积）
    per_file = dict(prev_stats.get("files_by_name") or {})
    for i, (sc, p) in enumerate(zip(scans, paths)):
        key = Path(display_names[i]).name if display_names and i < len(display_names) else p.name
        per_file[key] = {"shape": sc["shape"], "counts": sc["counts"],
                         "pool": sc.get("pool"), "dedupe": sc.get("dedupe"),
                         "has_status_col": bool(sc.get("has_status_col"))}
    counts = _aggregate_counts(per_file)
    shapes = {v.get("shape") for v in per_file.values()}
    shape = shapes.pop() if len(shapes) == 1 else ("mixed" if shapes else "unknown")

    origin = {"origin": "upload", "added_at": _now()}
    diff = _merge(base, parsed, origin=origin)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, p in enumerate(paths):
        name = (display_names[i] if display_names and i < len(display_names) else "") or p.name
        name = Path(name).name
        dst = UPLOAD_DIR / name
        if dst.exists() and _sha(dst) != _sha(p):
            dst = UPLOAD_DIR / f"{Path(name).stem}-{_sha(p)}{Path(name).suffix}"
        shutil.copy2(p, dst)
        saved.append(dst.name)

    base["_index_built"] = False
    base["sheet_stats"] = {
        "shape": shape,
        "files_by_name": per_file,
        "counts": counts,
        "dedupe": _aggregate_dedupe(per_file),
        "pool": _aggregate_pool(per_file),
        "has_status_col": any(bool(v.get("has_status_col")) for v in per_file.values()),
        "staging": [{"file": s["file"], "rows": s["rows"],
                     "dropped_columns": s["dropped_columns"]} for s in stagings],
        "committed": prev_committed,
        "at": _now(),
    }
    _write_json(MERGED, base)
    m = _bump_manifest({"source": "uploaded",
                        "files": sorted({x for x in manifest().get("files", []) + saved}),
                        "staging": ({"file": stagings[0]["file"], "rel": stagings[0]["rel"],
                                     "rows": stagings[0]["rows"]} if stagings else None),
                        "counts": counts,
                        "batches": manifest().get("batches", []) + [{
                            "at": _now(), "files": saved, "note": note,
                            "shape": shape,
                            "counts": counts,
                            "added_classes": len(diff["added_classes"]),
                            "added_attrs": len(diff["added_attrs"]),
                            "updated": len(diff["updated"]),
                        }]})
    _log({"action": "ingest", "files": saved, "note": note, "shape": shape,
          "counts": counts,
          "staging": [s["rel"] for s in stagings],
          "diff": {
              "added_classes": len(diff["added_classes"]), "added_attrs": len(diff["added_attrs"]),
              "updated": len(diff["updated"])},
          "version": m["version"]})
    _invalidate_cache()

    lib = load_library(force=True)
    return {"ok": True, "version": m["version"], "files": saved,
            "shape": shape, "counts": counts,
            "file_shapes": [sc["shape"] for sc in scans],
            "staging": [{"file": s["file"], "rel": s["rel"], "rows": s["rows"],
                         "columns": s["columns"], "dropped_columns": s["dropped_columns"]}
                        for s in stagings],
            # 计数与样例分开返回：added_class_count 才是真实数字
            "added_class_count": len(diff["added_classes"]),
            "added_attr_count": len(diff["added_attrs"]),
            "updated_count": len(diff["updated"]),
            "added_classes_sample": diff["added_classes"][:20],
            # 兼容旧字段
            "added_classes": diff["added_classes"][:50],
            "added_attrs": len(diff["added_attrs"]),
            "updated": diff["updated"][:50],
            "leak_warning": [{"mode": k, **v} for k, v in suspect.items()],
            "leak_check": leak,
            "class_count": len(lib.get("classes") or {}),
            "attr_count": lib.get("attr_count", 0)}


# ---------------------------------------------------------------------------
# 审核通过后回写
# ---------------------------------------------------------------------------
def commit_standard(standard_id: str, *, gate_id: str = "", actor: str = "human",
                    allow_overwrite: bool = False) -> dict:
    """把一份已通过人工审核的标准写回 D0。只在人勾选「通过并入库」时调用。"""
    from tools import standard_generator as sg

    std = sg.get_standard(standard_id)
    if std is None:
        raise FileNotFoundError(f"未找到标准 {standard_id}")

    classes: dict[str, dict] = {}
    attrs: list[dict] = []
    for sp in std.get("field_specs") or []:
        cn = sp.get("class_name") or std.get("object_class_name") or "（未归属）"
        classes.setdefault(cn, {"name": cn, "code": sp.get("class_code", ""),
                                "domain": "", "source_file": standard_id,
                                "status": ST_VALID, "status_explicit": True})
        attrs.append({
            "class_name": cn, "name": sp.get("name", ""),
            "code": sp.get("code", ""), "std_id": sp.get("std_id", ""),
            "data_type": sp.get("data_type", ""), "constraint": sp.get("constraint", ""),
            "unit": sp.get("unit", ""), "definition": sp.get("definition", ""),
            "status": ST_VALID, "status_explicit": True,   # 人已审核通过 → 有效
            "source_file": standard_id,
        })
    if not attrs:
        raise ValueError(f"标准 {standard_id} 没有可入库的字段")

    base = _read_json(MERGED, _empty())
    origin = {"origin": "review", "added_at": _now(),
              "source_standard_id": standard_id, "gate_id": gate_id, "actor": actor}

    # 审核入库允许覆盖（人已确认），但仍留旧值到 change_log
    index = {(a["class_name"], a["name"]): a for a in base.setdefault("attrs", [])}
    overwritten = []
    new_attr_names: set[str] = set()
    new_classes: set[str] = set()
    for a in attrs:
        key = (a["class_name"], a["name"])
        cur = index.get(key)
        if cur is None:
            row = {**a, **origin}
            base["attrs"].append(row)
            index[key] = row
            new_attr_names.add(a["name"])
            continue
        # 幂等：同一标准同一门控已写过就跳过
        if cur.get("source_standard_id") == standard_id and gate_id and cur.get("gate_id") == gate_id:
            continue
        changed = {f: (cur.get(f, ""), a.get(f, "")) for f in _INT_FIELDS
                   if (a.get(f) or "").strip() and (a.get(f) or "") != (cur.get(f) or "")}
        if not changed:
            continue
        if not allow_overwrite:
            # 不覆盖，只补空
            for f, (old, new) in changed.items():
                if not (old or "").strip():
                    cur[f] = new
            continue
        overwritten.append({"key": f"{a['class_name']}·{a['name']}",
                            "fields": [{"field": f, "old": o, "new": n} for f, (o, n) in changed.items()]})
        cur.update({f: v[1] for f, v in changed.items()})
        cur.update(origin)

    for nm, c in classes.items():
        if nm not in base["classes"]:
            new_classes.add(nm)
        base["classes"].setdefault(nm, {**c, **origin})

    # 审核入库带来的增量单独记账：D0 卡片的三段计数 = 上传行数 + 这里的增量
    ss = base.setdefault("sheet_stats", {})
    if ss:
        cm = ss.setdefault("committed", {})
        cm["classes"] = int(cm.get("classes", 0)) + len(new_classes)
        cm["class_attrs"] = int(cm.get("class_attrs", 0)) + len(new_attr_names)
        cm["attrs"] = int(cm.get("attrs", 0)) + len(new_attr_names)

    base["_index_built"] = False
    _write_json(MERGED, base)
    m = _bump_manifest({})
    _log({"action": "commit", "standard_id": standard_id, "gate_id": gate_id,
          "actor": actor, "overwritten": overwritten,
          "added_classes": sorted(new_classes),
          "added_attr_count": len(new_attr_names), "version": m["version"]})
    _invalidate_cache()

    lib = load_library(force=True)
    return {"ok": True, "standard_id": standard_id, "version": m["version"],
            "committed_attrs": len(attrs), "overwritten": overwritten[:20],
            "added_class_count": len(new_classes),
            "added_attr_count": len(new_attr_names),
            "class_count": lib.get("class_count", 0), "field_count": lib.get("field_count", 0)}


def reset(actor: str = "human") -> dict:
    """清空 D0，回到「未上传」状态（内置参考底座接管）。

    一并清掉 `_staging/` 里的属性暂存副本——它们是本库的派生物，
    随时可从 `uploaded/` 的原始文件重新生成；原始上传文件本身不删。
    """
    for p in (MERGED, MANIFEST):
        if p.exists():
            p.unlink()
    dropped_staging = []
    if STAGING_DIR.exists():
        for f in sorted(STAGING_DIR.glob("*.xlsx")):
            try:
                f.unlink()
                dropped_staging.append(f.name)
            except OSError:
                pass
    m = {"version": 0, "batches": [], "updated_at": _now(), "source": "builtin", "files": []}
    _write_json(MANIFEST, m)
    _log({"action": "reset", "actor": actor, "dropped_staging": dropped_staging})
    _invalidate_cache()
    return {"ok": True, "version": 0, "dropped_staging": dropped_staging}


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=1))
