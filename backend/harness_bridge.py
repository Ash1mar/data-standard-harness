"""
harness_bridge.py
=================
核电数据标准智能治理智能体（双模式）· Harness 编排集成层。

所有与外层 Harness / UI / 数据工件的互动都封装在此模块，便于：
- 数据就绪状态统一加载（单一可信源：data/readiness_status.json）
- SSE 事件广播（全局事件总线，供 P0 事件流与执行轨迹使用）
- A/B 流水线阶段定义（来自 SKILL 3.2 角色编排路径，非编造）
- 事件格式化（遵循 SKILL 4.1 SSE 事件 schema）

阶段 0 仅实现「骨架」：事件总线与就绪状态真实可读，流水线阶段为真实阶段名、
但工具逻辑在后续增量实现。任何产出都可追溯到此处发出的真实事件。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

# ---------------------------------------------------------------------------
# 路径约定
# ---------------------------------------------------------------------------
# 项目根 = 技能约定的 nuclear-standard-agent/ 根（本工作区即根）
ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"
DOTDSH = ROOT / ".dsh"
ROLES_DIR = DOTDSH / "roles"

READINESS_STATUS_FILE = DATA_ROOT / "readiness_status.json"
D0_MANIFEST_FILE = DATA_ROOT / "full_mode" / "d0_manifest.json"
SPLIT_MANIFEST_FILE = DATA_ROOT / "split_manifest.json"
LEAKAGE_FILE = DATA_ROOT / "leakage.json"

# ---------------------------------------------------------------------------
# 全局事件总线
# ---------------------------------------------------------------------------
# 单消费者队列的广播：每个 SSE 订阅者各自持有一个 asyncio.Queue，
# publish() 向所有订阅者投递同一事件副本。
_SUBSCRIBERS: set[asyncio.Queue] = set()
_BUS_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def subscribe() -> asyncio.Queue:
    """注册一个新的事件订阅者队列。"""
    q: asyncio.Queue = asyncio.Queue()
    async with _BUS_LOCK:
        _SUBSCRIBERS.add(q)
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _BUS_LOCK:
        _SUBSCRIBERS.discard(q)


async def publish(event: str, data: dict[str, Any]) -> None:
    """向所有订阅者广播一条事件。事件自动附带时间戳。"""
    payload = {"event": event, "data": data, "ts": _now_iso()}
    async with _BUS_LOCK:
        for q in list(_SUBSCRIBERS):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


def format_sse(payload: dict[str, Any]) -> str:
    """将内部事件载荷格式化为 SSE 文本块。"""
    return f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"


async def event_stream() -> AsyncIterator[str]:
    """SSE 生成器：从全局总线持续产出事件，支持断开清理。"""
    q = await subscribe()
    try:
        # 连接建立时先发一个 hello，便于前端确认
        yield format_sse({"event": "connected", "data": {"ts": _now_iso()}, "ts": _now_iso()})
        while True:
            payload = await q.get()
            yield format_sse(payload)
    finally:
        await unsubscribe(q)


# ---------------------------------------------------------------------------
# 数据就绪状态
# ---------------------------------------------------------------------------
def load_readiness_status() -> dict[str, Any]:
    """读取单一可信源 readiness_status.json。文件缺失时返回安全降级结构。"""
    if not READINESS_STATUS_FILE.exists():
        return {
            "schema": "fallback",
            "generated_at": _now_iso(),
            "items": [],
            "summary": "readiness_status.json 缺失，请先运行阶段 -1 就绪校验。",
        }
    return json.loads(READINESS_STATUS_FILE.read_text(encoding="utf-8"))


def status_color(status: str) -> str:
    return {"ready": "green", "degraded": "amber", "missing": "red"}.get(status, "gray")


# ---------------------------------------------------------------------------
# A/B 流水线阶段定义（SKILL 3.2）
# ---------------------------------------------------------------------------
PIPELINE_STAGES: dict[str, list[dict[str, str]]] = {
    "A": [
        {"stage": "doc-parser", "label": "文档解析", "role": "doc-parser"},
        {"stage": "knowledge-extractor", "label": "知识抽取", "role": "knowledge-extractor"},
        {"stage": "ontology-builder", "label": "本体构建", "role": "ontology-builder"},
        {"stage": "landscape-analyst", "label": "全景与新奇性分析", "role": "landscape-analyst", "gate": True},
        {"stage": "standard-generator", "label": "标准草案生成", "role": "standard-generator"},
        {"stage": "standard-evolver", "label": "标准演化", "role": "standard-evolver"},
        {"stage": "quality-auditor", "label": "质量评价", "role": "quality-auditor"},
    ],
    "B": [
        {"stage": "doc-parser", "label": "需求文件解析", "role": "doc-parser"},
        {"stage": "requirement-analyst", "label": "需求解析与完备性", "role": "requirement-analyst", "gate": True},
        {"stage": "standard-evolver", "label": "已有标准匹配", "role": "standard-evolver"},
        {"stage": "model-synthesizer", "label": "模型合成", "role": "model-synthesizer", "gate": True},
        {"stage": "quality-auditor", "label": "质量评价", "role": "quality-auditor"},
    ],
}


async def run_pipeline(mode: str, session_id: str) -> None:
    """
    阶段 0 流水线骨架：按真实阶段名逐段推进并广播事件。
    注意：本增量仅发出阶段标记（stage_start / stage_complete），
    真实工具调用逻辑在后续增量（增量 1~7）实现；不编造业务产出。
    """
    if mode not in PIPELINE_STAGES:
        await publish("error", {"message": f"未知模式 {mode}"})
        return
    stages = PIPELINE_STAGES[mode]
    await publish("pipeline_start", {"mode": mode, "session_id": session_id, "stage_count": len(stages)})
    for i, st in enumerate(stages, start=1):
        await publish("stage_start", {
            "mode": mode, "session_id": session_id, "stage": st["stage"],
            "label": st["label"], "role": st["role"], "index": i, "total": len(stages),
        })
        # 阶段 0 骨架：用短延时模拟阶段推进（真实逻辑后续接入）
        await asyncio.sleep(0.6)
        await publish("stage_complete", {
            "mode": mode, "session_id": session_id, "stage": st["stage"],
            "label": st["label"], "index": i, "total": len(stages),
            "summary": {
                "status": "skeleton",
                "note": "阶段0骨架：工具实现于后续增量；此处仅广播真实阶段标记",
            },
        })
    await publish("pipeline_complete", {
        "mode": mode, "session_id": session_id,
        "summary": f"模式 {mode} 流水线骨架走通 {len(stages)} 个阶段",
    })


# ---------------------------------------------------------------------------
# 角色清单
# ---------------------------------------------------------------------------
def list_roles() -> list[str]:
    if not ROLES_DIR.exists():
        return []
    return sorted(p.stem for p in ROLES_DIR.glob("*.md"))


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_SEQ = 0


async def start_session(mode: str) -> dict[str, Any]:
    """创建会话并广播模式选择事件（P0 验收锚点：带 [A]/[B] 标签的事件）。"""
    global _SESSION_SEQ
    _SESSION_SEQ += 1
    session_id = f"s-{_SESSION_SEQ:04d}"
    sess = {"session_id": session_id, "mode": mode, "created_at": _now_iso(), "events": 0}
    _SESSIONS[session_id] = sess
    await publish("mode_selected", {
        "session_id": session_id, "mode": mode,
        "label": "模式A · 发现式标准梳理" if mode == "A" else "模式B · 需求转化",
    })
    return sess


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    return _SESSIONS.get(session_id)


# ---------------------------------------------------------------------------
# 人机门控中心（P8）· 统一门控存储
# ---------------------------------------------------------------------------
# 门控类型：共享（low_confidence / incomplete / merge / parse_quality / quality_low）
#           模式 A（novelty_conflict）模式 B（requirement_gap / model_conflict）
_GATES: list[dict[str, Any]] = []
_GATE_SEQ = 0


async def add_gate(gate: dict[str, Any]) -> dict[str, Any]:
    """登记一个门控项并广播 gate_required 事件。"""
    global _GATE_SEQ
    _GATE_SEQ += 1
    gate_id = gate.get("gate_id") or f"g-{_GATE_SEQ:04d}"
    g = {
        "gate_id": gate_id,
        "created_at": _now_iso(),
        "status": "open",
        "decision": None,
        "note": "",
        **gate,
    }
    _GATES.append(g)
    await publish("gate_required", g)
    return g


def list_gates(mode: Optional[str] = None, status: Optional[str] = None,
               panel: Optional[str] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in _GATES:
        if mode and g.get("mode") != mode:
            continue
        if status and g.get("status") != status:
            continue
        if panel and g.get("panel") != panel:
            continue
        out.append(g)
    return out


def get_gate(gate_id: str) -> Optional[dict[str, Any]]:
    for g in _GATES:
        if g["gate_id"] == gate_id:
            return g
    return None


async def resolve_gate(gate_id: str, decision: str, note: str = "") -> Optional[dict[str, Any]]:
    """处置门控项：confirm / reject / escalate。广播 gate_resolved。"""
    for g in _GATES:
        if g["gate_id"] == gate_id:
            g["status"] = "resolved"
            g["decision"] = decision
            g["note"] = note
            await publish("gate_resolved", {"gate_id": gate_id, "decision": decision, "note": note})
            return g
    return None


# 抽取候选人工处置记录（P2 准确率抽样后置）
_EXTRACT_REVIEWS: dict[str, dict[str, Any]] = {}


def record_extract_review(candidate_id: str, decision: str, note: str = "") -> dict[str, Any]:
    rec = {"candidate_id": candidate_id, "decision": decision,
           "note": note, "at": _now_iso()}
    _EXTRACT_REVIEWS[candidate_id] = rec
    return rec


def get_extract_reviews() -> dict[str, dict[str, Any]]:
    return dict(_EXTRACT_REVIEWS)


# ---------------------------------------------------------------------------
# 数据工件探测（给 P0 状态面板补充实时计数）
# ---------------------------------------------------------------------------
def probe_data_artifacts() -> dict[str, Any]:
    """探测真实工件，返回供 UI 展示的底层数据事实（不替代 readiness_status）。"""
    facts: dict[str, Any] = {}
    if D0_MANIFEST_FILE.exists():
        facts["d0_manifest"] = json.loads(D0_MANIFEST_FILE.read_text(encoding="utf-8"))
    if SPLIT_MANIFEST_FILE.exists():
        facts["split_manifest"] = json.loads(SPLIT_MANIFEST_FILE.read_text(encoding="utf-8"))
    if LEAKAGE_FILE.exists():
        facts["leakage"] = json.loads(LEAKAGE_FILE.read_text(encoding="utf-8"))
    return facts
