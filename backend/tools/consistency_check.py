"""两条生成链路的离线一致性校验（不暴露给界面）。

背景：P7 早期为了能独立评分，除了「读 P6 产物」还有一条「从输入现场重算」的实现
（`eval_pipeline`）。现在界面只评 P6 产物，但这条捷径仍留在代码里 **只用于开发对账**：
它会暴露两条链路的实现漂移（历史上抓到过：P5 读错列导致 87 个数值字段判成 String）。

用法（在 backend 目录下）：
    python -m tools.consistency_check              # 校验 B
    python -m tools.consistency_check --mode A --group group2
    python -m tools.consistency_check --strict     # 有差异时退出码 1

输出：行数、完全一致的行数、差异明细（按类名/属性名/类型/约束/单位聚合）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import evaluator as ev          # noqa: E402
from tools import eval_pipeline as ep      # noqa: E402


def _key(x: dict) -> tuple:
    return (
        str(x.get("class_name") or "").strip(),
        str(x.get("name") or "").strip(),
        str(x.get("data_type") or "").strip(),
        str(x.get("constraint") or "").strip(),
        str(x.get("unit") or "").strip(),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="B")
    ap.add_argument("--group", default=None)
    ap.add_argument("--strict", action="store_true", help="有差异时返回非零退出码")
    a = ap.parse_args()

    group = a.group or ("default" if a.mode == "B" else "group1")

    try:
        std = ev.load_pred(a.mode)
    except Exception as e:
        print(f"[跳过] 没有 P6 产物可对照：{e}")
        return

    pred = ep.build_pred(a.mode, group)
    A = {_key(x) for x in pred.get("attrs") or []}
    B = {_key(x) for x in std.get("rows") or []}

    print(f"模式 {a.mode}/{group}")
    print(f"  现场重算（冻结，仅对账用）：{len(A)} 行")
    print(f"  P6 产物                  ：{len(B)} 行")
    print(f"  完全一致                 ：{len(A & B)} 行")

    only_a, only_b = A - B, B - A
    if not only_a and not only_b:
        print("  ✅ 两条链路输出完全一致")
        return

    print(f"  仅重算有 {len(only_a)} 行 / 仅产物有 {len(only_b)} 行")

    classes = Counter()
    for c, n, *_ in list(only_a) + list(only_b):
        classes[c] += 1
    print("  差异按对象类聚合：")
    for c, n in classes.most_common(10):
        print(f"    {c or '（未归属）'}：{n} 行")

    print("  样例：")
    for x in list(only_a)[:3]:
        print("    仅重算：", x)
    for x in list(only_b)[:3]:
        print("    仅产物：", x)

    if a.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
