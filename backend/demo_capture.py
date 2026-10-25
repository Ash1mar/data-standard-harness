"""
demo_capture.py
===============
把「模式 B · 大宗材料类-型钢」真实跑一遍，逐阶段录进内置演示预设。

用法（后端已在 8123 运行时）：
    .venv/Scripts/python.exe backend/demo_capture.py
    .venv/Scripts/python.exe backend/demo_capture.py --from p5   # 只补录 P5 之后
    .venv/Scripts/python.exe backend/demo_capture.py --only p2   # 只补录某一阶段

它做的事：
1. 把 P0 三件套从当前上传槽位 / data/规则.md 复制进 `data/_presets/型钢-B/p0/`
2. 打开录制开关（`/api/demo/capture?on=true`）
3. 按 P1→P7 顺序打真实接口（请求体与前端按钮完全一致，否则回放命中不了）
4. 关录制 → 保存产物快照 → 激活演示态

录的是**真实响应**（SSE 全量事件流 / JSON），回放时逐块吐回去，
所以演示看到的数据与这次真跑一字不差；P2 的模型裁决理由也在
`data/.llm_cache/confidence.json` 里一并快照，断网也能演示。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8123"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import demo_preset as demo  # noqa: E402


def _post(path: str, body: dict | None = None, timeout: int = 1800) -> tuple[int, str]:
    data = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _get(path: str, timeout: int = 120) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for blk in text.split("\n\n"):
        blk = blk.strip()
        if not blk:
            continue
        ev, data = "", None
        for line in blk.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    data = None
        if ev:
            out.append((ev, data or {}))
    return out


def _stage_line(name: str, code: int, text: str) -> str:
    if code != 200:
        return f"  ✗ {name}：HTTP {code} {text[:160]}"
    evs = _events(text)
    if not evs:      # JSON 响应
        try:
            d = json.loads(text)
            keys = list(d)[:6]
            return f"  ✓ {name}：JSON {len(text)} 字节 {keys}"
        except Exception:
            return f"  ✓ {name}：{len(text)} 字节"
    tail = evs[-1]
    return f"  ✓ {name}：{len(evs)} 个事件，末事件 {tail[0]}"


# ---------------------------------------------------------------------------
# 1) P0 三件套入库（预设侧）
# ---------------------------------------------------------------------------
def stage_p0() -> bool:
    p0 = demo.PRESET_DIR / "p0"
    p0.mkdir(parents=True, exist_ok=True)
    srcs = {
        "大宗材料类-型钢.xlsx": ROOT / "data" / "uploaded" / "b_in" / "大宗材料类-型钢.xlsx",
        "数据模型-大宗材料类-EM4型钢.xlsx": ROOT / "data" / "uploaded" / "b_gold" / "数据模型-大宗材料类-EM4型钢.xlsx",
        "规则.md": ROOT / "data" / "规则.md",
    }
    ok = True
    for name, src in srcs.items():
        if not src.exists():
            print(f"  ✗ 缺少 {name}（{src}）")
            ok = False
            continue
        shutil.copy2(src, p0 / name)
        print(f"  ✓ {name}  {src.stat().st_size / 1024:.0f} KB → {demo._rel(p0 / name)}")
    return ok


# ---------------------------------------------------------------------------
# 2) 阶段序列（请求体与前端按钮一字不差）
# ---------------------------------------------------------------------------
def build_plan():
    return [
        ("p1", "P1 文档解析", "/api/parse", {"mode": "B", "source": "sample"}),
        ("p2", "P2 知识抽取", "/api/extract", {"mode": "B", "source": "sample"}),
        ("p3", "P3 底座构建", "/api/graph/build", {"mode": "B", "with_candidates": True}),
        ("p3b", "P3 关系入图", "/api/graph/relations/apply",
         {"mode": "B", "min_confidence": 0.6, "types": ["subClassOf", "partOf", "associates"]}),
        ("p4", "P4 场景梳理", "/api/landscape/build", {"min_density": 1, "force": False}),
        ("p5a", "P5 需求解析", "/api/requirement/parse", {}),
        ("p5b", "P5 完备性分析", "/api/requirement/gap", {}),
        ("p5c", "P5 澄清推断", "/api/requirement/clarify", {"decisions": {}}),
        ("p5d", "P5 标准匹配", "/api/requirement/match", {}),
        ("p5e", "P5 冲突清单", "/api/requirement/conflict/resolve", {"decisions": {}, "note": ""}),
        ("p5f", "P5 模型合成", "/api/requirement/synthesize",
         {"clarifications": None, "conflict_decisions": None, "commit": False, "note": "P5 合成"}),
        ("p6a", "P6 标准生成", "/api/standard/generate", {"source": "B"}),
        ("p6b", "P6 演化匹配", "/api/evolve/analyze", None),   # standard_id 运行时填
        ("p7", "P7 准确率评测", "/api/eval/run",
         {"mode": "B", "group": "default", "pred_source": "standard"}),
    ]


def run_capture(only: str = "", start_from: str = "") -> int:
    plan = build_plan()
    if only:
        plan = [p for p in plan if p[0] == only]
    if start_from:
        keys = [p[0] for p in plan]
        if start_from in keys:
            plan = plan[keys.index(start_from):]
    std_id = ""
    rc = 0
    for key, name, path, body in plan:
        if path == "/api/evolve/analyze":
            if not std_id:
                print("  ! 没有 standard_id，跳过演化匹配")
                continue
            body = {"standard_id": std_id}
        t0 = time.time()
        code, text = _post(path, body)
        dt = time.time() - t0
        print(f"{_stage_line(name, code, text)}   [{dt:.1f}s]", flush=True)
        if code != 200:
            rc = 1
            continue
        evs = _events(text)
        if path == "/api/standard/generate":
            for ev, d in evs:
                if ev == "std_created":
                    std_id = d.get("standard_id") or ""
                    print(f"    标准号 {std_id}")
                if ev == "std_failed":
                    print(f"    ! 生成失败：{d.get('error')}")
        if path == "/api/extract":
            for ev, d in evs:
                if ev == "extract_complete":
                    print(f"    候选 {d.get('candidate_count')} 条，低置信 {d.get('low_confidence_count')}，"
                          f"模型 {json.dumps(d.get('llm') or {}, ensure_ascii=False)[:180]}")
        if path == "/api/graph/build":
            for ev, d in evs:
                if ev == "graph_complete":
                    print(f"    节点 {d['stats']['total_nodes']} / 边 {d['stats']['total_edges']}，"
                          f"OWL {'passed' if d['owl_profile']['passed'] else 'failed'}")
        if path == "/api/eval/run":
            try:
                d = json.loads(text)
                ov = d.get("overall") or {}
                print(f"    生成准确率 {ov.get('gen_accuracy')}% 覆盖率 {ov.get('coverage')}%")
            except Exception:
                pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某一阶段（p1/p2/p3/p5a/p6a/p7…）")
    ap.add_argument("--from", dest="start_from", default="", help="从某阶段开始往后跑")
    ap.add_argument("--skip-p0", action="store_true", help="不重抄 P0 三件套")
    ap.add_argument("--no-activate", action="store_true", help="录完不激活演示态")
    args = ap.parse_args()

    print("== 健康检查 ==")
    code, text = _get("/api/health")
    if code != 200:
        print(f"后端未就绪：{text}")
        return 2
    print("  ✓ 后端在线")

    if not args.skip_p0:
        print("== P0 三件套 → 预设 ==")
        if not stage_p0():
            print("P0 文件不全，中止")
            return 2

    print("== 打开录制 ==")
    code, text = _post("/api/demo/capture?on=true")
    print(f"  {code} {text[:120]}")

    print("== 真实跑一遍并录制 ==")
    rc = run_capture(only=args.only, start_from=args.start_from)

    print("== 关闭录制 ==")
    _post("/api/demo/capture?on=false")

    print("== 保存产物快照 ==")
    code, text = _post("/api/demo/snapshot")
    try:
        d = json.loads(text)
        print(f"  files={d.get('files')} mb={d.get('mb')} at={d.get('snapshot_at')}")
    except Exception:
        print(f"  {code} {text[:200]}")

    if not args.no_activate:
        print("== 激活演示态（回放）==")
        code, text = _post("/api/demo/activate?restore=true")
        try:
            d = json.loads(text)
            print(f"  ok={d.get('ok')} 快照 {d.get('snapshot')}")
        except Exception:
            print(f"  {code} {text[:200]}")

    code, text = _get("/api/demo/status")
    try:
        st = json.loads(text)
        print(f"== 预设状态 ==\n  可用 {st['available']} 启用 {st['active']} "
              f"阶段 {st['stage_count']} 个 快照 {st['snapshot_mb']}MB  {st['model_outputs']}")
        for s in st["stages"]:
            print(f"  · {s['label']:<16} {s['recorded_at']}  {s['bytes']/1024:.0f}KB")
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
