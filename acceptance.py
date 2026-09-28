# -*- coding: utf-8 -*-
"""
验收测试：用 GUI 的 argv 拼装逻辑，下载指定群前 100 条消息的全部附件。
"""
from __future__ import annotations

import os
import sys
import json
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.data import CMD_BY_ID, DEFAULT_TDL_PATH
from app.runner import build_argv

CHAT = "example_group"
EXPORT = "./_test/export.json"
OUTDIR = "./_test/dl"

msgs = json.load(open(EXPORT, encoding="utf-8"))["messages"]
urls = [f"https://t.me/{CHAT}/{m['id']}" for m in msgs]
print(f"待下载消息 {len(urls)} 条")

# 完全走 GUI 的表单值 → build_argv 通路
values = {
    "url": urls,
    "file": [],
    "group": False,
    "dir": [OUTDIR],
    "template": "{{ .DialogID }}_{{ .MessageID }}_{{ .FileName }}",
    "skip_same": False,
    "continue": False,
    "restart": False,
    "rewrite_ext": False,
    "desc": False,
    "takeout": False,
    "serve": "",
    "port": 0,
    "include": [],
    "exclude": [],
}
gvals = {"__ns": "default", "__threads": 4, "__limit": 2, "__pool": 8,
         "__delay": "0", "__reconn": "5m"}

cmd = CMD_BY_ID["dl"]
argv = build_argv(cmd, values, gvals)
print("argv 长度:", len(argv))
print("首 8 项:", argv[:8])

os.makedirs(OUTDIR, exist_ok=True)
env = dict(os.environ)
env.setdefault("NO_COLOR", "1")
t0 = time.time()
cp = subprocess.run([DEFAULT_TDL_PATH] + argv, capture_output=True,
                    timeout=600, env=env, stdin=subprocess.DEVNULL)
el = time.time() - t0

raw = (cp.stdout or b"").decode("utf-8", "replace")
eraw = (cp.stderr or b"").decode("utf-8", "replace")
tail = [l for l in raw.splitlines() if l.strip()][-6:]
print("\n--- 输出尾部 ---")
for l in tail:
    print("  | " + l)
if eraw.strip():
    print("--- stderr ---")
    for l in eraw.strip().splitlines()[:8]:
        print("  E " + l)

print(f"\n退出码 {cp.returncode}，耗时 {el:.1f}s")
files = os.listdir(OUTDIR)
total = sum(os.path.getsize(os.path.join(OUTDIR, f)) for f in files)
print(f"落地文件 {len(files)} 个，共 {total/1024/1024:.2f} MB")
for f in sorted(files)[:5]:
    print("   ·", f)
