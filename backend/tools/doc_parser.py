"""
doc_parser.py
=============
共享工具 `doc_parse` 的真实实现（增量 1）。

能力：
- 支持 ≥ 3 种格式：PDF（pdfplumber）、DOCX（python-docx）、XLSX（openpyxl）。
- 输出统一结构：{file, fmt, page_count, parse_quality, pages:[{page_no, text_blocks, tables, figures, parse_quality, note}]}
- parse_quality 显式标记 ok / degraded / failed；degraded/failed 由调用方推入 P8 门控。
- 模式 B 额外提供模板结构识别：章节标题、表格字段、复选框、空白待填项，
  并与 D5 需求模板（requirement-template.json）做结构比对，标注偏离。

所有解析在内部受控环境执行，不将核电文档原文发往外部服务（见 SKILL 9）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"

MODE_A_SAMPLE_DIR = DATA / "modeA" / "dev" / "input"
MODE_B_SAMPLE_REQ = (
    DATA / "modeB" / "dev" / "input"
    / "数据标准需求-业务人员填写-EPC互联-大宗材料类-EM4管道管件-建模用数据集.xlsx"
)
REQ_TEMPLATE = DATA / "templates" / "requirement-template.json"

# 质量阈值（每页平均可抽取字符）
OK_AVG_CHARS = 80
DEGRADED_AVG_CHARS = 5


def detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xls": "xlsx",
            ".md": "md", ".markdown": "md"}.get(ext, "unknown")


def _quality_from_text(total_chars: int, page_count: int, table_count: int) -> tuple[str, str]:
    if page_count == 0:
        return "failed", "空文件或无法读取页面"
    avg = total_chars / page_count
    if total_chars == 0 and table_count == 0:
        return "degraded", "图像型/扫描件：无文本层、无可抽取表格，建议 OCR 后重试"
    if avg < DEGRADED_AVG_CHARS:
        return "degraded", "文本密度极低（疑似扫描件或版式复杂）"
    if avg < OK_AVG_CHARS:
        return "degraded", "文本偏稀疏，部分版面可能丢失"
    return "ok", "文本层完整，可结构化抽取"


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _parse_pdf(path: str) -> dict:
    import pdfplumber

    pages = []
    total_chars = 0
    table_count = 0
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        for i, pg in enumerate(pdf.pages, start=1):
            text = pg.extract_text() or ""
            tbls = pg.extract_tables() or []
            table_count += len(tbls)
            total_chars += len(text)
            pages.append({
                "page_no": i,
                "text_blocks": [{"text": text}] if text.strip() else [],
                "tables": tbls,
                "figures": [],
                "parse_quality": "ok" if len(text) >= OK_AVG_CHARS else (
                    "degraded" if len(text) >= DEGRADED_AVG_CHARS else "degraded"),
                "note": "" if text.strip() else "本页无文本层（图像型）",
            })
    quality, note = _quality_from_text(total_chars, n, table_count)
    return {"fmt": "pdf", "page_count": n, "parse_quality": quality, "note": note, "pages": pages}


def _parse_xlsx(path: str) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    pages = []
    total_chars = 0
    for si, ws in enumerate(wb.worksheets, start=1):
        rows = list(ws.iter_rows(values_only=True))
        # 去除全空尾行
        while rows and all(c is None or str(c).strip() == "" for c in rows[-1]):
            rows.pop()
        grid = [[("" if c is None else str(c)) for c in r] for r in rows]
        text = " ".join(cell for r in grid for cell in r)
        total_chars += len(text)
        pages.append({
            "page_no": si,
            "sheet": ws.title,
            "text_blocks": [{"text": text[:2000]}],
            "tables": [grid] if grid else [],
            "figures": [],
            "parse_quality": "ok",
            "note": f"工作表「{ws.title}」{len(rows)} 行",
        })
    wb.close()
    quality, note = _quality_from_text(total_chars, len(pages), len(pages))
    return {"fmt": "xlsx", "page_count": len(pages), "parse_quality": quality, "note": note, "pages": pages}


def _parse_docx(path: str) -> dict:
    import docx

    doc = docx.Document(path)
    blocks = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            blocks.append(t)
    tables = [[ [c.text for c in row.cells] for row in tb.rows] for tb in doc.tables]
    # 将段落分块为伪页面（每 20 段一页）
    per = 20
    pages = []
    total_chars = 0
    for i in range(0, max(len(blocks), 1), per):
        chunk = blocks[i:i + per]
        txt = "\n".join(chunk)
        total_chars += len(txt)
        pages.append({
            "page_no": i // per + 1,
            "text_blocks": [{"text": b} for b in chunk],
            "tables": tables if i == 0 else [],
            "figures": [],
            "parse_quality": "ok",
            "note": "",
        })
    quality, note = _quality_from_text(total_chars, max(len(pages), 1), len(tables))
    return {"fmt": "docx", "page_count": len(pages), "parse_quality": quality, "note": note, "pages": pages}


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_TABLE_SEP = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")

# ---------------------------------------------------------------------------
# 表头识别（唯一真源）
# ---------------------------------------------------------------------------
# 背景（2026-10-24）：P2 抽取与 P3 取数各自维护过一套表头特征，两套判断迟早不一致，
# 出现「P2 认为是属性表、P3 认为是类表」的分裂。这里统一收口，两边都调 classify_table。
def _s(v) -> str:
    return str(v).strip() if v is not None else ""


ATTR_NAME_COLS = ("属性中文名称", "属性名称", "字段名称", "字段中文名",
                  "数据项名称", "中文名称", "名称")
ATTR_SPEC_COLS = ("数据类型", "属性单位", "单位", "数据格式/约束", "约束", "值域", "量纲")
CLASS_NAME_COLS = ("对象类中文名称", "对象类名称", "对象类", "类别名称", "中文名称", "名称")
CLASS_SPEC_COLS = ("中文定义", "定义", "说明", "父类ID", "父类", "所属专业",
                   "对象类ID", "对象类编码")

# 强特征：出现任意一个就能定性，比"名称列 + 定义列"这种模棱两可的组合可靠得多
_ATTR_STRONG = ("数据类型", "属性单位", "数据格式/约束", "值域", "量纲",
                "属性编码", "属性ID", "对象类属性", "计量单位")
_CLASS_STRONG = ("对象类ID", "对象类编码", "父类ID", "所属专业", "确权平台",
                 "对象类中文名称", "父类名称")


def col_index(hdr: list[str], names: tuple[str, ...]) -> int:
    """按优先级找列：names 里越靠前越优先（避免泛化的"名称"抢到"对象类中文名称"）。"""
    for n in names:
        if n in hdr:
            return hdr.index(n)
    return -1


def classify_table(hdr: list[str]) -> str:
    """判定表头属于哪种表：attr_table / class_table / other。"""
    h = [_s(c) for c in (hdr or [])]
    hs = set(h)
    has_attr_name = col_index(h, ATTR_NAME_COLS) >= 0
    has_class_name = col_index(h, CLASS_NAME_COLS) >= 0
    attr_strong = any(c in hs for c in _ATTR_STRONG)
    class_strong = any(c in hs for c in _CLASS_STRONG)

    if has_attr_name and attr_strong:
        return "attr_table"
    if has_class_name and class_strong:
        return "class_table"
    # 弱判定兜底
    if has_attr_name and col_index(h, ATTR_SPEC_COLS) >= 0:
        return "attr_table"
    if has_class_name and col_index(h, CLASS_SPEC_COLS) >= 0:
        return "class_table"
    return "other"


def table_columns(hdr: list[str]) -> dict:
    """按表头取各语义列的位置，-1 表示没有该列。"""
    h = [_s(c) for c in (hdr or [])]
    return {
        "attr_name": col_index(h, ATTR_NAME_COLS),
        "class_name": col_index(h, CLASS_NAME_COLS),
        "data_type": col_index(h, ("数据类型", "类型", "字段类型")),
        "unit": col_index(h, ("属性单位", "单位", "计量单位", "量纲")),
        "constraint": col_index(h, ("数据格式/约束", "格式/约束", "约束", "值域", "格式")),
        "definition": col_index(h, ("属性定义", "中文定义", "定义", "说明", "描述")),
        "parent": col_index(h, ("父类名称", "父类中文名称", "父类")),
        "parent_id": col_index(h, ("父类ID",)),
        "code": col_index(h, ("属性编码", "对象类编码", "编码", "代码")),
        "attr_id": col_index(h, ("属性ID", "对象类属性ID")),
        "class_id": col_index(h, ("对象类ID",)),
    }


def cell(row: list, idx: int) -> str:
    """安全取单元格（越界返回空串）。"""
    if 0 <= idx < len(row):
        return _s(row[idx])
    return ""



def _md_rows_to_grid(lines: list[str]) -> list[list[str]]:
    """把连续的 markdown 表格行转成二维网格；分隔行（|---|）丢弃。"""
    grid: list[list[str]] = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        if _MD_TABLE_SEP.match(s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        grid.append(cells)
    return grid


def _parse_md(path: str) -> dict:
    """Markdown 解析：按标题分节成「页」，抽取表格与文本块。

    2026-10-23 新增。背景：A 模式的 D1 改为可上传 .md（不再依赖扫描件 OCR），
    P3 知识底座需要从文档正文/表格里取「对象类 + 属性」，所以 md 必须能被结构化。
    """
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    sections: list[dict] = []
    cur_title, cur_level, cur_lines = "", 0, []
    for ln in lines:
        m = _MD_HEADING.match(ln.strip())
        if m:
            sections.append({"title": cur_title, "level": cur_level, "lines": cur_lines})
            cur_title, cur_level, cur_lines = m.group(2).strip(), len(m.group(1)), []
        else:
            cur_lines.append(ln)
    sections.append({"title": cur_title, "level": cur_level, "lines": cur_lines})

    pages, total_chars, table_count = [], 0, 0
    for i, sec in enumerate(sections, start=1):
        body = [x for x in sec["lines"]]
        # 表格：连续以 | 开头的行块
        tables, buf = [], []
        for ln in body:
            if ln.strip().startswith("|"):
                buf.append(ln)
            elif buf:
                g = _md_rows_to_grid(buf)
                if len(g) >= 2:
                    tables.append(g)
                buf = []
        if buf:
            g = _md_rows_to_grid(buf)
            if len(g) >= 2:
                tables.append(g)
        text = "\n".join(x for x in body if not x.strip().startswith("|")).strip()
        total_chars += len(text) + sum(len(str(c)) for t in tables for r in t for c in r)
        table_count += len(tables)
        blocks = [{"text": text}] if text else []
        blocks += [{"text": f"（表格 {k + 1}）" + " / ".join(r)} for k, t in enumerate(tables) for r in t[1:]]
        pages.append({
            "page_no": i,
            "section": sec["title"] or f"（无标题段 {i}）",
            "section_level": sec["level"],
            "text_blocks": blocks,
            "tables": tables,
            "figures": [],
            "parse_quality": "ok",
            "note": f"标题「{sec['title']}」" if sec["title"] else "",
        })
    if not pages:
        return {"fmt": "md", "page_count": 0, "parse_quality": "failed",
                "note": "空文件", "pages": []}
    # md 的"页"是"节"，节多而短很正常，不能用 pdf 的字符密度阈值判质量。
    # 只要抽到了表格，或有像样的正文，就算 ok —— 否则会给 P2/P3 一个误导性的 degraded。
    if table_count or total_chars >= 80:
        quality, note = "ok", f"Markdown 结构化解析：{len(pages)} 节，{table_count} 张表"
    else:
        quality, note = _quality_from_text(total_chars, len(pages), table_count)
    return {"fmt": "md", "page_count": len(pages), "parse_quality": quality,
            "note": note, "pages": pages}


# 解析缓存：相同文件（路径+修改时间+大小）仅解析一次，重复演示秒开。
_PARSE_CACHE: dict[tuple, dict] = {}

# 磁盘级缓存：P1 解析过的文档，P2/P3 再取时不必重跑 pdfplumber
# （扫描版 PDF 单份就要 50s+，不缓存会让 P3 构建看起来像卡死）
_CACHE_DIR = DATA / ".parse_cache"


def _cache_key(path: str) -> str:
    import hashlib
    try:
        st = os.stat(path)
        raw = f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        raw = str(path)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _read_disk_cache(path: str) -> dict | None:
    p = _CACHE_DIR / f"{_cache_key(path)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_disk_cache(path: str, res: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{_cache_key(path)}.json").write_text(
            json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def parse_document(path: str, format_hint: str | None = None) -> dict:
    """解析单份文档，返回统一结构。失败返回 parse_quality=failed。"""
    fmt = format_hint or detect_format(path)
    name = Path(path).name
    try:
        key = (str(Path(path).resolve()), os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        key = (str(path), 0, 0)
    if key in _PARSE_CACHE:
        return _PARSE_CACHE[key]
    cached = _read_disk_cache(path)
    if cached is not None:
        cached["file"] = name
        _PARSE_CACHE[key] = cached
        return cached
    try:
        if fmt == "pdf":
            res = _parse_pdf(path)
        elif fmt == "xlsx":
            res = _parse_xlsx(path)
        elif fmt == "docx":
            res = _parse_docx(path)
        elif fmt == "md":
            res = _parse_md(path)
        else:
            return {"file": name, "fmt": fmt, "page_count": 0, "parse_quality": "failed",
                    "note": f"不支持的格式：{fmt}", "pages": []}
    except Exception as e:  # noqa: BLE001
        return {"file": name, "fmt": fmt, "page_count": 0, "parse_quality": "failed",
                "note": f"解析异常：{type(e).__name__}: {e}", "pages": []}
    res["file"] = name
    _PARSE_CACHE[key] = res
    if res.get("parse_quality") != "failed":
        _write_disk_cache(path, res)
    return res


# ---------------------------------------------------------------------------
# 模式 A：批量解析
# ---------------------------------------------------------------------------
def list_sample_files_a() -> list[str]:
    """模式 A 文档集：**只读 D1 槽位的显式决策**（上传或采用内置），不再偷偷回落示例目录。"""
    from tools import upload_manager as um
    files: list[str] = []
    for s in ("a_in_g1", "a_in_g2"):
        files += um.active_files(s)
    files = sorted(set(files))
    files = [f for f in files if Path(f).suffix.lower() in (".pdf", ".docx", ".xlsx", ".md")]
    if not files:
        raise um.DataUnavailable("a_in_g1", "D1 两组都没有可用文档")
    return files


def parse_mode_a(file_paths: list[str] | None = None) -> dict:
    """批量解析模式 A 文档集。file_paths 为空时解析 D1 当前数据集。"""
    paths = file_paths or list_sample_files_a()
    documents = [parse_document(p) for p in paths]
    overview = _build_overview(documents)
    return {"mode": "A", "documents": documents, "overview": overview}


# ---------------------------------------------------------------------------
# 模式 B：需求文件解析 + 模板结构识别
# ---------------------------------------------------------------------------
def parse_mode_b(req_path: str | None = None) -> dict:
    """解析单份需求文件，并识别模板结构。"""
    if req_path:
        p = req_path
    else:
        from tools import upload_manager as um
        fs = um.active_files("b_in")
        if not fs:
            raise um.DataUnavailable("b_in")
        p = sorted(fs)[0]      # 只按稳定顺序取第一份，不偏袒任何文件名
    doc = parse_document(p)
    recognition = recognize_template_structure(p)
    return {"mode": "B", "document": doc, "template_recognition": recognition}


def recognize_template_structure(req_path: str) -> dict:
    """识别需求文件的模板结构：章节、表格字段、复选框、空白待填项，并比对 D5 模板。"""
    import openpyxl

    wb = openpyxl.load_workbook(req_path, read_only=True, data_only=True)
    sections = []
    all_blanks = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        while rows and all(c is None or str(c).strip() == "" for c in rows[-1]):
            rows.pop()
        if not rows:
            continue
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        # 空白待填项：每列空值统计（仅数据行）
        col_blanks = {}
        data_rows = rows[1:]
        for ci, col in enumerate(header):
            empty = sum(1 for r in data_rows if ci >= len(r) or r[ci] is None or str(r[ci]).strip() == "")
            if data_rows and empty / len(data_rows) >= 0.3:
                col_blanks[col] = empty
        # 复选框：✓/□/是/否/√ 单元格
        chk = sum(1 for r in rows for c in r if c is not None and str(c).strip() in ("✓", "□", "√", "是", "否"))
        sections.append({
            "title": ws.title,
            "columns": header,
            "row_count": len(data_rows),
            "blank_fields": col_blanks,
            "checkbox_cells": chk,
        })
        for col, n in col_blanks.items():
            all_blanks.append({"section": ws.title, "field": col, "empty_count": n})
    wb.close()

    # 比对 D5 模板（只在用户真提供了 D5 时才比；不拿内置管道模板硬套别的物料）
    templ = None
    deviation = None
    matched = []
    missing = []
    templ_path = None
    try:
        from tools import upload_manager as _um
        if _um.is_uploaded("req_template"):
            fs = _um.uploaded_files("req_template")
            templ_path = Path(fs[0]) if fs and fs[0].lower().endswith(".json") else None
    except Exception:
        templ_path = None
    if templ_path is not None and templ_path.exists():
        templ = json.loads(templ_path.read_text(encoding="utf-8"))
        expected = {s["title"]: s for s in templ.get("sections", [])}
        actual = {s["title"]: s for s in sections}
        matched = [t for t in expected if t in actual]
        missing = [t for t in expected if t not in actual]
        # 实际为单表 12 列、期望 5 分节 → 结构偏离
        if len(sections) == 1 and len(expected) >= 2:
            deviation = (f"实际为单工作表扁平结构（{len(sections[0]['columns'])} 列），"
                         f"偏离 D5 模板的 {len(expected)} 个分节结构；"
                         f"已识别分节：{list(expected.keys())}，缺失分节：{missing or '无'}")
    return {
        "sections": sections,
        "blanks_highlighted": all_blanks,
        "template": {"available": templ is not None,
                     "source": templ_path.name if templ_path else "",
                     "expected_sections": list(templ.get("sections", [])) if templ else [],
                     "matched": matched, "missing": missing, "deviation": deviation,
                     "note": "" if templ else "未提供 D5 需求模板：已按文件自身表头识别，不做分节比对"},
    }


def _build_overview(documents: list[dict]) -> dict:
    q = {"ok": 0, "degraded": 0, "failed": 0}
    fmts: dict[str, int] = {}
    pages = 0
    for d in documents:
        q[d["parse_quality"]] = q.get(d["parse_quality"], 0) + 1
        fmts[d["fmt"]] = fmts.get(d["fmt"], 0) + 1
        pages += d["page_count"]
    total = len(documents) or 1
    return {
        "doc_count": len(documents),
        "page_count": pages,
        "quality": q,
        "quality_ratio": {k: round(100 * v / total) for k, v in q.items()},
        "formats": fmts,
        "gate_items": [{"file": d["file"], "quality": d["parse_quality"], "note": d.get("note", "")}
                       for d in documents if d["parse_quality"] in ("degraded", "failed")],
    }
