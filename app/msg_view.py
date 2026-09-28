# -*- coding: utf-8 -*-
"""
消息浏览 —— 混合展示文字与媒体，支持 Telegram「媒体组（相册）」式排版。

一条消息 / 一个媒体组的卡片结构：
    单条消息（有正文+媒体）：         媒体组（相册，≥2 个媒体）：
        [#编号] [类型]   MM-DD HH:MM      [#首~末] [媒体组 N]   MM-DD HH:MM
        正文（在上）                       ┌────┐┌────┐
        ┌──────────┐                      │ 图1 ││ 视2 │   ← 网格拼排
        │  媒体预览 │                      └────┘└────┘
        └──────────┘                      ┌────┐┌────┐
        文件名 · 类型 · 大小  [✓已下载][查看] │ 图3 ││ 图4 │
                                          └────┘└────┘
                                          正文 / 说明（在下，对应相册 caption）
                                          N 个文件 · 类型 · 大小 [✓已下载][打开]

数据：{"id": 1234567890, "messages": [
        {"id":123, "type":"message", "file":"a.jpg", "date":1756684800, "text":"说明 #标签"}]}
    · date/text 需导出时带 `--with-content`（GUI 导出流程已带上）
    · **媒体组判定**：时间戳完全相同 + 编号相邻（TG 相册的特征）；无 date 时退化为单条
    · **空消息（无正文也无附件）不显示**
    · 文件按 `{DialogID}_{MessageID}_{原名}` 在 JSON 同目录 + 下载目录中匹配
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import html
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QSize, QPoint, Signal
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFontMetrics, QGuiApplication, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import paths
from .media_tools import ffmpeg_path, ffprobe_path           # noqa: F401（对外仍可用）
from .media_viewer import HAVE_QTMM, is_viewable, open_media
from .msg_cache import SlimError, load_messages
from .msg_dedup import dedup_keep_ends
from .task_panel import fmt_bytes_dec
from .theme import T
from .widgets import ElidedLabel, hand_cursor

# ---- 媒体类型 ----
_KINDS: list[tuple[str, str, set[str]]] = [
    ("图片", "#1a7f37", {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "tif", "tiff"}),
    ("视频", "#8250df", {"mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts"}),
    ("音频", "#9a6700", {"mp3", "m4a", "ogg", "wav", "flac", "aac", "opus", "wma"}),
    ("文档", "#0969da", {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
                        "md", "csv", "json", "xml", "epub", "rtf"}),
    ("压缩", "#57606a", {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}),
]
_OTHER = ("其他", "#6e7781")
KIND_ORDER = [k[0] for k in _KINDS] + [_OTHER[0]]

VIEW_W, VIEW_H = 360, 220        # 单个媒体预览区最大尺寸
CELL_W, CELL_H = 200, 200        # 媒体组网格中单格尺寸
GRID_COLS = 2                    # 网格最少列数（窄窗口时每行 2 个）
MAX_GRID_COLS = 5                # 网格最多列数（宽窗口时每行最多 5 个 → 9 个媒体只占 2 行）
RENDER_BATCH = 40                # 每帧插入的消息（组）数（分批渲染，界面不卡）
CARD_PAGE = 100                  # 每页显示的任务卡数量（上下页翻页）
BATCH = 6                        # 每批加载的预览数量（按可见性优先）
VID_BATCH = 2                    # 每批最多生成的视频缩略图数（ffmpeg 调用较慢）
# 缩略图缓存分区位数：缓存文件名是内容哈希，全部平铺在一个目录里（实测已 474 个，
# 随下载量增长会到几千上万，单目录文件过多会让资源管理器卡顿、枚举变慢）。
# 按哈希**前 2 位**分成 256 个子目录（Git / npm / 浏览器缓存都是这个做法，分布均匀）。
THUMB_SHARD = 2
_RE_TAG = re.compile(r"#([0-9A-Za-z_\u4e00-\u9fff]+)")
_TAG_SCHEME = "tdltag:"                      # #标签 链接协议（点击 → 按标签搜索）


def _tint(hex_color: str, alpha: int) -> str:
    """#RRGGBB + alpha → rgba()（Qt QSS 的 8 位 hex 是 AARRGGBB，统一用 rgba 规避）。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha / 255:.2f})"


def kind_of(fname: str) -> tuple[str, str]:
    e = str(fname or "").rsplit(".", 1)[-1].lower() if "." in str(fname or "") else ""
    for label, color, exts in _KINDS:
        if e in exts:
            return label, color
    return _OTHER


def _kind_color(label: str) -> str:
    for l, c, _e in _KINDS:
        if l == label:
            return c
    return _OTHER[1]


def fmt_ts(ts) -> str:
    """Unix 秒 → MM-DD HH:MM（跨年带年份）。"""
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    dt = datetime.datetime.fromtimestamp(t)
    today = datetime.date.today()
    return (dt.strftime("%m-%d %H:%M") if dt.year == today.year
            else dt.strftime("%Y-%m-%d %H:%M"))


def _fmt_dur_hms(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def text_html(text: str) -> str:
    """正文 → 富文本：#标签 渲染为可点击链接（点击后按标签搜索，类 TG 行为）。"""
    s = html.escape(str(text or "")).replace("\n", "<br>")
    return _RE_TAG.sub(
        r'<a href="tdltag:\1" style="color:#0969da; text-decoration:none;">'
        r'#\1</a>', s)


def cluster_messages(msgs: list[dict]) -> list[list[dict]]:
    """把连续且时间戳相同的消息聚成「媒体组」（对应 Telegram 相册）。

    TG 相册由多条消息组成：**时间戳完全相同 + 编号相邻**。
    无 date 数据时（旧导出）每条自成一组，退化为单条显示。
    """
    out: list[list[dict]] = []
    for m in msgs:
        d = m.get("date")
        if out and d and out[-1][-1].get("date") == d:
            try:
                near = int(m.get("id", 0)) - int(out[-1][-1].get("id", 0)) <= 20
            except (TypeError, ValueError):
                near = False
            if near:
                out[-1].append(m)
                continue
        out.append([m])
    return out


class CopyChip(QLabel):
    """编号徽标：点击复制编号（便于填「导出 JSON」的 id 区间参数）。"""

    def __init__(self, text: str, copy_text: str, parent=None):
        super().__init__(text, parent)
        self._copy = copy_text
        hand_cursor(self)
        self.setToolTip(f"点击复制消息编号：{copy_text}")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            QGuiApplication.clipboard().setText(self._copy)
            self.setText("已复制")
            QTimer.singleShot(900, lambda: self.setText(f"#{self._copy}"))
        e.accept()


class MediaCell(QLabel):
    """媒体格：单击交给上层用「内置查看器」打开（可在同批媒体间左右切换）。"""

    activated = Signal()

    def __init__(self, local: Path | None, parent=None):
        super().__init__(parent)
        self.local = local
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        if local is not None:
            hand_cursor(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.local is not None and self.local.exists():
            self.activated.emit()
        e.accept()


class MsgItem(QFrame):
    """一条消息，或一个媒体组（相册）。"""

    open_requested = Signal(int)        # 点开第 i 个媒体格（内置查看器）
    tag_clicked = Signal(str)           # 点 #标签 → 按该标签搜索
    context_menu_requested = Signal(object)   # 右键卡片（传 self，菜单由面板弹）

    def __init__(self, msgs: list[dict], locals_: list[Path | None], parent=None):
        super().__init__(parent)
        self.setObjectName("MsgItem")
        self.msgs = msgs
        self.locals = locals_
        self.media = [(m, p) for m, p in zip(msgs, locals_)
                      if str(m.get("file", "") or "").strip()]
        self.is_group = len(self.media) > 1
        self.cells: list[MediaCell] = []
        self._flags: list[bool] = []

        # 兼容单条语义的属性
        self.mid = str(msgs[0].get("id", ""))
        first_media = self.media[0] if self.media else (None, None)
        self.fname = str(first_media[0].get("file", "")) if first_media[0] else ""
        self.local = first_media[1]
        self.label = kind_of(self.fname)[0] if self.fname else (
            "媒体组" if self.is_group else "")
        self.body: QLabel | None = None
        self.preview: MediaCell | None = None
        self._grid: QGridLayout | None = None      # 媒体组网格（按宽度重排用）
        self._grid_cols = 0                        # 当前每行列数

        self.setStyleSheet(
            "QFrame#MsgItem { background:#ffffff; border:1px solid #d0d7de;"
            "border-radius:8px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        # ---- 头行 ----
        head = QHBoxLayout()
        head.setSpacing(8)
        ids = []
        for m in msgs:
            try:
                ids.append(int(m.get("id", 0)))
            except (TypeError, ValueError):
                pass
        no_txt = f"#{ids[0]}" if len(ids) <= 1 else f"#{min(ids)}~{max(ids)}"
        copy_txt = str(ids[0]) if len(ids) <= 1 else f"{min(ids)},{max(ids)}"
        no = CopyChip(no_txt, copy_txt)
        no.setFixedHeight(20)
        no.setStyleSheet(
            "color:#57606a; background:#f6f8fa; border:1px solid #d0d7de;"
            "border-radius:4px; padding:0 8px; font-size:12px;"
            "font-family:Consolas,monospace;")
        head.addWidget(no)

        tag_label = f"媒体组 {len(self.media)}" if self.is_group else self.label
        if tag_label:
            color = _OTHER[1] if self.is_group else _kind_color(tag_label)
            tag = QLabel(tag_label)
            tag.setFixedHeight(20)
            tag.setStyleSheet(
                f"color:{color}; background:{_tint(color, 38)}; border:1px solid"
                f" {_tint(color, 105)}; border-radius:4px; padding:0 9px;"
                f"font-size:12px; font-weight:600;")
            head.addWidget(tag)

        # 「重复 ×N」：同一文字共 N 条，本卡为首条或末条（中间已合并）
        try:
            dup_total = int(msgs[0].get("_dup_total") or 0)
        except (TypeError, ValueError):
            dup_total = 0
        if dup_total > 1:
            # 悬浮提示已全局禁用 → 说明直接写进徽标文本
            dup = QLabel(f"重复 ×{dup_total} · 保留首末")
            dup.setFixedHeight(20)
            dup.setStyleSheet(
                "color:#9a6700; background:rgba(154,103,0,0.10); border:1px solid"
                " rgba(154,103,0,0.34); border-radius:4px; padding:0 9px;"
                "font-size:12px; font-weight:600;")
            head.addWidget(dup)
        head.addStretch(1)
        ts = fmt_ts(msgs[0].get("date"))
        if ts:
            t = QLabel(ts)
            t.setStyleSheet("color:#8b949e; font-size:12px; background:transparent;")
            head.addWidget(t)
        lay.addLayout(head)

        text = next((str(m.get("text", "") or "").strip() for m in msgs
                     if str(m.get("text", "") or "").strip()), "")

        # ---- 正文：单条在上（上轮约定）；媒体组在下（相册 caption 位置，对照截图）----
        if text and not self.is_group:
            lay.addWidget(self._make_body(text))

        if self.media:
            if self.is_group:
                lay.addWidget(self._make_grid())
            else:
                self.preview = self._make_single()
                lay.addWidget(self.preview)

        if text and self.is_group:
            lay.addWidget(self._make_body(text))

        lay.addLayout(self._make_info())

    # ------------------------------------------------------------------ 组件
    def contextMenuEvent(self, e) -> None:
        """右键卡片 → 交给面板弹菜单（如「跳转到该条消息」）。"""
        self.context_menu_requested.emit(self)
        e.accept()

    def flash(self) -> None:
        """跳转定位后的短暂高亮（1.6 秒后恢复）。"""
        self.setStyleSheet(
            "QFrame#MsgItem { background:#fffdf3; border:2px solid #d4a72c;"
            "border-radius:8px; }")
        QTimer.singleShot(1600, self._unflash)

    def _unflash(self) -> None:
        try:
            self.setStyleSheet(
                "QFrame#MsgItem { background:#ffffff; border:1px solid #d0d7de;"
                "border-radius:8px; }")
        except RuntimeError:                       # 卡片已销毁（翻页）
            pass

    def _make_body(self, text: str) -> QLabel:
        body = QLabel(text_html(text))
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # 既能选中复制，又能点 #标签（LinksAccessibleByMouse）
        body.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        body.setOpenExternalLinks(False)
        body.setStyleSheet("color:#1f2328; font-size:13px; background:transparent;")
        body.linkActivated.connect(self._on_link)
        self.body = body
        return body

    def _on_link(self, href: str) -> None:
        """#标签链接 → 交给上层按标签搜索。"""
        if href.startswith(_TAG_SCHEME):
            self.tag_clicked.emit(href[len(_TAG_SCHEME):])

    def _make_single(self) -> MediaCell:
        cell = MediaCell(self.local)
        cell.setFixedHeight(VIEW_H)
        cell.setText("加载中…" if self.local is not None else "文件未找到")
        cell.setAlignment(Qt.AlignCenter)
        cell.setStyleSheet(
            "color:#8b949e; background:#f6f8fa; border:1px dashed #d0d7de;"
            "border-radius:6px; font-size:12px;")
        cell.activated.connect(lambda: self.open_requested.emit(0))
        self.cells.append(cell)
        self._flags.append(False)
        return cell

    def _make_grid(self) -> QWidget:
        host = QWidget()
        host.setStyleSheet("background:transparent;")
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self._grid = grid
        self._grid_cols = 0                          # 触发首次排版
        for i, (_m, p) in enumerate(self.media):
            cell = MediaCell(p)
            cell.setFixedSize(CELL_W, CELL_H)
            # ⚠️ 网格里的格子必须用 Fixed 策略：MediaCell 默认是 Ignored
            # （为让单条预览不撑大布局），但在 QGridLayout 里会让**列宽算成 0**，
            # 导致同一行的两个格子几乎重叠、看起来「少了一半媒体」。
            cell.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            cell.setText("加载中…" if p is not None else "文件未找到")
            cell.setAlignment(Qt.AlignCenter)
            cell.setStyleSheet(
                "color:#8b949e; background:#f6f8fa; border:1px dashed #d0d7de;"
                "border-radius:6px; font-size:12px;")
            cell.activated.connect(lambda _=None, k=i: self.open_requested.emit(k))
            self.cells.append(cell)
            self._flags.append(False)
        self._relayout_grid()                        # 按当前宽度先排一次
        return host

    def grid_cols(self) -> int:
        """按可用宽度与实际格子宽度，算出这一行能放几个。

        格子宽度是**图片等比缩放后的真实宽度**（竖屏视频只有 ~110px，
        横屏接近 200px），所以竖屏相册一行能放更多 —— 9 个媒体的相册
        在常见窗口宽度下排成 2 行；窄窗口自动退回 GRID_COLS 列。
        上限 MAX_GRID_COLS，避免一排缩略图过密。
        """
        avail = max(CELL_W + 8, self.width() - 28)      # 28 = 卡片左右内边距
        used, n = 0, 0
        for cell in self.cells:
            w = max(48, cell.width()) + 8
            if n >= GRID_COLS and used + w > avail + 8:
                break
            used += w
            n += 1
        return max(GRID_COLS, min(MAX_GRID_COLS, n))

    def _relayout_grid(self) -> None:
        """按当前列数重排网格（列数没变则跳过，避免 resize 振荡）。"""
        if self._grid is None:
            return
        cols = self.grid_cols()
        if cols == self._grid_cols:
            return
        self._grid_cols = cols
        for i, cell in enumerate(self.cells):
            self._grid.addWidget(cell, i // cols, i % cols)   # 已在布局里的控件会被移动
        for c in range(MAX_GRID_COLS + 1):                    # 末尾留一列吸掉多余宽度
            self._grid.setColumnStretch(c, 1 if c == cols else 0)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.is_group:
            self._relayout_grid()

    def _make_info(self) -> QHBoxLayout:
        info = QHBoxLayout()
        info.setSpacing(8)
        parts: list[str] = []
        if self.is_group:
            kinds = []
            for m, _p in self.media:
                k = kind_of(str(m.get("file", "")))[0]
                if k not in kinds:
                    kinds.append(k)
            parts.append(f"{len(self.media)} 个文件")
            parts.extend(kinds)
        elif self.fname:
            parts.append(self.fname if len(self.fname) <= 40 else self.fname[:39] + "…")
            parts.append(self.label)
        total = 0
        for _m, p in self.media:
            if p is not None:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        if total:
            parts.append(fmt_bytes_dec(total))
        if len(self.media) == 1 and self.label == "视频":
            d = self._duration(self.local)
            if d:
                parts.append(d)

        nm = ElidedLabel("　·　".join(x for x in parts if x), color="#57606a")
        nm.setFixedHeight(22)
        info.addWidget(nm, 1)

        have = sum(1 for _m, p in self.media if p is not None)
        if self.media and have:
            ok = QLabel("✓ 已下载" if have == len(self.media) else f"已下载 {have}/{len(self.media)}")
            ok.setFixedHeight(22)
            ok.setStyleSheet(
                "color:#1a7f37; background:rgba(26,127,55,0.14);"
                "border-radius:4px; padding:0 9px; font-size:12px; font-weight:600;")
            info.addWidget(ok)
            btn = QPushButton("查看" if self.label in ("图片", "视频", "音频") else "打开")
            btn.setObjectName("Btn")
            btn.setFixedHeight(24)
            btn.setFixedWidth(56)
            btn.clicked.connect(self._open_first)
            info.addWidget(btn)
        elif self.media:
            miss = QLabel("未下载")
            miss.setFixedHeight(22)
            miss.setStyleSheet(
                "color:#6e7781; background:#f6f8fa; border-radius:4px;"
                "padding:0 9px; font-size:12px;")
            info.addWidget(miss)
        return info

    def _open_first(self) -> None:
        for _m, p in self.media:
            if p is not None and p.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                return

    def _duration(self, local: Path | None) -> str:
        exe = ffprobe_path()
        if not exe or local is None:
            return ""
        try:
            r = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(local)],
                timeout=12, capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return _fmt_dur_hms(float((r.stdout or "0").strip() or 0))
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""

    # ---- 媒体加载（由 MsgBrowser 分批调用；idx = 格序号）----
    def set_image(self, idx: int, pix: QPixmap) -> None:
        """图片：完整显示（等比不裁剪），透明底、靠左上、高度贴合。"""
        cell = self.cells[idx]
        if self.is_group:
            scaled = pix.scaled(CELL_W, CELL_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            cell.setFixedSize(scaled.width(), scaled.height())
        else:
            scaled = pix.scaled(VIEW_W, VIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            cell.setFixedHeight(scaled.height())
        cell.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        cell.setPixmap(scaled)
        cell.setStyleSheet("background:transparent; border:none;")
        self._flags[idx] = True
        self._relayout_grid()        # 格子真实宽度变了 → 重新算每行能放几个

    def set_video_thumb(self, idx: int, pix: QPixmap) -> None:
        self.set_image(idx, pix)
        self.cells[idx].setToolTip("视频首帧 · 点击打开")

    def set_missing(self, idx: int) -> None:
        self.cells[idx].setText("文件未找到")
        self._flags[idx] = True

    def mark_loading(self, idx: int) -> None:
        self.cells[idx].setText("加载中…")

    @property
    def loaded(self) -> bool:
        return bool(self._flags) and all(self._flags)

    def media_kinds(self) -> list[str]:
        return [kind_of(str(m.get("file", "")))[0] for m, _p in self.media]


class MsgBrowser(QWidget):
    """消息浏览页主体：工具行 + 消息/媒体组混合流。"""

    loaded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: list[dict] = []
        self._chat_id = ""
        self._path = ""
        self._index: dict[str, Path] = {}
        self._by_name: dict[str, Path] = {}
        self._queue: list[tuple[MsgItem, int, Path, str]] = []
        self._groups: list[list[dict]] = []     # 当前视图的分组（全部，无上限）
        self._page = 1                          # 当前页码（1 基）
        self._page_count = 1                    # 总页数
        self._page_start = 0                    # 本页首个分组的全局下标
        self._page_end = 0                      # 本页之后一个分组的全局下标
        self._dup_removed = 0                   # 被合并的重复文字消息数
        self._pre_slimmed = False               # 数据来自精简缓存（无需再次去重）
        self._slim_info: dict = {}              # 精简缓存信息 {from_cache, removed, slim}
        self._pending_highlight = ""            # 待定位的消息编号（翻页完成后高亮）
        self._open_list: list[tuple[Path, str]] = []   # (路径, 消息编号)：查看器切换 + 跳回消息
        self._viewers: list[QDialog] = []       # 已打开的内置查看器（非模态，需持引用）
        self._gi = 0                            # 已插入的分组游标
        self._rendering = False
        self._released = False                  # 内容是否已被「切后台」卸载（见 release_memory）
        self._page_keep = 1                     # 卸载前所在页码（恢复时回到同一页）
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._load_batch)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_batch)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ---- 工具行 ----
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_open = QPushButton("选择 JSON 文件")
        self.btn_open.setObjectName("Btn")
        self.btn_open.setFixedHeight(T.H_CONTROL)
        self.btn_open.clicked.connect(self.pick_file)
        bar.addWidget(self.btn_open)

        # 卸载当前 JSON：主动释放内存，回到「尚未加载」状态（没有加载内容时禁用）
        self.btn_unload = QPushButton("卸载 JSON")
        self.btn_unload.setObjectName("Btn")
        self.btn_unload.setFixedHeight(T.H_CONTROL)
        self.btn_unload.setEnabled(False)
        hand_cursor(self.btn_unload)
        self.btn_unload.clicked.connect(self.unload)
        bar.addWidget(self.btn_unload)
        bar.addStretch(1)
        bar.addSpacing(4)

        from .form_renderer import NoWheelComboBox
        self.cmb_kind = NoWheelComboBox()
        self.cmb_kind.setFixedHeight(T.H_CONTROL)
        self.cmb_kind.setMinimumWidth(0)
        self.cmb_kind.setMinimumContentsLength(6)
        self.cmb_kind.addItem("全部类型", "")
        for label in KIND_ORDER:
            self.cmb_kind.addItem(label, label)
        self.cmb_kind.currentIndexChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.cmb_kind)

        self.edit_q = QLineEdit()
        self.edit_q.setFixedHeight(T.H_CONTROL)
        self.edit_q.setMinimumWidth(0)
        self.edit_q.setPlaceholderText("搜索：编号 / 文件名 / 正文")
        self.edit_q.setFixedWidth(190)
        self.edit_q.textChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.edit_q)
        lay.addLayout(bar)

        # ---- 摘要行：独立一行 + 自动换行（原来挤在工具行里会被截断）----
        self._info = QLabel("尚未加载消息 JSON")
        self._info.setObjectName("FieldHelp")
        self._info.setWordWrap(True)
        self._info.setMinimumWidth(0)
        self._info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self._info)

        # ---- 消息流 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        host = QWidget()
        host.setObjectName("MsgHost")
        host.setStyleSheet("QWidget#MsgHost { background:transparent; }")
        self._host_lay = QVBoxLayout(host)
        self._host_lay.setContentsMargins(0, 0, 0, 0)
        self._host_lay.setSpacing(10)
        self._host_lay.addStretch(1)
        self._scroll.setWidget(host)
        lay.addWidget(self._scroll, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(self._prioritize)

        self._empty = QLabel(self._empty_text())
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color:#57606a; font-size:12.5px; background:transparent;")
        lay.addWidget(self._empty)

        # ---- 分页器：每页 CARD_PAGE 张卡，支持上下页 + 页码跳转 ----
        self._pager = QWidget()
        self._pager.setVisible(False)
        pg = QHBoxLayout(self._pager)
        pg.setContentsMargins(0, 0, 0, 0)
        pg.setSpacing(6)

        def _pbtn(text: str, slot, width: int | None = None) -> QPushButton:
            b = QPushButton(text)
            b.setObjectName("Btn")
            b.setFixedHeight(T.H_CONTROL)
            hand_cursor(b)
            if width:
                b.setFixedWidth(width)
            b.clicked.connect(slot)
            return b

        self.btn_prev_page = _pbtn("‹ 上一页", lambda: self.go_page(self._page - 1))
        self.btn_next_page = _pbtn("下一页 ›", lambda: self.go_page(self._page + 1))
        pg.addWidget(self.btn_prev_page)
        self.lbl_page = QLabel("第 1 / 1 页")
        self.lbl_page.setObjectName("FieldHelp")
        self.lbl_page.setFixedHeight(T.H_CONTROL)
        self.lbl_page.setMinimumWidth(92)
        self.lbl_page.setAlignment(Qt.AlignCenter)
        pg.addWidget(self.lbl_page)
        pg.addWidget(self.btn_next_page)
        pg.addSpacing(10)
        _lbl_go = QLabel("跳转到")
        _lbl_go.setObjectName("FieldHelp")
        pg.addWidget(_lbl_go)
        self.edit_page = QLineEdit()
        self.edit_page.setFixedHeight(T.H_CONTROL)
        self.edit_page.setFixedWidth(56)
        self.edit_page.setPlaceholderText("页码")
        self.edit_page.setAlignment(Qt.AlignCenter)
        self.edit_page.returnPressed.connect(self._on_page_jump)
        pg.addWidget(self.edit_page)
        pg.addWidget(_pbtn("跳转", self._on_page_jump, width=52))
        pg.addStretch(1)
        lay.insertWidget(lay.indexOf(self._scroll), self._pager)

    # ------------------------------------------------------------------ 数据
    @staticmethod
    def _empty_text() -> str:
        return ("尚未加载消息：先到「任务 → 下载 → 导出 JSON」导出消息记录"
                "（勾选「同时导出全部消息」以获得正文与时间），"
                "再点上方「选择 JSON 文件」浏览。")

    def _pick_start(self) -> str:
        """「选择 JSON」对话框的起始目录。

        优先级：已加载文件所在目录 → `resources/dl`（消息 JSON 的固定存放处，
        每个群组一个子文件夹）→ `resources/`。没有已加载文件时**不要**落回系统
        默认位置，否则每次都得到软件目录里手动翻找。
        """
        if self._path:
            p = Path(self._path).parent
            if p.is_dir():
                return str(p)
        for d in (paths.dl_root(), paths.res_root()):
            if d.is_dir():
                return str(d)
        return ""

    # ------------------------------------------------------------------ 卸载
    def _sync_unload_btn(self) -> None:
        """没有加载内容时禁用「卸载 JSON」按钮。"""
        try:
            self.btn_unload.setEnabled(bool(self._path or self._all))
        except AttributeError:                               # 构造期早于按钮创建
            pass

    def unload(self) -> int:
        """**卸载当前已加载的 JSON**：释放内存并回到「尚未加载消息」状态。

        与 `release_memory()`（切后台自动卸载）的区别：这里**连来源路径也忘掉** ——
        不会自动重新加载；想再看就点「选择 JSON 文件」重新打开。
        释放的内容与后者相同：全量消息、文件索引、已渲染卡片、已打开的查看器。
        返回释放掉的条目数（0 = 本来就没加载东西）。
        """
        name = Path(self._path).name if self._path else ""
        n = len(self._all) + len(self._index) + len(self._by_name)
        self.teardown()                          # 停表 + 清队列/分组 + 销毁卡片
        for v in list(self._viewers):
            try:
                v.close()
                v.deleteLater()
            except (RuntimeError, AttributeError):
                pass
        self._viewers = []
        self._all = []
        self._chat_id = ""
        self._path = ""
        self._index.clear()
        self._by_name.clear()
        self._slim_info = {}
        self._pre_slimmed = False
        self._dup_removed = 0
        self._released = False                   # 主动卸载 ≠ 后台释放：不该被自动恢复
        self._page = self._page_count = 1
        self._page_keep = 1
        self._pending_highlight = ""
        self._scroll.setVisible(False)
        self._pager.setVisible(False)
        self._empty.setText(self._empty_text())
        self._empty.setVisible(True)
        self._info.setText(
            f"已卸载：{name}（内存已释放，可重新选择 JSON 文件）" if name
            else "尚未加载消息 JSON")
        self._sync_unload_btn()
        return n

    def pick_file(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 tdl 导出的消息 JSON", self._pick_start(),
            "JSON 文件 (*.json)")
        if p:
            self.load(p)

    def load(self, path: str) -> bool:
        """加载 JSON：先彻底结束旧文件的卡片与预览加载，再渲染新文件。

        走 `msg_cache`：首次打开会把「重复消息只留首末」的结果落盘
        （同目录 `<原名>-slim.json`），之后再打开直接读缓存；
        源文件被替换（重新导出）时 size/mtime 变化 → 自动重新精简。
        """
        self.teardown()                     # 切文件 → 旧卡片 / 旧加载队列全部结束
        self._released = False              # 主动加载 = 内容重新建立（清掉「已卸载」标记，
                                            # 否则切后台再回来会多走一次恢复）
        try:
            msgs, cid, info = load_messages(path)
        except SlimError as e:
            self._all = []
            self._chat_id = ""
            self._path = ""
            self._slim_info = {}
            self._pre_slimmed = False
            self._info.setText(f"加载失败：{e}")
            self._empty.setText(f"加载失败：{Path(path).name}\n\n{e}")
            self._empty.setVisible(True)
            self._scroll.setVisible(False)
            self._pager.setVisible(False)
            self._sync_unload_btn()
            return False
        self._all = msgs
        self._chat_id = cid
        self._path = path
        self._slim_info = info
        # 缓存命中，或首次已把重复合并掉 → 视图内不再重复去重（省一次近似聚类）
        self._pre_slimmed = bool(info.get("removed")) or bool(info.get("from_cache"))
        self._index_files()
        self.rebuild()
        self._sync_unload_btn()
        return True

    def _index_files(self) -> None:
        """建立「消息 → 本地媒体文件」索引 —— **只扫消息 JSON 所在的那个文件夹**。

        为什么要限定文件夹（2.9 起，用户设定）：
          · 下载布局本来就是「一个群一个文件夹」（`resources/dl/<缩写>-<群号>/`），
            消息 JSON 与它的媒体就在同一个文件夹里，扫别处纯属多余；
          · 原先还额外把**整个下载根目录**递归扫进来，这会**跨群串味** ——
            不同群常有同名文件（`IMG_1542.jpg` 这种），按文件名匹配时先扫到谁就算谁的，
            点开预览可能看到的是另一个群的文件；
          · 顺带省掉一次对下载根目录的递归扫描（下载量大时是上万条路径，
            每次加载 / 换文件都要重扫一遍），索引本身也小得多、更省内存。
        文件夹**内部仍递归**（允许用户自己按日期等再分子目录存放）。
        """
        self._index.clear()
        self._by_name.clear()
        if not self._path:
            return
        d = Path(self._path).parent
        if not d.is_dir():
            return
        for f in d.rglob("*"):
            if not f.is_file() or f.suffix.lower() == ".json":
                continue
            self._by_name.setdefault(f.name.lower(), f)
            parts = f.stem.split("_")
            if len(parts) >= 3 and parts[1].isdigit():
                self._index.setdefault(parts[1], f)

    def _local_of(self, m: dict) -> Path | None:
        fname = str(m.get("file", "") or "").strip()
        if not fname:
            return None
        return (self._index.get(str(m.get("id", "")))
                or self._by_name.get(fname.lower()))

    @staticmethod
    def _id_sort_key(m: dict) -> tuple[int, int]:
        """按消息编号排序的键（从小到大；非数字编号排最后）。"""
        try:
            return (0, int(str(m.get("id", "")).strip()))
        except (TypeError, ValueError):
            return (1, 0)

    def _visible_groups(self) -> list[list[dict]]:
        """空消息（无正文也无附件）不显示；按搜索/类型筛选；按消息编号升序；再聚成媒体组。

        去重逻辑与「精简缓存」共用同一套规则（`msg_dedup`）。若数据已来自精简
        缓存（或已精简过），则跳过再次去重 —— 近似聚类是这里最贵的一步。
        """
        q = self.edit_q.text().strip().lower()
        kind = self.cmb_kind.currentData() or ""
        picked: list[dict] = []
        for m in self._all:
            fname = str(m.get("file", "") or "").strip()
            text = str(m.get("text", "") or "").strip()
            if not fname and not text:
                continue
            if kind and kind_of(fname)[0] != kind:
                continue
            if q and (q not in str(m.get("id", "")).lower()
                      and q not in fname.lower() and q not in text.lower()):
                continue
            picked.append(m)

        picked.sort(key=self._id_sort_key)        # 消息编号从小到大

        if self._pre_slimmed:                     # 已精简 → 不必再算
            # 无筛选时沿用精简阶段的合并数（筛选后该数字已无对应关系，报 0）
            filtered = bool(q) or bool(kind)
            self._dup_removed = (0 if filtered else
                                 int(self._slim_info.get("removed") or 0))
            return cluster_messages(picked)
        kept, dups = dedup_keep_ends(picked)
        self._dup_removed = dups
        return cluster_messages(kept)

    def _visible_msgs(self) -> list[dict]:
        """扁平化视图（供统计/测试使用）。"""
        return [m for g in self._visible_groups() for m in g]

    # ------------------------------------------------------------------ 渲染（分页）
    def teardown(self) -> None:
        """彻底结束当前文件的渲染与预览加载（切换文件 / 离开页面时调用）。"""
        self._timer.stop()
        self._render_timer.stop()
        self._queue.clear()
        self._rendering = False
        self._gi = 0
        self._groups = []
        self._open_list.clear()
        self._destroy_cards()

    def _destroy_cards(self) -> None:
        """销毁消息流里的全部卡片（翻页 / 重新加载时调用）。"""
        while self._host_lay.count():
            it = self._host_lay.takeAt(0)
            w = it.widget()
            if w is None:
                continue
            w.setParent(None)
            w.deleteLater()

    def _insert_card(self, w: QWidget) -> None:
        """插到末尾弹簧之前（弹簧始终是布局最后一项）。"""
        self._host_lay.insertWidget(max(0, self._host_lay.count() - 1), w)

    def rebuild(self) -> None:
        """重建视图：算出可见分组 → 渲染第 1 页（每页 CARD_PAGE 张卡）。"""
        self.teardown()
        self._groups = self._visible_groups()
        self._page_count = max(1, math.ceil(len(self._groups) / CARD_PAGE))
        self._page = 1
        self._refresh_info()
        self._sync_pager()
        if not self._groups:
            self.loaded.emit()
            return
        self._render_page(1)

    # ------------------------------------------------------------------ 后台卸载
    def release_memory(self) -> int:
        """切到后台（最小化 / 关闭到托盘）时卸载本页占用的内存。

        卸载的东西（都是大块头）：
          · 已渲染的消息卡片 —— Qt 部件树，页内最重的一块
          · `_all` —— 整个群的消息（含正文），大群可到几十 MB
          · `_index` / `_by_name` —— 文件索引（扫消息 JSON 所在文件夹建出来的，
            一个群几百上千个文件时也是上千个 Path 对象）
          · `_viewers` —— 已打开的内置查看器（各自持有解码后的图像 / 视频帧）
        保留 `_path`（恢复时要按它重新加载）、页码与筛选条件（属用户输入，不占内存）。
        返回卸载掉的条目数（0 = 本来就没加载东西）。
        """
        if self._released:
            return 0
        n = len(self._all) + len(self._index) + len(self._by_name)
        if n == 0 and not self._viewers and not self._groups:
            return 0
        self._page_keep = self._page
        self.teardown()                          # 停表 + 清队列/分组 + 销毁卡片
        for v in list(self._viewers):
            try:
                v.close()
                v.deleteLater()
            except (RuntimeError, AttributeError):
                pass
        self._viewers = []
        self._all = []
        self._index.clear()
        self._by_name.clear()
        self._slim_info = {}
        self._released = True
        self._sync_unload_btn()
        if self._path:
            # 界面上留个说明，不要留一片空白让人以为坏了
            self._scroll.setVisible(False)
            self._pager.setVisible(False)
            self._empty.setText(
                f"已进入后台，为节省内存卸载了「{Path(self._path).name}」的内容。\n\n"
                "回到窗口后会自动重新加载。")
            self._empty.setVisible(True)
            self._info.setText("已进入后台：消息内容已释放（回到窗口后自动重新加载）")
        return n

    def restore_memory(self) -> bool:
        """从后台回到前台时，把 `release_memory()` 卸载的内容重新加载回来。

        按卸载前的 `_path` 重新解析 JSON（走精简缓存，很快）并回到原来的页码。
        没有卸载过、或本来就没加载文件时返回 False（调用方不必提示）。
        """
        if not self._released:
            return False
        self._released = False
        path = self._path
        page = self._page_keep or 1
        if not path:
            self._empty.setText(self._empty_text())
            self._sync_unload_btn()
            return False
        ok = self.load(path)                     # teardown + 重新解析 + 重建第 1 页
        if ok and page > 1:
            self.go_page(page)                   # 回到卸载前那一页
        return ok

    def go_page(self, page: int) -> None:
        """跳到指定页（1 基；越界自动夹取）。"""
        if not self._groups:
            return
        try:
            p = int(page)
        except (TypeError, ValueError):
            return
        p = max(1, min(p, self._page_count))
        if p == self._page and not self._rendering:
            return
        self._render_page(p)
        self._scroll.verticalScrollBar().setValue(0)       # 翻页后回到顶部

    def _render_page(self, page: int) -> None:
        self._render_timer.stop()
        self._timer.stop()
        self._queue.clear()
        self._destroy_cards()
        self._open_list.clear()
        self._page = page
        self._page_start = (page - 1) * CARD_PAGE
        self._page_end = min(self._page_start + CARD_PAGE, len(self._groups))
        self._gi = self._page_start
        self._host_lay.addStretch(1)
        self._sync_pager()
        self._refresh_info()
        if self._page_end <= self._page_start:
            self._rendering = False
            self.loaded.emit()
            return
        self._rendering = True
        self._render_timer.start()

    def _render_batch(self) -> None:
        """每帧插入 RENDER_BATCH 张卡，直到本页（CARD_PAGE 张）渲染完。"""
        end = min(self._gi + RENDER_BATCH, self._page_end)
        for g in self._groups[self._gi:end]:
            locals_ = [self._local_of(m) for m in g]
            item = MsgItem(g, locals_)
            item.open_requested.connect(
                lambda i, it=item: self._open_at(it, i))
            item.tag_clicked.connect(self.search_tag)
            item.context_menu_requested.connect(self._show_card_menu)
            self._insert_card(item)
            idx = 0
            for m, p in zip(g, locals_):
                if str(m.get("file", "") or "").strip():
                    if p is not None:
                        # (路径, 消息编号)：查看器左右切换用，也用于「跳转到该条消息」
                        self._open_list.append((p, str(m.get("id", ""))))
                        self._queue.append(
                            (item, idx, p, kind_of(str(m.get("file", "")))[0]))
                    idx += 1
        self._gi = end
        self._refresh_info()

        if self._gi >= self._page_end:                # 本页渲染完成
            self._render_timer.stop()
            self._rendering = False
            self._sync_pager()
            self._refresh_info()
            self._prioritize()
            if self._queue:
                self._timer.start()
            self.loaded.emit()
            if self._pending_highlight:               # 查看器跳转过来 → 定位高亮
                mid, self._pending_highlight = self._pending_highlight, ""
                QTimer.singleShot(0, lambda m=mid: self._scroll_to_message(m))
        else:
            self._render_timer.start()

    def _on_page_jump(self) -> None:
        """页码输入框 → 跳转（非法输入回退显示当前页）。"""
        t = self.edit_page.text().strip()
        try:
            n = int(t)
        except ValueError:
            self.edit_page.setText(str(self._page))
            return
        self.go_page(n)
        self.edit_page.clear()

    def _sync_pager(self) -> None:
        """刷新分页器：页码显示 + 按钮可用性。"""
        has = bool(self._groups)
        self._pager.setVisible(has)
        if not has:
            return
        self.lbl_page.setText(f"第 {self._page} / {self._page_count} 页")
        self.btn_prev_page.setEnabled(self._page > 1)
        self.btn_next_page.setEnabled(self._page < self._page_count)
        if not self.edit_page.hasFocus():
            self.edit_page.setText(str(self._page))

    def _prioritize(self, _v: int = 0) -> None:
        """把预览加载队列按「离视口由近到远」排序（滚动时重排，可见的先加载）。"""
        if not self._queue:
            return
        vp = self._scroll.viewport()
        top = vp.rect().top() - 800
        bot = vp.rect().bottom() + 800

        def dist(t) -> float:
            item = t[0]
            try:
                y = item.mapTo(vp, QPoint(0, 0)).y()
                h = item.height()
            except RuntimeError:                      # 控件已销毁
                return 1e12
            if y + h < top:
                return top - (y + h)
            if y > bot:
                return y - bot
            return 0.0

        self._queue.sort(key=dist)

    def is_busy(self) -> bool:
        """仍在分批渲染卡片，或还有待加载的预览。"""
        return self._rendering or bool(self._queue)

    def _refresh_info(self) -> None:
        if not self._all:
            self._info.setText("尚未加载消息 JSON")
            self._empty.setText(self._empty_text())
            self._empty.setVisible(True)
            self._scroll.setVisible(False)
            return
        n_media = sum(1 for m in self._all if str(m.get("file", "") or "").strip())
        n_text = sum(1 for m in self._all if str(m.get("text", "") or "").strip())
        n_album = sum(1 for g in self._groups if len(g) > 1)
        total = len(self._groups)                          # 当前视图（筛选后）卡片总数
        shown = sum(len(g) for g in self._groups[:self._gi])   # 本页已渲染条数
        name = Path(self._path).name
        tail = (f" · {n_album} 个媒体组" if n_album else "")
        if self._dup_removed:
            tail += f" · 重复/近似文字已合并 {self._dup_removed} 条"
        cache_tag = ""
        if self._pre_slimmed:
            # 合并条数由上面的「已合并 N 条」统一表达，这里只标数据来源
            cache_tag = (" · 精简缓存" if self._slim_info.get("from_cache")
                         else " · 已生成精简缓存")
        page_txt = (f" · 第 {self._page}/{self._page_count} 页"
                    f"（{self._page_start + 1}-{self._page_end} / {total} 卡）"
                    if total else "")
        cur = f" · 本页渲染 {shown}/{self._page_end - self._page_start} 条" \
            if self._rendering else ""
        self._info.setText(
            f"{name} · 会话 {self._chat_id or '?'} · 共 {len(self._all)} 条"
            f"（含媒体 {n_media} · 含正文 {n_text}，空消息不显示）"
            f"{page_txt}{cur}{cache_tag}{tail}")
        has = bool(self._groups)
        if not has:
            if self.edit_q.text().strip() or (self.cmb_kind.currentData() or ""):
                self._empty.setText("没有匹配的消息：换个关键词，或把类型筛选调回「全部类型」。")
            else:
                self._empty.setText("这个导出文件里没有可显示的消息（空消息不显示）。")
        self._empty.setVisible(not has)
        self._scroll.setVisible(has)

    # ------------------------------------------------------------------ 查看器
    def _open_at(self, item: MsgItem, i: int) -> None:
        """点击媒体格 → 内置查看器（可在当前视图的媒体间左右切换）。"""
        if i < 0 or i >= len(item.media):
            return
        p = item.media[i][1]
        if p is None or not p.exists():
            return
        self.open_path(p)

    def open_path(self, path: Path) -> None:
        """用内置查看器打开；格式不支持或 QtMultimedia 缺失时退化为系统程序。"""
        self._prune_viewers()
        idx, items = 0, [(Path(path), "")]
        for i, pair in enumerate(self._open_list):
            if pair[0] == path:                        # _open_list = [(路径, 消息编号)]
                idx, items = i, list(self._open_list)
                break
        viewer = open_media(items, idx, self) if is_viewable(path) else None
        if viewer is None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        # 关闭即销毁（不留隐藏窗口）——注意销毁后 Python 包装会变成悬空引用，
        # 必须靠 destroyed 回调 / _prune_viewers 及时摘除，否则下次打开会抛
        # RuntimeError: Internal C++ object already deleted
        viewer.setAttribute(Qt.WA_DeleteOnClose, True)
        self._viewers.append(viewer)
        try:
            viewer.destroyed.connect(lambda *_: self._prune_viewers())
            viewer.goto_message.connect(self.jump_to_message)   # 「跳转到该条消息」
        except (RuntimeError, TypeError):              # pragma: no cover
            pass
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def _show_card_menu(self, item: MsgItem) -> None:
        """卡片右键菜单：跳转到该条消息 / 复制编号。"""
        if not item.msgs:
            return
        mid = str(item.msgs[0].get("id", ""))
        menu = QMenu(self)
        act_go = menu.addAction("跳转到该条消息")
        act_go.setEnabled(bool(mid))
        act_go.triggered.connect(
            lambda _=False, m=mid: self.jump_to_message(m, clear_filter=True))
        act_cp = menu.addAction("复制消息编号")
        act_cp.setEnabled(bool(mid))
        act_cp.triggered.connect(
            lambda _=False, m=mid: QGuiApplication.clipboard().setText(m))
        menu.exec(QCursor.pos())

    def jump_to_message(self, mid: str, clear_filter: bool = False) -> None:
        """跳回列表并定位：翻到该消息所在页 → 滚动 → 高亮。

        `clear_filter=True`（右键菜单用）会先清掉搜索与类型筛选 ——
        语义是「在完整列表里找到它」，而不是留在筛选结果里。
        """
        mid = str(mid or "").strip()
        if not mid:
            return
        if clear_filter and (self.edit_q.text().strip()
                             or (self.cmb_kind.currentData() or "")):
            self.cmb_kind.setCurrentIndex(0)     # 触发 rebuild（同步算出新分组）
            self.edit_q.setText("")              # 清搜索（同样触发 rebuild）
        if not self._groups:
            return
        for gi, g in enumerate(self._groups):
            if any(str(m.get("id", "")) == mid for m in g):
                page = gi // CARD_PAGE + 1
                if page != self._page or self._rendering:
                    self._pending_highlight = mid   # 翻页/渲染完成后定位（见 _render_batch）
                    self.go_page(page)
                else:
                    QTimer.singleShot(0, lambda: self._scroll_to_message(mid))
                return

    def _scroll_to_message(self, mid: str) -> None:
        """滚动到含指定消息编号的卡片并短暂高亮。"""
        for item in self._lay_items():
            if any(str(m.get("id", "")) == mid for m in item.msgs):
                try:
                    self._scroll.ensureWidgetVisible(item, 0, 80)
                    item.flash()
                except RuntimeError:               # 卡片已被翻页销毁
                    pass
                return

    def _lay_items(self) -> list[MsgItem]:
        """当前布局里的全部消息卡片（跳过弹簧）。"""
        out: list[MsgItem] = []
        for i in range(self._host_lay.count()):
            w = self._host_lay.itemAt(i).widget()
            if isinstance(w, MsgItem):
                out.append(w)
        return out

    def _prune_viewers(self) -> None:
        """摘除已关闭（C++ 对象已销毁）的查看器引用。"""
        alive: list[QDialog] = []
        for v in self._viewers:
            try:
                if v.isVisible():
                    alive.append(v)
            except RuntimeError:                       # 对象已随关闭销毁
                pass
        self._viewers = alive

    def search_tag(self, tag: str) -> None:
        """点 #标签 → 填入搜索框按该标签筛选（再点一次同一个标签则清空）。"""
        text = f"#{tag}"
        self.edit_q.setText("" if self.edit_q.text() == text else text)

    # ------------------------------------------------------------------ 懒加载
    def _load_batch(self) -> None:
        """按「离视口最近优先」加载预览（滚动时重排），视频每批限量以免长阻塞。"""
        if not self._queue:
            self._timer.stop()
            return
        batch: list[tuple] = []
        n_vid = 0
        i = 0
        while i < len(self._queue) and len(batch) < BATCH:
            t = self._queue[i]
            if t[3] == "视频":
                if n_vid >= VID_BATCH:                    # 本批视频配额已满，留给下一批
                    i += 1
                    continue
                n_vid += 1
            batch.append(t)
            del self._queue[i]
        for item, idx, path, kind_label in batch:
            try:
                done = item._flags[idx]
            except RuntimeError:                          # 控件已销毁（切文件）
                continue
            if done:
                continue
            if kind_label == "视频":
                pix = self._video_thumb(path)
                if pix is not None and not pix.isNull():
                    item.set_video_thumb(idx, pix)
                else:
                    pix = QPixmap(str(path))
                    item.set_video_thumb(idx, pix) if not pix.isNull() \
                        else item.set_missing(idx)
                continue
            pix = QPixmap(str(path))
            if pix.isNull():
                item.set_missing(idx)
            else:
                item.set_image(idx, pix)

    def _video_thumb(self, path: Path) -> QPixmap | None:
        exe = ffmpeg_path()
        if not exe:
            return None
        try:
            st = path.stat()
            key = hashlib.md5(
                f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
        except OSError:
            return None
        cache_dir = paths.res_root() / "thumb"
        # 分区存放：thumb/<哈希前 2 位>/<哈希>.jpg（256 个桶，避免单目录堆几千个文件）
        out = cache_dir / key[:THUMB_SHARD] / f"{key}.jpg"
        old = cache_dir / f"{key}.jpg"        # 分区前的平铺路径：老缓存继续可用
        if not out.exists() and not old.exists():
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                # CREATE_NO_WINDOW：GUI 进程（pythonw 无控制台）调 ffmpeg 时
                # Windows 会给子进程新开 console → 每张缩略图闪一个黑窗；
                # media_tools 里的调用都带了这个 flag，这里原先漏了
                subprocess.run(
                    [exe, "-y", "-loglevel", "error", "-ss", "0", "-i", str(path),
                     "-frames:v", "1", "-vf", "scale=840:-1", str(out)],
                    timeout=20, capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                return None
        target = out if out.exists() else (old if old.exists() else None)
        if target is None:
            return None
        pix = QPixmap(str(target))
        return pix if not pix.isNull() else None

    # ------------------------------------------------------------------ 状态
    @property
    def count(self) -> int:
        return len(self._all)

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def group_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self._visible_msgs():
            k = kind_of(str(m.get("file", "")))[0]
            out[k] = out.get(k, 0) + 1
        return out
