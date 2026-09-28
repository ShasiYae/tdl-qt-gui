# -*- coding: utf-8 -*-
"""
离屏截图 —— 把窗口渲染成 PNG，用于人工核对视觉。

用法：python shot.py [输出目录]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import QApplication

from app.theme import QSS
from app.main_window import MainWindow


def grab(win: MainWindow, path: str) -> None:
    """把窗口内容渲染到 pixmap 并保存。"""
    win.resize(1180, 780)
    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()
    pm: QPixmap = win.grab()
    pm.save(path, "PNG")
    print(f"  已保存 {path}  ({pm.width()}x{pm.height()})")


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "shots"
    os.makedirs(out_dir, exist_ok=True)

    # 关键：offscreen 平台下 Qt 字体库为空（0 个字体），中文会渲染成方块。
    # 截图必须走真实 windows 平台，窗口会在屏幕上短暂出现后立刻被抓图。
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    win = MainWindow()
    win.show()
    # 不用 QEventLoop.exec()：实测本环境事件循环可能不返回（卡死）。
    # 抓图不需要动画帧，多轮 processEvents 足以让布局/绘制完成。
    for _ in range(30):
        app.processEvents()
    grab(win, os.path.join(out_dir, "00-main.png"))

    cases = [
        ("01-hub-task-download", "hub-task", None),
        ("02-hub-other",         "hub-other", None),
        ("03-hub-setting",       "hub-setting", None),
        ("04-login",             "login", None),
        ("05-tasks",             "tasks", None),
        ("06-msgview",           "msgview", None),
    ]

    for name, nav_id, _ in cases:
        win.on_nav(nav_id)
        for _ in range(30):
            app.processEvents()
        grab(win, os.path.join(out_dir, name + ".png"))

    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
