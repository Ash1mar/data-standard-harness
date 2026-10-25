"""
model_exporter.py
=================
P6 完整数据模型表格导出（xlsx），严格按 `规则.md` 的三表规范：

  1. `对象类`      13 列
  2. `对象类属性`  24 列
  3. `属性`        20 列（属性大池子，可被多个对象类复用）
  4. `说明`        导出元信息（非标准表，仅用于交付说明）

数据来源（二选一）：
  - 传 `group`：走 `eval_pipeline.build_pred(mode, group)`，即**从该组「输入」现场生成**（推荐，与 P7 同源）；
  - 传 `standard_id`：读 P6 已生成的标准 JSON（兼容旧流程，只有属性、没有对象类清单）。

ID / 编码规则（规则.md 一）：
  - 对象类ID = 所属大类英文缩写 2 位 + 6 位数字；缩写取对象类编码前 2 位，取不到则用临时前缀 `OC` 并备注；
  - 对象类编码 = 2 位大写 + `_` + 对象类英文名缩写；业务未填则留空（不编造）；
  - 属性ID = `Attr` + 6 位；同名属性在全表复用同一 ID（属性大池子语义）；
  - 关联ID = 对象类ID + 属性ID（两者都有才生成）；
  - 属性编码：业务未填则留空。

诚实标注：「说明」页写明临时编号、留空项、INCOMPLETE 字段。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import dataset_registry as registry  # noqa: E402
from tools import eval_pipeline as ep           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DELIVERABLES = ROOT / "deliverables"
STD_DIR = DELIVERABLES / "shared" / "standards"
EXPORT_DIR = DELIVERABLES / "exports"

CLASS_COLS = ["对象类ID", "对象类编码", "中文名称", "英文名称", "中文定义", "英文定义",
              "父类ID", "确权平台", "所属专业", "状态", "版本", "备注", "原对象类编码"]

ATTR_COLS = ["关联ID", "对象类ID", "对象类编码", "对象类中文名称", "属性ID", "属性编码",
             "属性中文名称", "属性单位", "数据类型", "数据格式/约束", "值域约束", "约束依赖",
             "是否主键", "可否为空", "是否唯一", "示例值", "确权平台", "所属专业",
             "状态", "版本", "备注", "原属性编码", "JsonSchema", "数据发布格式"]

POOL_COLS = ["属性ID", "属性编码", "中文名称", "中文定义", "数据类型", "数据格式/约束",
             "数据说明", "英文名称", "英文定义", "值域约束", "父属性ID", "可否多选",
             "量纲", "状态", "版本", "备注", "原属性编码对应", "原子属性对应",
             "JsonSchema", "数据发布格式"]


def _norm(s) -> str:
    s = str(s or "").strip().translate(str.maketrans("（）［］｛｝，。；：", "()[]{},.;:"))
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return re.sub(r"\s+", "", s).lower()


def _prefix(code: str) -> str:
    c = str(code or "").strip()
    if len(c) >= 2 and c[:2].isalpha():
        return c[:2].upper()
    return "OC"


def _load_standard(standard_id: str) -> dict:
    p = STD_DIR / f"{standard_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"未找到标准 {standard_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def _from_standard(std: dict, mode: str, group: str) -> dict:
    """标准条目 → 导出用的 {classes, attrs}。多类标准按 `spec.class_name` 分组。"""
    default_cls = std.get("object_class_name") or "待定对象类"
    default_code = std.get("object_class_code", "")
    attrs, seen_cls, classes = [], {}, []
    for s in std.get("field_specs") or []:
        cn = (s.get("class_name") or default_cls).strip() or default_cls
        cc = s.get("class_code") or (default_code if cn == default_cls else "")
        attrs.append({
            "name": s.get("name", ""), "code": s.get("code", ""),
            "data_type": s.get("data_type", ""), "constraint": s.get("constraint", ""),
            "unit": s.get("unit", ""), "definition": s.get("definition", ""),
            "class_name": cn, "class_code": cc,
            "source": s.get("source", ""),
        })
        if cn not in seen_cls:
            seen_cls[cn] = True
            classes.append({"id": "", "code": cc, "name": cn, "en_name": "",
                            "definition": "", "parent_id": "", "domain": "", "status": ""})
    # 标准里声明了但没有任何字段的类，也要出现在对象类表
    for c in std.get("classes") or []:
        cn = (c.get("name") or "").strip()
        if cn and cn not in seen_cls:
            seen_cls[cn] = True
            classes.append({"id": "", "code": c.get("code", ""), "name": cn, "en_name": "",
                            "definition": "", "parent_id": "", "domain": "", "status": ""})
    if not classes:
        classes = [{"id": "", "code": default_code, "name": default_cls, "en_name": "",
                    "definition": std.get("definition", ""), "parent_id": "",
                    "domain": "", "status": ""}]
    return {"classes": classes, "attrs": attrs, "input_files": [],
            "input_quality": "ready", "input_note": f"来自 P6 标准 {std.get('standard_id','')}"}


# ---------------------------------------------------------------------------
# 组装三表
# ---------------------------------------------------------------------------
def build_tables(pred_doc: dict) -> dict:
    classes = [dict(c) for c in (pred_doc.get("classes") or [])]
    attrs = [dict(a) for a in (pred_doc.get("attrs") or [])]

    # 对象类 ID / 编码
    seq: dict[str, int] = defaultdict(int)
    used_codes: dict[str, str] = {}
    code_re = re.compile(r"^[A-Z]{2}_[A-Za-z0-9]+$")
    for c in classes:
        code = str(c.get("code") or "").strip()
        pre = _prefix(code)
        seq[pre] += 1
        c["_id"] = f"{pre}{seq[pre]:06d}"
        c["_code"] = code

        note = ""
        if not code:
            note = "编码待分配"
        elif not code_re.match(code):
            note = "编码未按「两位大写_英文缩写」填写，待修正"
        elif code.upper() in used_codes:
            note = f"编码与「{used_codes[code.upper()]}」重复，待重新分配"
        if code:
            used_codes.setdefault(code.upper(), c.get("name", ""))
        c["_code_note"] = note

    # 属性池：同名属性复用同一 ID
    pool: dict[str, dict] = {}
    for a in attrs:
        k = _norm(a.get("name", ""))
        if not k:
            continue
        if k not in pool:
            pool[k] = {
                "id": f"Attr{len(pool) + 1:06d}",
                "name": a.get("name", ""), "code": a.get("code", ""),
                "data_type": a.get("data_type", ""), "constraint": a.get("constraint", ""),
                "unit": a.get("unit", ""), "definition": a.get("definition", ""),
                "biz": a.get("biz_constraint", ""), "source": a.get("source", ""),
                "classes": [],
            }
        pool[k]["classes"].append(a.get("class_name", ""))

    class_by_name = {_norm(c["name"]): c for c in classes}

    cls_rows = []
    for c in classes:
        cls_rows.append([
            c["_id"], c["_code"], c.get("name", ""), c.get("en_name", ""),
            c.get("definition", ""), "",
            c.get("parent_id", ""), "", c.get("domain", ""),
            "草稿", "", c.get("_code_note", ""), "",
        ])

    attr_rows = []
    for a in attrs:
        k = _norm(a.get("name", ""))
        if not k:
            continue
        ai = pool[k]
        cn = a.get("class_name", "")
        c = class_by_name.get(_norm(cn))
        cid = c["_id"] if c else f"OC000000"
        ccode = c["_code"] if c else ""
        rel = f"{cid}{ai['id']}" if cid else ""
        attr_rows.append([
            rel, cid, ccode, cn, ai["id"], ai["code"], ai["name"], ai.get("unit", ""),
            ai["data_type"], ai["constraint"], "", "",
            a.get("is_pk", ""), a.get("nullable", ""), a.get("unique", ""), "",
            "", "", "草稿", "",
            (f"业务原文：{ai['biz']}" if ai.get("biz") else ""), "", "", "",
        ])

    pool_rows = []
    for ai in pool.values():
        pool_rows.append([
            ai["id"], ai["code"], ai["name"], ai["definition"], ai["data_type"],
            ai["constraint"], "", "", "", "", "", "N", ai.get("unit", ""),
            "草稿", "", (f"归属：{'、'.join(sorted(set(x for x in ai['classes'] if x)))}"
                         if ai["classes"] else ""),
            "", "", "", "",
        ])

    return {"classes": cls_rows, "attrs": attr_rows, "pool": pool_rows,
            "n_classes": len(cls_rows), "n_attrs": len(attr_rows), "n_pool": len(pool_rows)}


# ---------------------------------------------------------------------------
# 写 xlsx
# ---------------------------------------------------------------------------
def export_xlsx(mode: str, group: str | None = None,
                standard_id: str | None = None) -> tuple[Path, dict]:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    m = str(mode).upper()
    if m not in ("A", "B"):
        raise ValueError("mode 只能是 A 或 B")

    if standard_id:
        std = _load_standard(standard_id)
        pred_doc = _from_standard(std, m, group or "")
        g = group or ""
        label = f"P6 标准 {standard_id}"
    else:
        ds = registry.resolve(m, group)
        g = ds["group"]
        label = ds["label"]
        pred_doc = ep.build_pred(m, g)

    t = build_tables(pred_doc)

    wb = openpyxl.Workbook()
    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True, size=10)

    def sheet(ws, cols, rows, widths):
        ws.append(cols)
        for i in range(1, len(cols) + 1):
            cell = ws.cell(row=1, column=i)
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top")

    ws1 = wb.active
    ws1.title = "对象类"
    sheet(ws1, CLASS_COLS, t["classes"], [13, 18, 20, 12, 30, 12, 13, 12, 12, 8, 8, 26, 14])

    ws2 = wb.create_sheet("对象类属性")
    sheet(ws2, ATTR_COLS, t["attrs"],
          [22, 13, 16, 20, 13, 16, 20, 10, 12, 26, 10, 10, 9, 9, 9, 10, 12, 12, 8, 8, 28, 12, 12, 12])

    ws3 = wb.create_sheet("属性")
    sheet(ws3, POOL_COLS, t["pool"],
          [13, 16, 20, 30, 12, 26, 8, 12, 12, 10, 12, 9, 10, 8, 8, 26, 14, 14, 12, 12])

    ws4 = wb.create_sheet("说明")
    meta = [
        ["项", "内容"],
        ["导出模式", f"模式 {m} / {g or '—'}"],
        ["数据集", label],
        ["生成输入", "、".join(pred_doc.get("input_files") or []) or "—"],
        ["输入质量", pred_doc.get("input_quality", "ready")],
        ["输入说明", pred_doc.get("input_note", "")],
        ["生成时间", datetime.now().isoformat(timespec="seconds")],
        ["对象类数", str(t["n_classes"])],
        ["对象类属性行数", str(t["n_attrs"])],
        ["属性池条数", str(t["n_pool"])],
        ["ID 规则", "对象类ID=大类缩写2位+6位数字；属性ID=Attr+6位；关联ID=对象类ID+属性ID（规则.md 一）"],
        ["临时前缀", "对象类编码缺失时用 `OC` 作临时前缀并在备注标「编码待分配」，导入中台前需按大类缩写修正"],
        ["留空项", "业务未填且无生成规则的编码/定义/示例值等一律留空，未编造"],
        ["口径", "本表由输入现场转换生成，非预期输出复制；状态统一「草稿」"],
    ]
    for r in meta:
        ws4.append(r)
    for c in (1, 2):
        ws4.cell(row=1, column=c).fill = head_fill
        ws4.cell(row=1, column=c).font = head_font
    ws4.column_dimensions["A"].width = 20
    ws4.column_dimensions["B"].width = 100
    for row in ws4.iter_rows(min_row=2):
        row[1].alignment = Alignment(vertical="top", wrap_text=True)
    ws4.freeze_panes = "A2"

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    name = f"数据模型-模式{m}-{g or 'std'}-{stamp}.xlsx"
    path = EXPORT_DIR / name
    wb.save(path)

    return path, {
        "mode": m, "group": g, "label": label,
        "classes": t["n_classes"], "attrs": t["n_attrs"], "pool": t["n_pool"],
        "input_files": pred_doc.get("input_files", []),
        "input_quality": pred_doc.get("input_quality", "ready"),
        "path": str(path).replace("\\", "/"),
    }


if __name__ == "__main__":
    for m, g in (("A", "group1"), ("A", "group2"), ("B", "default")):
        p, meta = export_xlsx(m, g)
        print(f"[{m}/{g}] 类 {meta['classes']} / 属性行 {meta['attrs']} / 属性池 {meta['pool']}"
              f" | {meta['input_quality']}")
        print("   ", p)
