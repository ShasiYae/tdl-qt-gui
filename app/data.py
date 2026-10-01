# -*- coding: utf-8 -*-
"""
命令与字段定义 —— **以真实 tdl v0.20.4 的 --help 输出为准**。

与 HTML 原型的差异（原型是凭文档推测写的，有多处不准，此处已全部校正）：
    · 删除 `--verify` / `--size` / `--part-size`（tdl 无此参数）
    · upload 的目标会话是 `-c, --chat`（原型的 `--to` 是表达式路由，另一套）
    · forward 的 `--from` / `--to` 无短选项；`--mode` 只有 direct / clone
    · migrate 只有 `--to`（没有 --src / --dst）
    · extension 是**位置参数子命令**（list / install / remove / upgrade），不是 --action
    · chat ls 的 `-o, --output` 是 table/json 格式，不是文件路径
    · chat export 用 `-i, --input ints` 传区间，`-T, --type` 默认是 time

字段数据结构（每个 field 是一个 dict）：
    key        内部键名，也是 store 里的存储键
    flag       命令行开关，如 "-d, --dir"；空串表示不生成参数（仅 UI 用）
    label      显示名
    type       控件类型（见下方 FT_*）
    default    默认值
    ph         占位提示
    help       帮助文字
    req        是否必填
    options    select 的选项 [{v, l}]
    show_when  条件显示 {f: 依赖字段, in_: [值...]}
    scope      'args' | 'dl' —— 全局参数归属
    move_to    'dl' —— 值仍存 global_store，只改显示位置
    locked     是否只读
    multiple   是否允许多值
    remember   是否记住上次输入
"""
from __future__ import annotations

# ============================================================================
# 字段类型常量
# ============================================================================
FT_TEXT = "text"
FT_NUMBER = "number"
FT_SWITCH = "switch"
FT_SELECT = "select"
FT_PATH = "path"
FT_MULTI = "multi"
FT_PASSWORD = "password"
FT_LIST = "list"        # 逗号分隔的多值（对应 tdl 的 strings 类型）
FT_NOTE = "note"        # 纯说明文字块（无输入控件）


# ============================================================================
# 全局参数（对应 tdl 的 Global Flags，逐条核对 --help）
# ============================================================================
GLOBAL_FIELDS = [
    # ---- 全局通用（「设置 → 全局参数 → 参数设置」显示）----
    dict(key="__ns", flag="-n, --ns", label="命名空间", type=FT_TEXT,
         default="default", ph="default / tn",
         help="Telegram 会话命名空间，多账号切换用。",
         scope="args"),

    dict(key="__proxy", flag="--proxy", label="代理地址", type=FT_TEXT,
         default="", ph="socks5://user:pass@127.0.0.1:1080",
         help="格式：protocol://username:password@host:port", scope="args"),

    dict(key="__debug", flag="--debug", label="调试模式", type=FT_SWITCH,
         default=False, help="启用调试级日志输出", scope="args"),

    dict(key="__storage", flag="--storage", label="存储后端", type=FT_TEXT,
         default="", ph="type=bolt,path=C:\\Users\\你\\.tdl\\data",
         help="格式 type=driver,key1=value1,...；驱动可选 legacy / bolt / file。"
              "留空用 tdl 默认（bolt + ~/.tdl/data）", scope="args"),

    # ---- 以下 6 项下移到「任务 → 下载 → 参数设置」（move_to 只改显示位置）----
    # 默认值取自用户旧批处理脚本（download.bat）：threads=12 / limit=4 / pool=8 /
    # reconnect-timeout=0（不限制）；脚本里的 TDL_SIZE=524288 在 tdl v0.20.4
    # 已无对应参数，无法迁移。
    dict(key="__threads", flag="-t, --threads", label="单任务线程数", type=FT_NUMBER,
         default=12, help="单个文件传输用的最大线程数（tdl 默认 4，本机脚本用 12）",
         scope="args", move_to="dl"),
    dict(key="__limit", flag="-l, --limit", label="并发任务数", type=FT_NUMBER,
         default=4, help="同时进行的任务上限（tdl 默认 2，本机脚本用 4）",
         scope="args", move_to="dl"),
    dict(key="__pool", flag="--pool", label="DC 连接池大小", type=FT_NUMBER,
         default=8, help="Telegram DC 连接池大小，0 为不限制（tdl 默认 8，本机脚本用 8；"
                         "太大容易断连，不建议调高）",
         scope="args", move_to="dl"),
    dict(key="__delay", flag="--delay", label="任务间隔", type=FT_TEXT,
         default="0", ph="5s / 1m / 0",
         help="每个任务之间的延迟，0 为无延迟（Go duration 格式）",
         scope="args", move_to="dl"),
    dict(key="__ntp", flag="--ntp", label="NTP 服务器", type=FT_TEXT,
         default="", ph="ntp1.aliyun.com",
         help="NTP 服务器地址；留空则使用系统时间（脚本中注释未启用）",
         scope="args", move_to="dl"),
    dict(key="__reconn", flag="--reconnect-timeout", label="重连超时", type=FT_TEXT,
         default="0", ph="5m / 0",
         help="重连退避超时，0 为不限制（本机脚本用 0；tdl 默认 5m0s）",
         scope="args", move_to="dl"),

    # ---- 下载专用（归到「任务 → 下载 → 参数设置」）----
    # 注意：tdl 的 download 没有 --size / --part-size / --verify，已全部删除
    dict(key="__progress_ps", flag="--disable-progress-ps", label="禁用终端进度刷新",
         type=FT_SWITCH, default=False,
         help="关闭进度条的 PS 刷新。某些终端会显示异常；GUI 里建议勾上以获得干净输出",
         scope="dl"),
]


def global_args_fields() -> list[dict]:
    """「设置 → 全局参数 → 参数设置」展示项（排除已下移到下载页的）。"""
    return [f for f in GLOBAL_FIELDS if f.get("scope") == "args" and not f.get("move_to")]


def dl_param_fields() -> list[dict]:
    """「任务 → 下载 → 参数设置」展示项：下载专用 + 从全局下移来的。"""
    return [f for f in GLOBAL_FIELDS if f.get("scope") == "dl" or f.get("move_to") == "dl"]


# ============================================================================
# 下载范围（对应 tdl download 的 -i/--include 与 -e/--exclude）
# ============================================================================
DL_SCOPES = [
    dict(id="photo", name="图片", desc="群里的照片（jpg/png/webp）",
         exts=["jpg", "jpeg", "png", "webp"]),
    dict(id="video", name="视频", desc="mp4 / mkv / mov 等",
         exts=["mp4", "mkv", "mov", "avi", "webm"]),
    dict(id="audio", name="音频", desc="音乐与语音",
         exts=["mp3", "m4a", "ogg", "flac", "wav"]),
    dict(id="doc", name="文档", desc="pdf / docx / zip 等附件",
         exts=["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
               "zip", "rar", "7z", "txt"]),
]


# ============================================================================
# 命令表 —— 每条都对照 `tdl <cmd> --help` 的真实输出
# ============================================================================
COMMANDS: list[dict] = [
    # ================================================================ 登录
    dict(
        id="login", name="登录", icon="key", cmd="tdl login",
        desc="登录 Telegram 账号，获取会话",
        notice=dict(
            type="info",
            text="首次使用请先登录。推荐从 Telegram Desktop 导入会话，无需验证码。"
                 "扫码登录（qr）会在下方日志区直接显示二维码字符画。",
        ),
        groups=[
            dict(title="基本设置", fields=[
                dict(key="type", flag="-T, --type", label="登录方式", type=FT_SELECT,
                     default="desktop", req=True,
                     options=[
                         dict(v="desktop", l="desktop — 从桌面客户端导入会话（推荐）"),
                         dict(v="code", l="code — 手机号 + 验证码登录"),
                         dict(v="qr", l="qr — 扫码登录（终端字符画）"),
                     ],
                     help="选择后下方只显示相关参数"),
            ]),

            dict(title="桌面客户端参数",
                 show_when=dict(f="type", in_=["desktop"]),
                 fields=[
                     dict(key="desktop", flag="-d, --desktop", label="桌面客户端路径",
                          type=FT_PATH, default="", ph="留空自动探测",
                          help="Telegram Desktop 的 tdata 目录路径。"
                               "留空时 tdl 会自动搜索常见安装位置"),
                     dict(key="passcode", flag="-p, --passcode", label="客户端密码",
                          type=FT_PASSWORD, default="", ph="无密码则留空",
                          help="桌面客户端的本地锁屏密码"),
                 ]),

            dict(title="提示", fields=[
                dict(key="__login_tip", flag="", label="", type=FT_NOTE,
                     help="code 方式需要交互式输入手机号与验证码，"
                          "GUI 暂不支持交互输入，建议用 desktop 或 qr 方式。"),
            ]),
        ],
    ),

    # ================================================================ 下载
    dict(
        id="dl", name="下载", icon="download", cmd="tdl download", group="task",
        desc="从 Telegram 会话或消息链接下载文件",
        sub_tabs=[
            dict(id="save", name="保存设置", icon="save"),
            dict(id="params", name="参数设置", icon="sliders"),
        ],
        groups=[
            # ---------- 常驻组：任何子选项卡下都可见 ----------
            dict(title="下载来源",
                 switchable=dict(
                     key="srcMode", default="json",
                     # 选项顺序 = 用户指定的优先级：JSON 文件下载 → 导出 JSON → 链接下载
                     options=[
                         dict(v="json", l="JSON 文件下载", hint="用官方客户端导出的聊天记录"),
                         dict(v="export", l="导出 JSON", hint="先把我群导出成 json，不下载文件"),
                         dict(v="url", l="链接下载", hint="粘贴 Telegram 消息链接"),
                     ],
                 ),
                 fields=[
                     dict(key="url", flag="-u, --url", label="消息链接", type=FT_MULTI,
                          default=[], show_when=dict(f="srcMode", in_=["url"]),
                          remember=True, ph="https://t.me/频道/123",
                          help="Telegram 消息链接，支持多条（每行一条）。"
                               "对应 tdl download -u\n"
                               "粘贴链接后下载目录自动按链接里的群组生成：\n"
                               "私有群 t.me/c/<群号>/… → resources/dl/<群号>/；"
                               "公开群 t.me/<username>/… → resources/dl/<username>/"
                               "（取第一条可识别的链接；手动改过目录后同一条链接不会反复覆盖）"),
                     dict(key="file", flag="-f, --file", label="导出文件路径",
                          type=FT_PATH, pick="file", default=[], pick_from="json",
                          show_when=dict(f="srcMode", in_=["json"]),
                          remember=True, ph="C:/Users/你/Downloads/result.json",
                          help="官方客户端导出的聊天记录 JSON（点「浏览」选择）。"
                               "对应 tdl download -f"),
                     dict(key="group", flag="--group", label="自动下载整组消息",
                          type=FT_SWITCH, default=False, show_when=dict(f="srcMode", in_=["url", "json"]),
                          help="媒体组（相册）整体下载"),
                 ]),

            # ---------- 常驻组：群组导出（仅「导出 JSON」模式显示）----------
            # 功能定义：导出群组的「下载 json」（媒体消息）与「消息 json」
            # （--all 完整记录）到指定文件夹；文件名自动 = <群号>.json / <群号>-all.json
            dict(title="群组导出（仅导出 JSON，不下载文件）",
                 fields=[
                     dict(key="ex_chat", flag="-c, --chat", label="会话 ID / 群号",
                          type=FT_TEXT, default="", req=True,
                          show_when=dict(f="srcMode", in_=["export"]),
                          ph="-1001234567895 或 1234567895",
                          help="群名会自动查出，用于生成 json 文件名。"
                               "对应 tdl chat export -c"),
                     dict(key="ex_type", flag="-T, --type", label="导出类型",
                          type=FT_SELECT, default="last",
                          show_when=dict(f="srcMode", in_=["export"]),
                          options=[
                              dict(v="last", l="last — 最近 N 条（填 1 个条数）"),
                              dict(v="id", l="id — 按消息 ID 区间（填 2 个 ID：起,止）"),
                              dict(v="time", l="time — 按时间区间（填 2 个 Unix 时间戳：起,止）"),
                          ],
                          help="决定下方区间参数怎么填；类型与填法不匹配时 tdl 会静默导出空文件"),
                     dict(key="ex_input", flag="-i, --input", label="区间参数",
                          type=FT_LIST, default=["100"],
                          show_when=dict(f="srcMode", in_=["export"]),
                          ph="如 100（最近 100 条）",
                     help="last 填 1 个条数（如 100）；id 填 2 个真实消息编号"
                          "（如 140149,140937——编号可在「消息浏览」页点 #编号 复制）；"
                          "time 填 2 个 Unix 秒级时间戳（不支持日期文字）。逗号分隔"),
                     dict(key="ex_all", flag="--all", label="同时导出消息 json",
                          type=FT_SWITCH, default=True,
                          show_when=dict(f="srcMode", in_=["export"]),
                          help="**默认开启**：导完下载 json 后自动再导出一份群组的完整消息记录"
                               "（含纯文本）到 resources/dl/<群名缩写>-<群号>/。"
                               "不需要时可关掉，只出下载 json"),
                     dict(key="ex_note", type=FT_NOTE, flag="", label="",
                          show_when=dict(f="srcMode", in_=["export"]),
                          help="导出位置自动管理（全部在软件目录内）：\n"
                               "① 下载 json → resources/json/<群名缩写>-<群号>.json\n"
                               "② 消息 json → resources/dl/<群名缩写>-<群号>/"
                               "<群名缩写>-<群号>-message.json（默认一并导出，群组文件夹自动创建；"
                               "分多次导出会按消息 id 合并进同一份）\n"
                               "群名缩写由软件自动查询群组名称后生成（拼音/英文首字母，纯英文）"),
                 ]),

            # ---------- 子选项卡内容 ----------
            dict(title="", pane="save", fields=[
                dict(key="dir", flag="-d, --dir", label="下载目录", type=FT_PATH,
                     default=None, remember=True,
                     help="文件保存目录，不存在会自动创建。默认在软件目录内："
                          "resources/dl（启动时自动填充）"),
                # ↑ default 由 _defaults() 动态填 paths.dl_root_str()（见 main_window）

                dict(key="template", flag="--template", label="文件名模板", type=FT_TEXT,
                     default="{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}",
                     ph="{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}",
                     help="Go 模板语法。可用字段：DialogID / MessageID / FileName 等"),

                dict(key="skipSame", flag="--skip-same", label="跳过同名同大小文件",
                     type=FT_SWITCH, default=True,
                     help="文件名（不含扩展名）与大小都相同时跳过，用于断点重跑。\n"
                          "默认开启：tdl 不带此参数时完全没有已存在检查，"
                          "同一批文件会整批重下（iter.go 源码实证）"),

                # 默认开启：tdl 检测到未完成记录时，若不带 --continue 会弹交互式
                # 确认（survey: Found unfinished download, continue from 'x/y'?），
                # GUI 没有终端无法回答，会直接卡住/失败。--continue 表示「不问，直接续传」。
                dict(key="continue", flag="--continue", label="继续上次下载（不再询问）",
                     type=FT_SWITCH, default=True, excl="restart",
                     help="开启：发现未完成的下载时直接续传，不再弹询问。\n"
                          "关闭：tdl 会在终端里询问「是否继续」，而 GUI 下没人应答会卡住。\n"
                          "与「重新开始上次下载」互斥（开启一个会自动关掉另一个）"),

                dict(key="restart", flag="--restart", label="重新开始上次下载",
                     type=FT_SWITCH, default=False, excl="continue",
                     help="把上一次的下载从头重新开始（与「继续上次下载」互斥）"),

                dict(key="rewriteExt", flag="--rewrite-ext", label="按 MIME 重写扩展名",
                     type=FT_SWITCH, default=False,
                     help="根据文件头部的 MIME 类型修正扩展名"),

                dict(key="desc", flag="--desc", label="从新到旧下载",
                     type=FT_SWITCH, default=False,
                     help="按时间倒序下载（可能影响断点续传）"),
            ]),

            # 参数设置内容由 dl_param_fields() 动态注入（见 form_renderer）
        ],
    ),

    # ================================================================ 上传
    dict(
        id="up", name="上传", icon="upload", cmd="tdl upload", group="task",
        desc="上传本地文件到 Telegram",
        groups=[
            dict(title="上传目标", fields=[
                dict(key="chat", flag="-c, --chat", label="目标会话",
                     type=FT_TEXT, default="", ph="-1001234567895 或 @username",
                     help="会话 ID 或域名；留空则上传到「收藏夹」。"
                          "对应 tdl upload -c"),
                dict(key="topic", flag="--topic", label="话题 ID", type=FT_NUMBER,
                     default=0, help="论坛群组的话题 ID，需与 --chat 一起用。0 表示不指定"),
                dict(key="path", flag="-p, --path", label="本地路径", type=FT_MULTI,
                     default=[], req=True, ph="D:/files  或  D:/a.jpg",
                     help="要上传的文件或目录，支持多条（每行一个）。"
                          "对应 tdl upload -p"),
            ]),

            dict(title="筛选与行为", fields=[
                dict(key="include", flag="-i, --include", label="仅上传这些扩展名",
                     type=FT_LIST, default=[], ph="mp4,jpg",
                     help="按文件扩展名包含，逗号分隔。留空表示不限制"),
                dict(key="exclude", flag="-e, --exclude", label="排除这些扩展名",
                     type=FT_LIST, default=[], ph="tmp,log",
                     help="按文件扩展名排除，逗号分隔"),
                dict(key="caption", flag="--caption", label="媒体说明文字",
                     type=FT_TEXT, default="", ph="留空用 tdl 默认模板",
                     help="文件附带的 caption，支持 Go 模板"),
                dict(key="photo", flag="--photo", label="图片以照片方式上传",
                     type=FT_SWITCH, default=False,
                     help="图片当作 photo 而非 document 上传（会被压缩）"),
                dict(key="rm", flag="--rm", label="上传后删除本地文件",
                     type=FT_SWITCH, default=False,
                     help="上传完成后删除源文件（谨慎使用）"),
            ]),
        ],
    ),

    # ================================================================ 转发
    dict(
        id="forward", name="转发", icon="forward", cmd="tdl forward", group="task",
        desc="在会话之间转发消息（带自动降级与路由）",
        groups=[
            dict(title="转发设置", fields=[
                dict(key="from", flag="--from", label="来源（消息/JSON）",
                     type=FT_MULTI, default=[], req=True,
                     ph="https://t.me/频道/123  或  D:/export.json",
                     help="要转发的消息，可以是链接或导出的 JSON 文件，支持多条。"
                          "对应 tdl forward --from"),
                dict(key="to", flag="--to", label="目标会话",
                     type=FT_TEXT, default="", req=True, ph="-1001234567895",
                     help="转发到哪个会话，支持表达式路由。对应 tdl forward --to"),
                dict(key="mode", flag="--mode", label="转发模式", type=FT_SELECT,
                     default="direct",
                     options=[
                         dict(v="direct", l="direct — 直接转发（保留原作者，无限制时可用）"),
                         dict(v="clone", l="clone — 克隆转发（重新上传，绕过转发限制）"),
                     ],
                     help="tdl 只支持 direct / clone 两种模式"),
            ]),

            dict(title="可选行为", fields=[
                dict(key="silent", flag="--silent", label="静默发送",
                     type=FT_SWITCH, default=False, help="不产生通知"),
                dict(key="single", flag="--single", label="不合并转发相册",
                     type=FT_SWITCH, default=False,
                     help="默认会自动识别并整组转发，勾选后逐条转发"),
                dict(key="desc", flag="--desc", label="倒序转发",
                     type=FT_SWITCH, default=False, help="对每个来源按时间倒序转发"),
                dict(key="dryRun", flag="--dry-run", label="预演（不实际发送）",
                     type=FT_SWITCH, default=False,
                     help="只显示会发生什么，不真正发送消息"),
                dict(key="edit", flag="--edit", label="编辑说明（表达式）",
                     type=FT_TEXT, default="", ph="留空不编辑",
                     help="用表达式引擎改写消息或 caption"),
            ]),
        ],
    ),

    # ==================================================== 其他：会话列表
    dict(
        id="chat-ls", name="会话列表", icon="list", cmd="tdl chat ls", group="other",
        desc="列出账号下的全部会话",
        groups=[
            dict(title="筛选与输出", fields=[
                dict(key="filter", flag="-f, --filter", label="筛选表达式",
                     type=FT_TEXT, default="true", ph="true",
                     help="按表达式过滤会话（tdl 默认 true = 全部）。"
                          "例：ID > 0 或 VisibleName contains 'a'"),
                dict(key="output", flag="-o, --output", label="输出格式",
                     type=FT_SELECT, default="table",
                     options=[
                         dict(v="table", l="table — 表格（在日志区直接可读）"),
                         dict(v="json", l="json — JSON（便于后续处理）"),
                     ],
                     help="注意：这是输出**格式**，不是文件路径"),
            ]),
        ],
    ),

    # ==================================================== 其他：导出消息
    dict(
        id="chat-export", name="导出消息", icon="chat", cmd="tdl chat export",
        group="other",
        desc="导出指定会话的消息记录，供下载使用",
        groups=[
            dict(title="导出范围", fields=[
                dict(key="chat", flag="-c, --chat", label="会话 ID / 域名",
                     type=FT_TEXT, default="", ph="-1001234567895 或 @channel",
                     help="留空则导出「收藏夹」。对应 tdl chat export -c"),
                dict(key="type", flag="-T, --type", label="导出类型", type=FT_SELECT,
                     default="time",
                     options=[
                         dict(v="time", l="time — 按时间区间（-i 传时间戳或日期）"),
                         dict(v="id", l="id — 按消息 ID 区间（-i 传两个 ID）"),
                         dict(v="last", l="last — 最近 N 条（-i 传条数）"),
                     ],
                     help="决定下方 -i 参数怎么填"),
                dict(key="input", flag="-i, --input", label="区间参数",
                     type=FT_LIST, default=[], ph="见上方说明",
                     help="视 --type 而定：id 模式填两个整数（起、止）；"
                          "last 模式填条数；time 模式填时间。逗号分隔"),
                dict(key="output", flag="-o, --output", label="输出文件",
                     type=FT_PATH, default="tdl-export.json", pick_from="json",
                     help="导出的 JSON 文件路径（tdl 默认 tdl-export.json）。"
                          "点「浏览」默认从软件的 resources/json 目录开始挑"),
            ]),

            dict(title="过滤与内容", fields=[
                dict(key="filter", flag="-f, --filter", label="消息筛选表达式",
                     type=FT_TEXT, default="true", ph="true",
                     help="按表达式过滤消息。填 '-' 可查看可用字段"),
                dict(key="reply", flag="--reply", label="指定频道帖 ID",
                     type=FT_NUMBER, default=0, help="只导出该帖子的回复。0 表示不指定"),
                dict(key="topic", flag="--topic", label="指定话题 ID",
                     type=FT_NUMBER, default=0, help="只导出该话题的消息。0 表示不指定"),
                dict(key="all", flag="--all", label="包含非媒体消息",
                     type=FT_SWITCH, default=False,
                     help="导出全部消息（含纯文本），仍受 filter 与 type 限制"),
                dict(key="withContent", flag="--with-content", label="同时导出消息内容",
                     type=FT_SWITCH, default=False, help="附带正文内容一起导出"),
                dict(key="raw", flag="--raw", label="导出原始 MTProto 结构",
                     type=FT_SWITCH, default=False, help="输出原始结构，用于调试"),
            ]),
        ],
    ),

    # ==================================================== 其他：导出成员
    dict(
        id="chat-users", name="导出成员", icon="list", cmd="tdl chat users",
        group="other",
        desc="导出会话成员名单（仅频道/超级群）",
        groups=[
            dict(title="成员导出", fields=[
                dict(key="chat", flag="-c, --chat", label="会话域名 / ID",
                     type=FT_TEXT, default="", req=True, ph="@channel 或 -1001234567895",
                     help="要导出成员的频道或超级群。对应 tdl chat users -c"),
                dict(key="output", flag="-o, --output", label="输出文件",
                     type=FT_PATH, default="tdl-users.json", pick_from="json",
                     help="成员名单保存路径（tdl 默认 tdl-users.json）。"
                          "点「浏览」默认从软件的 resources/json 目录开始挑"),
                dict(key="raw", flag="--raw", label="导出原始结构",
                     type=FT_SWITCH, default=False, help="附带原始 MTProto 字段，便于调试"),
            ]),
        ],
    ),

    # ==================================================== 设置：数据迁移
    dict(
        id="migrate", name="数据迁移", icon="db", cmd="tdl migrate",
        setting=True, group="setting",
        desc="把当前存储的数据迁移到另一个后端",
        groups=[
            dict(title="目标存储", fields=[
                dict(key="to", flag="--to", label="目标存储配置",
                     type=FT_TEXT, default="", req=True,
                     ph="type=bolt,path=C:\\Users\\你\\.tdl\\data",
                     help="格式 type=driver,key1=value1,...；"
                          "驱动可选 legacy / bolt / file。"
                          "源存储用全局参数里的 --storage 指定"),
                dict(key="dryRun", flag="--dry-run", label="预演（不实际迁移）",
                     type=FT_SWITCH, default=False,
                     help="只打印将要执行的操作"),
            ]),
        ],
    ),

    # ==================================================== 设置：扩展管理
    dict(
        id="extension", name="扩展管理", icon="tool", cmd="tdl extension",
        setting=True, group="setting",
        desc="管理 tdl 插件扩展",
        groups=[
            dict(title="操作", fields=[
                dict(key="action", flag="", label="操作类型", type=FT_SELECT,
                     default="list", req=True,
                     options=[
                         dict(v="list", l="list — 列出已安装的扩展"),
                         dict(v="install", l="install — 安装扩展"),
                         dict(v="remove", l="remove — 卸载扩展"),
                         dict(v="upgrade", l="upgrade — 升级扩展"),
                     ],
                     help="tdl 的扩展操作是子命令（tdl extension <action>）"),
                dict(key="name", flag="", label="扩展包名 / 路径",
                     type=FT_TEXT, default="",
                     show_when=dict(f="action", in_=["install", "remove", "upgrade"]),
                     ph="github.com/user/tdl-ext",
                     help="要操作的扩展标识"),
                dict(key="force", flag="--force", label="强制安装（覆盖已存在）",
                     type=FT_SWITCH, default=False,
                     show_when=dict(f="action", in_=["install"]),
                     help="即使扩展已存在也重新安装"),
            ]),
        ],
    ),

    # ==================================================== 设置：检查更新
    dict(
        id="update", name="检查更新", icon="refresh", cmd="tdl update",
        setting=True, group="setting",
        desc="检查并更新 tdl 自身版本",
        groups=[
            dict(title="更新选项", fields=[
                dict(key="check", flag="--check", label="仅检查不更新",
                     type=FT_SWITCH, default=False,
                     help="只查询是否有新版本，不执行下载替换"),
            ]),
        ],
    ),

    # ==================================================== 设置：版本信息
    dict(
        id="version", name="版本信息", icon="info", cmd="tdl version",
        setting=True, group="setting",
        desc="查看 tdl 版本与构建信息",
        groups=[
            dict(title="说明", fields=[
                dict(key="__ver_note", flag="", label="", type=FT_NOTE,
                     help="运行后会在下方日志区显示版本号、commit、构建时间与 Go 版本。"),
            ]),
        ],
    ),

    # ==================================================== 设置：恢复数据
    dict(
        id="recover", name="恢复数据", icon="db", cmd="tdl recover",
        setting=True, group="setting",
        desc="从备份中恢复 tdl 数据",
        groups=[
            dict(title="说明", fields=[
                dict(key="__rec_note", flag="", label="", type=FT_NOTE,
                     help="recover 需要交互式确认，GUI 暂不支持。"
                          "请在命令行执行：tdl recover"),
            ]),
        ],
    ),

    # ==================================================== 设置：备份数据
    dict(
        id="backup", name="备份数据", icon="save", cmd="tdl backup",
        setting=True, group="setting",
        desc="备份 tdl 数据到指定目录",
        groups=[
            dict(title="说明", fields=[
                dict(key="__bak_note", flag="", label="", type=FT_NOTE,
                     help="backup 需要交互式确认，GUI 暂不支持。"
                          "请在命令行执行：tdl backup"),
            ]),
        ],
    ),

    # ============================ 虚拟页（无等效命令，纯展示）================
    dict(
        id="tasks", name="任务进度", icon="list", cmd="实时任务表",
        virtual=True, kind="tasks",
        desc="每条任务一行：进度、速度、已耗时、预计结束时间",
    ),
    dict(
        id="dlstats", name="下载统计", icon="db", cmd="按日统计",
        virtual=True, kind="stats",
        desc="以自然日为周期，统计每日下载量、时长与平均速度",
    ),
    dict(
        id="msgview", name="消息浏览", icon="chat", cmd="tdl chat export 产物",
        virtual=True, kind="msgview",
        desc="按媒体类型分组浏览导出消息：图片显示完整图片、视频显示首帧缩略图（空消息不显示）",
    ),

    # ============================ Hub 页（横向药丸 Tab）====================
    dict(
        id="hub-task", name="任务", icon="list", cmd="download / upload / forward",
        virtual=True, kind="hub", hub="task",
        members=["dl", "up", "forward"],
        desc="下载、上传、转发（登录在左侧「登录」页）",
    ),
    dict(
        id="hub-other", name="其他", icon="folder", cmd="chat ls / export / users",
        virtual=True, kind="hub", hub="other",
        members=["chat-ls", "chat-export", "chat-users"],
        desc="会话列表、导出消息、导出成员",
    ),
    dict(
        id="hub-setting", name="设置", icon="sliders",
        cmd="全局参数 / 迁移 / 扩展 / 更新 / 备份",
        virtual=True, kind="hub", hub="setting",
        members=["__g_args", "migrate", "extension",
                 "update", "version", "backup", "recover"],
        desc="全局参数、数据迁移、扩展管理、检查更新、备份恢复",
    ),

    # ============================ 独立页：环境部署 =====================
    # 部署助手的完整能力合并进 GUI —— 左侧导航的独立一级页面。
    dict(
        id="deploy", name="环境部署", icon="tool", cmd="tdl 一键部署",
        virtual=True, kind="deploy",
        desc="安装 tdl / ffmpeg、写 PATH 与 TDL_* 环境变量、代理探测与生效修复",
    ),
]

CMD_BY_ID: dict[str, dict] = {c["id"]: c for c in COMMANDS}


# ============================================================================
# 侧边栏导航
# ============================================================================
NAV_GROUPS = [
    dict(id="login", type="cmd", cmd_id="login"),
    dict(id="hub-task", type="hub", hub="task", cmd_id="hub-task"),
    dict(id="hub-other", type="hub", hub="other", cmd_id="hub-other"),
    dict(id="tasks", type="cmd", cmd_id="tasks"),
    dict(id="dlstats", type="cmd", cmd_id="dlstats"),
    dict(id="msgview", type="cmd", cmd_id="msgview"),
    dict(id="hub-setting", type="hub", hub="setting", cmd_id="hub-setting"),
    dict(id="deploy", type="cmd", cmd_id="deploy"),
]


# ============================================================================
# Hub 内的虚拟卡片
# ============================================================================
GLOBAL_CARDS = [
    dict(id="__g_args", name="参数设置", icon="sliders", cmd="tdl --help",
         desc="全局通用参数：命名空间、代理、调试、存储后端"),
    # 「下载设置」卡片已删除：其内容（下载默认行为与传输参数）与
    # 「任务 → 下载 → 参数设置」子选项卡完全重复，只在下载页展示
]


# ============================================================================
# tdl 可执行文件路径
# ============================================================================
# 这里只是**兜底值**。真正用哪一个由 resolve_tdl_path() 动态解析，顺序为：
#   TDL_EXE 环境变量 → 部署助手导出的 deploy-state.json → 软件目录
#   → 系统 PATH → 常见位置 → 本常量
# 不写死的原因：tdl 可能被「tdl 部署助手」装在它自己的目录里，也可能在 PATH，
# 还可能换台机器就换了盘符 —— 写死会导致拷贝到别的机器直接跑不起来。
FALLBACK_TDL_PATH = r"D:\bin\tdl.exe"


def resolve_tdl_path() -> str:
    """动态定位 tdl.exe；全部落空时回退到 FALLBACK_TDL_PATH。"""
    try:
        from . import envcheck
        found = envcheck.resolve_tdl_path()
    except Exception:                                         # noqa: BLE001
        found = ""
    return found or FALLBACK_TDL_PATH


# 向后兼容旧引用：模块导入时解析一次（结果保证非空）
DEFAULT_TDL_PATH = resolve_tdl_path()
