"""
dataset_registry.py
===================
数据集注册表：(mode, group) → 输入 / 预期输出 的**唯一映射**。

用户 2026-10-22 重排数据结构后，项目里存在三组"输入 → 预期输出"：

| 模式 | 组 | 输入 | 预期输出 |
|---|---|---|---|
| A | group1 | `modeA/输入 group1/`（物项分级原则，PDF） | `modeA/预期输出 group1/`（E板块标准，仅对象类表） |
| A | group2 | `modeA/输入 group2/`（管道等级表，PDF） | `modeA/预期输出 group2/`（管道管件模型） |
| B | default | `modeB/输入/`（5 份业务需求表） | `modeB/预期输出/`（EM4 模型） |

**禁止**再按旧路径 `mode{A,B}/{dev,test}/{input,output}` 拼路径；
所有评测 / 生成 / 导出都必须经本模块 `resolve()` 取数。

预期输出的三张表（见 规则.md）：
- `对象类`（13 列）
- `对象类属性`（24 列）
- `属性`（21 列，属性大池子）
也兼容"只有对象类表"的 group1（sheet 名可能是 `Sheet1`）。
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = ROOT / "data"

DATASETS: dict[str, dict[str, dict]] = {
    "A": {
        "group1": {
            "label": "模式 A 当前数据集",
            "input": "modeA/输入 group1",
            "gold": "modeA/预期输出 group1",
        },
        "group2": {
            "label": "模式 A 备用数据集",
            "input": "modeA/输入 group2",
            "gold": "modeA/预期输出 group2",
        },
    },
    "B": {
        "default": {
            "label": "模式 B 当前数据集",
            "input": "modeB/输入",
            "gold": "modeB/预期输出",
        },
    },
}

DATA_EXT = (".xlsx", ".xls", ".pdf", ".docx", ".md", ".txt", ".csv")

# 界面上的「当前数据集」：每种模式只有一套，不并列展示
# A 的多组数据只是开发期测试用，跑完流程归档即可；group2 不进入 UI
ACTIVE_GROUP = {"A": "group1", "B": "default"}


# ---------------------------------------------------------------------------
# 1) 解析 (mode, group)
# ---------------------------------------------------------------------------
def list_groups(mode: str) -> list[dict]:
    """界面用：每种模式只返回「当前数据集」一项，不暴露其他 group。"""
    m = str(mode).upper()
    key = ACTIVE_GROUP.get(m)
    d = DATASETS.get(m, {}).get(key)
    if not d:
        return []
    inp = DATA_ROOT / d["input"]
    gold = DATA_ROOT / d["gold"]
    return [{
        "key": key,
        "label": d["label"],
        "input_dir": str(inp).replace("\\", "/"),
        "gold_dir": str(gold).replace("\\", "/"),
        "input_files": [os.path.basename(f) for f in _files(inp)],
        "gold_files": [os.path.basename(f) for f in _files(gold)],
    }]


def resolve(mode: str, group: str | None = None) -> dict:
    m = str(mode).upper()
    if m not in DATASETS:
        raise ValueError(f"未知模式：{mode}（只支持 A / B）")
    groups = DATASETS[m]
    g = group or ACTIVE_GROUP.get(m) or next(iter(groups))
    if g not in groups:
        raise ValueError(f"模式 {m} 没有组「{g}」，可选：{list(groups)}")
    d = groups[g]
    inp = DATA_ROOT / d["input"]
    gold = DATA_ROOT / d["gold"]
    if not inp.exists():
        raise FileNotFoundError(f"输入目录不存在：{inp}")
    # 预期输出是可选资源：用户梳理新数据时通常没有"答案"，
    # 缺它只影响能否算准确率，不影响生成；目录缺失不阻断。
    inp_slots = {"A": {"group1": "a_in_g1", "group2": "a_in_g2"},
                 "B": {"default": "b_in"}}[m]
    gold_slots = {"A": {"group1": "a_gold_g1", "group2": "a_gold_g2"},
                  "B": {"default": "b_gold"}}[m]
    return {
        "mode": m,
        "group": g,
        "label": d["label"],
        "input_dir": inp,
        "gold_dir": gold,
        # 现场上传优先，未上传才回落内置目录
        "input_files": _uploaded_or_builtin(inp_slots[g], inp),
        "gold_files": _uploaded_or_builtin(gold_slots[g], gold),
        "input_slot": inp_slots[g],
        "gold_slot": gold_slots[g],
    }


def _uploaded_or_builtin(slot: str, fallback_dir: Path) -> list[str]:
    """**只看槽位决策**：uploaded / builtin 才给文件，off / 未选择一律空。

    fallback_dir 仅为兼容旧签名保留，绝不再被读取 ——
    否则用户没在 P0 选择「采用内置数据」时，后端会悄悄拿内置数据继续算。
    """
    try:
        from tools import upload_manager as um
        return um.active_files(slot)
    except Exception:
        return []


def _files(d: Path) -> list[str]:
    if not d.exists():
        return []
    out = []
    for f in glob.glob(str(d / "*")):
        base = os.path.basename(f)
        if base.startswith("~$") or ".meta." in base or base.startswith("."):
            continue
        if os.path.isfile(f) and f.lower().endswith(DATA_EXT):
            out.append(f)
    return sorted(out)


# ---------------------------------------------------------------------------
# 2) 表头识别与解析（对象类 / 对象类属性 / 属性 三表）
# ---------------------------------------------------------------------------
def _hdr(rows) -> list[str]:
    return [str(c).strip() if c is not None else "" for c in rows[0]]


def _col(hdr: list[str], *names: str) -> int:
    for n in names:
        if n in hdr:
            return hdr.index(n)
    return -1


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _canon(v) -> str:
    """名称排版归一（全角括号→半角等）。**只改排版不改语义**，见 text_norm.py。"""
    from tools import text_norm as tn
    return tn.canonical_name(v)


def kind_of(hdr: list[str]) -> str | None:
    """按表头判断这是哪张表。顺序很重要：先判最具体的『关联ID』。"""
    if "关联ID" in hdr:
        return "attr"
    if "对象类ID" in hdr and ("中文名称" in hdr or "对象类名称" in hdr):
        return "class"
    if "属性ID" in hdr and "中文名称" in hdr:
        return "attr_dict"
    return None


def parse_classes(rows) -> list[dict]:
    hdr = _hdr(rows)
    c_id = _col(hdr, "对象类ID")
    c_code = _col(hdr, "对象类编码")
    c_name = _col(hdr, "中文名称", "对象类名称")
    c_en = _col(hdr, "英文名称")
    c_def = _col(hdr, "中文定义", "定义")
    c_parent = _col(hdr, "父类ID")
    c_domain = _col(hdr, "所属专业", "归口专业")
    c_status = _col(hdr, "状态")
    out = []
    for r in rows[1:]:
        name = _canon(r[c_name]) if c_name >= 0 and c_name < len(r) else ""
        if not name:
            continue
        out.append({
            "id": _s(r[c_id]) if c_id >= 0 and c_id < len(r) else "",
            "code": _s(r[c_code]) if c_code >= 0 and c_code < len(r) else "",
            "name": name,
            "en_name": _s(r[c_en]) if c_en >= 0 and c_en < len(r) else "",
            "definition": _s(r[c_def]) if c_def >= 0 and c_def < len(r) else "",
            "parent_id": _s(r[c_parent]) if c_parent >= 0 and c_parent < len(r) else "",
            "domain": _s(r[c_domain]) if c_domain >= 0 and c_domain < len(r) else "",
            "status": _s(r[c_status]) if c_status >= 0 and c_status < len(r) else "",
        })
    return out


def parse_attrs(rows) -> list[dict]:
    hdr = _hdr(rows)
    i_rel = _col(hdr, "关联ID")
    i_cid = _col(hdr, "对象类ID")
    i_ccode = _col(hdr, "对象类编码")
    i_cname = _col(hdr, "对象类中文名称")
    i_aid = _col(hdr, "属性ID")
    i_code = _col(hdr, "属性编码")
    i_name = _col(hdr, "属性中文名称", "属性名称")
    i_unit = _col(hdr, "属性单位", "单位")
    i_type = _col(hdr, "数据类型")
    i_cons = _col(hdr, "数据格式/约束", "约束")
    i_pk = _col(hdr, "是否主键")
    i_null = _col(hdr, "可否为空", "是否可为空")
    i_uniq = _col(hdr, "是否唯一")
    i_def = _col(hdr, "属性定义")
    i_vdom = _col(hdr, "值域约束")
    i_exam = _col(hdr, "示例值")
    i_req = _col(hdr, "是否必填")
    i_cnote = _col(hdr, "约束说明")
    out = []
    cur_cls = ""
    for idx, r in enumerate(rows[1:], start=2):
        name = _canon(r[i_name]) if i_name >= 0 and i_name < len(r) else ""
        if not name:
            continue
        cname = _canon(r[i_cname]) if i_cname >= 0 and i_cname < len(r) else ""
        if cname:
            cur_cls = cname
        out.append({
            "row": idx,
            "rel_id": _s(r[i_rel]) if i_rel >= 0 and i_rel < len(r) else "",
            "class_id": _s(r[i_cid]) if i_cid >= 0 and i_cid < len(r) else "",
            "class_code": _s(r[i_ccode]) if i_ccode >= 0 and i_ccode < len(r) else "",
            "class_name": cname or cur_cls,
            "attr_id": _s(r[i_aid]) if i_aid >= 0 and i_aid < len(r) else "",
            "code": _s(r[i_code]) if i_code >= 0 and i_code < len(r) else "",
            "name": name,
            "unit": _s(r[i_unit]) if i_unit >= 0 and i_unit < len(r) else "",
            "data_type": _s(r[i_type]) if i_type >= 0 and i_type < len(r) else "",
            "constraint": _s(r[i_cons]) if i_cons >= 0 and i_cons < len(r) else "",
            "definition": _s(r[i_def]) if i_def >= 0 and i_def < len(r) else "",
            "value_domain": _s(r[i_vdom]) if i_vdom >= 0 and i_vdom < len(r) else "",
            "example": _s(r[i_exam]) if i_exam >= 0 and i_exam < len(r) else "",
            "required": _s(r[i_req]) if i_req >= 0 and i_req < len(r) else "",
            "constraint_note": _s(r[i_cnote]) if i_cnote >= 0 and i_cnote < len(r) else "",
            "is_pk": _s(r[i_pk]) if i_pk >= 0 and i_pk < len(r) else "",
            "nullable": _s(r[i_null]) if i_null >= 0 and i_null < len(r) else "",
            "unique": _s(r[i_uniq]) if i_uniq >= 0 and i_uniq < len(r) else "",
        })
    return out


def parse_attr_dict(rows) -> list[dict]:
    hdr = _hdr(rows)
    i_id = _col(hdr, "属性ID")
    i_code = _col(hdr, "属性编码")
    i_name = _col(hdr, "中文名称", "属性中文名称")
    i_def = _col(hdr, "中文定义", "定义")
    i_type = _col(hdr, "数据类型")
    i_cons = _col(hdr, "数据格式/约束", "约束")
    i_unit = _col(hdr, "量纲", "单位")
    i_status = _col(hdr, "状态")
    out = []
    for r in rows[1:]:
        name = _canon(r[i_name]) if i_name >= 0 and i_name < len(r) else ""
        if not name:
            continue
        out.append({
            "attr_id": _s(r[i_id]) if i_id >= 0 and i_id < len(r) else "",
            "code": _s(r[i_code]) if i_code >= 0 and i_code < len(r) else "",
            "name": name,
            "definition": _s(r[i_def]) if i_def >= 0 and i_def < len(r) else "",
            "data_type": _s(r[i_type]) if i_type >= 0 and i_type < len(r) else "",
            "constraint": _s(r[i_cons]) if i_cons >= 0 and i_cons < len(r) else "",
            "unit": _s(r[i_unit]) if i_unit >= 0 and i_unit < len(r) else "",
            "status": _s(r[i_status]) if i_status >= 0 and i_status < len(r) else "",
        })
    return out


# ---------------------------------------------------------------------------
# 3) 读取一整组数据表（自动按表头分流）
# ---------------------------------------------------------------------------
def load_tables(paths: list[str]) -> dict:
    """读多个 xlsx，把每个 sheet 按表头分流到 classes / attrs / attr_dict。"""
    import openpyxl
    classes: list[dict] = []
    attrs: list[dict] = []
    attr_dict: list[dict] = []
    used: list[dict] = []

    for p in paths:
        if not p.lower().endswith((".xlsx", ".xlsm")):
            continue
        try:
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        except Exception as e:
            used.append({"file": os.path.basename(p), "error": str(e)})
            continue
        for sn in wb.sheetnames:
            ws = wb[sn]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            k = kind_of(_hdr(rows))
            if k == "class":
                got = parse_classes(rows)
                for g in got:
                    g["file"] = os.path.basename(p)
                classes += got
                used.append({"file": os.path.basename(p), "sheet": sn, "kind": "class", "rows": len(got)})
            elif k == "attr":
                got = parse_attrs(rows)
                for g in got:
                    g["file"] = os.path.basename(p)
                attrs += got
                used.append({"file": os.path.basename(p), "sheet": sn, "kind": "attr", "rows": len(got)})
            elif k == "attr_dict":
                got = parse_attr_dict(rows)
                attr_dict += got
                used.append({"file": os.path.basename(p), "sheet": sn, "kind": "attr_dict", "rows": len(got)})
        wb.close()

    return {"classes": classes, "attrs": attrs, "attr_dict": attr_dict, "sheets": used}


def load_gold_tables(mode: str, group: str | None = None) -> dict:
    """读预期输出（评测答案）。**只允许评测器调用，不得回流到生成链路。**"""
    r = resolve(mode, group)
    t = load_tables(r["gold_files"])
    t.update({
        "mode": r["mode"],
        "group": r["group"],
        "label": r["label"],
        "source": [str(f).replace("\\", "/") for f in r["gold_files"]],
        "counts": {"classes": len(t["classes"]), "attrs": len(t["attrs"]),
                   "attr_dict": len(t["attr_dict"])},
    })
    return t


if __name__ == "__main__":
    import json
    for m in ("A", "B"):
        print(f"=== 模式 {m}")
        for g in list_groups(m):
            print(" ", g["key"], "|", g["label"])
            print("    输入:", g["input_files"])
            print("    预期:", g["gold_files"])
            t = load_gold_tables(m, g["key"])
            print("    gold counts:", t["counts"], "| sheets:", t["sheets"])
