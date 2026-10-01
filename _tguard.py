# -*- coding: utf-8 -*-
"""测试/调试脚本的统一「超时护栏」—— 所有任务都要规定时间，超时就毙掉。

## 为什么有这个东西

用户明确要求：「所有的任务都规定时间。超时毙掉任务。」
这里说的**不是软件里的下载任务**，而是**开发/测试这一侧执行的任务** ——
测试脚本、探针、打包构建、我（AI）跑的任何命令。

吃过亏的场景：
  · 走镜像 pip 装大包，**卡死 8 分钟零输出**，只能人工干等；
  · 某条命令输出去重后不再刷新，界面「任务卡着不动」，不知道是死了还是在跑；
  · 一个测试脚本没有自爆定时器 → 整轮回归被它一个人吊死（run_tests 只能等超时）。

## 用法（import 即自动生效）

    import _tguard                 # 自动 arm(180)：180 秒内没跑完就自爆退出
    import _tguard as G

    G.arm(600)                     # 需要更久时改长（打包 / PyInstaller 类用），会重新计时
    G.disarm()                     # 已确定跑完、只是想慢慢收尾时撤掉
    G.remaining()                  # 还剩多少秒（用于给子进程挑一个更短的上限）

    out = G.run(["tdl", "--version"], timeout=10)   # 带超时的 subprocess.run 替代
    p   = G.spawn([...])                            # 登记子进程（自爆时会一起收）
    G.wait(p, timeout=30)                           # 带超时的 Popen.wait
    G.exec_loop(loop, ms=3000)                      # 带超时的 QEventLoop.exec

## 自爆行为

到点后：打印一行 `[TIMEOUT] …` → `taskkill /T /F` 收掉本脚本登记过的所有子进程
→ `os._exit(3)`。**必须硬退出**：卡在 C 层 / 死锁在锁上的线程没法优雅收尾，
护栏自己也会被一起吊住。退出码 3 便于和「断言失败(1)」区分。

环境变量 `TDL_NO_GUARD=1` 可整层关掉（只在调试护栏本身时才用）。

## 批量自检 / 安装

    python _tguard.py check   <目录或文件>...   # 审计：哪些脚本还没有护栏
    python _tguard.py install <目录或文件>...   # 给缺护栏的脚本补上（幂等）

`install` 只处理「测试/调试脚本」——跳过 `app/` 包与 `main.py`（产品代码），
也跳过已经有 daemon 自爆定时器的脚本；补的时长按脚本用途自动给
（打包/构建 600s、验收 120s、其余 60s）。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

__all__ = ["arm", "disarm", "remaining", "run", "spawn", "wait", "exec_loop",
           "kill_children", "DEFAULT_SECONDS", "EXIT_CODE"]

# import 即生效的硬上限：正常测试没有一个是该跑这么久的
DEFAULT_SECONDS = 180.0
EXIT_CODE = 3                    # 超时专用退出码（与断言失败 1 区分）

_timer: threading.Timer | None = None
_limit = DEFAULT_SECONDS         # 本次规定的时长（秒）
_armed_at = 0.0                  # 本次起算时刻
_lock = threading.Lock()
_children: list = []             # 本脚本启动过的子进程（自爆时一起收掉）


# ---------------------------------------------------------------- 打印
def _note(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:            # noqa: BLE001 - 护栏本身不许抛
        pass


# ---------------------------------------------------------------- 子进程登记
def _register(p) -> None:
    with _lock:
        _children.append(p)


def _unregister(p) -> None:
    with _lock:
        try:
            _children.remove(p)
        except ValueError:
            pass


def _kill_one(p) -> None:
    """结束单个子进程（连进程树）。taskkill 是异步的，所以先同步 kill 兜底。"""
    if p is None:
        return
    pid = getattr(p, "pid", None)
    try:
        if p.poll() is None:
            p.kill()
    except (OSError, ValueError):
        pass
    if pid:
        try:
            subprocess.Popen(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def kill_children() -> None:
    """结束本脚本登记过的全部子进程。"""
    with _lock:
        kids, _children[:] = list(_children), []
    for p in kids:
        _kill_one(p)


# ---------------------------------------------------------------- 自爆
def _boom() -> None:
    _note(f"[TIMEOUT] 超过规定时间 {_limit:.0f}s 仍未结束（已运行 "
          f"{time.monotonic() - _armed_at:.0f}s），自爆退出 —— 不留挂死的进程。")
    kill_children()
    os._exit(EXIT_CODE)


def arm(seconds: float = DEFAULT_SECONDS) -> None:
    """给本脚本规定一个时间上限（秒）；重复调用会**重新计时**。

    定时器必须是 daemon：否则它自己会让解释器一直等着，护栏反而变成吊死的原因。
    """
    global _timer, _limit, _armed_at
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        sec = DEFAULT_SECONDS
    _limit = max(1.0, sec)
    _armed_at = time.monotonic()
    if _timer is not None:
        _timer.cancel()
    _timer = threading.Timer(_limit, _boom)
    _timer.daemon = True
    _timer.start()


def disarm() -> None:
    """撤掉时间上限（脚本已确认跑完、只是要走一段较慢的收尾时用）。"""
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None


def remaining() -> float:
    """距离自爆还剩多少秒（未 arm 时返回极大值）。"""
    if _timer is None:
        return float("inf")
    return max(0.0, _limit - (time.monotonic() - _armed_at))


# ---------------------------------------------------------------- 带超时的子进程
def _pick_timeout(timeout) -> float:
    """给子进程挑一个上限：显式值优先，但绝不超过护栏剩余时间。"""
    rest = remaining()
    if timeout is None:
        return max(5.0, min(rest, DEFAULT_SECONDS))
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        t = DEFAULT_SECONDS
    return max(0.5, min(t, rest if rest != float("inf") else t))


def spawn(cmd, **kw):
    """启动子进程并登记（自爆时会被一起收掉）。"""
    p = subprocess.Popen(cmd, **kw)
    _register(p)
    return p


def wait(p, timeout: float | None = None):
    """带超时的 wait；超时先收进程树再抛 TimeoutExpired。"""
    to = _pick_timeout(timeout)
    try:
        return p.wait(timeout=to)
    except subprocess.TimeoutExpired:
        _note(f"[TIMEOUT] 子进程 pid={getattr(p, 'pid', '?')} 超过 {to:.0f}s，强制结束。")
        _kill_one(p)
        raise
    finally:
        _unregister(p)


def run(cmd, timeout: float | None = None, **kw):
    """带超时的 subprocess.run 替代（默认 capture_output + text）。"""
    to = _pick_timeout(timeout)
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    p = spawn(cmd, **kw)
    try:
        out, err = p.communicate(timeout=to)
    except subprocess.TimeoutExpired:
        _note(f"[TIMEOUT] 子进程超过 {to:.0f}s，强制结束：{cmd}")
        _kill_one(p)
        try:
            p.communicate(timeout=5)
        except Exception:        # noqa: BLE001
            pass
        raise
    finally:
        _unregister(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# ---------------------------------------------------------------- Qt 事件循环
def exec_loop(loop, ms: float = 3000, name: str = "事件循环") -> bool:
    """带超时的 `loop.exec()`；超时强制 quit 并返回 False（正常结束返回 True）。

    `loop` 只要有 `exec()` 与 `quit()` 即可（QEventLoop / QApplication 都行）。
    看门狗走独立线程，所以**不依赖被测代码自己回到事件循环**。
    """
    state = {"to": False}

    def _fire() -> None:
        state["to"] = True
        try:
            loop.quit()
        except Exception:        # noqa: BLE001 - 退不出去也只能靠自爆兜底
            pass

    t = threading.Timer(max(0.05, float(ms) / 1000.0), _fire)
    t.daemon = True
    t.start()
    try:
        loop.exec()
    finally:
        t.cancel()
    if state["to"]:
        _note(f"[TIMEOUT] {name} 超过 {ms:.0f}ms 未返回，已强制退出（不等它）。")
    return not state["to"]


# ---------------------------------------------------------------- 自动生效
if not os.environ.get("TDL_NO_GUARD"):
    arm(DEFAULT_SECONDS)


# ---------------------------------------------------------------- 自检 / 安装
# 用法：
#     python _tguard.py check   <目录或文件>...    # 审计哪些脚本还没护栏
#     python _tguard.py install <目录或文件>...    # 给缺护栏的脚本补上（幂等）
#
# 只处理「测试/调试脚本」：跳过 app/ 包、main.py（产品代码）与 _tguard.py 自己。
_SKIP_NAMES = {"main.py", "_tguard.py", "sitecustomize.py"}


def _targets(roots):
    """把命令行参数展开成待处理的 .py 文件列表。"""
    import pathlib
    out = []
    for r in roots:
        p = pathlib.Path(r)
        if p.is_dir():
            out.extend(sorted(q for q in p.glob("*.py")
                              if q.name not in _SKIP_NAMES))
        elif p.suffix == ".py" and p.name not in _SKIP_NAMES:
            out.append(p)
    return out


def _timeout_for(path) -> int:
    """按脚本用途给一个默认时长（秒）。"""
    n = path.name
    if n.startswith(("t_pack", "t_build")):
        return 600                       # 打包 / PyInstaller：实测 60~180s
    if n in ("acceptance.py", "e2e.py"):
        return 120                       # 要真的跑 tdl
    return 60                            # 探针 / 补丁 / 检查类：秒级


def _insert_line(lines, text) -> int | None:
    """找一个安全的插入位置：`__future__` 之后、其它 import 之前。"""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    pos = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                pos = node.end_lineno          # 1-based → 插到它后面
            else:
                pos = node.lineno - 1          # 0-based → 插到它前面
            break
    return pos


def _apply(path) -> str:
    """给一个脚本补护栏；返回 'ok' / 'skip:...' / 'fail:...'。"""
    import re as _re
    text = path.read_text(encoding="utf-8")
    if "_tguard" in text:
        return "skip:已有护栏"
    for m in _re.finditer(r"threading\.Timer\(", text):
        seg = text[m.start():m.start() + 250]
        if _re.search(r"daemon\s*=\s*True", seg):
            return "skip:已有 daemon 自爆定时器"
    pos = _insert_line(text.splitlines(keepends=True), text)
    if pos is None:
        return "fail:解析不了（语法错误？）"
    sec = _timeout_for(path)
    lines = text.splitlines(keepends=True)
    lines.insert(pos, "import _tguard as _G          "
                      "# 统一超时护栏（超时自爆退出，见 _tguard.py）\n")
    lines.insert(pos + 1, f"_G.arm({sec})                    "
                          f"# 本脚本规定 {sec} 秒，到点自爆、不留挂死的进程\n\n")
    path.write_text("".join(lines), encoding="utf-8")
    return f"ok:arm({sec})"


def check(roots) -> int:
    import re as _re
    bad = 0
    files = _targets(roots)
    for p in files:
        t = p.read_text(encoding="utf-8", errors="replace")
        has = "_tguard" in t
        daemon_timer = False
        for m in _re.finditer(r"threading\.Timer\(", t):
            if _re.search(r"daemon\s*=\s*True", t[m.start():m.start() + 250]):
                daemon_timer = True
                break
        subwo = 0
        for m in _re.finditer(r"subprocess\.(run|check_output|call|Popen)\(", t):
            if "timeout=" not in t[m.start():m.start() + 600]:
                subwo += 1
        tag = "ok" if (has or daemon_timer) else "缺护栏"
        if subwo:
            tag = (tag + f" · subprocess 无 timeout ×{subwo}").strip(" ·")
        if tag != "ok":
            bad += 1
        print(f"  {p.name:<28}{tag}")
    print(f"\n扫描 {len(files)} 个脚本，需处理 {bad} 个")
    return 0


def install(roots) -> int:
    n_ok = n_skip = 0
    for p in _targets(roots):
        r = _apply(p)
        print(f"  {p.name:<28}{r}")
        if r.startswith("ok"):
            n_ok += 1
        else:
            n_skip += 1
    print(f"\n补上护栏 {n_ok} 个，跳过/失败 {n_skip} 个")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 3 or _sys.argv[1] not in ("check", "install"):
        print(__doc__)
        print("用法：python _tguard.py check|install <目录或文件>...")
        _sys.exit(2)
    _sys.exit(check(_sys.argv[2:]) if _sys.argv[1] == "check"
              else install(_sys.argv[2:]))
