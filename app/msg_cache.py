# -*- coding: utf-8 -*-
"""消息 JSON 精简缓存。

**目录约定（用户确定）**：完整版 + 精简版**两个文件并存**
  · `<锚定名>-message.json`      —— 完整消息（导出产物，**始终保留不动**）
  · `<锚定名>-message-slim.json` —— 精简版（重复消息合并后的结果）

**为什么**：重复消息的「保留首末」合并（尤其近似聚类）需要遍历全部文字，
大文件每次打开都重算不划算。首次打开时把结果落盘，之后直接读缓存。

**重新生成即替换**：缓存里内嵌 `_slim` 元信息（源文件名 / 大小 / mtime_ns）。
源文件任一变化（重新导出、与既有文件合并后替换、被编辑）→ 校验不通过 →
**重新精简并覆盖旧精简文件**（不会新增第三份、也不会读到过期数据）。
源文件残缺（截断）时校验直接不通过，不会生成缓存。

**只在真的有精简收益（removed > 0）时才写盘**；源文件更新后已无重复时会
把过期的精简文件清掉（避免目录里留一份对不上的旧数据）。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from . import paths
from .msg_dedup import dedup_keep_ends

SLIM_SUFFIX = "-slim.json"


class SlimError(Exception):
    """源 json 不可用（缺失 / 截断 / 结构不对），消息为给用户看的中文原因。"""


def slim_path(src: str | Path) -> Path:
    """精简缓存文件路径：`<stem>-slim.json`（与原文件同目录）。"""
    p = Path(src)
    return p.with_name(f"{p.stem}{SLIM_SUFFIX}")


def _keep_fields(m: dict) -> dict:
    """写盘时保留的字段。

    `_dup_total`（该文字原本有多少条）**故意保留** —— 卡片靠它显示
    「重复 ×N」，让用户知道这条是被合并后的首 / 末条；
    tdl 读 json 时只取 id/file，多这个字段无影响。
    """
    return dict(m)


def _match(meta: dict, src: Path, st) -> bool:
    """缓存的 `_slim` 元信息是否匹配当前源文件（大小 + mtime 纳秒）。"""
    return (meta.get("src") == src.name
            and int(meta.get("size", -1)) == st.st_size
            and int(meta.get("mtime_ns", -1)) == st.st_mtime_ns)


def load_messages(path: str | Path) -> tuple[list[dict], str, dict]:
    """读消息 json（优先用精简缓存）。

    返回 `(messages, chat_id, info)`，其中
    `info = {"from_cache": bool, "removed": int, "slim": Path | None}`。

    源文件损坏时抛 `SlimError`（带用户可读原因）。
    命中缓存时**不读原文件**（只 stat 做失效判断），这是提速的关键。
    """
    src = Path(path)
    try:
        st = src.stat()
    except OSError as e:
        raise SlimError(f"读不到文件（{e}）") from e

    # ---- ① 命中缓存？（源文件 size + mtime_ns 都没变才算命中）----
    slim = slim_path(src)
    if slim.exists():
        try:
            d2 = json.loads(slim.read_text(encoding="utf-8"))
            meta = d2.get("_slim") or {}
            ms2 = d2.get("messages")
            if isinstance(ms2, list) and _match(meta, src, st):
                return ([m for m in ms2 if isinstance(m, dict)],
                        str(d2.get("id", "")),
                        {"from_cache": True, "removed": int(meta.get("removed", 0)),
                         "slim": slim})
        except (OSError, ValueError, UnicodeDecodeError):
            pass                                     # 缓存坏了 → 走重建

    # ---- ② 首次（或源文件被替换/重新导出）：重新精简 ----
    try:
        raw = src.read_bytes()
    except OSError as e:
        raise SlimError(f"读不到文件（{e}）") from e
    good, why = paths.inspect_export_raw(raw)
    if not good:
        raise SlimError(why)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SlimError(f"解析失败（{type(e).__name__}）") from e
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        raise SlimError("文件里没有 messages 数组")
    chat_id = str(data.get("id", ""))

    picked = [m for m in msgs if isinstance(m, dict)]
    kept, removed = dedup_keep_ends(picked)
    clean = [_keep_fields(m) for m in kept]

    written: Path | None = None
    if removed > 0:                                  # 有收益才落盘
        payload = {
            "_slim": {
                "src": src.name,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "removed": removed,
                "kept": len(clean),
                "built": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "id": data.get("id"),
            "messages": clean,
        }
        try:
            slim.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            written = slim
        except OSError:
            written = None                           # 写不了就只影响速度，不影响使用
    else:
        # 本次精简没有收益（源文件更新后已无重复）→ 旧的精简文件已过期，清掉
        drop_cache(src)

    return clean, chat_id, {"from_cache": False, "removed": removed, "slim": written}


def drop_cache(path: str | Path) -> bool:
    """删除某 json 对应的精简缓存（重新生成时用）。"""
    p = slim_path(path)
    try:
        p.unlink()
        return True
    except OSError:
        return False
