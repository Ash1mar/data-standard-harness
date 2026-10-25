"""一次性把某个标准里缺失的定义/单位补全（CLI）。

用法（在 backend 目录下）：
    python -m tools.enrich_all                    # 用 latest-B 标准，补全部
    python -m tools.enrich_all --limit 200        # 只补前 200 条
    python -m tools.enrich_all --standard-id STD-B-225629 --batch 12 --workers 4

说明：默认用 data/llm_config.json 里的 bulk_model（快模型），4 并发；
855 条约需 8–12 分钟。中途失败不影响已补的部分（每批完成即写入）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import llm_client as llm          # noqa: E402
from tools import standard_generator as sg   # noqa: E402


def latest_standard_id(mode: str = "B") -> str:
    p = sg.STANDARD_DIR / f"latest-{mode}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8-sig"))["standard_id"]
    cands = sorted(sg.STANDARD_DIR.glob(f"STD-{mode}-*.json"),
                   key=lambda x: x.stat().st_mtime)
    if not cands:
        raise SystemExit(f"没有可用的 {mode} 标准，请先在 P6 生成")
    return cands[-1].stem


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--standard-id", default="")
    ap.add_argument("--mode", default="B")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    st = llm.status()
    if not st["available"]:
        raise SystemExit("大模型不可用：" + (st.get("reason") or "unknown"))
    print(f"模型 {st['model']} / 批量模型 {llm.config().get('bulk_model') or st['model']}")

    sid = a.standard_id or latest_standard_id(a.mode)
    print("目标标准:", sid)
    t = time.time()
    r = sg.enrich_with_llm(sid, limit=a.limit, batch=a.batch, workers=a.workers)
    print(f"耗时 {round(time.time() - t, 1)}s | 批次数 {r['batches']} | "
          f"定义 +{r['filled_definition']} / 单位 +{r['filled_unit']}")
    if r["errors"]:
        print("错误:", *r["errors"][:3], sep="\n  ")
    print("规范性:", r["quality"]["normativity"], "| 缺口:", json.dumps(
        r["usability"]["gaps"], ensure_ascii=False))


if __name__ == "__main__":
    main()
