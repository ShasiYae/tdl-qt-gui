# -*- coding: utf-8 -*-
"""控制台回显子进程 —— 由 `app/console_mirror.py` 拉起，**独立窗口**。

它自己不做任何判断，只干一件事：把 stdin 收到的字节**原样**写到自己的控制台。
（GUI 是 pythonw，没有控制台，tdl 那套进度条必须由**拥有控制台窗口的进程**来渲染。）

三个关键点，改之前先看：
  1. **必须写 `CONOUT$`**，不能写 `sys.stdout` —— 父进程给子进程指定了 std 句柄
     （stdout=DEVNULL 之类），`sys.stdout` 可能指向别处；`CONOUT$` 永远是这个进程
     自己的控制台缓冲区，最可靠。
  2. **打开 VT（ANSI）处理**：tdl 的进度条靠 `\\x1b[A\\x1b[K` 重绘，不打开就是个
     满屏乱码。
  3. **控制台代码页设成 UTF-8(65001)**：tdl 输出含中文，默认 GBK 代码页会花屏。
"""
import ctypes
import os
import sys

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
UTF8_CP = 65001
TITLE = "tdl 原始输出（GUI 镜像 · 测试用）"

# 可选：把同样内容再写一份到文件（给自动化测试断言用，也方便事后翻查）
LOG_ENV = "TDL_MIRROR_LOG"


def _kernel32():
    return ctypes.windll.kernel32


def _setup_console(handle: int) -> None:
    """打开 VT、设 UTF-8 代码页、给窗口起个能认出来的标题。"""
    k = _kernel32()
    try:
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            k.SetConsoleMode(ctypes.c_void_p(handle),
                             mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:                                             # noqa: BLE001
        pass
    try:
        k.SetConsoleOutputCP(UTF8_CP)
    except Exception:                                             # noqa: BLE001
        pass
    try:
        k.SetConsoleTitleW(TITLE)
    except Exception:                                             # noqa: BLE001
        pass


def _console_out():
    """拿到控制台输出流（优先 CONOUT$，退回 sys.stdout）。"""
    import msvcrt
    try:
        f = open("CONOUT$", "w", encoding="utf-8", errors="replace", newline="")
    except OSError:
        f = sys.stdout
    handle = None
    try:
        handle = msvcrt.get_osfhandle(f.fileno())
    except Exception:                                             # noqa: BLE001
        pass
    _setup_console(handle if handle is not None else
                   _kernel32().GetStdHandle(STD_OUTPUT_HANDLE))
    return f


def main() -> None:
    out = _console_out()
    log = None
    path = (os.environ.get(LOG_ENV) or "").strip()
    if path:
        try:
            # ⚠️ `newline=""` 不能省：文本模式在 Windows 上会把 `\n` 转成 `\r\n`，
            #    于是「镜像字节 == tdl 输出字节」这条断言永远不成立（v2.12.1 踩过）。
            log = open(path, "w", encoding="utf-8", errors="replace", newline="")
        except OSError:
            log = None
    src = sys.stdin.buffer
    read = getattr(src, "read1", src.read)
    while True:
        chunk = read(65536)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        try:
            out.write(text)
            out.flush()
        except Exception:                                         # noqa: BLE001
            break
        if log is not None:
            try:
                log.write(text)
                log.flush()
            except (OSError, ValueError):
                log = None
    try:
        out.write("\n[镜像窗口] tdl 已结束，这个窗口可以直接关掉。\n")
        out.flush()
    except Exception:                                             # noqa: BLE001
        pass
    if log is not None:
        try:
            log.flush()
            log.close()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
