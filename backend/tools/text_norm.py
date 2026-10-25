"""
text_norm.py
============
名称/术语的**两级归一**，供数据读取、评测、导出共用同一套判定。

背景（2026-10-23）：`consistency_check` 发现 P6 产物与现场重算差 17 行，根因是同一个
对象类在「对象类」表写作 `空心型钢（方、圆、矩形钢）`、在「对象类属性」表写作
`空心型钢(方、圆、矩形钢)`。查重时 `norm_key()` 认作同一个类，但落到行里的
`class_name` 仍是两个字符串 → 导出、评测、对账都按两个类处理。

两级能力，刻意分开：

- `canonical_name()`：只抹平**排版差异**（全角括号→半角、首尾空白、连续空格），
  不动语义，用于落库/落盘的展示名。P0 上传的原始文件不动，只在内存模型里统一。
- `norm_key()`：更激进（抹掉括号里的补充说明、去空白、转小写），**只用于查重/匹配**，
  不得用于展示 —— 否则 `空心型钢（方圆矩形）` 与 `空心型钢` 会被当成一类的名字。
"""

from __future__ import annotations

import re

# 全角括号/方头括号 → 半角；其余全角标点（、。，）保持原样，避免改坏中文语义
_BRACKETS = str.maketrans("（）［］｛｝｟｠", "()[]{}()")

_WS = re.compile(r"\s+")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


def canonical_name(s) -> str:
    """抹平排版差异，得到「同一个名字只剩一种写法」的展示名。"""
    t = str(s or "").strip()
    if not t:
        return ""
    t = t.translate(_BRACKETS).replace("\u3000", " ")
    return _WS.sub(" ", t).strip()


def norm_key(s) -> str:
    """查重/匹配用的键：在 canonical_name 基础上再抹掉括号内容、去空白、转小写。"""
    t = canonical_name(s)
    if not t:
        return ""
    # canonical 之后理论上已无全角括号，这里再兜一次（历史数据可能有漏网）
    t = t.translate(str.maketrans("（）［］｛｝", "()[]{}"))
    t = _PAREN.sub("", t)
    return _WS.sub("", t).lower()
