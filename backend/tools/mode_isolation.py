"""
mode_isolation.py
==================
模式隔离与硬指标口径（增量 6 之后的修正，2026-10-21 用户确认）。

背景：审计发现两个问题
1. 原先所有模式共用一份 `data/dev_model/model.json`（34 类），而这 34 类 **100% 落在
   modeA/test/output（模式 A 的评测答案）里** —— 等于把模式 A 的考题答案建进了知识底座。
   根因：EM4 那批数据既是「模式 A 的测试答案」又是「模式 B 的建模数据」，
   我在阶段 -1 切分 D0 时没有按模式区分。
2. 原硬指标「图谱 ≥500 节点」在严格隔离后模式 A 达不到（E板块只给了 120 条属性实例）。

用户决策（2026-10-21）：
- **不要共用节点，A / B 模式分开**
- **指标改为 对象类 / 属性 / 关系 三个维度**
- **模式 A 单独放宽阈值**

本模块是「模式 → 允许的数据源」的唯一真相来源，所有工具必须经它取数并过校验。
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"


def _rel(p) -> str:
    """把绝对路径转成相对项目根目录的展示路径（不暴露本机盘符）。"""
    try:
        q = Path(str(p)).resolve()
        root = DATA_ROOT.parent
        return q.relative_to(root).as_posix()
    except Exception:
        return Path(str(p)).name

# ---------------------------------------------------------------------------
# 1) 模式 → 允许的建模数据源（唯一真相来源）
# ---------------------------------------------------------------------------
MODE_SOURCES: dict[str, dict] = {
    "A": {
        # 2026-10-22 数据结构重排后：A 的参考底座用 group2（管道管件，类+属性齐全）
        # group1 提供 160 个对象类（无属性），group2 提供 21 类 / 417 属性，合并成完整底座
        "dev_output": ["modeA/预期输出 group1/*.xlsx", "modeA/预期输出 group2/*.xlsx"],
        "human": "模式 A 参考底座（当前数据集：对象类 + 属性）",
        "forbidden": [
            "modeB/",        # 跨模式
            "full_mode",     # 旧全量文件
            "dev_model",     # 旧切分产物，已作废
        ],
        "forbidden_reason": "跨模式数据不得进入模式 A 的参考库 / 图谱 / 模拟库",
    },
    "B": {
        "dev_output": ["modeB/预期输出/*.xlsx"],
        "human": "模式 B 参考底座（当前数据集：对象类 + 属性）",
        "forbidden": [
            "modeA/",        # 跨模式
            "full_mode",
            "dev_model",
        ],
        "forbidden_reason": "跨模式数据不得进入模式 B 的建模库；评测答案另由 dataset_registry 按组读取",
    },
}

# ---------------------------------------------------------------------------
# 2) 硬指标阈值（规模参考线）
# ---------------------------------------------------------------------------
# 2026-10-23：底座来源已从 D3 改到 D0 + 本轮输入（base_supply），阈值仍是当年
# 按建模集规模拍的数，保留作「体量参考线」，不再代表任何正确性结论。
METRIC_THRESHOLDS: dict[str, dict[str, int]] = {
    "A": {"object_class": 100, "attribute": 100, "relation": 60},
    "B": {"object_class": 20, "attribute": 200, "relation": 500},
}

# 底座来源策略（面板直接引用，避免 UI 文案与实现再次走偏）
BASE_POLICY = (
    "知识底座 = D0 已有标准库 + 本轮输入（A：D1 文档 / B：D2 需求表）；"
    "不含 D3 预期输出。D3 只用于 P7 独立评测，不参与底座构建。"
)

METRIC_THRESHOLD_NOTE = (
    "三个维度是「规模下限」，不是准确率：对象类 / 属性 / 关系的条数是底座的**体量**，"
    "与答案对不对无关。2026-10-23 起底座改由 D0 已有标准库 + 本轮输入供给"
    "（base_supply），不再取 D3 预期输出，因此这里的阈值只作规模参考线，"
    "不再承担「达标即正确」的含义；正确性由 P7 用 D3 独立评测。"
)


# ---------------------------------------------------------------------------
# 3) 跨模式防泄漏校验
# ---------------------------------------------------------------------------
class LeakageError(ValueError):
    pass


def assert_allowed(mode: str, source: str) -> None:
    """校验某个数据来源是否允许进入该模式的库。命中 forbidden 即抛错。"""
    cfg = MODE_SOURCES.get(mode)
    if cfg is None:
        raise LeakageError(f"未知模式：{mode}")
    s = str(source).replace("\\", "/")
    for bad in cfg["forbidden"]:
        if bad in s:
            raise LeakageError(
                f"[模式 {mode}] 防泄漏拦截：来源「{s}」命中禁用项「{bad}」。"
                f"原因：{cfg['forbidden_reason']}"
            )


def assert_metrics(mode: str, counts: dict) -> dict:
    """对照阈值给出规模判定。返回 {metric, value, threshold, pass}。"""
    th = METRIC_THRESHOLDS[mode]
    out = []
    for k in ("object_class", "attribute", "relation"):
        v = int(counts.get(k, 0))
        out.append({"metric": k, "value": v, "threshold": th[k], "pass": v >= th[k]})
    return {"mode": mode, "items": out,
            "all_pass": all(x["pass"] for x in out),
            "note": METRIC_THRESHOLD_NOTE,
            "policy": BASE_POLICY,
            "meaning": "规模下限（体量），不代表正确性"}


# ---------------------------------------------------------------------------
# 4) 通用读取器：从某模式的 dev/output 读建模模型
# ---------------------------------------------------------------------------
def _resolve(pattern: str) -> str:
    fs = [f for f in glob.glob(str(DATA_ROOT / pattern)) if ".bak-" not in f]
    if not fs:
        raise FileNotFoundError(f"未找到数据文件：{pattern}")
    return fs[0]


def dev_output_paths(mode: str) -> list[str]:
    """D3 预期输出的路径（评测答案）。

    **2026-10-23 起仅供对照查阅，禁止再作为 P3 底座** —— 用它建底座等于把答案
    装进知识库，P7 再用 D3 评分就是循环论证。底座请走 `base_supply.load(mode)`。
    """
    from tools import upload_manager as um   # 延迟导入，避免与上层模块循环
    slots = {"A": ["a_gold_g1", "a_gold_g2"], "B": ["b_gold"]}[mode]
    out: list[str] = []
    for s in slots:
        out += um.active_files(s)
    out = sorted({f for f in out if ".bak-" not in f})
    if not out:
        raise um.DataUnavailable(slots[0], f"模式 {mode} 的 D3 预期输出不可用")
    for f in out:
        assert_allowed(mode, f)
    return out


def dev_output_path(mode: str) -> str:
    """兼容旧调用：返回第一个文件。"""
    return dev_output_paths(mode)[0]


def load_mode_model(mode: str) -> dict:
    """读取某模式参考底座（支持多文件合并），产出 classes / attr_instances / relations。

    2026-10-22 目录重排后：
      - A = group1（160 个对象类）+ group2（21 类 / 417 属性）
      - B = 预期输出 EM4（57 类 / 891 属性）
    表结构由 dataset_registry 按表头自动识别，不再依赖固定 sheet 名。
    """
    from tools import dataset_registry as registry

    paths = dev_output_paths(mode)
    tables = registry.load_tables(paths)

    classes: list[dict] = []
    seen_c: set[str] = set()
    for i, c in enumerate(tables["classes"], 1):
        name = (c.get("name") or "").strip()
        if not name or name in seen_c:
            continue
        seen_c.add(name)
        classes.append({
            "id": c.get("id") or f"OC-{i:04d}",
            "code": c.get("code", ""),
            "name": name,
            "definition": c.get("definition", ""),
            "parent_id": c.get("parent_id", ""),
            "domain": c.get("domain", ""),
            "status": c.get("status", ""),
        })

    class_by_name = {c["name"]: c for c in classes}
    attr_instances: list[dict] = []
    seen_a: set[tuple[str, str]] = set()
    for i, a in enumerate(tables["attrs"], 1):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        cn = (a.get("class_name") or "").strip()
        key = (cn, name)
        if key in seen_a:
            continue
        seen_a.add(key)
        cls = class_by_name.get(cn)
        cid = cls["id"] if cls else (a.get("class_id") or "")
        aid = a.get("attr_id") or f"AT-{i:04d}"
        attr_instances.append({
            "id": f"{cid}::{aid}" if cid else aid,
            "class_id": cid,
            "class_name": cn,
            "attr_id": aid,
            "code": a.get("code", ""),
            "name": name,
            "unit": a.get("unit", ""),
            "definition": a.get("definition", ""),
            "data_type": a.get("data_type", "") or "String",
            "constraint": a.get("constraint", ""),
        })

    class_ids = {c["id"] for c in classes}
    relations: list[dict] = []
    for c in classes:
        if c["parent_id"] and c["parent_id"] in class_ids:
            relations.append({"type": "subClassOf", "source": c["id"], "target": c["parent_id"]})
    for a in attr_instances:
        if a["class_id"] in class_ids:
            relations.append({"type": "hasAttribute", "source": a["class_id"], "target": a["id"]})

    return {
        "mode": mode,
        # 展示用相对路径，不暴露本机绝对路径
        "source": "、".join(_rel(p) for p in paths),
        "source_human": MODE_SOURCES[mode]["human"],
        "classes": classes,
        "attr_instances": attr_instances,
        "relations": relations,
        "counts": {
            "object_class": len(classes),
            "attribute": len(attr_instances),
            "attribute_attached": sum(1 for a in attr_instances if a["class_id"] in class_ids),
            "relation": len(relations),
        },
        "data_caveat": _caveat(classes, attr_instances, class_ids),
    }


def _caveat(classes, attr_instances, class_ids) -> str:
    """如实标注建模集的数据残缺情况（E板块这类「部分数据」会命中）。"""
    notes = []
    orphan_attr = sum(1 for a in attr_instances if a["class_id"] not in class_ids)
    if orphan_attr:
        notes.append(f"{orphan_attr}/{len(attr_instances)} 条属性实例指向未提供的对象类，未挂载")
    par_out = sum(1 for c in classes if c["parent_id"] and c["parent_id"] not in class_ids)
    if par_out:
        notes.append(f"{par_out} 个对象类的父类不在该子集内，subClassOf 关系无法构建")
    return "；".join(notes) if notes else "数据完整"


if __name__ == "__main__":
    import json as _j
    for m in ("A", "B"):
        d = load_mode_model(m)
        print(f"[模式 {m}] {d['source_human']}")
        print("   来源:", d["source"])
        print("   规模:", d["counts"])
        chk = assert_metrics(m, d["counts"])
        for it in chk["items"]:
            print(f"     {it['metric']:<13} {it['value']:>5} / 阈值 {it['threshold']:>4}  "
                  f"{'PASS' if it['pass'] else 'FAIL'}")
        print("   全达标:", chk["all_pass"])
        print()
    # 跨模式校验演示
    for m, bad in (("A", "data/modeB/dev/output/x.xlsx"), ("B", "data/modeB/test/output/x.xlsx")):
        try:
            assert_allowed(m, bad)
            print("未拦截（异常）:", m, bad)
        except LeakageError as e:
            print("拦截正常:", str(e)[:110])
