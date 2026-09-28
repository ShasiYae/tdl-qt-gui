# -*- coding: utf-8 -*-
"""
tdl 进程调用层。

职责：
    ① 把表单值拼成 argv（严格按 data.py 的 flag 定义）
    ② 用 QProcess 启动 tdl.exe，实时流式读取 stdout / stderr
    ③ 处理 Windows 下 tdl 进度条用的 \r 回车覆盖与 ANSI 转义
    ④ 支持取消（终止进程树）
    ⑤ 记录退出码与耗时

设计要点：
    · tdl 是**长驻进程**（下载可能跑几小时），必须流式输出，不能 waitForFinished
    · tdl 的进度条用 \r 覆盖同一行，GUI 里要改成「原地刷新最后一行」
    · QProcess 默认把 stdout/stderr 合并，这里分开接，便于区分日志与错误
"""
from __future__ import annotations

import codecs
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from . import procutil


# ============================================================================
# 运行记录
# ============================================================================
@dataclass
class RunRecord:
    """一次命令执行的完整记录。"""

    argv: list[str]
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    exit_code: int | None = None
    exit_status: str = ""
    output: list[str] = field(default_factory=list)   # 已归并的最终输出行
    error_text: str = ""

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def cmd_text(self) -> str:
        return " ".join(_quote(a) for a in self.argv)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> str:
        if self.exit_code is None:
            return "运行中…"
        mark = "成功" if self.ok else f"失败（退出码 {self.exit_code}）"
        return f"{mark} · 耗时 {self.duration:.1f}s"


def _quote(s: str) -> str:
    """命令行展示用引号（只处理含空格的）。"""
    if not s:
        return '""'
    if " " in s or "\t" in s:
        return f'"{s}"'
    return s


# ============================================================================
# 输出行缓冲器：处理 \r 覆盖与 ANSI 转义
# ============================================================================
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")


def strip_ansi(s: str) -> str:
    """去掉 ANSI 转义序列（颜色、光标控制等）。"""
    return _ANSI_RE.sub("", s)


class LineBuffer:
    """
    把字节流切成「显示行」。

    难点：tdl 的进度条用 `\r`（回车不换行）在同一行反复刷新，
    直接按 `\n` 切会产出成千上万行垃圾。这里把 `\r` 当作
    「覆盖当前行」信号，只保留最后一次的内容。
    """

    def __init__(self):
        self._buf = ""          # 尚未遇到换行的文本（可能被 \r 覆盖）
        self._lock_current = False

    def feed(self, text: str) -> list[str]:
        """
        喂入一段文本，返回**已完成**的行列表。
        未完成的最后一行留在内部缓冲，等下次 feed 或 flush 时产出。
        """
        out: list[str] = []
        # 统一换行
        text = text.replace("\r\n", "\n")
        self._buf += text

        while True:
            # 找最近的 \n 或 \r
            i_n = self._buf.find("\n")
            i_r = self._buf.find("\r")

            if i_n < 0 and i_r < 0:
                break

            # 谁在前面用谁
            if i_r >= 0 and (i_n < 0 or i_r < i_n):
                # \r：覆盖当前行 —— 丢弃已积累的当前行内容
                self._buf = self._buf[i_r + 1:]
                # 若紧接着是 \n（即 \r\n），已经在上面的 replace 处理过，不会再出现
            else:
                # \n：当前行完成
                line = self._buf[:i_n]
                self._buf = self._buf[i_n + 1:]
                out.append(strip_ansi(line).rstrip())

        return out

    def flush(self) -> list[str]:
        """把缓冲里残留的最后一行吐出来（进程结束时调用）。"""
        if self._buf:
            line = strip_ansi(self._buf).rstrip()
            self._buf = ""
            return [line] if line else []
        return []


# ============================================================================
# 运行器
# ============================================================================
class TdlRunner(QObject):
    """
    tdl 进程调用器（subprocess + 读线程版）。

    为什么不用 QProcess：本环境实测 QProcess 两个致命问题——
      ① waitForFinished 假超时（0.00s 直接返回 False）；
      ② FailedToStart 时不发 finished 信号 → 按钮永远「执行中…」且终止无效。
    subprocess.Popen 路径已被验收测试证明可靠（百条消息下载全通过），
    故改用 Popen + 后台读线程：read 循环在守护线程里跑，
    信号从线程 emit（Qt 自动 queued 切回主线程），GUI 不卡、退出必达。
    """

    line_out = Signal(str)
    line_err = Signal(str)
    started_ = Signal(list)
    finished_ = Signal(object)

    def __init__(self, tdl_path: str, parent=None):
        super().__init__(parent)
        self.tdl_path = tdl_path
        self._proc: subprocess.Popen | None = None
        self._record: RunRecord | None = None
        self._out_buf = LineBuffer()
        self._err_buf = LineBuffer()
        self._pending: list[list[str]] = []   # 命令队列（备用）
        self._queue_total = 1

    # ---------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def record(self) -> RunRecord | None:
        return self._record

    @property
    def child_pids(self) -> set:
        """本实例正在运行的 tdl 子进程 pid（残留检测时要排除自己人）。"""
        if self._proc is not None and self._proc.poll() is None:
            return {self._proc.pid}
        return set()

    def available(self) -> tuple[bool, str]:
        """检查 tdl.exe 是否可用（存在且大小 > 100KB）。"""
        p = self.tdl_path
        if not os.path.isfile(p):
            return False, f"未找到 tdl：{p}"
        try:
            if os.path.getsize(p) < 100 * 1024:
                return False, f"tdl 文件异常（过小）：{p}"
        except OSError as exc:
            return False, f"tdl 无法访问：{p}（{exc}）"
        return True, p

    def probe_version(self, timeout_ms: int = 5000) -> tuple[bool, str]:
        """同步探测 tdl 版本（启动自检用）。subprocess 阻塞语义，可靠。"""
        ok, msg = self.available()
        if not ok:
            return False, msg
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        try:
            cp = subprocess.run(
                [self.tdl_path, "version"],
                capture_output=True,
                timeout=max(1.0, timeout_ms / 1000.0),
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return False, "探测超时"
        except OSError as exc:
            return False, f"无法启动：{exc}"
        raw = (cp.stdout or cp.stderr or b"").decode("utf-8", "replace")
        text = strip_ansi(raw).strip()
        return cp.returncode == 0, text

    # ---------------------------------------------------------------- 启动
    def start(self, argv: list[str], cwd: str = "", env_extra: dict | None = None) -> bool:
        """启动 tdl 进程。argv 不含可执行文件本身。流式读输出，不阻塞。"""
        if self.running:
            return False

        ok, msg = self.available()
        if not ok:
            self.line_err.emit(f"[错误] {msg}")
            return False

        self._record = RunRecord(argv=list(argv))
        self._out_buf = LineBuffer()
        self._err_buf = LineBuffer()

        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        if env_extra:
            env.update(env_extra)

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [self.tdl_path] + list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=cwd or None,
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            self.line_err.emit(f"[错误] 进程启动失败：{exc}")
            self.line_err.emit(f"[诊断] tdl={self.tdl_path} "
                               f"存在={os.path.isfile(self.tdl_path)} "
                               f"cwd={os.getcwd()}")
            self._proc = None
            return False

        # 立刻把子进程挂进「关闭即杀」Job 对象：本 GUI 就算被任务管理器强杀 /
        # 崩溃，Windows 也会在进程句柄释放时连带结束 tdl —— 这样就不会再留下
        # 「占着账号数据库锁的残留 tdl」。失败不影响正常使用（退出路径还有兜底）。
        procutil.assign_kill_on_close_job(self._proc.pid)

        # 两个读线程 + 一个看护线程（守护线程，GUI 退出即消亡）
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        self.started_.emit(list(argv))
        return True

    # ---------------------------------------------------------------- 读线程
    def _pump_out(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        # 增量解码：tdl 输出含中文，read(4096) 会在任意字节处切断多字节字符，
        # 逐块 decode 会把半个汉字变成 "�"（任务名因此不稳定 → 卡片重复）
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                if not chunk:
                    break
                text = dec.decode(chunk)
                for ln in self._out_buf.feed(text):
                    if self._record:
                        self._record.output.append(ln)
                    self.line_out.emit(ln)
            tail = dec.decode(b"", True)
            if tail:
                for ln in self._out_buf.feed(tail):
                    self.line_out.emit(ln)
        except (OSError, ValueError):
            pass

    def _pump_err(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                if not chunk:
                    break
                text = dec.decode(chunk)
                for ln in self._err_buf.feed(text):
                    if self._record:
                        self._record.output.append(ln)
                    self.line_err.emit(ln)
        except (OSError, ValueError):
            pass

    def _watch(self) -> None:
        """看护线程：等进程退出 → flush 缓冲 → 发 finished（跨线程 queued）。"""
        proc = self._proc
        if proc is None:
            return
        try:
            code = proc.wait()
        except OSError:
            code = -1
        # 缓冲里可能还有最后半行
        for ln in self._out_buf.flush():
            if self._record:
                self._record.output.append(ln)
            self.line_out.emit(ln)
        for ln in self._err_buf.flush():
            if self._record:
                self._record.output.append(ln)
            self.line_err.emit(ln)

        if self._record:
            self._record.finished_at = time.time()
            self._record.exit_code = code
            self._record.exit_status = "正常退出" if code == 0 else "异常退出"
        rec = self._record
        self._proc = None
        self.finished_.emit(rec)

        # 队列接续（备用）：前一条成功才跑下一条
        if self._pending and rec is not None and rec.exit_code == 0:
            nxt = self._pending.pop(0)
            done = self._queue_total - len(self._pending)
            self.line_err.emit(f"[队列] {done}/{self._queue_total} 完成，继续下一条…")
            self.start(nxt)
        else:
            self._pending = []

    # ---------------------------------------------------------------- 终止
    @staticmethod
    def _taskkill_tree(pid: int) -> None:
        """兜底：taskkill /T /F 强制结束进程树（失败静默，调用方负责校验）。"""
        try:
            subprocess.Popen(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def cancel(self) -> None:
        """终止进程（非阻塞，界面「停止」用）。

        ⚠️ 不能拿外部 `taskkill` 当主路径：它是**异步**的（发完就返回），既不等
        结果也不校验，被安全策略拦掉时会静默失败 → tdl 变成残留进程、一直占着
        账号数据库锁。所以先 `proc.kill()`（Windows 下是 TerminateProcess，同步
        可靠），确认没死再用 taskkill 收进程树兜底。
        """
        self._pending = []            # 终止 = 放弃整个队列/流程
        proc = self._proc
        if proc is None or proc.poll() is not None:
            # 进程已死但 UI 还在 running 态（竞态兜底）——手动复位
            rec = self._record
            if rec is not None and rec.exit_code is None:
                rec.finished_at = time.time()
                rec.exit_code = -1
                rec.exit_status = "已终止"
                self._proc = None
                self.line_err.emit("[已取消] 进程未在运行，已复位。")
                self.finished_.emit(rec)
            return
        self.line_err.emit("[已取消] 正在终止进程…")
        pid = proc.pid
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:                          # noqa: BLE001 - 超时才兜底
            self._taskkill_tree(pid)
        # _watch 线程会等到退出并自然发 finished，无需额外处理

    def shutdown(self, timeout: float = 5.0) -> tuple[bool, list]:
        """**退出程序**专用：结束 tdl 子进程并确认它真的死了。

        返回 `(是否干净, 仍未结束的 pid 列表)`。与 `cancel()` 的区别是「同步 +
        校验」——退出后不该再有任何本实例启动的 tdl 活着，否则它会一直占着
        `~/.tdl/data/<ns>` 的排他锁，下次启动命令只会看到一句英文报错
        `Current database is used by another process`（用户实测踩过）。

        只结束**本实例自己启动的**那个子进程，不碰用户可能在终端里另起的 tdl
        （不同 --ns 用的是不同库文件，本可以并行）。
        """
        self._pending = []
        proc, self._proc = self._proc, None
        if proc is None:
            return True, []
        pid = proc.pid
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except Exception:                      # noqa: BLE001
                pass
        deadline = time.time() + timeout
        while proc.poll() is None and time.time() < deadline:
            self._taskkill_tree(pid)               # 兜底：连进程树一起收
            time.sleep(0.25)
        left = [] if proc.poll() is not None else [pid]
        return (not left), left

    # ---------------------------------------------------------------- 队列
    def start_queue(self, argv_list: list[list[str]]) -> bool:
        """按顺序执行多条命令（前一条成功才跑下一条；任一失败即终止队列）。"""
        if not argv_list:
            return False
        if self.running:
            return False
        self._pending = [list(a) for a in argv_list[1:]]
        self._queue_total = len(argv_list)
        return self.start(argv_list[0])

    def line_sys(self, text: str) -> None:
        """系统提示行（走 err 通道简化处理）。"""
        self.line_err.emit(text)


def split_flag(flag: str) -> str:
    """
    从 "-d, --dir" 这样的定义里取**规范化开关名**。

    tdl 用 cobra，长选项（--dir）总是可用，短选项只在定义过时是别名。
    这里优先取长选项，避免短选项歧义（如 -d 在 download 与 login 里含义不同）。
    """
    if not flag:
        return ""
    parts = [p.strip() for p in flag.split(",")]
    for p in parts:
        if p.startswith("--"):
            return p
    # 只有短选项时（本例中不存在，保底）
    return parts[0] if parts else ""


def is_multi_flag(field: dict) -> bool:
    """该字段是否产生多个值（cobra 的 strings / stringArray 类型）。"""
    return field.get("type") in ("multi", "list") and field.get("flag")


def build_argv(cmd: dict, values: dict, global_values: dict,
               sub_action: str = "", export_all: bool = False) -> list[str]:
    """
    把表单值拼成 argv。

    cmd           命令定义（data.COMMANDS 的条目）
    values        {key: value} —— 该命令自己的字段值
    global_values {key: value} —— 全局参数字段值
    sub_action    某些命令需要的位置参数（如 extension 的 list/install）
    export_all    dl 导出模式的第二条命令（--all 完整消息记录）标志

    规则：
      · flag 为空 → 不生成参数（纯 UI 字段）
      · switch 为 False → 跳过；True → 只输出开关本身
      · 空字符串 / 空列表 → 跳过（不输出空参数）
    特例：
      · dl 页 srcMode=export（导出 JSON）→ 实际执行 tdl chat export：
        第一条 export_all=False  → -o <dir>/<群号>.json（下载 json，无 --all）
        第二条 export_all=True   → -o <dir>/<群号>-all.json + --all（消息 json）
    """
    argv: list[str] = []
    # srcMode 的值兼容两种形态：纯字符串 "export" / select dict {'v':..,'l':..}
    mode = values.get("srcMode", "url")
    if isinstance(mode, dict):
        mode = mode.get("v", "")
    is_export_mode = (cmd.get("id") == "dl" and str(mode) == "export")

    # ---- 1. 子命令路径 ----
    # cmd['cmd'] 形如 "tdl download" / "tdl chat ls" / "tdl extension"
    cmd_str = "tdl chat export" if is_export_mode else cmd.get("cmd", "")
    if cmd_str.startswith("tdl "):
        cmd_str = cmd_str[4:]
    argv.extend(cmd_str.split())

    # ---- 2. 位置参数（extension 的 action / name）----
    if cmd.get("id") == "extension":
        act = values.get("action", "list")
        argv.append(act)
        nm = (values.get("name") or "").strip()
        if act in ("install", "remove", "upgrade") and nm:
            argv.append(nm)
        # --force 交给下面通用逻辑处理
    elif cmd.get("id") == "version":
        pass   # tdl version 无参数

    # ---- 3. 本命令字段 ----
    # 导出模式：-o 由主窗口按目录结构约定追加（resources/json 与 resources/dl/<群组>/）
    if is_export_mode and export_all:
        argv.append("--all")            # 包含非媒体消息（纯文本）
        argv.append("--with-content")   # 填充 date（时间戳）与 text（正文）供消息浏览展示
    for g in cmd.get("groups", []):
        for f in g.get("fields", []):
            if is_export_mode:
                if f.get("key") == "ex_all":
                    continue   # --all 已在上面处理
                fl = f.get("flag", "")
                if not fl.startswith(("-c,", "-T,", "-i,")):
                    continue   # 跳过下载字段与 note（srcMode/ex_note 无 flag，自然跳过）
            elif f.get("key", "").startswith("ex_") or f.get("key") == "srcMode":
                continue       # 下载模式不输出导出字段
            # 互斥兜底：一对互斥开关（字段用 excl 互指）同时为真时只保留 restart。
            # tdl 对 --continue / --restart 做了 MarkFlagsMutuallyExclusive，
            # 同传会直接报错。界面已做联动，这里防旧配置里残留的脏值。
            if f.get("excl") and values.get(f.get("key")) and values.get(f.get("excl")):
                if f.get("key") != "restart":
                    continue
            _emit_field(argv, f, values)

    # ---- 4. 全局字段（含下移到下载页的那些，值都在 global_values 里）----
    # 导出模式（chat export）不带下载专用参数（线程/并发/池/间隔/NTP/重连/进度刷新），
    # 保持导出命令干净、不依赖 tdl 对多余 flag 的容忍度
    from .data import GLOBAL_FIELDS
    for f in GLOBAL_FIELDS:
        if is_export_mode and (f.get("move_to") == "dl" or f.get("scope") == "dl"):
            continue
        _emit_field(argv, f, global_values)

    return argv


def _emit_field(argv: list[str], f: dict, values: dict) -> None:
    """把单个字段转成 argv 片段。"""
    key = f.get("key", "")
    flag = f.get("flag", "")

    # extension 的 action / name / force 已单独处理顺序，这里跳过重复
    if key in ("action", "name") and flag == "":
        return

    if not flag:
        return

    sw = split_flag(flag)
    if not sw:
        return

    ftype = f.get("type")
    val = values.get(key, f.get("default"))

    if ftype == "switch":
        if val:
            argv.append(sw)
        return

    # 多值：multi（每行一个）或 list（逗号分隔）
    if ftype in ("multi", "list"):
        seq = val if isinstance(val, (list, tuple)) else (
            [val] if val else []
        )
        if ftype == "list":
            # list 可能是 "a,b" 或 ["a","b"]，统一展开成多个值
            flat: list[str] = []
            for item in seq:
                for piece in str(item).split(","):
                    piece = piece.strip()
                    if piece:
                        flat.append(piece)
            seq = flat
        else:
            seq = [str(x).strip() for x in seq if str(x).strip()]
        for v in seq:
            argv.append(sw)
            argv.append(v)
        return

    # 单值
    if val is None:
        return
    # select 字段的值可能是 {'v': 值, 'l': 显示名} —— 只取 v
    if isinstance(val, dict):
        val = val.get("v", "")
    # path 类型在表单里存为列表（PathEdit 返回 ["D:/xxx"]），取第一个非空项
    if ftype == "path":
        seq = val if isinstance(val, (list, tuple)) else [val]
        first = next((str(x).strip() for x in seq if str(x).strip()), "")
        if first == "":
            return
        argv.append(sw)
        argv.append(first)
        return
    s = str(val).strip()
    if s == "":
        return
    # 数字 0 视作"未设置"（除 0 有意义的字段外，这里按 tdl 惯例：
    # --topic 0 / --reply 0 都表示不指定）
    if ftype == "number" and s in ("0", "0.0"):
        return

    argv.append(sw)
    argv.append(s)
