"""
archive_manager.py
==================
历史梳理存档：把"一次完整梳理"的输入、预期输出、生成结果、评测结果打成自包含快照。

设计边界（用户确认）：
- **只读**：归档写入后，任何生成 / 评测链路都不再从归档读数据；
- **不进图谱 / RAG / 缓存**，也不改变 P0–P7 的取数优先级（上传 > 内置）；
- 唯一回到主流程的通道是**显式「恢复为当前数据集」**：把归档的输入/预期输出复制进上传槽位，
  原归档不变。

目录结构：
    deliverables/archive/{mode}-{YYYYMMDD-HHMMSS}/
    ├── meta.json
    ├── input/     原始输入副本
    ├── gold/      预期输出副本（可能为空：D3 可选）
    ├── output/    生成的三表 xlsx
    ├── report.md  评测报告（有评测时）
    └── match_detail.csv
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import dataset_registry as registry  # noqa: E402
from tools import eval_pipeline as ep           # noqa: E402
from tools import model_exporter as mex         # noqa: E402
from tools import upload_manager as um          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DELIVERABLES = ROOT / "deliverables"
ARCHIVE_DIR = DELIVERABLES / "archive"
EVAL_DIR = DELIVERABLES / "evaluation"
RULE_FILE = ROOT / "data" / "modeling_rules.json"

SLOT_OF = {
    ("A", "input"): "a_in_g1",
    ("A", "gold"): "a_gold_g1",
    ("B", "input"): "b_in",
    ("B", "gold"): "b_gold",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _sha256(p: str | Path, limit: int = 0) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _file_meta(p: str | Path) -> dict:
    p = Path(p)
    return {"name": p.name, "size": p.stat().st_size, "sha256": _sha256(p)}


def _rule_version() -> str:
    try:
        return json.loads(RULE_FILE.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        return ""


def _extract_eval(result: dict) -> dict:
    """从 evaluator 结果里抽归档需要的字段。"""
    return {
        "evaluable": bool(result.get("evaluable")),
        "unevaluable_reason": result.get("unevaluable_reason", ""),
        "class": (result.get("class_layer") or {}).get("metrics"),
        "attr": (result.get("attr_layer") or {}).get("metrics"),
        "overall": result.get("overall") or {},
        "generated_at": result.get("generated_at", ""),
    }


def _status_of(eval_info: dict | None) -> str:
    if not eval_info:
        return "generated"
    return "complete" if eval_info.get("evaluable") else "unevaluable"


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def save(mode: str, title: str | None = None, eval_snapshot: dict | None = None) -> dict:
    """把当前数据集的一次梳理归档。eval_snapshot 为 P7 的当前结果（可选）。"""
    ds = registry.resolve(mode)
    m, g = ds["mode"], ds["group"]
    now = datetime.now()
    aid = f"{m}-{now:%Y%m%d-%H%M%S}"
    d = ARCHIVE_DIR / aid
    for sub in ("input", "gold", "output"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    input_meta = []
    for f in ds["input_files"]:
        shutil.copy2(f, d / "input" / Path(f).name)
        input_meta.append(_file_meta(f))

    gold_meta = []
    for f in ds["gold_files"]:
        shutil.copy2(f, d / "gold" / Path(f).name)
        gold_meta.append(_file_meta(f))

    pred = ep.build_pred(m, g)
    counts = {"classes": len(pred.get("classes") or []),
              "attrs": len(pred.get("attrs") or [])}

    model_name = ""
    model_note = ""
    try:
        p, _meta = mex.export_xlsx(m, g)
        dst = d / "output" / f"数据模型-模式{m}-三表.xlsx"
        shutil.copy2(p, dst)
        model_name = dst.name
    except Exception as e:
        model_note = f"三表导出失败：{e}"

    eval_info = None
    eval_source = "none"
    if eval_snapshot:
        eval_info = _extract_eval(eval_snapshot)
        eval_source = "p7"
    else:
        latest = EVAL_DIR / f"latest-{m}-{g}.json"
        if latest.exists():
            try:
                eval_info = _extract_eval(json.loads(latest.read_text(encoding="utf-8")))
                eval_source = "latest"
            except Exception:
                eval_info = None

    if eval_info:
        for src, dst in ((EVAL_DIR / "test_report.md", d / "report.md"),
                         (EVAL_DIR / "match_detail.csv", d / "match_detail.csv")):
            if src.exists():
                shutil.copy2(src, dst)

    meta = {
        "id": aid,
        "mode": m,
        "group": g,
        "title": (title or aid).strip(),
        "created_at": now.isoformat(timespec="seconds"),
        "input_files": input_meta,
        "gold_files": gold_meta,
        "counts": counts,
        "model_file": model_name,
        "model_note": model_note,
        "metrics": eval_info,
        "eval_source": eval_source,
        "status": _status_of(eval_info),
        "rule_version": _rule_version(),
        "note": "",
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _read_meta(d: Path) -> dict | None:
    p = d / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_archives(mode: str | None = None) -> list[dict]:
    if not ARCHIVE_DIR.exists():
        return []
    out = []
    for d in ARCHIVE_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = _read_meta(d)
        if not meta:
            continue
        if mode and meta.get("mode") != str(mode).upper():
            continue
        out.append(meta)
    return sorted(out, key=lambda x: x.get("created_at", ""), reverse=True)


def get(aid: str) -> dict:
    d = ARCHIVE_DIR / aid
    meta = _read_meta(d) if d.is_dir() else None
    if not meta:
        raise KeyError(f"未找到归档 {aid}")
    files = {}
    for sub in ("input", "gold", "output"):
        p = d / sub
        files[sub] = [f.name for f in sorted(p.iterdir())] if p.exists() else []
    for name in ("report.md", "match_detail.csv"):
        if (d / name).exists():
            files.setdefault("root", []).append(name)
    meta = dict(meta)
    meta["files"] = files
    return meta


def archive_file(aid: str, kind: str, name: str | None = None) -> Path:
    """返回归档内某个文件的路径。kind: model / report / csv / input / gold。"""
    d = ARCHIVE_DIR / aid
    if kind == "report":
        p = d / "report.md"
    elif kind == "csv":
        p = d / "match_detail.csv"
    elif kind == "model":
        xs = sorted((d / "output").glob("*.xlsx")) if (d / "output").exists() else []
        p = xs[-1] if xs else None
    elif kind in ("input", "gold"):
        if not name:
            raise ValueError("缺少文件参数 name")
        p = d / kind / Path(name).name
    else:
        raise ValueError(f"未知文件类型：{kind}")
    if not p or not p.exists():
        raise FileNotFoundError("归档中不存在该文件")
    return p


def _rmtree(path: Path, tries: int = 4) -> None:
    """Windows 上刚读写过的文件可能短暂被占用，重试几次再兜底。"""
    import stat
    import time
    for i in range(tries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            for f in path.rglob("*"):
                try:
                    f.chmod(stat.S_IWRITE)
                except Exception:
                    pass
            time.sleep(0.2 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def delete(aid: str) -> dict:
    d = ARCHIVE_DIR / aid
    if not d.is_dir():
        raise KeyError(f"未找到归档 {aid}")
    _rmtree(d)
    if d.exists():
        raise PermissionError("文件被占用，请稍后重试")
    return {"deleted": aid}


# ---------------------------------------------------------------------------
# 恢复为当前数据集
# ---------------------------------------------------------------------------
def restore(aid: str) -> dict:
    """把归档的输入/预期输出复制进上传槽位，替换当前数据集。原归档不变。"""
    meta = get(aid)
    m = meta["mode"]
    d = ARCHIVE_DIR / aid
    restored = []

    pairs = [("input", SLOT_OF[(m, "input")]), ("gold", SLOT_OF[(m, "gold")])]
    for kind, slot in pairs:
        src = d / kind
        if not src.exists():
            continue
        items = []
        for f in sorted(src.iterdir()):
            if f.is_file():
                items.append((f.name, f.read_bytes()))
        if not items:
            continue
        um.save_files(slot, items)
        restored.append({"slot": slot, "files": [n for n, _ in items]})

    um.invalidate_caches()
    return {"id": aid, "mode": m, "restored": restored}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["save", "list", "get", "restore", "delete"])
    ap.add_argument("--mode", default="B")
    ap.add_argument("--id", default="")
    a = ap.parse_args()
    if a.action == "save":
        print(json.dumps(save(a.mode), ensure_ascii=False, indent=2))
    elif a.action == "list":
        for x in list_archives(a.mode or None):
            print(x["id"], x["status"], x["counts"])
    elif a.action == "get":
        print(json.dumps(get(a.id), ensure_ascii=False, indent=2))
    elif a.action == "restore":
        print(json.dumps(restore(a.id), ensure_ascii=False))
    elif a.action == "delete":
        print(json.dumps(delete(a.id), ensure_ascii=False))
