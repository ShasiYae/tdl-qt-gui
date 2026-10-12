# -*- coding: utf-8 -*-
"""把 tdl 的原始输出**再打一份**到一个真正的控制台窗口（测试 / 验证用）。

## 为什么需要这么做

GUI 用管道接走了 tdl 的 stdout（并且用 `CREATE_NO_WINDOW` 启动），所以 tdl 自己
那个控制台窗口**根本不存在**。想「同时看到界面 + tdl 原本那套进度条」，就得把 GUI
读到的字节**原样**再送到一个有控制台的进程里去渲染。

为什么不让 GUI 自己开控制台（`AllocConsole`）：那样控制台窗口属于 GUI 进程，
**点它的关闭按钮会连带结束 GUI**（CTRL_CLOSE_EVENT），对正要查崩溃的我们风险太大。
所以这里另起一个**只做回显的独立小进程**——关掉它最多丢一个窗口，伤不到界面。

## 开关

    · 设置项 `debug.tdl_console`（界面「运行输出」标题栏的「tdl 窗口」复选框）
    · 环境变量 `TDL_GUI_MIRROR=1/0` **覆盖**设置项（临时不开窗口很方便）

## 设计红线

`feed()` 里的异常**一律吞掉**：镜像只是"顺便看一眼"的工具，它出任何问题都不允许
影响下载与界面（最差就是窗口不出现）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import paths, prefs

PREF_KEY = "debug.tdl_console"
ENV_KEY = "TDL_GUI_MIRROR"
HELPER = Path(__file__).with_name("_mirror_echo.py")

# 0x00000010 = CREATE_NEW_CONSOLE（子进程拿到一个**属于自己的**新控制台窗口）
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


def enabled() -> bool:
    """是否开启镜像窗口：环境变量 > 设置项 > 默认关。"""
    env = (os.environ.get(ENV_KEY) or "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    if env in ("1", "true", "on", "yes"):
        return True
    return prefs.get_bool(PREF_KEY, False)


def console_python() -> str:
    """找一个**控制台版**的解释器。

    必须是 `python.exe` 而不是 `pythonw.exe` —— 后者没有控制台，也没有 std 句柄。
    打包成 exe（`sys.frozen`）时旁边没有解释器，返回空串 → 本功能自动不可用。
    """
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")
    if cand.is_file():
        return str(cand)
    for name in ("python.exe", "python3.exe"):
        found = shutil.which(name)
        if found:
            return found
    return ""


class Mirror:
    """独立控制台窗口的「回显器」：`feed()` 把一批原始输出原样打进去。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._broken = False          # 试过但起不来（缺解释器 / 启动失败）→ 不再重试
        # 子进程（窗口）被关掉（用户点 X / 被杀）时的回调（由 runner 注入）；
        # 用于把「已开」状态清掉并同步取消界面勾选，避免「关了又自动弹」。
        self.on_closed = None
        self._closed_notified = False

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if self.alive:
            return True
        if self._broken:
            return False
        self._closed_notified = False
        exe = console_python()
        if not exe or not HELPER.is_file():
            self._broken = True
            return False
        try:
            self.proc = subprocess.Popen(
                [exe, "-u", str(HELPER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NEW_CONSOLE,
                cwd=str(paths.app_root()),
            )
        except OSError:
            self._broken = True
            self.proc = None
            return False
        return True

    def feed_bytes(self, data: bytes) -> None:
        """把**原始字节**原样写进镜像窗口（一个字节都不改）。

        ⚠️⚠️ **必须走原始字节，不能走 runner 切好行的文本**（v2.12.1 修）：
        `LineBuffer.feed()` 是为「界面显示」准备的，它会
          ① `strip_ansi()` —— `ESC[A` / `ESC[K` 被全部剥掉，
             **进度条就失去了「原地刷新」的能力**
          ② 丢掉换行符并 `rstrip()`
          ③ 把 `\\r` 当成「覆盖当前行」信号，丢掉已积累的内容
        结果喂进去的是一堆粘在一起、没有任何控制符的文本 → **窗口满屏乱码**
        （用户实测：「tdl 窗口显示太杂乱」）。
        原样透传之后，tdl 自己那套「光标上移 N 行 → 逐行清空 → 重画」就完整生效了：
        **每条任务占一行，进度在原行次上刷新**。

        ⚠️ 子进程（窗口）已经不在了（用户点 X 关闭 / 被杀）：此时写 stdin 会失败，
        这里统一在「proc 已死」时触发 `on_closed`（让上层把它从「已开」状态清掉、
        并同步取消界面勾选）—— 否则关掉的窗口下次命令又会弹回来。
        """
        if not data:
            return
        p = self.proc
        if p is None:
            return
        # 窗口（子进程）已死：通知上层，自己不再尝试写
        if p.poll() is not None:
            self._notify_closed()
            return
        try:
            with self._lock:
                p.stdin.write(data)
                p.stdin.flush()
        except (OSError, ValueError):
            self._notify_closed()
            self.stop()

    def _notify_closed(self) -> None:
        """窗口被关掉（仅触发一次）：调 on_closed 回调通知上层。"""
        if self._closed_notified:
            return
        self._closed_notified = True
        cb = self.on_closed
        if cb is not None:
            try:
                cb()
            except Exception:                                     # noqa: BLE001
                pass

    def feed(self, lines) -> None:
        """把一批行（list[str]）写进镜像窗口 —— 内部转成字节后走同一条路。"""
        self.feed_bytes("".join(lines).encode("utf-8", "replace"))

    def stop(self, wait: float = 1.5) -> None:
        """关掉 stdin → 子进程读到 EOF 自己退出（留一句告别再关）。

        ⚠️ 主动关窗**不**触发 `on_closed`（那是「被用户关闭」专用，避免反复横跳）。
        """
        proc, self.proc = self.proc, None
        self.on_closed = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=wait)
        except Exception:                                         # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass


def start_if_enabled() -> Mirror | None:
    """开了开关就返回一个已启动的 Mirror，否则 None（调用方不用管失败）。"""
    if not enabled():
        return None
    m = Mirror()
    return m if m.start() else None
