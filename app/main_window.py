# -*- coding: utf-8 -*-
"""
主窗口 —— 顶栏 / 侧边栏 / 内容区 / 状态栏 四段式布局，并接线真实 tdl 进程。

DOM 结构对应原型的 .app 容器：
    .titlebar   顶栏（logo + 原型徽标 + 空间切换 + 登录状态）
    .main       主体（.sidebar 左侧导航 | .content 右侧内容）
    .statusbar  底部状态栏

接线层：
    TdlRunner 负责进程生命周期，MainWindow 负责把 UI 事件转成 argv。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer, Signal, QPoint, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QPolygon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QSizePolicy, QSplitter, QToolButton, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QInputDialog, QMenu, QLineEdit, QComboBox, QCheckBox, QTextBrowser,
    QSystemTrayIcon, QApplication, QMessageBox,
)
from PySide6.QtGui import QAction

from .theme import T
from .version import __version__
from .icons import ICONS
from .data import (
    NAV_GROUPS, CMD_BY_ID, COMMANDS, GLOBAL_CARDS, DEFAULT_TDL_PATH,
    global_args_fields, dl_param_fields, GLOBAL_FIELDS,
)
from . import paths
from . import envcheck
from . import deploy
from . import deploy_flow
from .widgets import ElidedLabel, NavItem, HubTab, GroupBox, hand_cursor
from .form_renderer import FormRenderer, FieldRow
from .log_panel import LogPanel, ActionBar
from .task_panel import (
    TaskBox, parse_progress_line, is_noise_line, is_round_marker, strip_ansi,
    fmt_bytes, fmt_bytes_dec, fmt_duration,
)
from .msg_view import MsgBrowser
from . import stats
from . import procutil
from .runner import TdlRunner, build_argv


# ---- tdl 的固定提示行：译成中文、且每条只提示一次 ----
# 为什么：① 界面是中文的，英文 WARN 看着像出错（其实不是）；② 「限速暂停」这句
# 每次暂停都会打印一行，导出大群时能把日志面板刷满。命中后转成一句中文说明，
# 同一条只提示一次（`_tdl_notes_seen` 每次新命令清空）。
_TDL_NOTES = (
    ("Export only generates minimal JSON for tdl download, not for backup",
     "提示：导出的 JSON 为精简格式，仅供 tdl 下载使用，不能作为聊天备份。"),
    ("Occasional suspensions are due to Telegram rate limitations",
     "提示：导出会因 Telegram 限速偶发暂停（不是卡死），稍等会自动继续。"),
    ("Current database is used by another process",
     "提示：tdl 的账号数据库同一时刻只允许一个进程使用，现在被另一个 tdl "
     "进程占着（多半是上次异常退出留下的残留进程）。\n"
     "　　　处理：重新执行命令时会自动询问是否结束残留进程，也可到左侧"
     "「环境部署」页点「结束残留 tdl 进程」。"),
)


def _tdl_note(line: str) -> str:
    """命中 tdl 的固定提示行 → 返回中文说明；否则返回空串。"""
    t = (line or "").lower()
    for needle, cn in _TDL_NOTES:
        if needle.lower() in t:
            return cn
    return ""



# ============================================================================
# 顶栏
# ============================================================================
class TitleBar(QWidget):
    """顶栏，固定 44px。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(T.H_TITLEBAR)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        logo_wrap = QWidget()
        lw = QHBoxLayout(logo_wrap)
        lw.setContentsMargins(0, 0, 0, 0)
        lw.setSpacing(4)
        a = QLabel("tdl")
        a.setObjectName("Logo")
        b = QLabel("GUI")
        b.setObjectName("LogoAccent")
        lw.addWidget(a)
        lw.addWidget(b)
        lay.addWidget(logo_wrap)

        self.badge = QLabel("tdl v—")
        self.badge.setObjectName("ProtoBadge")
        lay.addWidget(self.badge)

        lay.addStretch(1)

        # 命名空间切换：下拉菜单（点击列出所有命名空间，勾选当前项）
        self.ns_btn = NsSwitch("default")
        lay.addWidget(self.ns_btn)

    def set_version(self, text: str) -> None:
        """把 tdl 版本号显示在徽标里。"""
        self.badge.setText(text)


class NsSwitch(QWidget):
    """顶栏命名空间切换（下拉菜单）。

    外观仍是原来的胶囊（文件夹图标 + 名称 + ▼），但点击弹**菜单**而不是循环切换：
    列出本机所有命名空间（读 `~/.tdl/data/` 下的库文件），当前项打勾，
    末尾附「自定义…」以便输入磁盘上还没有的名字。
    """

    chosen = Signal(str)                 # 选中某个命名空间

    def __init__(self, name: str = "default", parent=None):
        super().__init__(parent)
        self.setObjectName("NsSwitch")
        hand_cursor(self)
        self.setFixedHeight(24)
        self._name = name
        self._hover = False
        self._names: list[str] = [name] if name else []
        self._provider = None            # () -> list[str]，每次弹菜单前重新取
        self._recalc()

    # ------------------------------------------------------------ 数据
    def set_names(self, names: list[str]) -> None:
        """直接指定候选列表（当前值总会包含在内）。"""
        self._names = self._dedup(names)
        self._recalc()

    def set_provider(self, fn) -> None:
        """设置候选来源（每次打开菜单时调用，可反映磁盘上新增的命名空间）。"""
        self._provider = fn
        self.set_names(fn() if callable(fn) else [])

    def names(self) -> list[str]:
        """当前候选列表（磁盘顺序，当前值必然在内 —— 不在则补在末尾）。"""
        if self._provider is not None:
            try:
                got = self._dedup([str(x) for x in self._provider()])
                if got:
                    return self._dedup(got + [self._name])
            except Exception:                        # noqa: BLE001 - 取不到就用旧的
                pass
        return self._dedup(list(self._names) + [self._name])

    def _dedup(self, seq) -> list[str]:
        out: list[str] = []
        for n in seq:
            n = str(n or "").strip()
            if n and n not in out:
                out.append(n)
        return out

    # ------------------------------------------------------------ 菜单
    def build_menu(self) -> QMenu:
        """构造下拉菜单（与 `popup()` 分开，便于测试，不会阻塞）。"""
        m = QMenu(self)
        for n in self.names():
            act = m.addAction(n)
            act.setCheckable(True)
            act.setChecked(n == self._name)
            # 菜单项自己发信号（而不是靠 exec() 的返回值），便于程序化触发/测试
            act.triggered.connect(lambda _c=False, nm=n: self.chosen.emit(nm))
        m.addSeparator()
        m.addAction("自定义…").triggered.connect(lambda _c=False: self.ask_custom())
        return m

    def popup(self) -> None:
        self.build_menu().exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def ask_custom(self) -> None:
        """「自定义…」：允许输入磁盘上还不存在的命名空间。"""
        text, ok = QInputDialog.getText(self, "命名空间", "输入命名空间名称：",
                                        text=self._name)
        if ok and str(text).strip():
            self.chosen.emit(str(text).strip())

    # ------------------------------------------------------------ 外观
    def _recalc(self) -> None:
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(12)
        f.setWeight(QFont.DemiBold)
        fm = QFontMetrics(f)
        w = 9 + 12 + 5 + fm.horizontalAdvance(self._name) + 5 + 8 + 8
        self.setFixedWidth(min(240, max(70, w)))

    def set_name(self, name: str) -> None:
        self._name = name
        self._recalc()
        self.update()

    def name(self) -> str:
        """当前显示的命名空间。"""
        return self._name

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.popup()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(0, 0, -1, -1)
        radius = r.height() / 2.0

        p.setPen(QPen(QColor(T.ACCENT_LINE), 1))
        p.setBrush(QColor("#cfe1ff" if self._hover else T.ACCENT_SOFT))
        p.drawRoundedRect(r, radius, radius)

        x = r.left() + 9
        cy = r.center().y() + 1

        ICONS.get("folder", 12, T.ACCENT_INK).paint(p, x, cy - 6, 12, 12)
        x += 12 + 5

        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(12)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(T.ACCENT_INK))
        fm = QFontMetrics(f)
        avail = self.width() - x - 20
        txt = fm.elidedText(self._name, Qt.ElideRight, max(10, avail))
        p.drawText(x, cy + fm.ascent() // 2 - 1, txt)

        ax = r.right() - 14
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.ACCENT_INK))
        p.drawPolygon(QPolygon([QPoint(ax, cy - 3), QPoint(ax + 8, cy - 3),
                                QPoint(ax + 4, cy + 2)]))
        p.end()


# ============================================================================
# 侧边栏
# ============================================================================
class Sidebar(QWidget):
    """左侧导航栏，固定 236px。"""

    nav_picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(T.W_SIDEBAR)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QLabel("功能")
        head.setObjectName("SidebarHead")
        lay.addWidget(head)

        scroll = QScrollArea()
        scroll.setObjectName("NavScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        self._nav_lay = QVBoxLayout(inner)
        self._nav_lay.setContentsMargins(8, 0, 8, 12)
        self._nav_lay.setSpacing(2)

        self._items: dict[str, NavItem] = {}
        for g in NAV_GROUPS:
            c = CMD_BY_ID.get(g["cmd_id"])
            if not c:
                continue
            it = NavItem(c["name"], c.get("icon", ""), sub=c.get("cmd", ""))
            it.clicked.connect(lambda _=None, cid=g["cmd_id"]: self.nav_picked.emit(cid))
            self._nav_lay.addWidget(it)
            self._items[g["id"]] = it

        self._nav_lay.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

    def set_active(self, cmd_id: str) -> None:
        for gid, it in self._items.items():
            it.set_on(gid == cmd_id)


# ============================================================================
# 状态栏
# ============================================================================
class StatusBar(QWidget):
    """底部状态栏，固定 28px。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(T.H_STATUSBAR)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(0)

        self._vals: dict[str, QLabel] = {}

        def item(key: str, label: str, val: str, mono: bool = False, dim: bool = False):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(11, 0, 11, 0)
            h.setSpacing(5)
            if label:
                l = QLabel(label)
                l.setObjectName("SbLabel")
                h.addWidget(l)
            v = QLabel(val)
            v.setObjectName("SbMono" if mono else ("SbValueDim" if dim else "SbValue"))
            h.addWidget(v)
            self._vals[key] = v
            lay.addWidget(w)
            sep = QLabel("│")
            sep.setObjectName("SbSep")
            lay.addWidget(sep)

        item("ns", "", "default")
        item("task", "任务", "空闲", dim=True)
        item("dl", "下载", "0 B", mono=True)
        lay.addStretch(1)
        item("speed", "网速", "0 B/s", mono=True)
        item("tg", "tdl", "未检测")

    def set_val(self, key: str, text: str) -> None:
        lab = self._vals.get(key)
        if lab:
            lab.setText(text)


# ============================================================================
# 内容区（含表单 + 日志 + 操作条）
# ============================================================================
# ============================================================================
# 运行环境自检行（只读）
# ============================================================================
class EnvRow(QWidget):
    """一行只读状态：● 名称 ······ 值。

    注意：ElidedLabel 的 paintEvent 走自己的 `_color` 而不是样式表，
    所以改颜色必须直接改 `_color`（见 set_value）。
    """

    _DOT = {"ok": T.OK, "bad": T.DANGER, "warn": T.WARN, "idle": T.TEXT_FAINT}

    def __init__(self, name: str, name_width: int = 84, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)

        self._name = QLabel(name)
        self._name.setFixedWidth(name_width)
        self._name.setStyleSheet(
            f"color:{T.TEXT_DIM}; font-size:12px; background:transparent;")

        self._val = ElidedLabel("", T.TEXT_FAINT)

        lay.addWidget(self._dot)
        lay.addWidget(self._name)
        lay.addWidget(self._val, 1)

        self.set_value("检测中…", "idle")

    def set_value(self, text: str, state: str = "idle") -> None:
        self._dot.setStyleSheet(
            f"background:{self._DOT.get(state, T.TEXT_FAINT)}; border-radius:4px;")
        self._val.setText(text or "")
        self._val._color = T.TEXT if state == "ok" else T.TEXT_DIM
        self._val.update()


class Content(QWidget):
    """
    右侧内容区。

    DOM 对应原型 .content：
      .content-head  标题
      .form          表单（FormRenderer 动态填充）
      .actions       操作条（执行/终止/重置 + 等效命令）
      日志区          追加在下方
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- 标题区（原型 .content-head）：**图标 + 标题 + 描述同一行**，紧凑高度 --
        #    右侧留「页面操作位」：命令页把执行/终止/重置按钮组嵌到这里
        self._head = QWidget()
        self._head.setObjectName("ContentHead")
        hl = QHBoxLayout(self._head)
        hl.setContentsMargins(22, 7, 22, 7)
        hl.setSpacing(10)

        hrow = QHBoxLayout()
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(7)
        self._hico = QLabel("")
        self._hico.setFixedSize(17, 17)
        hrow.addWidget(self._hico)
        self._h1 = QLabel("")
        self._h1.setObjectName("HName")
        hrow.addWidget(self._h1)
        # 描述与标题同行：用省略标签，长描述不会把页头的最小宽度顶大
        self._desc = ElidedLabel("", color=T.TEXT_DIM)
        _df = self._desc.font()
        _df.setPixelSize(12)
        self._desc.setFont(_df)
        hrow.addSpacing(4)
        hrow.addWidget(self._desc, 1)
        hl.addLayout(hrow, 1)

        self._head_slot = QWidget()
        self._head_slot_lay = QHBoxLayout(self._head_slot)
        self._head_slot_lay.setContentsMargins(0, 0, 0, 0)
        self._head_slot_lay.setSpacing(8)
        hl.addWidget(self._head_slot, 0, Qt.AlignVCenter)
        outer.addWidget(self._head)

        # -- 主体：上表单、下日志（可拖动分隔）--
        split = QSplitter(Qt.Vertical)
        split.setObjectName("MainSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)

        # 表单滚动区
        self.scroll = QScrollArea()
        self.scroll.setObjectName("FormScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._form_host = QWidget()
        self._form_host.setObjectName("FormBody")
        self._form_lay = QVBoxLayout(self._form_host)
        self._form_lay.setContentsMargins(22, 0, 22, 16)
        self._form_lay.setSpacing(12)
        self.scroll.setWidget(self._form_host)
        split.addWidget(self.scroll)

        self._pinned: QWidget | None = None   # hub 药丸条等常驻部件

        # 底部：日志（默认收起，执行时自动展开）
        # 操作条（执行/终止/重置）不再是全局常驻：由各命令页渲染时嵌入页面内容，
        # 不需要按钮的页面（任务进度/下载统计/消息浏览/全局设置卡片）自然不显示。
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(22, 8, 22, 14)
        bl.setSpacing(10)
        self._bottom_lay = bl                        # 展开/收起时改留白用

        self.action_bar = ActionBar()

        # 日志标题行（常驻）：标题 + 状态 + 展开/收起。
        # 收起时本区高度 == 状态栏高度（H_STATUSBAR），不留多余空白。
        log_head = QWidget()
        log_head.setFixedHeight(T.H_STATUSBAR)
        self._log_head = log_head
        lh = QHBoxLayout(log_head)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.setSpacing(8)
        lt = QLabel("运行输出")
        lt.setObjectName("FieldLabel")
        lh.addWidget(lt)
        self._log_state = QLabel("就绪")
        self._log_state.setObjectName("FieldHelp")
        lh.addWidget(self._log_state)
        lh.addStretch(1)
        self._log_toggle = QToolButton()
        self._log_toggle.setText("展开 ▾")
        hand_cursor(self._log_toggle)
        self._log_toggle.setAutoRaise(True)
        self._log_toggle.clicked.connect(self.toggle_log)
        lh.addWidget(self._log_toggle)
        bl.addWidget(log_head)

        self.log = LogPanel()
        self.log.setVisible(False)          # 默认收起（原型无日志区）
        bl.addWidget(self.log, 1)

        split.addWidget(bottom)
        outer.addWidget(split, 1)
        self.set_log_visible(False)         # 应用「收起」时的紧凑留白与高度

        # 任务进度面板：跨页保留实例（clear_form 收回不销毁），
        # 渲染左侧「任务进度」页时嵌入表单区；执行与所在页面解耦。
        self.task = TaskBox()

        # 消息浏览：跨页保留实例（同上），已加载的 JSON 切页不丢
        self.msg = MsgBrowser()

    # ---------------------------------------------------------------- 日志
    def toggle_log(self) -> None:
        on = not self.log.isVisible()
        self.set_log_visible(on)

    def set_log_visible(self, on: bool) -> None:
        """展开 / 收起运行输出。

        收起时**不预留空白**：整条只占状态栏高度（`H_STATUSBAR`），
        上下留白与间距都归零；展开时恢复常规留白与分割比例（大小与原来一致）。
        """
        self.log.setVisible(on)
        self._log_toggle.setText("收起 ▴" if on else "展开 ▾")
        if on:
            self._bottom_lay.setContentsMargins(22, 8, 22, 14)
            self._bottom_lay.setSpacing(10)
            self._log_head.setFixedHeight(24)        # 展开时与原来一致
            self.parent_split().setSizes([620, 380])
        else:
            self._bottom_lay.setContentsMargins(22, 0, 22, 0)
            self._bottom_lay.setSpacing(0)
            self._log_head.setFixedHeight(T.H_STATUSBAR)
            self.parent_split().setSizes([1000, T.H_STATUSBAR])

    def parent_split(self) -> QSplitter:
        p = self.log.parentWidget()
        while p is not None and not isinstance(p, QSplitter):
            p = p.parentWidget()
        return p

    def set_log_state(self, text: str) -> None:
        self._log_state.setText(text)

    # ---------------------------------------------------------------- 标题
    def set_head(self, name: str, desc: str, icon: str = "") -> None:
        self.set_head_actions(None)          # 默认无页面操作按钮（命令页再放）
        self._h1.setText(name)
        self._desc.setText(desc)
        if icon:
            ic = ICONS.get(icon, 18, T.TEXT)
            if ic is not None:
                self._hico.setPixmap(ic.pixmap(18, 18))
                self._hico.setVisible(True)
                return
        self._hico.setVisible(False)

    def set_head_actions(self, w: QWidget | None) -> None:
        """装卸标题区右侧的操作部件（命令页的执行/终止/重置按钮组）。"""
        while self._head_slot_lay.count():
            it = self._head_slot_lay.takeAt(0)
            x = it.widget()
            if x is not None:
                x.setParent(None)            # 收回不销毁（按钮组跨页复用）
        if w is not None:
            self._head_slot_lay.addWidget(w)

    def after_render(self) -> None:
        """页面重建后的收尾，消除「界面自己往下跳」：

        · 清掉内容区内的控件焦点 —— 页面重建会销毁旧控件，Qt 会把焦点移交
          给新控件的第一个输入框，QScrollArea 随即自动滚动到它（表现为下移）
        · 内容滚回顶部 —— 换页后应从顶部开始显示
        """
        fw = self.window().focusWidget()
        if fw is not None and self.scroll.isAncestorOf(fw):
            fw.clearFocus()
        sb = self.scroll.verticalScrollBar()
        sb.setValue(0)
        # 布局稳定后再校正一次（内容高度变化会再次影响滚动位置）
        QTimer.singleShot(0, lambda: sb.setValue(0))

    @property
    def form_layout(self) -> QVBoxLayout:
        return self._form_lay

    def clear_form(self) -> None:
        """清空表单区。pinned 部件（hub 药丸条）、等效命令条、任务进度与消息浏览面板保留。"""
        # 先清焦点：否则销毁「持有焦点的控件」时 Qt 会把焦点移交给新控件，
        # QScrollArea 自动滚动到它 —— 表现为切换页面/选项卡后界面自己下移
        fw = self.window().focusWidget()
        if fw is not None and self._form_host.isAncestorOf(fw):
            fw.clearFocus()
        pin = self._pinned
        keep = {id(pin), id(self.task), id(self.msg), id(self.action_bar.cmd_row)}
        while self._form_lay.count():
            it = self._form_lay.takeAt(0)
            w = it.widget()
            if w is None:
                continue
            w.setParent(None)               # 一律先收回
            if id(w) not in keep:
                w.deleteLater()             # 非保留部件销毁
        if pin is not None:
            self._form_lay.addWidget(pin)   # 空了之后放回即 index 0

    def pin_widget(self, w: QWidget) -> None:
        """把 hub 药丸条等常驻部件钉在表单区顶部，clear_form 不清除。"""
        self._pinned = w

    def unpin(self) -> None:
        self._pinned = None


# ============================================================================
# 主窗口
# ============================================================================
class MainWindow(QMainWindow):
    # 后台环境自检 → UI 线程回传（探测要连本机端口和墙外地址，不能阻塞启动）
    env_ready = Signal(dict)
    # 部署任务（探测/安装/修复都是耗时网络操作，一律丢后台线程）
    deploy_status = Signal(dict)          # 环境状态 5 项 {key: (text, state)}
    deploy_proxies = Signal(list)         # 代理探测结果
    deploy_check = Signal(dict)           # 生效检测结果
    deploy_done = Signal(bool, str, list)  # (成功?, 摘要, 日志[(level, msg)])
    deploy_busy_changed = Signal(bool)

    def __init__(self, tdl_path: str = DEFAULT_TDL_PATH):
        super().__init__()
        self.setWindowTitle(f"tdl GUI v{__version__} — Telegram 下载器")
        self.resize(1240, 860)
        self.setMinimumSize(1000, 680)
        # 窗口图标（标题栏 / 任务栏 / Alt-Tab）；子窗口与对话框自动继承
        from .icons import app_icon
        self.setWindowIcon(app_icon())

        # ---- 数据层 ----
        self.tdl_path = tdl_path
        self.store: dict[str, dict] = {}         # {cmdId: {key: value}}
        self.global_store: dict[str, any] = {}   # 全局参数字段值
        self.current_cmd: dict | None = None
        self._hub_active: dict[str, str] = {}
        self._cur_form: FormRenderer | None = None
        # 导出 JSON 状态机：ls（查群名，静默）→ dl_json → msg_json
        self._export_stage: str | None = None
        self._tdl_notes_seen: set[str] = set()   # tdl 固定提示已提示过的（避免刷屏）
        self._tdl_ok = False                     # tdl 启动自检是否通过（连接字段用）
        self._export_vals: dict = {}
        self._export_chat: dict = {}
        self._ls_lines: list[str] = []
        # 当前运行命令的种类（dl/up/forward/…/export），用于统计口径
        self._run_kind = ""
        # 下载目录锚定：记录上次触发锚定的 file 路径（相同则不重复锚定，
        # 避免覆盖用户手动修改的下载目录）
        self._last_anchor_file = ""
        # 链接下载锚定：记录上次解析出的群组标识（群号/username）
        self._last_anchor_url = ""

        # 环境自检（只读）：结果缓存供设置页卡片复用
        self._env_res: dict | None = None
        self._env_rows: dict[str, EnvRow] = {}
        self.env_ready.connect(self._on_env_ready)

        # 部署页（探测/安装都是后台任务，经信号回 UI 线程）
        self._deploy_ui: dict = {}
        self._deploy_busy = False
        self._deploy_buttons: list = []
        self._deploy_last_status: dict = {}
        self._out_errors = 0                     # 输出解析异常计数（只报前几次）
        self.deploy_status.connect(self._on_deploy_status)
        self.deploy_proxies.connect(self._on_deploy_proxies)
        self.deploy_check.connect(self._on_deploy_check)
        self.deploy_done.connect(self._on_deploy_done)
        self.deploy_busy_changed.connect(self._on_deploy_busy)

        # 全局默认值
        for f in GLOBAL_FIELDS:
            self.global_store[f["key"]] = f.get("default")

        # ---- 运行器 ----
        self.runner = TdlRunner(self.tdl_path, self)
        self.runner.line_out.connect(self._on_out)
        self.runner.line_err.connect(self._on_err)
        self.runner.finished_.connect(self._on_finished)
        self.runner.started_.connect(self._on_started)

        # ---- UI ----
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.titlebar = TitleBar()
        lay.addWidget(self.titlebar)

        main = QWidget()
        ml = QHBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self.sidebar = Sidebar()
        self.content = Content()
        self.task_box = self.content.task      # 任务进度面板（跨页保留实例）
        self._bg = False                       # 是否处于「后台」（最小化 / 已隐藏到托盘）
        self._stray_refused: set = set()        # 用户已拒绝清理的残留 pid 集合（不再重复弹窗）
        ml.addWidget(self.sidebar)
        ml.addWidget(self.content, 1)
        lay.addWidget(main, 1)

        self.statusbar = StatusBar()
        lay.addWidget(self.statusbar)

        # ---- 接线 ----
        self.sidebar.nav_picked.connect(self.on_nav)
        self.titlebar.ns_btn.chosen.connect(self._set_ns)
        self.content.action_bar.run_clicked.connect(self.run_current)
        self.content.action_bar.cancel_clicked.connect(self._cancel_current)
        self.task_box.stop_all_requested.connect(self._cancel_current)
        self.content.action_bar.reset_clicked.connect(self._reset_current)

        # 同步命名空间显示（默认值来自 data.py）
        _ns0 = str(self.global_store.get("__ns") or "default")
        self.titlebar.ns_btn.set_name(_ns0)
        # 下拉候选来自本机 ~/.tdl/data 下的库文件（每次弹菜单重新读，新增的会自己出现）
        self.titlebar.ns_btn.set_provider(paths.ns_list)
        self.statusbar.set_val("ns", _ns0)

        # ---- 启动自检 ----
        QTimer.singleShot(60, self._boot_probe)
        QTimer.singleShot(900, self._boot_stray_check)   # 顺带体检残留 tdl 进程

        # 退出兜底：无论走哪条退出路径（菜单退出 / 无托盘时关窗 / app.quit()），
        # 都要把本实例的 tdl 子进程结束掉 —— 否则它会变成残留进程占着账号数据库锁
        _app = QApplication.instance()
        if _app is not None:
            _app.aboutToQuit.connect(self._kill_runner)

        # 初始页
        self.sidebar.set_active("hub-task")
        self.on_nav("hub-task")

        # ---- 系统托盘（关闭键 = 后台运行）----
        self._setup_tray()

    # ================================================================ 系统托盘
    def _setup_tray(self) -> None:
        """托盘图标 + 菜单：关闭键不再退出，而是缩到托盘后台运行。

        为什么：下载可能跑几十分钟，误点 X 把窗口关了进程还在跑却看不见；
        后台化后窗口隐藏、tdl 照常下载，恢复/退出都走托盘菜单。
        托盘不可用的环境（少数远程桌面/精简系统）保持默认关闭行为。
        """
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        from .icons import app_icon
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("tdl GUI — 后台运行中")
        menu = QMenu()
        act_show = QAction("显示主窗口", menu)
        act_show.triggered.connect(self._restore_from_tray)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray_menu = menu                   # 常驻引用，防被 GC 后菜单弹不出
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._restore_from_tray()
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
            else None)
        tray.show()
        self._tray = tray
        self._tray_hinted = False                # 首次关闭时提示一次

    def closeEvent(self, e) -> None:
        """点关闭键 → 隐藏到托盘后台运行（托盘菜单「退出」才是真退出）。"""
        if self._tray is not None and self._tray.isVisible():
            e.ignore()
            self.hide()
            self._enter_background()             # 后台运行：卸载界面内存
            if not getattr(self, "_tray_hinted", False):
                self._tray_hinted = True
                self._tray.showMessage(
                    "tdl GUI",
                    "软件已最小化到托盘，下载继续进行。\n"
                    "点击托盘图标恢复窗口，右键托盘可退出。",
                    QSystemTrayIcon.Information, 4000)
            return
        # 没有托盘（环境不支持托盘）时这里是**唯一**的退出路径 —— 必须把 tdl
        # 一起结束，否则它会成为残留进程、一直占着 tdl 的账号数据库锁（用户实测踩过）
        self._kill_runner()
        super().closeEvent(e)

    def _restore_from_tray(self) -> None:
        """从托盘恢复主窗口（单击/双击托盘图标或菜单项）。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._leave_background()                 # 恢复前台 → 重新加载被卸载的内容

    # ================================================================ 后台内存
    def _enter_background(self) -> None:
        """切到后台（最小化 / 关闭到托盘）：卸载主界面加载的大块内存。

        为什么值得做：消息浏览是**跨页常驻实例**，打开过一个几千条消息的群之后，
        消息正文、文件索引与已渲染的卡片会一直占着内存；而程序在后台运行时用户
        根本看不到界面，这些内容没有保留的必要。这里把它们卸载掉，并把空闲内存
        真正还给系统（`trim_memory`），回到前台时再按需重新加载（`_leave_background`）。
        """
        if self._bg:
            return
        self._bg = True
        before = procutil.working_set_mb()
        freed = 0
        try:
            freed = self.content.msg.release_memory()
        except Exception as e:                   # noqa: BLE001 - 后台逻辑不阻断
            try:
                self.content.log.err(f"[后台] 释放消息内容失败：{e}")
            except Exception:                    # noqa: BLE001
                pass
        try:
            from PySide6.QtGui import QPixmapCache
            QPixmapCache.clear()                 # Qt 全局 pixmap 缓存（默认上限 10MB）
        except Exception:                        # noqa: BLE001
            pass
        procutil.trim_memory()                   # gc + 收缩堆 + 把空闲页还给系统
        if freed:
            after = procutil.working_set_mb()
            mem = (f"，内存 {before:.0f} MB → {after:.0f} MB"
                   if before and after else "")
            try:
                self.content.log.sys(
                    f"[后台] 已卸载消息浏览内容（{freed} 项）{mem}；"
                    f"回到窗口后自动重新加载。")
            except Exception:                    # noqa: BLE001
                pass

    def _leave_background(self) -> None:
        """回到前台：把卸载掉的内容重新加载回来（没卸载过就是空操作）。"""
        self._bg = False
        try:
            if self.content.msg.restore_memory():
                self.content.log.sys("[后台] 已重新加载消息浏览内容。")
        except Exception as e:                   # noqa: BLE001
            try:
                self.content.log.err(f"[后台] 重新加载失败：{e}")
            except Exception:                    # noqa: BLE001
                pass

    def changeEvent(self, e) -> None:
        """最小化 = 进入后台（同样卸载内存；恢复显示时由 showEvent 重新加载）。"""
        try:
            if e.type() == QEvent.WindowStateChange and self.isMinimized():
                self._enter_background()
        except Exception:                        # noqa: BLE001
            pass
        super().changeEvent(e)

    def showEvent(self, e) -> None:
        """窗口重新显示（含从托盘恢复、从最小化还原）→ 按需重新加载。"""
        super().showEvent(e)
        QTimer.singleShot(0, self._leave_background)

    def _kill_runner(self) -> tuple:
        """结束本实例的 tdl 子进程并**确认真的结束了**（所有退出路径统一走这里）。

        用 runner.shutdown()（同步 + 校验）而不是 cancel()（异步）：退出后不该再
        有任何本实例启动的 tdl 活着，否则下次启动命令只会看到
        `Current database is used by another process`。
        """
        try:
            return self.runner.shutdown()
        except Exception:                        # noqa: BLE001 - 退出路径不再抛错
            return True, []

    def _quit_app(self) -> None:
        """真退出：先结束 tdl 子进程（等同「全部停止」），再收托盘、退应用。"""
        ok, left = self._kill_runner()
        try:
            if left:
                self.content.log.err(
                    f"[退出] tdl 进程未能在超时内结束：{left}。"
                    f"请在任务管理器手动结束，否则它会占用 tdl 的账号数据库。")
            else:
                self.content.log.sys("[退出] 已结束本次的 tdl 进程。")
        except Exception:                        # noqa: BLE001
            pass
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    # ================================================================ tdl 进程体检
    def _start(self, argv: list) -> bool:
        """启动 tdl 的**统一入口**：先做「残留进程」体检，再交给 runner 启动。

        为什么必须查：tdl 的账号数据（`~/.tdl/data/<ns>`）是 bbolt 库，同一时刻
        **只允许一个进程打开**（排他文件锁，超时立即失败）。上次 GUI 异常结束
        （被任务管理器结束 / 崩溃）可能留下一个仍在下载的 tdl 子进程，它一直占着
        锁；这时启动任何命令，tdl 只会打印一句英文
        `Current database is used by another process…` 就退出。
        这里提前发现，并给用户「结束残留进程并继续」的一键选择。
        """
        stray = procutil.stray_tdl_pids(self.runner.child_pids)
        if stray:
            key = frozenset(stray)
            # 用户刚拒绝过同一批 → 不再重复弹模态框（导出是多阶段的，会连环弹）；
            # 直接当作「未处理」拦下本次启动，日志里说明。
            if key in self._stray_refused:
                try:
                    self.content.log.err(
                        "[已取消] 残留的 tdl 进程仍在运行，未启动新命令"
                        "（同一批进程本次不再重复询问）。")
                except Exception:                # noqa: BLE001
                    pass
                return False
            if not self._ask_kill_stray(stray):
                self._stray_refused.add(key)
                try:
                    self.content.log.err(
                        "[已取消] 残留的 tdl 进程仍在运行，未启动新命令。")
                except Exception:                # noqa: BLE001
                    pass
                return False
            self._stray_refused.discard(key)     # 已处理 → 忘掉这条记录
        return self.runner.start(argv)

    def _ask_kill_stray(self, pids: list) -> bool:
        """提示存在残留 tdl 进程并询问是否结束；同意则结束并返回 True。"""
        try:
            self.content.set_log_visible(True)
            self.content.log.err(
                "[体检] 发现 %d 个残留的 tdl 进程：%s\n"
                "　　　它们占用了 tdl 的账号数据库（同一时刻只允许一个进程使用）。"
                % (len(pids), "、".join(procutil.describe_processes(pids))))
        except Exception:                        # noqa: BLE001
            pass
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("检测到残留的 tdl 进程")
        box.setText("tdl 的账号数据库同一时刻只能被一个进程使用。")
        box.setInformativeText(
            "当前有 %d 个 tdl 进程正在运行（%s），\n"
            "多半是上次异常退出时留下的 —— 你现在这个窗口控制不了它们，\n"
            "不结束的话新命令会因为数据库被占用而起不来。\n\n"
            "结束它们并继续吗？已下载完成的文件不会丢；\n"
            "带「跳过已下载 / 续传」的下载会跳过它们接着下。"
            % (len(pids), "、".join("PID %d" % p for p in pids[:6])))
        b_kill = box.addButton("结束残留进程并继续", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(b_kill)
        box.exec()
        if box.clickedButton() is not b_kill:
            return False
        ok, bad = procutil.kill_pids(pids)
        try:
            if bad:
                self.content.log.err(
                    "[体检] 已结束 %d 个；%d 个无法结束（%s），"
                    "可能是权限不足，请到任务管理器手动结束。"
                    % (ok, len(bad), bad))
            else:
                self.content.log.sys(
                    "[体检] 已结束 %d 个残留的 tdl 进程，可以继续了。" % ok)
        except Exception:                        # noqa: BLE001
            pass
        return True

    def _clean_stray_tdl(self) -> None:
        """「环境部署」页的手动入口：结束所有不属于本实例的 tdl 进程。"""
        try:
            self.content.set_log_visible(True)
            stray = procutil.stray_tdl_pids(self.runner.child_pids)
            if not stray:
                self.content.log.sys(
                    "[体检] 未发现残留的 tdl 进程（账号数据库当前没被占用）。")
                return
            if self._ask_kill_stray(stray):
                self.content.log.sys("[体检] 清理完成。")
        except Exception as e:                   # noqa: BLE001
            try:
                self.content.log.err("[体检] 出错：%s" % e)
            except Exception:                    # noqa: BLE001
                pass

    def _boot_stray_check(self) -> None:
        """启动时体检一次：发现别的 tdl 在跑就立刻在日志里说明白。"""
        try:
            stray = procutil.stray_tdl_pids(self.runner.child_pids)
            if stray:
                self.content.log.err(
                    "[体检] 检测到 %d 个残留的 tdl 进程：%s\n"
                    "　　　它会占用 tdl 的账号数据库（同一时刻只允许一个进程使用），"
                    "导致新命令起不来。\n"
                    "　　　到左侧「环境部署」页点「结束残留 tdl 进程」即可清理。"
                    % (len(stray), "、".join(procutil.describe_processes(stray))))
        except Exception:                        # noqa: BLE001
            pass

    # ================================================================ 启动自检
    def _boot_probe(self) -> None:
        ok, msg = self.runner.available()
        if not ok:
            self.content.log.err(f"[启动自检] tdl 不可用：{msg}")
            self.titlebar.set_version("tdl 未找到")
            self._tdl_ok = False
            self.statusbar.set_val("tg", "不可用")
            self._boot_envcheck()      # 找不到 tdl 也要把环境情况报出来
            return

        ok, text = self.runner.probe_version()
        if ok:
            # text 形如 "Version: 0.20.4\nCommit: ...\n..."
            ver = ""
            for ln in text.splitlines():
                if ln.lower().startswith("version"):
                    ver = ln.split(":", 1)[-1].strip()
                    break
            self.titlebar.set_version(f"tdl {ver or 'v?'}")
            self._tdl_ok = True
            self.statusbar.set_val("tg", self._conn_text())
            self.content.log.sys(f"[启动自检] tdl {ver} 可用 · {self.tdl_path}")
            self.content.log.sys(
                "[提示] 若提示 not authorized，请先到「任务 → 登录」完成登录。"
            )
        else:
            self.titlebar.set_version("tdl 探测失败")
            self._tdl_ok = False
            self.statusbar.set_val("tg", "异常")
            self.content.log.err(f"[启动自检] {text}")

        # 代理 / 系统时间 / ffmpeg 的体检丢后台跑，别卡启动
        self._boot_envcheck()

    # 命令类型 → 状态栏中文（任务进行中时显示）
    _KIND_CN = {
        "dl": "下载中", "up": "上传中", "forward": "转发中",
        "export": "导出中", "login": "登录中", "chat-ls": "查询中",
        "chat-users": "导出成员中", "migrate": "迁移中", "backup": "备份中",
        "recover": "恢复中", "update": "检查更新中", "version": "查询版本中",
    }

    def _set_task_status(self, text: str, group: str = "") -> None:
        """状态栏「任务」字段：进行中 +（可选的）当前任务所属群组名。"""
        if group:
            g = group if len(group) <= 16 else group[:15] + "…"
            self.statusbar.set_val("task", f"{text} · {g}")
        else:
            self.statusbar.set_val("task", text)

    def _conn_text(self) -> str:
        """状态栏连接字段：显示实际走哪条链路连到 Telegram。

        tdl 的 CLI 不暴露所连数据中心（DC）编号（version / --debug / login
        都没有），因此这里展示可确认的连接目标，优先级：
            命令行的 --proxy 参数  >  系统环境变量 TDL_PROXY  >  官方直连
        系统已配好代理时直接沿用，**只读不改**。
        """
        proxy = str(self.global_store.get("__proxy") or "").strip()
        if not proxy:
            env_proxy, _key = envcheck.system_proxy()
            if env_proxy:
                host = env_proxy.split("@")[-1].split("://")[-1].strip("/")
                return f"代理 {host}（环境变量）"
            return "直连 Telegram"
        host = proxy.split("@")[-1].split("://")[-1].strip("/")
        return f"代理 {host}"

    def _sync_conn_status(self) -> None:
        """代理参数变化后刷新状态栏连接信息（tdl 不可用时不动）。"""
        if getattr(self, "_tdl_ok", False):
            self.statusbar.set_val("tg", self._conn_text())

    # ================================================================ 环境自检（只读）
    def _boot_envcheck(self) -> None:
        """后台跑一次环境体检：tdl / ffmpeg / 代理 / 系统时间 / 登录。

        为什么丢后台：探测要连本机端口、还要请求墙外地址验证连通性，耗时数秒，
        放主线程会把启动卡住。
        本模块是**只读**的 —— 不写环境变量、不改配置、不下载安装。
        """
        def work():
            logs: list[tuple[str, str]] = []
            try:
                res = envcheck.check_env(
                    self.tdl_path, deep=True,
                    log=lambda lv, m: logs.append((lv, m)))
            except Exception as exc:                          # noqa: BLE001
                res = {"error": f"{type(exc).__name__}: {exc}"}
            res["_logs"] = logs
            self.env_ready.emit(res)

        threading.Thread(target=work, daemon=True).start()

    def _on_env_ready(self, res: dict) -> None:
        self._env_res = res
        for lv, msg in res.get("_logs", []):
            if lv in ("warn", "err"):
                self.content.log.sys(f"[环境自检] {msg}")
        if res.get("error"):
            self.content.log.err(f"[环境自检] 未完成：{res['error']}")
        else:
            p = res.get("proxy", {})
            if p.get("tone") in ("warn", "bad"):
                self.content.log.sys(f"[环境自检] {p.get('advice', '')}")
        self._fill_env_rows()

    def _env_recheck(self) -> None:
        """设置页「重新检测」按钮（只读，不改任何东西）。"""
        self._env_res = None
        for row in self._env_rows.values():
            row.set_value("检测中…", "idle")
        self._boot_envcheck()

    def _fill_env_rows(self) -> None:
        if not self._env_rows:
            return
        res = self._env_res
        if not res or res.get("error"):
            for row in self._env_rows.values():
                row.set_value("未完成", "idle")
            return

        t = res["tdl"]
        self._env_rows["tdl"].set_value(
            (f"{t['version'] or '?'} · {t['path']}" if t["ok"]
             else f"未找到（{t['path'] or '无可用路径'}）"),
            "ok" if t["ok"] else "bad")

        f = res["ffmpeg"]
        src_cn = {"local": "软件目录", "system": "系统 PATH", "none": ""}
        self._env_rows["ffmpeg"].set_value(
            (f"{src_cn.get(f['source'], '')} · {f['path']}" if f["ok"]
             else "未安装（仅影响 --rewrite-ext 转码）"),
            "ok" if f["ok"] else "warn")

        p = res["proxy"]
        if p["env"]:
            state = "ok" if p["alive"] else ("bad" if p["alive"] is False else "warn")
            self._env_rows["proxy"].set_value(
                f"{p['env_key']} = {p['env']}" + ("  ✓连通" if p["alive"] else ""),
                state)
        else:
            self._env_rows["proxy"].set_value(
                p.get("advice") or "系统未配置代理", "warn")
        self._env_rows["proxy"].setToolTip(p.get("advice", ""))

        lg = res["login"]
        self._env_rows["login"].set_value(
            ("、".join(lg["namespaces"]) if lg["ok"] else "无会话，需登录"),
            "ok" if lg["ok"] else "warn")

    def _render_env_card(self) -> None:
        """设置页的「运行环境」卡片 —— 纯只读，不提供任何修改入口。

        代理的写入与修改一律在「tdl 部署助手」里做；本程序只负责发现与告警，
        系统已有代理就直接沿用。
        """
        gb = GroupBox("运行环境")
        self._env_rows = {}
        for key, label in [("tdl", "tdl 主程序"), ("ffmpeg", "ffmpeg"),
                           ("proxy", "代理"), ("login", "登录")]:
            row = EnvRow(label)
            self._env_rows[key] = row
            gb.add(row)

        tip = QLabel("只读体检。代理的写入与修改请到左侧「环境部署」页 —— "
                     "系统已有代理则直接沿用，本卡片不会改动任何配置。")
        tip.setObjectName("FieldHelp")
        tip.setWordWrap(True)
        gb.add(tip)

        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("重新检测")
        btn.setObjectName("Btn")
        btn.clicked.connect(self._env_recheck)
        bl.addWidget(btn)
        bl.addStretch(1)
        gb.add(bar)

        self.content.form_layout.addWidget(gb)
        self._fill_env_rows()

    # ================================================================ 环境部署页
    def _dep_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setFixedWidth(84)
        lab.setStyleSheet(
            f"color:{T.TEXT_DIM}; font-size:12px; background:transparent;")
        return lab

    def _dep_button(self, text: str, fn, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("BtnPrimary" if primary else "Btn")
        b.clicked.connect(fn)
        self._deploy_buttons.append(b)
        return b

    def _dep_input(self, gb: GroupBox, label: str, text: str = "",
                   placeholder: str = "", ro: bool = False) -> QLineEdit:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self._dep_label(label))
        ed = QLineEdit(text)
        ed.setPlaceholderText(placeholder)
        if ro:
            ed.setReadOnly(True)
        h.addWidget(ed, 1)
        gb.add(w)
        return ed

    def _dep_combo(self, gb: GroupBox, label: str, placeholder: str = "") -> QComboBox:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self._dep_label(label))
        cb = QComboBox()
        if placeholder:
            cb.setPlaceholderText(placeholder)
        cb.setEnabled(False)
        h.addWidget(cb, 1)
        gb.add(w)
        return cb

    def _dep_check(self, gb: GroupBox, text: str, checked: bool = False) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setChecked(checked)
        cb.setStyleSheet(
            f"color:{T.TEXT_DIM}; font-size:12px; background:transparent;")
        gb.add(cb)
        return cb

    def render_deploy_page(self, cmd: dict) -> None:
        """「环境部署」独立页 —— 部署助手完整能力（已合并进 GUI）。"""
        self.current_cmd = None
        self.content.set_head(cmd["name"], cmd.get("desc", ""), cmd.get("icon", ""))
        self.content.set_head_actions(None)
        self.content.clear_form()

        ui = self._deploy_ui = {}
        self._deploy_buttons = []
        form = self.content.form_layout
        d = paths.app_root()

        # ── ① 环境状态 ──
        gb = GroupBox("环境状态")
        for key, label in [("tdl", "tdl 主程序"), ("ffmpeg", "ffmpeg"),
                           ("path", "用户 PATH"), ("env", "TDL_* 变量"),
                           ("login", "Telegram 登录")]:
            row = EnvRow(label)
            ui[f"st_{key}"] = row
            gb.add(row)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self._dep_button("重新检测", self._dep_check_status))
        bl.addStretch(1)
        gb.add(bar)
        form.addWidget(gb)

        # ── ② 安装设置 ──
        gb2 = GroupBox("安装设置")
        ui["dir"] = self._dep_input(gb2, "部署位置", str(d), ro=True)
        w_proxy = QWidget()
        h = QHBoxLayout(w_proxy)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(self._dep_label("代理地址"))
        ui["proxy"] = QLineEdit()
        ui["proxy"].setPlaceholderText("http://127.0.0.1:7890（留空则不写代理）")
        h.addWidget(ui["proxy"], 1)
        h.addWidget(self._dep_button("自动探测", self._dep_sniff))
        gb2.add(w_proxy)
        ui["found"] = self._dep_combo(gb2, "探测结果", "点「自动探测」查找本机代理")
        ui["found"].currentIndexChanged.connect(self._dep_pick_proxy)
        ui["channel"] = self._dep_combo(gb2, "下载通道")
        for name, base in deploy.MIRRORS:
            ui["channel"].addItem(name, base)
        ui["channel"].setToolTip(
            "国内直连 github.com 的 release 域名常被拦截；下载失败就换镜像")
        ui["cb_dl_proxy"] = self._dep_check(gb2, "下载依赖时也走上面的代理")
        ui["cb_ffmpeg"] = self._dep_check(gb2, "同时安装 ffmpeg（约 80 MB，转码需要）", True)
        ui["cb_force"] = self._dep_check(gb2, "强制重装（忽略已是最新版的判断）")
        form.addWidget(gb2)

        # ── ③ 运行参数 ──
        gb3 = GroupBox("tdl 运行参数（写用户级环境变量 TDL_*）")
        ui["env_fields"] = {}
        for name, cname, default, hint in deploy.ENV_SPECS:
            ed = self._dep_input(gb3, cname, deploy.ENV_DEFAULTS[name], hint)
            ed.setToolTip(f"{name} —— {hint}")
            ui["env_fields"][name] = ed
        form.addWidget(gb3)

        # ── ④ 执行 ──
        gb4 = GroupBox("执行")
        bar4 = QWidget()
        b4 = QHBoxLayout(bar4)
        b4.setContentsMargins(0, 0, 0, 0)
        b4.setSpacing(8)
        b4.addWidget(self._dep_button("⚡ 一键部署", self._dep_deploy_all, True))
        b4.addWidget(self._dep_button("安装 tdl", lambda: self._dep_step("tdl")))
        b4.addWidget(self._dep_button("安装 ffmpeg", lambda: self._dep_step("ffmpeg")))
        b4.addWidget(self._dep_button("写入 PATH", lambda: self._dep_step("path")))
        b4.addWidget(self._dep_button("写入变量", lambda: self._dep_step("env")))
        b4.addWidget(self._dep_button("结束残留 tdl 进程", self._clean_stray_tdl))
        b4.addStretch(1)
        gb4.add(bar4)
        w4 = QWidget()
        h4 = QHBoxLayout(w4)
        h4.setContentsMargins(0, 0, 0, 0)
        h4.setSpacing(8)
        h4.addWidget(self._dep_label("登录"))
        ui["login_mode"] = QComboBox()
        ui["login_mode"].addItems(["扫码 qr（推荐）", "验证码 code", "桌面会话 desktop"])
        h4.addWidget(ui["login_mode"], 1)
        ui["ns"] = QLineEdit("default")
        ui["ns"].setFixedWidth(96)
        ui["ns"].setPlaceholderText("命名空间")
        h4.addWidget(ui["ns"])
        h4.addWidget(self._dep_button("打开登录终端", self._dep_login))
        gb4.add(w4)
        form.addWidget(gb4)

        # ── ⑤ 生效检测与修复 ──
        gb5 = GroupBox("生效检测与修复")
        for key, label in [("written", "变量已写入"), ("visible", "当前进程可见"),
                           ("reachable", "代理可连外网"), ("tdl_direct", "tdl 显式代理"),
                           ("tdl_env", "tdl 用环境变量"), ("time", "系统时间偏差"),
                           ("ntp", "tdl NTP 校准")]:
            row = EnvRow(label, name_width=104)
            ui[f"ck_{key}"] = row
            gb5.add(row)
        bar5 = QWidget()
        b5 = QHBoxLayout(bar5)
        b5.setContentsMargins(0, 0, 0, 0)
        b5.setSpacing(8)
        b5.addWidget(self._dep_button("检测生效", self._dep_check_effective))
        b5.addWidget(self._dep_button("一键修复", self._dep_fix))
        b5.addStretch(1)
        gb5.add(bar5)
        ui["advice"] = QLabel("点「检测生效」逐项验证（最后两项用 tdl 端到端实测）")
        ui["advice"].setObjectName("FieldHelp")
        ui["advice"].setWordWrap(True)
        gb5.add(ui["advice"])
        form.addWidget(gb5)

        # ── ⑥ 详细部署流程（无法自动化的部分全在这里）──
        flow = QTextBrowser()
        flow.setObjectName("DeployFlow")
        flow.setOpenExternalLinks(True)
        flow.setStyleSheet(
            f"QTextBrowser{{background:{T.PANEL}; border:1px solid {T.BORDER};"
            f"border-radius:9px; padding:4px 10px;}}")
        flow.setHtml(deploy_flow.build_html())
        flow.setMinimumHeight(340)
        form.addWidget(flow)

        # 进入页面自动体检一次
        self._dep_check_status()

    # ---- 部署页：后台任务 ----
    def _deploy_run(self, fn, done_msg: str = "") -> None:
        if self._deploy_busy:
            self.content.log.sys("[部署] 已有任务在执行，请稍候")
            return
        self._deploy_busy = True
        self.deploy_busy_changed.emit(True)

        def work():
            logs: list[tuple[str, str]] = []
            try:
                note = fn(lambda lv, m: logs.append((lv, m)))
                self.deploy_done.emit(True, note or done_msg, logs)
            except Exception as exc:                          # noqa: BLE001
                self.deploy_done.emit(False, f"{type(exc).__name__}: {exc}", logs)

        threading.Thread(target=work, daemon=True).start()

    def _on_deploy_done(self, ok: bool, note: str, logs: list) -> None:
        self._deploy_busy = False
        self.deploy_busy_changed.emit(False)
        for lv, m in logs:
            if lv == "err":
                self.content.log.err(f"[部署] {m}")
            else:
                self.content.log.sys(f"[部署] {m}")
        (self.content.log.sys if ok else self.content.log.err)(
            f"[部署] {'✓ ' if ok else '✗ '}{note}")

    def _on_deploy_busy(self, busy: bool) -> None:
        for b in getattr(self, "_deploy_buttons", []):
            b.setEnabled(not busy)

    # ---- 部署页：环境状态 ----
    def _dep_collect_status(self) -> dict:
        d = paths.app_root()
        exe = envcheck.resolve_tdl_path()
        ver = envcheck.tdl_version(exe) if exe else ""
        ff_local = (d / "ffmpeg.exe").exists()
        ff_sys = envcheck.system_ffmpeg()
        envs = {n: (os.environ.get(n) or "").strip()
                for n in [deploy.ENV_PROXY_KEY] + deploy.ENV_NAMES}
        n_set = sum(1 for v in envs.values() if v)
        ns = envcheck.namespaces()
        has_path = deploy.path_has(str(d))
        return {
            "tdl": (f"v{ver} · {exe}" if exe else "未安装（可在本页一键安装）",
                    "ok" if exe else "bad"),
            "ffmpeg": ((f"软件目录 · {d / 'ffmpeg.exe'}" if ff_local
                        else f"系统 · {ff_sys}") if (ff_local or ff_sys)
                       else "未安装（仅 --rewrite-ext 转码需要）",
                       "ok" if (ff_local or ff_sys) else "warn"),
            "path": (f"已包含 {d}" if has_path else f"未包含 {d}",
                     "ok" if has_path else "bad"),
            "env": ("已配置 " + "、".join(
                        f"{k.replace('TDL_', '')}={v}"
                        for k, v in envs.items() if v) if n_set
                    else "未配置任何 TDL_* 变量",
                    "ok" if n_set else "warn"),
            "login": ("、".join(ns) if ns else "无会话，需登录",
                      "ok" if ns else "warn"),
        }

    def _dep_check_status(self) -> None:
        def job(log):
            self.deploy_status.emit(self._dep_collect_status())
            return "环境检测完成"

        self._deploy_run(job, "环境检测完成")

    def _on_deploy_status(self, st: dict) -> None:
        self._deploy_last_status = st
        for key, (text, state) in st.items():
            row = self._deploy_ui.get(f"st_{key}")
            if row:
                row.set_value(text, state)

    # ---- 部署页：代理探测 ----
    def _dep_sniff(self) -> None:
        def job(log):
            found = envcheck.sniff_proxies(deep=True, log=log)
            self.deploy_proxies.emit(found)
            if not found:
                log("warn", "没找到可用代理：确认代理软件已启动，或手动填地址")
                return "未找到代理"
            ok = [c for c in found if c.get("verified")]
            log("ok", f"共 {len(found)} 个候选，{len(ok)} 个已验证可用；"
                      f"推荐 {found[0]['url']}")
            return "代理探测完成"

        self._deploy_run(job, "代理探测完成")

    def _on_deploy_proxies(self, found: list) -> None:
        cb = self._deploy_ui.get("found")
        if not cb:
            return
        cb.blockSignals(True)
        cb.clear()
        for c in found:
            cb.addItem(c["label"], c["url"])
        cb.setCurrentIndex(0 if found else -1)
        cb.blockSignals(False)
        cb.setEnabled(bool(found))
        if found:
            self._deploy_ui["proxy"].setText(found[0]["url"])

    def _dep_pick_proxy(self, idx: int) -> None:
        if idx < 0:
            return
        cb = self._deploy_ui.get("found")
        url = cb.itemData(idx) if cb else None
        if url:
            self._deploy_ui["proxy"].setText(url)
            self.content.log.sys(f"[部署] 已选用代理：{url}")

    # ---- 部署页：安装 / 修复 ----
    def _dep_deploy_all(self) -> None:
        ui = self._deploy_ui
        d = paths.app_root()
        proxy = ui["proxy"].text().strip()
        mirror = ui["channel"].currentData() or ""
        dl_proxy = (proxy or None) if ui["cb_dl_proxy"].isChecked() else None
        force = ui["cb_force"].isChecked()
        do_ff = ui["cb_ffmpeg"].isChecked()
        ns = ui["ns"].text().strip() or "default"
        envs = {n: ui["env_fields"][n].text().strip() for n in deploy.ENV_NAMES}
        envs[deploy.ENV_PROXY_KEY] = proxy

        def job(log):
            log("step", "▶ 一键部署开始")
            route = "镜像 " + mirror if mirror else "直连 GitHub"
            if dl_proxy:
                route += f" + 代理 {dl_proxy}"
            log("info", f"下载通道：{route}")
            deploy.install_tdl(log, d, dl_proxy, force, None, mirror)
            if do_ff:
                deploy.install_ffmpeg(log, d, dl_proxy, force, None, mirror)
            deploy.ensure_path(log, d)
            deploy.apply_env(log, envs)
            deploy.export_state(d, {"proxy": proxy})
            res = deploy.check_effective(proxy, ns, run_tdl=True, log=log)
            self.deploy_check.emit(res)
            self.deploy_status.emit(self._dep_collect_status())
            log("ok" if res["ok"] else "warn", res["advice"])
            return "一键部署完成"

        self._deploy_run(job, "一键部署完成")

    def _dep_step(self, key: str) -> None:
        ui = self._deploy_ui
        d = paths.app_root()
        proxy = ui["proxy"].text().strip()
        mirror = ui["channel"].currentData() or ""
        dl_proxy = (proxy or None) if ui["cb_dl_proxy"].isChecked() else None
        force = ui["cb_force"].isChecked()
        names = {"tdl": "安装 tdl", "ffmpeg": "安装 ffmpeg",
                 "path": "写入 PATH", "env": "写入环境变量"}

        def job(log):
            if key == "tdl":
                deploy.install_tdl(log, d, dl_proxy, force, None, mirror)
            elif key == "ffmpeg":
                deploy.install_ffmpeg(log, d, dl_proxy, force, None, mirror)
            elif key == "path":
                deploy.ensure_path(log, d)
            elif key == "env":
                envs = {n: ui["env_fields"][n].text().strip()
                        for n in deploy.ENV_NAMES}
                envs[deploy.ENV_PROXY_KEY] = proxy
                deploy.apply_env(log, envs)
            deploy.export_state(d, {"proxy": proxy})
            self.deploy_status.emit(self._dep_collect_status())
            return names.get(key, key) + " 完成"

        self._deploy_run(job, names.get(key, key))

    # ---- 部署页：生效检测 / 修复 / 登录 ----
    def _dep_check_effective(self) -> None:
        ui = self._deploy_ui
        proxy = ui["proxy"].text().strip()
        ns = ui["ns"].text().strip() or "default"

        def job(log):
            res = deploy.check_effective(proxy, ns, run_tdl=True, log=log)
            self.deploy_check.emit(res)
            log("ok" if res["ok"] else "warn", res["advice"])
            return "生效检测完成"

        self._deploy_run(job, "生效检测完成")

    def _dep_fix(self) -> None:
        ui = self._deploy_ui
        proxy = ui["proxy"].text().strip()
        ns = ui["ns"].text().strip() or "default"
        d = paths.app_root()

        def job(log):
            deploy.fix_effective(proxy, d, ns, log=log)
            log("step", "修复后复检…")
            res = deploy.check_effective(proxy, ns, run_tdl=True, log=log)
            self.deploy_check.emit(res)
            log("ok" if res["ok"] else "warn", res["advice"])
            return "修复完成"

        self._deploy_run(job, "修复完成")

    def _on_deploy_check(self, res: dict) -> None:
        for it in res.get("items", []):
            row = self._deploy_ui.get(f"ck_{it['key']}")
            if not row:
                continue
            ok = it["ok"]
            row.set_value(it["text"],
                          "ok" if ok else ("idle" if ok is None else "bad"))
            row.setToolTip(it.get("hint", ""))
        adv = self._deploy_ui.get("advice")
        if adv:
            adv.setText(res.get("advice", ""))

    def _dep_login(self) -> None:
        ui = self._deploy_ui
        exe = envcheck.resolve_tdl_path()
        if not exe:
            self.content.log.err("[部署] 找不到 tdl.exe，请先在本页安装")
            return
        mode = ["qr", "code", "desktop"][ui["login_mode"].currentIndex()]
        ns = ui["ns"].text().strip() or "default"
        proxy = ui["proxy"].text().strip()
        cmd = deploy.open_login_terminal(mode, ns, proxy, exe)
        self.content.log.sys(f"[部署] 已在新窗口打开登录：{cmd}")

    def _set_ns(self, name: str) -> None:
        """切换到指定命名空间（由顶栏下拉菜单选中）。"""
        nxt = str(name or "").strip()
        if not nxt or nxt == self.global_store.get("__ns"):
            return
        self.global_store["__ns"] = nxt
        self.titlebar.ns_btn.set_name(nxt)
        self.statusbar.set_val("ns", nxt)
        self.content.log.sys(f"[命名空间] 已切换到 {nxt}")
        self._refresh_cmd_preview()

    # ================================================================ 路由
    def on_nav(self, cmd_id: str) -> None:
        c = CMD_BY_ID.get(cmd_id)
        if not c:
            return
        self.sidebar.set_active(cmd_id)
        self.content.unpin()                 # 直达命令页：旧 hub 药丸条可清除

        kind = c.get("kind")
        if kind == "hub":
            hub = c.get("hub", "")
            # 与原型一致：任务 hub 默认选「下载」，其他 hub 取第一个成员
            default_active = "dl" if hub == "task" else (c.get("members") or ["" ])[0]
            active = self._hub_active.get(hub) or default_active
            self._hub_active[hub] = active
            self.render_hub(c, active)
        elif kind == "tasks":
            self.render_tasks_page(c)
        elif kind == "stats":
            self.render_stats_page(c)
        elif kind == "msgview":
            self.render_msgview_page(c)
        elif kind == "deploy":
            self.render_deploy_page(c)
        else:
            self.render_cmd(c)
        self.content.after_render()          # 收尾：清内容区焦点 + 回到顶部

    # ---------------------------------------------------------------- Hub
    def render_hub(self, hub_cmd: dict, active_id: str) -> None:
        self.content.unpin()                 # 旧药丸条解除保护（可被清除）
        self.content.set_head(hub_cmd["name"], hub_cmd.get("desc", ""),
                              hub_cmd.get("icon", ""))
        self.content.clear_form()

        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 6)
        bl.setSpacing(T.TAB_GAP)

        for mid in hub_cmd.get("members", []):
            mc = CMD_BY_ID.get(mid)
            if not mc and mid.startswith("__g_"):
                mc = next((g for g in GLOBAL_CARDS if g["id"] == mid), None)
            if not mc:
                continue
            t = HubTab(mc["name"], mc.get("icon", ""))
            t.set_on(mid == active_id)
            t.clicked.connect(lambda _=None, x=mid, h=hub_cmd: self._switch_hub(h, x))
            bl.addWidget(t)
        bl.addStretch(1)

        # 药丸条放进横向滚动容器：成员多时（如设置页 8 个）横向滚动，
        # 而不是把页面最小宽度撑出可见范围
        holder = QScrollArea()
        holder.setObjectName("PillScroll")
        holder.setWidgetResizable(True)
        holder.setFrameShape(QFrame.NoFrame)
        holder.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        holder.setMinimumWidth(0)
        holder.setFixedHeight(T.H_TAB + 18)          # 药丸高 + 边距 + 细滚动条
        holder.setStyleSheet(
            "QScrollBar:horizontal { height:6px; background:transparent; margin:0; }"
            "QScrollBar::handle:horizontal { background:#3a424b; border-radius:3px;"
            " min-width:28px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }"
        )
        holder.setWidget(bar)

        self.content.pin_widget(holder)      # 钉住：成员页 render_cmd 不再清掉它
        self.content.form_layout.addWidget(holder)

        self._render_member(active_id)
        self.content.form_layout.addStretch(1)

    def _switch_hub(self, hub_cmd: dict, mid: str) -> None:
        self._hub_active[hub_cmd.get("hub", "")] = mid
        self.render_hub(hub_cmd, mid)
        self.content.after_render()          # 药丸切换后同理：清焦点 + 回顶部

    def _render_member(self, mid: str) -> None:
        """渲染 Hub 成员：虚拟全局卡片 或 真实命令。"""
        if mid == "__g_args":
            self.current_cmd = None
            self._render_global_card(
                "参数设置", "全局通用参数：命名空间、代理、调试、存储后端",
                global_args_fields(),
            )
            self._render_env_card()          # 只读体检，跟在参数设置后面
            return
        # （「下载设置」卡片已删：内容与「下载 → 参数设置」子选项卡重复）
        c = CMD_BY_ID.get(mid)
        if c:
            self.render_cmd(c, keep_head=True)

    def _render_global_card(self, title: str, desc: str, fields: list[dict]) -> None:
        """渲染全局参数字段（值存 global_store，直接生效于所有命令）。

        GroupBox 传空标题：药丸条已高亮显示「参数设置 / 下载设置」，不再重复。
        """
        self.content.set_head_actions(None)   # 设置卡片页无可执行命令，不显示按钮组
        gb = GroupBox("")
        for f in fields:
            key = f.get("key", "")
            val = self.global_store.get(key, f.get("default"))
            fr = FieldRow(f, val)
            fr.changed.connect(
                lambda k=key, r=fr: self._on_global_changed(k, r)
            )
            gb.add(fr)
        self.content.form_layout.addWidget(gb)
        self._refresh_cmd_preview()

    def _on_global_changed(self, key: str, row: FieldRow) -> None:
        self.global_store[key] = row.value()
        if key == "__ns":
            self.titlebar.ns_btn.set_name(str(row.value()))
            self.statusbar.set_val("ns", str(row.value()))
        elif key == "__proxy":
            self._sync_conn_status()             # 代理改了 → 状态栏连接信息同步
        self._refresh_cmd_preview()

    # ---------------------------------------------------------------- 命令页
    def render_cmd(self, cmd: dict, keep_head: bool = False) -> None:
        if not keep_head:
            self.content.set_head(cmd["name"], cmd.get("desc", ""), cmd.get("icon", ""))
        self.content.clear_form()

        # notice
        n = cmd.get("notice")
        if n:
            lab = QLabel(n.get("text", ""))
            lab.setWordWrap(True)
            lab.setStyleSheet(
                f"color:{T.TEXT_DIM}; font-size:12px; background:{T.ACCENT_HOVER};"
                f"border:1px solid {T.ACCENT_LINE};"
                f"border-radius:{T.RADIUS_SM}px; padding:8px 10px;"
            )
            self.content.form_layout.addWidget(lab)

        store = self.store.setdefault(cmd["id"], self._defaults(cmd))
        form = FormRenderer(cmd, store, self.global_store)
        form.values_changed.connect(self._on_form_changed)
        self.content.form_layout.addWidget(form)
        self.content.form_layout.addWidget(self.content.action_bar.cmd_row)  # 等效命令
        self.content.form_layout.addStretch(1)
        # 执行/终止/重置 → 页面右上方（标题区右侧）
        self.content.set_head_actions(self.content.action_bar.btn_row)
        self._cur_form = form
        self.current_cmd = cmd
        self._refresh_cmd_preview()

    def _defaults(self, cmd: dict) -> dict:
        d = {}
        for g in cmd.get("groups", []):
            for f in g.get("fields", []):
                k = f.get("key")
                if k:
                    v = f.get("default")
                    # 下载目录默认进软件目录（resources/dl），启动时动态求值
                    if k == "dir" and (v is None or str(v) == "downloads"):
                        v = paths.dl_root_str()
                    d[k] = v
            sw = g.get("switchable")
            if sw:
                d[sw["key"]] = sw.get("default")
        return d

    def _on_form_changed(self) -> None:
        self._auto_anchor_dl_dir()
        self._refresh_cmd_preview()

    def _auto_anchor_dl_dir(self) -> None:
        """下载目录自动锚定（两条规则，与产物目录约定闭环）：

        · JSON 模式：选了 resources/json/ 内的下载 json →
          resources/dl/<锚定名>/（json 文件名即 <缩写>-<群号>，同群产物同目录）
        · 链接模式：消息链接里解析群组标识 —— 私有群 t.me/c/<群号>/… 直接用
          群号、公开群 t.me/<username>/… 沿用 username（纯英文，跨平台安全）→
          resources/dl/<群号 或 username>/
        源值没变就绝不动目录，避免覆盖用户手动修改的下载目录。
        """
        import os as _os
        c = self.current_cmd
        if not c or c.get("id") != "dl" or self._cur_form is None:
            return
        vals = self._cur_form.values()
        mode = str(vals.get("srcMode", "url"))

        if mode == "url":
            urls = vals.get("url") or []
            if isinstance(urls, str):
                urls = [urls]
            tag = ""
            for u in urls:                       # 取第一条能解析出群组的链接
                tag = paths.tg_chat_tag(str(u))
                if tag:
                    break
            if tag == self._last_anchor_url:
                return                           # 链接没变：不覆盖手动改的目录
            self._last_anchor_url = tag
            if not tag:
                return                           # 解析不出（清空/tg://）：保持现状
            self._anchor_dl_dir_to(str(paths.dl_root() / tag),
                                   f"[锚定] 下载目录已跟随消息链接 → ")
            return

        if mode != "json":
            return
        f = vals.get("file") or ""
        if isinstance(f, (list, tuple)):
            f = next((str(x) for x in f if str(x).strip()), "")
        f = str(f).strip()
        if f == self._last_anchor_file:
            return                     # file 未变：不覆盖用户手动改的目录
        self._last_anchor_file = f
        if not f:
            return
        try:
            p = Path(f)
            if p.parent != paths.json_root():
                return                 # 只锚定软件自己导出的下载 json
        except (OSError, ValueError):
            return
        anchor = p.stem
        if not anchor:
            return
        self._anchor_dl_dir_to(str(paths.dl_root() / anchor),
                               "[锚定] 下载目录已跟随下载 json → ")

    def _anchor_dl_dir_to(self, target: str, log_prefix: str) -> None:
        """把「下载目录」字段写到目标值（与当前一致时不动，不刷日志）。"""
        import os as _os
        if self._cur_form is None:
            return
        vals = self._cur_form.values()
        cur = vals.get("dir") or ""
        if isinstance(cur, (list, tuple)):
            cur = next((str(x) for x in cur if str(x).strip()), "")
        if _os.path.normcase(str(cur).strip()) == _os.path.normcase(str(target).strip()):
            return
        self.store.setdefault("dl", {})["dir"] = [target]
        self._cur_form.set_field_value("dir", target)
        self.content.log.sys(f"{log_prefix}{target}")

    def _cancel_current(self) -> None:
        """终止：清掉导出状态机（放弃整段流程），再杀进程。"""
        self._export_stage = None
        self.runner.cancel()

    def _reset_current(self) -> None:
        c = self.current_cmd
        if not c:
            return
        self.store[c["id"]] = self._defaults(c)
        self.render_cmd(c, keep_head=True)
        self.content.log.sys(f"[重置] {c['name']} 已恢复默认值")

    # ---------------------------------------------------------------- 虚拟页
    def render_tasks_page(self, cmd: dict) -> None:
        """任务进度页：嵌入跨页保留的 TaskBox（实时进度表）。

        TaskBox 以 stretch=1 占满可视高度（卡片列表在内层滚动区伸展），
        不加尾部 stretch —— 否则面板固定 320px，下方留大片空白。
        """
        self.content.set_head(cmd["name"], cmd.get("desc", ""), cmd.get("icon", ""))
        self.content.clear_form()
        self.current_cmd = None
        self.content.form_layout.addWidget(self.content.task, 1)
        # 后台跑任务时卡片跳过渲染；此刻页面可见，补上最后一次进度
        self.content.task.flush_pending()

    def render_msgview_page(self, cmd: dict) -> None:
        """消息浏览页：嵌入跨页保留的 MsgBrowser（选择 JSON / 搜索 / 类型筛选 / 消息卡）。

        与任务进度页同策略：以 stretch=1 占满可视高度，不留尾部空白。
        """
        self.content.set_head(cmd["name"], cmd.get("desc", ""), cmd.get("icon", ""))
        self.content.clear_form()
        self.current_cmd = None
        self.content.form_layout.addWidget(self.content.msg, 1)
        # 内容可能被「切后台」卸载过（且当时不在这一页）→ 回到这页时补上重新加载
        QTimer.singleShot(0, self._leave_background)

    # ================================================================ 执行
    def _current_argv(self) -> list[str]:
        c = self.current_cmd
        if not c:
            return []
        # 从当前表单取值；若表单不在（如全局卡片页），用 store 里的值
        vals = dict(self.store.get(c["id"], self._defaults(c)))
        if self._cur_form is not None and self._cur_form.cmd is c:
            vals.update(self._cur_form.values())
        return build_argv(c, vals, self.global_store)

    def _refresh_cmd_preview(self) -> None:
        argv = self._current_argv()
        self.content.action_bar.set_cmd(argv, self.tdl_path)

    def run_current(self) -> None:
        c = self.current_cmd
        if not c:
            self.content.log.sys("[提示] 当前页面没有可执行的命令。")
            return
        if self.runner.running:
            self.content.log.sys("[提示] 已有命令在运行，请先终止。")
            return

        vals = dict(self.store.get(c["id"], self._defaults(c)))
        if self._cur_form is not None and self._cur_form.cmd is c:
            vals.update(self._cur_form.values())

        argv = build_argv(c, vals, self.global_store)
        if not argv:
            self.content.log.err("[错误] 命令为空，无法执行。")
            return

        # 虚拟命令不可执行
        if c.get("virtual"):
            self.content.log.sys("[提示] 这是展示页，无对应 tdl 命令。")
            return

        self.content.log.sys("─" * 60)
        self.content.log.sys(f"$ tdl {' '.join(argv)}")

        # ---- 导出 JSON：三段状态机 ----
        # ① chat ls -o json 查群名（静默）→ ② 导出下载 json → ③ 导出消息 json
        if c.get("id") == "dl" and str(vals.get("srcMode", "url")) == "export":
            chat_key = str(vals.get("ex_chat", "") or "").strip()
            if not chat_key:
                self.content.log.err("[错误] 请先填写「会话 ID / 群号」。")
                return
            # 导出参数校验：tdl 对「类型/区间不匹配」会静默导出空文件（退出码 0）：
            #   last 需 1 个正整数（条数）；id/time 需 2 个值（缺省时 tdl 自动补 MaxInt → 空结果）
            ex_type = vals.get("ex_type") or "last"
            if isinstance(ex_type, dict):
                ex_type = ex_type.get("v", "last")
            raw_in = vals.get("ex_input") or []
            if isinstance(raw_in, str):
                raw_in = [x.strip() for x in raw_in.split(",") if x.strip()]
            nums = []
            for x in raw_in:
                try:
                    nums.append(int(str(x).strip()))
                except (TypeError, ValueError):
                    pass
            if ex_type == "last" and (len(nums) != 1 or nums[0] <= 0):
                self.content.log.err("[错误] 导出类型 last 需要 1 个正整数条数（如 100），"
                                     "多个值或 0 会导致导出为空。")
                self.content.set_log_state("参数错误")
                return
            if ex_type in ("id", "time") and len(nums) != 2:
                self.content.log.err(f"[错误] 导出类型 {ex_type} 需要 2 个值（逗号分隔，如 100,200）；"
                                     "只填 1 个时 tdl 会自动补 MaxInt 导致导出为空。")
                self.content.set_log_state("参数错误")
                return
            paths.ensure_dirs()
            self._export_vals = dict(vals)
            self._export_stage = "ls"
            self._ls_lines = []
            self._run_kind = "export"
            self.task_box.begin(kind="export")
            self._set_task_status("导出中")
            ls_argv = build_argv(CMD_BY_ID["chat-ls"],
                                 {"output": "json", "filter": ""},
                                 self.global_store)
            self.content.log.sys("[导出] 正在查询群组名称…")
            self.content.action_bar.set_running(True)
            self.content.log.set_status("查询群组…", T.ACCENT)
            self.content.set_log_visible(True)
            self.content.set_log_state("查询群组…")
            if not self._start(ls_argv):
                self._export_stage = None
                self.content.action_bar.set_running(False)
                self.content.log.set_status("启动失败", T.DANGER)
                self.content.set_log_state("启动失败")
            return

        self._run_kind = str(c.get("id", ""))
        self.task_box.begin(kind=self._run_kind)
        self.statusbar.set_val("dl", "0 B")             # 新任务：下载量/网速归零
        self.statusbar.set_val("speed", "0 B/s")
        self._set_task_status(self._KIND_CN.get(self._run_kind, "进行中"))
        self.content.action_bar.set_running(True)
        self.content.log.set_status("运行中…", T.ACCENT)
        self.content.set_log_visible(True)          # 执行时自动展开日志
        self.content.set_log_state("运行中…")

        if not self._start(argv):
            self.content.action_bar.set_running(False)
            self.content.log.set_status("启动失败", T.DANGER)
            self.content.set_log_state("启动失败")

    # ================================================================ 统计页
    def render_stats_page(self, cmd: dict) -> None:
        """下载统计：每日一行（日期 / 下载量 / 下载时长 / 平均速度）。"""
        self.content.set_head(cmd["name"], cmd.get("desc", ""), cmd.get("icon", ""))
        self.content.clear_form()
        self.current_cmd = None

        gb = GroupBox("")                    # 页面标题已是「下载统计」，不再重复小标题
        rows = stats.days()

        # 白底卡片容器（与「任务进度」页的任务卡风格一致）
        card = QFrame()
        card.setObjectName("StatCard")
        card.setStyleSheet(
            "QFrame#StatCard { background:#ffffff; border:1px solid #d0d7de;"
            "border-radius:8px; }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(0)

        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["日期", "下载量", "下载时长", "平均速度", "任务数"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setStyleSheet(
            "QTableWidget { background:#ffffff; color:#1f2328;"
            " border:none; gridline-color:#eaeef2; font-size:12px; }"
            "QTableWidget::item { padding:4px 8px; }"
            "QHeaderView::section { background:#f6f8fa; color:#57606a;"
            " border:none; border-right:1px solid #eaeef2;"
            " border-bottom:1px solid #d0d7de;"
            " padding:5px 8px; font-size:12px; }"
        )
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 5):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setFixedHeight(28)
        table.setMinimumHeight(min(120 + len(rows) * 28, 380))
        for i, d in enumerate(rows):
            vals = [d["date"], fmt_bytes(d["bytes"]), fmt_duration(d["seconds"]),
                    (fmt_bytes(d["avg"]) + "/s") if d["avg"] > 0 else "--",
                    str(d["tasks"])]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setForeground(QColor("#1f2328" if col == 0 else "#1a7f37"))
                table.setItem(i, col, it)
        if not rows:
            table.insertRow(0)
            empty = QTableWidgetItem("暂无数据 —— 执行一次下载后这里会出现当天统计")
            empty.setForeground(QColor("#57606a"))
            table.setItem(0, 0, empty)
            table.setSpan(0, 0, 1, 5)
        cl.addWidget(table)
        gb.add(card)
        self.content.form_layout.addWidget(gb)

        note = QLabel(
            "统计口径：仅统计成功执行的下载任务（导出 JSON / 上传 / 转发不计入）。\n"
            "任务数 = 已下载的任务（文件）数；"
            "下载量 = 各文件最新已完成字节之和；下载时长 = 命令墙钟耗时；"
            "平均速度 = 下载量 ÷ 下载时长。\n"
            "数据保存在软件目录 resources/stats.json，每个自然日一个统计周期。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T.TEXT_DIM}; font-size:12px; background:transparent;")
        self.content.form_layout.addWidget(note)
        self.content.form_layout.addStretch(1)

    # ================================================================ 导出状态机
    def _resolve_chat_from_ls(self) -> dict | None:
        """从 chat ls -o json 的输出里解析出目标会话。"""
        import json as _json
        try:
            text = "\n".join(self._ls_lines)
            data = _json.loads(text[text.index("["): text.rindex("]") + 1])
        except (ValueError, AttributeError):
            return None
        return paths.find_chat(data, self._export_vals.get("ex_chat", ""))

    def _build_export_argv(self, chat: dict, export_all: bool) -> list[str]:
        """按目录结构约定构造 chat export 命令（-o 自动落位）。

        ⚠️ 消息 json 让 tdl 先写**临时文件**（`…-message.part.json`）：
        同一个群常常分多次导出不同区间，直接写正式文件会把上一次的内容
        覆盖丢；导出中途被打断还会把正式文件截断。改成临时文件后，
        成功后由 `_merge_msg_json()` 按消息 id 合并进正式文件（原子替换）。
        """
        argv = build_argv(CMD_BY_ID["dl"], dict(self._export_vals, ex_all=export_all),
                          self.global_store, export_all=export_all)
        name = chat.get("visible_name") or "chat"
        cid = chat.get("id")
        if export_all:
            out = paths.msg_json_part_path(name, cid)
        else:
            out = paths.dl_json_path(name, cid)
        out.parent.mkdir(parents=True, exist_ok=True)       # 群组文件夹自动创建
        return argv + ["-o", str(out)]

    def _advance_export(self, rec) -> None:
        """导出 JSON 的阶段推进。失败即整段终止。"""
        stage = self._export_stage

        if rec is None or rec.exit_code != 0:
            if stage == "msg_json":
                # 消息导出中断 → 删掉半截的临时文件（正式文件毫发无损）
                self._drop_msg_part()
            self._export_stage = None
            self.content.action_bar.set_running(False)
            self.task_box.finish(False)
            self.task_box.set_state("已终止" if rec is None else "失败")
            summary = rec.summary() if rec else "已终止"
            self.content.log.err(f"[导出失败] {summary}")
            self.content.log.set_status("导出失败", T.DANGER)
            self.content.set_log_state("导出失败")
            self.statusbar.set_val("task", "空闲")
            return

        if stage == "ls":
            chat = self._resolve_chat_from_ls()
            if not chat:
                self._export_stage = None
                self.content.action_bar.set_running(False)
                key = self._export_vals.get("ex_chat", "")
                self.content.log.err(f"[导出失败] 会话「{key}」不在会话列表中，"
                                     f"请确认群号或用户名。")
                self.content.log.set_status("群组未找到", T.DANGER)
                self.content.set_log_state("群组未找到")
                return
            name = chat.get("visible_name") or "chat"
            cid = chat.get("id")
            anchor = paths.anchor_dir_name(name, cid)
            self._export_chat = chat
            self._export_stage = "dl_json"
            argv1 = self._build_export_argv(chat, export_all=False)
            self.task_box.begin(reset=False, kind="export")   # 连续导出：保留 ls 阶段（无行），不清表
            self.content.log.sys("─" * 60)
            self.content.log.sys(f"[导出] 群组「{name}」→ 锚定名 {anchor}")
            self.content.log.sys(f"$ tdl {' '.join(argv1)}")
            self.content.log.set_status("导出下载 json…", T.ACCENT)
            self.content.set_log_state("下载 json 1/2")
            if not self._start(argv1):
                self._export_stage = None
                self.content.action_bar.set_running(False)
                self.content.log.set_status("启动失败", T.DANGER)
            return

        if stage == "dl_json":
            if self._export_vals.get("ex_all"):
                chat = self._export_chat
                self._export_stage = "msg_json"
                # 先清掉可能残留的临时文件，保证稍后合并进来的只有本次结果
                self._drop_msg_part()
                argv2 = self._build_export_argv(chat, export_all=True)
                self.task_box.begin(reset=False, kind="export")   # 保留下载 json 的任务行
                self.content.log.sys("─" * 60)
                self.content.log.sys("[导出] 继续导出全部消息记录"
                                     "（分多次导出会合并进同一份消息 json）")
                self.content.log.sys(f"$ tdl {' '.join(argv2)}")
                self.content.log.set_status("导出消息 json…", T.ACCENT)
                self.content.set_log_state("消息 json 2/2")
                if not self._start(argv2):
                    self._export_stage = None
                    self.content.action_bar.set_running(False)
                    self.content.log.set_status("启动失败", T.DANGER)
                return
            self._export_stage = None
            self._export_done_ok()
            return

        # msg_json 完成 → 先合并进正式文件（多次导出累积），再报结果
        self._export_stage = None
        self._merge_msg_json()
        self._export_done_ok()

    def _msg_part_path(self):
        """本次导出消息 json 的临时文件路径（还没跑过导出时为 None）。"""
        chat = self._export_chat or {}
        if not chat:
            return None
        return paths.msg_json_part_path(chat.get("visible_name") or "chat",
                                        chat.get("id"))

    def _drop_msg_part(self) -> None:
        """删除消息 json 的临时文件（导出前清残留 / 导出失败后清理）。"""
        part = self._msg_part_path()
        if part is None:
            return
        try:
            part.unlink()
        except OSError:
            pass

    def _merge_msg_json(self) -> None:
        """把本次导出的消息 json 合并进既有文件。

        规则（见 `paths.merge_export_json`）：按消息 id 去重，同 id 以本次为准，
        结果按 id 升序。目录里始终只有一份消息 json ——
        分多次导出不同区间自动累积；重复导出同一区间以最新为准，不重复堆叠。
        """
        chat = self._export_chat or {}
        name = chat.get("visible_name") or "chat"
        cid = chat.get("id")
        part = paths.msg_json_part_path(name, cid)
        target = paths.msg_json_path(name, cid)
        if not part.exists():
            return
        existed = target.exists()
        try:
            total, added = paths.merge_export_json(target, part)
        except (ValueError, OSError) as e:
            # 合并失败绝不能连累正式文件：本次结果留在 .part.json 里，日志说明
            self.content.log.err(
                f"[警告] 消息 json 合并失败（{e}）—— 本次结果暂存在 "
                f"{part.name}，既有文件 {target.name} 未被改动")
            return
        try:
            part.unlink()
        except OSError:
            pass
        if existed:
            self.content.log.sys(
                f"[导出] 已与既有消息 json 合并：共 {total} 条，本次新增 {added} 条")

    def _export_done_ok(self) -> None:
        self.content.action_bar.set_running(False)
        agg = self.task_box.finish(True)
        self.task_box.set_state(f"完成 · {agg.get('count', 0)} 项")
        st = self._report_export_files()
        dl, msg = st.get("dl"), st.get("msg")
        if dl:
            self.content.log.ok("[完成] 导出 JSON 全部完成")
            self.content.log.set_status("导出完成", T.OK)
        elif msg:
            # 区间内有消息但无媒体：属正常，不报错
            self.content.log.ok(
                f"[完成] 消息记录已导出 {msg} 条（该区间没有媒体，下载 json 为空属正常）")
            self.content.log.set_status("导出完成（无媒体）", T.OK)
        else:
            self.content.log.err("[未完成] 两份产物都为空 —— 请调整「导出类型 / 区间参数」后重试")
            self.content.log.set_status("导出为空", T.WARN)
        self.content.set_log_state("导出完成")
        self.statusbar.set_val("task", "空闲")

    def _report_export_files(self) -> dict:
        """统计两份产物条数并给出结论。返回 {"dl": n|None, "msg": n|None}。

        关键区分：**区间内有消息但没有媒体**（下载 json 为空属正常）
        vs **区间内没有任何消息**（参数填错，需提示修正）。
        """
        import json as _json
        chat = self._export_chat or {}
        name = chat.get("visible_name") or "chat"
        cid = chat.get("id")
        out: dict = {"dl": None, "msg": None}
        for key, tag, p in (("dl", "下载 json", paths.dl_json_path(name, cid)),
                            ("msg", "消息 json", paths.msg_json_path(name, cid))):
            if not p.exists():
                continue
            good, why = paths.inspect_export_json(p)
            if not good:
                # 截断的产物不能用于下载（download -f 会中断 → 表现为 0 字节）
                self.content.log.err(f"[警告] {tag} 不可用：{why}")
                self.content.log.err(f"        文件：{p}")
                out[key] = "bad"
                continue
            try:
                out[key] = len(_json.loads(p.read_text(encoding="utf-8")).get("messages", []))
            except Exception:                                    # noqa: BLE001
                self.content.log.err(f"[警告] {tag} 无法解析：{p}")
                out[key] = "bad"
                continue
            if out[key]:
                self.content.log.ok(f"[导出] {tag} 共 {out[key]} 条 → {p}")
            else:
                self.content.log.sys(f"[导出] {tag} 为 0 条 → {p}")

        dl, msg = out["dl"], out["msg"]
        if "bad" in (dl, msg):
            self.content.log.err(
                "[注意] 有产物不完整，本次结果可能不能用 —— 建议重新导出"
                "（导出完再关窗/终止，别中途打断）。")
        if dl == 0 and msg:
            self.content.log.sys(
                f"       · 该区间有 {msg} 条消息，但其中没有可下载的媒体 ——"
                "下载 json 为空属正常；消息记录可到「消息浏览」页浏览")
        elif dl == 0:
            self.content.log.err(
                "[警告] 下载 json 为空，且消息 json 也为空 —— 区间内没有匹配的消息。"
                "请检查「导出类型 / 区间参数」：last=条数（如 100）；"
                "id=两个真实消息编号（如 140149,140937，可在「消息浏览」页点 #编号 复制）；"
                "time=两个 Unix 秒级时间戳")
        return out

    def _on_started(self, argv: list) -> None:
        self._out_errors = 0                     # 新命令：异常计数归零
        self._tdl_notes_seen.clear()             # 新命令：tdl 提示可以再提示一次
        self._load_id_pool(argv)                 # 有下载源 JSON 时登记完整消息号

    def _load_id_pool(self, argv: list) -> None:
        """下载命令带 -f/--file（下载源 JSON）时，读出里面的完整消息号与文件名。

        两件事都只能从下载源里拿（tdl 的进度行都不给）：
          · 完整序号 —— message 被截断到 30 字符，序号常只剩前几位
          · 文件名 —— 进度行里没有文件名/扩展名，看不出下的是哪个文件
        交给面板做前缀还原与展示（详见 TaskPanel._pick_full_id / _file_of）。

        ⚠️ 池必须**按 tdl 的实际下载顺序**排列（paths.sort_id_pool）：tdl 的
        iter.go:sortDialogs 会强制按消息号数值排序（无 --desc 升序），而官方导出
        的 JSON 通常降序 —— 直接沿用 JSON 顺序会让「第 N 个任务 = 池里第 N 个号」
        的首尾颠倒（实测卡片显示 95396~95399、实际下载 95314~95318）。
        """
        ids: list[str] = []
        dl_dir = ""
        desc = False
        restart = False
        for i, a in enumerate(argv):
            if a in ("-f", "--file") and i + 1 < len(argv):
                # cobra 的 slice 参数支持逗号分隔，也可能多次出现
                ids.extend(x.strip() for x in str(argv[i + 1]).split(",") if x.strip())
            if a in ("-d", "--dir") and i + 1 < len(argv) and not dl_dir:
                dl_dir = str(argv[i + 1]).strip()   # 下载目录：完成行身份 oracle 用
            if a == "--desc":
                desc = True                         # tdl 反向排序：池也要跟着倒
            if a == "--restart":
                restart = True                      # 重下全部：不排除磁盘上已存在的号
        pool: list[str] = []
        files: dict = {}
        for p in ids:
            m = paths.export_id_files(p)
            pool.extend(m.keys())
            files.update(m)
        self.task_box.set_download_dir(dl_dir)
        self.task_box.set_id_pool(paths.sort_id_pool(pool, desc), files, restart)

    def _on_out(self, line: str) -> None:
        # ⚠️ 这里必须兜住异常：本方法是 Qt 信号槽，抛出的异常不会中断程序，
        # 只会被 Qt 吞掉并**静默终止**这条输出流 —— 表现就是「任务进度、网速、
        # 下载量全部停更，界面上看不出任何错误」。曾因 TaskBox 缺接口踩过一次。
        try:
            self._handle_out(line)
        except Exception as e:                       # noqa: BLE001
            self._out_errors += 1
            if self._out_errors <= 3:                # 只报前几次，避免刷屏
                self.content.log.err(
                    f"[进度解析异常] {type(e).__name__}: {e}")

    def _handle_out(self, line: str) -> None:
        if self._export_stage == "ls":
            self._ls_lines.append(line)     # 查询群名阶段静默（避免 JSON 刷屏）
            return
        # 轮次分隔行（CPU/Memory/Goroutines 状态行）= 上一轮渲染的开头。
        # 先把上一轮缓存的进度行整批交给任务面板认领身份，避免逐行去重时错乱
        # （tdl 每轮都会重画全部活跃任务，逐行处理会把同一任务当成多个新任务）。
        if is_round_marker(line):
            # 本轮结束：先让面板认领身份、刷新卡片，再同步状态栏统计
            # （字节/速率由 commit 时才写入，所以状态栏必须在这之后取值）
            self.task_box.commit_round()
            self.statusbar.set_val("dl", self.task_box.total_bytes_text())
            self.statusbar.set_val("speed", self.task_box.total_rate_text())
            return
        if is_noise_line(line):
            return                          # 总进度条行 / 其它状态行：丢弃
        note = _tdl_note(line)
        if note:                            # tdl 的固定提示 → 中文说明，且只提示一次
            if note not in self._tdl_notes_seen:
                self._tdl_notes_seen.add(note)
                self.content.log.sys(note)
            return
        info = parse_progress_line(line)
        if info is not None:                # 进度行 → 先缓存，本轮结束时统一提交
            self.task_box.push_row(info)
            self.statusbar.set_val("dl", self.task_box.total_bytes_text())
            self.statusbar.set_val("speed", self.task_box.total_rate_text())
            g = str(info.get("gname") or "").strip()
            if g:                           # 状态栏显示「下载中 · 群组名」
                self._set_task_status(
                    self._KIND_CN.get(self._run_kind, "进行中"), g)
            return
        self.content.log.out(strip_ansi(line))   # 剥掉重绘控制符再进日志

    def _on_err(self, line: str) -> None:
        self.content.log.err(strip_ansi(line))

    def _on_finished(self, rec) -> None:
        if self._export_stage:
            self._advance_export(rec)
            return
        self.content.action_bar.set_running(False)
        ok = bool(rec and rec.ok)
        agg = self.task_box.finish(ok)
        if rec is None:
            self.task_box.set_state("已终止")
            self.content.log.set_status("已结束")
            self.content.set_log_state("已结束")
            self.statusbar.set_val("task", "空闲")
            return
        # 下载统计：只要本次确实下到了数据就记账（哪怕命令整体返回失败，
        # 部分成功的文件也应计入），任务数按已下载的文件数计。
        if self._run_kind == "dl" and agg.get("bytes"):
            stats.add_download(int(agg["bytes"]), float(rec.duration),
                               int(agg.get("count", 1)))
        # 失败/中断时也保留实际下载量，不再强制归零（旧逻辑会把已下载的量抹掉）
        self.statusbar.set_val("dl", fmt_bytes_dec(agg.get("bytes", 0)))
        self.statusbar.set_val("speed", "0 B/s")
        self.statusbar.set_val("task", "空闲")
        if rec.ok:
            self.content.log.ok(f"[完成] {rec.summary()}")
            self.content.log.set_status(rec.summary(), T.OK)
            self.content.set_log_state(f"完成 · {rec.duration:.1f}s")
        else:
            self.content.log.err(f"[结束] {rec.summary()}")
            self.content.log.set_status(rec.summary(), T.DANGER)
            self.content.set_log_state(f"退出码 {rec.exit_code}")
        self.statusbar.set_val("task", "空闲")
