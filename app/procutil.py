# -*- coding: utf-8 -*-
"""进程工具：枚举 / 结束进程 + 单实例锁（Windows 优先，纯 ctypes）。

为什么不用现成办法：
  · `tasklist` / `wmic` 是外部程序 —— GUI 是 pythonw（无控制台）启动，调用时
    会闪一下黑窗；而且 wmic 在本机被安全策略拦过（Program Blacklist）。
  · psutil 是第三方依赖，打包进 exe 又要多带一个包。
  只用 kernel32 的 Toolhelp 快照 + OpenProcess/TerminateProcess + CreateMutexW，
  Windows 自带、零依赖、无窗口闪烁。

背景（本模块要解决的问题）：
  tdl 的账号数据 `~/.tdl/data/<ns>` 是 bbolt 库，**同一时刻只允许一个进程打开**
  （排他文件锁，超时立即失败）。上次 GUI 异常结束（被任务管理器结束 / 崩溃）会
  留下一个仍在下载的 tdl 子进程占着锁，此时再启动任何命令都会失败并打印一句
  英文 `Current database is used by another process, please terminate it first`。
  所以需要在启动前「体检」这些**不属于本实例**的 tdl 进程。

非 Windows 平台全部安全降级（枚举返回空、加锁恒成功），保证跨平台 import 不报错。
"""
from __future__ import annotations

import sys

# tdl 可执行文件名（小写比较）。留 "tdl" 是为了兼容不带扩展名的情况。
TDL_NAMES = ("tdl.exe", "tdl")

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
ERROR_ALREADY_EXISTS = 183
MAX_PATH = 260

# 单实例锁的句柄必须**一直持有**（CloseHandle 或被 GC 回收就等于释放锁）
_single_lock: list = []


# ============================================================================
# 进程枚举
# ============================================================================
def list_processes(names=None) -> list[tuple[int, int, str]]:
    """枚举进程，返回 [(pid, 父pid, 进程名)]。

    `names` 给定时只返回进程名（小写）命中其中的项，这样能少走一堆无关进程。
    非 Windows 返回空列表。
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32First.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESSENTRY32)]
        k32.Process32Next.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(PROCESSENTRY32)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        want = {n.lower() for n in names} if names else None
        out: list[tuple[int, int, str]] = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not k32.Process32First(snap, ctypes.byref(entry)):
                return []
            while True:
                exe = entry.szExeFile.decode("mbcs", "replace")
                if want is None or exe.lower() in want:
                    out.append((int(entry.th32ProcessID),
                                int(entry.th32ParentProcessID), exe))
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
        return out
    except Exception:                                     # noqa: BLE001
        return []


def stray_tdl_pids(exclude=None, names=TDL_NAMES) -> list[int]:
    """正在运行的 tdl 进程 pid，排除 `exclude`（本实例自己启动的子进程）。

    返回的每一个都可能是「占着账号数据库锁、让新命令起不来」的元凶。
    """
    skip = {int(x) for x in (exclude or ())}
    me = _current_pid()
    return [pid for pid, _ppid, _exe in list_processes(names)
            if pid not in skip and pid != me]


def _current_pid() -> int:
    try:
        import os
        return os.getpid()
    except Exception:                                     # noqa: BLE001
        return -1


def describe_processes(pids) -> list[str]:
    """把 pid 列表整理成 ["PID 20008（tdl.exe，父进程 3952 已退出）"] 供日志展示。"""
    info = {pid: (ppid, exe) for pid, ppid, exe in list_processes(TDL_NAMES)}
    alive = {pid for pid, _p, _e in list_processes()}
    out = []
    for pid in pids:
        ppid, exe = info.get(pid, (0, "tdl.exe"))
        parent = "父进程仍在" if ppid in alive else "父进程已退出（残留进程）"
        out.append(f"PID {pid}（{exe}，{parent}）")
    return out


# ============================================================================
# 结束进程
# ============================================================================
def kill_pids(pids) -> tuple[int, list[int]]:
    """强制结束进程，返回 (成功数, 失败的 pid 列表)。非 Windows 直接失败返回。"""
    pids = list(pids or [])
    if sys.platform != "win32" or not pids:
        return 0, list(pids)
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
    except Exception:                                     # noqa: BLE001
        return 0, list(pids)
    ok, bad = 0, []
    for pid in pids:
        try:
            h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if not h:
                bad.append(int(pid))
                continue
            try:
                if k32.TerminateProcess(h, 1):
                    ok += 1
                else:
                    bad.append(int(pid))
            finally:
                k32.CloseHandle(h)
        except Exception:                                 # noqa: BLE001
            bad.append(int(pid))
    return ok, bad


# ============================================================================
# 内存回收
# ============================================================================
def working_set_mb():
    """本进程当前占用的物理内存（MB）。取不到返回 None（非 Windows 等）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD]
        c = PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(c), c.cb):
            return None
        return c.WorkingSetSize / 1024.0 / 1024.0
    except Exception:                                     # noqa: BLE001
        return None


def trim_memory() -> None:
    """尽力把已经空闲的内存**还给系统**（否则 Python / Qt 会把空闲页留在工作集里）。

    三步，全部尽力而为、任何一步失败都不影响功能：
      ① `gc.collect()` 回收循环引用（Qt 部件销毁后常留下成环的 Python 对象）；
      ② `msvcrt.heapmin()` 收缩 C 运行时堆；
      ③ `SetProcessWorkingSetSize(-1, -1)` 让系统把空闲物理页换出 ——
         这一步才是任务管理器里能看到内存下降的关键。
    """
    try:
        import gc
        gc.collect()
    except Exception:                                     # noqa: BLE001
        pass
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.CDLL("msvcrt").heapmin()
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE,
                                                 ctypes.c_size_t,
                                                 ctypes.c_size_t]
        k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(),
                                     ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:                                     # noqa: BLE001
        pass


# ============================================================================
# Job 对象：关闭即杀（防残留子进程的根本手段）
# ============================================================================
_jobs: list = []          # Job 句柄必须持有：句柄一关，系统立即结束组内进程
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


def assign_kill_on_close_job(pid: int) -> bool:
    """把进程挂进「关闭即杀」Job 对象，成功返回 True。

    效果：本进程（GUI）无论以何种方式消失 —— 正常退出、点关闭、被任务管理器
    强杀、崩溃 —— Windows 都会在句柄释放的瞬间把组内的 tdl 子进程一起结束。
    这就是「残留 tdl 占着数据库锁」这类问题的根本解法：不依赖退出路径的代码
    有没有跑到，交给内核兜底。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation",
                         JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return False
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False,
                            int(pid))
        if not h:
            k32.CloseHandle(job)
            return False
        try:
            ok = bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
        if ok:
            _jobs.append(job)               # 持有句柄 → 进程退出时才释放 → 触发杀子
        else:
            k32.CloseHandle(job)
        return ok
    except Exception:                                     # noqa: BLE001
        return False


# ============================================================================
# 单实例锁
# ============================================================================
def acquire_single_instance(tag: str = "tdl_gui_client_v1") -> bool:
    """尝试取得单实例锁：成功（含非 Windows / 取锁失败）返回 True，已有实例返回 False。

    用命名互斥体（CreateMutexW）实现，进程退出时由系统自动释放 —— 不会像锁文件
    那样在崩溃后留下残骸。名字用 `Local\\` 前缀：只在当前登录会话内生效，多用户
    各自登录时互不影响。
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        name = f"Local\\{tag}"
        handle = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not handle:
            return True                                   # 拿不到锁：不拦用户
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _single_lock.append(handle)                       # 持有到进程结束
        return True
    except Exception:                                     # noqa: BLE001
        return True
