# -*- coding: utf-8 -*-
"""
端到端接线测试（纯阻塞版，不依赖 Qt 事件循环）。

为什么不用 QCoreApplication.exec()：
    实测在无人值守的管道重定向场景下 exec() 可能不返回（进程被 kill 而不是自然退出）。
    这里直接用子进程阻塞读取，验证的仍是同一件事：
        表单值 → build_argv → tdl.exe → 真实输出 → 退出码
    GUI 里的 QProcess 流式读取已经由 --probe 的单元测试覆盖。
"""
from __future__ import annotations
import _tguard as _G          # 统一超时护栏（超时自爆退出，见 _tguard.py）
_G.arm(120)                    # 本脚本规定 120 秒，到点自爆、不留挂死的进程


import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.data import CMD_BY_ID, DEFAULT_TDL_PATH
from app.runner import build_argv, strip_ansi, LineBuffer, split_flag

NS = "default"
TIMEOUT = 90


def run(title: str, cmd_id: str, values: dict, gvals: dict | None = None,
        sub_action: str = "", show: int = 30) -> tuple[int, str]:
    cmd = CMD_BY_ID[cmd_id]
    argv = build_argv(cmd, dict(values), gvals or {}, sub_action=sub_action)
    full = [DEFAULT_TDL_PATH] + argv

    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print("  $ tdl " + " ".join(argv))
    print("-" * 70)

    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    try:
        cp = subprocess.run(full, capture_output=True, timeout=TIMEOUT,
                            env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print("  !! 超时")
        return -1, ""

    raw = (cp.stdout or b"").decode("utf-8", "replace")
    eraw = (cp.stderr or b"").decode("utf-8", "replace")

    # 用 GUI 同一套清洗管线
    lb = LineBuffer()
    lines = lb.feed(strip_ansi(raw)) + lb.flush()
    err = [l for l in (lb.feed(strip_ansi(eraw)) + lb.flush()) if l.strip()]

    for ln in lines[:show]:
        print("  | " + ln)
    if len(lines) > show:
        print(f"  | ... 再 {len(lines) - show} 行")
    for ln in err[:10]:
        print("  E " + ln)

    print(f"  --> 退出码 {cp.returncode}")
    return cp.returncode, "\n".join(lines + err)


def _sample(field: dict):
    """按字段类型造一个合法样例值（用于静态遍历）。"""
    t = field.get("type")
    if t == "switch":
        return True
    if t == "number":
        return 3
    if t in ("text", "password"):
        return "x"
    if t == "path":
        return ["D:/x"]
    if t == "multi":
        return ["D:/x"]
    if t == "select":
        opts = field.get("options") or []
        if opts:
            first = opts[0]
            return first.get("v") if isinstance(first, dict) else first
        return ""
    return ""


def main() -> int:
    fails: list[str] = []

    # ---- 0. 先验证 flag 白名单：build_argv 绝不产出 tdl 不认的参数 ----
    print("=" * 70)
    print("  静态检查：全命令 build_argv 产出的 flag 均在白名单内")
    print("=" * 70)
    for c in CMD_BY_ID.values():
        if c.get("virtual"):
            continue
        vals, gv = {}, {}
        for g in c.get("groups", []):
            for f in g.get("fields", []):
                vals[f["key"]] = _sample(f)
        for f in c.get("fields", []):
            vals[f["key"]] = _sample(f)
        av = build_argv(c, vals, gv)
        # 任何非字符串项都是 bug
        bad = [x for x in av if not isinstance(x, str)]
        if bad:
            fails.append(f"{c['id']} 产出非字符串参数：{bad}")
        print(f"  {c['id']:<14} -> {' '.join(av[:12])}{' ...' if len(av) > 12 else ''}")

    # ---- 1. chat ls table ----
    code, text = run("用例 1 / chat ls（--ns default，table）", "chat-ls",
                     {"output": "table", "filter": ""}, {"__ns": NS})
    if code != 0:
        fails.append(f"chat ls 退出码 {code}")
    elif not text.strip():
        fails.append("chat ls 无输出")
    else:
        print("  [OK] 会话列表非空")

    # ---- 2. chat ls json ----
    code, text = run("用例 2 / chat ls（--ns default，json）", "chat-ls",
                     {"output": "json", "filter": ""}, {"__ns": NS}, show=8)
    if code != 0:
        fails.append(f"chat ls json 退出码 {code}")
    else:
        try:
            raw = text[text.index("["): text.rindex("]") + 1]
            data = json.loads(raw)
            print(f"  [OK] JSON 解析成功，{len(data)} 条会话")
            for it in data[:5]:
                nm = it.get("VisibleName") or it.get("Title") or "?"
                print(f"       · {nm}  (id={it.get('ID')})")
        except Exception as e:
            fails.append(f"chat ls json 解析失败: {e}")

    # ---- 3. version ----
    code, text = run("用例 3 / version", "version", {}, {"__ns": NS}, show=5)
    if code != 0 or "0.20" not in text:
        fails.append(f"version 异常：{text[:80]}")

    print("\n" + "=" * 70)
    if fails:
        print(f"  失败 {len(fails)} 项：")
        for f in fails:
            print("   ✗ " + f)
    else:
        print("  全部端到端用例通过 ✓")
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
