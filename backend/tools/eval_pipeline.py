"""
eval_pipeline.py
================
评测专用生成链路：**只用该组 `输入/` 现场生成**，不读 `预期输出/`，不写 dev 缓存。

用户 2026-10-22 重排数据后，输入与预期输出成组绑定（见 dataset_registry）：

| 模式 | 组 | 输入 | 预期输出 |
|---|---|---|---|
| A | group1 | 物项分级原则 PDF（扫描件） | E板块标准（仅对象类表，160 类） |
| A | group2 | 管道等级表 PDF（扫描件） | 管道管件模型（21 类 / 417 属性） |
| B | default | 5 份业务需求表（xlsx） | EM4 模型（57 类 / 891 属性 / 46 属性字典） |

本模块职责（按 规则.md 的转换要求）：
1. 读输入表 → 对象类 / 对象类属性；
2. 合并多个文件、按"对象类名 + 属性名"去重；
3. **自然语言类型转换**：`字符型（50）` → `String` + `VARCHAR(50)`；`数值型（12,2）` → `Float` + `FLOAT`；
   `参照型（50）` → `String`（引用目标未知，约束留空，备注说明）；
4. 业务原文（如"仅允许 SCH+数字"）保留到 `biz_constraint`，不硬塞进技术格式；
5. ID / 编码不在本模块生成 —— 由导出环节按 规则.md 统一编号。

安全边界：只读 `输入/`；**绝不读 `预期输出/`**；不写图谱 / RAG / 缓存 / deliverables。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import dataset_registry as registry  # noqa: E402


# ---------------------------------------------------------------------------
# 自然语言 → 数据类型 / 技术格式
# ---------------------------------------------------------------------------
def parse_type(v) -> tuple[str, int | None, int | None]:
    """`字符型（50）` / `数值型（12,2）` / `参照型（50）` / `日期型` → (类型, 长度, 精度)。"""
    s = str(v or "").strip()
    if not s:
        return "String", None, None
    m = re.search(r"[（(]\s*(\d+)\s*(?:[,，]\s*(\d+)\s*)?[）)]", s)
    length = int(m.group(1)) if m else None
    precision = int(m.group(2)) if (m and m.group(2)) else None

    low = s.lower()
    if "字符" in s or "文本" in s or "varchar" in low or "char" in low:
        t = "String"
    elif "参照" in s or "枚举" in s or "enum" in low:
        t = "String"
    elif "数值" in s or "数字" in s or "小数" in s or "decimal" in low or "float" in low or "numeric" in low:
        t = "Float"
    elif "日期时间" in s or "datetime" in low or "timestamp" in low:
        t = "DateTime"
    elif "日期" in s or "date" in low:
        t = "Date"
    elif "布尔" in s or "bool" in low:
        t = "Boolean"
    elif "整" in s or "int" in low:
        t = "Int"
    else:
        t = "String"
    return t, length, precision


def tech_constraint(data_type: str, length: int | None, precision: int | None,
                    raw: str = "") -> str:
    """按 规则.md 的技术格式口径生成约束；无法确定就留空，不编造。"""
    if "参照" in str(raw):
        return ""          # 引用哪张主数据表未知，留空并备注
    if data_type == "String":
        return f"VARCHAR({length})" if length else ""
    if data_type in ("Float", "Int"):
        return "FLOAT" if data_type == "Float" else "INT"
    if data_type == "Date":
        return "DATE"
    if data_type == "DateTime":
        return "DATETIME"
    if data_type == "Boolean":
        return "BOOLEAN"
    return ""


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def _norm(s) -> str:
    """查重键（去括号内容/空白、转小写）。实现统一收敛到 `text_norm.norm_key`，
    避免各模块各写一套导致「全角括号算不算同一个类」口径分裂。"""
    from tools import text_norm as tn
    return tn.norm_key(s)


def _dedup_classes(classes: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in classes:
        k = _norm(c.get("name", ""))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _dedup_attrs(attrs: list[dict], valid_classes: set[str]) -> list[dict]:
    seen, out = set(), []
    for a in attrs:
        k = (_norm(a.get("class_name", "")), _norm(a.get("name", "")))
        if not k[1] or k in seen:
            continue
        # 归属对象类不在对象类表里的，仍保留（导出时挂主类），但标记
        seen.add(k)
        a["class_known"] = k[0] in valid_classes if k[0] else False
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# 模式 B：5 份业务需求表 → 标准
# ---------------------------------------------------------------------------
def _from_requirement(r: dict) -> dict:
    t = registry.load_tables(r["input_files"])
    classes = _dedup_classes(t["classes"])
    valid = {_norm(c["name"]) for c in classes}
    attrs = _dedup_attrs(t["attrs"], valid)

    for a in attrs:
        raw = a.get("data_type", "")
        dt, ln, pr = parse_type(raw)
        a["data_type_raw"] = raw
        a["data_type"] = dt
        a["length"] = ln
        a["precision"] = pr
        a["biz_constraint"] = a.get("constraint", "")
        a["constraint"] = tech_constraint(dt, ln, pr, raw)

    # 属性归属的对象类若不在对象类表，补一个占位类，避免导出悬空
    known = {_norm(c["name"]) for c in classes}
    for a in attrs:
        cn = _norm(a.get("class_name", ""))
        if cn and cn not in known:
            classes.append({"id": "", "code": a.get("class_code", ""), "name": a["class_name"],
                            "en_name": "", "definition": "", "parent_id": "",
                            "domain": "", "status": "", "synthesized": True})
            known.add(cn)

    multi = len(r["input_files"])
    return {
        "mode": r["mode"],
        "group": r["group"],
        "label": r["label"],
        "classes": classes,
        "attrs": attrs,
        "input_files": [os.path.basename(f) for f in r["input_files"]],
        "input_quality": "ready" if classes or attrs else "degraded",
        "input_note": (f"合并 {multi} 份业务需求表：{len(classes)} 个对象类 / {len(attrs)} 条属性；"
                       f"自然语言类型已按规则转换（字符型→String+VARCHAR(n)、数值型→Float+FLOAT）；"
                       f"业务规则原文保留在 biz_constraint，不当作技术格式"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 模式 A：PDF 业务文档 → 标准（扫描件则降级）
# ---------------------------------------------------------------------------
def _from_documents(r: dict) -> dict:
    from tools import doc_parser as dp

    docs, total_chars, notes = [], 0, []
    for f in r["input_files"]:
        if not f.lower().endswith((".pdf", ".docx", ".md", ".txt", ".xlsx")):
            continue
        try:
            parsed = dp.parse_document(f)
            chars = 0
            for pg in parsed.get("pages", []):
                for b in pg.get("text_blocks", []):
                    chars += len(str(b.get("text") or ""))
            total_chars += chars
            docs.append((f, parsed))
            notes.append(f"{os.path.basename(f)}: {chars} 字符 / {parsed.get('parse_quality', '')}")
        except Exception as e:
            notes.append(f"{os.path.basename(f)}: 解析失败 {e}")

    base = {
        "mode": r["mode"],
        "group": r["group"],
        "label": r["label"],
        "input_files": [os.path.basename(f) for f in r["input_files"]],
        "input_note": "；".join(notes),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if total_chars < 20:
        return {**base, "classes": [], "attrs": [], "input_quality": "degraded",
                "input_note": (f"该组输入为扫描件（文本层合计 {total_chars} 字符），无法从原文识别对象类/属性。"
                               f"这是「输入不可解析」，不是 0% 准确率；需提供可解析文档或接入 OCR。详情："
                               + "；".join(notes))}

    # 有文本层时的弱抽取：表格优先，其次短行
    seen_c, seen_a, classes, attrs = set(), set(), [], []
    for f, parsed in docs:
        for pg in parsed.get("pages", []):
            for tb in pg.get("tables", []):
                rows = tb.get("rows") if isinstance(tb, dict) else tb
                for row in (rows or []):
                    for cell in (row or []):
                        name = str(cell or "").strip()
                        k = _norm(name)
                        if 2 <= len(name) <= 16 and k and k not in seen_c:
                            seen_c.add(k)
                            classes.append({"id": "", "code": "", "name": name, "en_name": "",
                                            "definition": "", "parent_id": "", "domain": "",
                                            "status": "", "weak": True})
            for b in pg.get("text_blocks", []):
                for line in str(b.get("text") or "").splitlines():
                    name = line.strip()
                    k = _norm(name)
                    if 2 <= len(name) <= 16 and re.search(r"[\u4e00-\u9fa5]", name) and k not in seen_c:
                        seen_c.add(k)
                        classes.append({"id": "", "code": "", "name": name, "en_name": "",
                                        "definition": "", "parent_id": "", "domain": "",
                                        "status": "", "weak": True})
    base.update({"classes": classes, "attrs": attrs, "input_quality": "ready",
                 "input_note": f"从文档弱抽取 {len(classes)} 个候选对象类（无属性级信息）"})
    return base


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_pred(mode: str, group: str | None = None) -> dict:
    r = registry.resolve(mode, group)
    if r["mode"] == "B":
        return _from_requirement(r)
    return _from_documents(r)


def describe(mode: str, group: str | None = None) -> dict:
    r = registry.resolve(mode, group)
    return {
        "mode": r["mode"], "group": r["group"], "label": r["label"],
        "input_dir": str(r["input_dir"]).replace("\\", "/"),
        "gold_dir": str(r["gold_dir"]).replace("\\", "/"),
        "input_files": [os.path.basename(f) for f in r["input_files"]],
        "gold_files": [os.path.basename(f) for f in r["gold_files"]],
    }


if __name__ == "__main__":
    import json
    for m, g in (("A", "group1"), ("A", "group2"), ("B", "default")):
        res = build_pred(m, g)
        print(f"[{m}/{g}] quality={res['input_quality']} classes={len(res['classes'])} attrs={len(res['attrs'])}")
        print("   ", res["input_note"][:180])
        if res["classes"]:
            print("    class0:", json.dumps(res["classes"][0], ensure_ascii=False))
        if res["attrs"]:
            print("    attr0:", json.dumps(res["attrs"][0], ensure_ascii=False)[:260])
