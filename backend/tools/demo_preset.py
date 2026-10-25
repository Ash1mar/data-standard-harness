"""
demo_preset.py
==============
内置演示数据集 + 「一键秒演示」缓存（不碰任何用户数据）。

背景（2026-10-24 用户要求）
---------------------------
比赛现场要按 P0→P7 点一遍按钮，但真实链路里有两类耗时无法接受：
  · P2 抽取要问模型裁决置信度（一轮 2–4 分钟）
  · P3 类间关系分析要 26 批 × 3~6 分钟（近 19 分钟）
于是把「已经真实跑过一遍」的结果按 **阶段** 缓存下来，演示时直接回放。

三段职责
--------
1. **预设与内置文件**：`data/_presets/<PID>/p0/` 存 P0 三件套（两份 xlsx + 业务规则），
   `install_p0()` 把它们放进上传槽位 —— 这就是"P0 自动装载内置数据"。
2. **阶段缓存（录制/回放）**：对白名单里的 POST 接口，按 `path + 请求体` 摘要把
   **真实响应**（SSE 全量事件流 / JSON）落盘到 `trace/`。演示态命中即回放：
   不调模型、不联网、不重算，秒出且与真实跑的一致。
3. **产物快照**：`snapshot/` 存图谱、标准、最终模型、评测报告等文件；
   演示态激活时铺进工作目录，退出时把用户原有文件**原样还原**。

隔离原则（用户硬要求：不能污染用户数据）
--------------------------------------
· 只有 `activate()` 之后才读写预设目录；没启用时预设内容对界面完全不可见。
· 激活时先把会被覆盖的用户文件备份到 `_backup/<时间戳>/`，`deactivate()` 按
  `restore.json` 逐个还原（原本不存在的就删掉）。
· 用户一旦在 P0 上传自己的文件 → 自动退出演示态（`on_user_upload()`）。

本模块只依赖标准库，不做业务判断，避免与 tools/* 循环导入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = ROOT / "data"

PRESET_HOME = DATA_ROOT / "_presets"
PID = "型钢-B"
PRESET_DIR = PRESET_HOME / PID
STATE_FILE = PRESET_HOME / "active.json"

TITLE = "模式 B · 大宗材料类-型钢"
SUBTITLE = "需求文件 + EM4 预期输出 + 建模规则（真实跑通的一遍结果）"

# P0 三件套：前两份进上传槽位，第三份是业务规则（P6 建模规则，代码内置同源）
P0_FILES = [
    {"name": "大宗材料类-型钢.xlsx", "slot": "b_in", "label": "D2 需求文件"},
    {"name": "数据模型-大宗材料类-EM4型钢.xlsx", "slot": "b_gold", "label": "D3 预期输出"},
    {"name": "规则.md", "slot": "", "label": "业务规则"},
]

# 快照范围：只收「跑完一遍后各阶段会读的产物」，不收预览图/临时文件
ARTIFACTS = [
    "data/uploaded",
    "data/.parse_cache",
    "data/.llm_cache",
    "data/knowledge_graph_B.json",
    "data/graph",
    "data/readiness_status.json",
    "deliverables/shared",
    "deliverables/mode-b",
    "deliverables/evaluation",
    "deliverables/archive",
    "deliverables/exports",
    "deliverables/llm",
]

# 只有「计算型」接口参与录制/回放。
# 上传、门控处置、清空/重置、入库审批一律**不**缓存 —— 那些是用户当场做的决定，
# 回放一个旧响应会让界面显得"点了没反应"，比慢更糟。
REPLAY_PATHS = {
    "/api/parse",
    "/api/extract",
    "/api/graph/build",
    "/api/graph/query",
    "/api/graph/relations/analyze",
    "/api/landscape/build",
    "/api/landscape/prioritize",
    "/api/requirement/parse",
    "/api/requirement/gap",
    "/api/requirement/clarify",
    "/api/requirement/conflict/resolve",
    "/api/requirement/match",
    "/api/requirement/synthesize",
    "/api/standard/generate",
    "/api/evolve/analyze",
    "/api/eval/run",
    "/api/data/validate",
    "/api/data/readiness",
    "/api/session/start",
}

_DEFAULT_STATE = {"active": False, "capture": False, "pid": PID, "updated_at": ""}

_LABELS = {
    "/api/parse": "P1 文档解析",
    "/api/extract": "P2 知识抽取",
    "/api/graph/build": "P3 底座构建",
    "/api/graph/relations/analyze": "P3 类间关系分析",
    "/api/landscape/build": "P4 场景梳理",
    "/api/requirement/parse": "P5 需求解析",
    "/api/requirement/gap": "P5 完备性分析",
    "/api/requirement/clarify": "P5 澄清推断",
    "/api/requirement/conflict/resolve": "P5 冲突清单",
    "/api/requirement/match": "P5 标准匹配",
    "/api/requirement/synthesize": "P5 模型合成",
    "/api/standard/generate": "P6 标准生成",
    "/api/evolve/analyze": "P6 演化匹配",
    "/api/eval/run": "P7 准确率评测",
    "/api/data/validate": "P0 数据校验",
    "/api/session/start": "会话启动",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except Exception:
        return ""


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except Exception:
        return 0


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return str(p)


def _state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            out = dict(_DEFAULT_STATE)
            out.update(d)
            return out
    except Exception:
        pass
    return dict(_DEFAULT_STATE)


def _set_state(**kw) -> dict:
    d = _state()
    d.update(kw)
    d["pid"] = PID
    d["updated_at"] = _now()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return d


def is_active() -> bool:
    return bool(_state().get("active"))


def is_capturing() -> bool:
    return bool(_state().get("capture"))


def interceptable(path: str) -> bool:
    return path in REPLAY_PATHS


# ---------------------------------------------------------------------------
# 状态 / 清单
# ---------------------------------------------------------------------------
def _manifest() -> dict:
    try:
        return json.loads((PRESET_DIR / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def available() -> bool:
    """预设是否完整：P0 三件套 + 至少一份快照。"""
    m = _manifest()
    if not m:
        return False
    p0_ok = all((PRESET_DIR / "p0" / f["name"]).exists() for f in P0_FILES)
    return bool(p0_ok)


def _stage_order(path: str) -> int:
    """按流水线顺序排（P1→P7），界面上读起来是一条动线而不是按字母。"""
    order = [
        "/api/data/validate", "/api/session/start",
        "/api/parse", "/api/extract",
        "/api/graph/build", "/api/graph/relations/analyze", "/api/graph/query",
        "/api/landscape/build", "/api/landscape/prioritize",
        "/api/requirement/parse", "/api/requirement/gap", "/api/requirement/clarify",
        "/api/requirement/match", "/api/requirement/conflict/resolve",
        "/api/requirement/synthesize",
        "/api/standard/generate", "/api/evolve/analyze",
        "/api/eval/run",
    ]
    return order.index(path) if path in order else 99


def traces() -> list[dict]:
    """列出已录制的阶段缓存（按接口归并，取最近一次）。"""
    d = PRESET_DIR / "trace"
    out: dict[str, dict] = {}
    if not d.exists():
        return []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        path = rec.get("path") or ""
        prev = out.get(path)
        if prev and str(prev.get("recorded_at")) >= str(rec.get("recorded_at")):
            continue
        out[path] = {
            "path": path,
            "label": _LABELS.get(path, path),
            "recorded_at": rec.get("recorded_at"),
            "bytes": _size(f),
            "status": rec.get("status"),
            "body": (rec.get("body_preview") or "")[:160],
            "chunks": len(rec.get("chunks") or []),
        }
    return sorted(out.values(), key=lambda x: _stage_order(x["path"]))


def status() -> dict:
    st = _state()
    m = _manifest()
    p0 = []
    for f in P0_FILES:
        src = PRESET_DIR / "p0" / f["name"]
        cur = (DATA_ROOT / "uploaded" / f["slot"] / f["name"]) if f["slot"] else (DATA_ROOT / f["name"])
        p0.append({
            "name": f["name"], "slot": f["slot"], "label": f["label"],
            "in_preset": src.exists(),
            "loaded": cur.exists() and (not src.exists() or _sha_file(cur) == _sha_file(src)),
        })
    snap_dir = PRESET_DIR / "snapshot"
    tr = traces()
    return {
        "available": available(),
        "active": bool(st.get("active")),
        "capture": bool(st.get("capture")),
        "pid": PID,
        "title": m.get("title") or TITLE,
        "subtitle": m.get("subtitle") or SUBTITLE,
        "created_at": m.get("created_at") or "",
        "snapshot_at": m.get("snapshot_at") or "",
        "snapshot_mb": round(sum(_size(p) for p in snap_dir.rglob("*") if p.is_file()) / 1048576, 1)
        if snap_dir.exists() else 0,
        "has_snapshot": bool(snap_dir.exists() and any(snap_dir.rglob("*"))),
        "p0": p0,
        "stages": tr,
        "stage_count": len(tr),
        "model_outputs": _model_note(),
        "dir": _rel(PRESET_DIR),
        "updated_at": st.get("updated_at"),
    }


def _model_note() -> str:
    """模型输出落盘情况：P2 的置信度裁决缓存里有几条真实模型判断。"""
    p = DATA_ROOT / ".llm_cache" / "confidence.json"
    n = 0
    try:
        n = len(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        n = 0
    return f"P2 置信度裁决 {n} 条（含模型理由）" if n else "尚无模型输出缓存"


# ---------------------------------------------------------------------------
# 1) P0 内置文件装载
# ---------------------------------------------------------------------------
def install_p0() -> dict:
    """把预设里的 P0 三件套放进上传槽位，并登记到 manifest（= 视为已上传）。"""
    out = {"installed": [], "missing": []}
    man_path = DATA_ROOT / "uploaded" / "manifest.json"
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception:
        man = {"slots": {}, "decisions": {}}
    man.setdefault("slots", {})
    man.setdefault("decisions", {})
    changed = False
    for f in P0_FILES:
        src = PRESET_DIR / "p0" / f["name"]
        if not src.exists():
            out["missing"].append(f["name"])
            continue
        if not f["slot"]:
            continue  # 业务规则文件不发槽位
        dst_dir = DATA_ROOT / "uploaded" / f["slot"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f["name"]
        if not dst.exists() or _sha_file(dst) != _sha_file(src):
            shutil.copy2(src, dst)
        man["slots"][f["slot"]] = {"files": [f["name"]], "uploaded_at": _now(),
                                   "preset": PID}
        man["decisions"].pop(f["slot"], None)
        out["installed"].append(f"{f['slot']} ← {f['name']}")
        changed = True
    if changed:
        man["updated_at"] = _now()
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 2) 阶段缓存：录制 / 回放
# ---------------------------------------------------------------------------
def _canon(body: bytes) -> bytes:
    """请求体规范化：JSON 先解析再按键排序重排，抹掉空格/键序差异。

    必须做这一步：回放命中是按「path + 请求体」摘要匹配的，而浏览器的
    `JSON.stringify` 产出紧凑格式（`{"mode":"B"}`），脚本/curl 往往带空格
    （`{"mode": "B"}`）。不归一化就会出现"明明录过却命中不了"。
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except Exception:
        return body


def _trace_key(path: str, body: bytes) -> str:
    return hashlib.sha1(path.encode("utf-8") + b"\x00" + _canon(body)).hexdigest()[:16]


def _trace_file(path: str, body: bytes) -> Path:
    slug = path.strip("/").replace("/", "_")
    return PRESET_DIR / "trace" / f"{slug}.{_trace_key(path, body)}.json"


def lookup(path: str, body: bytes) -> dict | None:
    """命中返回录制内容（status / headers / chunks），否则 None。"""
    f = _trace_file(path, body)
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not rec.get("chunks"):
        return None
    return rec


def record(path: str, body: bytes, *, status: int, headers: list, chunks: list[bytes],
           elapsed_ms: int = 0) -> bool:
    """把一次真实响应落盘。二进制响应（xlsx 等）不录。"""
    if not chunks:
        return False
    try:
        text_chunks = [c.decode("utf-8") for c in chunks]
    except UnicodeDecodeError:
        return False
    hdrs = []
    for k, v in headers or []:
        try:
            kk = k.decode("latin-1").lower() if isinstance(k, bytes) else str(k).lower()
            vv = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        except Exception:
            continue
        if kk in ("content-length", "transfer-encoding", "connection", "date"):
            continue   # 回放时重新算，留着会打架
        hdrs.append([kk, vv])
    rec = {
        "path": path,
        "key": _trace_key(path, body),
        "body_preview": body.decode("utf-8", "ignore")[:400],
        "status": int(status or 200),
        "headers": hdrs,
        "chunks": text_chunks,
        "bytes": sum(len(c) for c in text_chunks),
        "elapsed_ms": int(elapsed_ms),
        "recorded_at": _now(),
    }
    try:
        f = _trace_file(path, body)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


async def replay(rec: dict, send) -> None:
    """把录制内容原样吐回去；SSE 逐块小间隔发出，界面看起来仍是"边跑边出"。"""
    headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in rec.get("headers") or []]
    await send({"type": "http.response.start", "status": int(rec.get("status") or 200),
                "headers": headers})
    chunks = rec.get("chunks") or []
    paced = len(chunks) > 1
    for i, c in enumerate(chunks):
        if paced:
            # 事件多的时候压缩间隔：整体控制在 ~2.5s 内出完，够"在动"又不耽误演示
            await asyncio.sleep(min(0.02, 2.5 / max(len(chunks), 1)))
        await send({"type": "http.response.body", "body": c.encode("utf-8"),
                    "more_body": i < len(chunks) - 1})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


# ---------------------------------------------------------------------------
# 3) 产物快照：保存 / 铺开 / 还原
# ---------------------------------------------------------------------------
def _iter_files(rel: str):
    p = ROOT / rel
    if p.is_file():
        yield p
    elif p.is_dir():
        for f in p.rglob("*"):
            if f.is_file():
                yield f


def save_snapshot() -> dict:
    """把当前工作目录里的产物复制进预设（录制完成后调用）。"""
    snap = PRESET_DIR / "snapshot"
    if snap.exists():
        shutil.rmtree(snap, ignore_errors=True)
    count = 0
    total = 0
    files = []
    for rel in ARTIFACTS:
        for src in _iter_files(rel):
            dst = snap / _rel(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
            total += _size(src)
            files.append(_rel(src))
    man = _manifest()
    man.update({
        "pid": PID,
        "title": man.get("title") or TITLE,
        "subtitle": man.get("subtitle") or SUBTITLE,
        "created_at": man.get("created_at") or _now(),
        "snapshot_at": _now(),
        "artifact_files": len(files),
        "artifact_mb": round(total / 1048576, 1),
        "artifacts": ARTIFACTS,
        "replay_paths": sorted(REPLAY_PATHS),
    })
    try:
        p0dir = PRESET_DIR / "p0"
        p0dir.mkdir(parents=True, exist_ok=True)
        man["p0"] = [{"name": f["name"], "slot": f["slot"], "sha256": _sha_file(p0dir / f["name"]),
                      "bytes": _size(p0dir / f["name"])} for f in P0_FILES]
        (PRESET_DIR / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"files": count, "mb": round(total / 1048576, 1), "snapshot_at": man["snapshot_at"]}


def _backup_root() -> Path:
    return PRESET_DIR / "_backup" / datetime.now().strftime("%Y%m%d-%H%M%S")


def restore_snapshot(backup: bool = True) -> dict:
    """把快照铺进工作目录；被覆盖的用户文件先备份，退出时还原。"""
    snap = PRESET_DIR / "snapshot"
    if not snap.exists():
        return {"restored": 0, "note": "预设里还没有快照"}
    bk = _backup_root() if backup else None
    restore_map = {}
    n = 0
    for src in snap.rglob("*"):
        if not src.is_file():
            continue
        rel = str(src.relative_to(snap)).replace("\\", "/")
        dst = ROOT / rel
        if bk is not None:
            if dst.exists():
                b = bk / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, b)
                restore_map[rel] = _rel(b)
            else:
                restore_map[rel] = ""
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    if bk is not None:
        (bk / "restore.json").write_text(
            json.dumps(restore_map, ensure_ascii=False, indent=2), encoding="utf-8")
        _set_state(backup_dir=_rel(bk))
        _prune_backups(keep=2)
    return {"restored": n, "backup_dir": _rel(bk) if bk else ""}


def _prune_backups(keep: int = 2) -> None:
    """备份目录很占地方（每次一份产物快照的量级），只留最近几次。"""
    root = PRESET_DIR / "_backup"
    if not root.exists():
        return
    dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda d: d.name)
    for d in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(d, ignore_errors=True)


def restore_user() -> dict:
    """按最近一次备份把用户原有文件放回去（演示态退出时调用）。"""
    st = _state()
    bdir = st.get("backup_dir") or ""
    if not bdir:
        return {"restored": 0, "note": "没有备份记录"}
    bk = ROOT / bdir
    try:
        restore_map = json.loads((bk / "restore.json").read_text(encoding="utf-8"))
    except Exception:
        return {"restored": 0, "note": "备份清单缺失"}
    n = 0
    for rel, bak in restore_map.items():
        dst = ROOT / rel
        if bak:
            src = ROOT / bak
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
        else:
            try:
                if dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
            except Exception:
                pass
            n += 1
    _set_state(backup_dir="", active=False, capture=False, restored_at="")
    return {"restored": n, "from": bdir}


# ---------------------------------------------------------------------------
# 4) 生命周期
# ---------------------------------------------------------------------------
def activate(restore: bool = True) -> dict:
    """启用演示态：铺快照 → 装 P0。

    重复调用不重复备份：已经启用过（存在 backup_dir）就直接复用那份原始备份，
    否则第二次激活会把"预设文件"当成用户数据备份下来，退出时就还原不回去了。
    """
    if not available():
        return {"ok": False, "error": "预设不完整：缺 P0 文件或 manifest"}
    st = _state()
    already = bool(st.get("active")) and bool(st.get("backup_dir"))
    snap = {"restored": 0, "note": "已处于演示态，沿用原备份"}
    if restore and not already:
        snap = restore_snapshot()
    p0 = install_p0()   # 放在快照之后：快照里的 manifest 会被覆盖，重建一次才准
    _set_state(active=True, capture=False, restored_at=_now())
    return {"ok": True, "p0": p0, "snapshot": snap, "status": status()}


def deactivate() -> dict:
    """退出演示态：还原用户数据。"""
    r = restore_user()
    _set_state(active=False, capture=False)
    return {"ok": True, "restore": r, "status": status()}


def ensure_active() -> dict:
    """进程启动时调用：演示态开启过就自动装好 P0 与快照（演示机免手动）。

    已经铺过一次快照（`restored_at` 在）就不再重复铺 —— 重复铺会新开一份备份，
    把上一份真正的"用户原始数据"备份顶掉。
    """
    st = _state()
    if not st.get("active") or not available():
        return {"ok": False}
    if st.get("restored_at"):
        return {"ok": True, "p0": install_p0(), "snapshot": {"restored": 0, "note": "已铺过"}}
    snap = restore_snapshot()
    p0 = install_p0()
    _set_state(restored_at=_now())
    return {"ok": True, "p0": p0, "snapshot": snap}


def on_user_upload(slot: str = "", name: str = "") -> dict:
    """用户自己传了文件 → 立刻退出演示态（缓存不可见、不污染用户数据）。"""
    if not is_active():
        return {"ok": True, "deactivated": False}
    r = deactivate()
    return {"ok": True, "deactivated": True, "slot": slot, "name": name,
            "restore": r.get("restore")}


# ---------------------------------------------------------------------------
# 5) ASGI 中间件：一次挂载，覆盖全部计算型接口
# ---------------------------------------------------------------------------
class DemoMiddleware:
    """POST 请求的录制/回放。

    用原生 ASGI 而不是 BaseHTTPMiddleware：后者读完 body 后下游就拿不到请求体了，
    而这里必须"看一眼请求体算摘要"再把原样的 body 喂给下游。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path not in REPLAY_PATHS:
            return await self.app(scope, receive, send)
        st = _state()
        if not (st.get("active") or st.get("capture")):
            return await self.app(scope, receive, send)

        body = b""
        while True:
            msg = await receive()
            body += msg.get("body") or b""
            if not msg.get("more_body"):
                break

        fed = {"done": False}

        async def recv():
            if not fed["done"]:
                fed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if st.get("capture"):
            t0 = time.time()
            meta: dict = {}
            chunks: list[bytes] = []

            async def send_cap(message):
                t = message.get("type")
                if t == "http.response.start":
                    meta["status"] = message.get("status", 200)
                    meta["headers"] = message.get("headers") or []
                elif t == "http.response.body":
                    b = message.get("body") or b""
                    if b:
                        chunks.append(b)
                await send(message)

            await self.app(scope, recv, send_cap)
            try:
                record(path, body, status=meta.get("status", 200),
                       headers=meta.get("headers") or [], chunks=chunks,
                       elapsed_ms=int((time.time() - t0) * 1000))
            except Exception:
                pass
            return

        rec = lookup(path, body)
        if rec:
            await replay(rec, send)
            return
        await self.app(scope, recv, send)
