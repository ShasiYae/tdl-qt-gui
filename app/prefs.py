# -*- coding: utf-8 -*-
"""轻量偏好存储 —— 记住窗口大小 / 音量这类界面状态。

落盘位置：`resources/prefs.json`，结构就是扁平的 `key -> value`：

    {
      "viewer.win":  [1080, 720],
      "viewer.max":  false,
      "viewer.vol":  80
    }

设计要点：
- **读一次缓存**：首次访问读盘，之后 `get()` 只查内存（界面构造里可以放心用）
- **`set()` 只改内存，`save()` 才落盘**：拖动音量滑块不会每帧写文件
- **原子写**：先写 `.tmp` 再替换，中途崩溃不会把配置写坏
- **永不抛异常**：偏好丢了不算错，绝不能因此崩界面（读/写失败都静默降级）
- **可换文件**：`set_file()` 供测试指向临时文件，避免污染真实配置
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

NAME = "prefs.json"

_override: Path | None = None      # 测试用：临时文件覆盖
_cache: dict | None = None         # 内存缓存（None = 尚未读盘）


def set_file(path) -> None:
    """覆盖配置文件路径（传 None 恢复默认）。主要给测试用，会清空缓存。"""
    global _override, _cache
    _override = Path(path) if path else None
    _cache = None


def file_path() -> Path:
    """配置文件路径：覆盖值 > 环境变量 TDL_PREFS > resources/prefs.json。"""
    if _override is not None:
        return _override
    env = os.environ.get("TDL_PREFS")
    if env:
        return Path(env)
    return paths.res_root() / NAME


def _data() -> dict:
    """读取（并缓存）全部偏好。文件不存在 / 损坏都当空字典。"""
    global _cache
    if _cache is None:
        try:
            obj = json.loads(file_path().read_text(encoding="utf-8"))
            _cache = obj if isinstance(obj, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            _cache = {}
    return _cache


# ---------------------------------------------------------------- 读
def get(key: str, default=None):
    return _data().get(key, default)


def get_int(key: str, default: int = 0, lo: int | None = None,
            hi: int | None = None) -> int:
    """取整数并夹到 [lo, hi]；值非法时返回 default（同样夹范围）。"""
    try:
        v = int(_data().get(key, default))
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def get_bool(key: str, default: bool = False) -> bool:
    v = _data().get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_pair(key: str) -> tuple[int, int] | None:
    """取 `[w, h]` 形式的尺寸；不合法返回 None。"""
    v = _data().get(key)
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return None
    try:
        return int(v[0]), int(v[1])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 写
def set(key: str, value) -> None:                       # noqa: A001 - 语义即 set
    """只改内存（不落盘）；需要持久化时再调 `save()`。"""
    _data()[key] = value


def update(values: dict) -> None:
    """批量改内存。"""
    _data().update(values)


def save() -> bool:
    """把内存写回磁盘（原子替换）。成功返回 True，失败静默返回 False。"""
    target = file_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(_data(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, target)                          # 原子（同盘 rename）
        return True
    except (OSError, ValueError, TypeError):
        return False


def set_and_save(key: str, value) -> bool:
    set(key, value)
    return save()


def reset() -> None:
    """清空内存缓存（不删磁盘文件）；下次访问重新读盘。"""
    global _cache
    _cache = None
