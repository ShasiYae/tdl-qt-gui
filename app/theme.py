# -*- coding: utf-8 -*-
"""
设计令牌 —— 与 HTML 原型 index.html 的 :root 变量 1:1 对齐。

原型的尺寸令牌是「选项卡一定能对齐」的根本保障，Qt 侧照搬数值，
不另起一套，避免两边尺寸漂移。
"""
from __future__ import annotations


class T:
    """颜色 / 尺寸 / 字体令牌。干跑时不依赖 Qt，纯数据。"""

    # ===== 基础面 =====
    BG = "#f4f4f5"
    PANEL = "#ffffff"
    SIDEBAR = "#f7f8fa"
    BORDER = "#e6e8eb"
    BORDER_STRONG = "#d5d9de"
    LINE = "#e6e8eb"
    STATUS_BAR = "#eef1f5"
    SEG_BG = "#edf0f3"

    # ===== 文字 =====
    TEXT = "#0f1419"
    TEXT_DIM = "#55606c"
    TEXT_FAINT = "#8b95a1"

    # ===== 主题色（Telegram 蓝）=====
    ACCENT = "#2a8cf0"
    ACCENT_DEEP = "#1c7ad8"
    ACCENT_SOFT = "#e6f2fe"
    ACCENT_HOVER = "#f2f8ff"
    ACCENT_LINE = "#a8d3f8"
    ACCENT_INK = "#1668b8"

    # ===== 语义色 =====
    DANGER = "#d64545"
    OK = "#2e9e5b"
    WARN = "#d29922"
    WARN_BG = "#fff8e1"
    WARN_BORDER = "#f0d68a"
    WARN_TEXT = "#8a6100"
    LINK_UP = "#1a7f37"

    # ===== 形状 =====
    RADIUS = 9
    RADIUS_SM = 7
    RADIUS_PILL = 19

    # ===== 尺寸令牌（核心）=====
    H_TAB = 38          # 一级横向选项卡（药丸）高度
    H_TABSTRIP = 33     # 下划线型子选项卡高度
    H_CONTROL = 32      # 按钮 / 输入框统一高度
    H_CHIP = 26         # 小标签 / 迷你按钮
    H_TITLEBAR = 44
    H_STATUSBAR = 28
    H_NAV_SUB = 28
    H_BADGE = 17
    TAB_GAP = 4

    # ===== 侧边栏 =====
    W_SIDEBAR = 236

    # ===== 图标尺寸 =====
    ICO_NAV = 15        # 侧边栏导航项
    ICO_NAV_SUB = 13    # 侧边栏缩进子项
    ICO_TAB = 16        # 一级选项卡
    ICO_SUBTAB = 14     # 子选项卡


# ============================================================================
# QSS 样式表 —— 与原型 CSS 逐条对应
# ============================================================================

QSS = f"""
/* ===== 全局 ===== */
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {T.TEXT};
}}

QWidget#Root {{ background: {T.BG}; }}

/* ===== 顶栏 ===== */
QWidget#TitleBar {{
    background: {T.PANEL};
    border-bottom: 1px solid {T.BORDER};
}}
QLabel#Logo {{
    font-size: 15px;
    font-weight: 650;
    letter-spacing: -0.2px;
    background: transparent;
}}
QLabel#LogoAccent {{
    font-size: 15px;
    font-weight: 650;
    color: {T.ACCENT};
    background: transparent;
}}
QLabel#ProtoBadge {{
    font-size: 11px;
    color: {T.WARN_TEXT};
    background: {T.WARN_BG};
    border: 1px solid {T.WARN_BORDER};
    border-radius: 10px;
    padding: 2px 8px;
}}
QLabel#LoginStatus {{
    font-size: 12px;
    color: {T.TEXT_DIM};
    background: transparent;
}}
/* 注：顶栏「● 未登录」已于 2026-09-27 移除（tdl 不暴露登录态，恒为未登录，无意义）。
   该 QSS 保留仅为兼容旧截图脚本引用。 */
/* ===== 空间切换胶囊 ===== */
QPushButton#NsSwitch {{
    background: {T.ACCENT_SOFT};
    border: 1px solid {T.ACCENT_LINE};
    border-radius: 14px;
    color: {T.ACCENT_INK};
    font-size: 12px;
    font-weight: 600;
    padding: 3px 10px;
    text-align: left;
}}
QPushButton#NsSwitch:hover {{ background: #cfe1ff; }}

/* ===== 侧边栏 ===== */
QWidget#Sidebar {{
    background: {T.SIDEBAR};
    border-right: 1px solid {T.BORDER};
}}
QLabel#SidebarHead {{
    font-size: 11px;
    font-weight: 650;
    color: {T.TEXT_FAINT};
    padding: 12px 14px 4px;
    background: transparent;
}}

QScrollArea#NavScroll {{ background: transparent; border: none; }}
QScrollArea#NavScroll > QWidget > QWidget {{ background: transparent; }}

QPushButton#NavItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {T.RADIUS_SM}px;
    color: {T.TEXT};
    font-size: 13px;
    text-align: left;
    padding: 0 9px;
}}
QPushButton#NavItem:hover {{
    background: {T.ACCENT_HOVER};
    border-color: transparent;
}}
QPushButton#NavItem[active="true"] {{
    background: {T.ACCENT_SOFT};
    border-color: {T.ACCENT_LINE};
    color: {T.ACCENT_INK};
    font-weight: 600;
}}

/* ===== 内容区标题（原型 .content-head）===== */
QWidget#Content {{ background: {T.BG}; }}
QWidget#ContentHead {{
    background: {T.PANEL};
    border-bottom: 1px solid {T.BORDER};
}}
QLabel#HName {{
    font-size: 15px;
    font-weight: 600;
    background: transparent;
}}
QLabel#HDesc {{
    font-size: 12px;
    color: {T.TEXT_DIM};
    background: transparent;
}}

/* ===== 一级选项卡（药丸，固定高度）===== */
QPushButton#HubTab {{
    background: {T.PANEL};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.RADIUS_PILL}px;
    color: {T.TEXT_DIM};
    font-size: 13px;
    font-weight: 600;
    padding: 0 15px;
    text-align: center;
}}
QPushButton#HubTab:hover {{
    background: {T.ACCENT_HOVER};
    border-color: {T.ACCENT_LINE};
}}
QPushButton#HubTab[on="true"] {{
    background: {T.ACCENT_SOFT};
    border-color: {T.ACCENT};
    color: {T.ACCENT_INK};
}}

/* ===== 子选项卡（下划线条）===== */
QWidget#SubTabBar {{
    background: {T.PANEL};
    border-bottom: 1px solid {T.LINE};
}}
QPushButton#SubTab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: {T.TEXT_DIM};
    font-size: 12.5px;
    font-weight: 500;
    padding: 0 14px;
}}
QPushButton#SubTab:hover {{ color: {T.ACCENT_INK}; }}
QPushButton#SubTab[on="true"] {{
    color: {T.ACCENT_INK};
    font-weight: 650;
    border-bottom-color: {T.ACCENT};
}}

/* ===== 分组（原型 .group：透明无卡片，标题小灰字+底线）===== */
QWidget#GroupBox {{
    background: transparent;
    border: none;
    border-radius: 0px;
}}
QLabel#GroupTitle {{
    font-size: 11px;
    font-weight: 650;
    letter-spacing: 0.5px;
    color: {T.TEXT_DIM};
    background: transparent;
    padding-bottom: 7px;
    border-bottom: 1px solid {T.BORDER};
}}
QLabel#FieldLabel {{
    font-size: 13px;
    font-weight: 600;
    color: {T.TEXT};
    background: transparent;
}}
QLabel#FieldFlag {{
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
    color: {T.ACCENT};
    background: transparent;
}}
QLabel#FieldHelp {{
    font-size: 11.5px;
    color: {T.TEXT_FAINT};
    background: transparent;
}}

/* ===== 控件（统一 32px 高）===== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {T.PANEL};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.RADIUS_SM}px;
    padding: 0 9px;
    selection-background-color: {T.ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {T.ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {T.SEG_BG};
    color: {T.TEXT_FAINT};
}}
/* 数字框：隐藏上下调节小箭头（直接键入数值，避免多余小控件） */
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0px;
    border: none;
    background: transparent;
}}
QSpinBox::up-arrow, QSpinBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {T.PANEL};
    border: 1px solid {T.BORDER_STRONG};
    selection-background-color: {T.ACCENT_SOFT};
    selection-color: {T.ACCENT_INK};
    outline: none;
}}

/* ===== 按钮 ===== */
QPushButton#Btn {{
    background: {T.PANEL};
    border: 1px solid {T.BORDER_STRONG};
    border-radius: {T.RADIUS_SM}px;
    color: {T.TEXT};
    font-size: 13px;
    padding: 0 18px;
}}
QPushButton#Btn:hover {{ background: {T.ACCENT_HOVER}; border-color: {T.ACCENT_LINE}; }}
QPushButton#BtnPrimary {{
    background: {T.ACCENT};
    border: 1px solid {T.ACCENT};
    border-radius: {T.RADIUS_SM}px;
    color: #ffffff;
    font-size: 13px;
    font-weight: 600;
    padding: 0 18px;
}}
QPushButton#BtnPrimary:hover {{ background: {T.ACCENT_DEEP}; }}

/* ===== 底部状态栏 ===== */
QWidget#StatusBar {{
    background: {T.STATUS_BAR};
    border-top: 1px solid {T.BORDER_STRONG};
}}
QLabel#SbLabel {{ font-size: 11.5px; color: {T.TEXT_FAINT}; background: transparent; }}
QLabel#SbValue {{ font-size: 11.5px; color: {T.TEXT}; font-weight: 550; background: transparent; }}
QLabel#SbValueDim {{ font-size: 11.5px; color: {T.TEXT_FAINT}; background: transparent; }}
QLabel#SbMono {{
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
    color: {T.ACCENT};
    background: transparent;
}}
QLabel#SbSep {{ color: {T.BORDER}; background: transparent; }}

/* ===== 悬浮提示（白底深字，与白色卡片风格一致）===== */
QToolTip {{
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}
"""
