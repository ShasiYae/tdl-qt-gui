# -*- coding: utf-8 -*-
"""
自绘控件库 —— 实现「固定尺寸、永不漂移」的选项卡与导航项。

为什么要自绘而不是用 QPushButton 裸样式：
  1. QSS 的 padding/border 会把按钮实际高度撑大，高度不受控
  2. 图标 + 文字 + 徽标需要精确的水平排布（gap 可调）
  3. 药丸圆角 / 下划线指示条需要按状态精确控制

所有控件都显式 setFixedHeight，彻底排除尺寸漂移。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize, QRect, QEvent, QObject
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QSizePolicy, QPushButton,
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit,
    QPlainTextEdit, QTextEdit,
)

from .theme import T
from .icons import ICONS


# ============================================================================
# 光标规则：只有「可点击且可用」的控件悬停时显示手型，其余一律正常箭头
# ============================================================================
class _HandWhenEnabled(QObject):
    """事件过滤器：控件禁用/恢复时同步切换手型 ↔ 箭头。

    Qt 里 disabled 控件收不到鼠标事件，但**光标形状仍按控件设置显示**——
    所以禁用按钮会「看着能点、点了没反应」。挂上这个过滤器后，
    EnabledChange 事件一到就按 enabled 状态重设光标。
    """

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.EnabledChange:
            obj.setCursor(Qt.PointingHandCursor if obj.isEnabled()
                          else Qt.ArrowCursor)
        return False


_CURSOR_HELPER = _HandWhenEnabled()


def hand_cursor(w: QWidget) -> None:
    """给可点击控件挂手型光标；控件被禁用时自动还原普通箭头。"""
    w.setCursor(Qt.PointingHandCursor)
    w.installEventFilter(_CURSOR_HELPER)


# ----------------------------------------------------------------------------
# 全应用光标规则（挂在 QApplication 上，新控件自动生效）
#   · 可点击（按钮 / 下拉框）        → 手型（禁用时自动还原箭头）
#   · 文本栏（输入框 / 文本编辑区等） → 文本编辑光标（IBeam）
#   · 其余（自绘控件、标签、滑块…）   → 不动，保持控件自身设置
# ----------------------------------------------------------------------------
def _cursor_for(w: QWidget):
    """按控件类型给出应设的光标；返回 None 表示「不管」（保留控件自身设置）。"""
    if isinstance(w, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
        return Qt.IBeamCursor                          # 文本栏 = 文本编辑模式
    if isinstance(w, (QAbstractButton, QComboBox)):    # 可点击 = 手型
        return Qt.PointingHandCursor if w.isEnabled() else Qt.ArrowCursor
    return None                                        # 不可点击 / 自定义控件 → 不干预


class _CursorRules(QObject):
    """全应用光标规则。

    · `ChildPolished` / `ChildAdded` —— 控件一加入界面就按类型设好光标
      （含动态创建的卡片、懒加载的页面、弹窗按钮，不必逐个手写）。
      ⚠️ 实测（PySide6 6.11）：`ChildAdded` 在**构造期**触发，事件里的
      `child()` 只是基类 `QObject` 包装，`isinstance(child, QWidget)` 为 False；
      `ChildPolished`（控件即将显示时）给的才是正确类型的控件
      ⇒ 以 polished 为主，added 仅作兜底。
    · `EnabledChange` —— 按钮/下拉框在「手型 ↔ 箭头」之间切换。
    """

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.EnabledChange:
            if isinstance(obj, QWidget):
                c = _cursor_for(obj)
                if c is not None:
                    obj.setCursor(c)
            return False
        if t not in (QEvent.ChildPolished, QEvent.ChildAdded):
            return False
        try:
            child = ev.child()
        except Exception:                              # pragma: no cover
            return False
        # 构造期拿到的是基类包装（isWidgetType 为真但类型未知）→ 跳过，
        # 等控件 polish 时再设，那时类型正确。
        if not isinstance(child, QWidget):
            return False
        c = _cursor_for(child)
        if c is not None:
            child.setCursor(c)
        return False


def apply_cursors(root: QWidget) -> None:
    """按光标规则设置 root 及其全部子控件的光标（幂等，可反复调用）。"""
    c = _cursor_for(root)
    if c is not None:
        root.setCursor(c)
    for ch in root.findChildren(QWidget):
        c = _cursor_for(ch)
        if c is not None:
            ch.setCursor(c)


def install_cursor_rules(app) -> _CursorRules:
    """装上全应用光标规则：可点击 → 手型，文本栏 → 文本编辑光标，其余 → 箭头。"""
    rules = _CursorRules(app)
    app._cursor_rules = rules                           # 挂到 app 上防被回收
    app.installEventFilter(rules)
    for w in app.allWidgets():                          # 已有控件先扫一遍
        apply_cursors(w)
    return rules


# ============================================================================
# 一级选项卡：药丸形，固定 38px
# ============================================================================
class HubTab(QWidget):
    """
    一级横向选项卡（药丸）。

    DOM 对应：原型里的 .hub-tab
      * 固定高度 38px（--h-tab）
      * 圆角 19px（半高，正圆头）
      * 图标 16px + 文字 13px/600 + 可选徽标 17px
      * 激活态：淡蓝底 + 主题色描边 + 深蓝字
      * 不渲染副标题（原型里 .ht-cmd 是 display:none，避免撑高）
    """

    clicked = Signal()

    def __init__(self, name: str, icon_name: str = "", badge: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName("HubTab")
        hand_cursor(self)
        self.setFixedHeight(T.H_TAB)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._name = name
        self._icon_name = icon_name
        self._badge = badge
        self._on = False
        self._hover = False

        self._recalc_width()

    # ---------------------------------------------------------------- 尺寸
    def _recalc_width(self) -> None:
        """按内容算宽度：padding 15*2 + 图标 + gap + 文字 + 徽标。"""
        fm = QFontMetrics(self._name_font())
        w = 15 * 2
        if self._icon_name:
            w += T.ICO_TAB + 7
        w += fm.horizontalAdvance(self._name)
        if self._badge:
            w += 6 + max(17, fm.horizontalAdvance(str(self._badge)) + 10)
        self.setFixedWidth(w)

    def _name_font(self) -> QFont:
        f = QFont()
        f.setFamily("Microsoft YaHei UI")
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold)
        return f

    # ---------------------------------------------------------------- 状态
    def set_on(self, on: bool) -> None:
        if self._on != on:
            self._on = on
            self.update()

    def set_badge(self, n: int) -> None:
        if self._badge != n:
            self._badge = n
            self._recalc_width()
            self.update()

    # ---------------------------------------------------------------- 事件
    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        e.accept()          # 所有按键一律吃掉：中键不得穿透到父级滚动区（会触发自动滚动）

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        e.accept()

    def wheelEvent(self, e):
        e.ignore()          # 滚轮交给父级滚动区：只滚页面，不切换选项卡

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = self.rect().adjusted(0, 0, -1, -1)
        radius = T.H_TAB / 2.0

        # 底色 / 描边
        if self._on:
            bg, border, fg = T.ACCENT_SOFT, T.ACCENT, T.ACCENT_INK
        elif self._hover:
            bg, border, fg = T.ACCENT_HOVER, T.ACCENT_LINE, T.TEXT_DIM
        else:
            bg, border, fg = T.PANEL, T.BORDER_STRONG, T.TEXT_DIM

        p.setPen(QPen(QColor(border), 1))
        p.setBrush(QColor(bg))
        p.drawRoundedRect(r, radius, radius)

        # 内容从左往右排：图标 -> 文字 -> 徽标
        x = r.left() + 15
        cy = r.center().y() + 1

        if self._icon_name:
            ico = ICONS.get(self._icon_name, T.ICO_TAB,
                            T.ACCENT if self._on else T.TEXT_FAINT)
            ico.paint(p, x, cy - T.ICO_TAB // 2, T.ICO_TAB, T.ICO_TAB)
            x += T.ICO_TAB + 7

        # 文字
        f = self._name_font()
        f.setWeight(QFont.DemiBold if self._on else QFont.Medium)
        p.setFont(f)
        p.setPen(QColor(fg))
        fm = QFontMetrics(f)
        p.drawText(x, cy + fm.ascent() // 2 - 1, self._name)
        x += fm.horizontalAdvance(self._name)

        # 徽标
        if self._badge:
            x += 6
            bw = max(17, fm.horizontalAdvance(str(self._badge)) + 10)
            by = cy - 17 // 2
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.ACCENT))
            p.drawRoundedRect(x, by, bw, 17, 8.5, 8.5)
            p.setPen(QColor("#ffffff"))
            bf = QFont(f)
            bf.setPixelSize(10)
            bf.setWeight(QFont.Bold)
            p.setFont(bf)
            p.drawText(x, by, bw, 17, Qt.AlignCenter, str(self._badge))

        p.end()


# ============================================================================
# 子选项卡：下划线指示条，固定 33px
# ============================================================================
class SubTab(QWidget):
    """
    下划线型子选项卡。

    DOM 对应：原型里的 .sub-tab
      * 固定高度 33px（--h-tabstrip）
      * 底部 2px 指示条，激活时主题色
      * 图标 14px + 文字 12.5px
      * padding 14px
    """

    clicked = Signal()

    def __init__(self, name: str, icon_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("SubTab")
        hand_cursor(self)
        self.setFixedHeight(T.H_TABSTRIP)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._name = name
        self._icon_name = icon_name
        self._on = False
        self._hover = False

        self._recalc_width()

    def _name_font(self) -> QFont:
        f = QFont()
        f.setFamily("Microsoft YaHei UI")
        f.setPixelSize(12)
        f.setWeight(QFont.Medium)
        return f

    def _recalc_width(self) -> None:
        fm = QFontMetrics(self._name_font())
        w = 14 * 2
        if self._icon_name:
            w += T.ICO_SUBTAB + 6
        w += fm.horizontalAdvance(self._name)
        self.setFixedWidth(w)

    def set_on(self, on: bool) -> None:
        if self._on != on:
            self._on = on
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        e.accept()          # 所有按键一律吃掉：中键不得穿透到父级滚动区（会触发自动滚动）

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        e.accept()

    def wheelEvent(self, e):
        e.ignore()          # 滚轮交给父级滚动区：只滚页面，不切换选项卡

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = self.rect()
        fg = T.ACCENT_INK if self._on else (T.TEXT_DIM if not self._hover else T.ACCENT_INK)

        x = 14
        cy = r.height() // 2

        if self._icon_name:
            ico = ICONS.get(self._icon_name, T.ICO_SUBTAB,
                            T.ACCENT if self._on else T.TEXT_FAINT)
            ico.paint(p, x, cy - T.ICO_SUBTAB // 2, T.ICO_SUBTAB, T.ICO_SUBTAB)
            x += T.ICO_SUBTAB + 6

        f = self._name_font()
        f.setWeight(QFont.DemiBold if self._on else QFont.Medium)
        p.setFont(f)
        p.setPen(QColor(fg))
        fm = QFontMetrics(f)
        p.drawText(x, cy + fm.ascent() // 2 - 1, self._name)

        # 底部指示条
        if self._on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.ACCENT))
            p.drawRect(r.left() + 8, r.bottom() - 1, r.width() - 16, 2)

        p.end()


class SubTabBar(QWidget):
    """
    子选项卡容器 —— 底部通栏 1px 分隔线 + 左侧排列的 SubTab。

    原型里这条是 `.sub-tabs.pinned`（sticky 钉在「下载来源」组下方）。
    Qt 侧由布局保证位置：常驻组 → 本控件 → 内容，不需要 sticky。
    """

    changed = Signal(str)   # 参数为选中子选项卡 id

    def __init__(self, tabs: list[dict], active: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("SubTabBar")
        self.setFixedHeight(T.H_TABSTRIP + 1)   # 33 + 1px 底边线

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 0, 0)
        lay.setSpacing(2)

        self._tabs: dict[str, SubTab] = {}
        for t in tabs:
            st = SubTab(t["name"], t.get("icon", ""))
            st.clicked.connect(lambda _=None, tid=t["id"]: self._pick(tid))
            lay.addWidget(st)
            self._tabs[t["id"]] = st

        lay.addStretch(1)
        self.set_active(active or (tabs[0]["id"] if tabs else ""))

    def set_active(self, tid: str) -> None:
        for k, w in self._tabs.items():
            w.set_on(k == tid)

    def _pick(self, tid: str) -> None:
        self.set_active(tid)
        self.changed.emit(tid)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setPen(QPen(QColor(T.LINE), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


# ============================================================================
# 侧边栏导航项（图标 + 名称 + 副标题，固定 32px）
# ============================================================================
class NavItem(QWidget):
    """
    侧边栏导航项。

    DOM 对应：原型里的 .nav-item
      * 固定高度 32px（--h-control）—— 与按钮/输入框一致
      * 图标 15px + 名称 13px + 右侧等宽字体副标题
      * 激活态：淡蓝底 + 主题色描边 + 深蓝字
    """

    clicked = Signal()

    def __init__(self, name: str, icon_name: str = "", sub: str = "",
                 badge: int = 0, parent=None):
        super().__init__(parent)
        self.setObjectName("NavItem")
        hand_cursor(self)
        self.setFixedHeight(T.H_CONTROL)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._name = name
        self._icon_name = icon_name
        self._sub = sub
        self._badge = badge
        self._on = False
        self._hover = False

    def set_on(self, on: bool) -> None:
        if self._on != on:
            self._on = on
            self.update()

    def set_badge(self, n: int) -> None:
        if self._badge != n:
            self._badge = n
            self.update()

    def _badge_w(self) -> int:
        """徽标占用的水平宽度（含右侧留白），无徽标时返回基础留白。"""
        if not self._badge:
            return 0
        bf = QFont("Microsoft YaHei UI")
        bf.setPixelSize(10)
        bf.setWeight(QFont.Bold)
        fm = QFontMetrics(bf)
        return max(17, fm.horizontalAdvance(str(self._badge)) + 10) + 6

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        e.accept()          # 所有按键一律吃掉：中键不得穿透到父级滚动区（会触发自动滚动）

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        e.accept()

    def wheelEvent(self, e):
        e.ignore()          # 滚轮交给父级滚动区：只滚页面，不切换选项卡

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = self.rect().adjusted(0, 0, 0, -1)

        if self._on:
            p.setPen(QPen(QColor(T.ACCENT_LINE), 1))
            p.setBrush(QColor(T.ACCENT_SOFT))
        elif self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.ACCENT_HOVER))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(Qt.NoBrush)
        if self._on or self._hover:
            p.drawRoundedRect(r, T.RADIUS_SM, T.RADIUS_SM)

        x = r.left() + 9
        cy = r.center().y() + 1

        # 图标
        if self._icon_name:
            ico = ICONS.get(self._icon_name, T.ICO_NAV,
                            T.ACCENT if self._on else T.TEXT_FAINT)
            ico.paint(p, x, cy - T.ICO_NAV // 2, T.ICO_NAV, T.ICO_NAV)
        x += 16 + 9

        # 名称 + 副标题（两行）
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold if self._on else QFont.Normal)
        p.setFont(f)
        p.setPen(QColor(T.ACCENT_INK if self._on else T.TEXT))
        fm = QFontMetrics(f)

        if self._sub:
            # 两行排布：名称(13px) + 副标题(10px mono)，整块垂直居中。
            #
            # 为什么不用 drawText(x, baseline, text)：
            #   手工算基线与 Qt 实际字形范围（尤其中文 descent）会有 1~2px 偏差，
            #   32px 高度下这点偏差就会把副标题底边裁掉。
            # 改用 drawText(QRect, flags, text) + AlignVCenter，把垂直定位交给 Qt，
            # 每行给一个固定高度的矩形，Qt 保证字形完整落在框内。
            sf = QFont("Consolas")
            sf.setPixelSize(10)
            sfm = QFontMetrics(sf)

            h1 = fm.height()        # 16
            h2 = sfm.height()       # 12
            total = h1 + h2         # 28
            top = r.top() + (r.height() - total) // 2     # 垂直居中，=2

            avail_w = r.right() - x - (self._badge_w() if self._badge else 9)

            p.setFont(f)
            p.setPen(QColor(T.ACCENT_INK if self._on else T.TEXT))
            p.drawText(
                QRect(x, top, max(10, avail_w), h1),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                self._name,
            )

            p.setFont(sf)
            p.setPen(QColor(T.ACCENT if self._on else T.TEXT_FAINT))
            p.drawText(
                QRect(x, top + h1, max(10, avail_w), h2),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                self._sub,
            )
        else:
            p.drawText(x, cy + fm.ascent() // 2 - 1, self._name)

        # 徽标
        if self._badge:
            bf = QFont("Microsoft YaHei UI")
            bf.setPixelSize(10)
            bf.setWeight(QFont.Bold)
            p.setFont(bf)
            fm2 = QFontMetrics(bf)
            bw = max(17, fm2.horizontalAdvance(str(self._badge)) + 10)
            bx = r.right() - 9 - bw
            by = cy - 17 // 2
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.ACCENT))
            p.drawRoundedRect(bx, by, bw, 17, 8.5, 8.5)
            p.setPen(QColor("#ffffff"))
            p.drawText(bx, by, bw, 17, Qt.AlignCenter, str(self._badge))

        p.end()


# ============================================================================
# 分组卡片（白色圆角容器 + 标题）
# ============================================================================
class GroupBox(QWidget):
    """
    表单分组（对照原型 .group：透明无卡片）。

    结构：GroupTitle（11px 大写灰字 + 底线）→ 字段列表。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("GroupBox")
        self._title = title

        from PySide6.QtWidgets import QVBoxLayout
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(10)

        if title:
            lab = QLabel(title)
            lab.setObjectName("GroupTitle")
            self._lay.addWidget(lab)

    def body(self):
        return self._lay

    def add(self, w) -> None:
        self._lay.addWidget(w)


# ============================================================================
# 悬浮提示开关
# ============================================================================
class TooltipBlocker(QObject):
    """吞掉所有 ToolTip 事件的应用级过滤器（禁用鼠标停留弹窗）。"""

    def eventFilter(self, obj, ev):                   # noqa: N802, ANN001
        return ev.type() == QEvent.ToolTip


def install_no_tooltip(app) -> TooltipBlocker:
    """禁用「鼠标停留弹出悬浮提示」（tooltip 会遮挡卡片/输入框内容）。

    挂在 QApplication 上即可覆盖全部控件；代码里遗留的 setToolTip 调用
    不再产生任何提示（保留它们是为了以后需要时去掉本拦截器即可恢复）。
    """
    blocker = TooltipBlocker(app)
    app._tooltip_blocker = blocker                    # 挂到 app 上防被回收
    app.installEventFilter(blocker)
    return blocker


class ElidedLabel(QLabel):
    """超宽自动省略号的单行标签（不撑大布局的最小宽度）。

    为什么需要它：QLabel 的 sizeHint 按文本长度算，长文件名/长状态文本会把
    所在行的**最小宽度**顶上去（窗口再也缩不到该宽度以下）。这里把水平策略设成
    Ignored 并自己画省略号，文本再长也只占可用空间。
    """

    def __init__(self, text: str = "", color: str = "#9aa5b1", parent=None):
        super().__init__(text, parent)
        self._color = color
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setToolTip(text)

    def setText(self, t: str) -> None:              # noqa: N802
        super().setText(t)
        self.setToolTip(t)

    def minimumSizeHint(self):                      # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setPen(QColor(self._color))
        fm = QFontMetrics(self.font())
        p.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter),
                   fm.elidedText(self.text(), Qt.ElideRight, self.width()))
