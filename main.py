# -*- coding: utf-8 -*-
"""
tdl GUI 客户端入口。

用法：
    python main.py                  # 启动
    python main.py --probe          # 无头自检（不开窗口）
    python main.py --tdl <path>     # 指定 tdl.exe 路径
    python main.py --run <cmd> ...  # 命令行直跑（不启窗口，调试用）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _probe() -> int:
    """无头自检：验证数据层、图标解析、控件构造、命令拼装、tdl 可用性。

    报告同时写入软件目录的 probe-report.txt（UTF-8）—— 打包成 --windowed exe 后
    没有控制台，只有退出码与这份报告能反映结果。
    """
    # windowed exe 的 stdout/stderr 可能是无效句柄：换成安全对象，避免任何
    # 第三方库的 print 把自检搞崩（真正结果见退出码 + probe-report.txt）
    for _name in ("stdout", "stderr"):
        st = getattr(sys, _name, None)
        if st is None:
            continue
        try:
            st.write("")
        except Exception:                                 # noqa: BLE001
            try:
                setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:                             # noqa: BLE001
                pass

    from PySide6.QtWidgets import QApplication
    from app.theme import T, QSS
    from app.icons import ICON_PATHS, icon_path, app_icon, APP_ICON_SIZES
    from app.data import (
        COMMANDS, CMD_BY_ID, NAV_GROUPS, GLOBAL_FIELDS,
        global_args_fields, dl_param_fields, DEFAULT_TDL_PATH,
    )

    fails: list[str] = []
    oks: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        (oks if cond else fails).append(msg)

    # --- 设计令牌 ---
    ok(T.H_TAB == 38, "尺寸令牌 --h-tab = 38px")
    ok(T.H_TABSTRIP == 33, "尺寸令牌 --h-tabstrip = 33px")
    ok(T.H_CONTROL == 32, "尺寸令牌 --h-control = 32px")
    ok(T.H_CHIP == 26, "尺寸令牌 --h-chip = 26px")
    ok(T.ACCENT == "#2a8cf0", "主题色为 Telegram 蓝")

    # --- 图标库 ---
    ok(len(ICON_PATHS) >= 23, f"图标库条目数 {len(ICON_PATHS)} >= 23")
    parsed_ok = 0
    for name in ICON_PATHS:
        try:
            p = icon_path(name)
            if p is not None and p.elementCount() > 0:
                parsed_ok += 1
        except Exception as e:
            fails.append(f"图标 {name} 解析抛异常: {e}")
    ok(parsed_ok == len(ICON_PATHS),
       f"全部图标路径解析成功 {parsed_ok}/{len(ICON_PATHS)}")
    ok(icon_path("__不存在__") is None, "未知图标名安全返回 None")

    # --- 数据层 ---
    ok(len(COMMANDS) >= 18, f"命令数 {len(COMMANDS)} >= 18")
    ids = [c["id"] for c in COMMANDS]
    ok(len(ids) == len(set(ids)), "命令 id 无重复")
    for g in NAV_GROUPS:
        ok(g["cmd_id"] in CMD_BY_ID, f"导航项 {g['id']} 指向存在的命令")
    ok(len(GLOBAL_FIELDS) == 11, f"全局字段总数 {len(GLOBAL_FIELDS)} = 11（args 4 + dl 7）")
    ok(len(global_args_fields()) == 4, f"全局参数页 {len(global_args_fields())} 项 = 4")
    ok(len(dl_param_fields()) == 7, f"下载参数页 {len(dl_param_fields())} 项 = 7")
    ok(len(global_args_fields()) + len(dl_param_fields()) == len(GLOBAL_FIELDS),
       "两页字段并集 = 全局字段总数（无遗漏/无重复）")

    # --- 参数校正验证（关键：不能出现 tdl 里不存在的 flag）---
    all_flags = set()
    for c in COMMANDS:
        for g in c.get("groups", []):
            for f in g.get("fields", []):
                fl = f.get("flag", "")
                if fl:
                    for p in fl.split(","):
                        p = p.strip()
                        if p.startswith("--"):
                            all_flags.add(p)
    for f in GLOBAL_FIELDS:
        fl = f.get("flag", "")
        if fl:
            for p in fl.split(","):
                p = p.strip()
                if p.startswith("--"):
                    all_flags.add(p)

    # tdl v0.20.4 全量合法参数（人工从 --help 抄录）
    VALID = {
        # global
        "--debug", "--delay", "--disable-progress-ps", "--limit", "--ns",
        "--ntp", "--pool", "--proxy", "--reconnect-timeout", "--storage",
        "--threads", "--help",
        # login
        "--desktop", "--passcode", "--type",
        # download
        "--continue", "--desc", "--dir", "--exclude", "--file", "--group",
        "--include", "--port", "--restart", "--rewrite-ext", "--serve",
        "--skip-same", "--takeout", "--template", "--url",
        # upload
        "--caption", "--chat", "--path", "--photo", "--rm", "--to", "--topic",
        # forward
        "--dry-run", "--edit", "--from", "--mode", "--silent", "--single",
        # chat export / ls / users
        "--all", "--filter", "--input", "--output", "--raw", "--reply",
        "--with-content",
        # migrate
        # (--to 已在上面)
        # extension
        "--force",
        # update
        "--check",
    }
    bad_flags = sorted(fl for fl in all_flags if fl not in VALID)
    ok(not bad_flags,
       f"全部 flag 均存在于 tdl v0.20.4（越界项：{bad_flags or '无'}）")

    # --- 控件构造 ---
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        from app.widgets import HubTab, SubTab, SubTabBar, NavItem, GroupBox
        from app.form_renderer import Segmented, FieldRow, FormRenderer
        from app.log_panel import LogPanel, ActionBar
        from app.runner import build_argv, LineBuffer, strip_ansi, split_flag

        ic = app_icon()
        ok(not ic.isNull() and len(ic.availableSizes()) == len(APP_ICON_SIZES),
           f"应用图标可用（{len(ic.availableSizes())} 个尺寸）")

        t = HubTab("下载", "download")
        ok(t.height() == 38, f"HubTab 高度 {t.height()} = 38")
        st = SubTab("保存设置", "save")
        ok(st.height() == 33, f"SubTab 高度 {st.height()} = 33")
        ni = NavItem("任务", "list", sub="下载 / 上传 / 转发")
        ok(ni.height() == 32, f"NavItem 高度 {ni.height()} = 32")

        sg = Segmented([dict(v="a", l="链接下载"), dict(v="b", l="JSON")], "a")
        ok(sg.height() == 32, f"Segmented 高度 {sg.height()} = 32")
        ok(sg.value() == "a", "Segmented 初值正确")

        lp = LogPanel()
        lp.out("测试输出"); lp.err("测试错误"); lp.sys("系统消息")
        ok("测试输出" in lp.view.toPlainText(), "LogPanel 可写入输出")

        ab = ActionBar()
        ab.set_cmd(["download", "--url", "x"], "D:/bin/tdl.exe")
        ok("tdl.exe" in ab.cmd_label.text(), "ActionBar 命令预览含可执行名")

        # --- 命令拼装回归测试 ---
        ok(split_flag("-d, --dir") == "--dir", "split_flag 优先取长选项")
        ok(split_flag("--from") == "--from", "split_flag 处理单长选项")

        lb = LineBuffer()
        got = lb.feed("A\n10%\r90%\nB\n")
        ok(got == ["A", "90%", "B"], f"LineBuffer 正确折叠 \\r 进度条 -> {got}")

        ok(strip_ansi("\x1b[32m绿\x1b[0m") == "绿", "strip_ansi 清洗颜色")

        # download 链接模式
        argv = build_argv(
            CMD_BY_ID["dl"],
            {"srcMode": "url", "url": ["https://t.me/a/1", "https://t.me/a/2"],
             "group": True, "dir": "D:/dl", "skipSame": True, "continue": False,
             "restart": False, "desc": False, "rewriteExt": False, "template": "t"},
            {"__ns": "default", "__threads": 4, "__limit": 2, "__pool": 8,
             "__delay": "0", "__ntp": "", "__reconn": "5m"},
        )
        ok(argv[0] == "download", f"下载命令首词 = download")
        ok(argv.count("--url") == 2, "多条 --url 正确展开")
        ok("--group" in argv, "布尔开关正确生成")
        ok("--continue" not in argv, "False 开关不生成")
        ok(argv.count("--ns") == 1, "全局参数注入一次")

        # extension 位置参数
        argv2 = build_argv(
            CMD_BY_ID["extension"],
            {"action": "install", "name": "github.com/x/y", "force": True},
            {},
        )
        ok(argv2[:3] == ["extension", "install", "github.com/x/y"],
           f"extension 位置参数正确 -> {argv2[:3]}")

        # upload
        argv3 = build_argv(
            CMD_BY_ID["up"],
            {"chat": "-1001", "path": ["D:/a.jpg"], "photo": True, "rm": False,
             "topic": 0, "include": [], "exclude": [], "caption": ""},
            {"__ns": "default"},
        )
        ok(argv3[0] == "upload" and "--chat" in argv3 and "--photo" in argv3,
           "上传命令拼装正确")
        ok("--topic" not in argv3, "数字 0 不生成参数")

        # --- 主窗口 ---
        from app.main_window import MainWindow
        w = MainWindow(tdl_path=DEFAULT_TDL_PATH)
        ok(w.sidebar.width() == 236, f"侧边栏宽度 {w.sidebar.width()} = 236")
        ok(w.titlebar.height() == 44, f"顶栏高度 {w.titlebar.height()} = 44")
        ok(w.statusbar.height() == 28, f"状态栏高度 {w.statusbar.height()} = 28")
        ok(len(w.sidebar._items) == len(NAV_GROUPS),
           f"侧边栏导航项 {len(w.sidebar._items)} 个 = NAV_GROUPS（{len(NAV_GROUPS)}）")

        for g in NAV_GROUPS:
            try:
                w.on_nav(g["cmd_id"])
                oks.append(f"路由到 {g['id']} 正常")
            except Exception as e:
                import traceback
                fails.append(f"路由到 {g['id']} 失败: {e}\n{traceback.format_exc()}")

        # 逐命令渲染测试
        render_fail = 0
        for c in COMMANDS:
            if c.get("virtual"):
                continue
            try:
                w.render_cmd(c)
            except Exception as e:
                render_fail += 1
                fails.append(f"渲染 {c['id']} 失败: {e}")
        ok(render_fail == 0, f"全部 {len([c for c in COMMANDS if not c.get('virtual')])} 个命令页渲染成功")

    except Exception as e:
        import traceback
        fails.append(f"控件构造失败: {e}\n{traceback.format_exc()}")

    # --- tdl 可执行文件 ---
    ok(os.path.isfile(DEFAULT_TDL_PATH), f"tdl.exe 存在：{DEFAULT_TDL_PATH}")
    try:
        from app.runner import TdlRunner
        r = TdlRunner(DEFAULT_TDL_PATH)
        okv, text = r.probe_version()
        ok(okv, f"tdl version 探测成功 -> {text.splitlines()[0] if text else ''}")
    except Exception as e:
        fails.append(f"tdl 探测异常: {e}")

    # --- 输出 ---
    # ⚠️ 打包成 --windowed exe 后没有控制台：stdout 可能是 GBK 编码的无效句柄，
    #    直接 print("✓ …") 会抛 UnicodeEncodeError 让自检**假失败**（退出码 1）。
    #    所以统一走 _emit：容错打印 + 同时把报告写成 UTF-8 文件供查看。
    lines = ["=" * 64, "  tdl GUI Qt 客户端 —— 无头自检", "=" * 64]
    lines += [f"  ✓ {m}" for m in oks]
    if fails:
        lines.append("")
        lines += [f"  ✗ {m}" for m in fails]
    lines += ["-" * 64, f"  通过 {len(oks)} / 失败 {len(fails)}", "=" * 64]
    _emit("\n".join(lines))
    return 1 if fails else 0


def _emit(text: str) -> None:
    """输出自检报告：容错打印 + 落盘 probe-report.txt（UTF-8）。

    windowed exe 的 stdout 无效/编码为 GBK，print 会抛异常；报告落盘后
    用户在软件目录里能直接看到结果，`--probe` 的**退出码**仍是权威判据。
    """
    try:
        out = getattr(sys, "stdout", None)
        if out is not None:
            try:
                out.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                             # noqa: BLE001
                pass
            print(text)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from app.paths import app_root
        (app_root() / "probe-report.txt").write_text(text, encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass


def main() -> int:
    tdl_path = None
    for i, a in enumerate(sys.argv):
        if a == "--tdl" and i + 1 < len(sys.argv):
            tdl_path = sys.argv[i + 1]

    if "--probe" in sys.argv:
        return _probe()

    # 资源目录结构（resources/json、resources/dl）——启动即建，幂等
    from app.paths import ensure_dirs
    ensure_dirs()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    from PySide6.QtCore import Qt
    from app.theme import QSS
    from app.data import DEFAULT_TDL_PATH

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # Windows 任务栏归属：不设的话 pythonw 启动的窗口在任务栏上会并到
    # Python 的图标分组里，自定义图标显示不出来（必须在建 QApplication 前调）
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "tdlgui.client.1")
        except Exception:                              # pragma: no cover
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("tdl GUI")
    app.setStyleSheet(QSS)

    # ---- 单实例保护 ----
    # tdl 的账号数据（~/.tdl/data/<ns>）是 bbolt 库，**同一时刻只允许一个进程
    # 打开**（排他文件锁）。所以开第二个客户端必然互相抢锁 —— 表现就是那句
    # "Current database is used by another process, please terminate it first"。
    # 这里直接拦住第二次启动（常见场景：窗口已最小化到托盘，用户又双击了一次图标）。
    from app.procutil import acquire_single_instance
    if not acquire_single_instance():
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            None, "tdl GUI 已在运行",
            "检测到本程序已经有一个实例在运行（可能已最小化到系统托盘）。\n\n"
            "同一时间只能运行一个客户端：tdl 的账号数据库同一时刻只允许一个\n"
            "进程使用，多开会互相抢锁，导致命令起不来。\n\n"
            "请到任务栏右下角的托盘区找到它的图标（双击可恢复窗口）继续使用。")
        return 0

    from app.icons import app_icon
    app.setWindowIcon(app_icon())                      # 全局默认图标（对话框继承）

    # 关闭「鼠标停留弹出悬浮提示」：tooltip 会遮挡卡片/输入框内容，统一禁用
    from app.widgets import install_no_tooltip, install_cursor_rules
    install_no_tooltip(app)

    # 光标规则（全应用，含后续动态创建的控件）：
    #   可点击 → 手型 ｜ 文本栏 → 文本编辑光标 ｜ 其余 → 普通箭头
    install_cursor_rules(app)

    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    from app.main_window import MainWindow
    w = MainWindow(tdl_path=tdl_path or DEFAULT_TDL_PATH)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
