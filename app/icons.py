# -*- coding: utf-8 -*-
"""
内联 SVG 图标库 —— 数据直接取自 HTML 原型 index.html 的 ICONS 表。

原型用 createElementNS 造 <svg>，Qt 侧改用 QPainterPath，
路径数据（viewBox 24x24）完全一致，渲染结果等价。

设计约束（沿用原型）：
  * 一律 24x24 viewBox，靠缩放适配 15/16/14px 等尺寸
  * 颜色用 currentColor —— Qt 侧由调用方传 color
  * 找不到名字时安全回退为文字占位，不抛异常
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap, QColor, QIcon

# ============================================================================
# 路径数据（24x24 viewBox）—— 与原型 ICONS 表逐条对应
# ============================================================================
ICON_PATHS: dict[str, str] = {
    "key": "M12.65 10A5.99 5.99 0 1 0 7 13.98c.31 0 .62-.02.92-.07L9.5 15.5l1.5 1.5 1.5-1.5 1.06 1.06a1 1 0 0 0 1.42 0l1.5-1.5a1 1 0 0 0 0-1.42L12.65 10ZM7 11a2 2 0 1 1 0-4 2 2 0 0 1 0 4Z",
    "chat": "M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.2 3.5A.6.6 0 0 1 5 15.1V16h-.5A1.5 1.5 0 0 1 3 14.5v-9Z",
    "search": "M10.5 3a7.5 7.5 0 1 1-4.6 13.4l-2.2 2.2a1 1 0 0 1-1.4-1.4l2.2-2.2A7.5 7.5 0 0 1 10.5 3Zm0 2a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z",
    "forward": "M4 7a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7Zm5 2.2v3.6a.6.6 0 0 0 .93.5l2.9-1.8a.6.6 0 0 0 0-1l-2.9-1.8a.6.6 0 0 0-.93.5Z",
    "upload": "M12 3.6a1 1 0 0 1 .7.29l4 4a1 1 0 1 1-1.4 1.42L13 6.99V15a1 1 0 1 1-2 0V6.99L8.7 9.3a1 1 0 0 1-1.4-1.42l4-4A1 1 0 0 1 12 3.6ZM5 17a1 1 0 0 1 1 1v1h12v-1a1 1 0 1 1 2 0v1.5A1.5 1.5 0 0 1 18.5 21h-13A1.5 1.5 0 0 1 4 19.5V18a1 1 0 0 1 1-1Z",
    "list": "M4 6.5a1 1 0 0 1 1-1h9a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1Zm0 5.5a1 1 0 0 1 1-1h9a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1Zm1 4.5a1 1 0 1 0 0 2h9a1 1 0 1 0 0-2H5Zm17-7.3a1.2 1.2 0 1 1-2.4 0 1.2 1.2 0 0 1 2.4 0Zm0 5.8a1.2 1.2 0 1 1-2.4 0 1.2 1.2 0 0 1 2.4 0Zm0 5.6a1.2 1.2 0 1 1-2.4 0 1.2 1.2 0 0 1 2.4 0Z",
    "sliders": "M4 7h7a1 1 0 0 0 0-2H4a1 1 0 0 0 0 2Zm13.9-2a2.6 2.6 0 0 0-4.9.7H4a1 1 0 0 0 0 2h9c.24.42.6.75 1.05.94a2.6 2.6 0 0 0 3.9 2.7A2.6 2.6 0 0 0 17.9 5Zm.1 8H4a1 1 0 1 0 0 2h14a1 1 0 1 0 0-2Zm3-9a2.6 2.6 0 0 0-5.2 0h.02a2.6 2.6 0 1 0 5.18 0ZM7.1 15H4a1 1 0 1 0 0 2h3.1a2.6 2.6 0 1 0 0-2Zm12.9 2h-7a1 1 0 1 1 0-2h7a1 1 0 1 1 0 2ZM9.7 11a2.6 2.6 0 0 0-5.18 0A2.6 2.6 0 0 0 7.1 13.6a2.6 2.6 0 0 0 2.6-2.6Z",
    "geo": "M12 2.5c3.6 0 6.5 2.93 6.5 6.55 0 2.6-1.6 5.1-3.3 7.1a24.6 24.6 0 0 1-2.6 2.7.9.9 0 0 1-1.2 0 24.6 24.6 0 0 1-2.6-2.7C7.1 14.15 5.5 11.65 5.5 9.05 5.5 5.43 8.4 2.5 12 2.5Zm0 2a4.55 4.55 0 0 0-4.5 4.55c0 1.75 1.2 3.85 2.7 5.65a22 22 0 0 0 1.8 1.95 22 22 0 0 0 1.8-1.95c1.5-1.8 2.7-3.9 2.7-5.65A4.55 4.55 0 0 0 12 4.5Zm0 2.7a1.85 1.85 0 1 1 0 3.7 1.85 1.85 0 0 1 0-3.7Z",
    "download": "M12 3a1 1 0 0 1 1 1v9.2l2.6-2.3a1 1 0 1 1 1.35 1.48l-4.3 3.9a1 1 0 0 1-1.34 0l-4.3-3.9A1 1 0 1 1 8.4 10.9L11 13.2V4a1 1 0 0 1 1-1ZM5 18a1 1 0 0 1 1 1v1h12v-1a1 1 0 1 1 2 0v1.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 20.5V19a1 1 0 0 1 1-1Z",
    "save": "M5 4h11.2a2 2 0 0 1 1.4.6l2.8 2.8A2 2 0 0 1 21 8.8V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm0 2v13h14V8.8L16.2 6H17v4.5a1.5 1.5 0 0 1-1.5 1.5h-6A1.5 1.5 0 0 1 8 10.5V6H5Zm5 0v4h4V6h-4Zm-1.5 7h7a1 1 0 1 1 0 2h-7a1 1 0 1 1 0-2Z",
    "filter": "M3.6 5.5A1.5 1.5 0 0 1 5 4.5h14a1.5 1.5 0 0 1 1.17 2.44L15 13.2V19a1 1 0 0 1-1.55.83l-3-2A1 1 0 0 1 10 17v-3.8L3.83 6.94A1.5 1.5 0 0 1 3.6 5.5Zm2.9 1 4.5 4.9v4.3l2 1.33v-5.63l4.5-4.9H6.5Z",
    "behavior": "M12 3.6a3.4 3.4 0 0 1 3.4 3.4v1h1.1a1 1 0 1 1 0 2h-1.1v1.4h1.1a1 1 0 1 1 0 2h-1.1V15a3.4 3.4 0 1 1-6.8 0V6.99A3.4 3.4 0 0 1 12 3.6Zm1.4 4.4V6.99a1.4 1.4 0 1 0-2.8 0V8h2.8ZM12 11.4a1.4 1.4 0 0 0-1.4 1.4v2.2a1.4 1.4 0 1 0 2.8 0v-2.2a1.4 1.4 0 0 0-1.4-1.4Z",
    "tool": "M14.7 3.3a1 1 0 0 1 1.63.33 5.5 5.5 0 0 0 7.06 7.06 1 1 0 0 1 .33 1.64l-2.4 2.4a1 1 0 0 1-1.02.26 7.5 7.5 0 0 1-3.2-1.7l-6.4 6.4a2.1 2.1 0 1 1-2.97-2.97l6.4-6.4a7.5 7.5 0 0 1-1.7-3.2 1 1 0 0 1 .27-1.02l2-2Z",
    "refresh": "M12 4a8 8 0 0 1 6.9 3.96V6a1 1 0 1 1 2 0v4a1 1 0 0 1-1 1h-4a1 1 0 1 1 0-2h1.5A6 6 0 0 0 6.6 9.5a1 1 0 0 1-1.86-.74A8 8 0 0 1 12 4Zm-7 8a1 1 0 0 1 1 1 6 6 0 0 0 10.9 3.5 1 1 0 1 1 1.86.74A8 8 0 0 1 4 13a1 1 0 0 1 1-1Z",
    "info": "M12 2.5a9.5 9.5 0 1 1 0 19 9.5 9.5 0 0 1 0-19Zm0 2a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15Zm0 2.6a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5Zm0 3.4a1 1 0 0 1 1 1v4.8a1 1 0 1 1-2 0v-4.8a1 1 0 0 1 1-1Z",
    "warn": "M12 3.2a2 2 0 0 1 1.74 1l7.1 12.3A2 2 0 0 1 19.1 19.5H4.9a2 2 0 0 1-1.74-3l7.1-12.3A2 2 0 0 1 12 3.2Zm0 2.3L5.1 17.5h13.8L12 5.5Zm0 3.4a1 1 0 0 1 1 1v3a1 1 0 1 1-2 0v-3a1 1 0 0 1 1-1Zm0 5.6a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4Z",
    "folder": "M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2a1.5 1.5 0 0 1 1.17.56l.86 1.06A1.5 1.5 0 0 0 11.9 7h7.6A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-11Zm2 2.5h14v9H5V9Z",
    # --- 消息类型（与 MSG_KINDS 对应）---
    "photo": "M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Zm2 .5v8.3l3.2-3.2a1 1 0 0 1 1.4 0l2.3 2.3 1.9-1.9a1 1 0 0 1 1.4 0L18 13.6V6H6Zm3.4 1.4a1.6 1.6 0 1 1 0 3.2 1.6 1.6 0 0 1 0-3.2Z",
    "video": "M4 6.8A1.8 1.8 0 0 1 5.8 5h8.4A1.8 1.8 0 0 1 16 6.8v10.4a1.8 1.8 0 0 1-1.8 1.8H5.8A1.8 1.8 0 0 1 4 17.2V6.8Zm2 .2v10h8V7H6Zm11.3 1.1 2.1-1.5a.8.8 0 0 1 1.26.65v9.5a.8.8 0 0 1-1.26.65l-2.1-1.5a.8.8 0 0 1-.34-.65V8.76a.8.8 0 0 1 .34-.66Z",
    "voice": "M12 3.5a2.9 2.9 0 0 1 2.9 2.9v5a2.9 2.9 0 1 1-5.8 0v-5A2.9 2.9 0 0 1 12 3.5ZM6.6 10.6a1 1 0 0 1 1 1 4.4 4.4 0 1 0 8.8 0 1 1 0 1 1 2 0 6.4 6.4 0 0 1-5.4 6.33V20h2.3a1 1 0 1 1 0 2H8.7a1 1 0 1 1 0-2H11v-2.07a6.4 6.4 0 0 1-5.4-6.33 1 1 0 0 1 1-1Z",
    "audio": "M19.5 3.6a1 1 0 0 1 1.3.96v9.04a3.6 3.6 0 0 1-3.6 3.6h-.4a3.6 3.6 0 1 1 3.6-3.6V7.3l-8.8 2.2v7.1a3.6 3.6 0 0 1-3.6 3.6H7.6a3.6 3.6 0 1 1 3.6-3.6V7.9a1 1 0 0 1 .76-.97l7.54-1.88ZM7.6 15.7a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Zm8.8-2.2a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Z",
    "document": "M6.5 3h6.4a1.5 1.5 0 0 1 1.06.44l4.6 4.6A1.5 1.5 0 0 1 19 9.1V19.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5v-15A1.5 1.5 0 0 1 6.5 3Zm.5 2v14h10V10h-3.5A1.5 1.5 0 0 1 12 8.5V5H7Zm7 .4V8h2.6L14 5.4Z",
    "db": "M12 3c4.3 0 7.5 1.3 7.5 3v12c0 1.7-3.2 3-7.5 3s-7.5-1.3-7.5-3V6c0-1.7 3.2-3 7.5-3Zm0 2C8.2 5 6.3 6 5.8 6.3 6.3 6.7 8.2 7.6 12 7.6s5.7-.9 6.2-1.3C17.7 6 15.8 5 12 5Zm5.5 4.6a16.6 16.6 0 0 1-5.5.8 16.6 16.6 0 0 1-5.5-.8v3.2c.5.4 2.4 1.2 5.5 1.2s5-.8 5.5-1.2v-3.2Zm0 5.6a16.6 16.6 0 0 1-5.5.8 16.6 16.6 0 0 1-5.5-.8v2.8c.5.4 2.4 1.2 5.5 1.2s5-.8 5.5-1.2v-2.8Z",
}


# ============================================================================
# SVG path -> QPainterPath
# ============================================================================
def _parse_path(d: str) -> QPainterPath:
    """
    手工解析 SVG path 子集（本图标库只用得上这些命令）。

    支持：M m L l H h V v C c S s Q q T t A a Z z
    不支持：E 指数记法（图标数据里没有）

    Qt 的 QPainterPath 没有内置 SVG 解析器，所以这里自己实现。
    圆弧 A 用「端点参数 -> 圆心参数」公式转为三次贝塞尔，与浏览器渲染视觉一致。
    """
    import re
    import math

    path = QPainterPath()
    # 把命令与参数切成 token 流
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?", d)

    i = 0
    cmd = None
    cx = cy = 0.0          # 当前点
    sx = sy = 0.0          # 子路径起点
    prev_ctrl = None       # 上一个三次/二次控制点（S/T 用）
    prev_cmd = None
    start = True           # 需要 moveTo 而不是 lineTo

    def num():
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < len(tokens):
        t = tokens[i]
        if re.match(r"^[MmLlHhVvCcSsQqTtAaZz]$", t):
            cmd = t
            i += 1
        # 若是隐式重复（数字开头），沿用上一个命令；M/m 之后隐式变 L/l
        if cmd is None:
            break

        rel = cmd.islower()
        c = cmd.upper()

        if c == "M":
            x, y = num(), num()
            cx, cy = (cx + x, cy + y) if rel else (x, y)
            sx, sy = cx, cy
            path.moveTo(cx, cy)
            start = False
            if cmd == "M":
                cmd = "L"
            else:
                cmd = "l"
            prev_ctrl = None

        elif c == "L":
            x, y = num(), num()
            cx, cy = (cx + x, cy + y) if rel else (x, y)
            path.lineTo(cx, cy)
            prev_ctrl = None

        elif c == "H":
            x = num()
            cx = cx + x if rel else x
            path.lineTo(cx, cy)
            prev_ctrl = None

        elif c == "V":
            y = num()
            cy = cy + y if rel else y
            path.lineTo(cx, cy)
            prev_ctrl = None

        elif c == "C":
            x1, y1 = num(), num()
            x2, y2 = num(), num()
            x, y = num(), num()
            if rel:
                x1, y1 = cx + x1, cy + y1
                x2, y2 = cx + x2, cy + y2
                x, y = cx + x, cy + y
            path.cubicTo(x1, y1, x2, y2, x, y)
            prev_ctrl = (x2, y2)
            cx, cy = x, y

        elif c == "S":
            x2, y2 = num(), num()
            x, y = num(), num()
            if rel:
                x2, y2 = cx + x2, cy + y2
                x, y = cx + x, cy + y
            # 反射上一个控制点
            if prev_ctrl and prev_cmd in ("C", "S", "c", "s"):
                x1 = 2 * cx - prev_ctrl[0]
                y1 = 2 * cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            path.cubicTo(x1, y1, x2, y2, x, y)
            prev_ctrl = (x2, y2)
            cx, cy = x, y

        elif c == "Q":
            x1, y1 = num(), num()
            x, y = num(), num()
            if rel:
                x1, y1 = cx + x1, cy + y1
                x, y = cx + x, cy + y
            path.quadTo(x1, y1, x, y)
            prev_ctrl = (x1, y1)
            cx, cy = x, y

        elif c == "T":
            x, y = num(), num()
            if rel:
                x, y = cx + x, cy + y
            if prev_ctrl and prev_cmd in ("Q", "T", "q", "t"):
                x1 = 2 * cx - prev_ctrl[0]
                y1 = 2 * cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            path.quadTo(x1, y1, x, y)
            prev_ctrl = (x1, y1)
            cx, cy = x, y

        elif c == "A":
            rx, ry = num(), num()
            rot = num()
            laf = num()
            sf = num()
            x, y = num(), num()
            if rel:
                x, y = cx + x, cy + y
            _arc_to(path, cx, cy, rx, ry, rot, laf, sf, x, y)
            cx, cy = x, y
            prev_ctrl = None

        elif c == "Z":
            path.closeSubpath()
            cx, cy = sx, sy
            prev_ctrl = None
            start = True

        prev_cmd = cmd
        # Z 之后若还有数字，按新子路径起点算
        if start and i < len(tokens) and re.match(r"^-?\d", tokens[i]):
            # 隐式 M
            cmd = "M"

    return path


def _arc_to(path: QPainterPath, x1, y1, rx, ry, rot_deg, laf, sf, x2, y2):
    """SVG 圆弧端点参数 -> 圆心参数 -> 三次贝塞尔。"""
    import math

    if rx == 0 or ry == 0:
        path.lineTo(x2, y2)
        return

    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rot_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    # 步骤 1：计算 (x1', y1')
    dx2 = (x1 - x2) / 2.0
    dy2 = (y1 - y2) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2

    # 修正半径过小的情况
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s

    # 步骤 2：计算圆心
    sign = -1.0 if laf == sf else 1.0
    numr = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denr = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = sign * math.sqrt(max(0.0, numr / denr)) if denr else 0.0
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    # 步骤 3：计算起始角与扫过角
    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        if n == 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, dot / n)))
        return -a if (ux * vy - uy * vx) < 0 else a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )
    if sf == 0 and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sf == 1 and dtheta < 0:
        dtheta += 2 * math.pi

    # 步骤 4：用贝塞尔段逼近（每段 <= 90°）
    segs = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / segs
    t = 4.0 / 3.0 * math.tan(delta / 4.0)

    for _ in range(segs):
        cos1, sin1 = math.cos(theta1), math.sin(theta1)
        theta2 = theta1 + delta
        cos2, sin2 = math.cos(theta2), math.sin(theta2)

        # 椭圆上的点（含旋转）
        def pt(ea, eb):
            px = cos_p * rx * ea - sin_p * ry * eb + cx
            py = sin_p * rx * ea + cos_p * ry * eb + cy
            return px, py

        # 切线
        def tan(ea, eb):
            tx = -rx * ea
            ty = ry * eb
            return cos_p * tx - sin_p * ty, sin_p * tx + cos_p * ty

        p1x, p1y = pt(cos1, sin1)
        p2x, p2y = pt(cos2, sin2)
        t1x, t1y = tan(sin1, cos1)
        t2x, t2y = tan(sin2, cos2)

        path.cubicTo(
            p1x + t * t1x, p1y + t * t1y,
            p2x - t * t2x, p2y - t * t2y,
            p2x, p2y,
        )
        theta1 = theta2


# ============================================================================
# 路径缓存 + 渲染
# ============================================================================
_PATH_CACHE: dict[str, QPainterPath] = {}


def icon_path(name: str) -> QPainterPath | None:
    """取图标路径（带缓存）。未知名字返回 None。"""
    d = ICON_PATHS.get(name)
    if d is None:
        return None
    if name not in _PATH_CACHE:
        _PATH_CACHE[name] = _parse_path(d)
    return _PATH_CACHE[name]


def make_icon(name: str, size: int, color: str, dpr: float = 2.0) -> QIcon:
    """
    生成 QIcon。24x24 的路径数据按 size 缩放，用设备像素比超采样保证边缘清晰。
    """
    px_size = int(size * dpr)
    pm = QPixmap(px_size, px_size)
    pm.fill(Qt.transparent)

    p = icon_path(name)
    if p is None:
        return QIcon(pm)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(px_size / 24.0, px_size / 24.0)
    from PySide6.QtGui import QBrush
    painter.setBrush(QBrush(QColor(color)))
    painter.setPen(Qt.NoPen)
    painter.drawPath(p)
    painter.end()

    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


class IconCache:
    """
    图标缓存：同一 (name, size, color) 只渲染一次。

    Qt 的 QIcon 支持多状态，这里按「常态/激活态」两色缓存，
    因为侧边栏导航项激活时图标要变主题色。
    """

    def __init__(self, dpr: float = 2.0):
        self.dpr = dpr
        self._cache: dict[tuple, QIcon] = {}

    def get(self, name: str, size: int, color: str) -> QIcon:
        key = (name, size, color)
        if key not in self._cache:
            self._cache[key] = make_icon(name, size, color, self.dpr)
        return self._cache[key]

    def stats(self) -> dict:
        return {"cached": len(self._cache), "known": len(ICON_PATHS)}


# 全局单例
ICONS = IconCache()


# ============================================================================
# 应用图标（窗口 / 任务栏）
# ============================================================================
_APP_ICON: QIcon | None = None
APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def app_icon() -> QIcon:
    """应用图标：主题蓝圆角方块 + 白色下载箭头（与界面图标同一套路径数据）。

    一次把 16~256px 全部塞进同一个 QIcon —— 标题栏(16/24)、任务栏(32/48)、
    高 DPI 缩放(64+) 各取所需，避免系统缩放导致发虚。
    """
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON

    from PySide6.QtGui import QBrush, QLinearGradient, QPen

    arrow = icon_path("download")
    icon = QIcon()
    for size in APP_ICON_SIZES:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # 底色：圆角方块 + 上浅下深渐变（小尺寸下比纯色更有立体感）
        grad = QLinearGradient(0, 0, 0, size)
        grad.setColorAt(0.0, QColor("#4aa3ff"))
        grad.setColorAt(1.0, QColor("#1f7ae0"))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.NoPen)
        radius = size * 0.22
        p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
        # 白色下载箭头（24x24 路径缩放到 62% 居中）
        # ⚠️ 必须「填充 + 圆头描边」双管齐下：该路径的笔画只有 2 个单位宽，
        # 缩到 16px 后不足 1px，纯填充会被抗锯齿糊成一片（实测白像素为 0）。
        # 描边宽度按 24 坐标给（随 transform 等比缩放），小尺寸才看得清。
        if arrow is not None:
            side = size * 0.62
            p.save()
            p.translate((size - side) / 2.0, (size - side) / 2.0)
            p.scale(side / 24.0, side / 24.0)
            pen = QPen(QColor("#ffffff"))
            pen.setWidthF(24 * 0.075)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(QBrush(QColor("#ffffff")))
            p.drawPath(arrow)
            p.restore()
        p.end()
        icon.addPixmap(pm)

    _APP_ICON = icon
    return _APP_ICON
