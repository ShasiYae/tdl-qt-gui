# -*- coding: utf-8 -*-
"""
「环境部署」页底部的详细流程说明（无法自动化的步骤全在这里）。
输出 HTML 供 QTextBrowser 渲染。
"""

SECTIONS = [
    (
        "0 · 先看这里",
        """
<p>本页面负责<b>能自动跑的部分</b>：下载 tdl、装 ffmpeg、写 PATH、写 <code>TDL_*</code> 环境变量、
代理探测与生效修复 —— 点按钮即可。</p>
<p>但有两件事<b>任何工具都替不了你</b>：</p>
<ul>
  <li><b>代理</b> —— Telegram 服务器在海外，本机必须有可用的出口代理；</li>
  <li><b>登录</b> —— 需要你的手机接验证码、或在手机 Telegram 上扫码。</li>
</ul>
<p>下面 1~7 节把这两件事写清楚，照着做即可。部署产生的日志在本页操作后写入下方日志区。</p>
""",
    ),
    (
        "1 · 部署前准备",
        """
<table cellpadding="6" cellspacing="0" border="0" width="100%">
  <tr><td width="90"><b>操作系统</b></td><td>Windows 10 / 11，64 位</td></tr>
  <tr><td><b>磁盘空间</b></td><td>预留 <b>200 MB</b>（tdl 约 14 MB + ffmpeg 约 80 MB）</td></tr>
  <tr><td><b>网络</b></td><td>下载源走 GitHub（可换加速镜像）；访问 Telegram 必须走代理</td></tr>
  <tr><td><b>权限</b></td><td>写的是「用户级」环境变量（HKCU），<b>不需要管理员</b></td></tr>
</table>
<p><b>关于下载通道：</b>国内网络通常<b>能访问 api.github.com，却连不上 release 下载域名</b>，
表现是一直转圈。三个选项：直连 GitHub（海外/全局代理）、加速镜像（经 gh-proxy.com 中转，最省事）、
勾选「下载走代理」（有稳定本地代理时最可靠）。</p>
<p style="color:#8a6100;background:#fff8e1;padding:8px;">
部署位置固定为<b>本软件所在目录</b>（绿色便携）—— tdl.exe / ffmpeg.exe 与 main.py 并排，
整个文件夹可直接拷到别的机器用。
</p>
""",
    ),
    (
        "2 · 配置代理（必做·人工）",
        """
<p>tdl 连的是 Telegram 服务器，国内直连<b>必然超时</b>。</p>
<p><b>① 代理要满足什么条件</b></p>
<ul>
  <li>支持 <b>TCP 转发</b>：HTTP(S) 或 SOCKS5 均可</li>
  <li>能稳定跑长连接大流量（机场节点建议选不限流量的）</li>
  <li>常见形态：<code>127.0.0.1:7890</code>（Clash 混合端口）、<code>127.0.0.1:1080</code>（SOCKS5）</li>
</ul>
<p><b>② 地址怎么写</b></p>
<table cellpadding="6" cellspacing="0" border="0" width="100%">
  <tr><td width="100"><code>http://</code></td><td><code>http://127.0.0.1:7890</code></td></tr>
  <tr><td><code>socks5://</code></td><td><code>socks5://127.0.0.1:1080</code></td></tr>
  <tr><td>带账号密码</td><td><code>http://user:pass@host:port</code></td></tr>
</table>
<p><b>③ 怎么落地（先用「自动探测」）</b></p>
<ol>
  <li>点<b>「自动探测」</b> —— 工具会找正在运行的代理进程占用的端口
      （Clash / Mihomo / Verge / v2ray / Xray / sing-box…）、读它们的配置文件
      （<b>装了没开也能读到</b>）、再对候选端口做协议探测与真实连通验证；</li>
  <li>在「探测结果」下拉里挑一个带 <b>✓可用</b> 的，地址自动填好；</li>
  <li>点<b>「一键部署」</b>或「写入变量」，写成用户级 <code>TDL_PROXY</code>；</li>
  <li><b>重启本程序</b>（环境变量只对新进程生效），再验证。</li>
</ol>
""",
    ),
    (
        "3 · 登录 Telegram（必做·人工）",
        """
<p>tdl 内置了官方 API 凭证，<b>不需要申请 api_id / api_hash</b>。三种模式：</p>
<table cellpadding="6" cellspacing="0" border="1" width="100%"
       style="border-collapse:collapse;border-color:#e3e6ea;">
  <tr style="background:#f2f4f7;">
    <td width="70"><b>模式</b></td><td width="150"><b>命令</b></td><td><b>需要准备</b></td><td width="60"><b>推荐</b></td>
  </tr>
  <tr><td><b>扫码</b></td><td><code>tdl login -T qr</code></td>
      <td>手机端 Telegram「设置 → 设备 → 扫描二维码」扫终端里的码</td>
      <td style="color:#2e9e5b;"><b>★ 首选</b></td></tr>
  <tr><td><b>验证码</b></td><td><code>tdl login -T code</code></td>
      <td>手机号（含 +86）+ 验证码 + 两步验证密码（若开启）</td><td>备选</td></tr>
  <tr><td><b>桌面会话</b></td><td><code>tdl login -T desktop</code></td>
      <td>本机已登录的 Telegram Desktop 客户端</td><td>免验证码</td></tr>
</table>
<p><b>验证码是 Telegram App 内部推送</b>（不是短信）。一直收不到就用扫码模式。</p>
<p><b>多账号：</b>用 <code>-n</code> 隔离命名空间，如 <code>tdl -n default login -T qr</code>；
登录态存在 <code>%USERPROFILE%\\.tdl\\data\\&lt;命名空间&gt;</code>。</p>
""",
    ),
    (
        "4 · 验证是否真的生效（点「检测生效」）",
        """
<p>「探测到端口」不等于「tdl 真的用上了代理」。工具会分 7 项逐级验证：</p>
<table cellpadding="6" cellspacing="0" border="1" width="100%"
       style="border-collapse:collapse;border-color:#e3e6ea;">
  <tr style="background:#f2f4f7;"><td width="140"><b>检测项</b></td><td><b>含义</b></td></tr>
  <tr><td>变量已写入</td><td>注册表 <code>HKCU\\Environment</code> 里有没有 <code>TDL_PROXY</code></td></tr>
  <tr><td>当前进程可见</td><td>本进程读不读得到（读不到说明要重启程序）</td></tr>
  <tr><td>代理可连外网</td><td>直接通过该端口请求一次墙外地址</td></tr>
  <tr><td>tdl 显式代理</td><td><code>tdl --proxy … chat ls</code> 能否跑通</td></tr>
  <tr><td>tdl 用环境变量</td>
      <td><b>只靠 <code>TDL_PROXY</code></b> 跑 tdl —— 这才是「生效」的决定性证据</td></tr>
  <tr><td>系统时间偏差</td><td>见下（很容易被忽略，却常是真凶）</td></tr>
  <tr><td>tdl NTP 校准</td><td>是否已给 tdl 配好时间源</td></tr>
</table>
<p><b>为什么查系统时间：</b>Telegram 的 MTProto 握手<b>带时间戳</b>，系统时间偏差过大
（超过 2 分钟）会<b>直接连不上</b>，症状和「代理不通」一模一样 —— 都是超时、连接失败。
偏差超过 30 秒就会告警。</p>
<p><b>「一键修复」做四件事：</b></p>
<ol>
  <li>重写用户环境变量 <code>TDL_PROXY</code> 并广播刷新；</li>
  <li>生成 <code>tdlx.cmd</code> —— 把 <code>--proxy</code> 写死，<b>不依赖环境变量</b>（兜底入口）；</li>
  <li>设置 <code>TDL_NTP</code> —— 让 tdl 自己按 NTP 校准，<b>绕过本机时钟偏差且免管理员</b>；</li>
  <li>尝试 <code>w32tm /resync</code> 同步本机时间（一般需管理员）。</li>
</ol>
<p style="color:#1a7f37;background:#eaf3de;padding:8px;">
<b>判据小提示：</b>tdl 报 <code>not authorized. please login first</code> 时，网络<b>其实已经连通</b>
—— 要先连通才能做鉴权检查。说明代理是好的、只是没登录，工具会区分开，不会误报。
</p>
""",
    ),
    (
        "5 · 安装扩展（可选）",
        """
<div style="background:#f6f8fa;padding:10px;font-family:Consolas,monospace;">
<pre style="margin:0;">tdl extension list                       <span style="color:#6b7280;"># 列出已装扩展</span>
tdl extension install &lt;名称或仓库地址&gt;    <span style="color:#6b7280;"># 安装</span>
tdl extension upgrade &lt;名称&gt;              <span style="color:#6b7280;"># 升级</span>
</pre></div>
<p>具体可用列表<b>以官方仓库为准</b>，不要从不明来源安装。</p>
""",
    ),
    (
        "6 · deploy-state.json 的用途",
        """
<p>部署完成后会在<b>软件目录</b>生成 <code>deploy-state.json</code>，
供外部脚本 / 其它工具读取（GUI 自己通过 <code>envcheck.resolve_tdl_path()</code> 自动发现，
<b>不依赖这个文件</b>）：</p>
<div style="background:#f6f8fa;padding:10px;font-family:Consolas,monospace;">
<pre style="margin:0;">{
  "tdl_exe":     "&lt;软件目录&gt;\\\\tdl.exe",
  "tdl_version": "0.20.4",
  "ffmpeg_exe":  "&lt;软件目录&gt;\\\\ffmpeg.exe",
  "env":         { "TDL_PROXY": "http://127.0.0.1:7890", "TDL_THREADS": "8", ... },
  "namespaces":  ["default", "tn"],
  "logged_in":   true
}</pre></div>
""",
    ),
    (
        "7 · 常见问题排查",
        """
<table cellpadding="6" cellspacing="0" border="1" width="100%"
       style="border-collapse:collapse;border-color:#e3e6ea;">
  <tr style="background:#f2f4f7;"><td width="170"><b>现象</b></td><td><b>原因与处理</b></td></tr>
  <tr><td>下载 GitHub 一直转圈</td>
      <td>国内常见：api 通、release 域名不通。<b>下载通道换加速镜像</b>，或勾选「下载走代理」。</td></tr>
  <tr><td>杀软报毒 / 文件被删</td>
      <td>tdl.exe 与 ffmpeg.exe 均无签名，属<b>误报</b>。把软件目录加入白名单。</td></tr>
  <tr><td><code>not authorized</code></td>
      <td>没登录或 <code>-n</code> 命名空间不对。<b>注意：这说明网络是通的，代理没问题。</b></td></tr>
  <tr><td>连接超时 / connection reset</td>
      <td>代理未生效。确认已<b>重启程序</b>；用「检测生效」逐项排查。</td></tr>
  <tr><td>连不上，但代理明明是通的</td>
      <td><b>查系统时间</b>。偏差 &gt;2 分钟会让 MTProto 握手失败，「一键修复」会给 tdl 配 NTP。</td></tr>
  <tr><td>下载中途断流</td>
      <td>连接池过高。<code>TDL_POOL</code> 降到 3~5、<code>TDL_LIMIT</code> 降到 3~4。</td></tr>
  <tr><td>改了环境变量不生效</td>
      <td>已运行程序不会重读环境变量，<b>重启该程序</b>；或用 tdlx.cmd 启动。</td></tr>
</table>
""",
    ),
]


def build_html() -> str:
    parts = [
        """<html><head><meta charset="utf-8"></head>
<body style="font-family:'Microsoft YaHei UI',sans-serif;font-size:13px;
             color:#0f1419;line-height:1.75;margin:0;">""",
        '<p style="color:#55606c;margin:0 0 10px 0;">'
        "自动部分用上面的按钮完成，以下是需要你亲自操作的部分。</p>",
    ]
    for title, body in SECTIONS:
        parts.append(
            '<h3 style="font-size:14px;color:#0f1419;margin:18px 0 6px 0;'
            'padding-bottom:5px;border-bottom:1px solid #e6e8eb;font-weight:500;">'
            + title + "</h3>")
        parts.append(body)
    parts.append("</body></html>")
    return "".join(parts)


def plain_text() -> str:
    """纯文本版（复制用）。"""
    import re
    chunks = []
    for title, body in SECTIONS:
        txt = re.sub(r"<[^>]+>", "", body)
        txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
        chunks.append(f"【{title}】\n{txt}")
    return "\n\n".join(chunks)
