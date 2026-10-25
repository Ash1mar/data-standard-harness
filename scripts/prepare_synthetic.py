"""Create a fresh disposable application copy with exclusively synthetic data."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent


def prepare() -> Path:
    target = ROOT / ".runtime" / "validation" / uuid.uuid4().hex / "app"
    target.mkdir(parents=True, exist_ok=False)
    for name in ("backend", "frontend", ".dsh/roles"):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    data = target / "data"
    for path in ("modeB/输入", "modeB/预期输出", "modeA/输入 group1", "templates", "uploaded"):
        (data / path).mkdir(parents=True, exist_ok=True)

    def workbook(path: Path, *, gold=False):
        wb = Workbook()
        ws = wb.active; ws.title = "对象类"
        ws.append(["对象类ID", "对象类编码", "中文名称", "中文定义", "父类ID"])
        ws.append(["DM000001", "DM_DEMO", "演示设备", "仅用于集成测试的合成对象类", ""])
        ws = wb.create_sheet("对象类属性")
        ws.append(["关联ID", "对象类ID", "对象类编码", "对象类中文名称", "属性ID", "属性编码",
                   "属性中文名称", "属性单位", "数据类型", "数据格式/约束", "属性定义"])
        for i, name in enumerate(["演示编号", "演示说明"], 1):
            ws.append([f"DM000001Attr{i:06d}", "DM000001", "DM_DEMO", "演示设备", f"Attr{i:06d}",
                       f"DEMO_{i}", name, "", "String" if gold else "VARCHAR(64)", "VARCHAR(64)",
                       ("合成设备的唯一编号" if i == 1 else "")])
        wb.save(path)

    manifest = {"slots": {}, "decisions": {"req_template": "off", "a_gold_g1": "off", "a_gold_g2": "off"}}
    for slot, name, gold in [("b_in", "synthetic-requirements.xlsx", False), ("b_gold", "synthetic-gold.xlsx", True)]:
        folder = data / "uploaded" / slot; folder.mkdir()
        workbook(folder / name, gold=gold)
        manifest["slots"][slot] = {"files": [name], "uploaded_at": "synthetic-fixture"}
    folder = data / "uploaded/model_spec"; folder.mkdir()
    rules = {"fields": [{"name": n, "data_type": "String", "constraint": "VARCHAR(64)"}
                        for n in ["演示编号", "演示说明"]],
             "description": "Synthetic integration fixture, contains no business data"}
    (folder / "synthetic-rules.json").write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    manifest["slots"]["model_spec"] = {"files": ["synthetic-rules.json"], "uploaded_at": "synthetic-fixture"}
    (data / "uploaded/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # Kept as separate validation context; never copies any original data or model keys.
    result = {"project": str(target), "synthetic_only": True, "objects": 1, "attributes": 2}
    (ROOT / ".runtime/latest-validation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return target


if __name__ == "__main__":
    prepare()
