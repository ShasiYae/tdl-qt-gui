# -*- coding: utf-8 -*-
"""崩溃 / 异常日志 —— 把 pythonw 下的「无声闪退」变成有据可查。

## 为什么要有它

`启动.bat` 用 `pythonw.exe` 跑，**没有控制台**：Python 未捕获异常、Qt 自己的致命
错误、C 层硬崩（访问违规 / 栈溢出）全都不会显示给用户，表现就是「用着用着窗口
不见了」。没有日志就只能猜。

## 记五类信息

1. **Python 未捕获异常** —— 主线程（`sys.excepthook`）+ 子线程（`threading.excepthook`）
2. **Qt 自己的消息** —— `qInstallMessageHandler`（warning / critical / fatal）
3. **C 层硬崩** —— `faulthandler`：崩的瞬间把调用栈 dump 进同一个文件
   （这是唯一能查出「没抛异常就没了」那类闪退的手段）
4. **内存水位** —— 每 `MEM_INTERVAL` 秒一条：当前工作集 MB + 调用方上报的上下文
   （加载了哪个 json、多少条消息…）。「加载 json 后长时间运行才崩」这种场景，
   第一件要看的就是内存是不是一路涨上去。
5. **生命周期** —— 启动写 `START`、正常退出写 `EXIT`
   ⭐ 日志里**有 START 没有 EXIT** 就是闪退的铁证 —— 哪怕一条异常都没有。

## 日志文件

`resources/logs/crash.log`（UTF-8，追加）。超过 `MAX_BYTES` 轮转成 `crash.log.1`
（只留一代）。崩了之后把 `crash.log` 整个发出来即可。

## 设计约束

- **绝不能让日志本身把程序搞崩**：所有公开函数内部全部 try/except 兜住，
  拿不到目录、写不进去都只是「没有日志」，不影响主功能。
- **用二进制无缓冲写**：`faulthandler` 要往同一个 fd 写原始字节，
  文本层的缓冲会和它交错；`buffering=0` 同时也保证「崩的那一瞬间已经落盘」。
- `install()` 幂等，重复调用无副作用。
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import traceback
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024        # 2 MB 轮转
MEM_INTERVAL = 60.0                # 内存水位采样间隔（秒）
CTX_MAX = 8                        # 上下文条目上限（内存行里带上）

_lock = threading.RLock()
_fh = None                         # 日志文件对象（二进制无缓冲；faulthandler 也用它的 fd）
_path: Path | None = None
_installed = False
_mem_thread: threading.Thread | None = None
_qt_handler = None                 # 持有引用防 GC（Qt 只存函数指针）
_qt_last = {"key": None, "n": 0}   # Qt 消息去重（QSS 警告会刷屏）
_ctx: dict[str, str] = {}          # 调用方上报的上下文
_start_ts = 0.0
_default_hook = sys.excepthook


# ------------------------------------------------------------------ 路径 / 打开
def log_dir() -> Path:
    """日志目录（resources/logs）。取不到则回落到包目录旁的 resources/logs。"""
    try:
        from .paths import res_root
        return res_root() / "logs"
    except Exception:                                     # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "resources" / "logs"


def log_file() -> Path | None:
    """当前日志文件路径（未安装时也返回「将会写到哪」）。给 UI / 用户看的。"""
    if _path is not None:
        return _path
    try:
        return log_dir() / "crash.log"
    except Exception:                                     # noqa: BLE001
        return None


def _reopen() -> None:
    """（重新）打开日志文件，并把 faulthandler 指过去。"""
    global _fh, _path
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    _path = d / "crash.log"
    _fh = open(_path, "ab", buffering=0)
    try:
        import faulthandler
        faulthandler.enable(file=_fh, all_threads=True)
    except Exception:                                     # noqa: BLE001
        pass


def _rotate_if_needed() -> None:
    """超过 MAX_BYTES 就轮转成 crash.log.1（只留一代）。"""
    global _fh
    try:
        if _path is None or _fh is None or not _path.exists():
            return
        if _path.stat().st_size < MAX_BYTES:
            return
        try:
            _fh.close()
        except Exception:                                 # noqa: BLE001
            pass
        old = _path.with_name("crash.log.1")
        try:
            if old.exists():
                old.unlink()
            os.replace(_path, old)
        except OSError:
            pass                                          # 换不动就继续往原文件写
        _reopen()
    except Exception:                                     # noqa: BLE001
        pass


# ------------------------------------------------------------------ 写
def _write(tag: str, text: str = "") -> None:
    """写一条日志。**任何失败都吞掉** —— 日志不该成为崩溃源。"""
    if _fh is None:
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = (text or "").rstrip("\n").splitlines() or [""]
        # 空行写成真正的空行（不带 "[时间] TAG " 前缀）—— 否则多行块里的空行会变成
        # 一行没内容的 "[时间] START "，既难看又像是在报错
        buf = "".join(f"[{ts}] {tag} {ln}\n" if ln.strip() else "\n" for ln in lines)
        with _lock:
            _rotate_if_needed()
            if _fh is not None:
                _fh.write(buf.encode("utf-8", "replace"))
    except Exception:                                     # noqa: BLE001
        pass


def note(tag: str, text: str = "") -> None:
    """公开的写日志入口（其他模块想记点什么时用）。"""
    _write(tag, text)


def set_context(key: str, value) -> None:
    """上报一条上下文，会跟在内存水位行后面。

    比如消息浏览页在加载完 json 后调
    `set_context("msg_json", 文件名)`、`set_context("msgs", 12345)`，
    崩的时候就能一眼看出「崩在哪份数据上、当时有多少条」。
    """
    try:
        with _lock:
            if len(_ctx) >= CTX_MAX and str(key) not in _ctx:
                return
            _ctx[str(key)] = str(value)
    except Exception:                                     # noqa: BLE001
        pass


def clear_context() -> None:
    try:
        with _lock:
            _ctx.clear()
    except Exception:                                     # noqa: BLE001
        pass


# ------------------------------------------------------------------ 异常钩子
def _safe_default_hook(exc_type, exc, tb) -> None:
    """交回解释器默认处理（源码模式下仍能在控制台看到）。"""
    try:
        _default_hook(exc_type, exc, tb)
    except Exception:                                     # noqa: BLE001
        pass


def _hook_main(exc_type, exc, tb) -> None:
    # Ctrl+C 不算崩溃，不写日志（否则日志被无意义的 KeyboardInterrupt 塞满）
    if not issubclass(exc_type, KeyboardInterrupt):
        # ⚠️ 这句原来写的是「程序即将退出」—— **不准确**（v2.10.11 改准）：下面只是把异常
        #    交回默认 excepthook（pythonw 下等于打印到一个不存在的 stderr），**进程不会退出**。
        #    尤其是 Qt 槽里的异常：表现出来是「点了没反应 / 任务进度、网速静默停更」，
        #    而程序还在正常跑 —— 别被旧文案骗成"程序死了"。
        _write("EXC", "主线程未捕获异常（进程不会退出；若是 Qt 槽内异常，界面会静默失效）：")
        _write("EXC", "".join(traceback.format_exception(exc_type, exc, tb)))
    _safe_default_hook(exc_type, exc, tb)


def _hook_thread(args) -> None:
    """threading.excepthook —— 子线程里的异常默认**只打印不退出**，很容易被忽略。"""
    try:
        name = getattr(getattr(args, "thread", None), "name", "?")
        _write("EXC-T", f"子线程 [{name}] 未捕获异常：")
        _write("EXC-T", "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))
    except Exception:                                     # noqa: BLE001
        pass


# ------------------------------------------------------------------ Qt 消息
def install_qt() -> bool:
    """接管 Qt 的 qWarning / qCritical / qFatal。可单独调用（幂等）。"""
    global _qt_handler
    with _lock:
        if _qt_handler is not None:
            return True
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:                                     # noqa: BLE001
        return False

    keep = {"QtWarningMsg", "QtCriticalMsg", "QtFatalMsg"}

    def handler(mode, ctx, msg) -> None:
        try:
            name = getattr(mode, "name", None) or str(mode)
            if name not in keep:                          # Debug/Info 对排查崩溃没用，不记
                return
            key = (name, msg)
            with _lock:
                if key == _qt_last["key"]:
                    _qt_last["n"] += 1
                    return
                dup = _qt_last["n"]
                _qt_last["key"], _qt_last["n"] = key, 0
            if dup:
                _write("QT", f"（上一条重复 {dup} 次）")
            where = ""
            try:
                if ctx is not None and getattr(ctx, "file", None):
                    where = f"{ctx.file}:{getattr(ctx, 'line', '?')} "
            except Exception:                             # noqa: BLE001
                where = ""
            _write(f"QT-{name}", f"{where}{msg}".strip())
        except Exception:                                 # noqa: BLE001
            pass

    try:
        qInstallMessageHandler(handler)
        _qt_handler = handler                              # 攥住，否则会被 GC 掉
        return True
    except Exception:                                     # noqa: BLE001
        return False


# ------------------------------------------------------------------ 内存水位
def _fmt_uptime(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _mem_line() -> str:
    mb = None
    try:
        from .procutil import working_set_mb
        mb = working_set_mb()
    except Exception:                                     # noqa: BLE001
        mb = None
    parts = [f"工作集 {mb:.1f} MB" if isinstance(mb, (int, float)) else "工作集 n/a",
             f"已运行 {_fmt_uptime(time.time() - _start_ts)}"]
    # ⚠️ 补记资源计数（v2.10.9）：四次「创建 Qt 对象时 access violation」崩溃，
    #    内存都不高（400~650 MB），所以真正要看的是**句柄 / 线程有没有在累积**。
    try:
        from .procutil import res_counts
        _g, _u, _h = res_counts()
        parts.append(f"句柄 {_h}")
        parts.append(f"GDI {_g}")
        parts.append(f"USER {_u}")
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import threading as _th
        parts.append(f"线程 {_th.active_count()}")
    except Exception:                                     # noqa: BLE001
        pass
    try:
        with _lock:
            parts += [f"{k}={v}" for k, v in list(_ctx.items())[:CTX_MAX]]
    except Exception:                                     # noqa: BLE001
        pass
    return " · ".join(parts)


def _start_mem_thread() -> None:
    global _mem_thread
    if _mem_thread is not None:
        return

    def loop() -> None:
        while True:
            time.sleep(MEM_INTERVAL)
            try:
                _write("MEM", _mem_line())
            except Exception:                             # noqa: BLE001
                pass

    t = threading.Thread(target=loop, name="crashlog-mem", daemon=True)
    _mem_thread = t
    t.start()


# ------------------------------------------------------------------ 安装 / 退出
def _write_start(app_name: str) -> None:
    lines = ["", "=" * 64, f"{app_name} 启动"]
    try:
        from .version import __version__
        lines.append(f"版本   : v{__version__}")
    except Exception:                                     # noqa: BLE001
        pass
    lines.append(f"时间   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Python : {sys.version.split()[0]}  ({sys.executable})")
    try:
        import platform
        lines.append(f"系统   : {platform.platform()}")
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from PySide6.QtCore import qVersion
        lines.append(f"Qt     : {qVersion()}")
    except Exception:                                     # noqa: BLE001
        pass
    lines.append(f"进程   : pid={os.getpid()}  参数={sys.argv[1:] or '(无)'}")
    lines.append("=" * 64)
    _write("START", "\n".join(lines))


def _on_exit() -> None:
    try:
        _write("EXIT", f"正常退出（本次运行 {_fmt_uptime(time.time() - _start_ts)}）")
    except Exception:                                     # noqa: BLE001
        pass


def install(app_name: str = "tdl GUI") -> bool:
    """装好全部钩子。幂等。**越早调用越好**（此后的任何一步崩了都能留下记录）。"""
    global _installed, _start_ts
    with _lock:
        if _installed:
            return True
        try:
            _reopen()
        except Exception:                                 # noqa: BLE001
            return False                                  # 没日志也不能拦住程序启动
        _installed = True
        _start_ts = time.time()

    sys.excepthook = _hook_main
    try:
        threading.excepthook = _hook_thread
    except Exception:                                     # noqa: BLE001
        pass

    install_qt()
    try:
        atexit.register(_on_exit)
    except Exception:                                     # noqa: BLE001
        pass
    _start_mem_thread()
    _write_start(app_name)
    return True
