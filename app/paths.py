# -*- coding: utf-8 -*-
"""
软件目录结构（对照 HTML 原型的约定，1:1 对齐）。

结构（所有产物都只在软件目录内）：
    <APP>/
      main.py ...
      resources/                  ← 资源文件夹
        json/                     ← 下载 json（chat export 产物，供 download -f 用）
        dl/                       ← 下载根目录（下载的文件）
          <缩写>-<群号>/          ← 导出 json 时新建的群组文件夹
            <缩写>-<群号>-message.json   ← 消息 json 放这里

命名规则（全部英文）：
    缩写 = pinyinInitials(群名)：中文取声母（内置常用字表，查不到保留原字）、
           英文/数字小写保留、空格标点 emoji 丢弃
    群号 = 去掉 -100 前缀的正数
    群组文件夹   = <缩写>-<群号>
    下载 json    = resources/json/<缩写>-<群号>.json
    消息 json    = resources/dl/<缩写>-<群号>/<缩写>-<群号>-message.json
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

RES_DIR = "resources"
JSON_DIR = "json"     # 下载 json 存放处
DL_DIR = "dl"         # 下载根目录

# 拼音首字母常用字表（与 HTML 原型 PINYIN_INITIAL 一致，可按需扩充）
PINYIN_INITIAL = {
    "张": "z", "三": "s", "李": "l", "四": "s",
    "科": "k", "技": "j", "资": "z", "讯": "x", "频": "p", "道": "d",
    "摸": "m", "鱼": "y", "群": "q",
    "源": "y", "分": "f", "享": "x", "站": "z",
    "开": "k", "发": "f", "者": "z", "交": "j", "流": "l",
    "涂": "t", "狗": "g",
    "大": "d", "小": "x", "中": "z", "国": "g", "网": "w", "友": "y",
    "影": "y", "视": "s", "音": "y", "乐": "l", "游": "y", "戏": "x",
    "学": "x", "习": "x", "工": "g", "作": "z", "生": "s", "活": "h",
    "新": "x", "闻": "w", "财": "c", "经": "j", "股": "g", "票": "p",
    "房": "f", "地": "d", "产": "c", "汽": "q", "车": "c", "数": "s", "码": "m",
    # 常见群名高频字补充
    "软": "r", "件": "j", "讨": "t", "论": "l", "电": "d", "子": "z",
    "书": "s", "库": "k", "小": "x", "说": "s", "日": "r", "常": "c",
    "免": "m", "费": "f", "资": "z", "源": "y", "分": "f", "享": "x",
    "正": "z", "能": "n", "量": "l", "精": "j", "神": "s", "妹": "m",
    "混": "h", "乱": "l", "的": "d", "生": "s", "活": "h",
}


def app_root() -> Path:
    """软件根目录：打包 exe 后用 exe 所在目录，源码跑用 main.py 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def res_root() -> Path:
    return app_root() / RES_DIR


def json_root() -> Path:
    return res_root() / JSON_DIR


def dl_root() -> Path:
    return res_root() / DL_DIR


def ensure_dirs() -> None:
    """启动时创建资源目录（幂等）。"""
    for p in (res_root(), json_root(), dl_root()):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def dl_root_str() -> str:
    return str(dl_root()).replace("\\", "/")


# ============================================================ 命名规则
def pinyin_initials(name: str) -> str:
    """取名称的拼音首字母（中文取声母，英文/数字原样小写，去掉空格与符号）。"""
    out = ""
    for ch in str(name or ""):
        if re.match(r"[\u4e00-\u9fa5]", ch):
            out += PINYIN_INITIAL.get(ch, ch)
        elif re.match(r"[A-Za-z0-9]", ch):
            out += ch.lower()
        # 其余字符（空格、标点、emoji）直接丢弃
    return out or "chat"


def chat_no(chat_id) -> str:
    """群号：去掉 -100 前缀的正数。"""
    return re.sub(r"^-100", "", re.sub(r"^-", "", str(chat_id or "")))


# Telegram 消息链接（t.me 形式，含私有群 c/ 与公开 username 两种）
_RE_TG_C = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/c/(?P<c>\d{5,})(?:/\d+)?/?(?:\?\S*)?$",
    re.IGNORECASE)
_RE_TG_U = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(?P<u>[A-Za-z][A-Za-z0-9_]{3,})"
    r"(?:/\d+)?/?(?:\?\S*)?$",
    re.IGNORECASE)


def tg_chat_tag(url: str) -> str:
    """从 Telegram 消息链接里取「群组标识」（用于自动生成下载子目录）。

    · 私有群/频道  https://t.me/c/1539539150/95292  → `1539539150`（链接里的群号）
    · 公开群/频道  https://t.me/example_group/123 → `example_group`
      （公开链接里没有数字群号，直接沿用 username 创建文件夹 —— 纯英文，
        与命名规则兼容，也不必异步去查会话换算 ID）
    · 带 www. / 无协议 / 尾斜杠 / 尾部 query（?single、?comment=…）都能吃
    解析不出（tg:// 链接、非 t.me 地址、空行）返回空串。
    """
    s = str(url or "").strip()
    if not s:
        return ""
    m = _RE_TG_C.match(s)
    if m:
        return m.group("c")
    m = _RE_TG_U.match(s)
    if m:
        return m.group("u")
    return ""


def chat_no_key(chat_id) -> str:
    """归一化 key（用于匹配）：只留数字部分。"""
    return re.sub(r"\D", "", str(chat_id or ""))


def anchor_dir_name(chat_name: str, chat_id) -> str:
    """群组锚定文件夹名：<缩写>-<群号>（纯英文，跨平台安全）。"""
    return f"{pinyin_initials(chat_name)}-{chat_no(chat_id)}"


def dl_json_path(chat_name: str, chat_id) -> Path:
    """下载 json 的完整路径：resources/json/<缩写>-<群号>.json"""
    return json_root() / f"{anchor_dir_name(chat_name, chat_id)}.json"


def msg_json_path(chat_name: str, chat_id) -> Path:
    """消息 json 的完整路径：
    resources/dl/<缩写>-<群号>/<缩写>-<群号>-message.json
    """
    d = dl_root() / anchor_dir_name(chat_name, chat_id)
    return d / f"{anchor_dir_name(chat_name, chat_id)}-message.json"


def msg_json_part_path(chat_name: str, chat_id) -> Path:
    """消息 json 的**导出临时文件**：`<锚定名>-message.part.json`。

    为什么要它（而不是让 tdl 直接写正式文件）：
      · 同一个群会分多次导出（不同区间），直接写会**覆盖丢掉**上一次的内容；
      · 导出中途被打断也会把正式文件截断（原来会留下一个坏 json）。
    改成「tdl 写临时文件 → 成功后与既有文件合并 → 替换正式文件」，
    上面两个问题一起解决；临时文件用完即删，不会留在目录里。
    """
    p = msg_json_path(chat_name, chat_id)
    return p.with_name(f"{p.stem}.part.json")


def merge_export_json(target, incoming) -> tuple[int, int]:
    """把本次导出（incoming）合并进既有文件（target）。

    规则：
      · 按消息 **id 去重**，同 id 以本次为准（重导同一区间 → 用最新那条）
      · 结果按 **id 升序**（与消息浏览的排序一致）
      · 顶层其它字段（如会话 id）沿用既有文件，本次的非 messages 字段覆盖上去
      · target 不存在 / 不可解析（截断、格式错）→ 直接用本次内容

    返回 `(合并后条数, 本次新增条数)`；incoming 不可用时抛 ValueError。
    写盘用「临时文件 + os.replace」原子替换，避免写一半把正式文件弄坏。
    """
    import json as _json

    def _read(p):
        try:
            d = _json.loads(Path(p).read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        msgs = d.get("messages") if isinstance(d, dict) else d
        if not isinstance(msgs, list):
            return None
        return d, [m for m in msgs if isinstance(m, dict)]

    inc = _read(incoming)
    if inc is None:
        raise ValueError("本次导出的文件不可用（缺失 / 截断 / 格式错误）")
    inc_data, inc_msgs = inc
    old = _read(target)
    old_data, old_msgs = (old if old else ({}, []))

    def _key(m: dict):
        try:
            return (0, int(m.get("id")))
        except (TypeError, ValueError):
            return (1, 0)                     # 非数字 id 排最后

    by_id: dict[str, dict] = {}
    for m in old_msgs:
        by_id[str(m.get("id"))] = m
    added = 0
    for m in inc_msgs:
        k = str(m.get("id"))
        if k not in by_id:
            added += 1
        by_id[k] = m                          # 同 id：本次为准
    merged = sorted(by_id.values(), key=_key)

    data = dict(old_data) if isinstance(old_data, dict) else {}
    if isinstance(inc_data, dict):
        data.update({k: v for k, v in inc_data.items() if k != "messages"})
    data["messages"] = merged

    tp = Path(target)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tmp = tp.with_name(tp.name + ".writing")
    tmp.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, tp)
    return len(merged), added


# ============================================================ 会话匹配
def find_chat(chats: list[dict], key: str) -> dict | None:
    """在 chat ls -o json 的结果里查会话（id 精确 / username / 归一化群号）。"""
    key = str(key or "").strip().lstrip("@")
    if not key:
        return None
    nkey = chat_no_key(key)
    for c in chats:
        if str(c.get("id", "")) == key or str(c.get("username", "")).lstrip("@") == key:
            return c
    for c in chats:
        if chat_no_key(c.get("id")) == nkey and nkey:
            return c
    return None


def inspect_export_json(path) -> tuple[bool, str]:
    """检查 tdl 导出的 JSON 是否可用。返回 (ok, 说明)。

    把「导出被中断 → 文件截断」与「格式非法」分开报，便于用户判断该不该
    重新导出（截断的 json 会让 download -f 解析中断，表现为下载 0 字节）。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return False, f"读不到文件（{e}）"
    return inspect_export_raw(raw)


def inspect_export_raw(raw: bytes) -> tuple[bool, str]:
    """`inspect_export_json` 的字节版（已有内容时避免重复读盘）。"""
    import json as _json
    if not raw.strip():
        return False, "文件是空的"
    try:
        _json.loads(raw.decode("utf-8"))
        return True, ""
    except UnicodeDecodeError:
        return False, "不是 UTF-8 文本"
    except _json.JSONDecodeError as e:
        tail = raw.rstrip()[-1:]
        if tail not in (b"}", b"]"):
            return False, ("文件不完整 —— 末尾没有收尾符号，说明导出过程被中断"
                           "（终止 / 关窗 / 进程被杀）。请重新导出。")
        return False, f"JSON 格式错误（第 {e.lineno} 行第 {e.colno} 列）"


def export_id_files(path) -> dict:
    """从 tdl 导出的「下载源 JSON」里取出 {消息 ID: 文件名}。

    为什么要它：
      · 进度行的 message 被 tdl 截断到 30 字符，群名一长 `(群号):消息号` 就被切掉，
        序号只剩前几位（如 1759495254 → 175）；完整 ID 只能从下载源里取。
      · 进度行里**根本没有文件扩展名**，而用户要看「这是 jpg 还是 mp4」，
        也只能从下载源的文件名里取。

    JSON 里 `messages[].file` 是文件名字符串（实测形如 `IMG_1542.MOV`）。
    读不到 / 格式不对时返回空 dict（不抛异常 —— 调用方只当"没有可用信息"）。
    """
    import json as _json
    try:
        data = _json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return {}
    out = {}
    for m in msgs:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        out[str(m["id"])] = str(m.get("file") or "").strip()
    return out


def export_message_ids(path) -> list:
    """（保留）只要消息 ID 列表；文件名映射见 export_id_files。"""
    return list(export_id_files(path))


def sort_id_pool(pool, desc: bool = False) -> list:
    """把「消息号池」排成 tdl 的**实际下载顺序**。

    为什么要排：
      tdl 的 app/dl/iter.go:sortDialogs 在下发任务前会强制按消息号**数值**排序
      （无 --desc → 升序；带 --desc → 降序），而官方客户端导出的下载 JSON
      **通常是降序**（最新在前，实测 695 条严格降序）。GUI 若直接按 JSON 顺序
      做「第 N 个任务 = 池里第 N 个号」的分派，就会与 tdl 首尾颠倒 ——
      实测表现：磁盘 tmp 是 95314~95318，卡片却显示 95396~95399。
    规则与 tdl 一致：去重后按整数升序（desc=True 则降序）；非数字项原样排在末尾。
    """
    nums, others = [], []
    for x in pool or []:
        s = str(x).strip()
        if not s:
            continue
        (nums if s.isdigit() else others).append(s)
    return (sorted(set(nums), key=int, reverse=desc)
            + sorted(set(others)))


# ============================================================ 命名空间
def tdl_data_dir() -> Path:
    """tdl 的命名空间数据目录（每个命名空间一个 bolt 库文件）。"""
    return Path.home() / ".tdl" / "data"


def ns_list() -> list[str]:
    """本机已有的命名空间：读 `~/.tdl/data/` 下的库文件名。

    该目录里**每个命名空间就是一个文件**，所以文件名即命名空间名 ——
    这样用户新登录一个命名空间后，顶栏下拉里会自动出现，不用改代码。
    读不到目录时回退到常见值，保证下拉列表非空。
    """
    names: list[str] = []
    try:
        names = sorted(p.name for p in tdl_data_dir().iterdir() if p.is_file())
    except OSError:
        names = []
    for n in ("default", "tn"):
        if n not in names:
            names.append(n)
    return names
