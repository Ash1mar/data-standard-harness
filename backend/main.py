"""
main.py
=======
核电数据标准智能治理智能体（双模式）· 后端入口。

FastAPI + SSE 骨架（阶段 0）：
- 静态托管前端（P0）
- GET  /api/data/status        数据就绪状态快照（静态 + 上传槽位实时校验）
- POST /api/data/readiness     SSE 逐项正确输出就绪校验结果（SKILL 4.1）
- POST /api/data/upload        现场上传（multipart）→ 落盘 + 结构校验 + 缓存失效
- POST /api/data/validate      SSE 逐槽位广播校验结果
- GET  /api/data/upload/status 各槽位状态
- POST /api/data/upload/reset  清空上传、回落内置数据
- POST /api/session/start      创建会话 + 广播模式选择事件（[A]/[B]）
- POST /api/pipeline/run       SSE 跑通 A/B 流水线骨架
- GET  /api/events             SSE 全局事件总线（P0 执行轨迹流）

增量 3（P3 双知识底座）新增：
- POST /api/graph/build        SSE 构建参考底座 → OWL + 图谱（json_to_owl / graph_upsert）
- POST /api/graph/query        SSE 语义推理查询（graph_query / rag_graph，高亮子图）
- GET  /api/graph/data|stats|owl|node/{id}   图谱画布数据 / 统计 / OWL 全文 / 节点详情

所有交互经 harness_bridge 封装，便于后续增量替换真实工具实现。
"""

from __future__ import annotations

import json
import os
import sys
import asyncio
import tempfile
from pathlib import Path

# 确保 backend/ 在 sys.path 中，便于导入 harness_bridge
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import harness_bridge as bridge
from tools import doc_parser as dp
from tools import knowledge_extractor as ke
from tools import ontology_builder as ob
from tools import landscape_analyst as la
from tools import requirement_analyst as ra
from tools import model_synthesizer as ms
from tools import standard_generator as sg
from tools import standard_evolver as es
from tools import model_exporter as mex
from tools import archive_manager as am
from tools import upload_manager as um
from tools import evaluator as ev
from tools import llm_client as llm
from tools import confidence as cf
from tools import dataset_registry as registry
from tools import class_relations as cr
from tools import demo_preset as demo

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
INDEX_HTML = FRONTEND / "index.html"
VENDOR_DIR = FRONTEND / "vendor"   # Vue / Cytoscape 本地内置，不依赖 CDN

app = FastAPI(title="核电数据标准智能治理智能体 · 双模式", version="0.1.0")

# 允许跨域（前端可能以 file:// 或独立端口打开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内置演示数据的阶段缓存：演示态下 POST 计算接口直接回放真实跑过的响应（断网也秒出）。
# 必须挂在 CORS 之后（= 更外层），这样录下来的响应头里已经带了 CORS。
app.add_middleware(demo.DemoMiddleware)


@app.on_event("startup")
async def _demo_startup():
    """演示态开启过就自动装好 P0 内置文件与产物快照（演示机不用手动点）。"""
    try:
        r = demo.ensure_active()
        if r.get("ok"):
            um.invalidate_caches()
            print(f"[demo] 演示态已就绪：{r.get('p0')} 快照 {r.get('snapshot')}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[demo] 启动装载失败：{e}", flush=True)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class SessionStart(BaseModel):
    mode: str  # "A" | "B"


class PipelineRun(BaseModel):
    mode: str  # "A" | "B"
    session_id: str | None = None


class ParseSample(BaseModel):
    mode: str = "A"  # "A" | "B"
    source: str = "sample"


# ---------------------------------------------------------------------------
# 前端托管
# ---------------------------------------------------------------------------
# 本地内置依赖（Vue / Cytoscape）：比赛现场断网也必须可用，故不走 CDN
if VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


@app.get("/vendor-status")
async def vendor_status():
    """自检：确认本地依赖齐全（缺文件则前端会退化成原始模板）。"""
    need = ["vue.global.prod.js", "cytoscape.min.js"]
    optional = ["layout-base.js", "cose-base.js", "cytoscape-fcose.js"]  # 缺了退回内置 cose
    files = {n: (VENDOR_DIR / n).exists() for n in need}
    opt = {n: (VENDOR_DIR / n).exists() for n in optional}
    return JSONResponse({"dir": str(VENDOR_DIR), "exists": VENDOR_DIR.exists(),
                         "files": files, "optional": opt,
                         "fcose": all(opt.values()),
                         "ok": all(files.values())})


@app.get("/")
async def index():
    if not INDEX_HTML.exists():
        return JSONResponse({"error": "前端未构建：缺少 frontend/index.html"}, status_code=503)
    # 禁用缓存：避免浏览器缓存旧版前端，导致按钮/事件绑定失效却看不到变化
    return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store, must-revalidate"})


# ---------------------------------------------------------------------------
# 数据就绪
# ---------------------------------------------------------------------------
def _merge_upload_state(items: list[dict]) -> tuple[list[dict], dict]:
    """把「上传槽位」的校验结果合并进 D1–D8 就绪项，并标注数据来源。

    上传优先：某 item 对应槽位已上传时，用上传文件的真实校验结果覆盖静态状态。
    """
    v = um.validate_all()
    by_item: dict[str, list[dict]] = {}
    for r in v["slots"].values():
        by_item.setdefault(r.get("item"), []).append(r)
    order = {"ready": 0, "degraded": 1, "missing": 2}
    out: list[dict] = []
    for it in items:
        rec = dict(it)
        rs = by_item.get(it["id"], [])
        # 上传与内置都算「本轮真实取到的数据」：单槽位的数据项（如 D2 只有 b_in）
        # 用槽位实时校验结果覆盖静态文案 —— 只认 uploaded 的话，选了内置的槽位会继续
        # 显示 readiness_status.json 里那句历史统计（例如「5 份 xlsx」），与实际取数不是一回事。
        # 多槽位共用一个 D 编号时（D1 两组、D3 三组）不能这么覆盖：本函数不区分模式，
        # 会把 A 组的预期输出串到 B 模式上；这类仍按「有上传才覆盖」处理。
        if len(rs) > 1:
            up = [r for r in rs if r.get("source") == "uploaded"]
        else:
            up = [r for r in rs
                  if r.get("source") in ("uploaded", "builtin") and (r.get("files") or [])]
        if up:
            best = sorted(up, key=lambda r: order.get(r["status"], 3))[0]
            rec["status"] = best["status"]
            rec["count"] = best["count"]
            rec["source"] = best.get("source") or "builtin"
            note = "；".join(best.get("issues") or [])
            if best.get("leak_warning"):
                note = (note + " " + best["leak_warning"]).strip()
            rec["note"] = note or rec.get("note", "")
            rec["files"] = best.get("files", [])
        else:
            rec["source"] = "builtin"
            rec.setdefault("files", [])
        rec["color"] = bridge.status_color(rec["status"])
        out.append(rec)
    return out, v


@app.get("/api/data/status")
async def data_status():
    """P0 数据就绪面板的初始快照（静态 readiness_status + 上传槽位实时校验）。"""
    status = bridge.load_readiness_status()
    facts = bridge.probe_data_artifacts()
    items, upload = _merge_upload_state(status.get("items", []))
    return JSONResponse({
        "schema": status.get("schema"),
        "generated_at": status.get("generated_at"),
        "source_of_truth": status.get("source_of_truth"),
        "summary": status.get("summary"),
        "items": [
            {
                "id": it["id"],
                "name": it.get("name", it["id"]),
                "status": it["status"],
                "color": it["color"],
                "count": it.get("count", ""),
                "impact": it.get("impact", ""),
                "note": it.get("note", ""),
                "source": it.get("source", "builtin"),
                "files": it.get("files", []),
            }
            for it in items
        ],
        "upload": {
            "slot_count": upload["slot_count"],
            "uploaded_count": upload["uploaded_count"],
            "missing_required": upload["missing_required"],
            "all_source_builtin": upload["all_source_builtin"],
            "slots": {
                s: {
                    "label": v["label"], "name": um.SLOTS[s].get("name", ""),
                    "group": um.SLOTS[s].get("group", ""),
                    "modes": v["modes"], "accept": um.SLOTS[s]["accept"],
                    "multi": um.SLOTS[s].get("multi", False),
                    "required": um.SLOTS[s].get("required", False),
                    "hint": um.SLOTS[s].get("hint", ""),
                    "status": v["status"], "count": v["count"], "source": v["source"],
                    "files": v.get("files", []), "issues": v.get("issues", []),
                    "preset": v.get("preset", ""),
                    "leak_warning": v.get("leak_warning"),
                }
                for s, v in upload["slots"].items()
            },
        },
        "artifact_facts": facts,
        "role_count": len(bridge.list_roles()),
    })


# ---------------------------------------------------------------------------
# D0 · 已有标准库（常驻底座，跨模式共用一份）
# ---------------------------------------------------------------------------
@app.get("/api/library/status")
async def library_status():
    from tools import standard_library as sl
    return JSONResponse(sl.status())


@app.post("/api/library/upload")
async def library_upload(files: list[UploadFile] = File(...),
                         note: str = Form(""), append: bool = Form(True)):
    """上传/追加已有标准库。落盘 → 解析三表 → 合并 → 与 D3 做重合度校验。"""
    import tempfile as _tf
    from tools import standard_library as sl
    tmp_paths: list[Path] = []
    orig_names: list[str] = []
    try:
        for f in files:
            ext = Path(f.filename or "").suffix.lower()
            if ext not in (".xlsx", ".xls"):
                return JSONResponse({"error": f"标准库只接受 xlsx/xls，收到 {ext or '未知'}"},
                                    status_code=400)
            t = _tf.NamedTemporaryFile(delete=False, suffix=ext)
            t.write(await f.read())
            t.close()
            tmp_paths.append(Path(t.name))
            orig_names.append(f.filename or Path(t.name).name)
        if not tmp_paths:
            return JSONResponse({"error": "未收到文件"}, status_code=400)
        r = await asyncio.to_thread(sl.ingest, tmp_paths, note=note, append=append,
                                    display_names=orig_names)
        await bridge.publish("library_updated", {"class_count": r.get("class_count"),
                                                 "version": r.get("version"),
                                                 "counts": r.get("counts")})
        return JSONResponse(r)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    finally:
        for p_ in tmp_paths:
            try:
                p_.unlink()
            except Exception:
                pass


class LibraryCommit(BaseModel):
    standard_id: str
    gate_id: str = ""
    actor: str = "human"
    allow_overwrite: bool = True      # 人已勾选"通过并入库"，默认按覆盖处理


@app.post("/api/library/commit")
async def library_commit(body: LibraryCommit):
    """把已通过人工审核的标准写回 D0（只在人勾选「通过并入库」时调用）。"""
    from tools import standard_library as sl
    try:
        r = await asyncio.to_thread(sl.commit_standard, body.standard_id,
                                    gate_id=body.gate_id, actor=body.actor,
                                    allow_overwrite=body.allow_overwrite)
        await bridge.publish("library_updated", {"class_count": r.get("class_count"),
                                                 "version": r.get("version"),
                                                 "added_attr_count": r.get("added_attr_count")})
        return JSONResponse(r)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/library/reset")
async def library_reset():
    from tools import standard_library as sl
    return JSONResponse(await asyncio.to_thread(sl.reset))


# ---------------------------------------------------------------------------
# 现场数据接入（上传 → 落盘 → 结构校验）
# ---------------------------------------------------------------------------
@app.post("/api/data/upload")
async def data_upload(slot: str = Form(...), files: list[UploadFile] = File(...)):
    """现场上传。文件落盘到 data/uploaded/<slot>/，随后立即做真实结构校验。

    上传后该槽位即成为对应工具的取数来源（覆盖内置路径），并清空各模块缓存。
    """
    if slot not in um.SLOTS:
        return JSONResponse({"error": f"未知槽位：{slot}；可选：{sorted(um.SLOTS)}"},
                            status_code=400)
    cfg = um.SLOTS[slot]
    accepts = [a.strip() for a in cfg["accept"].split(",") if a.strip()]
    items: list[tuple[str, bytes]] = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if accepts and ext not in accepts:
            return JSONResponse(
                {"error": f"槽位「{cfg['label']}」不接受 {ext or '未知'} 文件，仅接受 {cfg['accept']}"},
                status_code=400)
        items.append((f.filename or "unnamed", await f.read()))
    if not items:
        return JSONResponse({"error": "未收到文件"}, status_code=400)
    if not cfg.get("multi") and len(items) > 1:
        items = items[:1]          # 单文件槽位只保留第一个
    # 用户自己传了文件：先退出内置演示态并还原原有数据，再落盘 ——
    # 顺序反了会把刚上传的记录写进 manifest 又被备份还原冲掉。
    demo_left = demo.on_user_upload(slot, items[0][0])
    um.save_files(slot, items)
    um.invalidate_caches()
    v = um.validate_slot(slot)

    # 上传建模集后直接重建该模式图谱，现场演示不必再手动点一次「构建」
    rebuilt = None
    if slot in ("d0_a", "d0_b"):
        mode = "A" if slot == "d0_a" else "B"
        try:
            base = await asyncio.to_thread(ob.build_base_graph, mode)
            await asyncio.to_thread(ob.graph_upsert, base["nodes"], base["edges"], mode, True)
            st = ob.graph_stats(mode)
            rebuilt = {"mode": mode, "nodes": st["total_nodes"], "edges": st["total_edges"],
                       "metric_counts": st["metric_counts"],
                       "all_pass": st["metric_check"]["all_pass"]}
        except Exception as e:
            rebuilt = {"mode": mode, "error": f"{type(e).__name__}: {e}"}

    await bridge.publish("upload", {"slot": slot, "status": v["status"],
                                    "count": v["count"], "files": v["files"]})
    return JSONResponse({"ok": True, "slot": slot, "validation": v,
                         "cleared_caches": True, "graph_rebuilt": rebuilt,
                         "demo_left": demo_left})


@app.post("/api/data/validate")
async def data_validate():
    """SSE：逐槽位广播真实校验结果（上传优先，未上传则校验内置回落数据）。"""
    async def gen():
        r = um.validate_all()
        yield bridge.format_sse({
            "event": "upload_validate_begin",
            "data": {"slot_count": r["slot_count"], "uploaded_count": r["uploaded_count"],
                     "all_source_builtin": r["all_source_builtin"]},
            "ts": bridge._now_iso(),
        })
        for s, v in r["slots"].items():
            yield bridge.format_sse({
                "event": "upload_validate_result",
                "data": {"slot": s, "label": v["label"], "status": v["status"],
                         "count": v["count"], "source": v["source"],
                         "issues": v.get("issues", []),
                         "leak_warning": v.get("leak_warning")},
                "ts": bridge._now_iso(),
            })
        yield bridge.format_sse({
            "event": "upload_validate_complete",
            "data": {"missing_required": r["missing_required"],
                     "uploaded_count": r["uploaded_count"]},
            "ts": bridge._now_iso(),
        })
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/api/data/upload/status")
async def upload_status():
    """各上传槽位当前状态（含校验结果与来源标记）。"""
    return JSONResponse(um.validate_all())


class UploadReset(BaseModel):
    slot: str | None = None


class DataDecision(BaseModel):
    slot: str
    choice: str = "builtin"      # 目前只支持 builtin（显式采用内置数据）


@app.post("/api/data/decision")
async def data_decision(body: DataDecision):
    """显式声明某槽位采用内置数据（不做默认假设：未声明的槽位是「待选择」）。"""
    if body.slot not in um.SLOTS:
        return JSONResponse({"error": f"未知槽位：{body.slot}"}, status_code=400)
    if body.choice != "builtin":
        return JSONResponse({"error": "choice 目前只支持 builtin"}, status_code=400)
    r = await asyncio.to_thread(um.set_builtin, body.slot)
    for slot, mode in (("d0_a", "A"), ("d0_b", "B")):
        if body.slot != slot:
            continue
        try:
            base = await asyncio.to_thread(ob.build_base_graph, mode)
            await asyncio.to_thread(ob.graph_upsert, base["nodes"], base["edges"], mode, True)
        except Exception:
            pass
    await bridge.publish("upload", {"slot": body.slot, "action": "builtin"})
    return JSONResponse({"ok": True, **r, "state": um.validate_all()})


class SlotEnable(BaseModel):
    slot: str
    enabled: bool = True


@app.post("/api/data/enable")
async def data_enable(body: SlotEnable):
    """滑动开关：启用/停用某数据项（停用 = 本轮不参与；必填项不允许停用）。"""
    if body.slot not in um.SLOTS:
        return JSONResponse({"error": f"未知槽位：{body.slot}"}, status_code=400)
    try:
        if body.enabled:
            r = await asyncio.to_thread(um.set_enabled, body.slot)
        else:
            r = await asyncio.to_thread(um.set_off, body.slot)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    await bridge.publish("upload", {"slot": body.slot,
                                    "action": "on" if body.enabled else "off"})
    return JSONResponse({"ok": True, **r, "state": um.validate_all()})


@app.post("/api/data/decision/all")
async def data_decision_all(body: dict | None = None):
    """一键把所有槽位设为「采用内置数据」。"""
    r = await asyncio.to_thread(um.set_all_builtin)
    await bridge.publish("upload", {"slot": "all", "action": "builtin"})
    return JSONResponse({"ok": True, **r, "state": um.validate_all()})


@app.post("/api/data/upload/reset")
async def upload_reset(body: UploadReset):
    """清空指定槽位（或全部）的上传内容，回落到内置数据。"""
    if body.slot and body.slot not in um.SLOTS:
        return JSONResponse({"error": f"未知槽位：{body.slot}"}, status_code=400)
    r = um.reset(body.slot)
    # 重置建模集槽位后，图谱也要跟着回到内置数据，否则画布还挂着上传数据的图
    for slot, mode in (("d0_a", "A"), ("d0_b", "B")):
        if body.slot and body.slot != slot:
            continue
        try:
            base = await asyncio.to_thread(ob.build_base_graph, mode)
            await asyncio.to_thread(ob.graph_upsert, base["nodes"], base["edges"], mode, True)
        except Exception:
            pass
    await bridge.publish("upload", {"slot": body.slot or "all", "action": "reset"})
    return JSONResponse({"ok": True, **r, "state": um.validate_all()})


@app.post("/api/data/readiness")
async def data_readiness():
    """SSE：逐项正确广播 D1–D8 就绪校验结果（遵循 SKILL 4.1 readiness_result）。"""
    async def gen():
        status = bridge.load_readiness_status()
        items, upload = _merge_upload_state(status.get("items", []))
        yield bridge.format_sse({
            "event": "readiness_begin",
            "data": {"summary": status.get("summary", ""),
                     "uploaded_count": upload["uploaded_count"],
                     "all_source_builtin": upload["all_source_builtin"],
                     "ts": bridge._now_iso()},
            "ts": bridge._now_iso(),
        })
        for it in items:
            yield bridge.format_sse({
                "event": "readiness_result",
                "data": {
                    "item": it["id"],
                    "status": it["status"],
                    "count": it.get("count", ""),
                    "impact": it.get("impact", ""),
                    "source": it.get("source", "builtin"),
                },
                "ts": bridge._now_iso(),
            })
        yield bridge.format_sse({
            "event": "readiness_complete",
            "data": {"ts": bridge._now_iso()},
            "ts": bridge._now_iso(),
        })
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# 会话与流水线
# ---------------------------------------------------------------------------
@app.post("/api/session/start")
async def session_start(body: SessionStart):
    if body.mode not in ("A", "B"):
        return JSONResponse({"error": "mode 必须为 A 或 B"}, status_code=400)
    sess = await bridge.start_session(body.mode)
    return JSONResponse(sess)


# ---------------------------------------------------------------------------
# 数据闸门：P0 未显式选择（上传 / 采用内置）的槽位，一律不给数据
# 目的：杜绝任何环节"悄悄"回落内置数据
# ---------------------------------------------------------------------------
def _data_gate(*slots, any_of: bool = False):
    """None = 通过；否则返回 400，说明缺哪一项、为什么。"""
    from tools import upload_manager as um
    ok = [x for x in slots if um.has_data(x)]
    if (any_of and ok) or (not any_of and len(ok) == len(slots)):
        return None
    missing = [x for x in slots if x not in ok] or list(slots)
    msgs: list[str] = []
    for x in missing:
        try:
            one = str(um.DataUnavailable(x))
        except Exception:
            one = str(x)
        if one not in msgs:
            msgs.append(one)
    return JSONResponse({"error": "数据未就绪：" + "；".join(msgs)
                                  + "。请先在 P0 上传，或点该行「采用内置数据」后再执行。"},
                        status_code=400)


@app.post("/api/pipeline/run")
async def pipeline_run(body: PipelineRun):
    if body.mode not in ("A", "B"):
        return JSONResponse({"error": "mode 必须为 A 或 B"}, status_code=400)
    gate = (_data_gate("a_in_g1", "a_in_g2", any_of=True) if body.mode == "A"
            else _data_gate("b_in"))
    if gate:
        return gate
    if body.session_id:
        sid = body.session_id
    else:
        sess = await bridge.start_session(body.mode)
        sid = sess["session_id"]
    # 在后台跑流水线骨架；事件经总线广播，客户端通过 /api/events 接收
    import asyncio
    asyncio.create_task(bridge.run_pipeline(body.mode, sid))
    return JSONResponse({"session_id": sid, "mode": body.mode, "status": "running"})


@app.get("/api/events")
async def events():
    """SSE：全局事件总线，P0 执行轨迹流。"""
    return StreamingResponse(bridge.event_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# 阶段 1 · P1 文档解析工作台（共享 doc-parser）
# ---------------------------------------------------------------------------
async def _emit_parse_gate(mode: str, item: dict) -> None:
    """degraded/failed 自动进入 P8 门控：登记门控项并广播 gate_required。"""
    await bridge.add_gate({
        "gate_id": f"g-parse-{mode}-{item['file']}",
        "mode": mode,
        "reason": "parse_quality",
        "panel": "P1",
        "item": {"file": item["file"], "quality": item["parse_quality"], "note": item.get("note", "")},
    })


@app.post("/api/parse")
async def parse_sample(body: ParseSample):
    """SSE：解析示例数据（模式 A 批量 / 模式 B 单份）。body={mode, source:'sample'}。"""
    mode = body.mode
    gate = (_data_gate("a_in_g1", "a_in_g2", any_of=True) if mode == "A"
            else _data_gate("b_in"))
    if gate:
        return gate

    async def gen():
        yield bridge.format_sse({
            "event": "parse_begin", "data": {"mode": mode, "source": "sample"}, "ts": bridge._now_iso(),
        })
        if mode == "A":
            files = dp.list_sample_files_a()
            docs = []
            for p in files:
                # 逐份解析（线程池，避免阻塞事件循环），边解析边推送
                d = await asyncio.to_thread(dp.parse_document, p)
                docs.append(d)
                yield bridge.format_sse({
                    "event": "parse_result",
                    "data": {"mode": "A", "file": d["file"], "fmt": d["fmt"],
                             "page_count": d["page_count"], "parse_quality": d["parse_quality"],
                             "note": d.get("note", ""), "pages": d["pages"]},
                    "ts": bridge._now_iso(),
                })
                await bridge.publish("parse_result", {
                    "mode": "A", "file": d["file"], "parse_quality": d["parse_quality"]})
                if d["parse_quality"] in ("degraded", "failed"):
                    await _emit_parse_gate("A", d)
            ov = dp._build_overview(docs)
            yield bridge.format_sse({
                "event": "parse_overview",
                "data": {"mode": "A", **ov}, "ts": bridge._now_iso(),
            })
            await bridge.publish("parse_overview", {"mode": "A", **ov})
        else:  # B
            result = await asyncio.to_thread(dp.parse_mode_b)
            doc = result["document"]
            yield bridge.format_sse({
                "event": "requirement_parsed",
                "data": {"mode": "B", "file": doc["file"], "fmt": doc["fmt"],
                         "page_count": doc["page_count"], "parse_quality": doc["parse_quality"],
                         "pages": doc["pages"]},
                "ts": bridge._now_iso(),
            })
            await bridge.publish("requirement_parsed", {
                "mode": "B", "file": doc["file"], "parse_quality": doc["parse_quality"]})
            if doc["parse_quality"] in ("degraded", "failed"):
                await _emit_parse_gate("B", doc)
            rec = result["template_recognition"]
            yield bridge.format_sse({
                "event": "template_recognized",
                "data": {"mode": "B", "recognition": rec}, "ts": bridge._now_iso(),
            })
            await bridge.publish("template_recognized", {"mode": "B", "sections": len(rec["sections"])})
        yield bridge.format_sse({
            "event": "parse_complete", "data": {"mode": mode}, "ts": bridge._now_iso(),
        })

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/api/parse/upload")
async def parse_upload(mode: str = Form("A"), files: list[UploadFile] = File(...)):
    """模式 A 真实批量上传（multipart）。返回解析结果与概览。"""
    if mode != "A":
        return JSONResponse({"error": "上传解析仅支持模式 A"}, status_code=400)
    documents = []
    for uf in files:
        suffix = Path(uf.filename).suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(await uf.read())
        tmp.close()
        d = dp.parse_document(tmp.name)
        documents.append(d)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    overview = dp._build_overview(documents)
    for d in documents:
        if d["parse_quality"] in ("degraded", "failed"):
            await _emit_parse_gate("A", d)
    return JSONResponse({"mode": "A", "documents": documents, "overview": overview})


@app.get("/api/parse/template")
async def parse_template():
    """返回用户上传的 D5 需求模板结构；未提供时明确说"没有"，不拿内置管道模板顶替。"""
    if not um.is_uploaded("req_template"):
        return JSONResponse({"available": False,
                             "reason": "未提供 D5 需求模板：已按文件自身表头识别，不做分节比对",
                             "sections": [], "blanks": []})
    fs = um.uploaded_files("req_template")
    tp = next((Path(f) for f in fs if f.lower().endswith(".json")), None)
    if tp is None or not tp.exists():
        return JSONResponse({"available": False,
                             "reason": "D5 模板不是 JSON，无法解析分节结构", "sections": [], "blanks": []})
    d = json.loads(tp.read_text(encoding="utf-8"))
    d["available"] = True
    d["source"] = tp.name
    return JSONResponse(d)


# ---------------------------------------------------------------------------
# 阶段 2 · P2 知识抽取审核台（共享 knowledge-extractor）
# ---------------------------------------------------------------------------
class ExtractSample(BaseModel):
    mode: str = "A"  # "A" | "B"
    source: str = "sample"


class ExtractReview(BaseModel):
    candidate_id: str
    decision: str  # confirm | discard | escalate
    mode: str = "A"
    note: str = ""


async def _emit_extract_gate(cand: dict, mode: str) -> None:
    """低置信度候选自动进入 P8 门控。"""
    await bridge.add_gate({
        "gate_id": f"g-extract-{mode}-{cand['id']}",
        "mode": mode,
        "reason": "low_confidence",
        "panel": "P2",
        "item": {
            "candidate_id": cand["id"], "name": cand["name"], "type": cand["type"],
            "confidence": cand["confidence"], "novelty": cand.get("novelty"),
            "evidence": cand.get("evidence_span", {}),
        },
    })


@app.post("/api/extract")
async def extract_sample(body: ExtractSample):
    """SSE：知识抽取（模式 A 广度 / 模式 B 目标导向）。逐候选推送 + 低置信入 P8。"""
    mode = body.mode
    gate = (_data_gate("a_in_g1", "a_in_g2", any_of=True) if mode == "A"
            else _data_gate("b_in"))
    if gate:
        return gate

    async def gen():
        yield bridge.format_sse({
            "event": "extract_begin", "data": {"mode": mode, "source": "sample"}, "ts": bridge._now_iso(),
        })
        # 只有模式 A 的 P2 会真实调用大模型（术语归一 + 定义补写）；
        # 模式 B 是从需求表结构化字段直接抽，不调模型，不能挂「大模型处理中」的招牌。
        if mode == "A" and llm.available():
            yield bridge.format_sse({
                "event": "llm_working",
                "data": {"panel": "P2", "model": llm.config().get("bulk_model") or llm.config()["model"],
                         "label": "大模型：术语归一 + 定义补写（约 60–90 秒）"},
                "ts": bridge._now_iso()})
        # 不强制重算：首次跑完进缓存，重复点击秒回（现场演示友好）
        result = await asyncio.to_thread(ke.extract, mode)
        if mode == "A":
            # 逐文档、逐候选推送
            for pd in result["documents"]:
                yield bridge.format_sse({
                    "event": "extract_section",
                    "data": {"mode": "A", "file": pd["file"], "candidate_count": pd["candidate_count"],
                             "sparse": pd["sparse"], "topical_terms": pd["topical_terms"]},
                    "ts": bridge._now_iso(),
                })
                for c in pd["candidates"]:
                    yield bridge.format_sse({
                        "event": "extract_candidate",
                        "data": {"mode": "A", **c}, "ts": bridge._now_iso(),
                    })
                    await bridge.publish("extract_candidate", {"mode": "A", **c})
                    if cf.as_float(c.get("confidence"), 0.0) < cf.LOW_CONF:
                        await _emit_extract_gate(c, "A")
        else:  # B
            yield bridge.format_sse({
                "event": "extract_document",
                "data": {"mode": "B", **result["document"]}, "ts": bridge._now_iso(),
            })
            for c in result["candidates"]:
                yield bridge.format_sse({
                    "event": "extract_candidate",
                    "data": {"mode": "B", **c}, "ts": bridge._now_iso(),
                })
                await bridge.publish("extract_candidate", {"mode": "B", **c})
        # 术语归一（A / B 共用）
        yield bridge.format_sse({
            "event": "extract_term_norm",
            "data": {"mode": mode, "term_norm": result["term_norm"]}, "ts": bridge._now_iso(),
        })
        if mode == "A":
            yield bridge.format_sse({
                "event": "extract_coverage",
                "data": {"mode": "A", "coverage_density": result["coverage_density"],
                         "low_confidence_count": result["low_confidence_count"],
                         "novelty_dist": result["novelty_dist"], "type_dist": result["type_dist"]},
                "ts": bridge._now_iso(),
            })
        # 关系候选（subClassOf / hasAttribute / associates）：边抽边推，和类/属性一样可追溯
        for r in (result.get("relations") or [])[:400]:
            yield bridge.format_sse({
                "event": "extract_relation",
                "data": {"mode": mode, **r}, "ts": bridge._now_iso(),
            })
        yield bridge.format_sse({
            "event": "extract_complete",
            "data": {"mode": mode, "basis": result["basis"], "basis_note": result["basis_note"],
                     "low_confidence_count": result["low_confidence_count"],
                     "candidate_count": len(result["candidates"]),
                     "type_dist": result.get("type_dist") or {},
                     "novelty_dist": result.get("novelty_dist") or {},
                     "relation_summary": result.get("relation_summary") or {},
                     "llm": result.get("llm") or {}},
            "ts": bridge._now_iso(),
        })

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/api/extract/review")
async def extract_review(body: ExtractReview):
    """P2 候选人工处置：confirm / discard / escalate。解析对应门控项并广播。"""
    if body.decision not in ("confirm", "discard", "escalate"):
        return JSONResponse({"error": "decision 必须为 confirm/discard/escalate"}, status_code=400)
    rec = bridge.record_extract_review(body.candidate_id, body.decision, body.note)
    gate_id = f"g-extract-{body.mode}-{body.candidate_id}"
    await bridge.resolve_gate(gate_id, body.decision, body.note)
    return JSONResponse({"ok": True, "review": rec})


# ---------------------------------------------------------------------------
# P3 · 双知识底座（json_to_owl / graph_upsert / graph_query）
# ---------------------------------------------------------------------------
class GraphBuild(BaseModel):
    mode: str = "A"
    with_candidates: bool = True


class GraphQuery(BaseModel):
    query: str
    mode: str = "A"
    topk: int = 12
    hop: int = 2          # 默认两跳：一拍同名/直连，二拍经 is-a / has-a 传播过来


class GraphReset(BaseModel):
    mode: str = "both"             # A / B / both
    archive: bool = True           # True=归档到 data/_purge/，False=直接删除
    include_parse_cache: bool = True


def _compact_node(n: dict) -> dict:
    """图谱画布用精简节点（定义等详信息走 /api/graph/node/{id}）。"""
    # 档位：按证据类型分（table_row / heading / text_line / seed），不看分数区间。
    # D0 标准库节点不参与分档（前端用中性色，表示"已人工确认，不标置信度"）。
    tier_id, tier_label = None, ""
    if n.get("origin") != "d0":
        t = cf.tier_for_node(n)
        tier_id, tier_label = t["id"], t["label"]
    conf_v = cf.as_float(n.get("confidence"))     # None = 未评估，**不能兜成 1.0 冒充已确认**
    return {
        "id": n.get("id"), "label": n.get("label"), "type": n.get("type"),
        "code": n.get("code", ""), "domain": n.get("domain", ""),
        "depth": n.get("depth", 0), "confidence": conf_v,
        "confidence_known": conf_v is not None,
        "confidence_source": n.get("confidence_source", ""),
        "origin": n.get("origin", ""),
        "confidence_basis": n.get("confidence_basis", ""),
        "evidence_kind": n.get("evidence_kind", ""),
        "tier": tier_id, "tier_label": tier_label,
        "source_mode": n.get("source_mode", ""),
        "owl_entity": (n.get("owl") or {}).get("entity"),
        "status": (n.get("completeness") or {}).get("status", ""),
        "relevance": n.get("relevance", 0.0),
        "novelty": n.get("novelty", ""),
        # JSON 树需展示「约束/量纲」
        "data_type": n.get("data_type", ""),
        "constraint": n.get("constraint", ""),
        "unit": n.get("unit", ""),
        # 折叠时按需展开用：属性挂在哪个类、池里被引用几次
        "owner_class": n.get("owner_class", ""),
        "ref_count": n.get("ref_count", 0),
        "field_count": n.get("field_count", 0),
    }


def _compact_edge(e: dict) -> dict:
    """画布/查询用精简边。

    类间关系（subClassOf / partOf / associates）要能区分「原有骨架」与
    「大模型推断」：前者实线细箭头，后者虚线 + 可点开看依据。
    所以 origin / confidence / evidence 这几个字段不能丢。
    """
    return {
        "id": e.get("id"), "source": e["source"], "target": e["target"],
        "type": e.get("type"), "label": e.get("label", ""),
        "origin": e.get("origin", ""),
        "confidence": cf.as_float(e.get("confidence")),
        "evidence": e.get("evidence") or e.get("basis") or "",
        "source_ref": e.get("source_ref", ""),
    }


def _base_gate(mode: str):
    """P3 底座门控：D0 已有标准库或本轮输入任一可用即通过。

    2026-10-23：不再要求 D3 有数据 —— 底座本来就**不该**取评测答案。
    """
    from tools import base_supply as bs
    try:
        d = bs.load(mode)
    except Exception as e:
        return JSONResponse({"error": f"知识底座无可用供给：{e}"}, status_code=400)
    if not d["counts"]["object_class"]:
        return JSONResponse({"error": "知识底座无可用供给：请先上传 D0 已有标准库，"
                                      "或准备本轮输入（A：D1 文档 / B：D2 需求表）。"
                                      "D3 预期输出不参与底座构建。"}, status_code=400)
    return None


@app.post("/api/graph/reset")
async def graph_reset(body: GraphReset):
    """清空 P3 双知识底座（本体 JSON + OWL + 解析缓存 + 全部进程内缓存）。

    只清「可重建的派生物」，不碰 D0 上传原件、D1–D3 槽位、上传原文件、P5 产物。
    """
    r = await asyncio.to_thread(ob.purge, body.mode, archive=body.archive,
                                include_parse_cache=body.include_parse_cache)
    await bridge.publish("graph_reset", {"modes": r["modes"],
                                         "removed_count": r["removed_count"]})
    return JSONResponse(r)


@app.post("/api/graph/build")
async def graph_build(body: GraphBuild):
    """SSE：构建双知识底座（D0 + 本轮输入 → JSON 树 → OWL + 图谱）。

    硬约束：底座只经 base_supply.load(mode) 取数（D0 已有标准库 + 本轮输入），
    **不读 D3 预期输出**；写入前经 ob._assert_no_test_source() 防泄漏校验。
    """
    mode_ = (body.mode or "A").upper()
    gate = _base_gate(mode_)
    if gate:
        return gate

    async def gen():
        def emit(ev, data):
            return bridge.format_sse({"event": ev, "data": data, "ts": bridge._now_iso()})

        # 重建走 graph_upsert(reset=True) 全量替换，图里已有的 AI 推断边会被一起冲掉。
        # 这不是 bug（底座必须能重算），但用户会以为"关系又没了"，所以在完成事件里
        # 明确报出被清掉的条数与待确认候选，前端据此提示一句"点应用可恢复"。
        lost_ai = 0
        try:
            _g = ob.get_graph((body.mode or "A").upper())
            lost_ai = sum(1 for e in _g["edges"]
                          if str(e.get("source_ref") or "") == cr.SOURCE_REF)
        except Exception:                                  # 图还没建过 / 读失败：当 0，不影响构建
            lost_ai = 0

        yield emit("graph_begin", {"mode": body.mode, "label": "开始构建双知识底座"})
        yield emit("graph_stage", {"stage": 1, "total": 4,
                                   "label": "载入底座供给（base_supply：D0 已有标准库 + 本轮输入，不含 D3）"})
        mode = (body.mode or "A").upper()
        base = await asyncio.to_thread(ob.build_base_graph, mode)
        dropped = base.get("dropped_edges", {})
        yield emit("graph_stage", {"stage": 2, "total": 4,
                                   "label": f"json_to_owl：{len(base['nodes'])} 节点 / {len(base['edges'])} 边 → OWL Turtle",
                                   "nodes": len(base["nodes"]), "edges": len(base["edges"])})
        r1 = await asyncio.to_thread(ob.graph_upsert, base["nodes"], base["edges"], mode, True)
        yield emit("graph_upsert", {**r1, "label": "基础图谱写入（防泄漏校验通过）",
                                    "dropped_edges": dropped.get("count", 0),
                                    "dropped_reason": dropped.get("reason", "")})

        cand_n = 0
        if body.with_candidates:
            yield emit("graph_stage", {"stage": 3, "total": 4,
                                       "label": f"P2 抽取候选入图（mode={body.mode}，节点带 confidence/source_mode）"})
            r2, cand_n = await asyncio.to_thread(ob.upsert_candidates, body.mode)
            yield emit("graph_upsert", {**r2, "label": f"候选节点入图 {cand_n} 个"})

        yield emit("graph_stage", {"stage": 4, "total": 4, "label": "OWL 2 DL 结构化剖面校验"})
        owl = await asyncio.to_thread(ob.json_to_owl, ob.get_graph(mode), mode)
        for c in owl["owl_profile"]["checks"]:
            yield emit("owl_check", {"name": c["name"], "passed": c["passed"], "detail": c["detail"]})

        stats = ob.graph_stats(mode)
        yield emit("graph_stats", stats)
        rel_state = {}
        try:
            _c = cr.load(mode)
            rel_state = {"pending": _c.get("pending", 0), "in_graph": _c.get("in_graph", 0),
                         "candidates": len(_c.get("relations") or [])}
        except Exception:
            rel_state = {}
        yield emit("graph_complete", {
            "dropped_edges": dropped,
            "owl_profile": owl["owl_profile"],
            "owl_path": owl["owl_path"],
            "candidate_count": cand_n,
            "lost_ai_relations": lost_ai,
            "rel_state": rel_state,
            "stats": stats,
        })
        await bridge.publish("graph_complete", {"mode": mode, "nodes": stats["total_nodes"]})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/api/graph/query")
async def graph_query_endpoint(body: GraphQuery):
    """SSE：语义推理查询（术语归一 → 实体匹配 → 邻域扩展 → 子图裁剪），流式返回高亮子图。"""

    async def gen():
        def emit(ev, data):
            return bridge.format_sse({"event": ev, "data": data, "ts": bridge._now_iso()})

        yield emit("query_begin", {"query": body.query, "mode": body.mode, "hop": body.hop})
        qmode = (body.mode or "A").upper()
        res = await asyncio.to_thread(ob.graph_query, body.query, qmode, body.topk, body.hop)
        for t in res["trace"]:
            yield emit("query_trace", {"step": t["step"], "detail": t["detail"]})
        for m in res["matched"]:
            yield emit("query_match", m)
        for p in res.get("paths") or []:
            yield emit("query_path", p)
        yield emit("query_subgraph", {
            "nodes": [_compact_node(n) for n in res["subgraph"]["nodes"]],
            "edges": res["subgraph"]["edges"],
        })
        yield emit("query_complete", {
            "matched_count": len(res["matched"]),
            "subgraph_nodes": len(res["subgraph"]["nodes"]),
            "subgraph_edges": len(res["subgraph"]["edges"]),
            "normalized": res["normalized"],
        })
        await bridge.publish("graph_query", {"mode": body.mode, "query": body.query,
                                             "matched": len(res["matched"])})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/api/graph/data")
async def graph_data(mode: str = "A", limit: int = 1500, per_class: int = 0):
    """图谱画布数据（骨架 + 按需展开）。

    底座可达 2 万+ 属性节点，一次性推给 Cytoscape 会卡死、也没人看得清。
    所以画布**默认只给对象类骨架**（`per_class=0`），属性由
    `/api/graph/expand?class_id=...` 点开某个类时按需拉取。
    `per_class>0` 仍兼容旧的「每类前 N 个」视图。
    """
    mode = (mode or "A").upper()
    g = ob.get_graph(mode)
    all_nodes, all_edges = g["nodes"], g["edges"]

    cls = [n for n in all_nodes if n.get("type") == "object_class"]
    cand = [n for n in all_nodes if n.get("type") == "candidate"]
    fields = [n for n in all_nodes if n.get("type") == "field"]
    pool = {n["id"]: n for n in all_nodes if n.get("type") == "attribute"}

    # 每个类挂多少条对象类属性（JSON 树/画布提示用）
    field_count: dict[str, int] = {}
    for e in all_edges:
        if e.get("type") == "hasAttribute":
            field_count[e["source"]] = field_count.get(e["source"], 0) + 1

    # 画布只画三类节点：对象类 / 对象类属性 / 属性池。
    # 抽取候选（candidate）是 P5 匹配用的中间产物，画到图上只是一堆红绿点，用户明确不要。
    picked: dict[str, dict] = {}
    for n in sorted(cls, key=lambda x: str(x.get("id"))):
        m = dict(n)
        m["field_count"] = field_count.get(n["id"], 0)
        picked[n["id"]] = m

    if per_class > 0:                # 兼容旧视图：每类取前 N 个属性 + 其取用的池节点
        per: dict[str, int] = {}
        for n in fields:
            owner = n.get("owner_class") or ""
            if per.get(owner, 0) >= per_class:
                continue
            per[owner] = per.get(owner, 0) + 1
            picked[n["id"]] = n
        fids = {k for k, v in picked.items() if v.get("type") == "field"}
        for e in all_edges:
            if e.get("type") == "refersTo" and e["source"] in fids and e["target"] in pool:
                picked[e["target"]] = pool[e["target"]]

    truncated = len(picked) < len(all_nodes)
    if len(picked) > limit:                     # 最后再兜一层硬上限
        keep = sorted(picked.values(), key=lambda n: ({"object_class": 0,
                                                       "field": 1}.get(n.get("type"), 2), n["id"]))[:limit]
        picked = {n["id"]: n for n in keep}
        truncated = True

    nodes = [_compact_node(n) for n in picked.values()]
    if mode == "B":
        rel = ob.compute_requirement_relevance("B")
        for n in nodes:
            n["relevance"] = rel.get(n["id"], 0.0)
    edges = [_compact_edge(e) for e in all_edges
             if e["source"] in picked and e["target"] in picked]
    return JSONResponse({
        "nodes": nodes,
        "edges": edges,
        "view": {"shown_nodes": len(nodes), "total_nodes": len(all_nodes),
                 "shown_edges": len(edges), "total_edges": len(all_edges),
                 "per_class": per_class, "truncated": truncated,
                 "folded": per_class <= 0,
                 "field_total": len(fields), "pool_total": len(pool),
                 "candidate_total": len(cand),
                 "note": ("画布默认只画对象类与类间关系（父子/组成/业务关联），"
                          "点类节点再展开它的对象类属性与属性池"
                          if per_class <= 0 else
                          f"画布按「全部对象类 + 每类前 {per_class} 个属性」渲染子图")},
        "relations": cr.summary(mode),
        "stats": ob.graph_stats(mode),
    })


@app.get("/api/graph/expand")
async def graph_expand(mode: str = "A", class_id: str = "", limit: int = 400):
    """展开某个对象类：返回它的对象类属性 + 这些属性取用的池节点 + 相关边。

    画布折叠时点类节点调这个接口，一次只拉一个类的属性，
    不把两万多条属性全塞进浏览器。
    """
    mode = (mode or "A").upper()
    g = ob.get_graph(mode)
    by_id = {n["id"]: n for n in g["nodes"]}
    if class_id not in by_id:
        return JSONResponse({"error": f"类不存在：{class_id}"}, status_code=404)

    fields = [n for n in g["nodes"] if n.get("type") == "field" and n.get("owner_class") == class_id]
    total = len(fields)
    fields = fields[:max(1, limit)]
    fids = {n["id"] for n in fields}

    edges = [e for e in g["edges"]
             if (e.get("type") == "hasAttribute" and e["source"] == class_id and e["target"] in fids)
             or (e.get("type") == "refersTo" and e["source"] in fids)]
    pool_ids = {e["target"] for e in edges if e.get("type") == "refersTo" and e["target"] in by_id}
    nodes = fields + [by_id[i] for i in pool_ids]
    return JSONResponse({
        "class_id": class_id, "nodes": [_compact_node(n) for n in nodes],
        "edges": [_compact_edge(e) for e in edges],
        "total_fields": total, "truncated": total > len(fields),
    })


@app.get("/api/graph/stats")
async def graph_stats(mode: str = "A"):
    return JSONResponse(ob.graph_stats((mode or "A").upper()))


# ---------------------------------------------------------------------------
# P3 · 类间关系分析（大模型 + 本体论模式）
# ---------------------------------------------------------------------------
class RelAnalyze(BaseModel):
    mode: str = "B"
    batch: int = 15          # 每次调用喂几个类
    workers: int = 4         # 并发调用数
    limit: int = 0           # >0 只分析前 N 个类（调试/演示用）
    with_same_as: bool = False   # 是否把同义关系也当候选（默认不要，容易误合并）
    retry_failed: bool = False   # 只重跑上次超时的批次，结果合并进现有候选


class RelApply(BaseModel):
    mode: str = "B"
    min_confidence: float = cr.MIN_CONF_DEFAULT
    types: list[str] | None = None
    dry_run: bool = False


@app.get("/api/graph/relations")
async def graph_relations(mode: str = "B"):
    """类间关系候选 + 现状摘要（图里已有多少、候选多少、多少待确认）。"""
    mode = (mode or "B").upper()
    d = cr.load(mode)
    d["summary"] = cr.summary(mode)
    return JSONResponse(d)


@app.post("/api/graph/relations/analyze")
async def graph_relations_analyze(body: RelAnalyze):
    """SSE：让模型读「类名 + 定义 + 属性清单 + 出处证据」，产出类间关系候选。

    只产出候选，**不写图**。入图走 /api/graph/relations/apply（人给阈值后触发）。
    """
    mode = (body.mode or "B").upper()
    if not llm.available():
        return JSONResponse({"error": llm.status().get("reason") or "大模型未配置"}, status_code=400)

    # 演示态：类间关系分析要跑 26 批 × 3~6 分钟的模型调用，现场绝不能真跑。
    # 已有候选是那次真实分析的产物（data/graph/class_relations_{mode}.json），
    # 这里只回放"结论"，并明确标注本次没有重新调用模型。
    if demo.is_active() and not demo.is_capturing():
        cand = cr.load(mode)
        rels = cand.get("relations") or []
        counts: dict[str, int] = {}
        for r in rels:
            t = r.get("type") or "unknown"
            counts[t] = counts.get(t, 0) + 1
        summ = cr.summary(mode)
        recorded_at = cand.get("built_at") or ""

        async def gen_replay():
            yield bridge.format_sse({"event": "rel_begin", "data": {
                "mode": mode, "classes": summ.get("classes", 0),
                "total_classes": summ.get("classes", 0), "batches": 0,
                "model": cand.get("model") or "（回放）", "replayed": True,
            }, "ts": bridge._now_iso()})
            yield bridge.format_sse({"event": "rel_done", "data": {
                "mode": mode, "relations": len(rels), "counts": counts, "failed": 0,
                "elapsed_ms": 0, "replayed": True, "recorded_at": recorded_at,
                "note": "演示回放：没有重新调用模型，候选来自录制时的真实分析",
            }, "ts": bridge._now_iso()})

        return StreamingResponse(gen_replay(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    async def gen():
        def emit(ev, data):
            return bridge.format_sse({"event": ev, "data": data, "ts": bridge._now_iso()})

        import queue as _q
        q: "_q.Queue" = _q.Queue()
        fut = asyncio.create_task(asyncio.to_thread(
            cr.analyze, mode, batch=max(4, min(40, body.batch)),
            workers=max(1, min(8, body.workers)), limit=body.limit,
            with_same_as=body.with_same_as, retry_failed=body.retry_failed,
            progress=lambda ev, d: q.put((ev, d))))
        while True:
            drained = False
            while True:
                try:
                    ev, data = q.get_nowait()
                except _q.Empty:
                    break
                drained = True
                yield emit(ev, data)
            if fut.done():
                break
            if not drained:
                await asyncio.sleep(0.25)
        try:
            res = await fut
        except Exception as e:
            yield emit("rel_error", {"error": f"{type(e).__name__}: {e}"})
            return
        yield emit("rel_done", {
            "mode": mode, "classes": res["classes"], "batches": res["batches"],
            "analyzed": res.get("analyzed_batches", res["batches"]),
            "retry_failed": res.get("retry_failed", False),
            "relations": len(res["relations"]), "counts": res["counts"],
            "duplicates": res["duplicates"], "dropped": res["dropped"],
            "failed": len(res["failed_batches"]), "elapsed_ms": res["elapsed_ms"],
            "model": res["model"], "notes": res.get("notes") or [],
        })
        await bridge.publish("graph_relations", {"mode": mode,
                                                 "relations": len(res["relations"])})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/api/graph/relations/apply")
async def graph_relations_apply(body: RelApply):
    """把候选关系按阈值写进图谱（防环 + 去重），并重导 OWL。"""
    mode = (body.mode or "B").upper()
    types = body.types or list(cr.DEFAULT_TYPES)
    bad = [t for t in types if t not in cr.lt.REL_TYPES]
    if bad:
        return JSONResponse({"ok": False, "error": f"未知关系类型：{bad}"}, status_code=400)
    r = await asyncio.to_thread(cr.apply, mode,
                                min_confidence=max(0.0, min(1.0, body.min_confidence)),
                                types=types, dry_run=body.dry_run)
    if r.get("ok") and not body.dry_run and r.get("applied"):
        await bridge.publish("graph_relations_applied", {"mode": mode,
                                                         "applied": r["applied"]})
    return JSONResponse(r)


@app.post("/api/graph/relations/clear")
async def graph_relations_clear(body: RelApply):
    """撤销模型推断的类间关系（只删 source_ref=llm:class_relations 的边）。"""
    mode = (body.mode or "B").upper()
    r = await asyncio.to_thread(cr.clear, mode)
    await bridge.publish("graph_relations_cleared", {"mode": mode,
                                                     "removed": r["removed_edges"]})
    return JSONResponse(r)


@app.get("/api/graph/owl")
async def graph_owl(mode: str = "A"):
    """OWL Turtle 全文 + 剖面校验结果（供 UI 展示/下载）。"""
    owl = ob.json_to_owl(ob.get_graph((mode or "A").upper()), (mode or "A").upper(), write=True)
    return JSONResponse({
        "turtle": owl["owl_turtle"],
        "profile": owl["owl_profile"],
        "path": owl["owl_path"],
    })


@app.get("/api/graph/node/{node_id:path}")
async def graph_node(node_id: str, mode: str = "A"):
    """节点详情（Cytoscape 点击 → OWL 属性面板）。"""
    d = ob.node_detail(node_id, (mode or "A").upper())
    if not d:
        return JSONResponse({"error": f"节点不存在：{node_id}"}, status_code=404)
    return JSONResponse(d)


# ---------------------------------------------------------------------------
# P4 · 模式 A 发现式梳理（novelty_check / landscape_map / prioritize_candidates）
# ---------------------------------------------------------------------------
class LandscapeBuild(BaseModel):
    min_density: int = 1
    force: bool = False


class LandscapePrioritize(BaseModel):
    candidate_ids: list[str] | None = None


class LandscapeApprove(BaseModel):
    candidate_ids: list[str]
    note: str = ""


@app.post("/api/landscape/build")
async def landscape_build(body: LandscapeBuild):
    """SSE：构建候选标准全景图（新颖性检测 + 业务域×章节矩阵 + 覆盖盲区）。

    硬约束：新颖性标签全部来自 novelty_check 真实返回；conflict 必附字段级 diff；
    检索源为 D0 已有标准库；D0 为空则明确"未提供"，不套用任何内置库。
    """
    # P4 只依赖 D1（候选来自 D1 抽取）与 D0（已有标准库，可为空）；不读 D3
    gate = _data_gate("a_in_g1", "a_in_g2", any_of=True)
    if gate:
        return gate

    async def gen():
        def emit(ev, data):
            return bridge.format_sse({"event": ev, "data": data, "ts": bridge._now_iso()})

        yield emit("landscape_begin", {"mode": "A", "label": "模式 A 发现式梳理：构建候选标准全景图"})
        lib = await asyncio.to_thread(la.load_mock_library)
        yield emit("library_loaded", {
            "source": lib["source"], "note": lib["source_note"],
            "class_count": lib["class_count"], "field_count": lib["field_count"],
            "conflict_count": lib["conflict_count"],
        })

        if llm.available():
            yield emit("llm_working", {
                "panel": "P4",
                "model": llm.config().get("bulk_model") or llm.config()["model"],
                "label": "大模型：新颖性判断 + 立项优先级（约 60–120 秒）"})
        m = await asyncio.to_thread(la.landscape_map, body.min_density, body.force)
        for k, v in m["degraded"].items():
            yield emit("landscape_degraded", {"item": k, "detail": v})

        for c in m["matrix"]:
            yield emit("landscape_cell", {
                "domain": c["domain"], "section": c["section"],
                "candidate_count": c["candidate_count"], "avg_confidence": c["avg_confidence"],
                "novelty_dist": c["novelty_dist"],
                "candidate_ids": [x["id"] for x in c["candidates"]],
            })

        for c in m["candidates"]:
            nv = c["novelty"]
            yield emit("novelty_result", {
                "candidate_id": c["id"], "term": c["term"], "canonical": c.get("name"),
                "type": c.get("type"), "confidence": c.get("confidence"),
                "status": nv["status"], "similarity": nv["similarity"],
                "matched_standard_id": nv["matched_standard_id"],
                "diff": nv["diff"], "mapping": nv["mapping"], "basis": nv["basis"],
                "definition": c.get("definition", ""),
                "evidence_span": c.get("evidence_span", {}),
                "domain": c.get("domain", ""), "priority": c.get("priority"),
                "priority_level": c.get("priority_level"),
                "priority_rationale": c.get("priority_rationale"),
                # 大模型判断（与规则结论并存，界面标分歧）
                "llm_verdict": c.get("llm_verdict"),
                "llm_matched": c.get("llm_matched"),
                "llm_reason": c.get("llm_reason"),
                "llm_confidence": c.get("llm_confidence"),
                "llm_agrees_rule": c.get("llm_agrees_rule"),
                "llm_priority": c.get("llm_priority"),
                "llm_score": c.get("llm_score"),
                "llm_priority_reason": c.get("llm_priority_reason"),
            })
            if nv["status"] == "conflict":
                await bridge.add_gate({
                    "gate_id": f"g-novelty-{c['id']}", "mode": "A",
                    "reason": "novelty_conflict", "panel": "P4",
                    "item": {"candidate_id": c["id"], "name": c["term"], "type": c.get("type"),
                             "confidence": c.get("confidence"), "diff": nv["diff"],
                             "matched_standard_id": nv["matched_standard_id"]},
                })

        for b in m["blind_spots"]:
            yield emit("blind_spot", b)

        yield emit("landscape_complete", {
            "novelty_dist": m["novelty_dist"],
            "cells": len(m["matrix"]), "blind_spot_count": len(m["blind_spots"]),
            "domains": m["domains"], "sections": m["sections"],
            "config": m["config"], "degraded": m["degraded"], "library": m["library"],
            "candidate_count": len(m["candidates"]),
            "llm": m.get("llm") or {},
        })
        await bridge.publish("landscape_complete", {"mode": "A", "cells": len(m["matrix"])})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.post("/api/landscape/prioritize")
async def landscape_prioritize(body: LandscapePrioritize):
    """计算候选优先级（每项必带 rationale）。"""
    m = await asyncio.to_thread(la.landscape_map)
    cands = m["candidates"]
    if body.candidate_ids:
        want = set(body.candidate_ids)
        cands = [c for c in cands if c["id"] in want]
    ranked = await asyncio.to_thread(la.prioritize_candidates, cands)
    return JSONResponse({"priorities": ranked, "count": len(ranked)})


@app.get("/api/landscape/blindspots")
async def landscape_blindspots(min_density: int = 1):
    """覆盖盲区列表（低于密度阈值的单元格）。"""
    return JSONResponse(await asyncio.to_thread(la.blind_spots, min_density))


@app.post("/api/landscape/approve")
async def landscape_approve(body: LandscapeApprove):
    """提交立项清单 → 写入 deliverables/mode-a/approved-candidates/。"""
    if not body.candidate_ids:
        return JSONResponse({"error": "立项清单为空"}, status_code=400)
    payload = await asyncio.to_thread(la.approve, body.candidate_ids, body.note)
    await bridge.publish("landscape_approved", {"mode": "A", "count": payload["count"]})
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# P5 · 模式 B 需求转化（requirement-analyst + model-synthesizer）
# ---------------------------------------------------------------------------
# 澄清/推断决策与冲突消解决策（进程内会话态；前端也可在 synthesize 时随请求传入覆盖）
CLARIFY_DECISIONS: dict[str, dict] = {}
CONFLICT_DECISIONS: dict[str, dict] = {}


class ClarifyIn(BaseModel):
    decisions: dict[str, dict] = {}


class ConflictIn(BaseModel):
    decisions: dict[str, dict] = {}
    note: str = ""


class SynthesizeIn(BaseModel):
    clarifications: dict[str, dict] | None = None
    conflict_decisions: dict[str, dict] | None = None
    commit: bool = False
    note: str = ""


def _emit_factory():
    def emit(ev, data):
        return bridge.format_sse({"event": ev, "data": data, "ts": bridge._now_iso()})
    return emit


@app.post("/api/requirement/parse")
async def requirement_parse():
    """SSE：区 1 需求解析结果（目标对象类、预期属性含空缺标记、约束、模糊项、来源）。"""
    gate = _data_gate("b_in")
    if gate:
        return gate
    async def gen():
        emit = _emit_factory()
        yield emit("req_begin", {"mode": "B", "label": "区1 需求解析（D2 需求文件）"})
        r = await asyncio.to_thread(ra.parse_requirement)
        yield emit("req_parsed", {
            "file": r["file"], "files": r.get("files", []),
            "file_count": r.get("file_count", 1),
            "sheet": r["sheet"], "target_object": r["target_object"],
            "target_object_label": r.get("target_object_label", r["target_object"]),
            "target_objects": r.get("target_objects", []),
            "target_object_code": r["target_object_code"], "purpose": r["purpose"],
            "scope": r["scope"], "source_spans": r["source_spans"], "degraded": r["degraded"],
        })
        for a in r["expected_attributes"]:
            yield emit("req_attribute", a)
        for x in r["ambiguities"]:
            yield emit("req_ambiguity", x)
        yield emit("req_complete", {"attr_count": len(r["expected_attributes"]),
                                    "ambiguity_count": len(r["ambiguities"]),
                                    "degraded": r["degraded"]})
        await bridge.publish("requirement_parsed", {"mode": "B",
                                                    "target": r["target_object"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/requirement/gap")
async def requirement_gap():
    """SSE：区 2 完备性分析（对照 D4 模板，评分公式可审计）。"""
    gate = _data_gate("b_in") or _data_gate("model_spec")
    if gate:
        return gate
    async def gen():
        emit = _emit_factory()
        yield emit("gap_begin", {"mode": "B", "label": "区2 完备性分析（对照 D4 完整模型模板）"})
        g = await asyncio.to_thread(ra.gap_analyze)
        yield emit("gap_score", {"completeness_score": g["completeness_score"],
                                 "formula": g["formula"], "weights": g["weights"],
                                 "template_field_total": g["template_field_total"],
                                 "status_dist": g["status_dist"]})
        for x in g["gaps"]:
            yield emit("gap_item", x)
        yield emit("gap_complete", {"status_dist": g["status_dist"], "blocking": g["blocking"],
                                    "note": g["note"]})
        await bridge.publish("requirement_gap", {"mode": "B", "score": g["completeness_score"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/requirement/clarify")
async def requirement_clarify(body: ClarifyIn):
    """提交澄清/推断决策（adopt / answer / pending）。"""
    gate = _data_gate("b_in")
    if gate:
        return gate
    if body.decisions:
        CLARIFY_DECISIONS.update(body.decisions)
    c = await asyncio.to_thread(ra.generate_clarification)
    low = [i for i in c["inferences"] if i["confidence"] < ra.CONFIDENCE_REFERENCE_ONLY]
    return JSONResponse({
        "ok": True,
        "applied": len(CLARIFY_DECISIONS),
        "questions": c["questions"], "inferences": c["inferences"],
        "low_confidence_count": len(low),
        "threshold": ra.CONFIDENCE_REFERENCE_ONLY,
        "note": c["note"],
    })


@app.post("/api/requirement/match")
async def requirement_match():
    """SSE：区 3 已有标准匹配（rag_vector 语义召回 + rag_graph 图谱校验）。"""
    gate = _data_gate("b_in")
    if gate:
        return gate
    async def gen():
        emit = _emit_factory()
        yield emit("match_begin", {"mode": "B", "label": "区3 已有标准匹配（rag_vector + rag_graph）"})
        m = await asyncio.to_thread(ra.match_existing)
        yield emit("match_library", m["library"])
        for it in m["items"]:
            yield emit("match_item", it)
        yield emit("match_complete", {"verdict_dist": m["verdict_dist"],
                                      "retrieval": m["retrieval"]})
        await bridge.publish("requirement_match", {"mode": "B", "verdicts": m["verdict_dist"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/requirement/synthesize")
async def requirement_synthesize(body: SynthesizeIn):
    """SSE：区 4 模型合成（每字段标 source，全 COMPLETE 才允许导出）。"""
    gate = _data_gate("b_in")
    if gate:
        return gate
    clar = body.clarifications if body.clarifications is not None else dict(CLARIFY_DECISIONS)
    cdec = body.conflict_decisions if body.conflict_decisions is not None else dict(CONFLICT_DECISIONS)

    async def gen():
        emit = _emit_factory()
        yield emit("syn_begin", {"mode": "B", "label": "区4 模型合成与冲突消解"})
        m = await asyncio.to_thread(ms.synthesize_model, clar, cdec)
        yield emit("syn_overview", {"object_class": m["object_class"],
                                    "object_class_code": m["object_class_code"],
                                    "scope": m["scope"], "field_completeness": m["field_completeness"],
                                    "source_dist": m["source_dist"], "complete": m["complete"],
                                    "gate": m["gate"]})
        for a in m["attributes"]:
            yield emit("syn_field", a)
        if body.commit:
            r = await asyncio.to_thread(ms.commit_final_model, m, body.note)
            yield emit("syn_commit", r)
        yield emit("syn_complete", {"complete": m["complete"], "missing_fields": m["missing_fields"],
                                    "export_allowed": m["export_allowed"], "gate": m["gate"]})
        await bridge.publish("requirement_synthesized", {"mode": "B", "complete": m["complete"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/requirement/conflict/resolve")
async def requirement_conflict_resolve(body: ConflictIn):
    """冲突消解：返回建议，并在传入 decisions 时写入冲突日志。"""
    rc = await asyncio.to_thread(ms.resolve_conflict, ra.gap_analyze()["gaps"])
    log = None
    if body.decisions:
        CONFLICT_DECISIONS.update(body.decisions)
        log = await asyncio.to_thread(ms.commit_conflict_decisions, body.decisions, body.note)
        for f, d in body.decisions.items():
            if d.get("action") == "manual":
                await bridge.add_gate({
                    "gate_id": f"g-model-conflict-{f}", "mode": "B",
                    "reason": "model_conflict", "panel": "P5",
                    "item": {"field": f, "action": "manual", "note": "冲突转专家裁定"},
                })
    return JSONResponse({"ok": True, "conflicts": rc["conflicts"], "count": rc["count"],
                         "auto_resolvable_count": rc["auto_resolvable_count"],
                         "note": rc["note"], "log": log})


@app.post("/api/requirement/export")
async def requirement_export(body: SynthesizeIn):
    """导出最终数据模型（全字段 COMPLETE 才允许）。"""
    clar = body.clarifications if body.clarifications is not None else dict(CLARIFY_DECISIONS)
    cdec = body.conflict_decisions if body.conflict_decisions is not None else dict(CONFLICT_DECISIONS)
    m = await asyncio.to_thread(ms.synthesize_model, clar, cdec)
    r = await asyncio.to_thread(ms.commit_final_model, m, body.note)
    return JSONResponse(r)


# ---------------------------------------------------------------------------
# P6 · 标准生成与演化（双入口：P4 立项清单 / P5 合成模型）
# ---------------------------------------------------------------------------
class StandardGenerate(BaseModel):
    source: str = "A"      # "A" | "B"


class StandardComplete(BaseModel):
    standard_id: str
    patches: dict[str, dict] = {}


class StandardEnrich(BaseModel):
    standard_id: str
    limit: int = 60      # 本次最多处理多少个字段（按批次请求，控制耗时与费用）
    batch: int = 15


@app.post("/api/standard/enrich")
async def standard_enrich(req: StandardEnrich):
    """用大模型给缺失「定义 / 单位」的字段补内容（只填空值，不覆盖业务已填）。"""
    try:
        r = await asyncio.to_thread(sg.enrich_with_llm,
                                    req.standard_id, req.limit, req.batch)
        await bridge.publish("standard_enriched", {"mode": "B",
                                                   "standard_id": req.standard_id,
                                                   "filled": r.get("filled_definition")})
        return JSONResponse(r)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


class EvolveAnalyze(BaseModel):
    standard_id: str


class EvolveCommit(BaseModel):
    standard_id: str
    decisions: dict[str, dict] = {}
    note: str = ""


@app.post("/api/standard/generate")
async def standard_generate(body: StandardGenerate):
    """SSE：生成标准。入口 A 用 P4 立项清单；入口 B 用 P5 合成模型。"""
    if (body.source or "A").upper() == "B":
        gate = _data_gate("b_in")
        if gate:
            return gate
    async def gen():
        emit = _emit_factory()
        src = (body.source or "A").upper()
        yield emit("std_begin", {"source": src, "label": f"入口 {src}：生成标准"})
        try:
            std = await asyncio.to_thread(
                sg.draft_from_mode_a if src == "A" else sg.draft_from_mode_b)
        except FileNotFoundError as e:
            yield emit("std_failed", {"error": str(e)})
            return

        yield emit("std_created", {"standard_id": std["standard_id"],
                                   "source_path": std["source_path"],
                                   "source_note": std["source_note"],
                                   "object_class_name": std["object_class_name"],
                                   "class_count": std.get("class_count", 1),
                                   "classes": std.get("classes", []),
                                   "attribute_count": len(std["attribute_list"])})
        # 落盘：否则「按 standard_id 导出完整模型」会找不到文件
        try:
            sg.save_standard(std)
        except Exception:
            pass
        for s in std["field_specs"]:
            yield emit("std_field", s)
            if s["status"] == "INCOMPLETE":
                await bridge.add_gate({
                    "gate_id": f"g-incomplete-{std['standard_id']}-{s['name']}",
                    "mode": std["source_path"], "reason": "INCOMPLETE", "panel": "P6",
                    "item": {"standard_id": std["standard_id"], "field": s["name"],
                             "missing": s["missing"], "status": "INCOMPLETE"},
                })
        yield emit("std_quality", {**std["quality"], "usability": std["usability"]})
        yield emit("std_complete", {
            "standard_id": std["standard_id"], "source_path": std["source_path"],
            "source_note": std["source_note"], "usability": std["usability"],
            "quality": std["quality"],
            "incomplete_fields": [s["name"] for s in std["field_specs"]
                                  if s["status"] == "INCOMPLETE"],
        })
        await bridge.publish("standard_generated", {"mode": std["source_path"],
                                                    "standard_id": std["standard_id"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/standard/complete")
async def standard_complete(body: StandardComplete):
    """人工补全 INCOMPLETE 字段（并重算质量与可使用率）。"""
    try:
        r = await asyncio.to_thread(sg.complete_standard, body.standard_id, body.patches)
    except (KeyError, FileNotFoundError) as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    for f in r["applied"]:
        await bridge.resolve_gate(f"g-incomplete-{body.standard_id}-{f}", "confirm")
    return JSONResponse(r)


@app.get("/api/standard/list")
async def standard_list():
    return JSONResponse({"standards": sg.list_standards()})


@app.get("/api/standard/export")
async def standard_export(mode: str = "B", group: str | None = None,
                          standard_id: str | None = None):
    """导出完整数据模型表格（xlsx，对象类 / 对象类属性 / 属性 / 说明，按 规则.md）。"""
    try:
        p, _meta = mex.export_xlsx(mode, group, standard_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return FileResponse(
        p, filename=os.path.basename(p),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/eval/groups")
async def eval_groups(mode: str = "B"):
    """某模式下的数据集组（A 有 group1/group2，B 只有 default）。"""
    try:
        return JSONResponse({"mode": mode.upper(), "groups": registry.list_groups(mode)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/evolve/analyze")
async def evolve_analyze(body: EvolveAnalyze):
    """SSE：演化匹配（rag_vector + rag_graph + diff_compare）。"""
    async def gen():
        emit = _emit_factory()
        yield emit("evo_begin", {"standard_id": body.standard_id, "label": "演化匹配"})
        try:
            res = await asyncio.to_thread(es.analyze, body.standard_id)
        except KeyError as e:
            yield emit("evo_failed", {"error": str(e)})
            return
        yield emit("evo_library", res["library"])
        for it in res["items"]:
            yield emit("evo_item", it)
        for m in res["merge_suggestions"]:
            yield emit("merge_suggestion", m)
        yield emit("evo_complete", {"standard_id": res["standard_id"],
                                    "verdict_dist": res["verdict_dist"],
                                    "merge_count": len(res["merge_suggestions"]),
                                    "retrieval": res["retrieval"]})
        await bridge.publish("evolve_analyzed", {"mode": res["source_path"],
                                                 "verdicts": res["verdict_dist"]})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/evolve/commit")
async def evolve_commit(body: EvolveCommit):
    """提交演化决策；manual 项进 P8 门控。"""
    try:
        log = await asyncio.to_thread(es.commit, body.standard_id, body.decisions, body.note)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    for f, d in (body.decisions or {}).items():
        if d.get("action") == "manual":
            await bridge.add_gate({
                "gate_id": f"g-merge-{body.standard_id}-{f}", "mode": "shared",
                "reason": "merge_suggestion", "panel": "P6",
                "item": {"standard_id": body.standard_id, "field": f, "note": "合并建议需人工确认"},
            })
    return JSONResponse(log)


# ---------------------------------------------------------------------------
# 大模型接入（状态 / 连通性自检）
# ---------------------------------------------------------------------------
@app.get("/api/llm/status")
async def llm_status():
    """是否已接入大模型、用的哪个模型。绝不回传 key。"""
    return JSONResponse(llm.status())


@app.post("/api/llm/selftest")
async def llm_selftest():
    """做一次极小的真实调用，用来现场证明「不是规则引擎冒充」。"""
    if not llm.available():
        return JSONResponse({"ok": False, "error": llm.status()["reason"]}, status_code=400)
    try:
        return JSONResponse(await asyncio.to_thread(llm.selftest))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/gates")
async def gates(mode: str | None = None, status: str | None = None, panel: str | None = None):
    """P8 人机门控队列。"""
    return JSONResponse({"gates": bridge.list_gates(mode=mode, status=status, panel=panel)})


# ---------------------------------------------------------------------------
# P7 · D7 评测（数据标准生成准确率）
# ---------------------------------------------------------------------------
class EvalRun(BaseModel):
    mode: str = "B"
    group: str | None = None          # A: group1 / group2；B: default
    # standard（默认，唯一口径）：评 P6 当前数据集产物；P7 不生成标准
    # test_input 仅保留给离线一致性校验脚本使用，界面已移除入口
    pred_source: str = "standard"
    standard_id: str | None = None
    scope_to_class: bool | None = None


@app.post("/api/eval/run")
async def eval_run(req: EvalRun):
    """按 (mode, group) 用「输入」生成标准，与该组「预期输出」比对。"""
    try:
        return JSONResponse(ev.evaluate(req.mode, req.group, standard_id=req.standard_id,
                                        scope_to_class=req.scope_to_class,
                                        pred_source=req.pred_source))
    except Exception as e:  # 数据缺失统一转 400，前端显示原因
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/eval/latest")
async def eval_latest(mode: str = "B", group: str | None = None):
    m = str(mode).upper()
    g = group or ("default" if m == "B" else "group1")
    p = ev.EVAL_DIR / f"latest-{m}-{g}.json"
    if not p.exists():
        return JSONResponse({"error": f"尚无 {m}/{g} 的评测结果，请先运行评测"}, status_code=404)
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/eval/file")
async def eval_file(kind: str = "csv"):
    """下载评测产物：kind=csv 明细 / kind=md 报告。"""
    name = "match_detail.csv" if kind == "csv" else "test_report.md"
    p = ev.EVAL_DIR / name
    if not p.exists():
        return JSONResponse({"error": "文件不存在，请先运行评测"}, status_code=404)
    return FileResponse(p, filename=name)


# ---------------------------------------------------------------------------
# 历史梳理存档（只读快照，可显式恢复为当前数据集）
# ---------------------------------------------------------------------------
class ArchiveSave(BaseModel):
    mode: str = "B"
    title: str | None = None
    eval_snapshot: dict | None = None


@app.post("/api/archive/save")
async def archive_save(req: ArchiveSave):
    try:
        return JSONResponse(am.save(req.mode, req.title, req.eval_snapshot))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/archive/list")
async def archive_list(mode: str | None = None):
    try:
        items = am.list_archives(mode)
        return JSONResponse({"items": items, "count": len(items)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/archive/{aid}/file")
async def archive_file(aid: str, kind: str = "model", name: str | None = None):
    try:
        p = am.archive_file(aid, kind, name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return FileResponse(p, filename=p.name)


@app.post("/api/archive/{aid}/restore")
async def archive_restore(aid: str):
    try:
        return JSONResponse(am.restore(aid))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/archive/{aid}")
async def archive_delete(aid: str):
    try:
        return JSONResponse(am.delete(aid))
    except KeyError:
        return JSONResponse({"error": "未找到归档"}, status_code=404)


@app.get("/api/archive/{aid}")
async def archive_get(aid: str):
    try:
        return JSONResponse(am.get(aid))
    except KeyError:
        return JSONResponse({"error": "未找到归档"}, status_code=404)


# ---------------------------------------------------------------------------
# 演示数据集（内置「大宗材料类-型钢」+ 阶段缓存回放）
# ---------------------------------------------------------------------------
@app.get("/api/demo/status")
async def demo_status():
    """演示预设状态：是否可用 / 是否启用 / 已录哪些阶段 / P0 是否已装载。"""
    return JSONResponse(demo.status())


@app.post("/api/demo/activate")
async def demo_activate(restore: bool = True):
    """载入内置演示数据：装 P0 三件套 + 铺产物快照 + 打开回放。"""
    r = demo.activate(restore=restore)
    if r.get("ok"):
        um.invalidate_caches()   # 换了一套数据，进程内缓存必须整体失效
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@app.post("/api/demo/exit")
async def demo_exit():
    """退出演示态：还原用户原有数据，预设缓存不再参与任何请求。"""
    r = demo.deactivate()
    um.invalidate_caches()
    return JSONResponse(r)


@app.post("/api/demo/capture")
async def demo_capture(on: bool = True):
    """录制开关：打开后点一遍流程，每个计算接口的真实响应都会落进预设。"""
    demo._set_state(capture=bool(on))
    return JSONResponse({"ok": True, "capture": bool(on), "status": demo.status()})


@app.post("/api/demo/snapshot")
async def demo_snapshot():
    """把当前产物保存成预设快照（录制完成后点一次）。"""
    try:
        r = demo.save_snapshot()
        return JSONResponse({"ok": True, **r, "status": demo.status()})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return JSONResponse({
        "ok": True,
        "roles": bridge.list_roles(),
        "data_root": str(bridge.DATA_ROOT),
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8123, reload=False)
