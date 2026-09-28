# -*- coding: utf-8 -*-
"""外部媒体工具探测与元数据读取（ffmpeg / ffprobe）。

被 `msg_view`（视频首帧缩略图）与 `media_viewer`（播放器帧率 / 时长 / 跳帧）
共用。单独成模块是为了避免这两个模块互相 import 造成循环依赖。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

_FFMPEG_CANDIDATES = [
    r"D:\Program Files (x86)\Duplicate Cleaner 5\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
]
_FFPROBE_CANDIDATES = [
    r"D:\Program Files (x86)\Duplicate Cleaner 5\ffprobe.exe",
    r"C:\ffmpeg\bin\ffprobe.exe",
    r"D:\ffmpeg\bin\ffprobe.exe",
]
_FFMPEG: str | None | bool = False
_FFPROBE: str | None | bool = False
_PROBE_CACHE: dict[str, dict] = {}          # 路径 → 元数据（进程内缓存）


def _probe(binary: str, candidates: list[str]) -> str | None:
    p = shutil.which(binary)
    if not p:
        for c in candidates:
            if Path(c).exists():
                p = c
                break
    return p or None


def ffmpeg_path() -> str | None:
    """ffmpeg 可执行文件路径（找不到返回 None，调用方需容错）。"""
    global _FFMPEG
    if _FFMPEG is False:
        _FFMPEG = _probe("ffmpeg", _FFMPEG_CANDIDATES) or ""
    return _FFMPEG or None


def ffprobe_path() -> str | None:
    """ffprobe 可执行文件路径（优先与 ffmpeg 同目录）。"""
    global _FFPROBE
    if _FFPROBE is False:
        cands = []
        exe = ffmpeg_path()
        if exe:
            cands.append(str(Path(exe).with_name("ffprobe.exe")))
        cands += _FFPROBE_CANDIDATES
        _FFPROBE = _probe("ffprobe", cands) or ""
    return _FFPROBE or None


def probe_stream(path: str | Path) -> dict:
    """读视频元数据：{ok, fps, duration, width, height, codec}。

    fps 用于「跳帧」（步长 = 1/fps）；duration 用于进度条兜底。
    优先用 ffprobe；**本机 ffmpeg 目录常只有 ffmpeg.exe 没有 ffprobe.exe**，
    因此退化用 `ffmpeg -i` 解析 stderr（Duration / fps / 分辨率 / 编码）。
    任何失败都返回 {'ok': False}，调用方按缺省值降级。
    """
    key = str(path)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    info: dict = {"ok": False, "fps": 0.0, "duration": 0.0,
                  "width": 0, "height": 0, "codec": ""}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    exe = ffprobe_path()
    if exe:
        args = [exe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate,width,height,codec_name",
                "-show_entries", "format=duration", "-of", "json", key]
        try:
            r = subprocess.run(args, capture_output=True, timeout=20,
                               creationflags=flags)
            if r.returncode == 0:
                d = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
                st = (d.get("streams") or [{}])[0]
                rate = str(st.get("r_frame_rate") or "0/1")
                try:
                    num, _, den = rate.partition("/")
                    fps = float(num) / float(den or 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    fps = 0.0
                info.update(
                    ok=True,
                    fps=round(fps, 4) if 0 < fps < 1000 else 0.0,
                    duration=float((d.get("format") or {}).get("duration") or 0.0),
                    width=int(st.get("width") or 0),
                    height=int(st.get("height") or 0),
                    codec=str(st.get("codec_name") or ""),
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    if not info["ok"] or info["fps"] <= 0:
        _probe_with_ffmpeg(key, info, flags)      # 无 ffprobe → 解析 ffmpeg -i
    _PROBE_CACHE[key] = info
    return info


def _parse_ffmpeg_stderr(text: str, info: dict) -> None:
    """从 `ffmpeg -i` 的 stderr 里抽取时长 / 帧率 / 分辨率 / 编码。

    典型行：
      Duration: 00:00:03.20, start: 0.000000, bitrate: 1506 kb/s
      Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 29.97 fps, 29.97 tbr
    """
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        if info.get("duration", 0.0) <= 0:
            info["duration"] = h * 3600 + mi * 60 + s
    # 只取视频流那一行，避免把音频参数当视频
    vline = ""
    for ln in text.splitlines():
        if "Stream #" in ln and "Video:" in ln:
            vline = ln
            break
    if vline:
        if info.get("fps", 0.0) <= 0:
            fm = re.search(r"([\d.]+)\s*fps", vline)
            if fm:
                try:
                    fps = float(fm.group(1))
                    if 0 < fps < 1000:
                        info["fps"] = round(fps, 4)
                except ValueError:
                    pass
        if not info.get("width"):
            rm = re.search(r"(\d{2,5})x(\d{2,5})", vline)
            if rm:
                info["width"], info["height"] = int(rm.group(1)), int(rm.group(2))
        if not info.get("codec"):
            cm = re.search(r"Video:\s*([A-Za-z0-9_]+)", vline)
            if cm:
                info["codec"] = cm.group(1)
    if info.get("duration", 0.0) > 0 or info.get("fps", 0.0) > 0 or info.get("width"):
        info["ok"] = True


def _probe_with_ffmpeg(path: str, info: dict, flags: int) -> None:
    """无 ffprobe 时的兜底：`ffmpeg -i <file>`（不产出文件，靠 stderr 取信息）。"""
    exe = ffmpeg_path()
    if not exe:
        return
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", path],
                           capture_output=True, timeout=25, creationflags=flags)
        text = r.stderr.decode("utf-8", "replace")
        if "Duration" not in text and "Stream" not in text:
            text = r.stderr.decode("gbk", "replace")       # Windows 中文环境兜底
        _parse_ffmpeg_stderr(text, info)
    except (OSError, subprocess.SubprocessError):
        pass


def make_thumb(src: str | Path, out: str | Path, at: float = 0.0,
               width: int = 840) -> bool:
    """用 ffmpeg 在 at 秒处抽一帧存为 jpg（at<=0 取首帧）。"""
    exe = ffmpeg_path()
    if not exe:
        return False
    args = [exe, "-y", "-loglevel", "error"]
    if at > 0:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-1", str(out)]
    try:
        r = subprocess.run(args, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and Path(out).exists()
    except (OSError, subprocess.SubprocessError):
        return False
