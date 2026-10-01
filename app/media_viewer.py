# -*- coding: utf-8 -*-
"""内置媒体查看器：**图片与视频共用同一个窗口**，按媒体序号连续切换。

交互（对照 Telegram 的习惯）：
- 上一项 / 下一项：在**整个媒体列表**里移动（图片、视频混排，一起走）
- 图片：←/→ 切换前后媒体；↑/↓ 缩放；滚轮以鼠标为中心缩放；拖动平移；双击 1:1
- 视频：←/→ **只做跳转**（前进/后退），单位可用按钮在「秒 / 帧」间切换，
        秒数由下拉框选（1 / 3 / 5 / 10 秒）；↑/↓ 调音量；空格播放/暂停；
        `,`/`.` 逐帧；F 全屏；`[`/`]` 上下一个媒体
- 「跳转到该条消息」：把当前媒体的消息编号发回消息浏览页定位

`open_media(items, index)`：统一入口；`items` 支持 `list[Path]` 或
`list[(Path, 消息编号)]`。QtMultimedia 缺失时视频返回 None（调用方退化到系统程序）。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSlider, QComboBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from . import prefs
from .form_renderer import NoWheelComboBox
from .media_tools import probe_stream
from .widgets import ElidedLabel, hand_cursor

try:                                                  # 调色用；缺失时自动降级（不崩）
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None                                         # type: ignore[assignment]

try:                                                  # QtMultimedia 在 PySide6-Addons 里
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    HAVE_QTMM = True
except ImportError:                                   # pragma: no cover - 环境缺模块时降级
    QAudioOutput = QMediaPlayer = QVideoSink = None   # type: ignore[assignment]
    HAVE_QTMM = False

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "tif", "tiff"}
VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg"}
AUDIO_EXTS = {"mp3", "m4a", "ogg", "wav", "flac", "aac", "opus", "wma"}

STEP_SECONDS = (1, 3, 5, 10)          # 跳秒下拉可选的秒数
# 播放倍速循环（「1×」按钮点一下切下一个）。宽度按这里最长的文案算，
# 见 _RATE_TEXT_MAX —— 写死宽度装不下「1.25×」会把按钮文字挤成两行。
RATES = (1.0, 1.25, 1.5, 2.0, 0.5)
_RATE_TEXT_MAX = max(f"{r:g}×" for r in RATES)

# ---- 窗口尺寸 / 音量记忆（存 resources/prefs.json）----
WIN_DEFAULT = (1080, 720)             # 首次打开（无记忆）时的窗口尺寸
WIN_MIN = (420, 300)                  # 「窗口缩小」的下限
WIN_ZOOM = 1.15                       # 每次「窗口放大 / 缩小」的比例
PREF_WIN = "viewer.win"               # [w, h] 常规尺寸（最大化时记的是还原后尺寸）
PREF_MAX = "viewer.max"               # 上次关闭时是否处于最大化
PREF_VOL = "viewer.vol"               # 音量 0~100
VOL_DEFAULT = 80

_BG = "#11151a"
_BAR = "#1b2129"
_FG = "#e6edf3"
_FG_DIM = "#93a1b0"
_ACCENT = "#2a8cf0"

_BTN_QSS = (
    f"QPushButton {{ color:{_FG}; background:#242c36; border:1px solid #313b47;"
    f" border-radius:5px; padding:4px 10px; font-size:12px; }}"
    f"QPushButton:hover {{ background:#2d3742; border-color:#3d4956; }}"
    f"QPushButton:pressed {{ background:{_ACCENT}; border-color:{_ACCENT}; }}"
    f"QPushButton:disabled {{ color:#5b6570; background:#1d232b; border-color:#2a323b; }}"
)
_SLIDER_QSS = (
    "QSlider::groove:horizontal { height:4px; background:#2a323b; border-radius:2px; }"
    f"QSlider::sub-page:horizontal {{ background:{_ACCENT}; border-radius:2px; }}"
    "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
    " background:#e6edf3; border-radius:6px; }"
    "QSlider::handle:horizontal:hover { background:#ffffff; }"
)
_COMBO_QSS = (
    f"QComboBox {{ color:{_FG}; background:#242c36; border:1px solid #313b47;"
    f" border-radius:5px; padding:3px 8px; font-size:12px; }}"
    "QComboBox QAbstractItemView { background:#1b2129; color:#e6edf3;"
    " selection-background-color:#2a8cf0; outline:none; }"
)


def _kind_of(path: str | Path) -> str:
    e = Path(path).suffix.lower().lstrip(".")
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    return ""


def is_viewable(path: str | Path) -> bool:
    """该文件是否能用内置查看器打开。"""
    k = _kind_of(path)
    return k == "image" or (k in ("video", "audio") and HAVE_QTMM)


def _hms(ms: int) -> str:
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _sz_text(n: int) -> str:
    v = float(max(0, n))
    for u in ("B", "KB", "MB", "GB"):
        if v < 1024 or u == "GB":
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} GB"


def _norm_items(items) -> list[tuple[Path, str]]:
    """统一成 [(Path, 消息编号)]；兼容传入 Path 列表。"""
    out: list[tuple[Path, str]] = []
    for it in items:
        if isinstance(it, (tuple, list)) and len(it) >= 1:
            out.append((Path(it[0]), str(it[1]) if len(it) > 1 else ""))
        else:
            out.append((Path(it), ""))
    return out


# ============================================================================
# 图片画布
# ============================================================================
class ImageCanvas(QWidget):
    """自绘图片画布：滚轮缩放（以鼠标为中心）+ 拖动平移 + 双击适应/1:1。"""

    scale_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._zoom = 1.0                       # 相对「适应窗口」的倍数（fit 模式）
        self._fit = True
        self._off = QPointF(0, 0)              # 平移量（屏幕像素）
        self._drag: QPointF | None = None
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setCursor(Qt.OpenHandCursor)

    # ---------------------------------------------------------------- 数据
    def set_pixmap(self, pix: QPixmap) -> None:
        self._pix = pix
        self.reset_fit()

    def has_image(self) -> bool:
        return self._pix is not None and not self._pix.isNull()

    def reset_fit(self) -> None:
        self._fit = True
        self._zoom = 1.0
        self._off = QPointF(0, 0)
        self.update()
        self.scale_changed.emit(self.eff_scale())

    def set_actual_size(self) -> None:
        """1:1 实际像素。"""
        self._fit = False
        self._zoom = 1.0
        self._off = QPointF(0, 0)
        self.update()
        self.scale_changed.emit(self.eff_scale())

    def zoom_by(self, factor: float, pos: QPointF | None = None) -> None:
        if self._pix is None:
            return
        old = self.eff_scale()
        new = max(0.02, min(24.0, old * factor))
        k = new / old
        center = QPointF(pos) if pos is not None else QPointF(
            self.width() / 2, self.height() / 2)
        m = QPointF(center.x() - self.width() / 2, center.y() - self.height() / 2)
        # 保持鼠标下的图像点不动：off' = m(1-k) + off·k
        self._off = QPointF(m.x() * (1 - k) + self._off.x() * k,
                            m.y() * (1 - k) + self._off.y() * k)
        self._fit = False
        self._zoom = new
        self.update()
        self.scale_changed.emit(new)

    def eff_scale(self) -> float:
        if self._pix is None or self._pix.width() == 0 or self._pix.height() == 0:
            return 1.0
        if self._fit:
            return min(self.width() / self._pix.width(),
                       self.height() / self._pix.height())
        return self._zoom

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_BG))
        if self._pix is None or self._pix.isNull():
            return
        s = self.eff_scale()
        w, h = self._pix.width() * s, self._pix.height() * s
        cx = self.width() / 2 + self._off.x()
        cy = self.height() / 2 + self._off.y()
        target = QRectF(cx - w / 2, cy - h / 2, w, h)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(target, self._pix, QRectF(0, 0, self._pix.width(), self._pix.height()))

    # ---------------------------------------------------------------- 交互
    def wheelEvent(self, e) -> None:
        self.zoom_by(1.25 if e.angleDelta().y() > 0 else 1 / 1.25, e.position())
        e.accept()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._pix is not None:
            self._drag = e.position()
            self.setCursor(Qt.ClosedHandCursor)
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self._off += e.position() - self._drag
            self._drag = e.position()
            self.update()
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)
        e.accept()

    def mouseDoubleClickEvent(self, e) -> None:
        if self._fit:
            self.set_actual_size()
        else:
            self.reset_fit()
        e.accept()


# ============================================================================
# 视频画布（与图片同一套缩放 / 平移 + 画面调节）
# ============================================================================
# 处理分辨率的**各档上限**（真正的目标由 `_target_size()` 按「屏幕上画多大」算，见下）。
# ⚠️ 上限只是**兜底**，不是目标：实测「缩放 + QPixmap」只要 5~7 ms，贵的 `toImage()`
#    跟处理分辨率无关 —— 所以没必要靠压低分辨率来省算力（那是 2.9.8 之前的误区）。
#    但**也不能完全不设**：不调色时若不限，8K 源会拖垮帧队列（2.9.8 的坑）。
#   · 播放中 · 调色    1280×720   —— 三项全开约 45 ms/帧；再高就拖不动调色滑块
#   · 播放中 · 不调色  3840×2160  —— 处理几乎零成本，给足清晰度
#   · 暂停中 · 不调色  3840×2160
#   · 暂停中 · 调色    1920×1080  —— **保持 1920 不动**：4K 全量单次调色 420 ms，
#     拖滑块会不断积压（2.5.4 好不容易调出来的，别放）
PLAY_MAX_W, PLAY_MAX_H = 1280, 720                # 播放中 · 调色
PLAY_MAX_W_PLAIN, PLAY_MAX_H_PLAIN = 3840, 2160   # 播放中 · 不调色
IDLE_MAX_W, IDLE_MAX_H = 3840, 2160               # 暂停中 · 不调色
IDLE_MAX_W_COLOR, IDLE_MAX_H_COLOR = 1920, 1080   # 暂停中 · 调色

# 处理分辨率的目标 = 屏幕上真正画出来的尺寸 × 缩放倍率，倍率封顶这么多倍。
# 放大时需要更多源像素才不糊，但继续往上就只是跟播放帧率抢 CPU 了。
ZOOM_FOR_QUALITY = 2.0

# 调色重绘的节流下限（实际间隔取 max(此值, 上次耗时×1.2)，见 _schedule_render）
RENDER_MIN_MS = 80

# 节流 tick 的周期（只做时间比较，很轻）；实际渲染间隔由 _on_tick 决定
TICK_MS = 50

# 收帧处理的最小间隔 = **丢帧保护**（2.5.3）。处理一帧（toImage + 缩放 + 调色 +
# QPixmap）在 1080p 要 27~40 ms，而视频按 30/60 fps 推帧：每帧都处理就会在 Qt 事件
# 队列里越积越多 —— 表现为「刚调色还行，多次调节后越来越卡、最终卡死」（用户实测）。
# 宁可把有效帧率降到 ~24fps，也绝不让队列堆积。
FRAME_MIN_INTERVAL = 1 / 24            # 调色时
FRAME_MIN_INTERVAL_PLAIN = 1 / 60      # 未调色时（处理几乎零成本，基本不丢）

# 自适应丢帧系数（2.9.8）：实际间隔 = max(上面的基准, **上帧实测整帧耗时** × 此系数)。
# ⚠️ 必须量**整帧**（toImage + 缩放 + 调色 + QPixmap），不能只看调色那一段 ——
#    4K MOV 的瓶颈主要在 `QVideoFrame.toImage()` 的格式转换与整帧拷贝上。
# 留 40% 余量给绘制与事件调度；这样 40 ms/帧的视频会自动落到 ~18fps，队列不再积压。
FRAME_COST_FACTOR = 1.4


def _apply_color(img: QImage, bright: int, contrast: int, sat: int) -> QImage:
    """按滑块值调整画面：亮度 / 对比度 / 饱和度（numpy 加速）。

    滑块范围 -100..100，0 = 原样；三者全为 0（或环境里没有 numpy）时**原样返回**，
    不做任何拷贝 —— 不调色就不付性能代价。

    ⚠️ 性能：亮度/对比度是**逐像素单调映射**，所以预计算 256 项查找表一次索引搞定。
    早期版本直接在大数组上做 float 运算，实测 1080p **109 ms/帧（上限 9 fps）**，
    拖调色滑块时主线程被堵死、界面像卡住 —— 改 LUT 后降到 ~15 ms。
    饱和度涉及通道混合，仍需 float，但只在 sat != 0 时才做。
    公式：对比度 c=(100+contrast)/100 → (in-128)·c+128；亮度 += bright·1.2；
         饱和度 out = lum + (in-lum)·s，lum 取 Rec.601 权重（s=0 即灰度）。
    """
    if np is None or (bright == 0 and contrast == 0 and sat == 0):
        return img
    img = img.convertToFormat(QImage.Format_RGB32)      # 统一 32bpp 便于原地改
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return img
    arr = np.frombuffer(img.bits(), dtype=np.uint8).reshape(h, w, 4)   # 小端 BGRA
    bgr = arr[:, :, :3]                                 # 视图：写回即改原图
    if bright or contrast:
        lut = np.clip((np.arange(256, dtype=np.float32) - 128.0)
                      * ((100.0 + contrast) / 100.0) + 128.0 + bright * 1.2,
                      0, 255).astype(np.uint8)
        bgr[:] = lut[bgr]                               # 查表，比大数组 float 快一个量级
    if sat:
        s = (100.0 + sat) / 100.0
        f = bgr.astype(np.float32)
        lum = f @ np.array([0.114, 0.587, 0.299], dtype=np.float32)   # (h, w)
        f = lum[..., None] + (f - lum[..., None]) * s
        np.clip(f, 0, 255, out=f)
        bgr[:] = f.astype(np.uint8)
    return img


class VideoCanvas(ImageCanvas):
    """视频画布：QVideoSink 收帧 →（可选）调色 → 复用图片那套缩放 / 拖拽平移。

    为什么不用 QVideoWidget：Qt6 的 QVideoWidget 既不能缩放平移，也没有任何
    画面调节接口。改成自绘画布后，视频与图片的交互**完全一致**（滚轮缩放、
    拖拽平移、双击 1:1、↑/↓ 缩放），调色也只需在帧上做一次向量运算。
    代价是逐帧过一遍 Python —— 换来功能完整与操作统一，值得。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._has_frame = False
        self._raw = None                          # 最近一帧的原始图（改参数时重新调色用）
        self._bright = self._contrast = self._sat = 0     # 0 = 原样
        self._press = None
        self._dragged = False
        self._last_frame = 0.0                    # 最近一次收帧时刻（判断是否在播放）
        self._last_render = 0.0                   # 最近一次处理帧的时刻（丢帧保护用）
        self._render_cost = 0.0                   # 上次渲染实测耗时（自适应节流用）
        self._frame_cost = 0.0                    # 上帧**整帧**实测耗时（自适应丢帧用）
        self._last_paint = 0.0                    # 上次渲染时刻（节流判据）
        self._dirty = False                       # 有待生效的调色改动
        self._tick = None                         # 节流 tick（见 _ensure_tick）
        self.setCursor(Qt.OpenHandCursor)
        self.sink = QVideoSink(self) if HAVE_QTMM else None
        if self.sink is not None:
            self.sink.videoFrameChanged.connect(self._on_frame)

    # ---------------------------------------------------------------- 画面调节
    def set_params(self, bright: int = 0, contrast: int = 0, sat: int = 0) -> None:
        """更新调节参数。

        · **播放中**：只记参数 —— 下一帧会自动带上新参数，不必现在重算
          （重算一次要十几毫秒，会直接把渲染线程堵住 → 界面像卡死）
        · **暂停时**：节流重绘（最多每 80ms 一次），拖滑块既有实时反馈又不会冻结
        """
        if (bright, contrast, sat) == (self._bright, self._contrast, self._sat):
            return
        self._bright, self._contrast, self._sat = bright, contrast, sat
        if time.monotonic() - self._last_frame < 0.5:
            return                                 # 正在播放：等下一帧自然带上
        self._dirty = True
        self._ensure_tick()                        # 交给 tick 按自适应间隔渲染

    def _ensure_tick(self) -> None:
        """确保节流 tick 在跑（周期 50ms，只做一次时间比较，开销可忽略）。

        ⚠️ 不要用「单次 QTimer + isActive() 判断」：QTimer 在 timeout 信号处理
        期间 isActive() 仍为 True，`if not isActive(): start()` 会**触发一次后
        再也起不来**（实测拖滑块只刷新一次）。周期 tick + 时间戳判断没有这个坑。
        """
        if self._tick is None:
            self._tick = QTimer(self)
            self._tick.setInterval(TICK_MS)
            self._tick.timeout.connect(self._on_tick)
        if not self._tick.isActive():
            self._tick.start()

    def _on_tick(self) -> None:
        """按**自适应间隔**渲染待生效的调色改动。

        间隔 = max(RENDER_MIN_MS, 上次实测耗时 × 1.2) —— 处理慢就自动拉长，
        绝不让请求堆积（固定 80ms 遇上 4K 单次 420ms 会越拖越卡）。
        """
        if not self._dirty:
            return
        gap = max(RENDER_MIN_MS / 1000.0, self._render_cost * 1.2)
        if time.monotonic() - self._last_paint >= gap:
            self._dirty = False
            self._render()

    def params(self) -> tuple[int, int, int]:
        return self._bright, self._contrast, self._sat

    # ---------------------------------------------------------------- 收帧
    def _on_frame(self, frame) -> None:
        """收帧 → （可调色）→ 显示。

        ⚠️ **丢帧保护**：处理一帧（`toImage()` 拷贝 + 缩放 + 调色 + `QPixmap` 转换）
        在 1080p 下要 27~40 ms、4K 要几十毫秒，而视频按 30/60 fps 推帧 —— 每帧都处理
        就会在 Qt 事件队列里**越积越多**（每个队列项还攥着帧缓冲），表现就是
        「越播越卡、内存涨到程序退出」（用户实测）。
        间隔由 `_frame_gap()` 给：**按上帧实测的整帧耗时自适应**（2.9.8），
        宁可把有效帧率降下来，也绝不让事件排队堆积。
        丢帧路径只做一次时间比较（微秒级），所以队列能迅速排空。
        """
        now = time.monotonic()
        self._last_frame = now                     # 供 _is_playing() 判断播放状态
        if now - self._last_render < self._frame_gap():
            return                                 # 丢帧：不拷贝、不调色，立即返回
        self._last_render = now
        if not frame.isValid():
            return
        t0 = now
        try:
            img = frame.toImage()
            if img.isNull():
                return
            self._raw = img                        # 留底，调参时不用等下一帧
            self._render()
        finally:
            # 整帧成本（toImage + 缩放 + 调色 + QPixmap）→ 下一帧的自适应间隔
            self._frame_cost = time.monotonic() - t0

    def _target_size(self, src_w: int, src_h: int) -> tuple[int, int]:
        """这一帧该按多大分辨率处理 —— 目标是「屏幕上真正画出来的像素数」。

        处理得比屏幕大＝白费算力（还多一道重采样）；比屏幕小＝怎么都要再放大一次、更糊。
        所以按显示尺寸取（2.9.9 起）：
          · 适应窗口：画布尺寸 × 设备像素比 × 缩放倍率（倍率封顶 ZOOM_FOR_QUALITY）
          · 实际尺寸(1:1)：直接按源分辨率 —— 屏幕上就是 1:1 放，要的就是原像素
        """
        if not self._fit:
            return max(1, int(src_w)), max(1, int(src_h))
        dpr = float(self.devicePixelRatioF() or 1.0)
        z = max(1.0, min(float(self._zoom or 1.0), ZOOM_FOR_QUALITY))
        return (max(1, int(round(self.width() * dpr * z))),
                max(1, int(round(self.height() * dpr * z))))

    def _render(self) -> None:
        """按当前参数把最近一帧画出来（保持用户已有的缩放 / 平移视角）。

        处理分辨率 = min(按显示尺寸算出的目标, 本档上限)：
          · 播放中 · 调色    1280×720
          · 播放中 · 不调色  3840×2160
          · 暂停中 · 不调色  3840×2160
          · 暂停中 · 调色    1920×1080（4K 全量单次调色 420 ms，拖滑块会积压）

        ⚠️ 三条历史教训，都别再犯：
          ① **上限不能只在调色时生效**（2.9.8）：不调色时 4K MOV 每帧按 3840×2160
             全量处理 → 主线程压满 → 帧事件无界积压 →「越播越卡、最后程序退出」。
          ② **缩放必须用平滑**（2.9.9）：最近邻降采样有锯齿、播放时还闪；
             实测平滑只贵 ~1.6 ms（实测 5.6 vs 4.0 ms 量级），画质差别却很明显。
          ③ **别固定成一个拍脑袋的分辨率**（2.9.9）：比屏幕大是浪费、比屏幕小是白糊；
             按显示尺寸取，既不浪费也不糊。
        `_raw` 一直保留全分辨率原帧，所以这里限的只是**处理分辨率**：
        暂停 / 放大时会按需重渲出更清晰的图（见 on_playback_paused / _request_render）。
        """
        if self._raw is None:
            return
        src = self._raw
        tw, th = self._target_size(src.width(), src.height())
        if not self._is_playing():
            if self._needs_color():
                lim_w, lim_h = IDLE_MAX_W_COLOR, IDLE_MAX_H_COLOR
            else:
                lim_w, lim_h = IDLE_MAX_W, IDLE_MAX_H
        elif self._needs_color():
            lim_w, lim_h = PLAY_MAX_W, PLAY_MAX_H
        else:
            lim_w, lim_h = PLAY_MAX_W_PLAIN, PLAY_MAX_H_PLAIN
        cap_w, cap_h = max(1, min(tw, lim_w)), max(1, min(th, lim_h))
        if src.width() > cap_w or src.height() > cap_h:
            src = src.scaled(cap_w, cap_h, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        t0 = time.monotonic()
        img = _apply_color(src, self._bright, self._contrast, self._sat)
        self._pix = QPixmap.fromImage(img)
        self._render_cost = time.monotonic() - t0   # 实测耗时 → 供自适应节流
        self._last_paint = time.monotonic()          # 本次渲染时刻（节流判据）
        if not self._has_frame:                    # 首帧：按窗口适应一次
            self._has_frame = True
            self.reset_fit()
            # ⚠️ reset_fit() 已被重写成「顺手请求一次重渲」（窗口尺寸/缩放变化时要用），
            #    但这里**刚按当前尺寸渲完** —— 不清掉就又排一轮，白做一次。
            self._dirty = False
        else:
            self.update()                          # 后续帧只刷新画面，**保持**缩放 / 平移

    def _frame_gap(self) -> float:
        """两帧之间至少隔多久才值得处理 = max(基准间隔, 上帧整帧耗时 × 1.4)。

        目的：处理一帧要 40 ms 时，把有效帧率自动降到 ~18fps —— 宁可掉帧，也绝不让
        Qt 的帧事件在队列里越积越多（积压 = 越播越卡，最后内存涨爆 / 程序退出）。
        ⚠️ 用的是**整帧**成本（`_frame_cost`，含 toImage / 缩放 / 调色 / QPixmap），
        不是只有调色那段的 `_render_cost` —— 4K 的瓶颈主要在 `toImage()`。
        """
        base = FRAME_MIN_INTERVAL if self._needs_color() else FRAME_MIN_INTERVAL_PLAIN
        return max(base, self._frame_cost * FRAME_COST_FACTOR)

    def on_playback_paused(self) -> None:
        """播放暂停 / 停止 / 结束 → 按「暂停档」重渲一次。

        播放中的上限更保守（调色 1280×720）；停下来了就没必要再糊着 —— 按暂停档
        重渲（`_raw` 一直是全分辨率原帧，只是把降采样那一步放宽）。
        同时把 `_last_frame` 归零，让 `_is_playing()` 立刻变 False。
        """
        if self._raw is None:
            return
        self._last_frame = 0.0
        self._render()

    def _request_render(self) -> None:
        """请求按**当前尺寸**重渲一次（暂停态才需要）。

        处理分辨率是按显示尺寸算的（见 `_target_size`），所以窗口大小 / 缩放倍率一变，
        就该按新尺寸重画一帧才够清晰。播放中不用管 —— 下一帧自然带上。
        走既有的「周期 tick + 自适应间隔」节流，连点缩放也不会堆积。
        """
        if self._raw is None or self._is_playing():
            return
        self._dirty = True
        self._ensure_tick()

    def scale_to_source(self, s: float) -> float:
        """把「相对当前处理图」的缩放换算成「相对原片」的缩放（信息条显示用）。

        2.9.9 起处理分辨率按显示尺寸算，适应窗口时 `eff_scale()` 恒为 ~1.0 ——
        直接显示会永远写「100%」，看不出实际是原片的多少（4K 片缩进窗口通常只有
        20~40%）。这里按 `_raw` 的真实尺寸换算回来。
        """
        if self._raw is None or self._pix is None:
            return s
        if not self._raw.width() or not self._pix.width():
            return s
        return s * self._pix.width() / self._raw.width()

    # ---- 窗口尺寸 / 缩放变化 → 暂停时按新尺寸重渲（播放中下一帧自然带上）----
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._request_render()

    def zoom_by(self, factor: float, pos=None) -> None:
        super().zoom_by(factor, pos)
        self._request_render()

    def reset_fit(self) -> None:
        super().reset_fit()
        self._request_render()

    def set_actual_size(self) -> None:
        super().set_actual_size()
        self._request_render()

    def _needs_color(self) -> bool:
        """有任一调节参数非 0（全 0 时不调色、也不做多余的降采样）。"""
        return bool(self._bright or self._contrast or self._sat)

    def _is_playing(self) -> bool:
        """近 0.5 秒内收到过帧 → 认为在播放（不需要外部通知播放状态）。"""
        return (time.monotonic() - self._last_frame) < 0.5

    def clear_frame(self) -> None:
        """切换媒体时清掉上一段画面与视角。"""
        if self._tick is not None:
            self._tick.stop()                      # 别让遗留的 tick 再处理上一段画面
        self._dirty = False
        self._last_paint = 0.0
        self._has_frame = False
        self._raw = None
        self._pix = None
        self._last_frame = 0.0
        self._last_render = 0.0
        self._render_cost = 0.0
        self._frame_cost = 0.0
        self._zoom = 1.0
        self._fit = True
        self._off = QPointF(0, 0)
        self.update()

    def has_image(self) -> bool:
        return self._has_frame

    # ---------------------------------------------------------------- 交互
    def mousePressEvent(self, e) -> None:
        self._press = e.position()
        self._dragged = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._press is not None and \
                (e.position() - self._press).manhattanLength() > 4:
            self._dragged = True                 # 动了就算拖拽，不算「点击」
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton and not self._dragged:
            self.clicked.emit()                  # 单击（没拖动）→ 播放 / 暂停
        self._press = None


# ============================================================================
# 统一查看器（图片 + 视频同窗口）
# ============================================================================
class MediaViewer(QDialog):
    """图片 / 视频共用的查看器窗口（上一项、下一项在混合列表里移动）。"""

    goto_message = Signal(str)             # 「跳转到该条消息」→ 消息编号

    def __init__(self, items, index: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("媒体查看")
        self.setWindowFlag(Qt.Window, True)          # 独立顶层窗口（父在滚动区内）
        self._items = _norm_items(items)
        if not self._items:
            raise ValueError("没有可查看的媒体")
        self._index = max(0, min(index, len(self._items) - 1))
        self._fps = 0.0
        self._duration_ms = 0
        self._seeking = False
        self._rate = 1.0
        self._full = False
        self._step_unit = "sec"                      # sec | frame
        self._nav_btn = None                         # 翻页后按住不放的按钮（加载完弹起）
        self._shown = False                          # 首次显示时才恢复最大化状态
        self._want_max = prefs.get_bool(PREF_MAX, False)
        # 常规尺寸（非最大化/全屏时跟踪；最大化时也要记着还原后的尺寸，供下次用）
        self._norm_size = list(self._win_pref())
        self.setStyleSheet(f"QDialog {{ background:{_BG}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- 顶部信息条 ----------------
        top = QFrame()
        top.setStyleSheet(f"QFrame {{ background:{_BAR}; border:none; }}")
        top.setFixedHeight(36)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(12, 0, 12, 0)
        tl.setSpacing(10)
        self.lbl_title = ElidedLabel("", color=_FG)     # 长文件名省略，不顶最小宽度
        _f = self.lbl_title.font()
        _f.setPixelSize(12)
        self.lbl_title.setFont(_f)
        tl.addWidget(self.lbl_title, 1)
        self.lbl_meta = ElidedLabel("", color=_FG_DIM)  # 同理：不撑最小宽度
        _f2 = self.lbl_meta.font()
        _f2.setPixelSize(11)
        self.lbl_meta.setFont(_f2)
        tl.addWidget(self.lbl_meta)
        self.btn_goto = QPushButton("跳转到该条消息")
        self.btn_goto.setStyleSheet(_BTN_QSS)
        self.btn_goto.setFixedHeight(24)
        hand_cursor(self.btn_goto)
        self.btn_goto.clicked.connect(self.emit_goto)
        tl.addWidget(self.btn_goto)

        # 窗口尺寸控件（自带按钮，不依赖系统标题栏）；尺寸会被记住，下次沿用。
        # 标签用「窗口－/窗口＋」而不是「缩小/放大」——后者已被图片缩放占用。
        self.btn_win_out = QPushButton("窗口－")
        self.btn_win_in = QPushButton("窗口＋")
        self.btn_win_max = QPushButton("最大化")
        for b in (self.btn_win_out, self.btn_win_in, self.btn_win_max):
            b.setStyleSheet(_BTN_QSS)
            b.setFixedHeight(24)
            hand_cursor(b)
        self.btn_win_out.clicked.connect(lambda: self.scale_window(1 / WIN_ZOOM))
        self.btn_win_in.clicked.connect(lambda: self.scale_window(WIN_ZOOM))
        self.btn_win_max.clicked.connect(self.toggle_max)
        tl.addWidget(self.btn_win_out)
        tl.addWidget(self.btn_win_in)
        tl.addWidget(self.btn_win_max)

        self.btn_close = QPushButton("关闭")
        self.btn_close.setStyleSheet(_BTN_QSS)
        self.btn_close.setFixedHeight(24)
        hand_cursor(self.btn_close)
        self.btn_close.clicked.connect(self.close)
        tl.addWidget(self.btn_close)
        root.addWidget(top)

        # ---------------- 内容区（图片 / 视频 共用一个窗口） ----------------
        self.stack = QStackedWidget()
        self.canvas = ImageCanvas(self)
        self.canvas.scale_changed.connect(self._on_scale)
        self.stack.addWidget(self.canvas)            # 0
        self._video_host = QWidget()
        vh = QVBoxLayout(self._video_host)
        vh.setContentsMargins(0, 0, 0, 0)
        if HAVE_QTMM:
            # 视频用自绘画布：QVideoSink 收帧 →（可选）调色 → 与图片同一套缩放/平移。
            # 缩放百分比也走 scale_changed，与图片共用右下角的提示。
            self.video = VideoCanvas(self._video_host)
            self.video.clicked.connect(self.toggle_play)   # 点画面 → 播放/暂停
            self.video.scale_changed.connect(self._on_scale)
            vh.addWidget(self.video, 1)
            # 音频没有画面，画布只会是一块黑 —— 叠一个占位标签，
            # 显示 🎵 + 文件名，让「音频正在此查看器里播放」看得见。
            # 点击穿透（WA_TransparentForMouseEvents），点它 = 点视频区 = 播放/暂停。
            self.lbl_audio = QLabel("", self._video_host)
            self.lbl_audio.setAlignment(Qt.AlignCenter)
            self.lbl_audio.setWordWrap(True)
            self.lbl_audio.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.lbl_audio.setStyleSheet(
                f"color:{_FG_DIM}; font-size:14px; background:{_BG};")
        else:                                        # pragma: no cover
            self.video = None
            self.lbl_audio = None
        self.stack.addWidget(self._video_host)       # 1
        root.addWidget(self.stack, 1)

        # ---------------- 底部控制区 ----------------
        ctl = QFrame()
        ctl.setStyleSheet(f"QFrame {{ background:{_BAR}; border:none; }}")
        cl = QVBoxLayout(ctl)
        cl.setContentsMargins(12, 6, 12, 8)
        cl.setSpacing(6)

        # 进度行（仅视频 / 音频）
        self.row_seek = QWidget()
        rs = QHBoxLayout(self.row_seek)
        rs.setContentsMargins(0, 0, 0, 0)
        rs.setSpacing(8)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setStyleSheet(_SLIDER_QSS)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._on_seek_end)
        self.slider.sliderMoved.connect(self._on_seek_move)
        rs.addWidget(self.slider, 1)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setStyleSheet(
            f"color:{_FG}; font-size:12px; font-family:Consolas,monospace;"
            "background:transparent;")
        self.lbl_time.setMinimumWidth(106)
        rs.addWidget(self.lbl_time)
        cl.addWidget(self.row_seek)

        # 按钮行
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_prev = QPushButton("‹ 上一项")
        self.btn_next = QPushButton("下一项 ›")
        self.btn_play = QPushButton("▶ 播放")
        for b in (self.btn_prev, self.btn_next, self.btn_play):
            b.setStyleSheet(_BTN_QSS)
            b.setFixedHeight(28)
            hand_cursor(b)
        self.btn_play.setMinimumWidth(84)
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.btn_play.clicked.connect(self.toggle_play)
        # 顺序：上一项 → 下一项 → 播放。
        # 「上一项 / 下一项」紧挨着，连续翻页时手指不用跨过播放键（避免误触播放）；
        # 图片和视频共用同一套按钮与位置，切换后翻页手感一致。
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        row.addWidget(self.btn_play)
        # 视频跳转控件（步长标签 + 秒数下拉 + 单位切换 + 前后跳转）：
        # 与主按钮同处一行 —— 之前独立成一行是为了窗口能缩得更窄，
        # 但两行按钮太占高度，合并后最小宽度约 900+（默认 1080 宽窗口放得下）。
        self.lbl_step = QLabel("跳转步长")
        self.lbl_step.setStyleSheet(f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
        row.addWidget(self.lbl_step)
        self.cmb_sec = NoWheelComboBox()
        self.cmb_sec.setStyleSheet(_COMBO_QSS)
        self.cmb_sec.setFixedHeight(28)
        self.cmb_sec.setFixedWidth(78)
        for s in STEP_SECONDS:
            self.cmb_sec.addItem(f"{s} 秒", s)
        self.cmb_sec.setCurrentIndex(0)
        self.cmb_sec.currentIndexChanged.connect(lambda _=0: self._sync_step_ui())
        row.addWidget(self.cmb_sec)
        self.btn_unit = QPushButton("单位：秒")
        self.btn_unit.setStyleSheet(_BTN_QSS)
        self.btn_unit.setFixedHeight(28)
        hand_cursor(self.btn_unit)
        self.btn_unit.clicked.connect(self.toggle_step_unit)
        row.addWidget(self.btn_unit)
        self.btn_back = QPushButton("◀ 后退")
        self.btn_fwd = QPushButton("前进 ▶")
        for b in (self.btn_back, self.btn_fwd):
            b.setStyleSheet(_BTN_QSS)
            b.setFixedHeight(28)
            b.setMinimumWidth(72)
            hand_cursor(b)
        self.btn_back.clicked.connect(lambda: self.seek_step(-1))
        self.btn_fwd.clicked.connect(lambda: self.seek_step(1))
        row.addWidget(self.btn_back)
        row.addWidget(self.btn_fwd)
        row.addStretch(1)

        # 缩放（作用于**当前画布**：图片与视频共用同一套缩放/平移交互）
        self.btn_zoom_out = QPushButton("缩小")
        self.btn_zoom_in = QPushButton("放大")
        self.btn_fit = QPushButton("适应窗口")
        self.btn_100 = QPushButton("1:1")
        for b in (self.btn_zoom_out, self.btn_zoom_in, self.btn_fit, self.btn_100):
            b.setStyleSheet(_BTN_QSS)
            b.setFixedHeight(28)
            hand_cursor(b)
        self.btn_zoom_in.clicked.connect(lambda: self._zoom_by(1.25))
        self.btn_zoom_out.clicked.connect(lambda: self._zoom_by(1 / 1.25))
        self.btn_fit.clicked.connect(self._reset_fit)
        self.btn_100.clicked.connect(self._actual_size)
        row.addWidget(self.btn_zoom_out)
        row.addWidget(self.btn_zoom_in)
        row.addWidget(self.btn_fit)
        row.addWidget(self.btn_100)

        # 视频专用：倍速 / 音量 / 全屏
        self.btn_rate = QPushButton("1×")
        self.btn_rate.setStyleSheet(_BTN_QSS)
        self.btn_rate.setFixedHeight(28)
        # ⚠️ 不要写死宽度：文字会在 1× / 1.25× / 1.5× / 2× / 0.5× 之间切换，
        # 按「1×」定的 46px 装不下「1.25×」（实测需 80px），
        # 文字会被挤成两行（按钮高度只有 28px，下半截还会被裁掉）。
        # 按最长文案留足，这样以后改 RATES 也不会再出问题。
        self.btn_rate.setMinimumWidth(
            self.btn_rate.fontMetrics().horizontalAdvance(_RATE_TEXT_MAX) + 22)
        hand_cursor(self.btn_rate)
        self.btn_rate.clicked.connect(self.cycle_rate)
        row.addWidget(self.btn_rate)
        self.lbl_vol = QLabel("音量")
        self.lbl_vol.setStyleSheet(f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
        row.addWidget(self.lbl_vol)
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setStyleSheet(_SLIDER_QSS)
        self.slider_vol.setFixedWidth(96)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(prefs.get_int(PREF_VOL, VOL_DEFAULT, 0, 100))  # 记忆音量
        row.addWidget(self.slider_vol)
        self.btn_full = QPushButton("全屏")
        self.btn_full.setStyleSheet(_BTN_QSS)
        self.btn_full.setFixedHeight(28)
        hand_cursor(self.btn_full)
        self.btn_full.clicked.connect(self.toggle_full)
        row.addWidget(self.btn_full)
        cl.addLayout(row)

        # 画面调节行（仅视频）：亮度 / 对比度 / 饱和度 + 重置
        # 三个滑块都取 -100..100（0 = 原样），拖动即时生效 —— 暂停时也会立刻重绘
        # 当前帧，不用等下一帧（否则用户会以为没反应）。
        self.row_color = QWidget()
        rq = QHBoxLayout(self.row_color)
        rq.setContentsMargins(0, 0, 0, 0)
        rq.setSpacing(6)
        self.lbl_color = QLabel("画面")
        self.lbl_color.setStyleSheet(
            f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
        rq.addWidget(self.lbl_color)
        self.sl_bright = QSlider(Qt.Horizontal)
        self.sl_contrast = QSlider(Qt.Horizontal)
        self.sl_sat = QSlider(Qt.Horizontal)
        for _name, _sl in (("亮度", self.sl_bright), ("对比度", self.sl_contrast),
                           ("饱和度", self.sl_sat)):
            _lb = QLabel(_name)
            _lb.setStyleSheet(
                f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
            rq.addWidget(_lb)
            _sl.setStyleSheet(_SLIDER_QSS)
            _sl.setRange(-100, 100)
            _sl.setValue(0)
            _sl.setFixedWidth(100)
            _sl.setToolTip(f"{_name}：左右拖动调节（0 为原样）；点「重置画面」还原")
            _sl.valueChanged.connect(self._on_color)
            rq.addWidget(_sl)
        self.lbl_color_val = QLabel("原样")
        self.lbl_color_val.setStyleSheet(
            f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
        self.lbl_color_val.setMinimumWidth(148)
        rq.addWidget(self.lbl_color_val)
        self.btn_color_reset = QPushButton("重置画面")
        self.btn_color_reset.setStyleSheet(_BTN_QSS)
        self.btn_color_reset.setFixedHeight(28)
        hand_cursor(self.btn_color_reset)
        self.btn_color_reset.clicked.connect(self._reset_color)
        rq.addWidget(self.btn_color_reset)
        rq.addStretch(1)
        cl.addWidget(self.row_color)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)               # 窗口窄时换行（否则会顶最小宽度）
        self.lbl_hint.setStyleSheet(f"color:{_FG_DIM}; font-size:11.5px; background:transparent;")
        cl.addWidget(self.lbl_hint)
        root.addWidget(ctl)

        # ---------------- 播放器 ----------------
        if HAVE_QTMM:
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.audio.setVolume(self.slider_vol.value() / 100)     # 沿用记忆音量
            self.player.setAudioOutput(self.audio)
            if self.video is not None and self.video.sink is not None:
                # 视频输出到画布的 QVideoSink（自绘 + 可调色），不再是 QVideoWidget
                self.player.setVideoOutput(self.video.sink)
            self.slider_vol.valueChanged.connect(self._on_vol)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_play_state)
            self.player.mediaStatusChanged.connect(self._on_status)
            self.player.errorOccurred.connect(self._on_error)
        else:                                        # pragma: no cover
            self.player = None
            self.audio = None

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)

        # 窗口尺寸/音量落盘用（去抖：拖滑块时不必每帧写文件）
        self._pref_timer = QTimer(self)
        self._pref_timer.setSingleShot(True)
        self._pref_timer.setInterval(600)
        self._pref_timer.timeout.connect(prefs.save)

        # 控件一律不接收键盘焦点：否则焦点落在按钮/滑块上时，←/→ 会被它们吃掉
        # （按钮做焦点导航、滑块改数值），事件到不了窗口的 keyPressEvent ——
        # 这就是「方向键跑到按钮控件上」的根因。
        for w in self.findChildren(QWidget):
            if isinstance(w, (QAbstractButton, QAbstractSlider, QComboBox)):
                w.setFocusPolicy(Qt.NoFocus)
        self.setFocusPolicy(Qt.StrongFocus)

        self.resize(*self._win_pref())            # 沿用上次窗口大小
        self._load(self._index)

    # ---------------------------------------------------------------- 窗口尺寸
    @staticmethod
    def _screen_rect() -> tuple[int, int]:
        """主屏可用区域尺寸（拿不到就给个安全兜底）。"""
        scr = QGuiApplication.primaryScreen()
        if scr is None:                                # pragma: no cover
            return (1920, 1080)
        g = scr.availableGeometry()
        return max(WIN_MIN[0], g.width()), max(WIN_MIN[1], g.height())

    def _win_pref(self) -> tuple[int, int]:
        """从偏好读窗口尺寸：非法 / 超屏 / 过小都退回默认值（并夹到屏幕内）。"""
        sw, sh = self._screen_rect()
        pair = prefs.get_pair(PREF_WIN)
        if pair is None:
            return min(WIN_DEFAULT[0], sw), min(WIN_DEFAULT[1], sh)
        w, h = pair
        if w < WIN_MIN[0] or h < WIN_MIN[1]:
            return min(WIN_DEFAULT[0], sw), min(WIN_DEFAULT[1], sh)
        return min(w, sw), min(h, sh)

    def scale_window(self, factor: float) -> None:
        """按比例缩放窗口（保持屏幕居中），并夹在屏幕可用区域内。"""
        if self.isFullScreen():
            return
        if self.isMaximized():
            self.showNormal()                          # 最大化状态下先还原再缩放
        sw, sh = self._screen_rect()
        w = max(WIN_MIN[0], min(int(round(self.width() * factor)), sw))
        h = max(WIN_MIN[1], min(int(round(self.height() * factor)), sh))
        self.resize(w, h)
        self._center_on_screen(w, h)
        self._norm_size = [w, h]
        self._sync_max_btn()
        self._pref_timer.start()

    def _center_on_screen(self, w: int, h: int) -> None:
        scr = QGuiApplication.primaryScreen()
        if scr is None:                                # pragma: no cover
            return
        g = scr.availableGeometry()
        self.move(g.x() + max(0, (g.width() - w) // 2),
                  g.y() + max(0, (g.height() - h) // 2))

    def toggle_max(self) -> None:
        """最大化 / 还原（与全屏互斥）。"""
        if self.isFullScreen():
            self.showNormal()
            self._full = False
        elif self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_max_btn()
        self._pref_timer.start()

    def _sync_max_btn(self) -> None:
        self.btn_win_max.setText(
            "还原" if (self.isMaximized() or self.isFullScreen()) else "最大化")

    def _on_vol(self, v: int) -> None:
        """音量变化：同步播放器 + 记入偏好（去抖落盘）。"""
        if self.audio is not None:
            self.audio.setVolume(max(0, min(100, int(v))) / 100)
        prefs.set(PREF_VOL, int(v))
        self._pref_timer.start()

    # ------------------------------------------------------- 画面缩放 / 调节
    def _active_canvas(self):
        """当前有画面的画布 —— 图片（index 0）或视频（index 1）。

        缩放按钮与 ↑/↓ 快捷键统一作用于它：视频改成自绘画布后，两者接口一致，
        所以同一套代码就能同时管图片和视频。
        """
        return self.canvas if self.stack.currentIndex() == 0 else self.video

    def _on_color(self, *_a) -> None:
        """画面调节滑块变化 → 应用到视频画布（暂停时也会立刻重绘当前帧）。"""
        if self.video is None:
            return
        b, c, s = self.sl_bright.value(), self.sl_contrast.value(), self.sl_sat.value()
        self.video.set_params(b, c, s)
        parts = [f"{n} {v:+d}" for n, v in
                 (("亮度", b), ("对比度", c), ("饱和度", s)) if v]
        self.lbl_color_val.setText(" · ".join(parts) if parts else "原样")

    def _reset_color(self) -> None:
        """三个滑块归零（valueChanged 会自动走到 _on_color 生效）。"""
        for sl in (self.sl_bright, self.sl_contrast, self.sl_sat):
            sl.setValue(0)

    def _zoom_by(self, factor: float) -> None:
        c = self._active_canvas()
        if c is not None:
            c.zoom_by(factor)

    def _reset_fit(self) -> None:
        c = self._active_canvas()
        if c is not None:
            c.reset_fit()

    def _actual_size(self) -> None:
        c = self._active_canvas()
        if c is not None:
            c.set_actual_size()

    def _save_prefs(self) -> None:
        """关窗时把窗口尺寸（含最大化状态）与音量落盘。"""
        try:
            if not (self.isMaximized() or self.isFullScreen()):
                self._norm_size = [self.width(), self.height()]
            prefs.update({
                PREF_WIN: list(self._norm_size),
                PREF_MAX: bool(self.isMaximized()),
                PREF_VOL: int(self.slider_vol.value()),
            })
            prefs.save()
        except (RuntimeError, TypeError, ValueError):  # pragma: no cover
            pass

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if not (self.isMaximized() or self.isFullScreen()):
            self._norm_size = [self.width(), self.height()]
        if getattr(self, "lbl_audio", None) is not None:
            self.lbl_audio.setGeometry(self._video_host.rect())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._shown:                            # 首次显示时恢复最大化
            self._shown = True
            if self._want_max:
                self.showMaximized()
        self._sync_max_btn()
        self.setFocus(Qt.OtherFocusReason)             # 键盘事件始终由窗口处理

    # ---------------------------------------------------------------- 载入
    @property
    def paths(self) -> list[Path]:
        """当前列表里的所有路径（测试 / 外部查看用）。"""
        return [p for p, _m in self._items]

    def current_mid(self) -> str:
        return self._items[self._index][1]

    def _load(self, idx: int) -> None:
        self._index = max(0, min(idx, len(self._items) - 1))
        path, mid = self._items[self._index]
        kind = _kind_of(path)
        self.btn_goto.setEnabled(bool(mid))

        if kind == "image":
            self._is_audio = False               # 供 _sync_type_ui 决定控件显隐
            pix = QPixmap(str(path))
            self.canvas.set_pixmap(pix)
            self.stack.setCurrentIndex(0)
            if self.player is not None:
                self.player.stop()
            self._timer.stop()
            size_txt = ""
            try:
                size_txt = _sz_text(path.stat().st_size)
            except OSError:
                pass
            dim = f"{pix.width()}×{pix.height()}" if not pix.isNull() else "无法解码"
            self.lbl_title.setText(path.name)
            self.lbl_meta.setText(
                f"{dim}　·　{size_txt}　·　{self._index + 1}/{len(self._items)}")
            self.setWindowTitle(f"图片查看 — {path.name}")
            self._release_nav_btn()          # 图片同步解码完成 → 翻页按钮恢复
        else:
            self.stack.setCurrentIndex(1)
            is_audio = kind == "audio"
            self._is_audio = is_audio
            if self.video is not None:
                self.video.clear_frame()         # 切媒体：清掉上一段画面与缩放视角
            info = probe_stream(path)
            self._fps = float(info.get("fps") or 0.0)
            self._duration_ms = int(float(info.get("duration") or 0) * 1000)
            bits = []
            if info.get("width") and info.get("height"):
                bits.append(f"{info['width']}×{info['height']}")
            if is_audio:
                bits.append("音频")          # 音频没有帧率概念，别显示「帧率未知」
            else:
                bits.append(f"{self._fps:.3f} fps" if self._fps else "帧率未知")
            if info.get("codec"):
                bits.append(str(info["codec"]).upper())
            try:
                bits.append(_sz_text(path.stat().st_size))
            except OSError:
                pass
            bits.append(f"{self._index + 1}/{len(self._items)}")
            self.lbl_title.setText(path.name)
            self.lbl_meta.setText("　·　".join(bits))
            self.setWindowTitle(
                f"{'音频' if is_audio else '视频'}查看 — {path.name}")
            if getattr(self, "lbl_audio", None) is not None:
                # 音频时盖住黑屏的视频区；视频时还原透明
                self.lbl_audio.setText(f"🎵　{path.name}" if is_audio else "")
                self.lbl_audio.setVisible(is_audio)
                self.lbl_audio.setGeometry(self._video_host.rect())
            if self.player is not None:
                self.player.setSource(QUrl.fromLocalFile(str(path)))
                self.player.play()
                self._timer.start()
        self._sync_type_ui()

    def _sync_type_ui(self) -> None:
        """按当前媒体类型切换控件可见性与提示文案。"""
        is_img = self.stack.currentIndex() == 0
        is_audio = bool(getattr(self, "_is_audio", False))
        # 缩放按钮：**凡是有画面的都显示**（图片 + 视频）—— 视频已改成自绘画布，
        # 与图片共用同一套缩放 / 拖拽平移 / 双击复位。
        for w in (self.btn_zoom_out, self.btn_zoom_in, self.btn_fit, self.btn_100):
            w.setVisible(not is_audio)
        for w in (self.lbl_step, self.cmb_sec, self.btn_unit, self.btn_back,
                  self.btn_fwd, self.btn_rate, self.lbl_vol, self.slider_vol,
                  self.btn_full, self.btn_play):
            w.setVisible(not is_img)
        self.row_seek.setVisible(not is_img)
        # 画面调节（亮度 / 对比度 / 饱和度）只有视频有意义（音频没有画面）
        self.row_color.setVisible(not is_img and not is_audio)
        self._sync_play_btn()
        self._sync_step_ui()
        self.btn_prev.setEnabled(self._index > 0)
        self.btn_next.setEnabled(self._index < len(self._items) - 1)
        if is_img:
            hint = ("←/→ 切换媒体　↑/↓ 缩放　滚轮缩放　拖动平移　双击 1:1　"
                    "[ ] 上下项　Esc 关闭")
        elif not is_audio:
            hint = (f"←/→ 跳转（{self._step_label()}）　↑/↓ 音量　空格 播放/暂停　"
                    "滚轮缩放　拖动平移　双击 1:1　F 全屏　Esc 关闭")
        else:
            hint = (f"←/→ 跳转（{self._step_label()}）　↑/↓ 音量　空格 播放/暂停　"
                    ", . 逐帧　[ ] 上下项　F 全屏　Esc 关闭")
        self.lbl_hint.setText(hint)

    def _step_label(self) -> str:
        if self._step_unit == "frame":
            return f"1 帧（{self._fps:.3g} fps）" if self._fps else "1 帧（按 25fps 估）"
        return f"{int(self.cmb_sec.currentData() or 1)} 秒"

    def _sync_step_ui(self) -> None:
        is_frame = self._step_unit == "frame"
        self.btn_unit.setText("单位：帧" if is_frame else "单位：秒")
        self.cmb_sec.setEnabled(not is_frame)        # 帧模式下秒数下拉不生效
        self.btn_back.setText("◀ 一帧" if is_frame else "◀ 后退")
        self.btn_fwd.setText("一帧 ▶" if is_frame else "前进 ▶")
        if self.stack.currentIndex() == 1:
            self.lbl_hint.setText(
                f"←/→ 跳转（{self._step_label()}）　↑/↓ 音量　空格 播放/暂停　"
                ", . 逐帧　[ ] 上下项　F 全屏　Esc 关闭")

    def toggle_step_unit(self) -> None:
        """在「秒 / 帧」之间切换 ←/→ 的跳转单位。"""
        self._step_unit = "frame" if self._step_unit == "sec" else "sec"
        self._sync_step_ui()

    # ---------------------------------------------------------------- 播放
    def toggle_play(self) -> None:
        if self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _sync_play_btn(self) -> None:
        if self.player is None:
            return
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.btn_play.setText("⏸ 暂停" if playing else "▶ 播放")

    def cycle_rate(self) -> None:
        if self.player is None:
            return
        rates = list(RATES)
        self._rate = rates[(rates.index(self._rate) + 1) % len(rates)] \
            if self._rate in rates else 1.0
        self.player.setPlaybackRate(self._rate)
        self.btn_rate.setText(f"{self._rate:g}×")

    # ---------------------------------------------------------------- 切换
    def step(self, d: int) -> None:
        """上一个 / 下一个媒体（图片、视频混在一起按序号走）。"""
        n = self._index + d
        if 0 <= n < len(self._items):
            self._hold_nav_btn(self.btn_prev if d < 0 else self.btn_next)
            self._load(n)

    def _hold_nav_btn(self, btn) -> None:
        """翻页点击后按钮保持按下态，直到新媒体真正加载完再弹起。

        视频/音频是异步加载（setSource 后要等解码），鼠标一松按钮高亮立刻
        消失，看不出「已点了、还在加载」。用 setDown(True) 程序化按住：
        图片同步解码，在 _load 末尾释放；视频/音频等 mediaStatus
        LoadedMedia / BufferedMedia 释放；播放出错在 _on_error 释放。
        """
        self._release_nav_btn()
        self._nav_btn = btn
        btn.setDown(True)

    def _release_nav_btn(self) -> None:
        btn = getattr(self, "_nav_btn", None)
        if btn is not None:
            try:
                btn.setDown(False)
            except RuntimeError:                   # pragma: no cover - 控件已销毁
                pass
        self._nav_btn = None

    # ---------------------------------------------------------------- 跳转
    def step_ms(self) -> int:
        """当前一步的毫秒数（帧 → 1000/fps，秒 → 下拉框的值）。"""
        if self._step_unit == "frame":
            return int(round(1000.0 / (self._fps or 25.0)))
        return int(self.cmb_sec.currentData() or 1) * 1000

    def seek_step(self, d: int) -> None:
        """按当前单位前进 / 后退一步（←/→ 与「前进/后退」按钮都走这里）。"""
        self.seek_rel(d * self.step_ms())

    def seek_rel(self, ms: int) -> None:
        if self.player is None:
            return
        dur = self.player.duration() or self._duration_ms or 0
        pos = self.player.position() + int(ms)
        self.player.setPosition(max(0, min(dur if dur > 0 else pos, pos)))

    def step_frame(self, d: int) -> None:
        """逐帧（按帧率步进，帧率未知按 25fps 兜底）。"""
        if self.player is not None:
            self.player.pause()
        self.seek_rel(int(round(d * 1000.0 / (self._fps or 25.0))))

    def toggle_full(self) -> None:
        if self._full:
            self.showNormal()
            self._full = False
        else:
            self.showFullScreen()
            self._full = True
        self._sync_max_btn()

    def emit_goto(self) -> None:
        """「跳转到该条消息」→ 把消息编号发回列表页定位。"""
        mid = self._items[self._index][1]
        if mid:
            self.goto_message.emit(mid)

    # ---------------------------------------------------------------- 回调
    def _on_position(self, pos: int) -> None:
        if not self._seeking:
            self.slider.setValue(int(pos))
        self.lbl_time.setText(
            f"{_hms(pos)} / {_hms(self.player.duration() or self._duration_ms)}")

    def _on_duration(self, dur: int) -> None:
        self.slider.setRange(0, max(0, int(dur)))
        self.lbl_time.setText(
            f"{_hms(self.player.position())} / {_hms(dur or self._duration_ms)}")

    def _on_seek_move(self, v: int) -> None:
        self.lbl_time.setText(
            f"{_hms(v)} / {_hms(self.player.duration() or self._duration_ms)}")

    def _on_seek_end(self) -> None:
        self._seeking = False
        if self.player is not None:
            self.player.setPosition(self.slider.value())

    def _on_play_state(self, st) -> None:
        """播放状态变化：刷新播放按钮，并在**非播放**时让画布按暂停档重渲一次。

        播放中画布把处理分辨率限到 960×540（流畅优先），停下来之后按 1920×1080
        重渲，画面更清楚（`_raw` 一直是全分辨率原帧）。
        """
        self._sync_play_btn()
        try:
            _paused = (QMediaPlayer is not None and st != QMediaPlayer.PlayingState)
        except Exception:                                # noqa: BLE001
            _paused = False
        if _paused and self.video is not None:
            self.video.on_playback_paused()

    def _on_status(self, st) -> None:
        if st in (QMediaPlayer.MediaStatus.LoadedMedia,
                  QMediaPlayer.MediaStatus.BufferedMedia):
            self._release_nav_btn()                # 新媒体解码完成 → 翻页按钮弹起
        if st == QMediaPlayer.MediaStatus.EndOfMedia:
            self._sync_play_btn()

    def _on_error(self, _err, msg: str = "") -> None:
        self._release_nav_btn()                    # 加载失败也要把按钮弹回来
        if msg:
            self.lbl_title.setText(f"{self._items[self._index][0].name}　（播放失败：{msg}）")

    def _on_scale(self, s: float) -> None:
        """缩放变化 → 刷新信息条（图片显示「尺寸·大小·百分比」，视频显示百分比）。"""
        path, _mid = self._items[self._index]
        # 视频：百分比要按**原片**算 —— 画布是按显示尺寸处理的，`eff_scale()` 在
        # 适应窗口时恒为 ~1.0，直接用它会永远显示「100%」（看不出是原片的多少）
        if self.stack.currentIndex() != 0 and self.video is not None:
            s = self.video.scale_to_source(s)
        pct = f"{max(1, round(s * 100))}%"
        if self.stack.currentIndex() == 0:
            pix = self.canvas._pix
            dim = f"{pix.width()}×{pix.height()}" if pix is not None else "—"
            try:
                size_txt = _sz_text(path.stat().st_size)
            except OSError:
                size_txt = ""
            self.lbl_meta.setText(
                f"{dim}　·　{size_txt}　·　{pct}　·　"
                f"{self._index + 1}/{len(self._items)}")
            return
        # 视频/音频：在原有信息尾部追加缩放百分比（不改动其他字段）
        txt = self.lbl_meta.text()
        base = "　·　".join(x for x in txt.split("　·　") if not x.endswith("%"))
        self.lbl_meta.setText(f"{base}　·　{pct}")

    def _tick(self) -> None:
        if self.player is None or self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        if not self._seeking:
            pos = self.player.position()
            if pos and abs(self.slider.value() - pos) > 400:
                self.slider.setValue(int(pos))

    # ---------------------------------------------------------------- 键盘
    def keyPressEvent(self, e) -> None:
        k = e.key()
        is_img = self.stack.currentIndex() == 0

        if k == Qt.Key_Escape:
            if self._full:
                self.toggle_full()
            else:
                self.close()
            return
        if k in (Qt.Key_BracketLeft, Qt.Key_PageUp):     # 上下一个媒体（两类通用）
            self.step(-1)
            return
        if k in (Qt.Key_BracketRight, Qt.Key_PageDown):
            self.step(1)
            return

        if is_img:                                       # ---- 图片 ----
            if k == Qt.Key_Left:
                self.step(-1)
            elif k == Qt.Key_Right:
                self.step(1)
            elif k in (Qt.Key_Up, Qt.Key_Plus, Qt.Key_Equal):
                self.canvas.zoom_by(1.25)
            elif k in (Qt.Key_Down, Qt.Key_Minus):
                self.canvas.zoom_by(1 / 1.25)
            elif k == Qt.Key_0:
                self.canvas.reset_fit()
            elif k in (Qt.Key_1, Qt.Key_F):
                self.canvas.set_actual_size()
            else:
                super().keyPressEvent(e)
            return

        # ---- 视频 / 音频：←/→ 只做跳转，↑/↓ 调音量 ----
        if k == Qt.Key_Left:
            self.seek_step(-1)
        elif k == Qt.Key_Right:
            self.seek_step(1)
        elif k == Qt.Key_Up:
            self.slider_vol.setValue(min(100, self.slider_vol.value() + 5))
        elif k == Qt.Key_Down:
            self.slider_vol.setValue(max(0, self.slider_vol.value() - 5))
        elif k == Qt.Key_Space:
            self.toggle_play()
        elif k == Qt.Key_Comma:
            self.step_frame(-1)
        elif k == Qt.Key_Period:
            self.step_frame(1)
        elif k == Qt.Key_U:
            self.toggle_step_unit()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_by(1.25)          # 视频缩放：+/- 键（↑/↓ 留给音量）
        elif k == Qt.Key_Minus:
            self._zoom_by(1 / 1.25)
        elif k == Qt.Key_0:
            self._reset_fit()
        elif k == Qt.Key_F:
            self.toggle_full()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e) -> None:
        self._timer.stop()
        self._pref_timer.stop()
        self._save_prefs()                         # 关窗时记住窗口大小与音量
        try:
            if self.player is not None:
                self.player.stop()
                self.player.setSource(QUrl())
        except RuntimeError:                       # pragma: no cover
            pass
        super().closeEvent(e)


# ============================================================================
# 统一入口
# ============================================================================
def open_media(items, index: int, parent=None) -> QDialog | None:
    """打开统一查看器（图片 / 视频同窗口，上一项下一项在整个列表里走）。

    `items` 支持 `list[Path]` 或 `list[(Path, 消息编号)]`；
    当前项格式不支持、或视频但 QtMultimedia 缺失时返回 None（调用方退化）。
    """
    pairs = [(p, mid) for p, mid in _norm_items(items) if p.exists()]
    if not pairs:
        return None
    if not is_viewable(pairs[max(0, min(index, len(pairs) - 1))][0]):
        return None
    viewer = MediaViewer(pairs, index, parent)
    if parent is not None:
        try:
            viewer.setWindowIcon(parent.window().windowIcon())
        except RuntimeError:                       # pragma: no cover
            pass
    return viewer
