# -*- coding: utf-8 -*-
"""
按日下载统计：resources/stats.json。

结构：
    {
      "2026-09-25": {"bytes": 12345678, "seconds": 123.4, "tasks": 3},
      ...
    }

口径：
    · 仅统计成功（exit_code == 0）的下载任务（dl，不含导出/上传/转发）
    · bytes = 本次命令内所有 tracker 最新字节值之和
    · seconds = 命令墙钟时长
"""
from __future__ import annotations

import datetime
import json

from . import paths

_CACHE: dict | None = None


def _path():
    return paths.res_root() / "stats.json"


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _CACHE = {}
        if not isinstance(_CACHE, dict):
            _CACHE = {}
    return _CACHE


def _save(data: dict) -> None:
    global _CACHE
    _CACHE = data
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except OSError:
        pass


def add_download(total_bytes: int, seconds: float, tasks: int = 1) -> None:
    """记一笔成功下载（今天为 key）。

    tasks = 本次命令已下载的任务（文件）数，按任务卡计数（1 个文件 = 1 个任务），
    而非下载命令次数。
    """
    if total_bytes <= 0:
        return
    day = datetime.date.today().isoformat()
    data = _load()
    d = data.setdefault(day, {"bytes": 0, "seconds": 0.0, "tasks": 0})
    d["bytes"] = int(d.get("bytes", 0)) + int(total_bytes)
    d["seconds"] = float(d.get("seconds", 0.0)) + float(seconds)
    d["tasks"] = int(d.get("tasks", 0)) + max(1, int(tasks))
    _save(data)


def days() -> list[dict]:
    """全部统计（按日期倒序）：date/bytes/seconds/avg/tasks。"""
    data = _load()
    out = []
    for day in sorted(data.keys(), reverse=True):
        d = data[day]
        secs = float(d.get("seconds", 0.0))
        byt = int(d.get("bytes", 0))
        out.append({
            "date": day,
            "bytes": byt,
            "seconds": secs,
            "avg": (byt / secs) if secs > 0 else 0.0,
            "tasks": int(d.get("tasks", 0)),
        })
    return out
