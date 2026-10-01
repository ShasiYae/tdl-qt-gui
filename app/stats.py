# -*- coding: utf-8 -*-
"""
按日下载统计：resources/stats.json。

结构：
    {
      "2026-09-25": {"bytes": 12345678, "seconds": 123.4, "tasks": 3},
      ...
    }

口径：
    · 仅统计下载任务（dl，不含导出/上传/转发；命令中途失败但已下到的部分也计入）
    · **按实际发生的时间分账**：下载过程中逐次累计增量，落到增量发生的那一刻所在的
      自然日 —— 一次跨零点的下载会分别记到两天，不会整笔算在某一天
    · bytes = 各文件最新已完成字节之和（本次运行的累计值）
    · seconds = 实际在下载的时长（同样按天分开累计）
"""
from __future__ import annotations

import datetime
import json
import time

from . import paths

_CACHE: dict | None = None
_RUN: dict | None = None            # 本次运行的增量基准（见 begin_run / add_progress）
_LAST_SAVE = 0.0
SAVE_MIN_INTERVAL = 5.0            # 落盘节流：进度行很密，不能每一行都写盘


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


def _save(data: dict, force: bool = False) -> None:
    """写盘（默认**节流**：距上次落盘不足 `SAVE_MIN_INTERVAL` 秒就先只留在内存）。

    内存里的 `_CACHE` 始终是最新的（`days()` 读的就是它），所以统计页看到的数据
    永远准确；`flush()` 用于命令结束/退出时强制落盘。
    """
    global _CACHE, _LAST_SAVE
    _CACHE = data
    now = time.time()
    if not force and now - _LAST_SAVE < SAVE_MIN_INTERVAL:
        return
    _LAST_SAVE = now
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except OSError:
        pass


def begin_run(when: float | None = None) -> None:
    """新一次下载开始：复位「本次运行累计值」的基准。

    `when` 缺省取当前时刻；显式传入只是为了测试能构造跨零点的场景。
    """
    global _RUN
    _RUN = {"bytes": 0, "tasks": 0,
            "when": when if when is not None else time.time()}


def _bump(when: float, db: int, dt: float, dn: int) -> None:
    """把一个增量记到 `when` **所在的那一天** —— 这就是「按实际时间分账」。"""
    day = datetime.date.fromtimestamp(when).isoformat()
    data = _load()
    d = data.setdefault(day, {"bytes": 0, "seconds": 0.0, "tasks": 0})
    d["bytes"] = int(d.get("bytes", 0)) + int(db)
    d["seconds"] = float(d.get("seconds", 0.0)) + float(dt)
    d["tasks"] = int(d.get("tasks", 0)) + int(dn)
    _save(data)


def add_progress(total_bytes: int, task_count: int = 0,
                 when: float | None = None) -> None:
    """实时累计：把「本次运行累计值」的**增量**记入**增量发生的那一刻所在的自然日**。

    ⚠️ 为什么必须这样记（用户实测反馈「今天没下载，但是有数据」）：
       一次 09-28 18:56 开始、跨零点到 09-29 00:06 才结束的下载，无论按「结束日」还是
       「开始日」整笔归档都会出错 —— 前者让 09-29 凭空多出 44 GB，后者又把 09-29 那 6
       分钟的量也算到 09-28。用户要求**按实际时间划分**，所以改成：下载过程中逐次累计，
       每次的增量记到**当下**那一天。跨零点时自然就分到了两天，量还按真实进度成比例。

    `total_bytes` / `task_count` 传的是本次运行的**累计值**（不是增量）：函数与上次调用
    比对求出增量。调用方每次刷新调一次即可（落盘有节流，见 `SAVE_MIN_INTERVAL`）。
    """
    global _RUN
    t = when if when is not None else time.time()
    if _RUN is None:
        _RUN = {"bytes": 0, "tasks": 0, "when": t}
    total_bytes, task_count = max(0, int(total_bytes)), max(0, int(task_count))
    db = max(0, total_bytes - _RUN["bytes"])
    dn = max(0, task_count - _RUN["tasks"])
    dt = max(0.0, t - _RUN["when"])
    _RUN["bytes"] = max(_RUN["bytes"], total_bytes)
    _RUN["tasks"] = max(_RUN["tasks"], task_count)
    _RUN["when"] = t
    if db or dt or dn:
        _bump(t, db, dt, dn)


def flush() -> None:
    """把内存里的统计强制写盘（命令结束 / 退出时调用）。"""
    _save(_load(), force=True)


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
