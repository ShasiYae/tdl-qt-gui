# -*- coding: utf-8 -*-
"""
tdl 部署核心 —— 从独立部署助手合并进 GUI 的部分。

分工边界（与 envcheck.py 的关系）：
    · envcheck.py  只读：探测、自检、发现（不写任何东西）
    · deploy.py    **会写**：下载安装、写 PATH / 环境变量、生成兜底脚本、修复
两边的探测函数（verify_proxy / tdl_version 等）直接复用 envcheck，不重复实现。

页面交互约定：所有耗时函数都接受 `log(level, msg)` 回调，由 UI 层丢后台线程跑。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import envcheck, paths

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
UA = {"User-Agent": "tdl-qt-client-deploy/1.0"}

TDL_REPO = "iyear/tdl"
TDL_ASSET = "tdl_Windows_64bit.zip"
FFMPEG_REPO = "BtbN/FFmpeg-Builds"
FFMPEG_ASSET = "ffmpeg-master-latest-win64-gpl.zip"

# 下载通道（国内 release 域名常被墙，api.github.com 却通）
MIRRORS = [
    ("直连 GitHub", ""),
    ("加速镜像 gh-proxy.com", "https://gh-proxy.com"),
    ("加速镜像 hk.gh-proxy.com", "https://hk.gh-proxy.com"),
]

# tdl 认 TDL_<FLAG> 形式的环境变量（内含 viper；旧批处理脚本就是这么用的）
ENV_SPECS = [
    ("TDL_THREADS",           "单任务线程", "8",      "单文件分段下载线程数，默认 4"),
    ("TDL_LIMIT",             "并发任务",   "6",      "同时下载文件数，默认 2"),
    ("TDL_POOL",              "DC 连接池",  "5",      "默认 8，设太大会频繁断流"),
    ("TDL_SIZE",              "分片大小",   "262144", "单位字节，默认 262144"),
    ("TDL_RECONNECT_TIMEOUT", "重连超时",   "0",      "0 表示不限制重连"),
    ("TDL_NTP",               "NTP 服务器", "",       "留空用系统时间；时间不准时强烈建议配"),
]
ENV_NAMES = [s[0] for s in ENV_SPECS]
ENV_DEFAULTS = {s[0]: s[2] for s in ENV_SPECS}
ENV_PROXY_KEY = "TDL_PROXY"

NTP_SERVERS = ["ntp.aliyun.com", "ntp1.aliyun.com", "cn.pool.ntp.org", "time.windows.com"]
NTP_EPOCH_DELTA = 2208988800
TIME_WARN_SEC = 30
TIME_BAD_SEC = 120

_PROXY_FAIL_KEYS = ("timeout", "timed out", "connection refused", "connection reset",
                    "no such host", "network is unreachable", "i/o timeout",
                    "dial tcp", "context deadline exceeded", "eof")


# ============================================================ 基础
def run(args, timeout: int = 60, env: Optional[dict] = None):
    """执行命令返回 (rc, 合并输出)；绝不抛异常。"""
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True,
                           timeout=timeout, env=env, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        out = (p.stdout or b"") + (b"\n" + p.stderr if p.stderr else b"")
        return p.returncode, out.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, "执行超时"
    except Exception as e:                                    # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _opener(proxy: Optional[str]):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))       # 显式直连
    return urllib.request.build_opener(*handlers)


def _apply_mirror(url: str, mirror: str) -> str:
    if not mirror:
        return url
    m = mirror.rstrip("/")
    return url if url.startswith(m) else f"{m}/{url}"


def download(url: str, dest: Path, progress=None, proxy: Optional[str] = None,
             cancel: Optional[Callable[[], bool]] = None, mirror: str = "") -> Path:
    """分块下载；progress(ratio, 文案) 回调；支持取消。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(_apply_mirror(url, mirror), headers=UA)
    with _opener(proxy).open(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got, t0 = 0, time.time()
        with open(tmp, "wb") as f:
            while True:
                if cancel and cancel():
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise InterruptedError("用户取消")
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    speed = got / max(time.time() - t0, 0.001) / 1048576
                    progress(got / total if total else 0.0,
                             f"{got / 1048576:.1f} MB"
                             + (f" / {total / 1048576:.1f} MB" if total else "")
                             + f" ({speed:.1f} MB/s)")
    tmp.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def http_text(url: str, proxy=None, timeout: int = 20, mirror: str = "") -> str:
    req = urllib.request.Request(_apply_mirror(url, mirror), headers=UA)
    with _opener(proxy).open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def github_latest(repo: str, proxy=None) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest", headers=UA)
    with _opener(proxy).open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ============================================================ 注册表 / PATH
def _broadcast_env_change() -> None:
    if os.name != "nt":
        return
    try:
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x1A, 0, ctypes.c_wchar_p("Environment"), 0x0002, 5000,
            ctypes.byref(res))
    except Exception:                                         # noqa: BLE001
        pass


def read_registry_value(name: str) -> str:
    """用户级环境变量的注册表真值（不受当前进程影响）。"""
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, name)
            return (v or "").strip()
    except (FileNotFoundError, OSError):
        return ""


def set_user_env(name: str, value: str) -> None:
    """写用户级环境变量；value 为空串则删除。"""
    if os.name != "nt":
        raise RuntimeError("仅支持 Windows")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as k:
        if value == "":
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    os.environ[name] = value
    _broadcast_env_change()


def read_user_path() -> str:
    if os.name != "nt":
        return os.environ.get("PATH", "")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, "Path")
            return v or ""
    except FileNotFoundError:
        return ""


def path_has(folder: str) -> bool:
    want = str(Path(folder).resolve()).rstrip("\\").lower()
    return want in [p.rstrip("\\").lower() for p in read_user_path().split(";") if p.strip()]


def add_to_user_path(folder: str) -> bool:
    """目录加入用户 PATH；已存在返回 False。"""
    if os.name != "nt":
        raise RuntimeError("仅支持 Windows")
    import winreg
    folder = str(Path(folder).resolve()).rstrip("\\")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
        try:
            cur, typ = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in (cur or "").split(";") if p.strip()]
        if folder.lower() in [p.rstrip("\\").lower() for p in parts]:
            return False
        parts.append(folder)
        winreg.SetValueEx(k, "Path", 0, typ or winreg.REG_EXPAND_SZ, ";".join(parts))
    os.environ["PATH"] = os.environ.get("PATH", "") + ";" + folder
    _broadcast_env_change()
    return True


# ============================================================ 安装
def _progress_log(log):
    """把 progress 回调节流成日志（每 20% 一条，避免刷屏）。"""
    last = {"v": -1}

    def cb(ratio, text):
        v = int(ratio * 5)
        if v != last["v"]:
            last["v"] = v
            log("info", f"  {int(ratio * 100)}%  {text}")
    return cb


def install_tdl(log: Callable, install_dir: Path, proxy=None, force: bool = False,
                cancel=None, mirror: str = "") -> dict:
    """下载并安装 tdl 到 install_dir（已装且同版本时跳过）。"""
    install_dir = Path(install_dir)
    local = install_dir / "tdl.exe"
    cur = local if local.exists() else None

    log("step", "查询 tdl 最新版本…")
    rel = github_latest(TDL_REPO, proxy)
    tag = rel["tag_name"].lstrip("v")
    log("info", f"最新版本 v{tag}")

    if cur and not force:
        have = envcheck.tdl_version(cur)
        if have == tag:
            log("ok", f"已是最新版 v{have}，跳过下载（{cur}）")
            return {"ok": True, "version": have, "path": str(cur), "skipped": True}
        if have:
            log("info", f"本机已有 v{have}，将更新到 v{tag}")

    asset = next((a for a in rel["assets"] if a["name"] == TDL_ASSET), None)
    if not asset:
        raise RuntimeError(f"Release 里找不到 {TDL_ASSET}")
    tmp = Path(tempfile.gettempdir()) / f"tdl-{tag}"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / TDL_ASSET

    log("info", f"下载 {TDL_ASSET}（{asset['size'] / 1048576:.1f} MB）…")
    download(asset["browser_download_url"], zip_path,
             _progress_log(log), proxy, cancel, mirror)

    sums = next((a["browser_download_url"] for a in rel["assets"]
                 if a["name"] == "tdl_checksums.txt"), "")
    if sums:
        try:
            want = ""
            for line in http_text(sums, proxy, 15, mirror).splitlines():
                if line.strip().endswith(TDL_ASSET):
                    want = line.split()[0]
            got = sha256_of(zip_path)
            if want and got.lower() != want.lower():
                raise RuntimeError(f"SHA256 不匹配（期望 {want}，实际 {got}）")
            log("ok", "SHA256 校验通过")
        except RuntimeError:
            raise
        except Exception as e:                                # noqa: BLE001
            log("warn", f"跳过校验（{type(e).__name__}）")

    log("info", f"解压到 {install_dir} …")
    install_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if Path(member).name.lower() == "tdl.exe":
                with z.open(member) as src, open(install_dir / "tdl.exe", "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    exe = install_dir / "tdl.exe"
    if not exe.exists():
        raise RuntimeError("解压后没找到 tdl.exe")
    ver = envcheck.tdl_version(exe)
    log("ok", f"tdl v{ver} 安装完成 → {exe}")
    return {"ok": True, "version": ver, "path": str(exe), "skipped": False}


def install_ffmpeg(log: Callable, install_dir: Path, proxy=None, force: bool = False,
                   cancel=None, mirror: str = "") -> dict:
    """安装 ffmpeg（Windows 不自带；只提取 ffmpeg.exe / ffprobe.exe）。"""
    install_dir = Path(install_dir)
    exe = install_dir / "ffmpeg.exe"
    if exe.exists() and not force:
        log("ok", f"ffmpeg 已在软件目录：{exe}")
        return {"ok": True, "path": str(exe), "skipped": True}
    if envcheck.system_ffmpeg() and not force:
        log("ok", f"系统 PATH 已有 ffmpeg，跳过（{envcheck.system_ffmpeg()}）")
        return {"ok": True, "path": envcheck.system_ffmpeg(), "skipped": True}

    log("info", "查询 ffmpeg 构建版本…")
    rel = github_latest(FFMPEG_REPO, proxy)
    asset = next((a for a in rel["assets"] if a["name"] == FFMPEG_ASSET), None)
    if not asset:
        raise RuntimeError(f"Release 里找不到 {FFMPEG_ASSET}")
    tmp = Path(tempfile.gettempdir()) / "tdl-ffmpeg"
    tmp.mkdir(parents=True, exist_ok=True)
    zip_path = tmp / FFMPEG_ASSET
    log("info", f"下载 ffmpeg（{asset['size'] / 1048576:.1f} MB，较大请耐心）…")
    download(asset["browser_download_url"], zip_path,
             _progress_log(log), proxy, cancel, mirror)

    log("info", "解压提取 ffmpeg.exe / ffprobe.exe …")
    install_dir.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe"}
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if Path(member).name.lower() in wanted:
                with z.open(member) as src, open(install_dir / Path(member).name, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    if not exe.exists():
        raise RuntimeError("解压后没找到 ffmpeg.exe")
    rc, out = run([exe, "-version"], timeout=15)
    log("ok", f"ffmpeg 安装完成：{out.splitlines()[0] if out else exe}")
    return {"ok": True, "path": str(exe), "skipped": False}


def ensure_path(log: Callable, install_dir: Path) -> dict:
    added = add_to_user_path(str(install_dir))
    if added:
        log("ok", f"已把 {install_dir} 写入用户 PATH（新开终端生效）")
    else:
        log("info", f"{install_dir} 已在 PATH 里，无需重复写")
    return {"ok": True, "added": added}


def apply_env(log: Callable, values: dict) -> dict:
    """写 TDL_* 环境变量（含 TDL_PROXY）。空串 = 删除该变量。"""
    written, removed = [], []
    for name, val in values.items():
        if name not in (ENV_NAMES + [ENV_PROXY_KEY]):
            continue
        val = (val or "").strip()
        if val == os.environ.get(name, ""):
            continue
        set_user_env(name, val)
        (written if val else removed).append(name)
    if written:
        log("ok", "已写入：" + ", ".join(f"{n}={values[n]}" for n in written))
    if removed:
        log("info", "已清除：" + ", ".join(removed))
    if not written and not removed:
        log("info", "环境变量与目标值一致，无需改动")
    return {"ok": True, "written": written, "removed": removed}


def write_tdl_wrapper(proxy: str, install_dir: Path) -> Path:
    """生成 tdlx.cmd —— 把 --proxy 写死，不依赖环境变量的兜底入口。"""
    install_dir = Path(install_dir)
    bat = install_dir / "tdlx.cmd"
    body = ("@echo off\r\n"
            "rem tdl wrapper -- proxy baked in, no env vars needed\r\n"
            f'"{install_dir / "tdl.exe"}" --proxy "{proxy}" %*\r\n')
    try:
        bat.write_text(body, encoding="gbk")
    except UnicodeEncodeError:
        bat.write_text(body, encoding="gbk", errors="replace")
    return bat


def open_login_terminal(mode: str, ns: str, proxy: str, exe: str) -> str:
    """新控制台窗口跑交互式登录（验证码 / 扫码都需要键盘输入），返回命令行。

    bat 用 GBK 落盘 —— cmd.exe 按 ANSI 代码页解析脚本，UTF-8 中文会乱码。
    """
    args = [f'"{exe}"']
    if ns:
        args.append(f"-n {ns}")
    if proxy:
        args.append(f'--proxy "{proxy}"')
    args.append(f"login -T {mode}")
    cmd = " ".join(args)
    bat = Path(tempfile.gettempdir()) / f"tdl-login-{int(time.time())}.bat"
    body = "@echo off\r\nchcp 65001 >nul\r\n" + cmd + "\r\npause\r\n"
    try:
        bat.write_text(body, encoding="gbk")
    except UnicodeEncodeError:
        bat.write_text(body, encoding="gbk", errors="replace")
    subprocess.Popen(["cmd", "/c", "start", "", "tdl login", str(bat)])
    return cmd


# ============================================================ 系统时间
def ntp_offset(server: str, timeout: float = 4.0) -> Optional[float]:
    """SNTP 查询，返回「本地 − 服务器」秒数（正=本地快）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\x00", (server, 123))
        data, _ = s.recvfrom(48)
        t1 = time.time()
        s.close()
        if len(data) < 48:
            return None
        secs = int.from_bytes(data[40:44], "big")
        frac = int.from_bytes(data[44:48], "big")
        if secs == 0:
            return None
        return (t0 + t1) / 2.0 - (secs - NTP_EPOCH_DELTA + frac / 4294967296.0)
    except Exception:                                         # noqa: BLE001
        return None


def http_time_offset(proxy=None, timeout: float = 6) -> Optional[float]:
    """降级：用 HTTP 响应头 Date 估算偏差（UDP 123 常被拦）。"""
    import email.utils
    for url in ("http://www.gstatic.com/generate_204",
                "http://cp.cloudflare.com/generate_204"):
        try:
            req = urllib.request.Request(url, headers=UA)
            with _opener(proxy).open(req, timeout=timeout) as r:
                hdr = r.headers.get("Date")
            if not hdr:
                continue
            return time.time() - email.utils.parsedate_to_datetime(hdr).timestamp()
        except Exception:                                     # noqa: BLE001
            continue
    return None


def check_system_time(proxy=None, log=None) -> dict:
    """Telegram 的 MTProto 握手带时间戳 —— 时间偏差过大症状与「代理不通」一样。"""
    def say(level, msg):
        if log:
            log(level, msg)

    off = None
    src = ""
    for srv in NTP_SERVERS[:3]:
        say("info", f"向 {srv} 校时…")
        off = ntp_offset(srv)
        if off is not None:
            src = f"NTP {srv}"
            break
    if off is None:
        say("warn", "NTP（UDP 123）不可达，改用 HTTP 时间头估算")
        off = http_time_offset(proxy)
        src = "HTTP Date 头（±2s）"

    local_str = time.strftime("%Y-%m-%d %H:%M:%S")
    if off is None:
        return {"ok": None, "offset": None,
                "text": f"无法自动校时（本机 {local_str}）",
                "hint": "UDP 123 与 HTTP 都不通；请手动核对系统时间与时区"}

    a = abs(off)
    if a < TIME_WARN_SEC:
        return {"ok": True, "offset": off,
                "text": f"偏差 {off:+.1f}s，正常（{src}）", "hint": ""}
    if a < TIME_BAD_SEC:
        return {"ok": False, "offset": off,
                "text": f"偏差 {off:+.1f}s，偏大（{src}）",
                "hint": "可能引起连接异常，点「一键修复」校准"}
    return {"ok": False, "offset": off,
            "text": f"偏差 {off:+.1f}s，严重偏大（{src}）",
            "hint": "偏差过大会直接连不上 Telegram，点「一键修复」"}


def check_ntp_env() -> str:
    return read_registry_value("TDL_NTP") or (os.environ.get("TDL_NTP") or "").strip()


def fix_system_time(ntp: str = "ntp.aliyun.com", log=None) -> dict:
    """① 给 tdl 设 TDL_NTP（免管理员，首选）② 顺带尝试 w32tm /resync。"""
    def say(level, msg):
        if log:
            log(level, msg)

    actions = []
    old = check_ntp_env()
    if old != ntp:
        set_user_env("TDL_NTP", ntp)
        say("ok", f"已设置 TDL_NTP={ntp} —— tdl 自行校准，绕过本机时钟偏差")
    else:
        say("info", f"TDL_NTP 已是 {ntp}")
    actions.append(f"TDL_NTP={ntp}")

    rc, out = run(["w32tm", "/resync", "/force"], timeout=30)
    if rc == 0:
        say("ok", "Windows 系统时间已重新同步")
        actions.append("w32tm /resync 成功")
    else:
        say("warn", f"w32tm 同步未成功：{(out or '').strip()[:80] or '通常需管理员权限'}")
        actions.append("本机时间未同步（w32tm /resync 需管理员）")
    return {"ok": True, "actions": actions, "ntp": ntp}


# ============================================================ 生效检测与修复
def _tdl_net_probe(exe, ns: str, proxy: str, via_env: bool, timeout: int = 25):
    """跑 `tdl chat ls` 判断网络是否真通。

    注意：tdl 报 `not authorized` 时**网络其实已经连通**（要先连通才做鉴权），
    算「代理好、只是没登录」，不能误报为代理故障。
    """
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    args = [str(exe), "-n", ns]
    if via_env:
        env["TDL_PROXY"] = proxy
    else:
        env.pop("TDL_PROXY", None)
        if proxy:
            args += ["--proxy", proxy]
    rc, out = run(args + ["chat", "ls"], timeout=timeout, env=env)
    low = out.lower()
    if rc == 0:
        return "ok", out
    if "not authorized" in low or "unauthorized" in low:
        return "noauth", out
    return "fail", out


def check_proxy_effective(proxy: str = "", ns: str = "default",
                          run_tdl: bool = True, timeout: int = 25,
                          log=None) -> dict:
    """代理 5 级验证：注册表 → 进程可见 → 可连外网 → tdl 显式 → tdl 环境变量。"""
    def say(level, msg):
        if log:
            log(level, msg)

    proxy = (proxy or "").strip()
    exe = envcheck.resolve_tdl_path()
    items: list[dict] = []

    def add(key, title, ok, text, hint=""):
        items.append({"key": key, "title": title, "ok": ok, "text": text, "hint": hint})

    reg = read_registry_value(ENV_PROXY_KEY)
    if not proxy:
        add("written", "变量已写入", bool(reg),
            f"注册表 {ENV_PROXY_KEY} = {reg}" if reg else "未配置",
            "" if reg else "先填代理地址，再点「一键修复」")
    elif reg == proxy:
        add("written", "变量已写入", True, f"注册表 {ENV_PROXY_KEY} = {proxy}")
    elif reg:
        add("written", "变量已写入", False, f"注册表是 {reg}，与目标不一致", "点「一键修复」重写")
    else:
        add("written", "变量已写入", False, "注册表里没有", "点「一键修复」写入")

    visible = (os.environ.get(ENV_PROXY_KEY) or "").strip()
    if not proxy:
        add("visible", "当前进程可见", bool(visible), visible or "不可见")
    elif visible == proxy:
        add("visible", "当前进程可见", True, proxy)
    else:
        add("visible", "当前进程可见", False, visible or "当前进程读不到",
            "环境变量只对新进程生效：GUI 与终端都要重启")

    reachable = None
    if proxy:
        say("info", f"验证 {proxy} 能否连外网…")
        reachable = envcheck.verify_proxy(proxy, timeout=8)
        add("reachable", "代理可连外网", reachable,
            "✓ 能访问墙外地址" if reachable else "✗ 连不上（代理没开？端口变了？）",
            "" if reachable else "点「自动探测」换一个端口")

    tdl_direct = tdl_env = None
    if run_tdl and proxy and exe:
        say("step", "tdl 实测（显式 --proxy）…")
        st, _ = _tdl_net_probe(exe, ns, proxy, False, timeout)
        tdl_direct = st in ("ok", "noauth")
        add("tdl_direct", "tdl 显式代理", tdl_direct,
            {"ok": "✓ 连上且已登录", "noauth": "✓ 连上（未登录，网络是通的）"}.get(st, "✗ 连不上"),
            "" if tdl_direct else "代理本身不通，先解决上一项")
        if tdl_direct:
            say("step", "tdl 实测（只靠环境变量）…")
            st2, _ = _tdl_net_probe(exe, ns, proxy, True, timeout)
            tdl_env = st2 in ("ok", "noauth")
            add("tdl_env", "tdl 用环境变量", tdl_env,
                {"ok": "✓ 完全生效（已登录）", "noauth": "✓ 生效（未登录）"}.get(st2, "✗ 没连上"),
                "" if tdl_env else "环境变量没被 tdl 读到：重启 GUI / 终端，或用 tdlx.cmd 兜底")
        else:
            add("tdl_env", "tdl 用环境变量", None, "跳过（上一项未通过）")
    else:
        why = "未配置代理" if not proxy else ("找不到 tdl.exe" if not exe else "已跳过")
        add("tdl_direct", "tdl 显式代理", None, why)
        add("tdl_env", "tdl 用环境变量", None, why)

    effective = tdl_env is True
    if not proxy:
        advice = "尚未配置代理。先「自动探测」找可用端口，再「一键修复」写入。"
    elif effective:
        advice = "✓ 代理已生效：tdl 能通过 TDL_PROXY 连上 Telegram。"
    elif reachable and tdl_direct and tdl_env is False:
        advice = "代理端口是通的，但 tdl 没读到环境变量。点「一键修复」；仍不行用 tdlx.cmd。"
    elif reachable is False:
        advice = "代理端口连不通。点「自动探测」换端口，再「一键修复」。"
    else:
        advice = "未能确认生效，按上面逐项排查。"

    return {"proxy": proxy, "items": items, "ok": effective, "advice": advice}


def check_effective(proxy: str = "", ns: str = "default",
                    run_tdl: bool = True, timeout: int = 25, log=None) -> dict:
    """连通性体检 = 代理 5 级验证 + 系统时间 + NTP 配置。"""
    res = check_proxy_effective(proxy, ns, run_tdl, timeout, log)
    items = list(res["items"])

    if log:
        log("step", "检查系统时间…")
    t = check_system_time(proxy or None, log)
    items.append({"key": "time", "title": "系统时间偏差", "ok": t["ok"],
                  "text": t["text"], "hint": t["hint"]})
    ntp_set = check_ntp_env()
    items.append({"key": "ntp", "title": "tdl NTP 校准",
                  "ok": (bool(ntp_set) if t["ok"] is False else None),
                  "text": f"TDL_NTP = {ntp_set}" if ntp_set else "未配置（tdl 用系统时间）",
                  "hint": "" if ntp_set else "时间不准时建议配置，tdl 会自行校准"})

    proxy_ok, time_ok = res["ok"], t["ok"] is not False
    if proxy_ok and time_ok:
        advice = "✓ 代理与系统时间都正常，可以正常下载。"
    else:
        parts = []
        if not proxy_ok:
            parts.append(res["advice"])
        if t["ok"] is False:
            parts.append(f"系统时间偏差 {t['offset']:+.0f}s 同样会导致连不上，"
                         f"「一键修复」会给 tdl 配 NTP。")
        advice = " ".join(parts) or res["advice"]

    return {**res, "items": items, "ok": proxy_ok and time_ok,
            "proxy_ok": proxy_ok, "time_ok": time_ok, "advice": advice}


def fix_proxy(proxy: str, install_dir: Path, ns: str = "default", log=None) -> dict:
    """修复「代理没生效」：重写环境变量 + 生成 tdlx.cmd 兜底。"""
    def say(level, msg):
        if log:
            log(level, msg)

    proxy = (proxy or "").strip()
    if not proxy:
        return {"ok": False, "msg": "没有可用的代理地址，先「自动探测」"}

    old = read_registry_value(ENV_PROXY_KEY)
    if old != proxy:
        set_user_env(ENV_PROXY_KEY, proxy)
        say("ok", f"写入 {ENV_PROXY_KEY}={proxy}" + (f"（原值 {old}）" if old else ""))
    else:
        say("info", f"{ENV_PROXY_KEY} 已是目标值，无需重写")
    wp = write_tdl_wrapper(proxy, install_dir)
    say("ok", f"生成兜底脚本 {wp.name}（--proxy 写死，不依赖环境变量）")
    say("info", "环境变量只对新进程生效：已开的终端 / GUI 需重启")
    return {"ok": True, "proxy": proxy, "wrapper": str(wp)}


def fix_effective(proxy: str, install_dir: Path, ns: str = "default", log=None) -> dict:
    """一键修复：代理 + 系统时间。"""
    out = {}
    if (proxy or "").strip():
        out["proxy"] = fix_proxy(proxy, install_dir, ns, log=log)
    else:
        out["proxy"] = {"ok": False, "msg": "未填代理地址，跳过代理修复"}
        if log:
            log("warn", out["proxy"]["msg"])
    out["time"] = fix_system_time(log=log)
    return out


# ============================================================ 状态导出
def export_state(install_dir: Path, extra: Optional[dict] = None) -> dict:
    """导出 deploy-state.json —— 给外部脚本 / 其它工具读的接口契约。"""
    from . import data as _data          # 延迟导入避免环
    exe = envcheck.resolve_tdl_path()
    state = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tdl_exe": exe,
        "tdl_version": envcheck.tdl_version(exe) if exe else "",
        "ffmpeg_exe": (paths.app_root() / "ffmpeg.exe").as_posix()
                      if (paths.app_root() / "ffmpeg.exe").exists() else envcheck.system_ffmpeg(),
        "install_dir": str(install_dir),
        "env": {n: os.environ.get(n, "") for n in ([ENV_PROXY_KEY] + ENV_NAMES)},
        "namespaces": envcheck.namespaces(),
        "logged_in": bool(envcheck.namespaces()),
        "gui_default_tdl": _data.DEFAULT_TDL_PATH,
    }
    if extra:
        state.update(extra)
    out = paths.app_root() / "deploy-state.json"
    out.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
