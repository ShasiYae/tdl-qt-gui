# -*- coding: utf-8 -*-
"""消息文字去重：相同 / 近似文字只保留「第一条 + 最后一条」。

被两处共用，保证「显示时去重」与「写盘精简」结果一致：
- `msg_view._visible_groups()`：筛选后动态去重（用户看到的）
- `msg_cache`：首次打开时把精简结果落盘，之后直接读缓存

判定「相同」的规则：
1. 折叠连续空白 + 忽略大小写（`casefold`）
2. 近似合并：字符 bigram 倒排筛候选 → `SequenceMatcher.ratio() >= FUZZY_RATIO`
   才 Union-Find 并成一条（同一串刷屏消息的大小写/拼写变体视为同一条）
3. 仅对**纯文字消息**（无 file）生效；带附件消息的 text 是 caption，不参与去重
"""
from __future__ import annotations

import difflib
import functools
import re

FUZZY_RATIO = 0.90                 # 「近似文字」相似度阈值（越大越严格）
FUZZY_MIN_LEN = 3                  # 短于此长度的文本不做近似匹配（防误合并）
_WS = re.compile(r"\s+")


def plain_key(m: dict) -> str | None:
    """纯文字消息的归一化键；带附件的消息返回 None（caption 不参与去重）。"""
    if str(m.get("file", "") or "").strip():
        return None
    txt = str(m.get("text", "") or "").strip()
    return _WS.sub(" ", txt).casefold() if txt else None


def fuzzy_text_map(texts: list[str]) -> dict[str, str]:
    """把「近似」文本聚成一类，返回 {文本: 代表文本}（只含需要改写的）。

    结果按「文本集合」缓存（搜索框每敲一个字都会重建视图，不缓存会反复重算）。
    """
    uniq = tuple(sorted(set(texts)))
    if len(uniq) < 2:
        return {}
    return dict(_fuzzy_cached(uniq))


@functools.lru_cache(maxsize=4)
def _fuzzy_cached(uniq: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """近似聚类的实现（入参必须是可哈希的唯一文本元组，便于 lru_cache）。

    相似判定：字符 bigram 倒排筛出候选 → SequenceMatcher 比率 ≥ FUZZY_RATIO。
    过短（< FUZZY_MIN_LEN）或长度差异大的直接跳过，避免误合并。
    复杂度靠倒排索引压到「只比较可能相似的」，文本种数上千也不卡。
    """
    inv: dict[str, list[int]] = {}
    for i, t in enumerate(uniq):
        if len(t) >= FUZZY_MIN_LEN:
            for g in {t[j:j + 2] for j in range(len(t) - 1)}:
                inv.setdefault(g, []).append(i)
    parent = list(range(len(uniq)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, t in enumerate(uniq):
        if len(t) < FUZZY_MIN_LEN:
            continue
        cand: dict[int, int] = {}
        for g in {t[j:j + 2] for j in range(len(t) - 1)}:
            for j in inv.get(g, ()):
                if j > i:
                    cand[j] = cand.get(j, 0) + 1
        for j, shared in cand.items():
            other = uniq[j]
            if len(other) < FUZZY_MIN_LEN:
                continue
            if abs(len(t) - len(other)) > max(2, int(len(t) * 0.3)):
                continue
            if shared < max(2, int(min(len(t), len(other)) * 0.5)):
                continue                               # 共享 n-gram 太少，粗筛掉
            if difflib.SequenceMatcher(None, t, other).ratio() >= FUZZY_RATIO:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra

    groups: dict[int, list[int]] = {}
    for i in range(len(uniq)):
        groups.setdefault(find(i), []).append(i)
    rep: dict[str, str] = {}
    for members in groups.values():
        name = uniq[min(members)]                       # 组内最先出现者作为代表
        for i in members:
            rep[uniq[i]] = name
    return tuple((k, v) for k, v in rep.items() if k != v)   # 只保留需要改写的


def dedup_keep_ends(msgs: list[dict],
                    rep: dict[str, str] | None = None) -> tuple[list[dict], int]:
    """相同 / 近似文字只保留首条与末条，其余合并掉。

    返回 (结果列表, 合并条数)。保留的首/末条会带上 `_dup_total`（该文字总条数），
    供卡片显示「重复 ×N」。
    """
    if rep is None:
        rep = fuzzy_text_map([k for k in (plain_key(m) for m in msgs) if k is not None])

    def key_of(m: dict) -> str | None:
        k = plain_key(m)
        return rep.get(k, k) if k is not None else None

    count: dict[str, int] = {}
    last_idx: dict[str, int] = {}
    for i, m in enumerate(msgs):
        k = key_of(m)
        if k is not None:
            count[k] = count.get(k, 0) + 1
            last_idx[k] = i

    out: list[dict] = []
    seen: set[str] = set()
    dups = 0
    for i, m in enumerate(msgs):
        k = key_of(m)
        if k is not None and count.get(k, 1) > 1:
            if k not in seen:
                seen.add(k)                            # 第一条：保留
            elif last_idx.get(k) != i:
                dups += 1                              # 中间重复：合并
                continue
            m = {**m, "_dup_total": count[k]}
        out.append(m)
    return out, dups
