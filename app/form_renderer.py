# -*- coding: utf-8 -*-
"""
表单渲染器 —— 把 data.py 的字段定义渲染成 Qt 控件。

核心能力：
    · 7 种字段类型 → 对应控件（text/number/switch/select/path/multi/password/list）
    · show_when 条件联动（依赖字段变化时自动显隐）
    · switchable 分段控件（如下载来源三选一）
    · 值收集（to_values）供 build_argv 使用

渲染顺序（用户明确要求的下载页规则）：
    ① 常驻组（无 pane 标记，如「下载来源」）—— 任何子选项卡下都可见
    ② 子选项卡条
    ③ pane 内容（如「保存设置」/「参数设置」）
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QSpinBox, QComboBox, QPlainTextEdit, QPushButton, QCheckBox,
    QFileDialog, QSizePolicy, QFrame, QScrollArea,
)

from .theme import T
from .icons import ICONS
from .data import (
    FT_TEXT, FT_NUMBER, FT_SWITCH, FT_SELECT, FT_PATH,
    FT_MULTI, FT_PASSWORD, FT_LIST, FT_NOTE,
)
from .widgets import GroupBox, hand_cursor
from . import paths


def default_pick_dir(field: dict) -> str:
    """字段「浏览」对话框的默认起始目录（用 pick_from 声明）。

    目录约定见 paths.py：
        "json" → resources/json —— 下载 json（供 download -f 使用）
        "dl"   → resources/dl   —— 消息 json（消息浏览使用，按群组分子目录）
    声明为空则返回空串，交给系统默认位置。
    """
    src = field.get("pick_from")
    if src == "json":
        return str(paths.json_root()).replace("\\", "/")
    if src == "dl":
        return paths.dl_root_str()
    return ""


# ============================================================================
# 通用小控件
# ============================================================================
class NoWheelComboBox(QComboBox):
    """下拉框：滚轮不改变选中项。

    Qt 默认在滚轮经过（未展开的）下拉框时切换选项，极易误改设置；
    这里把滚轮事件向下传递 → 只滚动页面，设置值不变。
    """

    def wheelEvent(self, e):
        e.ignore()


class NoWheelSpinBox(QSpinBox):
    """数字框：滚轮不改变数值（同上，只滚页面）。"""

    def wheelEvent(self, e):
        e.ignore()


class Segmented(QWidget):
    """
    分段控件（对应原型的 .seg / switchable）。

    用于「下载来源」这类互斥多选一：整体一个灰色圆角底，
    选中的段落白底凸起。
    """

    changed = Signal(str)

    def __init__(self, options: list[dict], value: str = "", parent=None):
        super().__init__(parent)
        self.setFixedHeight(T.H_CONTROL)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        hand_cursor(self)

        self._opts = options
        self._val = value or (options[0]["v"] if options else "")
        self._hover = -1
        self._rects: list[tuple[int, int, int]] = []   # (x, w, index)

    def value(self) -> str:
        return self._val

    def set_value(self, v: str) -> None:
        if any(o["v"] == v for o in self._opts):
            self._val = v
            self.update()

    def _calc(self) -> None:
        """算每段的宽度（按文字宽度 + padding）。"""
        f = self._font()
        fm = QFontMetrics(f)
        self._rects = []
        x = 3
        for i, o in enumerate(self._opts):
            w = fm.horizontalAdvance(o["l"]) + 24
            self._rects.append((x, w, i))
            x += w + 2
        total = x + 1
        self.setFixedWidth(max(total, 120))

    def _font(self) -> QFont:
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(12.5)
        f.setWeight(QFont.DemiBold)
        return f

    def sizeHint(self) -> QSize:
        self._calc()
        return QSize(self.width(), T.H_CONTROL)

    def mouseReleaseEvent(self, e):
        pos = e.position().toPoint()
        for x, w, i in self._rects:
            if x <= pos.x() <= x + w:
                v = self._opts[i]["v"]
                if v != self._val:
                    self._val = v
                    self.update()
                    self.changed.emit(v)
                break

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        h = -1
        for x, w, i in self._rects:
            if x <= pos.x() <= x + w:
                h = i
                break
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()

    def paintEvent(self, _e):
        if not self._rects:
            self._calc()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.SEG_BG))
        p.drawRoundedRect(r, T.RADIUS_SM, T.RADIUS_SM)

        f = self._font()
        p.setFont(f)
        fm = QFontMetrics(f)

        for x, w, i in self._rects:
            o = self._opts[i]
            on = o["v"] == self._val
            box = r.adjusted(x, 3, 0, -3)
            box.setWidth(w)

            if on:
                p.setPen(QPen(QColor(T.ACCENT_LINE), 1))
                p.setBrush(QColor(T.PANEL))
                p.drawRoundedRect(box, T.RADIUS_SM - 2, T.RADIUS_SM - 2)
                p.setPen(QColor(T.ACCENT_INK))
            elif i == self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#e2e7ed"))
                p.drawRoundedRect(box, T.RADIUS_SM - 2, T.RADIUS_SM - 2)
                p.setPen(QColor(T.TEXT))
            else:
                p.setPen(QColor(T.TEXT_DIM))

            p.drawText(box, int(Qt.AlignCenter), o["l"])
        p.end()


class PathEdit(QWidget):
    """路径输入框 + 浏览按钮（目录或文件）。"""

    def __init__(self, pick_dir: bool = True, placeholder: str = "",
                 default_dir: str = "", parent=None):
        super().__init__(parent)
        self.setFixedHeight(T.H_CONTROL)
        self._pick_dir = pick_dir
        self._default_dir = default_dir      # 输入框无有效路径时的浏览起点
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFixedHeight(T.H_CONTROL)
        self.edit.setMinimumWidth(0)      # 允许收缩：窄窗口下输入框仍在可见范围内
        lay.addWidget(self.edit, 1)

        btn = QPushButton("浏览")
        btn.setObjectName("Btn")
        btn.setFixedHeight(T.H_CONTROL)
        # 全局按钮 QSS 左右 padding 各 18px：58px 宽只剩 22px，放不下两个 13px 汉字（被裁）
        btn.setFixedWidth(68)
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

    def pick_start(self) -> str:
        """浏览对话框的起始位置。

        优先级：输入框里已有的**有效绝对目录** → 字段声明的默认目录 → 空（系统默认）。
        ⚠️ 不能直接拿输入框的原始文本当起点：它可能只是个文件名（如默认值
        `tdl-export.json`），那样对话框会落在一个不存在的位置。
        """
        cur = self.edit.text().strip()
        if cur:
            p = Path(cur)
            # 选文件时 cur 可能是完整文件路径 → 从它所在目录开始；目录选择则按目录理解
            cand = p.parent if (not self._pick_dir and p.suffix) else p
            if cand.is_absolute() and cand.is_dir():
                return str(cand)
        if self._default_dir:
            d = Path(self._default_dir)
            if d.is_dir():
                return str(d).replace("\\", "/")
        return ""

    def _browse(self) -> None:
        start = self.pick_start()
        if self._pick_dir:
            d = QFileDialog.getExistingDirectory(self, "选择目录", start)
            if d:
                self.edit.setText(d.replace("\\", "/"))
        else:
            f, _ = QFileDialog.getOpenFileName(self, "选择文件", start)
            if f:
                self.edit.setText(f.replace("\\", "/"))

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, s: str) -> None:
        self.edit.setText(s)


class Switch(QCheckBox):
    """开关（带文字标签）。"""

    def __init__(self, text: str = "", checked: bool = False, parent=None):
        super().__init__(text, parent)
        self.setChecked(checked)
        self.setFixedHeight(T.H_CHIP)
        hand_cursor(self)


class NoteBlock(QLabel):
    """纯说明文字块（无输入）。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setStyleSheet(
            f"color:{T.TEXT_DIM}; font-size:12px; background:{T.ACCENT_HOVER};"
            f"border:1px solid {T.ACCENT_LINE}; border-radius:{T.RADIUS_SM}px;"
            f"padding:8px 10px;"
        )


# ============================================================================
# 字段工厂
# ============================================================================
class FieldRow(QWidget):
    """
    单个字段的完整行（对照原型 .field 上下式结构）。

    布局（与 HTML 原型一致）：
        标签  -u, --url                ← 13px 加粗 + 蓝色 mono flag，同一行
        [ 控件（全宽） ]
        帮助文字（灰色小字，无缩进）
    """

    changed = Signal()
    # 开关被切换：(字段 key, 新值)。用于互斥联动（如 --continue / --restart）
    switch_toggled = Signal(str, bool)

    def __init__(self, field: dict, value, parent=None):
        super().__init__(parent)
        self.field = field
        self._widgets: list[QWidget] = []
        self._editor = None          # 取值用的主控件
        self._multi_editors: list = []  # 多值控件的编辑器中转

        key = field.get("key", "")
        ftype = field.get("type", FT_TEXT)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # ---- note 类型：整块说明，无标签行 ----
        if ftype == FT_NOTE:
            lay.addWidget(NoteBlock(field.get("help", "")))
            return

        # ---- 标签行：label + flag 同行（原型 .f-head）----
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        lab = QLabel(field.get("label", key))
        lab.setObjectName("FieldLabel")
        top.addWidget(lab)

        if field.get("req"):
            star = QLabel("*")
            star.setStyleSheet("color:#d64545; font-weight:700;")
            top.addWidget(star)

        flag = field.get("flag", "")
        if flag:
            fl = QLabel(flag)
            fl.setObjectName("FieldFlag")
            top.addWidget(fl)

        top.addStretch(1)
        lay.addLayout(top)

        # ---- 控件（全宽）----
        editor = self._make_editor(field, value)
        if editor is not None:
            self._editor = editor
            lay.addWidget(editor)
        else:
            lay.addStretch(1)

        # ---- 帮助文字（控件下方，无缩进）----
        help_text = field.get("help", "")
        if help_text:
            h = QLabel(help_text)
            h.setObjectName("FieldHelp")
            h.setWordWrap(True)
            lay.addWidget(h)

    # ---------------------------------------------------------------- 造控件
    def _make_editor(self, field: dict, value):
        ftype = field.get("type", FT_TEXT)
        ph = field.get("ph", "")
        locked = field.get("locked", False)

        if ftype == FT_SWITCH:
            w = Switch("", bool(value))
            w.toggled.connect(lambda _: self.changed.emit())
            key = field.get("key", "")
            w.toggled.connect(
                lambda v, k=key: self.switch_toggled.emit(k, bool(v))
            )
            return w

        if ftype == FT_SELECT:
            w = NoWheelComboBox()
            w.setFixedHeight(T.H_CONTROL)
            # 长选项文案（如导出类型说明）不撑大控件：最小宽度按字符数计，
            # 窗口变窄时控件跟随收缩，始终落在可见范围内
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(10)
            w.setMinimumWidth(0)
            w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            for o in field.get("options", []):
                w.addItem(o["l"], o["v"])
            v = value if value is not None else field.get("default")
            i = w.findData(v)
            if i >= 0:
                w.setCurrentIndex(i)
            w.currentIndexChanged.connect(lambda _: self.changed.emit())
            return w

        if ftype in (FT_MULTI,):
            w = QPlainTextEdit()
            w.setPlaceholderText(ph or "每行一个")
            w.setFixedHeight(72)
            w.setMinimumWidth(0)
            w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if isinstance(value, (list, tuple)):
                w.setPlainText("\n".join(str(x) for x in value))
            elif value:
                w.setPlainText(str(value))
            w.textChanged.connect(lambda: self.changed.emit())
            return w

        if ftype == FT_LIST:
            w = QLineEdit()
            w.setFixedHeight(T.H_CONTROL)
            w.setMinimumWidth(0)
            w.setPlaceholderText(ph or "逗号分隔，如 mp4,jpg")
            if isinstance(value, (list, tuple)):
                w.setText(",".join(str(x) for x in value))
            elif value:
                w.setText(str(value))
            w.textChanged.connect(lambda: self.changed.emit())
            return w

        if ftype == FT_NUMBER:
            w = NoWheelSpinBox()
            w.setFixedHeight(T.H_CONTROL)
            w.setMinimumWidth(0)
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            w.setRange(0, 999999999)
            try:
                w.setValue(int(value if value is not None else field.get("default", 0)))
            except (TypeError, ValueError):
                w.setValue(0)
            w.valueChanged.connect(lambda _: self.changed.emit())
            return w

        if ftype == FT_PATH:
            # pick="file" 显式选文件；否则默认选目录（key 以 output 结尾的选文件）
            pick_dir = (field.get("pick") != "file"
                        and not str(field.get("key", "")).endswith("output"))
            w = PathEdit(pick_dir=pick_dir, placeholder=ph,
                         default_dir=default_pick_dir(field))
            w.setMinimumWidth(0)
            if value:
                w.set_text(str(value))
            w.edit.textChanged.connect(lambda: self.changed.emit())
            if locked:
                w.edit.setReadOnly(True)
            return w

        # text / password 及兜底
        w = QLineEdit()
        w.setFixedHeight(T.H_CONTROL)
        w.setMinimumWidth(0)
        w.setPlaceholderText(ph)
        if value:
            w.setText(str(value))
        if ftype == FT_PASSWORD:
            w.setEchoMode(QLineEdit.Password)
        if locked:
            w.setReadOnly(True)
        w.textChanged.connect(lambda: self.changed.emit())
        return w

    # ---------------------------------------------------------------- 取值
    def value(self):
        field = self.field
        ftype = field.get("type", FT_TEXT)
        w = self._editor
        if w is None:
            return field.get("default")

        if ftype == FT_SWITCH:
            return w.isChecked()
        if ftype == FT_SELECT:
            return w.currentData()
        if ftype == FT_MULTI:
            return [ln.strip() for ln in w.toPlainText().splitlines() if ln.strip()]
        if ftype == FT_LIST:
            return [x.strip() for x in w.text().split(",") if x.strip()]
        if ftype == FT_NUMBER:
            return w.value()
        if ftype == FT_PATH:
            return w.text().strip()
        return w.text().strip()

    def set_enabled_soft(self, on: bool) -> None:
        """软禁用：置灰但不从布局里移除（保留空间感）。"""
        self.setEnabled(on)

    def set_value_text(self, text: str) -> bool:
        """编程式设置文本值（仅 path/text 类行）。值未变化时返回 False，不触发信号。"""
        w = self._editor
        if w is None:
            return False
        cur = ""
        if hasattr(w, "text") and callable(w.text):
            cur = w.text()
        elif hasattr(w, "edit"):
            cur = w.edit.text()
        if cur == text:
            return False
        if hasattr(w, "set_text") and callable(w.set_text):
            w.set_text(text)          # PathEdit
        elif hasattr(w, "setText"):
            w.setText(text)           # QLineEdit
        else:
            return False
        return True

    def set_switch(self, on: bool) -> bool:
        """编程式设置开关值（仅 FT_SWITCH 行）。值未变化时返回 False。

        用于互斥联动：打开 A 时自动关掉它的伙伴 B。setChecked 会触发
        toggled → changed，store 随之同步，无需手动改。
        """
        w = self._editor
        if w is None or not hasattr(w, "setChecked"):
            return False
        if bool(w.isChecked()) == bool(on):
            return False
        w.setChecked(bool(on))
        return True


# ============================================================================
# 分组渲染
# ============================================================================
class GroupRenderer(QWidget):
    """
    渲染一个分组卡片（GroupBox + 若干 FieldRow）。

    负责：
        · show_when 联动（依赖字段变化时自动显隐行）
        · 把每行的值收集起来
    """

    changed = Signal()

    def __init__(self, group: dict, cmd: dict, store: dict, parent=None):
        super().__init__(parent)
        self.group = group
        self.cmd = cmd
        self.store = store
        self._rows: list[FieldRow] = []
        self._switchable: Segmented | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        box = GroupBox(group.get("title", ""))
        outer.addWidget(box)

        # ---- switchable 分段控件 ----
        sw_def = group.get("switchable")
        if sw_def:
            key = sw_def["key"]
            cur = store.get(key, sw_def.get("default"))
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            self._switchable = Segmented(sw_def["options"], cur)
            self._switchable.changed.connect(
                lambda v, k=key: self._on_switchable(k, v)
            )
            rl.addWidget(self._switchable)
            rl.addStretch(1)
            box.add(row)

        # ---- 字段行 ----
        for f in group.get("fields", []):
            key = f.get("key", "")
            val = store.get(key, f.get("default"))
            fr = FieldRow(f, val)
            fr.changed.connect(self._on_row_changed)
            fr.switch_toggled.connect(self._on_switch_toggled)
            box.add(fr)
            self._rows.append(fr)

        self._refresh_visibility()

    # ---------------------------------------------------------------- 交互
    def _on_switchable(self, key: str, v: str) -> None:
        self.store[key] = v
        self._refresh_visibility()
        self.changed.emit()

    def _on_row_changed(self) -> None:
        # 把值同步进 store，供联动判断
        for fr in self._rows:
            k = fr.field.get("key")
            if k:
                self.store[k] = fr.value()
        self._refresh_visibility()
        self.changed.emit()

    def _on_switch_toggled(self, key: str, on: bool) -> None:
        """互斥开关联动：打开 A 自动关掉它的伙伴。

        字段用 `excl="另一个key"` 声明互斥关系，对应 tdl 里
        MarkFlagsMutuallyExclusive 声明过的参数对（如 dl 的 --continue / --restart）。
        同时传两个互斥参数 tdl 会直接报错，所以必须保证界面层不会同时为真。
        """
        if not on:
            return                       # 只在「打开」时联动，避免递归
        peer_key = ""
        for fr in self._rows:
            if fr.field.get("key") == key:
                peer_key = fr.field.get("excl", "") or ""
                break
        if not peer_key:
            return
        for fr in self._rows:
            if fr.field.get("key") == peer_key:
                fr.set_switch(False)     # 触发 toggled → store 同步
                break

    def _refresh_visibility(self) -> None:
        """按 show_when 决定每行的显隐（store 是共享引用，可读其他组的值）。"""
        for fr in self._rows:
            cond = fr.field.get("show_when")
            if not cond:
                fr.setVisible(True)
                continue
            dep = cond.get("f")
            allow = cond.get("in_", [])
            cur = self.store.get(dep)
            fr.setVisible(cur in allow)

    def refresh_visibility(self) -> None:
        """公共入口：供 FormRenderer 在任意组变化后广播刷新（跨组联动）。"""
        self._refresh_visibility()

    # ---------------------------------------------------------------- 取值
    def values(self) -> dict:
        out = {}
        for fr in self._rows:
            k = fr.field.get("key")
            if k:
                out[k] = fr.value()
        return out

    def set_field_value(self, key: str, text: str) -> bool:
        """按 key 编程式设置某行文本值（找不到该行返回 False）。"""
        for fr in self._rows:
            if fr.field.get("key") == key:
                return fr.set_value_text(text)
        return False


# ============================================================================
# 命令页渲染器
# ============================================================================
class FormRenderer(QWidget):
    """
    渲染整个命令页（可能含多个分组 + 子选项卡）。

    下载页特殊：常驻组 → 子选项卡条 → pane 内容
    其他页：按顺序渲染所有分组
    """

    values_changed = Signal()

    def __init__(self, cmd: dict, store: dict, global_store: dict, parent=None):
        super().__init__(parent)
        self.cmd = cmd
        self.store = store
        self.global_store = global_store
        self._groups: list[GroupRenderer] = []
        self._sub_tab = ""          # 当前子选项卡
        self._sub_bar = None

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(12)

        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        cmd = self.cmd
        groups = cmd.get("groups", [])
        sub_tabs = cmd.get("sub_tabs")

        # ---- 无子选项卡：顺序渲染全部组 ----
        if not sub_tabs:
            for g in groups:
                self._add_group(g)
            self._lay.addStretch(1)
            return

        # ---- 有子选项卡：常驻组 → 选项卡条 → pane 内容 ----
        from .widgets import SubTabBar

        always = [g for g in groups if not g.get("pane")]
        paned = [g for g in groups if g.get("pane")]

        # ① 常驻组
        for g in always:
            self._add_group(g)

        # ② 子选项卡条
        init = self._sub_tab or sub_tabs[0]["id"]
        self._sub_tab = init
        bar = SubTabBar(sub_tabs, init)
        bar.changed.connect(self._on_sub_tab)
        self._sub_bar = bar
        self._lay.addWidget(bar)

        # ③ pane 内容容器
        self._pane_host = QWidget()
        self._pane_lay = QVBoxLayout(self._pane_host)
        self._pane_lay.setContentsMargins(0, 0, 0, 0)
        self._pane_lay.setSpacing(12)
        self._lay.addWidget(self._pane_host)

        self._paned_groups = paned
        self._render_pane(init)
        self._lay.addStretch(1)

    def _add_group(self, g: dict) -> GroupRenderer:
        gr = GroupRenderer(g, self.cmd, self.store)
        gr.changed.connect(self._on_any_changed)
        self._lay.addWidget(gr)
        self._groups.append(gr)
        return gr

    def _on_any_changed(self) -> None:
        """任一组变化 → 广播刷新所有组的 show_when（跨组联动，如
        「群组导出」组的字段依赖「下载来源」组的 srcMode）。"""
        for gr in self._groups:
            gr.refresh_visibility()
        self.values_changed.emit()

    # ---------------------------------------------------------------- 子选项卡
    def _find_scroll(self) -> QScrollArea | None:
        """向上找到承载本渲染器的滚动区（用于保持切换时的滚动位置）。"""
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QScrollArea):
                return p
            p = p.parentWidget()
        return None

    @staticmethod
    def _restore_scroll(bar, pos: int) -> None:
        """还原滚动位置（控件可能已随窗口销毁，需容错）。"""
        try:
            bar.setValue(min(int(pos), bar.maximum()))
        except RuntimeError:
            pass

    def _on_sub_tab(self, tid: str) -> None:
        """切换子选项卡。

        pane 内容会「清空 → 重建」。若不做处理，被销毁控件的焦点会转移给
        新 pane 的第一个输入框，QScrollArea 随之自动滚动到该控件，
        表现为「点一下子选项卡页面就自己往下跳」。
        对策：切换前清掉焦点，重建后还原原滚动位置。
        """
        self._sub_tab = tid

        scroll = self._find_scroll()
        bar = scroll.verticalScrollBar() if scroll is not None else None
        pos = bar.value() if bar is not None else 0

        # ① 切断「旧控件销毁 → 焦点转移给新控件」的链路
        fw = self.window().focusWidget()
        if fw is not None and scroll is not None and scroll.isAncestorOf(fw):
            fw.clearFocus()

        self._render_pane(tid)
        self.values_changed.emit()

        # ② 布局更新后再还原滚动位置（切换瞬间内容高度会先塌缩）
        if bar is not None:
            QTimer.singleShot(0, lambda: self._restore_scroll(bar, pos))

    def _render_pane(self, tid: str) -> None:
        """渲染当前子选项卡的内容。"""
        # 清空
        while self._pane_lay.count():
            it = self._pane_lay.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        # 从 _groups 里摘掉旧的 pane 组
        self._groups = [g for g in self._groups
                        if not g.group.get("pane")]

        from .data import dl_param_fields
        from .widgets import GroupBox

        if tid == "params":
            # 「参数设置」：渲染 GLOBAL_FIELDS 里的 dl 相关项
            # （GroupBox 传空标题：小标题与子选项卡名重复，不再显示）
            gb = GroupBox("")
            for f in dl_param_fields():
                key = f.get("key", "")
                val = self.global_store.get(key, f.get("default"))
                fr = FieldRow(f, val)
                fr.changed.connect(self._on_global_row_changed)
                gb.add(fr)
            self._pane_lay.addWidget(gb)
        else:
            # 「保存设置」等普通 pane
            for g in getattr(self, "_paned_groups", []):
                if g.get("pane") == tid:
                    self._add_group_to(self._pane_lay, g)

    def _add_group_to(self, lay, g: dict) -> None:
        gr = GroupRenderer(g, self.cmd, self.store)
        gr.changed.connect(self.values_changed.emit)
        lay.addWidget(gr)
        self._groups.append(gr)

    def _on_global_row_changed(self) -> None:
        self.values_changed.emit()

    # ---------------------------------------------------------------- 取值
    def values(self) -> dict:
        out = {}
        for gr in self._groups:
            out.update(gr.values())
        # switchable 的值也要带上
        for g in self.cmd.get("groups", []):
            sw = g.get("switchable")
            if sw:
                k = sw["key"]
                out[k] = self.store.get(k, sw.get("default"))
        return out

    def set_field_value(self, key: str, text: str) -> bool:
        """跨组按 key 编程式设置某行文本值（供主窗口做联动，如目录锚定）。"""
        for gr in self._groups:
            if gr.set_field_value(key, text):
                return True
        return False
