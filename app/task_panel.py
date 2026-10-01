# -*- coding: utf-8 -*-
"""
任务进度面板：解析 tdl 的 go-pretty 进度输出行，按 tracker 逐任务显示。

tdl 进度行结构（StyleDefault，非 TTY 管道输出，约 100ms 一轮全量刷新）：

    <message>... [<bar>] [<value> in <elapsed>; <rate>] [done!|failed!]

    · download/up : message = 群名(ID):消息ID -> 文件路径，value = 12.5MiB（二进制字节，
                    tracker 带 total → 行内可能还有 "45.2%" 百分比段）
    · chat export : message = 群名-群号，value = 96（条数，无单位 → 不计字节）
    · bar         : [..<#>...]（长度 = 终端宽度/5）

面板行为：
    · 每个 tracker（message 去重）占一行：任务 / 进度 / 速度 / 已耗时 / 预计结束
    · begin(reset) 开始新命令；finish(ok) 结束着色并返回本次字节合计（供统计）
    · 仅字节型 value（带 KiB/MiB/... 单位）计入字节合计；条数型不计
"""
from __future__ import annotations

import os
import re
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QProgressBar, QScrollArea, QToolButton, QMenu,
)

from .theme import T
from .widgets import hand_cursor

# ---- 解析：尾部 "<value> in <elapsed>; <rate>" ----
_RE_TAIL = re.compile(
    r"\[(?P<val>[^\[\]]+) in (?P<el>[^\[\];]+); (?P<rate>[^\[\]]+)\]\s*$")
# ---- 进度条：[..<#>...] ----
_RE_BAR = re.compile(r"\[(?P<bar>[. #<>=_\-]*)\]")
# ---- 显式百分比（下载行实测输出 "0.0%"，在 bar 之前）----
_RE_PCT = re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")
# ---- 结束标记（渲染在 message 与 bar 之间）----
_RE_TAG = re.compile(r"\s*(done!|failed!)\s*")
# ---- 字节值："65.41 KB" / "12.5MiB"（无单位 = 条数，不计字节）----
_RE_BYTES = re.compile(r"^(?P<n>[\d.]+)\s*(?P<u>B|[KMGT]i?B)$", re.IGNORECASE)
# ---- 速率："17.42 KB/s" / "3.9MiB/s" / "42/s" ----
_RE_RATE = re.compile(r"^(?P<n>[\d.]+)\s*(?P<u>B|[KMGT]i?B)?/s$", re.IGNORECASE)
_RE_DUR = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?$")
_UNIT = {
    "B": 1,
    # 二进制（KiB/MiB…）
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
    # tdl 的 utils.Byte.FormatBinaryBytes（pkg/utils/byte.go）用 1024 进制换算
    # 但标签写的是 KB/MB/GB/TB —— 实测 done! [118.41 KB] 对应 121251 B（×1024），
    # 按 1000 算会差 2.4%，与 tdl 的 FormatBinaryBytes 对不上
    "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
}

# ---- ANSI 控制序列：tdl 在非 TTY 下仍输出光标上移/清行（\x1b[A \x1b[K），
#      不剥离会导致同一任务因前缀不同被判成多行，也会污染日志 ----
_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# ---- tdl 高频噪声行：资源状态行、汇总进度条行（无任务信息）----
_RE_NOISE_STAT = re.compile(r"^CPU: [\d.]+% Memory: [\d.]+ ?[KMG]?i?B Goroutines: \d+$")
_RE_NOISE_BAR = re.compile(r"^\[[. #<>=_\-]*\] \[[\d.]+(?:ms|s)\]$")
# ---- 任务名里的群组号 / 消息号：`群名(1234567890):1238~` 或 `群名-1234567890` ----
_RE_GID = re.compile(r"\((?P<id>\d+)\)")
_RE_GID_DASH = re.compile(r"-(?P<id>\d{5,})\s*$")
_RE_MID = re.compile(r":\s*(?P<id>\d+)")
# ---- 速度段清洗：tdl 的 rate 可能带 "~ETA:: 2s:" 之类前缀 ----
_RE_RATE_FIND = re.compile(r"[\d.]+\s*(?:B|[KMGT]i?B)?/s", re.IGNORECASE)


def clean_rate(text: str) -> str:
    """从 rate 段提取纯速度文本（如 "371.30 KB/s"），剥掉 "~ETA:: 2s:" 前缀。"""
    m = _RE_RATE_FIND.search(text)
    return m.group(0) if m else text.strip()


def strip_ansi(text: str) -> str:
    """剥离 ANSI 转义序列（tdl 重绘控制符）。"""
    return _RE_ANSI.sub("", text)


def is_noise_line(line: str) -> bool:
    """tdl 的资源状态行 / 汇总进度条行：无任务信息，不进日志也不进面板。"""
    t = strip_ansi(line).strip()
    if not t:
        return True
    # 廉价预筛：两类噪声行要么含 "["，要么以 "CPU:" 开头
    if "[" not in t and not t.startswith("CPU:"):
        return False
    return bool(_RE_NOISE_STAT.match(t) or _RE_NOISE_BAR.match(t))


def is_round_marker(line: str) -> bool:
    """这一行是不是「本轮渲染结束」标记（CPU/Memory/Goroutines 状态行）。

    tdl 每渲染一轮进度，最后都补一行资源状态。调用方用它当**轮次分隔符**：
    重置「槽位计数器」，从而给本轮内的 bar 行按顺序编号。
    槽位是 message 被终端宽度截断（消息号丢失）时唯一稳定的任务身份。
    """
    return bool(_RE_NOISE_STAT.match(strip_ansi(line).strip()))


def _parse_bytes(text: str) -> int | None:
    """"12.5MiB" → 字节数；"96"（无单位，条数）→ None。"""
    m = _RE_BYTES.match(text.strip())
    if not m:
        return None
    n = float(m.group("n"))
    u = (m.group("u") or "B").upper()
    return int(n * _UNIT.get(u, 1))


def parse_duration(text: str) -> float:
    """"1m40.5s" / "3.2s" / "2h3m" / "201ms" → 秒。"""
    m = _RE_DUR.match(text.strip())
    if not m:
        return 0.0
    h = float(m.group("h") or 0)
    mi = float(m.group("m") or 0)
    s = float(m.group("s") or 0)
    ms = float(m.group("ms") or 0)
    return h * 3600 + mi * 60 + s + ms / 1000


def fmt_duration(sec: float) -> str:
    """秒 → 紧凑显示。"""
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{sec:.1f}s" if sec < 10 else f"{sec:.0f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def parse_progress_line(line: str) -> dict | None:
    """解析 tdl 进度行；非进度行返回 None（照常进日志）。"""
    s = strip_ansi(line).strip()          # 先剥 ANSI，保证同一任务名稳定
    # 廉价预筛：进度行必有 "["..."]" 段，普通日志行直接跳过（省掉后续全部正则）
    if not s or len(s) < 8 or "[" not in s or "]" not in s:
        return None
    m = _RE_TAIL.search(s)
    if not m:
        return None
    head = s[: m.start()].strip()

    # 进度条：取 head 中最后一个 bar 块；done/failed 定格行可能没有 bar
    bars = list(_RE_BAR.finditer(head))
    is_final = bool(_RE_TAG.search(head))
    if bars:
        last = bars[-1]
        bar = last.group("bar")
        head_wo = (head[: last.start()] + " " + head[last.end():]).strip()
    elif is_final:
        bar = ""
        head_wo = _RE_TAG.sub(" ", head).strip()
    else:
        return None

    # 任务名：去标记/尾部省略号/百分比残留（messageWidth 截断产生的 padding）
    name = _RE_TAG.sub(" ", head_wo)
    mp0 = _RE_PCT.search(name)
    if mp0:
        name = _RE_PCT.sub(" ", name)
    name = name.replace("...", " ")
    name = re.sub(r"\s+", " ", name).strip(" -.·")
    # 兜底：解码残渣（半个汉字被替换成 �）不参与任务标识，
    # 否则同一任务会因每次残渣位置不同而生成多张卡片
    name = name.replace("\ufffd", "").strip()

    # 百分比：优先显式（tdl 实测输出 "0.0%"）；done 定格 = 100%；否则按 bar 估算
    pct = None
    if mp0:
        pct = float(mp0.group("pct"))
    elif is_final and "done!" in head:
        pct = 100.0
    elif "#" in bar and len(bar) > 0:
        idx = bar.index("#")
        n = bar.count("#")
        pct = round((idx + n) / len(bar) * 100, 1)

    # 运行状态：失败 / 完成 / 进行中（同一任务行内更新）
    if "failed!" in head:
        state = "failed"
    elif "done!" in head:
        state = "done"
    else:
        state = "running"

    # 群组号 + 消息号：`群名(1234567890):1238~`
    gid = mid = gname = ""
    g = _RE_GID.search(name) or _RE_GID_DASH.search(name)
    if g:
        gid = g.group("id")
        gname = name[:g.start()].strip(" -·")      # 括号前的群名（tdl 可能已截断）
        tail = name[g.end():]
        m2 = _RE_MID.search(tail)
        if m2:
            mid = m2.group("id")

    return {
        "raw": name or "任务",              # 稳定标识（去重 key）
        "msg": (name or "任务")[:120],
        "gid": gid,
        "gname": gname,
        "mid": mid,
        "state": state,
        "val": m.group("val").strip(),
        "rate": clean_rate(m.group("rate")),
        "elapsed": parse_duration(m.group("el")),
        "pct": pct,
        "bytes": _parse_bytes(m.group("val")),
    }


def fmt_bytes(n: float) -> str:
    """字节数 → 二进制单位字符串（与 tdl 的 FormatBinaryBytes 同口径）。"""
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.1f}{u}" if u != "B" else f"{n:.0f}B"
        n /= 1024
    return f"{n:.1f}TiB"


def parse_rate(text: str) -> float:
    """"3.9MiB/s" → B/s；"42/s"（条数/秒）→ 0（不计速率）。"""
    m = _RE_RATE.match(text.strip())
    if not m or not m.group("u"):
        return 0.0
    return float(m.group("n")) * _UNIT.get(m.group("u").upper(), 1)


# ---- 卡片配色（对齐深色主题）----
def _tint(hex_color: str, alpha: int) -> str:
    """#RRGGBB + alpha(0-255) → rgba()。

    ⚠️ Qt QSS 的 8 位 hex 是 #AARRGGBB（alpha 在前），按 CSS 习惯写 #RRGGBBAA
    会被解析成完全不同的颜色（绿色变橙色），统一用 rgba 规避。
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha / 255:.2f})"


KIND_META = {
    "dl": ("下载", "#0969da"),
    "up": ("上传", "#1a7f37"),
    "forward": ("转发", "#9a6700"),
    "export": ("导出", "#8250df"),
}
# fg=徽章/文字色（白底深色变体，保证可读）；bar=进度条颜色
_STATE_META = {
    "running": ("进行中", "#0969da", "#2a8cf0"),
    "done": ("已完成", "#1a7f37", "#2da44e"),
    "failed": ("失败", "#cf222e", "#cf222e"),
    "stopped": ("已终止", "#9a6700", "#d29922"),
}
_BORDER_BY_STATE = {
    "running": "#2a8cf055",
    "done": "#3fb95044",
    "failed": "#ff7b7255",
    "stopped": "#d2992244",
}


def fmt_bytes_dec(n: float) -> str:
    """十进制单位（与 tdl 输出同口径：KB / MB…）。"""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_duration_cn(sec: float) -> str:
    """中文时长：17 秒 / 2 分 15 秒 / 1 时 02 分。"""
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"{sec:.0f} 秒"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m} 分 {s:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 时 {m:02d} 分"


class TaskCard(QFrame):
    """单任务卡片：类型徽标 + 标题 + 状态徽章 + 进度条 + 统计行 + 详情行。"""

    remove_requested = Signal()          # 右键菜单「移除这张卡」（任何状态都可移除）

    def __init__(self, kind: str, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setObjectName("TaskCard")
        self.kind = kind if kind in KIND_META else ""
        self.key = ""                           # 在 `_cards` 里的**当前**身份（会被换，见 _remove_clicked）
        self._state = "running"
        self.gname = ""                         # 所属群组名（状态栏显示用）
        self.gid = ""                           # 所属群组号
        self.mid = ""                           # 完整序号（还原后的消息号）
        self.fname = ""                         # 文件名（含扩展名，来自下载源 JSON）
        self._val = ""                          # 最后一次统计值（如 "3.20 MB"）
        self._compact = False                   # 是否已收成单行（终态卡片用）
        self._pending: tuple | None = None      # 最后一次进度数据（不可见时暂存）
        self._dirty = False                     # 有跳过渲染的更新待补
        self.finished_at: float | None = None     # 完成时刻（用于「只留最新 N 张已完成」）
        kn, kc = KIND_META.get(self.kind, ("任务", "#8b949e"))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # -- 头行：类型徽标 + 标题 + 状态徽章 --
        head = QHBoxLayout()
        head.setSpacing(8)
        tag = QLabel(kn)
        tag.setFixedHeight(22)
        tag.setAlignment(Qt.AlignCenter)
        tag.setStyleSheet(
            f"color:{kc}; background:{_tint(kc, 42)}; border:1px solid {_tint(kc, 105)};"
            f"border-radius:4px; padding:0 10px; font-size:12px; font-weight:600;")
        head.addWidget(tag)
        self._tag = tag

        # 标题不设 tooltip：详情行已展示完整任务名，悬浮窗只会遮挡统计行
        self._title = QLabel(title)
        self._title.setObjectName("CardTitle")
        self._title.setStyleSheet(
            "color:#1f2328; font-size:13px; font-weight:600; background:transparent;")
        head.addWidget(self._title, 1)

        self._badge = QLabel("进行中")
        self._badge.setFixedHeight(22)
        self._badge.setAlignment(Qt.AlignCenter)
        head.addWidget(self._badge)

        # ✕ 移除按钮：**不依赖右键**（用户实测右键不好用/找不到），任何状态都能删。
        # 卡片一旦卡在「进行中」，修剪与「清空已完成」都不碰它 —— 这里是唯一出口，
        # 所以做成一直可见（低调灰色，悬停变红），一眼能看到。
        self.btn_remove = QToolButton()
        self.btn_remove.setText("✕")
        self.btn_remove.setFixedSize(24, 24)      # 点得中（20×20 偏小）
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.setToolTip("移除这张卡（只是不显示它，不影响下载）")
        # ⚠️ 做成**明确的按钮外观**（浅底 + 边框 + 悬停红）：之前是淡灰小字，
        #    既不像可点、又只有 20px —— 用户反馈「点了完全没反应」，可点性是嫌疑之一。
        self.btn_remove.setStyleSheet(
            "QToolButton { color:#57606a; background:#f6f8fa;"
            " border:1px solid #d0d7de; border-radius:6px; font-size:13px; }"
            "QToolButton:hover { color:#ffffff; background:#cf222e;"
            " border-color:#cf222e; }")
        self.btn_remove.clicked.connect(lambda *_: self.remove_requested.emit())
        head.addWidget(self.btn_remove)
        lay.addLayout(head)

        # -- 进度条 --
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)              # 0.1% 精度
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        lay.addWidget(self._bar)

        # -- 统计行（富文本；未指定颜色的前缀词用本控件默认色 #57606a）--
        self._stats = QLabel("")
        self._stats.setTextFormat(Qt.RichText)
        self._stats.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._stats.setStyleSheet(
            "color:#57606a; font-size:12px; background:transparent;")
        lay.addWidget(self._stats)

        # -- 底行：详情（可选）。现在群号/序号/文件名都并到标题里了，通常不传 —
        #    传空字符串就不建这个控件，省掉一行高度 --
        self._detail = None
        if detail:
            self._detail = QLabel(detail)
            self._detail.setObjectName("CardDetail")
            self._detail.setStyleSheet(
                "color:#57606a; font-size:12px; background:transparent;")
            lay.addWidget(self._detail)

        self.set_state("running")
        self.update_progress(None, "0 B", "0 B/s", 0.0, "running")

    # ------------------------------------------------------------------
    def _apply_bar(self, color: str) -> None:
        self._bar.setStyleSheet(
            f"QProgressBar {{ background:#eaeef2; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{color}; border-radius:3px; }}")

    def contextMenuEvent(self, e) -> None:
        """右键卡片 → 移除这张卡。

        为什么必须有：卡片一旦卡在「进行中」（完成行丢了 / 身份错位），
        `_prune_finished` 与「清空已完成」都不会动它 —— 此前用户完全没办法清理，
        只能眼看着它堆积（用户实测反馈）。这里给一个**任何状态都能用**的出口。
        """
        m = QMenu(self)
        act = m.addAction("移除这张卡")
        act.triggered.connect(self.remove_requested.emit)
        m.exec(e.globalPos())

    def set_state(self, state: str) -> None:
        """状态徽章 / 边框 / 进度条颜色（仅在状态变化时调用）。"""
        self._state = state if state in _STATE_META else "running"
        if self._state != "running" and self.finished_at is None:
            self.finished_at = time.monotonic()   # 记录完成时刻（用于修剪旧卡片）
        label, fg, bar = _STATE_META[self._state]
        self._badge.setText(label)
        self._badge.setStyleSheet(
            f"color:{fg}; background:{_tint(fg, 38)}; border:none; border-radius:10px;"
            f"padding:0 10px; font-size:12px; font-weight:600;")
        self._apply_bar(bar)
        self.setStyleSheet(
            f"QFrame#TaskCard {{ background:#ffffff; border:1px solid {_tint(bar, 110)};"
            f"border-radius:8px; }}")

    def update_progress(self, pct, val: str, rate: str, elapsed: float,
                        state: str) -> None:
        """刷新进度条与统计行（高频调用，只动文本/数值，不动 QSS）。

        卡片不在显示中（不在「任务进度」页）时跳过 UI 渲染，只留数据统计，
        同时记下最后一次数据；切回页面时由 flush_pending() 补渲染，
        避免「任务在后台跑完、切回来统计行却空白」。
        """
        self._val = str(val or "").strip()      # 终态单行摘要要用（不受可见性影响）
        self._pending = (pct, val, rate, elapsed, state)
        if not self.isVisible():
            self._dirty = True
            return
        self._paint_progress(pct, val, rate, elapsed, state)

    def flush_pending(self) -> None:
        """若曾有跳过渲染的更新，此刻补上（页面重新可见时调用）。"""
        if self._dirty and self._pending is not None:
            self._dirty = False
            self._paint_progress(*self._pending)

    def set_compact(self, summary: str = "") -> None:
        """收成单行：只留「文件名  文件大小」。

        进行中的卡片要显示实时进度条与速度；任务结束后这些都没意义了，
        收成一行能把纵向空间让给**仍在跑**的任务（并发多时尤其明显）。
        文件名取卡片自身记录的 gid / mid / fname，按 `_` 连成与落盘文件名
        同构的三段式（见 _display_name）；大小不属于名字，用空格放在最后。
        """
        if self._compact:
            return
        self._compact = True
        name = "_".join(p for p in (self.gid, self.mid, self.fname) if p)
        txt = summary or "  ".join(p for p in (name, self._val) if p)
        if txt:
            self._title.setText(txt)
        # 终态样式：灰色小字，视觉上退到背景
        self._title.setStyleSheet(
            "color:#57606a; font-size:12px; font-weight:500; background:transparent;")
        for w in (self._tag, self._badge, self._bar, self._stats, self._detail):
            if w is not None:
                w.setVisible(False)             # 隐藏即释放布局占位，卡片自然缩成一行
        lay = self.layout()
        if lay is not None:                     # 收紧内边距，让它看着就是一行
            lay.setContentsMargins(12, 6, 12, 6)
            lay.setSpacing(0)

    def retitle(self, gid: str, mid: str, fname: str) -> None:
        """矫正身份：更新 gid/mid/fname 并重写标题。

        调用场景只有一处 —— commit_round ⓪ 的磁盘 `.tmp` 锚定：把第一轮按池序
        分派出来的卡**按位置搬到磁盘真值**上（不这么做旧身份的卡会留成僵尸卡）。
        完成时**不做任何身份变更**（用户要求：完成后沿用下载时的标题与大小）。
        running 状态重写三段式标题；已收单行（compact）重写「文件名  大小」摘要。
        """
        self.gid, self.mid, self.fname = gid, mid, fname
        name = "_".join(p for p in (gid, mid, fname) if p)
        if not name:
            return
        if self._compact:
            txt = "  ".join(p for p in (name, self._val) if p)
            if txt:
                self._title.setText(txt)
        else:
            self._title.setText(name)

    def _paint_progress(self, pct, val: str, rate: str, elapsed: float,
                        state: str) -> None:
        if self._compact:
            return                              # 已收成单行：不再刷进度细节
        self._bar.setValue(int(round(max(0.0, min(100.0, pct or 0.0)) * 10)))

        b = _parse_bytes(val)
        total_txt = ""
        if b is not None:
            if pct is not None and pct > 0.5:
                total_txt = f"（{val} / {fmt_bytes_dec(b / (pct / 100.0))}）"
            else:
                total_txt = f"（{val}）"
        p_txt = f"{pct:.1f}%" if pct is not None else "--"
        if state != "running":
            eta_txt = "—"
        elif pct is not None and pct > 2 and elapsed > 0:
            eta_txt = fmt_duration_cn(elapsed * (100 - pct) / pct)
        else:
            eta_txt = "--"
        self._stats.setText(
            f"进度 <b style='color:#1f2328'>{p_txt}</b>"
            f"<span style='color:#57606a'>{total_txt}</span>"
            f" &nbsp;·&nbsp; 速度 <b style='color:#0969da'>{rate}</b>"
            f" &nbsp;·&nbsp; 已用 <b style='color:#1f2328'>{fmt_duration_cn(elapsed)}</b>"
            f" &nbsp;·&nbsp; 预计剩余 <b style='color:#6e7781'>{eta_txt}</b>")

    @property
    def state(self) -> str:
        return self._state


class TaskPanel(QWidget):
    """卡片式任务列表：汇总行 + 卡片流（每条任务一张卡片）。

    对外接口与旧表格版一致（begin / update_task / finish / total_*），
    供 TaskBox / MainWindow 无感切换。
    """

    stop_all_requested = Signal()
    # 某张卡片被用户移除（✕ / 右键菜单）→ 主窗口记一行日志：
    # 既让用户**看得见点击生效了**，也是「点击到底有没有到达」的判据。
    card_removed = Signal(str)

    # 卡片总数上限（含进行中）—— 用户要求「任务进度的卡片渲染缩减至 10 个」（2.9.7）。
    # ⚠️ **进行中的卡片永不修剪**（见 _prune_finished，它只挑 state != "running" 的）：
    #    压掉的是历史遗留的**完成卡**，绝不能把正在跑的任务从界面上抹掉。
    #    因此并发数超过这个上限时，实际卡片数会**多于 10** —— 这是有意的。
    # ⚠️ 值也不能过小：tdl 边下边完成时，上限太小会让卡片被实时删到只剩几张，
    #    表现就是「明明 4 并发，页面只显示 2 个任务」，旧卡被删后新完成的补位还会
    #    看起来像「同一张卡的内容在不同任务间来回切换」。
    MAX_CARDS = 10
    MAX_FINISHED_CARDS = 10        # 已完成卡片的保留上限（与总数上限分开控制）
    # 进行中的卡片连续这么久没有任何进度行认领 → 视为已中断（见 _finalize_stale）
    STALE_SECONDS = 60.0

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # -- 汇总行：「共 N 个任务 · M 个进行中」+ 全部停止 / 清空已完成 --
        head = QHBoxLayout()
        head.setSpacing(8)
        self._summary = QLabel("共 0 个任务")
        self._summary.setObjectName("FieldHelp")
        head.addWidget(self._summary)
        head.addStretch(1)
        btn_stop = QToolButton()
        btn_stop.setText("全部停止")
        btn_stop.setToolTip("终止当前正在执行的 tdl 命令（tdl 不支持停止单个任务）")
        hand_cursor(btn_stop)
        btn_stop.setAutoRaise(True)
        btn_stop.clicked.connect(self.stop_all_requested.emit)
        head.addWidget(btn_stop)
        btn_clear = QToolButton()
        btn_clear.setText("清空已完成")
        btn_clear.setToolTip("移除已完成 / 已失败 / 已终止的卡片")
        hand_cursor(btn_clear)
        btn_clear.setAutoRaise(True)
        btn_clear.clicked.connect(self.clear_finished)
        head.addWidget(btn_clear)
        lay.addLayout(head)

        # -- 卡片滚动区 --
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        host = QWidget()
        host.setObjectName("CardHost")
        host.setStyleSheet("QWidget#CardHost { background:transparent; }")
        self._cards_lay = QVBoxLayout(host)
        self._cards_lay.setContentsMargins(0, 0, 0, 0)
        self._cards_lay.setSpacing(8)
        self._cards_lay.addStretch(1)
        self._scroll.setWidget(host)
        lay.addWidget(self._scroll, 1)

        self._cards: dict[str, TaskCard] = {}      # 任务身份 → 卡片
        self._bytes_by_key: dict[str, int] = {}
        self._rate_by_key: dict[str, float] = {}
        self._kind = ""
        self._seq = 0
        self._ok: bool | None = None
        self._status = ""                          # 运行状态前缀（就绪/进行中/完成…）
        self._running_count = 0                    # 进行中卡片数（增量维护，避免每次全遍历）
        self._pruned_keys: set[str] = set()        # 已修剪任务：忽略 tdl 后续重绘（防复活）
        # ⭐ 用户**手动删掉**的卡片身份（✕ 按钮 / 右键「移除这张卡」）。tdl 每轮还会把
        #    这个任务当活跃任务重画，`_touch_card` 找不到卡就会**就地新建** —— 必须
        #    靠这个集合压制，卡才是「真的删掉」而不是「删了又回来」。本轮命令结束
        #    （`begin()`）才清空。
        self._removed_keys: set[str] = set()
        self._unseen: dict[str, float] = {}         # 进行中卡片「本轮没被认领」的起始时刻

        # ---- 轮次级任务追踪（详见 push_row / commit_round 的说明）----
        self._live: list[list] = []                # 活跃任务身份，顺序 = 卡片顺序：[key, 已下载字节]
        self._round_active: list[dict] = []        # 本轮「进行中」行缓存
        self._round_done: list[dict] = []          # 本轮「已完成」行缓存
        self._uid = 0                              # 拿不到消息号时用的自增身份
        self._card_order: list[str] = []           # 当前卡片的排列顺序（按 id 排，避免重复重排）
        # 本次下载源 JSON 里的完整消息号列表（还原被截断的序号用）
        self._id_pool: list[str] = []
        self._mid_used: set[str] = set()
        self._file_map: dict[str, str] = {}        # 消息号 → 文件名（含扩展名）
        self._dl_dir = ""                          # 下载目录（.tmp 锚定 / 已下载号排除用）
        self._restart = False                      # --restart：tdl 重下全部（不做已存在排除）
        self._done_mids: set[str] = set()          # 磁盘上「已下载完成」的消息号（每轮刷新）
        # 本次运行累计进度回调 `fn(total_bytes, task_count)` —— 下载统计据此
        # **按实际发生的时间**分账（见 stats.add_progress）；None 时不做任何事。
        self._progress_hook = None          # 磁盘上「已下载完成」的消息号（每轮刷新）

    # ------------------------------------------------------------------
    def set_status(self, text: str) -> None:
        """设置运行状态前缀（显示在汇总行最前）。"""
        self._status = (text or "").strip()
        self._refresh_summary()
    def begin(self, kind: str = "") -> None:
        """新命令开始：清空卡片流；kind 决定卡片上的类型徽标。"""
        for card in list(self._cards.values()):
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._bytes_by_key.clear()
        self._rate_by_key.clear()
        self._kind = kind if kind in KIND_META else ""
        self._seq = 0
        self._ok = None
        self._running_count = 0
        self._pruned_keys.clear()
        self._removed_keys.clear()
        self._unseen.clear()
        self._live.clear()
        self._round_active.clear()
        self._round_done.clear()
        self._uid = 0
        self._mid_used.clear()
        self._done_mids = set()
        self._card_order = []
        self._summary.setText("共 0 个任务")

    # ------------------------------------------------------------------
    # 轮次级任务追踪
    #
    # tdl 用 go-pretty 渲染进度（不是 mpb）。实测 122 轮真实输出确认，每一轮刷新的
    # 输出固定是：PS 行（CPU/Memory/Goroutines）→ N 个「进行中」行 → 总进度行
    # → [本轮的「已完成」定格行]。据此：
    #   · 每一轮都把**全部活跃任务**输一遍，顺序 = tracker 创建顺序
    #   · 任务完成后从活跃列表移出，**剩余任务保持相对顺序**（于是位置整体前移）
    #   · 完成行在本轮末尾补一次
    #
    # ⚠️ 所以「本行在本轮里的位置」跨轮并不稳定（这正是上一版会错乱的根因）。
    # ⚠️ 而且 tdl 把 message 宽度写死为 30 字符（width=(100-50), messageWidth=width*3/5），
    #    群名超过 ~16 字符时 `(群号):消息号` 会被整段切掉 —— 消息号经常拿不到。
    #
    # 可靠的身份信号是**已下载字节数**：同一个任务只增不减，跨轮用它认领同一任务。
    # 能解析出「群号:消息号」时优先用它，被截断时靠字节数兜底。
    # ------------------------------------------------------------------

    def set_id_pool(self, ids, files=None, restart: bool = False) -> None:
        """登记本次下载源 JSON 里的**完整消息号**及各自的**文件名**。

        这两件事都只能从下载源里拿：
          · 完整序号 —— tdl 把进度行 message 截断到 30 字符，序号常只剩前几位
          · 文件名 —— 进度行里**完全没有文件名/扩展名**（用户要知道下的是哪个文件）
        `files` 是 {消息号: 文件名}；只传 ids 也能工作（退化为不显示文件名）。
        `restart`（--restart）时 tdl 会重下全部文件，此时**不能**排除磁盘上已存在的
        号（见 _scan_done_mids）—— 其余情况（--skip-same / 续传）tdl 都会跳过它们。
        """
        self._id_pool = [str(i).strip() for i in (ids or []) if str(i).strip()]
        self._file_map = {str(k).strip(): str(v).strip()
                          for k, v in (files or {}).items() if str(v).strip()}
        self._restart = bool(restart)

    def set_download_dir(self, d: str) -> None:
        """登记本次下载的落盘目录（-d/--dir 参数）—— 两处磁盘真值都靠它：

          · 下载卡身份：扫 `*.tmp`（`群号_消息号_文件名.tmp`，OnDone 才改名）
          · 池序分派排除：扫已有最终文件的号（tdl 的 --skip-same / 续传会跳过）
        """
        self._dl_dir = str(d or "").strip()

    def _file_of(self, mid) -> str:
        """消息号对应的文件名（含扩展名，如 `IMG_1542.MOV`）；取不到返回空串。"""
        return self._file_map.get(str(mid or "").strip(), "")

    def _pick_full_id(self, mid: str) -> str:
        """把被截断的序号还原成完整消息号；实在还原不了就原样返回（不编造）。

        顺序 = **池顺序**（池已由 paths.sort_id_pool 排成 tdl 的实际下载顺序），
        取第一个「未被占用 **且磁盘上还没有最终文件**」的号：
          · 完整序号（≥5 位且在池中）→ 直接用它（tdl 用的就是这个号）
          · 前缀命中 → 按池顺序取第一个可用
          · 前缀没线索（连前几位都被切掉）→ 同样按池顺序取
        ⚠️ 必须跳过「磁盘上已经下载完成」的号：tdl 的 --skip-same（GUI 默认开启）
        与 --continue 续传都会跳过它们 —— 若不跳，第 1 个任务就会拿到一个已下载过的
        号，之后**每个任务都提前一位**（用户实测：卡片 95313、实际在下的 95314，
        因为 95313 早就下好了）。--restart 时 tdl 会重下全部，不排除。
        还原不成功就返回原值，宁可显示不全也不显示错的。
        """
        mid = str(mid or "").strip()
        if not self._id_pool:
            return mid
        skip = self._done_mids if not self._restart else set()

        def usable(i: str) -> bool:
            return i not in self._mid_used and i not in skip

        if mid and len(mid) >= 5 and mid in self._id_pool:
            self._mid_used.add(mid)
            return mid                              # 本来就是完整的
        if mid:
            for i in self._id_pool:                 # 池顺序 = tdl 的产出顺序
                if i.startswith(mid) and usable(i):
                    self._mid_used.add(i)
                    return i
        for i in self._id_pool:
            if usable(i):
                self._mid_used.add(i)
                return i
        return mid                                  # 池已用尽 / 对不上 → 原样

    def _scan_done_mids(self) -> set:
        """下载目录里**已经下载完成**（最终名已落盘、非 .tmp）的消息号。

        池序分派（回退路径）要跳过这些号：tdl 的 --skip-same（GUI 默认开启）
        与 --continue 续传都会跳过它们。磁盘上「最终名」= OnDone 已改名，
        对应 tdl 的 `--skip-same` 检查（同名同大小）与 resume 的已完成记录。
        """
        if not self._dl_dir:
            return set()
        out: set[str] = set()
        try:
            with os.scandir(self._dl_dir) as it:
                for e in it:
                    if not e.is_file() or e.name.endswith(".tmp"):
                        continue
                    m = re.match(r"^(\d+)_(\d+)_", e.name)
                    if m:
                        out.add(m.group(2))
        except OSError:
            return set()
        return out

    def push_row(self, info: dict) -> None:
        """收到一行进度：先按「进行中 / 已完成」分桶缓存，等本轮结束再统一认领身份。"""
        if (info.get("state") or "running") == "running":
            self._round_active.append(info)
        else:
            self._round_done.append(info)

    def _tmp_mids(self) -> list:
        """下载目录里**正在下载**的 .tmp 对应的消息号，按池顺序排列。

        tdl 下载时先把文件写成 `<群号>_<消息号>_<文件名>.tmp`（iter.go 的
        `tempExt`），OnDone 成功后才重命名为最终名（progress.go:donePost）。
        所以「此刻存在的 .tmp」就是 tdl 此刻真正在下的任务集合 —— 号是**完整的**，
        不掺任何池序猜测。按池顺序排列后与 tdl 的输出顺序（tracker 创建序）
        一一对应，行数吻合时可直接按位置锚定身份（见 commit_round ⓪）。

        三重过滤（缺一不可）：
          · **size > 0** —— iter.go 在下发任务前就 `os.Create` 了 tmp（elem 先进
            容量 10 的 channel 缓冲），磁盘上会有一批**还没开始下载的空 tmp**；
            它们不是活跃任务，混进来会把「未开始」的号锚定到正在下载的行上
            （实测：4 张卡对应 5 个 tmp，其中 1 个 0 字节）
          · **mtime 10 分钟内** —— 排除上次中断留下的残骸（有数据但不再增长）
          · 文件名前缀必须是 `群号_消息号_`
        没有下载目录（链接模式）时返回空列表 —— 调用方回退到池序分派。
        """
        if not self._dl_dir:
            return []
        now, ids = time.time(), set()
        try:
            with os.scandir(self._dl_dir) as it:
                for e in it:
                    if not e.is_file() or not e.name.endswith(".tmp"):
                        continue
                    m = re.match(r"^(\d+)_(\d+)_", e.name)
                    if not m:
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0:
                        continue                # 预创建、还没开始下载
                    if now - st.st_mtime > 600:
                        continue                # 残骸（上次中断留下的）
                    ids.add(m.group(2))
        except OSError:
            return []
        if not ids:
            return []
        if self._id_pool:                           # 按池顺序 = tdl 的产出顺序
            ordered = [i for i in self._id_pool if i in ids]
            return ordered + sorted(ids - set(ordered), key=int)
        return sorted(ids, key=int)

    def commit_round(self) -> None:
        """一轮渲染结束：把本轮的活跃行映射到稳定身份，然后刷新卡片。"""
        active, done = self._round_active, self._round_done
        if not active and not done:
            return
        self._round_active, self._round_done = [], []
        # 磁盘真值：已下载完成（会被 tdl 的 --skip-same / 续传跳过）的号 ——
        # 池序分派要绕过它们，否则每个任务都会提前一位（见 _pick_full_id）
        self._done_mids = self._scan_done_mids()

        live = self._live                 # [[key, 已下载字节], ...]，顺序 = 卡片顺序
        claimed: set[str] = set()

        # ---- ① 活跃行认领身份 ----
        # ⓪ 磁盘锚定（最高优先级，下载卡专用）：下载中的文件在磁盘上就是
        #   `<群号>_<消息号>_<文件名>.tmp`（tdl 的 OnDone 才改名）。此刻磁盘上
        #   存在的 .tmp 集合 = tdl 此刻真正在下的任务集合 —— 号完整、零猜测，
        #   比池序/字节启发式都硬。行数吻合时按位置锚定：第 i 行 ↔ 池序第 i 个
        #   活跃 .tmp（池已按 tdl 的 sortDialogs 顺序排好）。
        #   完成卡则**直接沿用这里定下的身份**（用户要求：完成后不做检测）。
        disk_mids = self._tmp_mids()
        anchored = False
        if disk_mids and len(disk_mids) == len(active):
            gid = next((str(a.get("gid") or "").strip() for a in active
                        if str(a.get("gid") or "").strip()), "")
            if gid:
                old_keys = [x[0] for x in live]
                new_keys = [f"{gid}:{m}" for m in disk_mids]
                # ⚠️ 旧卡必须**按位置搬到新身份上（复用同一张卡片对象）**，不能只
                # 重写 live：否则旧身份那张卡会留在 _cards 里，变成永远「进行中」
                # 的僵尸卡 —— 实测表现就是序号「提前一位」且混进了已下载过的号
                # （第一轮 tmp 还没落盘时按池序建了卡，后续锚定又另外建了新卡）。
                # ⚠️ 只搬**公共前缀**（min 长度）：两边长度不等时（本轮多了一个任务 /
                #    完成行没定位到）也绝不能整列错位 —— 多出来的新键靠 `_touch_card`
                #    新建，多出来的旧键其卡片已不属于活跃集，交给 `_finalize_stale` 定格。
                nn = min(len(old_keys), len(new_keys))
                moved = [self._cards.pop(k, None) for k in old_keys[:nn]]
                for k in new_keys:
                    # ⚠️ 必须**连控件一起收掉**：只摘字典项的话，控件还挂在布局里，
                    #    就成了 `_cards` 里不存在的**幽灵卡** —— 点 ✕ 无反应、
                    #    `_prune_finished` 与「清空已完成」都遍历不到它，永远删不掉
                    #    （用户实测「任务卡依然无法删除」的成因之一）。
                    self._drop_card_widget(self._cards.pop(k, None))
                for k in old_keys:                    # 旧键的统计一并清掉（下轮重建）
                    if k in self._removed_keys:
                        continue                      # 用户删掉的卡：保留其已下载量（属事实）
                    self._bytes_by_key.pop(k, None)
                    self._rate_by_key.pop(k, None)
                    self._pruned_keys.discard(k)
                live[:] = [[k, 0] for k in new_keys]
                for i, k in enumerate(new_keys):
                    self._mid_used.add(disk_mids[i])
                    if k not in self._removed_keys:
                        self._pruned_keys.discard(k)
                    card = moved[i] if i < nn else None
                    if card is not None:
                        self._cards[k] = card
                        card.key = k        # ⚠️ 身份变了 → 回调要按新身份删（见 _remove_clicked）
                        card.retitle(gid, disk_mids[i], self._file_of(disk_mids[i]))
                for i, a in enumerate(active):
                    self._touch_card(live[i][0], a)
                    live[i][1] = a.get("bytes") or 0
                    claimed.add(live[i][0])
                anchored = True

        # 回退：tdl 每轮按 tracker 创建顺序输出全部活跃任务，而 tracker 创建顺序 =
        # 下载源产出顺序（iter.go：elem 逐个 FIFO 产出）。行数与活跃卡片数
        # 吻合时直接按位置一一对应 —— 这是零猜测的强对齐；行数对不上（丢行/
        # 解析失败/没有下载目录）才退回「不倒退 + 进度最接近」的启发式。
        if not anchored:
            strict = len(active) == len(live)
            for i, a in enumerate(active):
                ab = a.get("bytes") or 0
                key = None
                if strict and i < len(live):
                    key = live[i][0]
                # 启发式：首选同位置（要求进度不倒退）
                if key is None and i < len(live) and live[i][0] not in claimed \
                        and (live[i][1] or 0) <= ab:
                    key = live[i][0]
                # 位置对不上（前面有任务完成了）→ 在未认领且不倒退的候选里挑进度最接近的
                if key is None:
                    cands = [(x[0], x[1] or 0) for x in live
                             if x[0] not in claimed and (x[1] or 0) <= ab]
                    if cands:
                        key = max(cands, key=lambda t: t[1])[0]
                # 都找不到 → 确实是新任务
                if key is None:
                    key = self._new_key(a)
                    live.append([key, 0])
                claimed.add(key)
                self._touch_card(key, a)
                for x in live:
                    if x[0] == key:
                        x[1] = ab
                        break

        # ---- ② 完成行：把对应任务从活跃列表摘掉 ----
        # ⚠️ 只**定位**是哪一项，绝不改身份/标题：身份与文件名在下载阶段已经由
        # 磁盘 .tmp 锚定 / 池序分派定下来（可靠），完成时沿用即可。历史教训：
        # 按「完成行字节 == 文件大小」做磁盘反查会撞上同大小的别的文件，把本来
        # 正确的标题改成错的（用户明确要求：完成后不检测，直接沿用下载时的标题与大小）。
        for d in done:
            idx = self._pick_done(live, d)
            if idx is None:
                continue
            key = live[idx][0]
            claimed.discard(key)
            self._finish_card(key, d)
            live.pop(idx)

        # ⚠️ 每轮结束都要压一次上限：只挂在「有任务完成」的分支里是不够的 ——
        # 某一轮如果只新增任务、没有完成的，卡片数会一路涨过上限。
        # 卡住的进行中卡片先定格成「已终止」（否则它既不会被修剪也清不掉）
        self._finalize_stale(claimed)
        # _prune_finished 只动「已完成」的卡片，对进行中的无影响，无条件调用是安全的。
        self._prune_finished()
        # 兜底：不在 `_cards` 里的卡片控件一律收掉（漏过一次就会留点不掉的幽灵卡）
        self._sweep_orphan_widgets()
        self._reorder_cards()
        # 上报本次运行累计进度 → 下载统计按**实际发生的时间**分账（跨零点分记两天）
        self._emit_progress()

    def _reorder_cards(self) -> None:
        """按消息 id 升序排列卡片（id 拿不到的排在最后）。

        卡片默认按 tdl 的输出顺序排；但加了 `--desc` 就是倒序，
        跟「看第几条」的直觉相反。id 是稳定且唯一的，按它排更符合预期。
        顺序没变时直接返回，避免每轮都白做一次布局重算。
        """
        def sort_key(item):
            _, card = item
            mid = (card.mid or "").strip()
            return (0, int(mid), "") if mid.isdigit() else (1, 0, mid)

        items = sorted(self._cards.items(), key=sort_key)
        order = [k for k, _ in items]
        if order == self._card_order:
            return                              # 顺序没变，不动
        self._card_order = order
        for i, (_, card) in enumerate(items):
            self._cards_lay.insertWidget(i, card)

    def update_task(self, info: dict) -> None:
        """单行便捷入口（兼容旧调用）：内部直接当作一轮提交。"""
        self.push_row(info)
        self.commit_round()

    def _new_key(self, info: dict) -> str:
        """给一个还没见过的任务分配身份。

        身份在**创建时一次性定死**（后续轮次复用），所以这里就把被 tdl 截断的
        序号还原成完整消息号 —— 这样卡片上显示的序号是完整的，且不会每轮变化。
        """
        gid = str(info.get("gid") or "").strip()
        mid = self._pick_full_id(info.get("mid"))
        if gid and mid:
            k = f"{gid}:{mid}"
            if k in self._removed_keys:
                return k          # 用户删过：沿用真实身份（`_touch_card` 不会再建卡）
            if k not in self._cards and k not in self._pruned_keys:
                return k
        self._uid += 1
        return f"{gid or 'task'}#{self._uid}"

    def _pick_done(self, live: list, d: dict) -> int | None:
        """找出「完成行」对应活跃列表里的哪一项 —— **只定位，不改身份**。

        ① 按「群号:消息号」精确匹配：磁盘 .tmp 锚定后 key 就是完整真实号，
           完成行能给出完整号时直接命中（最可靠）。
        ② 完成行的序号可能被 tdl 截断（只剩前几位）→ 允许前缀匹配。
           前缀命中多张时不能拿第一张 —— 拿错会把本次完成的字节数写到别的任务
           头上，而真正完成的那张卡留在活跃列表里变成「僵尸」，后续轮次的进度行
           又会认领到它上面，形成连锁张冠李戴（实测表现：118 KB 的 jpg 卡片显示
           下载了 419 MB）。完成行的字节数是该任务的总大小，必然不小于它最后
           上报的进度值 —— 多个候选里挑「已下载字节最大且不超过完成值」的那张。
        ③ 序号完全没线索时，退化为对整个活跃列表按同样的字节规则兜底。
        """
        gid = str(d.get("gid") or "").strip()
        mid = str(d.get("mid") or "").strip()
        if gid and mid:
            want = f"{gid}:{mid}"
            for i, x in enumerate(live):
                if x[0] == want:
                    return i                    # 完整序号直接命中：最可靠
            cands = [i for i, x in enumerate(live) if x[0].startswith(want)]
            if len(cands) == 1:
                return cands[0]                 # 前缀只命中一张：也没歧义
            if len(cands) > 1:
                db = d.get("bytes") or 0
                best, best_v = None, -1
                for i in cands:
                    v = live[i][1] or 0
                    if v <= db and v > best_v:
                        best, best_v = i, v
                return best if best is not None else cands[0]
        # ⚠️ 完成行指的是「用户已经删掉的那张卡」时**直接丢弃**：这张卡可能已经不
        #    在 live 里了（它的 .tmp 被改名 → 本轮磁盘锚定重建 live 时就没有它），
        #    再往下走字节兜底会把这次完成**认领到别的卡上**（把无关的卡标成已完成）。
        if gid and mid:
            if any(k.partition(":")[0] == gid and k.partition(":")[2].startswith(mid)
                   for k in self._removed_keys):
                return None
        db = d.get("bytes") or 0
        best, best_v = None, -1
        for i, x in enumerate(live):
            v = x[1] or 0
            if v <= db and v > best_v:
                best, best_v = i, v
        return best

    def _touch_card(self, key: str, info: dict) -> None:
        """按身份更新（不存在则新建）卡片。

        ⚠️ 用户手动删掉的卡（`_removed_keys`）**绝不重建** —— tdl 下一秒还会把这个
        任务当活跃任务重画，重建就等于「删不掉」。
        """
        card = self._cards.get(key)
        if card is None and key in self._removed_keys:
            # 卡不建，但已下载量仍要计入统计（已下完的数据是事实）
            if info.get("bytes") is not None:
                self._bytes_by_key[key] = int(info["bytes"])
                self._refresh_summary()
            return
        if card is None:
            self._seq += 1
            # 详情行：只显示「群号:消息号」（与 tdl 终端输出对应），不显示群名
            card = TaskCard(self._kind, self._display_name(info, self._seq, key), "")
            # 完整信息（含群名与 tdl 的原始行）收进悬浮提示，需要时再看
            card.setToolTip(f"群名：{info.get('gname') or '—'}\n"
                            f"tdl 原始输出：{info.get('raw') or '—'}")
            self._cards[key] = card
            card.key = key
            # ⚠️ 回调**不要**把 key 闭包进来：卡片会被磁盘锚定换身份（见 commit_round ⓪），
            #    闭包里的旧 key 那时已不在 `_cards` 里 → 点 ✕ 静默无效；旧 key 若被别的卡
            #    接手，还会**删错卡**（用户实测「任务卡依然无法删除」的主因）。
            #    改成点击时按卡片**当前**身份解析（_remove_clicked）。
            card.remove_requested.connect(lambda _=False, c=card: self._remove_clicked(c))
            self._cards_lay.insertWidget(self._cards_lay.count() - 1, card)
            if card.state == "running":
                self._running_count += 1
        state = info.get("state") or "running"
        if info.get("gname"):
            card.gname = info["gname"]       # 供状态栏显示「正在下载：<群名>」
        if info.get("gid"):
            card.gid = info["gid"]
        # 身份里带完整序号时同步给卡片（终态收成单行时要用「群号_序号_文件名」）
        if ":" in key and "#" not in key:
            g, _, m = key.partition(":")
            card.gid = g
            card.mid = m
            card.fname = self._file_of(m)
        card.update_progress(info.get("pct"), info.get("val"), info.get("rate"),
                             info.get("elapsed"), state)
        if state != card.state:
            if card.state == "running":
                self._running_count -= 1
            elif state == "running":
                self._running_count += 1
            card.set_state(state)
        if info.get("bytes") is not None:
            self._bytes_by_key[key] = int(info["bytes"])
        # 进行中的任务：记录**实时速率**（状态栏网速就是它们之和）
        self._rate_by_key[key] = parse_rate(info.get("rate"))
        self._refresh_summary()

    def _finish_card(self, key: str, info: dict) -> None:
        """把卡片定格为已完成：**直接沿用下载时定下的标题**，只补最终大小。

        身份（gid/mid/fname）一律用卡片自己记录的 —— 它们在下载阶段就由磁盘
        `.tmp` 锚定 / 池序分派定下来，已经是完整且正确的；完成时**不做任何磁盘
        检测**（用户明确要求）。历史教训：按「完成行字节 == 文件大小」反推身份
        会撞上同大小的别的文件，把本来正确的标题改错。
        大小取完成行的最终值（`_val`），收成一行「标题  大小」，把纵向空间让给
        仍在跑的任务。
        """
        card = self._cards.get(key)
        if card is None:
            return
        if card.state == "running":
            self._running_count -= 1
        pct = info.get("pct")
        card.update_progress(100.0 if pct is None else pct,
                             info.get("val"), info.get("rate"),
                             info.get("elapsed"), "done")
        card.set_state("done")
        card.set_compact()                      # 用卡片既有 gid/mid/fname + 最终大小
        if info.get("bytes") is not None:
            self._bytes_by_key[key] = int(info["bytes"])
        # ⚠️ 完成的任务不再有「实时速率」—— 必须从速率表移除。原先这里把完成行报出的
        #    平均速率写了进去、且从不清理，状态栏网速会把所有历史任务一路累加
        #    （用户实测「长时间运行会累计」）。下载量（bytes）则要保留，用于统计。
        self._rate_by_key.pop(key, None)
        self._refresh_summary()

    def finish(self, ok: bool) -> dict:
        """命令结束：仍在「进行中」的卡片定格（成功=已完成 / 失败=已终止）。"""
        self.commit_round()                  # 收尾：提交最后一批还没到轮次分隔行的进度行
        self._ok = ok
        count = self._seq                    # 历史处理过的任务总数（含已被自动修剪的）
        for card in self._cards.values():
            if card.state == "running":
                card.set_state("done" if ok else "stopped")
                self._running_count -= 1
            card.set_compact()               # 命令已结束：全部收成单行（幂等）
        self._rate_by_key.clear()            # 命令已结束 → 没有实时速率了（网速归零）
        self._unseen.clear()
        self._prune_finished()
        self._refresh_summary()
        return {"bytes": sum(self._bytes_by_key.values()),
                "count": count}

    def _prune_finished(self) -> None:
        """把卡片数压到上限内：总数 ≤ MAX_CARDS，已完成 ≤ MAX_FINISHED_CARDS。

        两条约束一起生效（谁都需要让位就按大的那个让）：
          · **进行中的卡片永不删** —— 它们反映实时进度，删了就看不到在跑什么
          · 超出的部分从「已完成」里按完成时间**由旧到新**移除
        只移除卡片 UI；bytes/rate 数据保留到 begin()（命令结束的统计仍完整）。
        """
        finished = sorted((c.finished_at or 0.0, k) for k, c in self._cards.items()
                          if c.state != "running")
        overflow = max(len(finished) - self.MAX_FINISHED_CARDS,
                       len(self._cards) - self.MAX_CARDS)
        for _, key in finished[:max(0, overflow)]:
            card = self._cards.pop(key)
            card.setParent(None)
            card.deleteLater()
            self._pruned_keys.add(key)
            self._rate_by_key.pop(key, None)     # 卡片没了就别再往网速里加

    def _remove_clicked(self, card) -> None:
        """✕ / 右键「移除这张卡」的**唯一入口**：按卡片**当前**身份删。

        卡片对象会在磁盘 `.tmp` 锚定时被搬到新身份上（`_cards[new_keys[i]] = moved[i]`），
        所以「建卡时闭包进来的 key」和「此刻 `_cards` 里的 key」可能已经不是同一个 ——
        拿旧 key 调 `remove_card` 会静默失败，甚至删到接手了那个 key 的另一张卡。
        这里改成点击时反查：
          · 先用卡片自己记的 `key`（O(1)）；
          · 对不上就按**对象**反查 `_cards`（键上限 20，代价可忽略）；
          · 都不在（卡片已被清掉）→ 至少把控件收掉，别在界面上留删不掉的幽灵卡。
        """
        key = getattr(card, "key", "")
        if key and self._cards.get(key) is card:
            self.remove_card(key)
            return
        for k, c in self._cards.items():
            if c is card:
                self.remove_card(k)
                return
        self._drop_card_widget(card)

    def _drop_card_widget(self, card) -> None:
        """把一张卡片控件从界面上彻底收掉（连同 `_running_count` / 网速的账）。

        与 `remove_card` 的区别：**不记 `_removed_keys`** —— 它只表示「这个控件不再需要」，
        不代表用户要求压住这个任务（例如磁盘锚定把同名键的重复卡收掉）。
        """
        if card is None:
            return
        if card.state == "running":
            self._running_count = max(0, self._running_count - 1)
        self._rate_by_key.pop(getattr(card, "key", ""), None)
        card.setParent(None)
        card.deleteLater()

    def _card_widgets(self) -> list:
        """布局里当前实际存在的卡片控件（跳过尾部 stretch 这类非控件项）。"""
        lay = self._cards_lay
        return [w for w in (lay.itemAt(i).widget() for i in range(lay.count()))
                if w is not None]

    def _sweep_orphan_widgets(self) -> int:
        """兜底清扫：把布局里**不属于 `_cards`** 的卡片控件收掉，返回收掉的数量。

        为什么要有：卡片控件一旦与 `_cards` 脱钩，就成了点 ✕ 无反应、
        `_prune_finished` 与「清空已完成」都遍历不到的**幽灵卡** —— 用户只能重启程序
        （实测反馈「任务卡依然无法删除」的成因之一）。
        正常生命周期里不该出现这种控件，所以这一扫与具体成因无关，纯兜底。
        """
        known = {id(c) for c in self._cards.values()}
        n = 0
        for w in self._card_widgets():
            if id(w) not in known:
                self._drop_card_widget(w)
                n += 1
        return n

    def remove_card(self, key: str) -> bool:
        """移除**任意一张**卡片（右键菜单入口），进行中的也能删。

        用户实测的痛点：卡片一旦卡在「进行中」（完成行丢了 / 身份错位），
        `_prune_finished` 与「清空已完成」都不会动它 → 只能看着它堆积。
        这里给一个无条件出口：
          · 卡片 UI 直接销毁；
          · 记进 `_pruned_keys`，**后续轮次 tdl 再画到它也不会复活**；
          · 速率一并清掉（不再计入状态栏网速）；`_bytes_by_key` 保留 —— 已下载的
            数据量属于事实，仍要计入本次统计。
        返回是否真的移除了。
        """
        card = self._cards.pop(key, None)
        if card is None:
            return False
        if card.state == "running":
            self._running_count = max(0, self._running_count - 1)
        card.setParent(None)
        card.deleteLater()
        # ⚠️⚠️ 记入 `_removed_keys` 是「真的删掉」的关键：tdl 下一轮**还会把这个任务
        #     当活跃任务重画**，而 `_touch_card` 在 _cards 里找不到 key 时会**就地新建
        #     一张卡** → 卡立刻复活（用户实测「任务卡依然无法删除」）。
        self._removed_keys.add(key)
        self._pruned_keys.add(key)
        # ⚠️ **不要**把这个 key 从 `_live` 摘掉！磁盘锚定是按**位置**搬移卡片的，
        #     少一个旧键会让**后面所有卡整体错位**（实测把别的卡也改成错的序号）。
        #     保留占位 → 位置一一对应 → 只是这张卡不再重建。
        self._rate_by_key.pop(key, None)
        self._unseen.pop(key, None)
        self._refresh_summary()
        self.card_removed.emit(key)          # 让「移除生效」看得见（见信号定义）
        return True

    def _finalize_stale(self, claimed) -> None:
        """把「连续多轮没有任何进度行认领」的进行中卡片定格为**已终止**。

        为什么需要：tdl 每轮都会重画**全部活跃任务**，所以一张进行中的卡连续多轮
        没人认领，就说明它已经不在 tdl 的活跃列表里了（完成行被别张卡吃掉 / 身份错位）。
        这类卡原先会永远停在「进行中」—— 修剪和「清空已完成」都不碰进行中的卡，
        于是越堆越多（用户实测「出错的任务卡无法删除导致堆积」）。
        定格成「已终止」之后它就变成可修剪 / 可清空的普通终态卡了。

        两道保险，避免误伤真正在跑的任务：
          · 只有**本轮确实解析到了进度行**（进程在跑、行也解析得出来）才判；
            tdl 卡住 / 暂停时不判，否则会把在跑的任务全判死；
          · 阈值 `STALE_SECONDS`（默认 60 秒）内没被认领才算 —— 正常下载每轮都会被认领。
        """
        if not claimed:
            return
        claimed = set(claimed)
        now = time.monotonic()
        changed = False
        # 遍历「有卡片的」+「只在 _live 里占位的」（后者=用户删掉的卡，见 remove_card）
        keys = list(dict.fromkeys(list(self._cards) + [x[0] for x in self._live]))
        for key in keys:
            card = self._cards.get(key)
            if key in claimed:                   # 本轮有进度行认领 → 还活着
                self._unseen.pop(key, None)
                continue
            if card is not None and card.state != "running":
                self._unseen.pop(key, None)
                continue
            t0 = self._unseen.setdefault(key, now)
            if now - t0 < self.STALE_SECONDS:
                continue
            self._unseen.pop(key, None)
            if card is None:
                # 用户删掉、且 tdl 已不再报告它 → 撤掉 _live 占位（不必再参与位置对齐）
                # ⚠️ 但**绝不取消压制**：长下载里 Telegram 限速暂停、某一轮没报这个
                #    任务，很容易出现这种「没被认领」的空档；一旦在此把 key 从
                #    `_removed_keys` 移除，那张卡就又能被重建，用户看到的就是
                #    「删了过一会儿又冒出来」（实测反馈第二次）。压制的生命周期
                #    只有 `begin()` 能结束。
                self._live[:] = [x for x in self._live if x[0] != key]
                changed = True
                continue
            card.set_state("stopped")            # 已终止（不再是进行中 → 可修剪/可清空）
            card.set_compact()
            self._running_count = max(0, self._running_count - 1)
            self._rate_by_key.pop(key, None)
            changed = True
        if changed:
            self._prune_finished()
            self._refresh_summary()

    def clear_finished(self) -> None:
        """移除非「进行中」的卡片（数据保留到 begin() 统一清空）。"""
        for key, card in list(self._cards.items()):
            if card.state != "running":
                card.setParent(None)
                card.deleteLater()
                del self._cards[key]
                self._pruned_keys.add(key)   # 忽略后续重绘（否则清完又被 tdl 画回来）
                self._rate_by_key.pop(key, None)
                self._unseen.pop(key, None)
        self._refresh_summary()

    def set_progress_hook(self, fn) -> None:
        """登记「本次运行累计进度」回调，签名 `fn(total_bytes, task_count)`。

        total_bytes = 本次运行所有任务卡已下载字节之和（累计值，非增量）；
        task_count = 其中已经有数据的任务（文件）数。
        每次刷新（`commit_round` 末尾）调用一次；**回调抛错绝不影响下载界面**。
        """
        self._progress_hook = fn

    def progress_snapshot(self) -> tuple[int, int]:
        """本次运行的累计进度：`(已下载字节之和, 已有数据的任务数)`。"""
        vals = [v for v in self._bytes_by_key.values() if v]
        return sum(vals), len(vals)

    def _emit_progress(self) -> None:
        fn = self._progress_hook
        if fn is None:
            return
        try:
            fn(*self.progress_snapshot())
        except Exception:                        # noqa: BLE001 - 统计失败不能影响下载
            pass

    def _refresh_summary(self) -> None:
        n = len(self._cards)
        base = f"共 {n} 个任务 · {self._running_count} 个进行中" if n else "共 0 个任务"
        self._summary.setText(f"{self._status}  ·  {base}" if self._status else base)

    def _display_name(self, info: dict, seq: int, key: str = "") -> str:
        """卡片标题：**最终落盘的文件名** —— `{群组号}_{消息号}_{文件名}`。

        tdl 的 --template 是 `{{ .DialogID }}_{{ .MessageID }}_{{ .FileName }}`，
        下载产物就叫 `1539539150_95292_8月25日(2).mp4`。标题按同样的三段式拼出
        同一个名字，用户可以直接拿它去下载目录对照（此前用「群号 #序号  文件名」
        的自创格式，与真实文件名对不上，用户明确要求改成一致）。

        序号用**消息 id**（还原后的完整值），不用「本页第几个」的自增序号 ——
        自增序号跟上面对不上（翻页、重跑、并发顺序一变就换号），没法定位文件。
        文件名来自下载源 JSON（tdl 的进度行里没有文件名也没有扩展名）。

        ⚠️ 不带群名：tdl 的 message 按终端宽度硬截断，群名越长越容易把后面的
        `(群号):消息号` 整段吃掉，带上只会显示成半截乱码。

        id / 文件名拿不到时（没有下载源 JSON）就退化为能显示的部分；
        连 id 都没有时用「#自增序号」占位（带 # 明示这不是真实文件名）。
        """
        if ":" in key and "#" not in key:
            gid, _, mid = key.partition(":")
        else:
            gid = str(info.get("gid") or "").strip()
            mid = str(info.get("mid") or "").strip()
        if gid and mid:
            return "_".join(p for p in (gid, mid, self._file_of(mid)) if p)
        if gid:
            return f"{gid} #{seq}"              # 拿不到 id：退回自增序号
        return "任务"

    def total_bytes_text(self) -> str:
        """状态栏「下载」字段：十进制单位，与卡片统计行同口径。"""
        return (fmt_bytes_dec(sum(self._bytes_by_key.values()))
                if self._bytes_by_key else "0 B")

    def total_rate_text(self) -> str:
        """状态栏「网速」字段：**只累加进行中任务的速率**。

        ⚠️ 不能直接 `sum(self._rate_by_key.values())` —— 那个字典里还会留着已完成
        任务在完成行报出的平均速率，长跑会把历史速率一路累加（用户实测「网速会累计」）。
        这里按「卡片此刻是不是进行中」过滤，双保险。
        """
        total = 0.0
        for key, card in self._cards.items():
            if card.state == "running":
                total += self._rate_by_key.get(key, 0.0)
        return f"{fmt_bytes_dec(total)}/s" if total > 0 else "0 B/s"

    def current_group(self) -> str:
        """当前仍在进行中的任务所属群组名（供状态栏显示）；无则空串。"""
        for key, card in self._cards.items():
            if card.state == "running":
                gname = getattr(card, "gname", "") or ""
                if gname:
                    return gname
                return f"群 {getattr(card, 'gid', '')}" if getattr(card, "gid", "") else ""
        return ""

    def flush_pending(self) -> None:
        """页面重新可见时补渲染曾经跳过的卡片更新。"""
        for card in self._cards.values():
            card.flush_pending()

    def active_count(self) -> int:
        return len(self._cards)


class TaskBox(QWidget):
    """任务进度页主体：卡片式任务列表（含汇总行）。

    实例跨页保留（Content.clear_form 收回不销毁），执行与页面解耦：
    无论用户停留在哪个页面，进度都在后台更新，切回本页即可看到。
    页面标题已是「任务进度」，这里不再放「当前任务」小标题栏；
    运行状态（就绪/进行中/完成…）并入汇总行最前。
    """

    stop_all_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.table = TaskPanel()
        self.table.stop_all_requested.connect(self.stop_all_requested.emit)
        self.table.card_removed.connect(self.card_removed.emit)
        self.table.setMinimumHeight(320)
        lay.addWidget(self.table, 1)

    def set_state(self, text: str) -> None:
        self.table.set_status(text)

    def begin(self, reset: bool = True, kind: str = "") -> None:
        """命令启动：默认清空卡片流；导出多段命令 reset=False 连续累积。"""
        if reset:
            self.table.begin(kind)
        self.set_state("进行中")

    def update_task(self, info: dict) -> None:
        self.table.update_task(info)

    def push_row(self, info: dict) -> None:
        """进度行入缓存（等本轮结束再统一认领身份，见 TaskPanel.commit_round）。"""
        self.table.push_row(info)

    def commit_round(self) -> None:
        """一轮渲染结束：让面板统一认领身份并刷新卡片。"""
        self.table.commit_round()

    def set_id_pool(self, ids, files=None, restart: bool = False) -> None:
        """登记下载源 JSON 里的完整消息号与文件名（还原序号 + 显示文件名）。"""
        self.table.set_id_pool(ids, files, restart)

    def set_download_dir(self, d: str) -> None:
        """透传下载目录（.tmp 锚定 / 已下载号排除用，见 TaskPanel.set_download_dir）。"""
        self.table.set_download_dir(d)

    def current_group(self) -> str:
        """当前进行中任务所属群组名（状态栏显示）。"""
        return self.table.current_group()

    def flush_pending(self) -> None:
        """页面重新可见时补渲染跳过的卡片更新。"""
        self.table.flush_pending()

    def finish(self, ok: bool) -> dict:
        return self.table.finish(ok)

    card_removed = Signal(str)

    def set_progress_hook(self, fn) -> None:
        """透传累计进度回调（下载统计按实际时间分账用，见 TaskPanel.set_progress_hook）。"""
        self.table.set_progress_hook(fn)

    def progress_snapshot(self) -> tuple[int, int]:
        """透传本次运行累计进度快照。"""
        return self.table.progress_snapshot()

    def total_bytes_text(self) -> str:
        return self.table.total_bytes_text()

    def total_rate_text(self) -> str:
        return self.table.total_rate_text()
