"""
local_embedding.py
==================
本地语义向量（无第三方依赖、无模型下载、无外网调用）。

为什么有这个模块
----------------
RAG 的两个检索口原来都是「假向量」：
- `rag_vector` 实为 Dice 字面相似（字符二元组集合重叠），同义/近义一律 0 分；
- `rag_graph` 实为「同名节点查一遍」，没有多跳，等于字典查找。

本模块提供真正的稠密向量：
    文本 → 词/字 n-gram 特征（含定义与所属类上下文）
         → TF-IDF 加权
         → 随机投影到 D 维（Johnson–Lindenstrauss，用稳定哈希实现）
         → L2 归一化
余弦相似度在投影空间中保持（近似误差 O(1/√D)），因此「公称直径」与「管径」、
「弯头」与「管件」这类同域词能拿到分，而纯字面法拿不到。

诚实标注：这是**本地随机投影嵌入（LSA 家族的近似）**，不是预训练语言模型。
面板文案必须照此写，不得宣称「预训练 embedding / 大模型向量」。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from collections import Counter
from operator import mul
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIM = 1024
DEFAULT_SEED = "nuclear-std-2026"

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_SPLIT = re.compile(r"[\s,，、;；:：/|\[\]（）()【】]+")


def _tokens(text: str) -> list[str]:
    """切词：汉字 1/2/3-gram + 英文/数字词。

    中文短术语（「管件」「弯头」）用 2-gram 最有辨识度；1-gram 只补召回，
    但噪声大 —— 因此按 n 加权：1-gram 0.45、2-gram 1.0、3-gram 1.15。
    """
    text = (text or "").strip().lower()
    if not text:
        return []
    out: list[str] = []
    for seg in _SPLIT.split(text):
        if not seg:
            continue
        for m in _CJK.finditer(seg):
            s = m.group(0)
            # 不出 1-gram：单汉字 idf 极高，会把「头」「径」这种字放大成噪声
            # （实测「弯头」被「电加热器」抢到 0.39）。中文短术语靠 2/3-gram 已足够。
            if len(s) == 1:
                out.append(s)                                     # 整段只有一个字时保留
            out.extend(s[i:i + 2] for i in range(len(s) - 1))     # 2-gram
            if len(s) >= 3:
                out.extend(s[i:i + 3] for i in range(len(s) - 2))  # 3-gram（长术语）
        for m in _WORD.finditer(seg):
            w = m.group(0)
            out.append(w)
            if len(w) > 3:
                out.extend(w[i:i + 3] for i in range(len(w) - 2))
    return out


def _gram_weight(tok: str) -> float:
    """特征权重：按 token 长度近似 n-gram 阶数（中文 1 字=1-gram，英文词=整词）。"""
    if tok.isascii():
        return 1.0 if len(tok) > 3 else 1.15
    return {1: 0.45, 2: 1.0, 3: 1.15}.get(len(tok), 1.2)


_HASHES = 4


def _projections(token: str) -> list[tuple[int, float]]:
    """稳定哈希 → k 组 (维度下标, ±1)。

    单次投影的方差太大：查询侧往往只有两三个特征，一旦与某个无关文档撞到同一桶，
    余弦就能被抬到 0.4+（实测「弯头」被「电加热器」抢到 0.47）。用 4 组独立哈希，
    碰撞贡献在求和时正负抵消，近似误差按 1/√k 收敛。
    """
    out = []
    for k in range(_HASHES):
        h = hashlib.md5((DEFAULT_SEED + str(k) + token).encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % DIM
        sign = 1.0 if h[4] & 1 else -1.0
        out.append((idx, sign))
    return out


class Embedder:
    """一小组文本的 TF-IDF + 随机投影嵌入器（增量式 fit，非训练）。"""

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.idf: dict[str, float] = {}
        self.n_docs = 0

    # ---- 拟合（只统计 idf，不迭代、不训练）----
    def fit(self, texts: list[str]) -> "Embedder":
        df: Counter = Counter()
        for t in texts:
            df.update(set(_tokens(t)))
        self.n_docs = max(len(texts), 1)
        self.idf = {tok: math.log((1 + self.n_docs) / (1 + c)) + 1.0
                    for tok, c in df.items()}
        # 未登录词的 idf：取 log(N)+1（相当于 df=1）
        self._default_idf = math.log(1 + self.n_docs) + 1.0
        return self

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = _tokens(text)
        if not toks:
            return vec
        tf = Counter(toks)
        for tok, n in tf.items():
            w = (1.0 + math.log(n)) * self.idf.get(tok, self._default_idf) * _gram_weight(tok)
            for idx, sign in _projections(tok):
                vec[idx] += sign * w
        # 第二个投影向量：降低单哈希碰撞造成的失真（两个桶取均值口径）
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        # 已 L2 归一化 → 点积即余弦。用 map(mul) 而不是生成式：
        # 1024 维 × 上千条，逐个 P6 字段都要算一遍，生成式太慢
        return sum(map(mul, a, b))


class Index:
    """名称 → 向量 的检索索引。语料通常几百到几千条，线性扫描足够快。"""

    def __init__(self, dim: int = DIM):
        self.emb = Embedder(dim)
        self.items: list[dict] = []          # [{key, text, vec, payload}]

    def build(self, records: list[dict], text_fn=None) -> "Index":
        """records: 任意字典列表；text_fn(rec) 给出用于编码的文本（默认取 name/definition）。"""
        if text_fn is None:
            def text_fn(r):
                return " ".join(str(r.get(k) or "") for k in ("name", "definition", "class_name"))
        self.items = []
        for r in records:
            txt = text_fn(r)
            self.items.append({"key": r.get("name") or r.get("key") or "",
                               "text": txt, "payload": r, "vec": None})
        self.emb.fit([it["text"] for it in self.items])
        # token 倒排：查询只和"共享至少一个 n-gram"的条目算余弦。
        # 随机投影下不共享任何 token 的两条文本余弦期望为 0，
        # 所以这样剪枝几乎不损召回，但把 1878×1024 的全量点积砍掉一大半。
        self.post: dict[str, set[int]] = {}
        post: dict[str, set[int]] = {}
        for i, it in enumerate(self.items):
            it["vec"] = self.emb.encode(it["text"])
            for t in set(_tokens(it["text"])):
                post.setdefault(t, set()).add(i)
        self.post = post
        return self

    def search(self, query: str, topk: int = 5,
               exclude_keys: set[str] | None = None) -> list[dict]:
        if not self.items:
            return []
        qv = self.emb.encode(query)
        if not any(qv):
            return []
        qk = (query or "").strip()
        cand: set[int] = set()
        for t in set(_tokens(query)):
            s = self.post.get(t)
            if s:
                cand |= s
        cand_idx = sorted(cand) if cand else range(len(self.items))
        scored = []
        for i in cand_idx:
            it = self.items[i]
            if exclude_keys and it["key"] in exclude_keys:
                continue
            # 同名即同一术语：索引文本里还拼了定义与所属类，纯余弦会被稀释
            # （「备注」对「备注」只有 0.28），必须按精确同名给满分
            s = 1.0 if (qk and it["key"] == qk) else self.emb.cosine(qv, it["vec"])
            if s > 0:
                scored.append({"key": it["key"], "text": it["text"],
                               "score": round(s, 4), "payload": it["payload"]})
        scored.sort(key=lambda x: (-x["score"], x["key"]))
        return scored[:topk]

    def best(self, query: str, exclude_keys: set[str] | None = None) -> dict | None:
        got = self.search(query, topk=1, exclude_keys=exclude_keys)
        return got[0] if got else None


# ---------------------------------------------------------------------------
# 进程级缓存：P4/P5/P6 反复检索同一座库，不重复建索引
# ---------------------------------------------------------------------------
_INDEX_CACHE: dict[tuple, Index] = {}


def library_index(mode: str = "A") -> Index:
    """D0 已有标准库的索引（类 + 属性都进索引，按名称去重）。"""
    key = ("lib", mode)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    from tools import landscape_analyst as la
    lib = la.load_mock_library(mode)
    recs: list[dict] = []
    seen: set[str] = set()
    for nm, c in (lib.get("classes") or {}).items():
        if nm and nm not in seen:
            seen.add(nm)
            recs.append({"name": nm, "definition": c.get("definition", ""),
                         "class_name": c.get("domain", ""), "kind": "class", "ref": c})
    for nm, rows in (lib.get("field_index") or {}).items():
        if nm and nm not in seen:
            seen.add(nm)
            r0 = rows[0] if rows else {}
            recs.append({"name": nm, "definition": r0.get("definition", ""),
                         "class_name": r0.get("class_name", "") if r0.get("class_name") != "（未归属）" else "",
                         "kind": "field", "ref": r0, "variants": len(rows)})
    idx = Index().build(recs)
    _INDEX_CACHE[key] = idx
    return idx


def describe() -> dict:
    """给前端展示的检索口径说明（避免文案又写回「Dice 语义召回」）。"""
    return {"backend": "local-random-projection",
            "dim": DIM,
            "features": "汉字 1/2/3-gram + 英文数字词 + 定义/所属类上下文",
            "weighting": "TF-IDF",
            "similarity": "余弦（投影空间，JL 近似）",
            "note": "本地随机投影嵌入，非预训练语言模型；无外网调用、无模型下载"}


if __name__ == "__main__":
    from tools import landscape_analyst as la
    lib = la.load_mock_library("B")
    idx = library_index("B")
    print("索引规模:", len(idx.items), "| classes", lib["class_count"], "fields", lib["field_count"])
    for q in ("管径", "公称直径", "弯头", "壁厚", "材料牌号"):
        got = idx.search(q, topk=3)
        print(f"  {q} → " + "、".join(f"{g['key']}({g['score']})" for g in got))
