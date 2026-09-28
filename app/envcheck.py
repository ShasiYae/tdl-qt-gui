# -*- coding: utf-8 -*-
"""
环境自检 —— tdl 运行环境的**只读**体检。

与主程序的分工（重要）：
    · 本模块**只读**：只做检测与探测，绝不写环境变量、不改配置、不下载安装
    · 代理的写入与修改一律在「tdl 部署助手」里完成，这里只负责发现与告警
    · 系统里已有代理配置（TDL_PROXY）时**直接沿用**，不做任何修改

检测项：
    ① tdl 可执行文件 —— deploy-state.json → 软件目录 → PATH → 常见位置
    ② ffmpeg —— 软件目录 / 系统 PATH（Windows 不自带，缺失只影响转码）
    ③ 代理 —— TDL_PROXY 环境变量 + 本机代理端口探测 + 真实连通性验证
    ④ 登录态 —— ~/.tdl/data 下的命名空间库文件

对外主要接口：
    resolve_tdl_path()      定位 tdl.exe
    sniff_proxies()         探测本机可用代理（三级来源）
    check_env()             一次体检，返回结构化结果
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from . import paths

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_UA = {"User-Agent": "tdl-qt-client/1.0"}


def _parallel_map(fn, items, workers: int = 16) -> list:
    """守护线程版并发 map。

    **为什么不用 ThreadPoolExecutor**：它建的线程是**非守护**的，解释器退出时
    会 `join` 它们（`concurrent.futures.thread._python_exit`）。我们的探测会跑
    网络请求，用户关窗口时探测还没结束 → 进程一直退不掉，表现为「卡死」。
    这里全部用 daemon 线程，主程序想退随时能退。
    """
    items = list(items)
    if not items:
        return []
    out: list = [None] * len(items)
    lock = threading.Lock()
    cursor = iter(range(len(items)))

    def worker():
        while True:
            with lock:
                try:
                    i = next(cursor)
                except StopIteration:
                    return
            try:
                out[i] = fn(items[i])
            except Exception:                                 # noqa: BLE001
                out[i] = None

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out

# ---------------------------------------------------------------- 代理探测常量
PROXY_HINTS = [7890, 7891, 7892, 7897, 7899, 1080, 1081, 1086, 1087,
               10808, 10809, 10810, 20171, 2080, 2081, 4780, 8118, 8888, 8889]

PROXY_PROC_KEYS = ["clash", "mihomo", "verge", "nyanpasu", "flclash", "v2ray", "xray",
                   "sing-box", "singbox", "shadowsocks", "hysteria", "tuic", "naive",
                   "netch", "proxifier", "winsw", "ss-local"]

CLASH_CONFIG_TEMPLATES = [
    (r"%USERPROFILE%\.config\clash\config.yaml",                          "Clash for Windows"),
    (r"%USERPROFILE%\.config\clash\profiles\*.yaml",                      "CFW 订阅配置"),
    (r"%USERPROFILE%\.config\clash\profiles\*.yml",                       "CFW 订阅配置"),
    (r"%USERPROFILE%\.config\clash-verge\config.yaml",                    "Clash Verge"),
    (r"%APPDATA%\clash-verge\config.yaml",                                "Clash Verge"),
    (r"%USERPROFILE%\.config\clash-verge-rev\config.yaml",                "Clash Verge Rev"),
    (r"%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\config.yaml",  "Clash Verge Rev"),
    (r"%USERPROFILE%\.config\mihomo\config.yaml",                         "Mihomo / Clash Meta"),
    (r"%USERPROFILE%\.config\clash-meta\config.yaml",                     "Clash Meta"),
    (r"%USERPROFILE%\.config\clash-nyanpasu\config.yaml",                 "Clash Nyanpasu"),
    (r"%APPDATA%\clash-nyanpasu\config.yaml",                             "Clash Nyanpasu"),
    (r"%USERPROFILE%\.config\flclash\config.yaml",                        "FlClash"),
    (r"%APPDATA%\FlClash\config.yaml",                                    "FlClash"),
    (r"%USERPROFILE%\.config\clash-rs\config.yaml",                       "clash-rs"),
]

# tdl.exe 的常见落点（最后的兜底）
TDL_FALLBACK_PATHS = [r"D:\bin\tdl.exe", r"C:\bin\tdl.exe"]

_PORT_LINE = re.compile(r"(?m)^\s*(mixed-port|socks-port|port)\s*:\s*[\"']?(\d+)")
_NETSTAT_LINE = re.compile(r"\s*TCP\s+(\S+?):(\d+)\s+\S+\s+LISTENING\s+(\d+)")

# 系统代理环境变量（TDL_PROXY 是 tdl 认的那个，优先）
PROXY_ENV_KEYS = ["TDL_PROXY", "ALL_PROXY", "all_proxy",
                  "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]


# ============================================================ 基础：定位 tdl
def _deploy_state_paths() -> list[Path]:
    """部署助手可能导出的状态文件位置（本程序目录 + 其上一级）。"""
    root = paths.app_root()
    return [root / "deploy-state.json", root.parent / "tdl-deploy" / "deploy-state.json"]


def _from_deploy_state() -> str:
    """从 tdl 部署助手导出的 deploy-state.json 里取 tdl 路径。"""
    for p in _deploy_state_paths():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        exe = str(obj.get("tdl_exe") or "").strip()
        if exe and os.path.isfile(exe):
            return exe
    return ""


def resolve_tdl_path() -> str:
    """按优先级定位 tdl.exe：

        ① 环境变量 TDL_EXE（显式覆盖，最高优先）
        ② 部署助手导出的 deploy-state.json
        ③ 本软件目录下的 tdl.exe（绿色便携）
        ④ 系统 PATH
        ⑤ 常见位置兜底

    全部找不到时返回空串，由调用方决定怎么提示。
    """
    env = (os.environ.get("TDL_EXE") or "").strip()
    if env and os.path.isfile(env):
        return env

    from_state = _from_deploy_state()
    if from_state:
        return from_state

    local = paths.app_root() / ("tdl.exe" if os.name == "nt" else "tdl")
    if local.is_file():
        return str(local)

    found = shutil.which("tdl")
    if found:
        return found

    for p in TDL_FALLBACK_PATHS:
        if os.path.isfile(p):
            return p
    return ""


def tdl_version(exe) -> str:
    """同步取 tdl 版本号（失败返回空串）。"""
    if not exe or not os.path.isfile(str(exe)):
        return ""
    try:
        cp = subprocess.run(
            [str(exe), "version"], capture_output=True, timeout=8,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (cp.stdout or cp.stderr or b"").decode("utf-8", "replace")
    for ln in text.splitlines():
        if ln.lower().startswith("version"):
            return ln.split(":", 1)[-1].strip()
    return ""


def system_ffmpeg() -> str:
    """系统 PATH 里的 ffmpeg（Windows 不自带，通常为空）。"""
    return shutil.which("ffmpeg") or ""


def local_ffmpeg() -> str:
    """本软件目录里的 ffmpeg。"""
    p = paths.app_root() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return str(p) if p.is_file() else ""


# ============================================================ 系统代理读取
def system_proxy() -> tuple[str, str]:
    """读系统里已配置的代理，返回 (地址, 来源环境变量名)。

    **只读**：tdl 认 TDL_PROXY 优先，其次通用代理变量。
    系统已经配好就直接沿用 —— 本程序不做任何写入或修改。
    """
    for k in PROXY_ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v, k
    return "", ""


# ============================================================ 端口 / 进程
def listening_ports() -> list[tuple[str, int, int]]:
    """本机 TCP 监听端口 [(地址, 端口, PID), ...]。"""
    if os.name != "nt":
        return []
    try:
        cp = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                            timeout=25, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (cp.stdout or b"").decode("utf-8", "replace")
    res = []
    for line in text.splitlines():
        m = _NETSTAT_LINE.match(line)
        if m:
            res.append((m.group(1), int(m.group(2)), int(m.group(3))))
    return res


def list_processes() -> dict[int, str]:
    """{PID: 进程名}。"""
    if os.name != "nt":
        return {}
    try:
        cp = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                            timeout=25, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    text = (cp.stdout or b"").decode("utf-8", "replace")
    procs: dict[int, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            try:
                procs[int(parts[1])] = parts[0]
            except ValueError:
                continue
    return procs


def proxy_process_ports() -> list[tuple[int, str]]:
    """正在运行的代理进程所占端口 → [(端口, 进程名)]。"""
    procs = list_processes()
    hits = []
    for _addr, port, pid in listening_ports():
        name = procs.get(pid, "")
        low = name.lower()
        if any(k in low for k in PROXY_PROC_KEYS):
            hits.append((port, name))
    return hits


def sniff_clash_configs() -> list[dict]:
    """从常见 clash 系配置里读端口（装了没开也能读到）。"""
    found, seen = [], set()
    for tpl, label in CLASH_CONFIG_TEMPLATES:
        for path in glob.glob(os.path.expandvars(tpl)):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _PORT_LINE.finditer(text):
                port, kind = int(m.group(2)), m.group(1)
                if (port, label) in seen:
                    continue
                seen.add((port, label))
                found.append({"port": port, "kind": kind, "source": label})
    return found


# ============================================================ 协议探测
def _socks5_handshake(host: str, port: int, timeout: float) -> bool:
    try:
        s = socket.create_connection((host, port), timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        r = s.recv(2)
        s.close()
        return len(r) == 2 and r[0] == 5 and r[1] == 0
    except OSError:
        return False


def _http_connect_ok(host: str, port: int, timeout: float) -> bool:
    try:
        s = socket.create_connection((host, port), timeout)
        s.settimeout(timeout)
        s.sendall(b"CONNECT www.gstatic.com:443 HTTP/1.1\r\n"
                  b"Host: www.gstatic.com:443\r\n\r\n")
        r = s.recv(64)
        s.close()
        first = r.split(b"\r\n", 1)[0]
        return first.startswith(b"HTTP/1.") and (b" 2" in first)
    except OSError:
        return False


def probe_proxy(host: str, port: int, timeout: float = 0.8) -> dict:
    """探测端口是 HTTP 还是 SOCKS5（mixed 端口两者都命中）。"""
    http_ok = _http_connect_ok(host, port, timeout)
    socks_ok = _socks5_handshake(host, port, timeout)
    kinds = ([ "http"] if http_ok else []) + (["socks5"] if socks_ok else [])
    scheme = "http" if http_ok else ("socks5" if socks_ok else "")
    return {"ok": bool(kinds), "kinds": kinds, "scheme": scheme}


def socks5_connect(proxy_host: str, proxy_port: int, dst_host: str, dst_port: int,
                   timeout: float = 6) -> socket.socket:
    """手搓 SOCKS5 CONNECT（urllib 不认 socks5://）。"""
    s = socket.create_connection((proxy_host, proxy_port), timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        s.close()
        raise OSError("SOCKS5 握手失败")
    hb = dst_host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + dst_port.to_bytes(2, "big"))
    r = s.recv(10)
    if len(r) < 2 or r[1] != 0:
        s.close()
        raise OSError("SOCKS5 CONNECT 被拒绝")
    return s


def _http_status(sock: socket.socket) -> str:
    data = sock.recv(256)
    parts = data.split(b"\r\n", 1)[0].decode("latin-1", "replace").split()
    return parts[1] if len(parts) >= 2 else ""


def verify_proxy(url: str, timeout: float = 7) -> bool:
    """通过代理实际请求一次墙外小资源，确认能不能用。"""
    if not url:
        return False
    if url.startswith("socks5://"):
        try:
            body = url[len("socks5://"):]
            host, _, port = body.rpartition(":")
            s = socks5_connect(host, int(port), "www.gstatic.com", 80, timeout)
            s.sendall(b"GET /generate_204 HTTP/1.1\r\nHost: www.gstatic.com\r\n"
                      b"User-Agent: tdl-qt-client\r\nConnection: close\r\n\r\n")
            code = _http_status(s)
            s.close()
            return code.startswith(("2", "3"))
        except (OSError, ValueError):
            return False
    import urllib.error
    import urllib.request
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url}))
        with opener.open(urllib.request.Request(
                "http://www.gstatic.com/generate_204", headers=_UA), timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except Exception:                                         # noqa: BLE001
        return False


def sniff_proxies(deep: bool = True, log=None,
                  probe_timeout: float = 0.8, verify_timeout: float = 7) -> list[dict]:
    """探测本机可用的 HTTP / SOCKS5 代理（三级来源，按可用性排序）。

    ① 正在运行的代理进程所占端口（最准）
    ② clash 系配置文件里的 mixed-port / socks-port（装了没开也能读到）
    ③ 常见代理端口 ∩ 本机实际监听
    deep=True 时还会通过代理实际请求一次墙外地址做可用性验证。
    """
    def say(level, msg):
        if log:
            log(level, msg)

    cand: dict[int, dict] = {}

    def add(port: int, source: str, rank: int):
        if not (0 < port <= 65535):
            return
        d = cand.setdefault(port, {"port": port, "sources": [], "rank": 9})
        if source not in d["sources"]:
            d["sources"].append(source)
        d["rank"] = min(d["rank"], rank)

    for port, name in proxy_process_ports():
        add(port, f"进程 {name}", 0)
    for item in sniff_clash_configs():
        add(item["port"], f"{item['source']} 配置", 1)
    live = {p for _a, p, _i in listening_ports()}
    for p in PROXY_HINTS:
        if p in live:
            add(p, "常见端口", 2)

    if not cand:
        say("warn", "没有发现候选端口：代理软件可能没启动，或用了非常规端口")
        return []

    ports = sorted(cand)
    say("step", f"探测 {len(ports)} 个候选端口…")
    for got in _parallel_map(
            lambda p: (p, probe_proxy("127.0.0.1", p, probe_timeout)), ports, 16):
        if got:
            cand[got[0]].update(got[1])

    alive = [c for c in cand.values() if c.get("ok")]
    if not alive:
        say("warn", "候选端口都没有代理协议应答")
        return []

    if deep:
        say("step", "验证连通性…")

        def check(c):
            return c, verify_proxy(f"{c['scheme'] or 'http'}://127.0.0.1:{c['port']}",
                                   verify_timeout)

        for got in _parallel_map(check, alive, 8):
            if got:
                got[0]["verified"] = got[1]
    else:
        for c in alive:
            c["verified"] = None

    for c in alive:
        scheme = c["scheme"] or "http"
        c["url"] = f"{scheme}://127.0.0.1:{c['port']}"
        c["label"] = (c["url"]
                      + ("  ✓可用" if c.get("verified") else
                         ("  ✗连不通" if c.get("verified") is False else ""))
                      + f"  ← {'、'.join(c['sources'])}")

    alive.sort(key=lambda c: (not c.get("verified"), c["rank"], c["port"]))
    return alive


# ============================================================ 登录态
def namespaces() -> list[str]:
    """本机已有的命名空间（每个命名空间是 ~/.tdl/data 下的一个库文件）。"""
    try:
        return sorted(p.name for p in paths.tdl_data_dir().iterdir() if p.is_file())
    except OSError:
        return []


# ============================================================ 汇总体检
def check_env(tdl_path: str = "", deep: bool = True, log=None) -> dict:
    """一次环境体检（只读），返回结构化结果供界面展示。

    {
      "tdl":    {"ok", "path", "version"},
      "ffmpeg": {"ok", "path", "source"},        source: local / system / none
      "proxy":  {"env", "env_key", "alive", "candidates", "advice", "tone"},
      "login":  {"ok", "namespaces"},
    }
    """
    def say(level, msg):
        if log:
            log(level, msg)

    exe = tdl_path or resolve_tdl_path()
    ver = tdl_version(exe)

    ff_local, ff_sys = local_ffmpeg(), system_ffmpeg()
    ff_path, ff_src = ((ff_local, "local") if ff_local else
                       ((ff_sys, "system") if ff_sys else ("", "none")))

    env_proxy, env_key = system_proxy()
    p_info: dict = {
        "env": env_proxy, "env_key": env_key, "alive": None,
        "candidates": [], "advice": "", "tone": "idle",
    }
    if env_proxy:
        alive = verify_proxy(env_proxy) if deep else None
        p_info["alive"] = alive
        if alive:
            # 系统已经配好且能用 —— 沿用，不做任何修改
            p_info["advice"] = f"已沿用系统代理（{env_key}）"
            p_info["tone"] = "ok"
            say("ok", f"[代理] 沿用 {env_key} = {env_proxy}（连通）")
        elif alive is False:
            say("warn", f"[代理] {env_proxy} 当前连不通，正在探测本机其它代理…")
            cands = sniff_proxies(deep=deep, log=log)
            p_info["candidates"] = cands
            best = next((c for c in cands if c.get("verified")), None)
            if best:
                p_info["advice"] = (f"当前代理连不通；本机检测到可用代理 {best['url']}，"
                                    f"请到「tdl 部署助手」里更改")
                p_info["tone"] = "warn"
            else:
                p_info["advice"] = "当前代理连不通，且未探测到其它可用代理；请检查代理软件"
                p_info["tone"] = "bad"
    else:
        say("warn", "[代理] 系统未配置 TDL_PROXY，正在探测本机代理…")
        cands = sniff_proxies(deep=deep, log=log)
        p_info["candidates"] = cands
        best = cands[0] if cands else None
        if best:
            p_info["advice"] = (f"系统未配置代理；检测到可用代理 {best['url']}，"
                                f"请到「tdl 部署助手」里配置后使用")
            p_info["tone"] = "warn"
        else:
            p_info["advice"] = "系统未配置代理，也没探测到本机代理；Telegram 直连不可达，请先启动代理软件"
            p_info["tone"] = "bad"

    ns = namespaces()
    return {
        "tdl": {"ok": bool(exe and os.path.isfile(exe)), "path": exe, "version": ver},
        "ffmpeg": {"ok": bool(ff_path), "path": ff_path, "source": ff_src},
        "proxy": p_info,
        "login": {"ok": bool(ns), "namespaces": ns},
    }
