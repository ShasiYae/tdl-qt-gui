# -*- coding: utf-8 -*-
"""
日志输出面板 + 执行控制条。

DOM 对应原型的 .actions（执行/重置）+ .cmdbar（等效命令）+ 日志区。

特性：
    · 实时流式追加（stdout 白字 / stderr 红字 / 系统消息灰字）
    · 自动滚到底（用户手动上滚时暂停自动滚）
    · 等效命令预览（等宽字体，可复制）
    · 执行 / 取消 / 清空 / 复制
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat, QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSizePolicy, QScrollBar, QLineEdit,
)

from .theme import T
from .icons import ICONS


# ============================================================================
# 日志面板
# ============================================================================
class LogPanel(QWidget):
    """带着色与自动滚底的日志输出区。"""

    def __init__(self, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ---- 标题行 ----
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)

        self._title = QLabel("运行输出")
        self._title.setObjectName("GroupTitle")
        head.addWidget(self._title)

        self._status = QLabel("就绪")
        self._status.setStyleSheet(
            f"color:{T.TEXT_FAINT}; font-size:11.5px; background:transparent;"
        )
        head.addWidget(self._status)
        head.addStretch(1)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("Btn")
        self.btn_clear.setFixedHeight(T.H_CHIP)
        self.btn_clear.clicked.connect(self.clear)
        head.addWidget(self.btn_clear)

        self.btn_copy = QPushButton("复制全部")
        self.btn_copy.setObjectName("Btn")
        self.btn_copy.setFixedHeight(T.H_CHIP)
        self.btn_copy.clicked.connect(self.copy_all)
        head.addWidget(self.btn_copy)

        lay.addLayout(head)

        # ---- 输出区 ----
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.document().setMaximumBlockCount(2000)   # 上限：自动丢弃最老的行，防长时间任务内存无限增长
        f = QFont("Consolas")
        f.setPixelSize(12)
        self.view.setFont(f)
        self.view.setStyleSheet(
            f"QPlainTextEdit {{ background:#1b1f24; color:#d7dde5;"
            f" border:1px solid {T.BORDER_STRONG};"
            f" border-radius:{T.RADIUS}px; padding:8px; }}"
        )
        self.view.setMinimumHeight(160)
        lay.addWidget(self.view, 1)

        self._autoscroll = True
        self.view.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ---------------------------------------------------------------- 滚动
    def _on_scroll(self, val: int) -> None:
        sb = self.view.verticalScrollBar()
        # 距底部 4px 内视为"在底部"，恢复自动滚
        self._autoscroll = val >= sb.maximum() - 4

    def _scroll_to_end(self) -> None:
        if self._autoscroll:
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ---------------------------------------------------------------- 写入
    def _append(self, text: str, color: str) -> None:
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur.insertText(text + "\n", fmt)
        self.view.setTextCursor(cur)
        self._scroll_to_end()

    def out(self, line: str) -> None:
        """标准输出（白）。"""
        self._append(line, "#d7dde5")

    def err(self, line: str) -> None:
        """标准错误（红）。"""
        self._append(line, "#ff7b72")

    def sys(self, line: str) -> None:
        """系统消息（灰）。"""
        self._append(line, "#8b95a1")

    def ok(self, line: str) -> None:
        """成功消息（绿）。"""
        self._append(line, "#7ee787")

    # ---------------------------------------------------------------- 操作
    def clear(self) -> None:
        self.view.clear()
        self._autoscroll = True

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText())

    def set_status(self, text: str, color: str = "") -> None:
        self._status.setText(text)
        c = color or T.TEXT_FAINT
        self._status.setStyleSheet(
            f"color:{c}; font-size:11.5px; background:transparent;"
        )


# ============================================================================
# 执行控制条（等效命令 + 按钮）
# ============================================================================
class ActionBar(QWidget):
    """
    底部操作条。

    DOM 对应原型：.actions + .cmdbar
      左：执行 / 重置 按钮
      下：等效命令预览（等宽）+ 复制
    """

    run_clicked = Signal()
    reset_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ---- 按钮行：嵌入页面右上方（标题区右侧）----
        self.btn_row = QWidget()
        btns = QHBoxLayout(self.btn_row)
        btns.setContentsMargins(0, 0, 0, 0)
        btns.setSpacing(8)

        self.btn_run = QPushButton("执行")
        self.btn_run.setObjectName("BtnPrimary")
        self.btn_run.setFixedHeight(T.H_CONTROL)
        self.btn_run.clicked.connect(self.run_clicked.emit)
        btns.addWidget(self.btn_run)

        self.btn_cancel = QPushButton("终止")
        self.btn_cancel.setObjectName("Btn")
        self.btn_cancel.setFixedHeight(T.H_CONTROL)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_clicked.emit)
        btns.addWidget(self.btn_cancel)

        self.btn_reset = QPushButton("重置")
        self.btn_reset.setObjectName("Btn")
        self.btn_reset.setFixedHeight(T.H_CONTROL)
        self.btn_reset.clicked.connect(self.reset_clicked.emit)
        btns.addWidget(self.btn_reset)

        lay.addWidget(self.btn_row)

        # ---- 等效命令：嵌入页面内容末尾 ----
        self.cmd_row = QWidget()
        cmd_row = QHBoxLayout(self.cmd_row)
        cmd_row.setContentsMargins(0, 0, 0, 0)
        cmd_row.setSpacing(8)

        tag = QLabel("等效命令")
        tag.setFixedHeight(T.H_CHIP)
        tag.setStyleSheet(
            f"color:{T.TEXT_FAINT}; font-size:11.5px; background:{T.SEG_BG};"
            f"border-radius:4px; padding:0 8px;"
        )
        cmd_row.addWidget(tag)

        # 只读输入框（而非 QLabel）：长命令不会撑大页面最小宽度，
        # 超出部分可横向滚动查看，避免把右侧内容挤出可见范围
        self.cmd_label = QLineEdit()
        self.cmd_label.setObjectName("CmdPreview")
        self.cmd_label.setReadOnly(True)
        self.cmd_label.setFixedHeight(T.H_CHIP)
        self.cmd_label.setMinimumWidth(0)
        self.cmd_label.setStyleSheet(
            f"font-family:Consolas,monospace; font-size:11.5px;"
            f"color:{T.ACCENT_INK}; background:{T.ACCENT_HOVER};"
            f"border:1px solid {T.ACCENT_LINE}; border-radius:5px;"
            f"padding:0 8px;"
        )
        self.cmd_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cmd_row.addWidget(self.cmd_label, 1)

        self.btn_copy_cmd = QPushButton("复制")
        self.btn_copy_cmd.setObjectName("Btn")
        self.btn_copy_cmd.setFixedHeight(T.H_CHIP)
        self.btn_copy_cmd.clicked.connect(self._copy_cmd)
        cmd_row.addWidget(self.btn_copy_cmd)

        lay.addWidget(self.cmd_row)

    def _copy_cmd(self) -> None:
        QGuiApplication.clipboard().setText(self.cmd_label.text())

    def set_cmd(self, argv: list[str], tdl_path: str = "") -> None:
        """更新等效命令预览。"""
        import os as _os
        from .runner import _quote
        if not argv:
            self.cmd_label.setText("（无参数）")
            self.cmd_label.setToolTip("")
            return
        # 只显示文件名，避免路径过长
        exe = _os.path.basename(tdl_path) if tdl_path else "tdl"
        parts = [exe] + [_quote(a) for a in argv]
        text = " ".join(parts)
        self.cmd_label.setText(text)
        self.cmd_label.setToolTip(text)      # 超长命令可悬停查看 / 光标横向滚动

    def set_running(self, running: bool) -> None:
        self.btn_run.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_reset.setEnabled(not running)
        self.btn_run.setText("执行中…" if running else "执行")
