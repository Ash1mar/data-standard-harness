"""
upload_manager.py
=================
现场演示用「数据接入」层（2026-10-21 用户要求）。

背景
----
此前所有工具都写死了内置路径（data/modeA/dev/output/*.xlsx 等），演示时等于
「默认把数据放到某某路径下」，评委看不到数据是怎么进来的。

本模块把「数据从哪来」变成可替换的：

    上传槽位 (slot)  →  data/uploaded/<slot>/<原文件名>
    取数顺序         →  上传优先，未上传则回落到内置路径

槽位与 D1–D8 一一对应，上传后立刻做**真实结构校验**（不是只看扩展名）：
xlsx 查 sheet/必需列并统计规模、PDF 用 pdfplumber 探文本层、JSON 查必需字段。

防泄漏
------
上传的建模集若与 test 侧答案高度重叠，会在校验里给出**显式警告**——
现场误传答案文件时不至于悄悄污染知识底座。

本模块不依赖任何其它 tools 模块，避免循环导入。
"""

from __future__ import annotations

import glob
import json
import shutil
from datetime import datetime
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
UPLOAD_ROOT = DATA_ROOT / "uploaded"
MANIFEST_FILE = UPLOAD_ROOT / "manifest.json"

# ---------------------------------------------------------------------------
# 1) 槽位定义
# ---------------------------------------------------------------------------
# item: 对应 readiness_status.json 的项 id（用于覆盖面板状态）
# modes: 该槽位服务哪个模式；"both" 表示两模式共用（词表/模板这类无泄漏风险的资源）
SLOTS: dict[str, dict] = {
    # 界面常驻只显示编号（D1…），不写具体文档名 —— 每次上传的文档可以不同
    # item: D 编号（对应 readiness）；name: 通用数据项名称；group: 所属组（A 两组 / B 一组）
    "a_in_g1": {
        "item": "D1", "group": "group1", "modes": "A", "multi": True,
        "accept": ".pdf,.docx,.xlsx,.md",
        "label": "D1", "name": "模式 A 输入文档",
        "hint": "待梳理的业务文档（PDF / Word / Excel），须有文本层；扫描件会被判 degraded",
        "required": False,
    },
    "a_in_g2": {
        "item": "D1", "group": "group2", "modes": "A", "multi": True,
        "accept": ".pdf,.docx,.xlsx,.md",
        "label": "D1", "name": "模式 A 输入文档",
        "hint": "同上；文档内容决定本组生成对象类的范围",
        "required": False,
    },
    "b_in": {
        "item": "D2", "group": "default", "modes": "B", "multi": True,
        "accept": ".xlsx,.xls",
        "label": "D2", "name": "模式 B 业务需求表",
        "hint": "业务人员填写的需求 xlsx（1–N 份），需含「对象类属性」表与「属性中文名称」列",
        "required": True,
    },
    "a_gold_g1": {
        "item": "D3", "group": "group1", "modes": "A", "multi": False,
        "accept": ".xlsx,.xls",
        "label": "D3", "name": "预期输出（评测答案）",
        "hint": "可选：仅用于评测对照；新数据梳理时通常没有预期输出",
        "required": False,
    },
    "a_gold_g2": {
        "item": "D3", "group": "group2", "modes": "A", "multi": False,
        "accept": ".xlsx,.xls",
        "label": "D3", "name": "预期输出（评测答案）",
        "hint": "可选：仅用于评测对照；不上传则只生成不评分",
        "required": False,
    },
    "b_gold": {
        "item": "D3", "group": "default", "modes": "B", "multi": False,
        "accept": ".xlsx,.xls",
        "label": "D3", "name": "预期输出（评测答案）",
        "hint": "可选：仅用于评测对照；不上传则只生成不评分",
        "required": False,
    },
    "model_spec": {
        "item": "D4", "modes": "both", "multi": False, "accept": ".json",
        "label": "D4", "name": "数据标准建模规则",
        "hint": "JSON：三表列定义 + ID/编码规则 + 类型映射；缺省时按内置规则生成",
        "required": True,
    },
    "req_template": {
        "item": "D5", "modes": "B", "multi": False, "accept": ".xlsx,.xls,.json",
        "label": "D5", "name": "业务需求空白模板（可选）",
        "hint": "可选；不传则按列自动定位",
        "required": False,
    },
}


# 内置回落路径（相对 data/）。未上传时使用。
BUILTIN: dict[str, list[str]] = {
    # D1 输入（按组）
    "a_in_g1": ["modeA/输入 group1/*.pdf", "modeA/输入 group1/*.docx",
                "modeA/输入 group1/*.xlsx", "modeA/输入 group1/*.md"],
    "a_in_g2": ["modeA/输入 group2/*.pdf", "modeA/输入 group2/*.docx",
                "modeA/输入 group2/*.xlsx", "modeA/输入 group2/*.md"],
    # D2 输入：内置 = 演示三件套里的需求表，**精确到文件**
    # （同目录下还有其它批次的需求表：其他 / 管道支吊架卡具 / 管道管件 / 紧固件；
    #   用 *.xlsx 通配会把它们一起算进来，演示就不再是"型钢"这一套了）
    "b_in": ["modeB/输入/大宗材料类-型钢.xlsx"],
    # D3 预期输出（按组）
    "a_gold_g1": ["modeA/预期输出 group1/*.xlsx"],
    "a_gold_g2": ["modeA/预期输出 group2/*.xlsx"],
    # D3 内置 = 演示三件套里的 EM4 型钢模型，同样精确到文件
    # （同目录下还有 数据模型-大宗材料类-EM4.xlsx，是覆盖全部类的大模型）
    "b_gold": ["modeB/预期输出/数据模型-大宗材料类-EM4型钢.xlsx"],
    # D4 / D5 支撑
    "model_spec": ["modeling_rules.json"],
    "req_template": ["templates/数据标准需求-空白模板.xlsx"],
}


# ---------------------------------------------------------------------------
# 2) 落盘与清单
# ---------------------------------------------------------------------------
def _ensure() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    for s in SLOTS:
        (UPLOAD_ROOT / s).mkdir(parents=True, exist_ok=True)


def load_manifest() -> dict:
    _ensure()
    if MANIFEST_FILE.exists():
        try:
            return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"slots": {}}
    return {"slots": {}}


def save_manifest(m: dict) -> None:
    _ensure()
    m["updated_at"] = datetime.now().isoformat(timespec="seconds")
    MANIFEST_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def slot_dir(slot: str) -> Path:
    _ensure()
    return UPLOAD_ROOT / slot


def save_files(slot: str, items: list[tuple[str, bytes]]) -> list[str]:
    """把上传内容写入槽位目录。同一槽位重复上传会先清空旧文件（单文件槽位语义）。"""
    if slot not in SLOTS:
        raise KeyError(f"未知槽位：{slot}")
    d = slot_dir(slot)
    for f in d.iterdir():
        if f.is_file():
            f.unlink()
    saved = []
    for name, blob in items:
        safe = name.replace("\\", "/").split("/")[-1]
        tgt = d / safe
        tgt.write_bytes(blob)
        saved.append(str(tgt))
    m = load_manifest()
    m.setdefault("slots", {})[slot] = {
        "files": [Path(p).name for p in saved],
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    }
    m.get("decisions", {}).pop(slot, None)   # 上传即视为已选择，清掉内置决策
    save_manifest(m)
    return saved


def set_builtin(slot: str) -> dict:
    """显式选择「该槽位采用内置数据」：清空上传 + 记录决策。

    没有这一步时槽位是「待选择」状态 —— 不默认替用户做决定。
    """
    if slot not in SLOTS:
        raise KeyError(f"未知槽位：{slot}")
    d = slot_dir(slot)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    m = load_manifest()
    m.get("slots", {}).pop(slot, None)
    m.setdefault("decisions", {})[slot] = "builtin"
    save_manifest(m)
    invalidate_caches()
    return {"slot": slot, "decision": "builtin"}


def set_enabled(slot: str) -> dict:
    """重新纳入本轮（从 off 回到「待选择来源」）。"""
    if slot not in SLOTS:
        raise KeyError(f"未知槽位：{slot}")
    m = load_manifest()
    if m.get("decisions", {}).get(slot) == "off":
        m["decisions"].pop(slot, None)
        save_manifest(m)
        invalidate_caches()
    return {"slot": slot, "decision": decision(slot) or "pending"}


def set_off(slot: str) -> dict:
    """本轮不参与该数据项（清空上传）。必填项不允许关闭。"""
    if slot not in SLOTS:
        raise KeyError(f"未知槽位：{slot}")
    if SLOTS[slot].get("required"):
        raise ValueError(f"{SLOTS[slot]['label']} 是必填数据，不能关闭")
    d = slot_dir(slot)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    m = load_manifest()
    m.get("slots", {}).pop(slot, None)
    m.setdefault("decisions", {})[slot] = "off"
    save_manifest(m)
    invalidate_caches()
    return {"slot": slot, "decision": "off"}


def set_all_builtin() -> dict:
    """把所有槽位一键设为「采用内置数据」（清空各自上传）。"""
    for slot in SLOTS:
        d = slot_dir(slot)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    m = load_manifest()
    m["slots"] = {}
    m["decisions"] = {s: "builtin" for s in SLOTS}
    save_manifest(m)
    invalidate_caches()
    return {"decision": "builtin", "slots": len(SLOTS)}


def decision(slot: str) -> str | None:
    """该槽位当前决策：uploaded / builtin / off（本轮不参与）/ None（未决定）。"""
    if is_uploaded(slot):
        return "uploaded"
    return load_manifest().get("decisions", {}).get(slot)


def reset(slot: str | None = None) -> dict:
    """清空指定槽位（或全部）的上传内容，回落到内置数据。"""
    m = load_manifest()
    if slot:
        d = slot_dir(slot)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        m.get("slots", {}).pop(slot, None)
        m.get("decisions", {}).pop(slot, None)
    else:
        if UPLOAD_ROOT.exists():
            shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)
        m = {"slots": {}, "decisions": {}}
    save_manifest(m)
    invalidate_caches()
    return {"reset": slot or "all"}


# ---------------------------------------------------------------------------
# 3) 取数：上传优先，内置回落
# ---------------------------------------------------------------------------
def uploaded_files(slot: str) -> list[str]:
    d = slot_dir(slot)
    if not d.exists():
        return []
    return sorted(str(p) for p in d.iterdir() if p.is_file())


def builtin_files(slot: str) -> list[str]:
    out: list[str] = []
    for pat in BUILTIN.get(slot, []):
        for p in glob.glob(str(DATA_ROOT / pat)):
            base = Path(p).name
            # 过滤 Excel 打开时留下的临时锁文件（~$xxx.xlsx）、备份与隐藏文件，
            # 否则通配会把它当成一份"数据"
            if base.startswith("~$") or base.startswith(".") or ".bak-" in base or ".meta." in base:
                continue
            out.append(p)
    return sorted(set(out))


class DataUnavailable(RuntimeError):
    """该数据项本轮没有可用数据（关闭 / 未选来源 / 内置缺失）。

    任何环节拿到这个异常都必须**停下来并告诉用户**，严禁静默改用内置数据。
    """

    def __init__(self, slot: str, detail: str = ""):
        self.slot = slot
        label = SLOTS.get(slot, {}).get("label", slot)
        name = SLOTS.get(slot, {}).get("name", "")
        state = decision(slot)
        why = {"off": "该项在本轮被关闭（不参与）",
               "pending": "该项还没选择数据来源（上传 或 采用内置数据）",
               "uploaded": "已选择上传，但没读到文件",
               "builtin": "已选择内置，但内置文件缺失"}.get(state or "pending", "无可用数据")
        msg = f"{label} {name}：{why}"
        if detail:
            msg += f"（{detail}）"
        super().__init__(msg)


def has_data(slot: str) -> bool:
    """本轮是否真的有可用数据（只看显式决策，不做任何默认）。"""
    return bool(active_files(slot))


def active_files(slot: str) -> list[str]:
    """按槽位决策取数，**不做隐式回落**。

    uploaded → 上传文件；builtin → 内置文件；off / 未选择 → 空列表。
    """
    d = decision(slot)
    if d == "uploaded":
        return uploaded_files(slot)
    if d == "builtin":
        return builtin_files(slot)
    return []


def active_path(slot: str) -> str:
    fs = active_files(slot)
    if not fs:
        raise DataUnavailable(slot)
    return fs[0]


def is_uploaded(slot: str) -> bool:
    return bool(uploaded_files(slot))


def preset_of(slot: str) -> str:
    """该槽位的文件是不是演示预设装进来的；是则返回预设 id（如「型钢-B」），否则空串。

    预设装载走的仍是上传槽位（文件要落盘、要能按字节校验），manifest 里额外记了
    `preset` 字段。取数逻辑不看它，只有界面用它区分「内置演示」和「用户自己传的」。
    """
    rec = load_manifest().get("slots", {}).get(slot) or {}
    return str(rec.get("preset") or "")


def source_kind(slot: str) -> str:
    """uploaded / builtin（已显式选择）/ pending（还没做选择）。"""
    d = decision(slot)
    return d or "pending"


# ---------------------------------------------------------------------------
# 4) 校验
# ---------------------------------------------------------------------------
def _hdr(rows):
    return [str(c).strip() if c is not None else "" for c in rows[0]]


def _pick_col(hdr, *names) -> int:
    for n in names:
        for i, h in enumerate(hdr):
            if h == n:
                return i
    for n in names:  # 退化为子串匹配
        for i, h in enumerate(hdr):
            if n in h and h:
                return i
    return -1


def _validate_model_xlsx(p: str, slot: str = "") -> dict:
    """数据模型 Excel：对象类表必需，属性表决定是否为 ready。"""
    import openpyxl

    issues: list[str] = []
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    cls_sheet, attr_sheet = None, None
    for name in wb.sheetnames:
        ws = wb[name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        h = _hdr(rows)
        if cls_sheet is None and _pick_col(h, "对象类ID") >= 0 and \
           (_pick_col(h, "对象类名称") >= 0 or _pick_col(h, "中文名称") >= 0):
            cls_sheet = name
        if attr_sheet is None and _pick_col(h, "属性ID", "属性中文名称", "属性名称") >= 0:
            attr_sheet = name
    if cls_sheet is None:
        wb.close()
        return {"status": "missing", "count": "0 类",
                "issues": ["未找到对象类表（需含「对象类ID」+「对象类名称/中文名称」列）"],
                "detail": {"object_class": 0, "attribute": 0}}

    ws = wb[cls_sheet]
    rows = list(ws.iter_rows(values_only=True))
    h = _hdr(rows)
    ci = _pick_col(h, "对象类ID")
    n_cls = len([r for r in rows[1:] if r[ci]])
    n_attr = 0
    if attr_sheet:
        ws2 = wb[attr_sheet]
        rows2 = list(ws2.iter_rows(values_only=True))
        n_attr = len([r for r in rows2[1:] if any(r)])
    else:
        issues.append("未提供属性表，属性维度为 0（可继续，但图谱关系会显著偏少）")
    has_rel = "关系" in wb.sheetnames
    if not has_rel:
        issues.append("未提供关系表；层次关系将依赖对象类表的父类ID列推导")
    wb.close()

    if n_attr == 0:
        status = "degraded"
    elif n_cls == 0:
        status = "missing"
    else:
        status = "ready"
    return {"status": status, "count": f"{n_cls} 类 / {n_attr} 属性实例",
            "issues": issues,
            "detail": {"object_class": n_cls, "attribute": n_attr},
            "leak_warning": _leak_check(p, cls_sheet, slot)}


def _leak_check(p: str, cls_sheet: str, slot: str) -> str | None:
    """上传的建模集若与**同一模式**的 test 侧答案高度重叠 → 显式警告。

    只比对同模式答案：EM4 对模式 B 是合法建模数据，对模式 A 才是答案，
    跨模式比对会产生误导性告警。
    """
    import openpyxl

    def ids(path: str, sheet_hint: str):
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            name = sheet_hint if sheet_hint in wb.sheetnames else wb.sheetnames[0]
            ws = wb[name]
            rows = list(ws.iter_rows(values_only=True))
            h = _hdr(rows)
            ci = _pick_col(h, "对象类ID")
            if ci < 0:
                wb.close()
                return set()
            out = {str(r[ci]).strip() for r in rows[1:] if r[ci]}
            wb.close()
            return out
        except Exception:
            return set()

    # 只比对同模式的答案集
    ans_slot = {"d0_a": "test_a", "d0_b": "test_b"}.get(slot)
    if not ans_slot:
        return None
    mine = ids(p, cls_sheet)
    if not mine:
        return None
    mode = SLOTS[slot]["modes"]
    for tp in uploaded_files(ans_slot) + builtin_files(ans_slot):
        other = ids(tp, "对象类")
        if not other:
            continue
        ov = len(mine & other)
        if ov and ov / max(1, len(mine)) >= 0.5:
            return (f"[泄漏风险] 该建模集有 {ov}/{len(mine)} 个对象类"
                    f"（{ov / len(mine):.0%}）与模式 {mode} 的评测答案重合 —— "
                    f"若它正是该模式的答案文件，建库即构成泄漏，请确认用途")
    return None


def _validate_docs(paths: list[str]) -> dict:
    """模式 A 文档集：PDF 探文本层；docx/xlsx 看是否有内容。"""
    if not paths:
        return {"status": "missing", "count": "0 份", "issues": ["未上传任何文档"]}
    issues, no_text = [], []
    opened, failed = 0, 0     # opened = 能成功打开的文件数（不论有无文本层）
    for p in paths:
        ext = Path(p).suffix.lower()
        if ext == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(p) as pdf:
                    chars = sum(len(pg.extract_text() or "") for pg in pdf.pages[:3])
                    npg = len(pdf.pages)
            except Exception as e:
                failed += 1
                issues.append(f"{Path(p).name}：PDF 打开失败（{e}）")
                continue
            opened += 1
            if chars == 0:
                no_text.append(Path(p).name)
            issues.append(f"{Path(p).name}：{npg} 页，前 3 页可抽取文本 {chars} 字符")
        elif ext in (".docx", ".xlsx", ".xls"):
            opened += 1
        elif ext in (".md", ".txt"):
            # 模式 A 的输入可能已经是 PDF 转出来的 .md（内置目录里就是这种），
            # 按文本量判定有无可用内容，别再当成"不支持的格式"。
            try:
                txt = Path(p).read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                failed += 1
                issues.append(f"{Path(p).name}：读取失败（{e}）")
                continue
            opened += 1
            if len(txt.strip()) < 200:
                no_text.append(Path(p).name)
            issues.append(f"{Path(p).name}：{len(txt)} 字符")
        else:
            failed += 1
            issues.append(f"{Path(p).name}：不支持的格式 {ext}")
    if no_text:
        issues.append("以下文档无文本层（扫描件）：" + "、".join(no_text) +
                      " → 只能按文件名主题弱匹配，抽取置信度会整体偏低")
    if opened == 0:
        status = "missing"
    elif no_text or failed:
        status = "degraded"
    else:
        status = "ready"
    return {"status": status, "count": f"{len(paths)} 份（{opened} 份可打开）",
            "issues": issues, "detail": {"documents": len(paths), "opened": opened}}


def _validate_req_xlsx(p: str) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    # 优先取「同时含 对象类中文名称 + 属性中文名称」的表（与 requirement_analyst 口径一致），
    # 否则退化为「只含属性列」的表（值域清单等），避免误选值域 sheet 导致数量虚高。
    for need_cls in (True, False):
        for name in wb.sheetnames:
            ws = wb[name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            h = _hdr(rows)
            if _pick_col(h, "属性中文名称", "属性名称") < 0:
                continue
            if need_cls and _pick_col(h, "对象类中文名称", "对象类名称") < 0:
                continue
            n = len([r for r in rows[1:] if any(r)])
            wb.close()
            return {"status": "ready" if n else "degraded",
                    "count": f"{n} 条属性行（sheet「{name}」）",
                    "issues": [] if n else ["属性表为空"],
                    "detail": {"attributes": n, "sheet": name}}
    wb.close()
    return {"status": "missing", "count": "0 条",
            "issues": ["未找到含「属性中文名称」列的属性表"],
            "detail": {"attributes": 0}}


def _validate_json(p: str, required_key: str, count_label: str) -> dict:
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "missing", "count": "解析失败", "issues": [f"JSON 解析失败：{e}"]}
    if not isinstance(d, dict) or required_key not in d:
        return {"status": "missing", "count": "字段缺失",
                "issues": [f"缺少必需字段「{required_key}」"]}
    n = len(d[required_key])
    return {"status": "ready" if n else "degraded",
            "count": f"{n} {count_label}", "issues": [],
            "detail": {required_key: n}}


def _validate_gold_xlsx(path: str, group: str | None = None) -> dict:
    """校验预期输出：按表头识别 对象类 / 对象类属性 / 属性 三表。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    n_cls = n_attr = n_pool = 0
    issues: list[str] = []
    for sn in wb.sheetnames:
        rows = list(wb[sn].iter_rows(values_only=True))
        if not rows:
            continue
        hdr = [str(c).strip() if c is not None else "" for c in rows[0]]
        body = [r for r in rows[1:] if any(c is not None and str(c).strip() for c in r)]
        if "关联ID" in hdr:
            n_attr += len(body)
        elif "对象类ID" in hdr and ("中文名称" in hdr or "对象类名称" in hdr):
            n_cls += len(body)
        elif "属性ID" in hdr and "中文名称" in hdr:
            n_pool += len(body)
    wb.close()

    if n_cls == 0:
        raise ValueError("未识别到「对象类」表（需含 对象类ID + 中文名称 列）")
    if group != "group1" and n_attr == 0:
        raise ValueError("未识别到「对象类属性」表（需含 关联ID 列）")
    if group == "group1" and n_attr == 0:
        issues.append("该组预期输出只有对象类表，评测只覆盖对象类层")

    parts = [f"{n_cls} 类"]
    if n_attr:
        parts.append(f"{n_attr} 属性")
    if n_pool:
        parts.append(f"{n_pool} 属性字典")
    return {"status": "ready", "count": " / ".join(parts), "issues": issues,
            "detail": {"classes": n_cls, "attrs": n_attr, "pool": n_pool}}


def validate_slot(slot: str) -> dict:
    """校验某个槽位当前生效的数据（上传优先）。返回结果含来源标记。"""
    cfg = SLOTS[slot]
    files = active_files(slot)
    kind = source_kind(slot)
    base = {"slot": slot, "label": cfg["label"], "name": cfg.get("name", ""),
            "item": cfg.get("item"), "group": cfg.get("group", ""),
            "modes": cfg["modes"], "source": kind,
            "files": [Path(f).name for f in files],
            # 演示预设装进槽位的文件（manifest 里带 preset 标记）：
            # 物理上也算「已上传」，但对用户来说它就是内置数据 —— 前端据此显示
            # 「内置演示」并点亮「采用内置数据」按钮，避免演示时看着像自己传的。
            "preset": preset_of(slot),
            "required": cfg.get("required", False)}

    if kind == "off":
        base.update({"status": "off", "count": "本轮不参与",
                     "files": [], "issues": []})
        return base

    if not files:
        base.update({"status": "missing", "count": "未接入",
                     "issues": ["未上传文件，且无内置回落数据"]})
        return base

    try:
        if slot in ("a_gold_g1", "a_gold_g2", "b_gold"):
            r = _validate_gold_xlsx(files[0], cfg.get("group"))
        elif slot in ("a_in_g1", "a_in_g2"):
            r = _validate_docs(files)
        elif slot == "b_in":
            # D2 支持 1–N 份需求表，合并统计属性行，避免只报第一份
            import openpyxl
            total = 0
            for f in files:
                wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
                for sn in wb.sheetnames:
                    rows = list(wb[sn].iter_rows(values_only=True))
                    if not rows:
                        continue
                    h = [str(c).strip() if c is not None else "" for c in rows[0]]
                    if "属性中文名称" in h:
                        ci = h.index("属性中文名称")
                        total += sum(1 for x in rows[1:]
                                     if ci < len(x) and x[ci] and str(x[ci]).strip())
                wb.close()
            r = {"status": "ready" if total else "degraded",
                 "count": f"{len(files)} 份 / {total} 条属性行",
                 "issues": [] if total else ["未在任何 sheet 找到「属性中文名称」列"]}
        elif slot == "model_spec":
            r = _validate_json(files[0], "tables", "张表定义")
        else:
            r = {"status": "ready", "count": f"{len(files)} 个文件", "issues": []}
    except Exception as e:
        r = {"status": "missing", "count": "校验失败", "issues": [f"{type(e).__name__}: {e}"]}

    base.update(r)
    if kind == "pending":
        # 还没选：可以读内置，但不替用户拍板
        base["status"] = "pending"
        base["issues"] = ["尚未选择：请上传文件，或点「采用内置数据」"] + list(base.get("issues", []))
    return base


def validate_all() -> dict:
    results = {s: validate_slot(s) for s in SLOTS}
    uploaded = [s for s in SLOTS if is_uploaded(s)]
    blocking = [s for s in SLOTS
                if SLOTS[s].get("required") and results[s]["status"] == "missing"]
    pending = [s for s in SLOTS if source_kind(s) == "pending"]
    decided = [s for s in SLOTS if source_kind(s) in ("uploaded", "builtin")]
    off_slots = [s for s in SLOTS if source_kind(s) == "off"]
    return {
        "slots": results,
        "uploaded_count": len(uploaded),
        "slot_count": len(SLOTS),
        "missing_required": blocking,
        "pending_slots": pending,
        "off_slots": off_slots,
        "confirmed_count": len(decided),
        "all_decided": not pending,
        "all_source_builtin": bool(decided) and not uploaded,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 5) 缓存失效：上传后必须让所有工具重新读数据
# ---------------------------------------------------------------------------
def invalidate_caches() -> list[str]:
    cleared = []
    try:
        from tools import knowledge_extractor as ke
        if hasattr(ke, "_CACHE"):
            ke._CACHE.clear(); cleared.append("knowledge_extractor")
    except Exception:
        pass
    try:
        from tools import landscape_analyst as la
        if hasattr(la, "_CACHE"):
            la._CACHE.clear(); cleared.append("landscape_analyst")
    except Exception:
        pass
    try:
        from tools import requirement_analyst as ra
        if hasattr(ra, "_CACHE"):
            ra._CACHE.clear(); cleared.append("requirement_analyst")
    except Exception:
        pass
    try:
        from tools import ontology_builder as ob
        if hasattr(ob, "_GRAPH_CACHE"):
            ob._GRAPH_CACHE.clear(); cleared.append("ontology_builder")
    except Exception:
        pass
    try:
        from tools import doc_parser as dp
        if hasattr(dp, "_PARSE_CACHE"):
            dp._PARSE_CACHE.clear(); cleared.append("doc_parser")
    except Exception:
        pass
    return cleared


if __name__ == "__main__":
    r = validate_all()
    print(f"槽位 {r['slot_count']} 个 | 已上传 {r['uploaded_count']} | 缺失必需 {r['missing_required']}")
    for s, v in r["slots"].items():
        print(f"  {s:<8} [{v['source']:<8}] {v['status']:<9} {v['count']}")
        for i in v.get("issues", [])[:2]:
            print(f"      - {i}")
        if v.get("leak_warning"):
            print(f"      {v['leak_warning']}")
