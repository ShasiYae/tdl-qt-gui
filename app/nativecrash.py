# -*- coding: utf-8 -*-
"""原生（C 层）崩溃定位器 —— 崩的那一刻记下「**崩在哪个 DLL、哪条指令、原生调用栈**」。

# ⛔⛔ 结论：**不要在 Qt 应用里装它**（10-11 实测，血的教训）

装上之后，原来的**硬崩（AppCrash，有转储、有事件日志）变成了死锁（AppHang，什么都没留下）**：
10-11 18:16 那次 `crash.log` 里**一条记录都没有**、Windows 事件日志给的是
`Application Hang 1002 / AppHangB1`（而不是 Application Error）。
原因：VEH 里跑的是 **Python 回调**，进去要抢 GIL；Qt 主线程此时很可能正卡在某个
不释放 GIL 的 C 调用里 → 抢不到 → **整个进程死锁**（用户看到的是"卡死"，比崩还糟）。

⇒ `install()` 现在**默认拒绝安装**，必须显式设环境变量 `TDL_ALLOW_VEH=1` 才启用
（只在离线诊断、且能接受卡死时用）。**普通运行千万别装。**

## 真正好用的"黑匣子"是 Windows 自带的（我们一行都不用写）

10-11 最终是靠这两样定位的，它们**没有死锁风险**：
1. **崩溃转储**：`%LOCALAPPDATA%\\CrashDumps\\pythonw.exe.<pid>.dmp`（WER 自动存，约 70 MB/份）
2. **事件日志**：`事件查看器 → Windows 日志 → 应用程序`，Provider = `Application Error`，
   直接写出 **错误模块名称 + 错误偏移量**：
   ```
   Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'} -MaxEvents 8
   ```
   解析转储用工作目录里的 `_dumpinfo.py`（纯手写 minidump 解析，无需 windbg）。
   ⚠️ minidump 偏移极易写错，实测正确值：异常流的嵌套结构在 **+8**、出错地址 **+0x18**、
   NumberParameters **+0x20**、Information **+0x28**、ThreadContext 描述符 **+0xA0**；
   线程结构 48 字节、栈描述符在 **+24**；模块结构 108 字节、模块名 RVA 在 **+20**。

## 为什么当初要做它

`faulthandler` 只能给 **Python 栈**：它能说"崩的时候 Python 正跑到哪一行"，但访问违规
（access violation）绝大多数发生在 **C/C++ 层**（Qt / QtMultimedia / WMF / D3D / 显卡驱动）。
于是每次崩溃都只能看到「崩在 `QLabel(...)`、`QVideoSink(...)` 这种**完全无害的分配**上」——
10-11 四次崩溃（两次在 `QVideoSink(self)`、两次在建任务卡）就是这个样子：
**崩点在无害处飘来飘去，Python 栈提供不了任何线索。**

本模块装一个 **VEH（向量化异常处理器）**，在异常发生的第一时间记录：

```
NATIVE 硬崩 #1：访问违规（access violation）
NATIVE 异常码 0xC0000005 · 出错线程 tid=12345
NATIVE 出错地址 0x7FFB12345678 → Qt6Multimedia.dll+0x1a2b3    ← 关键：哪个模块
NATIVE → 崩在 **Qt6Multimedia.dll+0x1a2b3**
NATIVE 访问类型 写入 · 目标地址 0x...（不在任何已加载模块内）
NATIVE 原生调用栈（从记录器往外的真实栈，非猜测）：
NATIVE     libffi-8.dll+0x44A0        ← 前几帧是记录器自己
NATIVE     Qt6Multimedia.dll+0x1a2b3
NATIVE     Qt6Gui.dll+0x2f19a
NATIVE     ...
```

## 实测结论（都是量出来的，别再"优化"回去）

- `EXCEPTION_POINTERS.ContextRecord` 里的 CONTEXT **不能按标准结构读寄存器** ——
  实测 `Rip` 位置读到 0、`Rbp` 是垃圾值（网上那些偏移表在这条路径上不成立）。
  ✅ 只有 `ExceptionRecord.ExceptionAddress` 可靠（它就是出错指令地址）。
- 想要原生栈**必须用 `ntdll.RtlCaptureStackBackTrace`**（`kernel32` 里没有这个导出名）；
  在处理器里直接调它，拿到的是**真实栈**，且实测**包含出错模块**。
- 别在 VEH 里调 `GetThreadContext(当前线程)` —— 它给的是**处理器自己**的上下文，没用。

## 约束（都是硬性要求）

- **绝不改变原崩溃行为**：处理完照常 `EXCEPTION_CONTINUE_SEARCH`，进程该崩还是崩。
- **绝不把程序搞崩**：全部包 try/except；拿不到就只是"没有这条记录"。
- **死锁风险**：崩溃时**不走** `crashlog._write`（那个带锁，万一别的线程正持锁就会挂死），
  直接用 `crashlog` 已打开的无缓冲文件句柄写原始字节。
- **平时零开销**：模块枚举 / 栈回溯都在崩溃时才做；DLL 句柄只在 `install()` 时取一次。
- 只记「硬崩」类异常码（访问违规 / 栈溢出 / 非法指令 …），普通 Python 异常、C++ 异常
  （0xE06D7363）都不掺和。
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

# ---------------------------------------------------------------- 常量
EXCEPTION_ACCESS_VIOLATION = 0xC0000005
EXCEPTION_IN_PAGE_ERROR = 0xC0000006
EXCEPTION_ILLEGAL_INSTRUCTION = 0xC000001D
EXCEPTION_STACK_OVERFLOW = 0xC00000FD
EXCEPTION_PRIV_INSTRUCTION = 0xC0000096

_HARD = {
    EXCEPTION_ACCESS_VIOLATION: "访问违规（access violation）",
    EXCEPTION_IN_PAGE_ERROR: "页错误（in-page error）",
    EXCEPTION_ILLEGAL_INSTRUCTION: "非法指令",
    EXCEPTION_STACK_OVERFLOW: "**栈溢出**（递归/深调用把栈用完了）",
    EXCEPTION_PRIV_INSTRUCTION: "特权指令",
}

_MAX_RECORDS = 4              # 一次运行最多记几条（防刷屏）
_BT_FRAMES = 48               # 栈回溯取多少帧
_BT_KEEP = 18                 # 最多列出多少帧

_installed = False
_count = 0
_handler_ref = None           # 必须留住引用，否则回调被 GC 回收 → 崩
_bt = None                    # RtlCaptureStackBackTrace（install 时取好，崩溃现场不加载 DLL）


# ---------------------------------------------------------------- 结构
class EXCEPTION_RECORD(ctypes.Structure):
    pass


EXCEPTION_RECORD._fields_ = [
    ("ExceptionCode", wintypes.DWORD),
    ("ExceptionFlags", wintypes.DWORD),
    ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
    ("ExceptionAddress", ctypes.c_void_p),
    ("NumberParameters", wintypes.DWORD),
    ("ExceptionInformation", ctypes.c_ulonglong * 15),
]


class EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.c_void_p)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260),
    ]


# ---------------------------------------------------------------- 模块表
_mods: list | None = None


def _load_modules() -> list:
    """枚举本进程已加载的模块 [(base, end, 名字)]。崩溃时只做一次。"""
    global _mods
    if _mods is not None:
        return _mods
    out: list = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x00000008, 0x00000010
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
        k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                            k32.GetCurrentProcessId())
        if not snap or snap == ctypes.c_void_p(-1).value:
            return out
        try:
            me = MODULEENTRY32W()
            me.dwSize = ctypes.sizeof(MODULEENTRY32W)
            ok = k32.Module32FirstW(snap, ctypes.byref(me))
            while ok:
                base, size = me.modBaseAddr or 0, me.modBaseSize or 0
                if base and size:
                    out.append((base, base + size, me.szModule))
                ok = k32.Module32NextW(snap, ctypes.byref(me))
        finally:
            try:
                k32.CloseHandle(ctypes.c_void_p(snap))
            except Exception:                                       # noqa: BLE001
                pass
    except Exception:                                               # noqa: BLE001
        pass
    _mods = out
    return out


def module_of(addr: int) -> tuple:
    """地址 → (模块名, 偏移)。找不到返回 ("?", 0)。"""
    try:
        a = int(addr or 0)
        for base, end, name in _load_modules():
            if base <= a < end:
                return name, a - base
    except Exception:                                               # noqa: BLE001
        pass
    return "?", 0


def _fmt_addr(addr: int) -> str:
    name, off = module_of(addr)
    if name == "?":
        return f"0x{int(addr or 0):X}（不在任何已加载模块内）"
    return f"0x{int(addr):X} → {name}+0x{off:X}"


# ---------------------------------------------------------------- 原生栈
def _backtrace() -> list:
    """`RtlCaptureStackBackTrace` 取真实原生栈 → ["模块+0x偏移", …]。

    ⚠️ 别改成读 CONTEXT 的寄存器：实测那条路上的偏移表不成立（Rip 读出来是 0）。
    """
    out: list = []
    if _bt is None:
        return out
    try:
        frames = (ctypes.c_void_p * _BT_FRAMES)()
        n = int(_bt(1, _BT_FRAMES, frames, None))
        for i in range(min(n, _BT_FRAMES)):
            a = int(frames[i] or 0)
            name, off = module_of(a)
            if name == "?":
                continue
            item = f"{name}+0x{off:X}"
            if not out or out[-1] != item:          # 连续重复压掉
                out.append(item)
            if len(out) >= _BT_KEEP:
                break
    except Exception:                                               # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- 写日志（无锁）
def _raw_write(text: str) -> None:
    """崩溃现场专用：**不加锁**、直接写已打开的无缓冲句柄。

    为什么不用 `crashlog._write`：那个要拿 `_lock`；万一崩溃时另一个线程正持锁，
    我们会在崩溃处理器里**挂死**（变成"程序卡住不退出"），比丢一条日志糟得多。
    """
    try:
        from . import crashlog
        fh = crashlog._fh                                     # noqa: SLF001 现场专用
        if fh is None:
            return
        fh.write(text.encode("utf-8", "replace"))
    except Exception:                                               # noqa: BLE001
        pass


# ---------------------------------------------------------------- 处理器
def _on_exception(ep) -> int:
    global _count
    try:
        rec = ep.contents.ExceptionRecord.contents
        code = int(rec.ExceptionCode)
        if code not in _HARD:
            return 0                              # EXCEPTION_CONTINUE_SEARCH
        _count += 1
        if _count > _MAX_RECORDS:
            return 0

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fault = int(rec.ExceptionAddress or 0)
        name, off = module_of(fault)

        lines = [f"硬崩 #{_count}：{_HARD[code]}",
                 f"异常码 0x{code:08X} · 出错线程 tid={k32.GetCurrentThreadId()}",
                 f"出错地址 {_fmt_addr(fault)}"]
        lines.append(f"→ 崩在 **{name}+0x{off:X}**" if name != "?"
                     else "→ 出错地址不在任何已加载模块内")

        if rec.NumberParameters >= 2:
            kind = {0: "读取", 1: "写入", 8: "执行"}.get(
                int(rec.ExceptionInformation[0]), str(rec.ExceptionInformation[0]))
            lines.append(f"访问类型 {kind} · 目标地址 "
                         f"{_fmt_addr(int(rec.ExceptionInformation[1]))}")

        if code != EXCEPTION_STACK_OVERFLOW:
            frames = _backtrace()
            if frames:
                lines.append(f"原生调用栈（真实栈；前几帧是记录器自己的调用链）：")
                lines += [f"    {x}" for x in frames]

        _raw_write("".join(f"[NATIVE] {ln}\n" for ln in lines) + "\n")
    except Exception:                                               # noqa: BLE001
        pass
    return 0                                     # 不改行为：照常崩


# ---------------------------------------------------------------- 安装
def install() -> bool:
    """装 VEH（并预先取好栈回溯函数）。幂等。

    ⚠️⚠️ **默认拒绝安装**（见模块头）：VEH 里跑 Python 回调要抢 GIL，在 Qt 应用里
    会把"硬崩"变成"死锁"（10-11 实测 AppHang，日志一无所获）。
    要启用必须显式设环境变量 **`TDL_ALLOW_VEH=1`** —— 只在离线诊断、能接受卡死时用。
    """
    global _installed, _handler_ref, _bt
    if _installed:
        return True
    if os.environ.get("TDL_ALLOW_VEH") != "1":
        return False                    # 默认不装：装了会把崩变成卡死
    if not sys.platform.startswith("win"):
        return False
    try:
        # ntdll 才导出 RtlCaptureStackBackTrace（kernel32 的 CaptureStackBackTrace 常取不到）
        ntdll = ctypes.WinDLL("ntdll")
        fn = getattr(ntdll, "RtlCaptureStackBackTrace", None) or \
            getattr(ntdll, "CaptureStackBackTrace", None)
        if fn is not None:
            fn.argtypes = [wintypes.ULONG, wintypes.ULONG,
                           ctypes.POINTER(ctypes.c_void_p),
                           ctypes.POINTER(wintypes.ULONG)]
            fn.restype = wintypes.USHORT
            _bt = fn

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.POINTER(EXCEPTION_POINTERS))
        _handler_ref = proto(_on_exception)      # 必须留引用
        k32.AddVectoredExceptionHandler.argtypes = [wintypes.ULONG, proto]
        k32.AddVectoredExceptionHandler.restype = ctypes.c_void_p
        h = k32.AddVectoredExceptionHandler(1, _handler_ref)      # 1 = 最先被调用
        if not h:
            return False
        _installed = True
        return True
    except Exception:                                               # noqa: BLE001
        return False


def backtrace_available() -> bool:
    """原生栈回溯是否可用（测试/诊断用）。"""
    return _bt is not None
