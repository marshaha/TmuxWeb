# TmuxWeb

[English](README_EN.md) · [测试与验收](TESTING.md) · [Testing](TESTING_EN.md)

TmuxWeb 是一个自托管的 tmux 与 AI CLI 会话管理中心。它把本机及 SSH 远程主机上的 tmux pane 组织为 `主机 → session → window → pane`，并提供 ChatGPT 风格聊天、真实 PTY 终端、结构化会话记录和受控分享。

> 安全提醒：TmuxWeb 能向 tmux pane 输入文字并提供交互终端，等同于访问运行服务的系统账户。不要直接暴露到公网；请使用 HTTPS、访问控制、强密码和防火墙。

## 功能

- **多主机管理**：一个本地服务管理 localhost 和 SSH 远程主机；远端只需 SSH 与 tmux，不需安装守护进程。
- **主机表单**：「主机/IP」独占一整行、输入框满宽；「显示凭据内容」勾选框可把密码转明文、私钥去掉遮蔽；「不校验连接直接保存」允许密码/密钥有误、甚至主机暂时不可达时先把主机存下来（列表显示离线、指纹显示「待首次连通时记录」），该主机第一次真正连上时自动记录并固定指纹（TOFU，`StrictHostKeyChecking=accept-new`），之后按固定指纹校验；稍后改好凭据即可正常使用。
- **macOS 的 tmux 路径**：ssh 非交互命令看不到 Homebrew 的 PATH，因此首次连接会把 tmux 解析成绝对路径并缓存（`command -v` → `/opt/homebrew/bin` → `/usr/local/bin` → `~/bin` → `bash/zsh -lc`），之后每条远端命令都带 `TMUX_BIN`；确实没装时报错会提示 `brew install tmux`；tmux 装在别处时可用 `TMUX_WEB_TMUX_PATH` 指定。
- **tmux 树导航**：按主机、session、window、pane 展示；展开状态保存在浏览器本地。
- **AI 聊天视图**：Claude Code、Codex、Hermes、Kimi Code 读取结构化 transcript（本地和 SSH 远端都能读）；无法解析时回退 ANSI `capture-pane`。
- **跨服务器中继**：任意 pane 里的 agent 可以通过 HTTP API / `relay_client.py` 向另一台服务器上的 agent pane 提问并拿到回答，TmuxWeb 负责注入、等待和回收答案。
- **远端渲染优化**：SSH 连接复用（ControlMaster）、远端内容摘要轮询、空闲退避，远端 pane 的中文标题和内容不再被本地化设置改写成下划线。
- **文件面板**：侧边栏显示当前 pane 工作目录的文件树（可进入子目录、逐级返回、滚动），支持点击下载和拖拽上传，本地与 SSH 远端一致。
- **宽表格不会撑破气泡**：markdown 表格放在 `.table-holder` 里横向滚动（列多时保持自然宽度，不把每格挤成一字一行），
  气泡自己 `max-width:min(92%,820px)` + `overflow-x:clip`，宽内容撑不大它；「复制全部 / 时间」留在气泡里
  （`.assistant-body > .msg-actions`），触屏下常显（`@media (hover:none)`），不再靠 0.45 的透明度飘着。
- **消息操作**：每条消息右下角「复制全部」复制整条原文；消息内每个块（代码 / mermaid / 表格）有独立工具条——「复制」复制块内容（表格复制为 TSV，mermaid 复制源码），表格与 mermaid 还可「源码 / 渲染」互切。mermaid 用本地 vendored 的 mermaid.js 懒加载渲染（只有出现 mermaid 块时才下载 3MB）。
- **多窗口**（`/grid`）：和主页同一套外壳（主页现在也直接 `TmuxShell.mount()`，不再自带一份侧栏）——**左侧就是主页那条会话树侧边栏**（同样的图标行、展开/折叠全部、可拖宽、可折叠、手机是抽屉），点树里的会话直接把它加成窗口——已经在窗口里的行会带一条橙色标记，再点它只是跳到那个窗口闪一下，不会重复添加。把几个会话并排放在一页里同时看。布局三种：**并排**（可拖宽）、**网格**、**画布**（触控板/平板上也能直接拖：标题栏和把手都 `touch-action:none`，
拖动时锁住画布滚动与 iframe 手势，只有这一格在动；4px 死区保证"点一下"不会把它抖歪，触屏把手常显并放大到 22px）。 画布里的「整理」会按画布比例自动挑行列：每格的宽高比尽量接近 1.4:1，
 竖排额外罚分、空位也罚分，所以 2 个窗口是 2×1、3–4 个是 2×2、5–6 个是 3×2、7–9 个是 3×3
 （手机上仍是一列，一屏一格）。手机上存下来的单列布局换到宽屏会自动重排，不会一路竖着堆；
 点错了就点旁边的「撤销」（或 Ctrl/⌘+Z）——只记最近一次，整理和换行列都能撤回，撤回后不会再被自动横排覆盖前两种的格子数像 Word 插表格一样选——点工具栏的「格子」按钮弹出 6×6 小格，**鼠标划过去就预览列×行**并告诉你「几个窗口、要几行、留几个空位」，点一下生成；不够用时点**「自定义…」自己填列 × 行（1–6 × 1–6）**，回车即应用，用过的小形状会留在按钮上（比如 `4×2`）和布局一起记在本地。**画布**把整块面板当成一张画布：每个窗口都是可以自由摆放的矩形——拖标题栏移动、拖四边或四角改大小（最窄 240×160），拖到贴近别的窗口边缘/中线时**停顿一下才淡出虚线**并吸附过去（`SNAP=6px`、`GUIDE_DELAY=140ms`，拖得快的时候不会一直闪），松手即消失；拖动时角上或标题旁显示实时尺寸，拖出右边/下边画布会自己变大。手动摆乱了点「整理」重新平铺；选中过的窗口用 `Alt/⌘ + 方向键` 以 8px（加 Shift 40px）微调，不会因为手抖错过 1px。位置、大小、格子数都跟着布局存本地。选完在一行或一列里**拖接缝就能单独调这一格的大小**，同一行的其余窗口按比例自动适配（最小 170px，拖过头会停住）；拖动窗口标题栏可以调换顺序；布局、行列比、顺序、单列宽度都存在浏览器本地。换行列、加/移窗口都是**原地复用**这些窗口（iframe 不重载），所以改布局不会把正在跑的终端断开、也不会让聊天从头拉一次 transcript。滚出视野的窗口会自动降到 15 秒查一次（回到视野立刻刷新），所以同时开四五个远端会话不会一直打满。控制台侧边栏底部的「+」把当前会话直接加进去（`/grid?add=…`），完整预览页顶栏也有一个多窗口入口。
  每个窗口可以单独选**聊天**还是**终端**（标题栏第二个按钮切换，shell 这类没有 transcript 的 pane 默认就是终端），切换只重载那一个窗口；双击标题栏把某个窗口**最大化**（Esc 还原，最大化只是切 class，不会重连终端）；「预设」可以给整套排布起个名字存下来，之后一键应用。每个窗口是一个独立控件（标题 + 状态点 + 在控制台打开 + 移出），内容就是嵌进去的对话页（`/preview?pane=…&embed=1`），可以继续在里面提问；窗口之间的分隔线可以拖动改宽窄，宽度按 pane 记录在 localStorage，新加的窗口均分剩余空间；手机上自动变成纵向堆叠的整宽卡片。入口：控制台侧边栏顶部的四宫格图标（紧挨主页图标）、主页侧边栏同一个图标、以及侧边栏底部「+ 窗口」（把当前会话直接加进去）。所有页面（`/`、`/home`、`/preview`、`/grid`、`/hosts`、`/shares`）都要求登录并带 `next` 回跳。
- **真实终端**：xterm.js + PTY attach，支持输入、resize、ANSI。手机上按 Termius 的做法重做过：
  - **没有输入框**：键盘直接打进终端（一个不可见的 contenteditable 承接软键盘），点终端任意位置就起键盘，底部键盘按钮收起；用 contenteditable 而不是 `<input>` 是为了让 iOS 不弹出键盘上方那条「↑ ↓ 完成」的表单助手条——那是 WebKit 给**表单控件**加的系统条，网页无法关闭；若某些 iOS 版本对 contenteditable 不友好，加 `?kb=input`（或 `localStorage['tmux-kb-input']='1'`）可切回真正的 input；**iOS 上默认用自绘键盘**（不唤起系统键盘，所以根本没有那条白条）：点终端就弹出我们自己的键盘——字母三行（qwerty，`shift` 切大写）、`123` 符号层（`- / : ; ( ) $ & @ %` 与 `. , ? ! # * + = _ ~`）、`del`（长按连删）、空格、`ret`，右下角 `系统键盘` 可切回系统键盘并记住选择；`?kb=custom` / `?kb=system` 可强制。因为那条「↑ ↓ 完成」是 WKWebView 的 `WKFormAccessoryView`，只要聚焦任何可编辑元素（`input`、`textarea`、`contenteditable` 都一样）就会挂上，网页端无 API 可关（Apple radar 27763084、W3C editing 组的 opt-out 诉求至今未实现），所以不唤起系统输入框才是唯一解；安卓/桌面不受影响，仍是系统键盘；`?kb=auto` 可让 iOS 回到「点终端即起系统键盘」；
  - **一行快捷键条**：`shift tab`、粘贴、`?`、`/`、`esc`、`tab`、`ctrl`、`alt`、方向键、`^C` 横向滚动，右侧常驻「更多」和「键盘」两个图标；`ctrl`/`alt` 是锁存键（按一下点亮，作用于下一个字符）；
  - **更多按键面板**：点「更多」展开四列扩展键（`^C ^D ^Z ^L ^R ^U ^K ^W`、`| ~ - = : ; ! $ % ^ < > ( ) { } [ ]`、`home/end/pgUp/pgDn`、方向键、`paste/del/@/&`），展开时会先收起系统键盘，让面板顶替它的位置；
  - **双指捏合改字号**（9–22px，跟随列宽重排，记住选择），滚动离开底部时出现「回到底部」；
  - 键盘弹出时终端只收缩、顶栏保留，PTY 收到新行列并重画。
  `/term`（多窗口里的终端格子）同样支持粘贴和捏合缩放。
- **安全发送**：文字经 tmux buffer 原样粘贴，不拼接 shell 命令；提交时处理旧草稿和 bracketed paste。
- **新建会话**：主机行右侧的「+」可在本机或任意远程主机上创建新的 tmux 会话（`tmux new-session -d`），
  弹窗里填会话名（字母/数字/`.`/`-`/`_`，1–64 字符）和可选工作目录，目录不存在会直接报错；建完自动展开并选中该 pane。
- **远程连接**：支持密码、私钥，以及直接使用本机密钥/SSH Agent（远端已授权该公钥时不必再输入凭据）；固定 SSH host key 指纹；密码/私钥使用独立 Fernet 主密钥加密保存（本机密钥模式不保存任何凭据）。
- **会话分享**：可设置 slug、密码、有效期，并可选择**只读 / 可对话**；支持续期、重置密码、停止和删除；**任何 pane 都能分享**——有结构化 transcript 的（Claude / Codex / Kimi / Hermes）按消息渲染，纯 shell 则按屏幕文本渲染且可继续输入。
- **PWA/移动端**：所有页面（控制台、主页、多窗口、终端、主机、分享）都带 `manifest`、`apple-touch-icon`、`apple-mobile-web-app-*` 和 `theme-color`，可以「添加到主屏幕」以独立窗口运行；manifest 里有 `shortcuts`（主页看板 / 多窗口）。终端在 **>760px 的桌面浏览器上不显示底部辅助键条**，整块高度全给终端；窄屏才出现键条与自绘键盘。Service Worker 只缓存**静态资源**（`shell.*`、`settings.js`、图标、`xterm/mermaid/ansi_up/marked`）且走**网络优先**：每次上线都是新的，断网时才有旧副本兜底；页面本身、`/api/`、`/ws/`、登录状态从不进缓存。
- **图标**：`web/app-icon.svg` 是唯一源文件（深色圆角底 + 米白 `>` ＋ 陶土色块光标），`scripts/build_icons.py` 从它渲染出 32/180/192/512 的 PNG 与 maskable 变体；所有页面（含登录页）都带 favicon，manifest 里 `any` 与 `maskable` 分开声明，安卓圆形裁剪不会切到图案。
- **文件面板**：路径栏可直接编辑（绝对路径或相对 pane 当前目录，回车跳转、Esc 取消，聚焦时全选），工具栏的下载按钮直接打包当前目录（有选中则只打包选中项）；每行都带勾选框，`Ctrl/⌘ + 点` 切换单项、`Shift + 点` 选范围（桌面习惯），选中后页脚出现「全选 / 清空 / 下载 zip (N)」，文件或整个目录都能打包（本地用 zipfile，远端只需 `tar`，拉回字节后本地转 zip）；解压总量上限 `TMUX_WEB_ZIP_MAX`（默认 128MB）、文件数 `TMUX_WEB_ZIP_MAX_FILES`（默认 1000）。
- **一个 pane 只有一个名字（完全同步）**：钉子区和主页收藏是两份列表，但名字共用一份。在侧栏改名
  会写收藏卡片，在看板改名会写钉子，钉住/收藏时没给名字就继承另一边已有的名字；清空则两边一起回到
  自动名。打开旧的 `home.db` 时会把历史上分叉的名字合并（只填空的一侧，不删记录）。
  显示顺序固定为「钉子 label → 收藏 title → 会话/窗口自动名」，侧栏、控制台顶栏（含浏览器窗口标题）、
  `/grid` 每格、`/preview` 都走同一个出口，所以钉住一个收藏过的 pane 不会退化成
  `claude · 0: main · oracle`。
- **每个窗口一个短编号 `#N`**：跨主机统一（`home.db` 的 `pane_numbers`，首次见到时分配、**不回收**），
  侧栏的会话树和「最近」、控制台顶栏、`/grid` 每格标题栏、`/preview` 与 `/term` 标题栏都显示它，浏览器/PWA 的窗口标题也带 `#N` 前缀（控制台、单窗口或最大化的多窗口、预览、终端）。用途有三处：`?pane=#12` 直接打开、MCP relay 里
  `ask_agent`/`send_agent`/`read_agent` 的 `target` 可以写 `#12`（`list_agents` 会返回 `handle`），
  口头/文档里也能用一个号指代某个 agent。编号解析统一在 `pane_or_404` / `pane_transport`，所以 `read_agent`、`send_agent`、`ask_agent` 以及任何 `/api/panes/#12/...` 的 HTTP 调用都认这个写法。编号指向已经关掉的窗口会 404 并说明原因，不会落到别人的会话上。
- **侧栏悬停预览**：鼠标在会话树的一行或「最近」的一行上停住（默认 2 秒，设置里可改 1/2/3 秒或关闭），
  旁边浮出这个会话的最后一条消息，卡片底部是「完整预览 / 聊天 / 关闭」。面板和首页卡片是**同一份实现**
  （`/assets/pane-preview.js` + `/api/home` 的数据），不在收藏/最近里的 pane 悬停时才按需取一次 transcript
  （60 秒内复用）。手机 web 没有悬停，默认关闭；个别浏览器把 hover 能力报错时可以用 `?preview=1` 强制打开。
- **文件面板的删除 / 重命名**：选中一项后工具栏出现「重命名」（只改当前目录里的名字，不覆盖同名文件），
  选中任意多项出现「删除」（非空目录要先勾「连同目录内容一起删除」）。桌面还可以右键、手机长按 520ms
  弹出同一个菜单（打开 / 下载 / 重命名 / 删除），键盘 `F2` 改名、`Delete` 删除。改动走
  `POST /api/panes/<pane>/files/rename` 与 `/files/delete`，护栏拒绝操作 `/`、家目录和顶层目录。
- **受控 MCP 邮箱**：可选的 Claude Code、Codex、Hermes 文本消息中继，不开放任意 shell/文件能力。

## 守护与故障排查

`tmux kill-session`、`exec` 断链、进程被杀都会让"会话整个消失"——因为 tmux 在窗格进程退出时就关掉会话，
看起来像服务自己不见了。所以线上用 `scripts/run_service.sh` 起服务，而不是直接 `exec uvicorn`：

```
tmux new-session -d -s tmuxweb -c ~/workspace/TmuxWeb './scripts/run_service.sh'
```

- 进程退出后 2 秒自动重启（tmux 会话不会因为一次崩溃而消失）。
- 每次退出都记一行 `logs/service-exit.log`，退出码说明死因：`129`=SIGHUP（会话被 kill-session 销毁）、
  `130`=SIGINT、`137`=SIGKILL（`kill -9`）、`143`=SIGTERM、`139`=段错误。访问日志在 `logs/service.log`。
- 想抓"谁杀的"：`tmux set-hook -g session-closed 'run-shell "echo \"$(date -Is) closed session=#{hook_session_name}\" >> /tmp/tmux-session-closed.log"'`
  记录哪个会话何时关闭（注意 hook 里 `#{session_name}` 是错的，要用 `#{hook_session_name}`）；再看 `./.venv/bin/python scripts/tmux_watch.py --seconds 1800` 记录的 tmux 客户端命令
  （`kill-session` 只活几毫秒，只能这样抓）。`logs/` 不入库。

排查结论（2026-10-30）：`tmuxweb` 两次消失时，内核 `kern.log` 无 OOM、无 segfault，`systemd-oomd` 无 kill
记录（`user@1002.service` 虽然是 `ManagedOOMMemoryPressure=kill`），apport 无崩溃报告，`destroy-unattached=off`，
tmux server 未重启——即会话不是被 tmux 自己回收的，而是窗格里的进程被外部信号终止（无 shutdown 日志 ⇒
SIGKILL/SIGHUP），随后 tmux 关闭了会话。想精确定位需要 auditd（当前未安装），现在有上面三层记录 + 自动重启兜底。

### 手机上怎么看多窗口

- 顶栏自动瘦身：只留图标（添加窗口 / 预设 / 主页），「并排 / 网格 / 画布」在手机上隐藏——手机永远是单列。
- 竖屏一格一屏，可以上下滑动换窗口（外壳正好一屏高，滚动发生在窗口列表里）；窗口里的 iframe 会吃掉竖向
  手势，所以右下角还常驻一个 `↑ 1/2 ↓` 翻页器，点一下平滑滚到上一条/下一条，到头会自动置灰。
  它默认贴在右下（`env(safe-area-inset-*)` 之内，不会被 iPhone 圆角咬掉）；**长按 380ms 可以拖到任何顺手的位置**
  （位置记在本机），**双击胶囊空白处回到默认位置**。
- 每个窗口是一屏高的卡片，上下滑动切换，滚动会吸附到卡片边界。
- 窗口默认是**聊天**；普通 shell 没有结构化对话时，聊天卡片里显示它的当前屏幕（只读，5 秒刷新）并给一个「切到终端（可输入）」按钮，而不是一片空白。要长期用终端就点标题栏的切换按钮，这个选择会被记住。

### 掉线的卡片

收藏的会话所在主机/窗格掉线时，卡片的状态点显示「离线」，预览面板标题栏写「离线」；如果之前读到过内容，
就继续显示最后一次缓存（标题栏写「离线 · 缓存 · 10 分钟前」），不会变成一片空白。缓存只存在内存里
（`TMUX_WEB_HOME_ENTRY_SECONDS` 之外仍然保留最后一份），不会写入磁盘。

### 暂停实时刷新

顶栏的 ⏸（暂停）只是**停掉实时刷新**：终端模式下画面原样留在屏幕上（不会黑屏、不会清空），聊天模式下已
渲染的消息也保持不动，状态点亮灰、文字变成「已暂停实时刷新（点 ▶ 继续）」。点 ▶ 继续时**不会先黑屏**：旧画面一直留在屏幕上，等新的 attach 真正开始送数据的那一刻才清屏重绘（服务端
attach 会整屏重绘一次），所以在慢链路上点继续也是「旧的定格 → 直接变成最新的」；聊天模式则是重新拉取。
暂停期间输入不会发出去。

### 发送失败与重发

输入框下面那行状态文字：默认灰色，发送成功显示绿色「已发送 ✓」并在约 1.4 秒后自动清空，发送失败显示**红色**
「发送失败」并停在原地（同时气泡上出现「重发」按钮）。点「重发」会先显示「重发中…」，成功后同样变绿并清空——
不会像以前那样把上一次的「发送失败」留在左下角。

### 消息里的路径可以点

聊天里出现的路径会被后处理成链接（`/assets/path-links.js`，控制台和 `/preview` 共用，
也可以单独引用来处理任意 DOM 子树）：

- 识别**纯文本**里的路径（不要求是 markdown 链接）：`本地文件：/Users/x/y.md`、`（/a/b）`、
  `路径；/a/b` 这类中英文标点后面跟的都能认；也支持 `~/x`、`./x`、`../x` 和
  `results/out.md` 这种"斜杠 + 扩展名"的相对路径；
- 太短的（`/grid`、`/OUT-OF-BAND`，只有一段）和接口前缀（`/api`、`/assets` 等）不当作文件；
- 点之前先问服务器 `GET /api/panes/<pane>/files/stat?path=…`（`~` 由服务端展开，
  相对路径按 pane 当前目录解析），所以不会把普通词点成死链：
  - **目录** → 左侧文件面板直接进到该目录；
  - **文件** → 文件面板切到它所在目录并开始下载；
  - **markdown** → 打开预览（查看器里带「下载」按钮）；
  - 不存在 → 顶部提示 `找不到：…`。
- 只改文本节点，`<pre>` 代码块、已有链接、按钮都不动；支持键盘 Tab + Enter。
- **懒处理**：渲染时不扫，鼠标移入（`pointerover`）/手指按下（`pointerdown`）/键盘聚焦到某条消息时
  才扫那一条，结果打 `data-paths` 不重复处理 —— 长会话不必每次重绘都全量扫文本；
  触摸时同一个手势里就会把刚生成的路径打开（不用点两次）。
- **悬停气泡**：鼠标停在路径上约 0.4 秒，浮出小卡片（文件名、大小/「目录」，以及「预览」「下载」）。
  `log`/`txt`/`json`/`yaml`/`csv`/源码等文本类点开是**预览**（查看器里带下载按钮），
  不再是点一下就静默下载；二进制文件才直接下载。气泡在触摸设备上不弹。
- **相对路径自动补偏移，再往下搜一级**：agent 常吐“自己 cwd 下”的路径（如
  `ate/s3k/ca_pu_crossboard_1102.md`），但真实文件可能在 cwd 的下一级（`ddr_patch/ate/s3k/...`）。
  `GET /files/stat` 的顺序是：按 pane 当前目录 → 上一级目录 → 家目录解析；都不中就在 cwd / 上一级里
  做一次**有界后缀搜索**（pty 只发一条命令：远端 `find -maxdepth 4` 并跳过 `.git`/`node_modules`/`.venv`
  等目录；本地是剪枝后的 `os.walk`，上限 4000 个目录）。命中后返回体带 `via:"search"` 与 `relative`
  （相对于 pane cwd 的真实位置），前端气泡会显示「子目录 · <relative>」，免得看起来像路径本身存在。
  `offset` 仍然表示用了第几个基准（0 = 原样命中），`path` 一定是最终绝对路径。结果按
  pane+基准+路径缓存 60 秒，悬停不会每次都付一次 SSH 往返。
- **移动端下载必须先确认**：`/assets/downloads.js` 的 `TmuxDownload.maybe(href,{name,meta})` ——
  触摸设备先弹底部卡片（文件名 + 大小 + 取消/下载），点了才真的下载；桌面点一下直接下。
  文件面板单个文件、zip 打包、md 查看器的下载按钮、路径气泡里的下载都走这一层
  （iOS Safari 直接点下载链接会跳进没有返回按钮的系统预览）。

### 聊天里上传文件

「附件」按钮在**输入框卡片内部的第一行**，下面一条 1px 的细线把它和输入区隔开
（也可以直接把文件拖进输入区，或点它选多个）。同一份实现由 `/assets/composer-files.js`
提供，控制台聊天和 `/preview`（多窗口里每个格子内嵌的就是它）共用，行为完全一致。
文件会先传到
**该 pane 当前目录下的 `upload_files/`**（远端 pane 也一样，走同一条 SSH 通道），然后：

- 输入框上方出现待发送列表：图片是小格子缩略图，其它文件显示名字和大小；
- 每个附件右上角有 ×，点掉就连服务器上的那份一起删掉（`DELETE /api/panes/<pane>/files/upload`，
  只允许删 `upload_files` 目录下的单层文件名）；
- 发送时自动在消息前面补一行 `user uploaded file “a.png & b.pdf”`，再空一行接你写的内容，
  所以 agent 一眼就知道该读哪些文件；
- 多窗口里的窗口和正常聊天窗口是同一个界面：同样的附件、同样的发送前缀，
  标题也统一优先「最近 / 收藏」里改过的名字（不再出现"窗口名/对话名"两套）；
- 切到别的会话会清空这条列表（附件属于上一个 pane 的目录），发送成功后也会清空；
- 上传上限沿用文件面板的 `TMUX_WEB_FILE_UPLOAD_MAX`（默认 256MB）。

### 手机全屏与输入框

「对话全屏 / 终端全屏」在手机上走 `height:var(--visual-height,100dvh)`，而不是写死 `100vh`：
键盘弹出时 `visualViewport` 会变矮，写死 100vh 会把输入框顶到键盘底下，只露出上半截
（2026-11 反馈"全屏后输入框有部分不显示"）。全屏里 iOS 不再给 `env(safe-area-inset-bottom)`，
所以输入区底部额外留 4px，越过底部圆角与 home indicator。

### 主题

设置 → 主题，六套配色：**默认（暖灰 + 陶土橙）、ChatGPT（白 + 绿）、Claude（米白 + 橙）、GitHub（白 + 蓝）、
Claude Code（深色终端）、夜间（中性深灰）**。选择**跟着账号走**：存在服务端（`home.db` 的 `prefs` 表，
`GET/PUT /api/prefs`），所以换设备/换浏览器登录后是同一个主题。实现上登录时服务端把主题写进一个可读 cookie
（`tmux_theme`），页面首帧就能上色，不闪；`localStorage` 只在没登录的登录页和离线时兜底。所有页面
（控制台、主页、多窗口、终端、预览、登录、主机/分享管理）以及插件自己的页面一起变。

实现上只换 CSS 变量（`--page/--surface/--ink/--accent/…`），`theme.css` 里 `:root[data-theme=…]` 的优先级
高于各页自己的 `:root`，所以加新主题只需要加一段变量。
变量分两层：骨架是 `--page/--sidebar/--surface/--surface2/--ink/--muted/--faint/--line/--accent`，
「表面」是 `--card/--card-hover/--bubble/--line-strong/--shimmer`；页面里**不许再写死浅色底**，
否则深色主题就是白底白字（`claude-code` 的多窗口和设置面板踩过这个坑）。
彩色的浅底（状态胶囊、提示条）一律写成 `color-mix(in srgb,var(--green) 14%,var(--card))` 这种
「色相 + 表面」的组合。回归测试 `tests/test_theme_contrast.py` 会扫描所有页面和共享资源，
出现亮度 > 0.65 的写死底色直接失败。

### 侧边栏窄了怎么办

侧栏顶部改成上下两段：**标题一行在上，图标独占一行在下**。图标永远不换行到「半行」：

- **PC（有鼠标、没触控）**：图标保持 30px，一行放不下就**换到第二行**（左对齐、间距一致），所以侧栏拖到
  200px 也能全部看见、且不需要横向滚动——鼠标横滑太别扭，这是刻意做的。
- **手机/触屏**：图标 40px，一行放不下就**横滑**；右边缘用侧栏底色做了一道**渐隐**（只有图标滑到下面时才显现），
  就是"右边还有东西，可以滑"的提示。

控制台、主页、多窗口三处一致（控制台 `index.html`，另外两处走共享的 `shell.css`）。

**侧栏是同一份实现**：`assets/recents.js` + `assets/recents.css` 负责「最近」这块的标记、数据、钉子、
收藏、改名、高度和折叠，三个页面都只 `TmuxShell.mount()` / `TmuxRecents.mount()` 挂一次，页面里不再各抄一份。
页面之间只有「点一行做什么」不同：控制台切到那个会话、主页跳到控制台并选中它、多窗口把它加成新的格子
（已在窗口里的就闪一下）。会话树的星和最近行里的星是同一份收藏，谁点了另一处立刻跟着变。控制台打开
文件面板时这块自动让位。

**会话树下面的「最近」**：一条分隔线下面是最多 6 个最近打开过的会话（`/api/home/recent`，跟账号走），
每条显示「主机 · session:窗口」+ 命令 + 相对时间（时间只在悬停时出现，把宽度让给标题），左侧圆点跟着
busy/idle/offline 变；点一下就切过去，切过去的会话会自动顶到第一位。

每条右侧两个小按钮：**钉子**把这一条钉在区块最上面（`/api/home/recent/pin`，服务端 `pins` 表，跟账号走，
不占最近 30 条的名额，主机掉线也还在、用它记下的标题显示，固定项之间用一条细线跟普通「最近」分开）、
**星星**收藏到主页看板（跟会话树、预览页的星是同一份收藏）。钉子再点一次取消固定；触屏没有悬停，两个按钮
常显、行高 44。钉住的会话被打开时只更新时间、不会被从固定区拖下来。

**钉住的还能改名**：双击标题（触屏长按 480ms，或点这一行右侧的铅笔）就地改名，Enter 保存、Esc 放弃、
清空就回到自动生成的「主机 · session:窗口」。

**钉住和收藏是两份独立的数据**，互不生成、互不借用：收藏不会凭空长出一条钉子，钉住也不会把卡片上的
标题搬过来，最近列表只显示钉子自己的名字。唯一的联系是"用户主动改名"这一个动作——同一 pane 既钉住
又收藏时，改名会顺手把另一边也改成同一个名字（`PATCH /api/home/recent/pin/{pane}` 与
`PATCH /api/home/favorites/{pane}` 在同一个事务里），这样两处不会各叫各的；不想联动就别改名，两边各用
各自的默认名字。

固定项被框在一块浅色圆角面板里（跟下面的普通「最近」一眼分开；深浅色主题各自跟着强调色走）。

**这块的高度可以调，默认占侧栏 2/5**：拖它的上边缘（`row-resize`）改高度，15%–75% 之间夹紧，
双击把手回到 2/5；比例存在浏览器本地，换页面/重开都在。标题行最左边的箭头可以**整个折叠**这块
（只留一行标题），折叠状态同样记住。条数多了就在这块里用滚轮/手指滑动（`overscroll-behavior:contain`，
不会带着整页一起弹）。标题行还有一个钉子，直接钉/取消钉**当前会话**；右上角 × 只清普通最近，
固定的不动。文件面板打开时整块自动让位。

**侧栏里显示会话还是文件**：控制台（`/`）的侧栏在「会话列表」和「文件面板」之间二选一，默认规则是
**默认永远是会话列表**；点标题行里的文件夹图标（或文件面板里点目录）才切到文件管理，切 pane 时面板会跟着
换成那个 pane 的目录，点面板右上角 × 或再点一次图标就回到会话列表。这个开关**不跨页面加载记忆**——以前它会存进 `localStorage`，于是你上次开着文件面板，之后从主页卡片/多窗口/直接链接进 chat 时侧栏都先弹文件
管理，看起来像是随机的。主页和多窗口的侧栏只有会话树，没有文件面板。

想让它按会话记住的话，**设置 → 侧栏** 打开「按 pane 记住」：之后每个 pane 各自记住自己上次是会话列表还是
文件面板（连目录一起），重新打开控制台、或从别的 pane 切回来都恢复成上次的样子；没打开过的 pane 仍然是会话
列表。这个偏好和主题一样存在账号里（`/api/prefs` 的 `sidebar`，值 `pane` / `none`），本浏览器镜像一份
`localStorage['tmux-sidebar-memory']` 供页面加载时同步读取；每个 pane 的具体视图存在
`localStorage['tmux-files-view']`（最多保留 60 个 pane）。

### 消息时间

每条消息下面那一行（原来只有「复制全部」）现在左边是消息时间：今天的显示 `HH:MM`，更早的显示
`MM-DD HH:MM`，等宽数字、淡灰色；右边仍是复制按钮。用户气泡和 agent 气泡一致，`/grid`、`/preview`
里的同一行也一样。

### 图标

界面图标是内联 SVG sprite（`<use href="#i-...">`）。「复制」字形照 `icon.png` 里的两个叠放圆角矩形画成了
`#i-copy`（描边 1.7、`currentColor`）；「分享」沿用原来的箭头（控制台是 `#i-external`，主页/多窗口侧栏是
各自的 `#i-share`），没有再换成 icon.png 里那个粗轮廓箭头。

## 设置面板

控制台左下角（`/grid` 侧边栏底部也有同一个入口，「设置」面板由 `/assets/settings.js` 提供、各页面共用）：

面板打开时焦点落在面板自己身上，**不会**给任何一行画焦点框（Chrome/Firefox/Safari 都会给第一个控件加蓝框，
这里专门绕开了）；用键盘 Tab 时才出现品牌色的橙色描边。

- **服务器管理 / 分享管理**：两行直接跳 `/hosts`、`/shares`，省得再从侧栏小图标里找。
- **修改密码**（点开才显示表单）：当前密码 + 新密码（≥6 位）。首次修改后密码不再来自环境变量，改以
  pbkdf2 哈希存 `passwords.json`（0600，路径 `TMUX_WEB_PASSWORD_DB`）；当前浏览器自动续上会话，
  其他设备需用新密码重新登录。
- **导入 / 导出**（一行，里面再分「导出 / 导入」两个小标签）：
  - 导出：先设一个**导出密码**（≥6 位），再选「导出服务器 / 导出分窗布局 / 全部导出」。文件在浏览器里
    用 PBKDF2(SHA-256, 250k) + AES-GCM 加密后才落盘，**密码不上传服务器**；内容含服务器凭据（密码/私钥）、
    跳板链、tmux 路径、分窗布局与预设，请当作密钥文件保管。
  - 导入：选文件 + 输入导出时的密码。密码不对直接报错（AES-GCM 认证失败）；已存在的主机
    （同地址/端口/用户名）跳过，跳板关系按主机名重新接上，分窗布局与预设覆盖本机。
- **退出登录**：面板左下角。侧边栏不再单独放「退出」按钮，避免和「设置」抢位置、也防误触。
## 插件

功能可以做成插件放在 `plugins/<名字>/`（一个 `plugin.toml` + 一段 Python），默认只加载第一方插件，
**设置 → 插件**里可以逐个开关（即时生效，选择存在 `plugins.json`），`TMUX_WEB_PLUGINS` 是环境变量层面
的默认值；`GET /api/plugins` 列出装了哪些、哪些启用、哪些加载失败。
**跨服务器通信（relay）已经是插件**：它的四个 HTTP 端点就在 `plugins/relay/`，关掉就没有这些接口。
插件还能往侧栏加按钮、往 `/mcp` 注册工具、提供自己的静态页面（`/plugin-assets/<名字>/`，需登录）；
这三样要在 `plugin.toml` 里用 `permissions = ["ui","mcp","assets"]` 显式声明。插件与主进程**同一解释器**，
声明只约束这三类对外能力，不是内核沙箱——不受信的插件请整机容器化运行。
写法和 API 见 [`plugins/README.md`](plugins/README.md)。

## 架构与边界

- 聊天模式不创建长期 `tmux attach`，避免多个客户端参与 tmux 尺寸协商。
- 只有终端模式使用 PTY attach；WebSocket 断开后释放。
- agent 的识别不只看 tmux 报的前台命令：Codex/Claude 有时被 shell 包着启动（`zsh -c 'codex …'`、
  后台进程、runtime 外壳），`pane_current_command` 就成了 `zsh`。这时按 `pane_pid` 的后代进程判断
  （远端在同一次 SSH 里顺带取 `ps -A -o pid=,ppid=,args=`），`#92` 这种就能正常出对话；窗口/会话名
  里带 codex 不算数，避免误判。
- 远端结构化 transcript 也不依赖远端 python3：远端只跑一段 POSIX sh 定位会话文件（Claude 项目目录 + 标题/屏幕探测、Codex session_index、Kimi state.json、Hermes `--resume` 的 session jsonl 或 `sqlite3`），把文件尾部字节拉回本地，用**本地同一套解析器**解析——等同 `scp` 拉回来再解析。定位不到时回退远端 `capture-pane`。远端会话由前端按 1 秒节奏轮询：服务端按 (主机, pane, limit) 缓存解析结果，2 秒内重复请求不打远端，之后只用一次 `stat` 判断文件是否变化，变了才重新拉取——所以远程对话跟着远端文件实时更新，空闲时几乎无开销。
- 远端 transcript 的传输是增量且压缩的：首次定位只扫最新 3 个会话文件的尾部 256KB（不再 grep 整个几百 MB 的文件），之后每次只按字节偏移拉取**新增部分**（`tail -c +offset`），并用 `gzip` 压缩上线——实测一台 300MB 会话、0.7s RTT 的主机：首次 53s → 8.6s，后续轮询 1.5s 左右，单次传输从 4MB 降到几十字节到几百 KB。窗口大小可用 `TMUXWEB_PAYLOAD_BYTES` 调整。
- 远端 **Claude / Codex** 的会话文件优先按 **pane 自己进程里的 `--resume <id>` / `--session-id <id>` / `codex resume <id>`** 定位，找不到才退回「按 cwd 找最新文件 + 标题/屏幕行打分匹配」。同一个目录里并行跑好几个 Claude 会话时，后一种猜法会挑到隔壁的会话——网页上的 chat 会一直停在另一段对话上、看起来「不刷新」。Codex 另外还要求
`session_index.jsonl` 的紧凑 JSON（`"thread_name":"…"`，冒号后没有空格）能被匹配到——以前只按带空格的写法 grep，
整个标题索引形同虚设。
- 前端会缓存每个 pane 的 transcript、滚动位置和未发送草稿：切换回看过的会话时立刻从缓存渲染（实测 0.02–0.14s），同时在后台按 1 秒节奏重新校验，只有签名（mtime + 消息数 + 最后一条 id）变化时才重绘；最多保留 8 个 pane 的工作集。这样在多窗口之间来回切换时不会每次都白屏等待远端。
- 远端会话文件会**定期重新定位**：client 端记住了上次的会话文件并按字节增量读取，但 agent 在同一个 pane 里开新会话会写新文件；为此每次 tail 会顺带读取 pane 标题，标题变了（或在 `TMUX_WEB_TRANSCRIPT_RELOCATE` 秒后，默认 30s）就重新定位一次，避免一直读旧文件。网页版与客户端的刷新按钮会带 `refresh=1`，直接丢掉增量状态与缓存。
- 远端 pane ID 带主机作用域，避免不同 tmux server 的 `%0` 冲突。
- 远端 tmux 调用统一带 `LC_ALL=C.UTF-8`，字段分隔符使用可打印字符串，因为 tmux 在非 UTF-8 locale 下会把控制字符和非 ASCII 字段改写成 `_`。
- 实时视图先取远端 `cksum` 摘要，内容未变就不传输正文；空闲后轮询间隔退避到 2 秒。
- 当前管理登录是单用户模型；多用户场景还需要用户级主机授权与审计。

## 文件浏览与上传下载

侧边栏右边缘可以拖动改宽度（200–640px，双击边缘恢复默认），宽度记在 `localStorage` 里，控制台与主页各记各的；侧边栏变窄时顶部的图标按钮会自动换行（放不下才横向滚动），不会被裁掉点不到；手机上侧边栏是抽屉，宽度固定，不受影响。

侧边栏顶部「▤」按钮切换到当前 pane 工作目录的文件视图；再点一次（或点面板里的「✕」）就把面板收起来，侧栏回到 pane 树。开关状态会记在浏览器里，刷新后按上次的布局恢复（连同上次选中的 pane）。

侧边栏顶部「▤」按钮切换到当前 pane 工作目录的文件视图：

- 目录在前、文件在后，点目录进入、点「..」返回上级；隐藏文件默认不显示。
- 点击文件名即下载（`Content-Disposition: attachment`，支持中文文件名）。
- 把文件拖到面板里即上传到当前目录；也可以用面板上的「⇧」按钮多选上传。
- 本地 pane 直接读文件系统；SSH 远端完全用 POSIX 工具：目录清单靠远端 `find`（GNU 有 `-printf` 就用，否则回退 GNU/BSD `stat`），下载 `head -c`，上传 `cat >`。**远端不需要 python3、不需要装任何东西**，等同 scp 的依赖水平。
- 上传先写临时文件再原子替换，避免半截文件覆盖原文件；单文件上限由环境变量控制（默认下载 64MB、上传 256MB）。

### agent 弹出来的选择，聊天里也能点

harness 在等你做选择时会占住整个屏幕：kimi 的 `/model` 选择器、claude 的
`Do you want to proceed?`、kimi 的 `1. Compact and continue`。以前聊天里只看到"已发送"，
人得切到终端才能答。现在这些会被认出来，变成输入框上方的一张卡：

- 认得出三种形态：光标列表（`❯`）、编号列表（`1.`）、是/否或回车确认（`[y/N]`、`Press Enter`）；
- 点按钮、或者卡片聚焦时用 `↑↓` 选 `Enter` 确认、`Esc` 取消；
- 卡片里带「屏幕原文」，能核对到底在答哪个问题；`✕` 只是收起，不发任何键；
- 永远不自动应答，认不出就不弹，随时可以切终端。

实现上只有一个新接口和两条规矩：

```bash
GET  /api/panes/{pane}/prompt          # 解析屏幕，认不出返回 {"prompt": null}
POST /api/panes/{pane}/prompt/choose   # {"index": 2} 或 {"key": "esc"}
```

1. **列表没有序号就不发数字**：kimi 的选择器里数字会被当成搜索过滤词，列表一变位置全错。
   所以按键序列是「先按够多次 ↑ 顶到第一项（顶部多余无害）→ 按 index 次 ↓ → Enter」。
2. **发完要复核**：重新抓屏，菜单消失/标题变了/光标动了才算成功；三轮都没变化就告诉用户
   "再点一次或切到终端"，绝不假装成功。

### 开关（`[ On ] Off`）也能点

kimi 的 `Thinking  Low [ High ] Max` 这种分段开关同样会进卡片：一行若干段、正好一段被方括号
包住的就是当前值。点任意一段，服务端按「当前值 → 目标值」算方向，只发必要的 `←`/`→`
（点在当前值上什么都不发），发完照样复核。

tab 栏、进度条、自由文本输入仍然不做卡片 —— 那些要理解每个 TUI 的布局语义，猜错等于替用户
答错一个不该答的问题。

### 聊天里直接看视频

消息里出现视频链接（`.mp4` / `.webm` / `.mov` / `.m4v` / `.ogv` / `.ogg` / `.mkv`，大小写不限，
带查询串也认），或者指向本地视频文件的路径（`/path/to/clip.mp4`，会被改写成 `/files/raw`），
就地渲染一个播放器：

- `preload="metadata"` —— 不点播放只取时长和第一帧，不会把整段视频拖下来（手机流量友好）；
- 播放器下方保留原文件名链接，方便右键另存或换窗口打开；
- 本地视频走 `/api/panes/{pane}/files/raw`，`video/*` 现在按 `inline` 返回，否则 iOS Safari 会直接下载；
- 只认 `http(s)` / `blob`，`javascript:`、`data:` 这类链接不会变成播放器。

HTTP 接口（需登录 Cookie 或 `TMUX_WEB_API_TOKEN`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/panes/{pane}/files?path=&hidden=&limit=` | 列目录，`path` 省略时用 pane 的 cwd |
| GET | `/api/panes/{pane}/files/download?path=` | 下载单文件 |
| PUT | `/api/panes/{pane}/files/upload?dir=&name=` | 请求体为文件原始字节 |

> 注意：文件读写等同于服务运行账户的权限，远程主机上同样如此。只给可信的人开端口。

## 跨服务器中继

任何能访问 TmuxWeb 的进程都可以中继，典型用法是让 A 机器上的 agent 去问 B 机器上的 agent：

```bash
export TMUXWEB_URL=https://tmux.example.internal
export TMUXWEB_TOKEN=...              # 与 .env 里的 TMUX_WEB_API_TOKEN 一致
chmod +x relay_client.py

./relay_client.py peers               # 列出所有主机上的 agent pane、状态和可达性
./relay_client.py ask '%12' '把刚才的部署结论整理成三条'
./relay_client.py ask 'build01~%3' '这版单元测试过了吗？' --timeout 300
./relay_client.py send 'build01~%3' '请先跑一遍单元测试'
```

pane 引用是 `%12`（本地）或 `<host_id>~%3`（远端，`peers` 会输出）。

HTTP 接口（`Authorization: Bearer $TMUX_WEB_API_TOKEN`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/relay/peers` | 所有主机的 pane、provider、`idle`/`busy` 状态 |
| GET | `/api/panes/status` | 精简状态，供前端轮询 |
| POST | `/api/relay/ask` | `{target, question, timeout, force, source}`，同步返回 `reply` |
| POST | `/api/relay/message` | 只发送不等待 |
| GET | `/api/relay/log` | 最近 100 次中继记录（内存环形缓冲） |

中继协议：TmuxWeb 在问题里嵌入一次性标记，要求目标 agent 在回答最后一行输出
`TMUXWEB_REPLY_<token>: "<JSON 字符串>"`，然后按 0.8 秒轮询远端/本地 capture 直到命中或超时（504 会带 pane 尾部内容，便于判断）。目标 pane 正忙时默认拒绝（409），需要 `force=true` 显式覆盖。

## 环境要求

- Linux 或 macOS
- Python 3.11+
- tmux
- OpenSSH 客户端（使用远程主机时还需要 `ssh-keyscan`）
- 可选：Claude Code、Codex、Hermes、Kimi Code CLI

## 安装

```bash
git clone https://github.com/marshaha/TmuxWeb.git
cd TmuxWeb
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

生成登录密码与稳定的 Session Secret：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

将结果分别写入 `.env`：

```dotenv
TMUX_WEB_PASSWORD=替换为长随机密码
TMUX_WEB_SESSION_SECRET=替换为64位十六进制随机值
TMUX_WEB_HOST=127.0.0.1
TMUX_WEB_PORT=8787
```

`.env`、数据库、SSH 凭据和主密钥均被 `.gitignore` 排除。不要提交它们。

## 启动

```bash
chmod +x start_tmux_web.sh
./start_tmux_web.sh
```

浏览器访问 `http://127.0.0.1:8787`。

后台运行示例：

```bash
tmux new-session -d -s tmuxweb "$PWD/start_tmux_web.sh"
tmux capture-pane -p -t tmuxweb -S -100
tmux kill-session -t tmuxweb
```

如果必须监听所有接口，可在 `.env` 中设置：

```dotenv
TMUX_WEB_HOST=0.0.0.0
```

这必须配合防火墙、HTTPS 反向代理或安全隧道。浏览器访问 HTTPS 时，代理到本地源站仍可使用 HTTP。

管理员登录和分享密码按客户端地址限速（默认 60 秒内最多 5 次失败）。Cookie 在 HTTPS 请求下自动带 `Secure`；如果 TLS 代理没有正确传递请求协议，请在 `.env` 设置 `TMUX_WEB_SECURE_COOKIES=true`。

## 添加远程主机

打开 `/hosts`，依次完成：

1. 输入地址、端口和用户名；
2. 选择认证方式：密码（默认掩码，点右侧眼睛才显示）、私钥，或「本机密钥（SSH Agent）」——后者用本机 `~/.ssh` 的默认私钥或 ssh-agent，远端 `~/.ssh/authorized_keys` 里已有对应公钥时可以直接测通，不落任何凭据；注意服务进程要能读到 `SSH_AUTH_SOCK`（systemd 部署时需显式透传）；
3. 测试联通：会自动扫描并记录该主机的 SHA256 host key 指纹，并把指纹显示在表单下方；
4. 添加主机：再次校验 SSH 与远程 `tmux list-panes`，成功后保存。

指纹采用首次连接即记录（TOFU）：之后同一主机返回不同指纹时连接会被直接拒绝，需要到 `/hosts` 删除重加。

密码和私钥不会返回浏览器列表 API。私钥应是完整的 OpenSSH/PEM 私钥，而不是 `ssh-ed25519 AAAA...` 公钥。当前不支持带 passphrase 的加密私钥。

注意：分享一个 shell pane 等于把该 pane 的**读写**终端交给拿到链接的人（可以执行命令）；只想给人看输出时，请在 agent 会话上分享，或分享后尽快撤销/设短有效期。

## 管理远程主机

`/hosts` 页面除了新增，还能直接维护已保存的主机：

- **编辑**：显示名称、主机/IP、端口、用户名、登录方式都能改；凭据留空表示沿用已保存的密码/私钥，填了才替换
- **重新确认指纹**：只有改了主机名或端口才重新扫描并锁定新的 SSH 指纹，其它编辑不动指纹
- **排序**：每行的 ↑ / ↓ 调整顺序，顺序存库（`position` 列，旧库自动迁移），左侧树与 `/api/hosts` 都按这个顺序
- **删除**：连同本地加密凭据一起删除

保存前会先用新配置做一次 SSH + tmux 探测；探测失败就把这次编辑**整体回滚**并返回错误，避免把一台能用的主机改成连不上。
换了密码或密钥但探测失败时，旧凭据仍在库里。

接口：`PUT /api/remote-hosts/{id}`（部分字段，`credential` 省略即不修改）、`POST /api/remote-hosts/reorder`（`{"ids": [...]}`）。

## 性能：选中「刷屏」pane 不会把浏览器拖死

日志型 pane（uvicorn、`tail -f`、`make`、`while true; do echo; done`）每秒能产出成千上万行。之前选中这类 pane 会重建整块屏幕 DOM、反复重排字体、并让 xterm 的写入队列无限增长，Chrome 直接卡死。现在两边都做了限制：

- 实时屏幕流（`/ws/panes/{pane}` 聊天模式的 capture）第一帧带 1500 行，之后只回传最近 800 行；单帧超过 192KB 只保留尾部并标注「输出过快，已跳过前面 N 字节」；空闲时轮询间隔从 0.7s 退避到 3s
- 前端最多每 500ms 重绘一次、只渲染最近 600 行、只保留最后一条待绘帧（中间帧丢弃）；字体自适应当容器宽度不变时不再重排
- 终端模式（xterm）：写入按 rAF 合并；积压超过 1.5MB 时丢弃最早的数据并插入一行提示，而不是让内存无限增长
- 顶栏新增 **暂停 / 继续实时刷新** 按钮（⏸/▶）：暂停后关闭实时连接，屏幕保持不动，适合看日志时临时冻结
- `/api/hosts` 5 秒快照，`?refresh=1` 才真正重新探测远端；侧边栏刷新从每 5 秒改为每 20 秒，且内容没变时不再重建 DOM（顺带修掉了点击被吞、滚动位置被重置的问题）
- 相关环境变量：`TMUX_WEB_STREAM_LINES`、`TMUX_WEB_STREAM_FIRST_LINES`、`TMUX_WEB_STREAM_MAX_BYTES`、`TMUX_WEB_HOSTS_TTL`

实测（140×40 的 `while true; do echo; done` pane，10 秒窗口）：聊天模式主线程耗时 0.69s → **0.09s**，DOM 文本 363k → 55k 字符；暂停后 6 秒仅 0.02s。

### 状态扫描与 transcript 轮询

- busy/idle 扫描原来**每个 pane 一次 SSH capture**：69 个 pane（macmini 35、oracle 15）要 69 次往返，一次 8.7–14.4s。
  现在按主机批量：`capture_many()` 用一条命令取回该主机所有 pane 的尾部，再在本地 `classify()`，并且优先复用 `/api/hosts` 刚探测过的 pane 列表
  （不再重复 `list-panes`）。实测 **2.1–3.2s**（冷启动首个扫描仍是 ~8s，因为要建立 4 条 SSH 连接）。
- transcript 轮询的缓存 TTL 跟着链路走：最后一次拉取耗时多少，就缓存多久（下限 2s、上限 10s）。
  所以弱网主机不会每秒发一次 SSH——oracle 的连续 8 次轮询实测 0.36–2.0s（首次 10.6s），之前每次 1.4–7.6s。
- 聊天视图改成**增量渲染**：新消息只追加自己那一个气泡（连同 markdown/代码块增强），不再把整段对话重建一遍；
  只有「加载更早」、发送中的乐观气泡、或 DOM 与消息列表不一致时才回退到全量重绘。
- 页面隐藏（切到别的标签）时暂停 transcript 轮询，回到前台立刻补一次。

实测：158 条消息的会话，闲置 10 秒主线程 0.047s、60fps；`/api/panes/status` 命中缓存 0.01s、缓存过期后 2.16s 内给新值。

## 跳板机（jump host）与让 agent 自己添加服务器

添加主机时可以选一个已保存的主机作为**跳板机**（`jump_host_id`），连接会先到跳板机再跃到目标：

- 实现方式是 `ProxyCommand` + 内层 `ssh -W %h:%p`，所以**每一跳可以有不同的凭据**（跳板机用自己的密码/私钥，目标用自己的）；密码场景下会生成一个 askpass 脚本，按提示里的主机名区分返回哪一跳的密码
- 跳板机的主机指纹和普通主机一样固定：已记录就 `StrictHostKeyChecking=yes`，没有就首次连通自动记录
- **支持多级跳板**（A → B → C，最多 `TMUX_WEB_JUMP_MAX_DEPTH` 层，默认 5）：每一跳都用自己的凭据，嵌套成 `ProxyCommand=ssh -W %h:%p ... -o ProxyCommand=ssh -W %h:%p ...`；不允许指向自己、指向不存在的主机、或形成环（保存时返回 422）
- 删除某台跳板机时，经由它的主机自动回退为直连，不会留下悬空引用
- 主机列表里会显示完整的「经由 B → A」链路；编辑弹窗里可以改跳板机；`GET /api/remote-hosts` 也返回 `jump_chain` 和 `jump_path`
- **跳板机不必先添加成主机**：下拉选一台已保存的主机，或者直接在下面那行填 `user@host:port`（也可以只填 `host`）。手填的跳板机复用目标机自己的凭据（同一把 key / 同一个密码），所以密码只存一份；如果跳板机需要**不同**的凭据，就把它单独添加成主机再用下拉引用。两种方式只能选一种，同时填会返回 422

### HTTP API

```bash
# 列表（包含 jump_host_id）
curl -H "Authorization: Bearer $TMUX_WEB_API_TOKEN" https://your-host/api/remote-hosts

# 添加一台经由跳板机的目标机（agent 认证，不需要传密码）
curl -X POST https://your-host/api/remote-hosts -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "inner-box", "hostname": "10.0.0.20", "username": "ubuntu", "port": 22,
    "auth_type": "agent", "jump_host_id": "<跳板机的 id>", "verify": false
  }'

# 或者不保存跳板机，直接写 user@host:port（复用目标机的凭据）
curl -X POST https://your-host/api/remote-hosts -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{
    "name": "inner-box", "hostname": "10.0.0.20", "username": "ubuntu",
    "auth_type": "agent", "jump_spec": "ops@bastion.example:2222", "verify": false
  }'
```

### MCP

| 工具 | 作用 |
| --- | --- |
| `list_remote_hosts` | 列出已保存主机（id/名称/地址/认证方式/跳板机），不返回凭据 |
| `add_remote_host` | 添加主机：`name`、`hostname`、`username`、`port`、`auth_type`（默认 `agent`）、`credential`、`jump_host`（已保存跳板机的 id）**或** `jump`（`user@host:port`，不必先添加）、`verify` |
| `remove_remote_host` | 按 id 删除主机 |

`auth_type=agent` 用本机 ssh-agent 密钥，agent 无需传任何密码；`verify=false` 时即使暂时连不上也会保存，等下次连通再固定指纹。


## 新建会话：目录自动创建 + 直接拉起 agent

侧边栏每个主机后面的 `+` 打开「新建 tmux 会话」，三个字段都可留空：

- **工作目录**：目录不存在就自动 `mkdir -p`（远端同样是远端自己建），建不出来才报错并把目录名带回来。
  不想要这个行为时传 `"create_dir": false`，此时恢复成「目录不存在就 422」。
- **启动 agent（可选）**：下拉里只列**这台主机真的装了的** agent，不选就是普通 shell。
  扫描走一条 POSIX `sh` 探针（`command -v` + `~/.local/bin`、`~/.kimi-code/bin`、`/opt/homebrew/bin`
  等常见安装目录），不需要远端有 `python3`，也不跑 `--version`（macOS 没有 `timeout`，跑一次就慢一截）。
- **agent 参数（可选）**：按 shell 词法切分后原样跟在该 agent 后面，例如 `--model o3`、
  `--dangerously-skip-permissions`。引号没闭合会直接 422，不会被悄悄吞掉。
- **用探测到的绝对路径启动**：tmux pane 的 PATH 和 ssh 探测时看到的不一定一样
  （oracle 上 `claude` 装在 `~/.local/bin`，非交互 shell 里 `command not found`，exit 127）。
  所以命令行里写的是 `/home/ubuntu/.local/bin/claude`，不是裸 `claude`。
- **agent 退出不带走会话**：默认 `keep_open=true`，agent 一退出就先打印
  `[<路径> 已退出，code N] 会话保留…` 再 `exec $SHELL`，省得“创建成功然后会话凭空消失”。
  想要“agent 退出即结束会话”就传 `"keep_open": false`。

接口：

    GET  /api/hosts/{host}/agents           # {"agents":[{"name","label","path"}], ...}，缓存 5 分钟
    GET  /api/hosts/{host}/agents?refresh=1 # 强制重新探测（每台一次 ssh 往返）
    POST /api/sessions {"host","name","path","agent","agent_args","create_dir"}

    返回 {"session":{"session","path","created_dir","command"}, "panes":[...]}

支持的 agent 目录在 `agent_catalog.py`（hermes / claude / codex / kimi / opencode / gemini）；
加新的只要往 `AGENTS` 里加一行，UI 和接口自动带上。探测结果按主机缓存，别的地方要「这台机器能跑什么」
直接读缓存，不要再 ssh。

## 主页：收藏、看板与搜索

（主页右上角有「主机 / 分享 / 刷新」，回到控制台的入口在左侧会话树顶部。）控制台左侧每个 pane 右侧有一个五角星：空心 = 未收藏，实心 = 已收藏（点一下切换）。收藏的会话会出现在 `/home` 主页。

主页（`/home`）左侧保留与控制台一致的**可折叠会话树**（主机 → session → window → pane，带 busy/idle 状态点与收藏星标；宽屏时可折叠、折叠状态记在 `localStorage`，窄屏时变成抽屉 + 遮罩）。右侧是看板：

- **收藏卡片**：卡片名可改（铅笔图标 → 输入新名字，或「恢复默认」）；默认名是 **session · window · 主机**（tmux 的 pane title 常常只是主机名，所以不再直接用它）；卡片右上角有**完整预览**按钮：电脑上从右侧滑出抽屉，里面是完整的对话页（markdown/代码/表格、可继续发送、可停止、每 2 秒刷新），手机等窄屏则直接跳到 `/preview?pane=…`，返回手势/按钮回到主页；**鼠标悬停卡片会浮出最后一条 agent 回复的完整内容**（浮层可直接用鼠标滚轮滚动）（若最新一条是用户消息，则自动往上找最近的一条 AI 回复，最多 6000 字）；另有状态徽标（busy / idle / 离线；busy 且在等回复时显示「思考中…」并跳动）、provider、标题、路径、**最近一条对话预览**、pane 引用；点卡片直接跳回控制台并选中该 pane（`/?pane=<pane>`）
- **看板卡片**：agent 通过 MCP 工具 `publish_home_card` 推上来的结论/告警/链接，可在主页删除；
  正文按 **markdown** 渲染（粗体/列表/代码/链接/表格，复用 marked 与 `.md` 样式），卡片里约 6 行封顶，
  点卡片进预览看全文，正文里的链接新窗口打开、不会误触发卡片
- **「抓屏」和「终端」是两件事**：聊天模式兜底时用 `tmux capture-pane` 取一份**文本快照**（把整屏文字 + ANSI 转成 HTML 塞进页面），它没有终端语义、每帧整屏重建，全屏 TUI 会卡；「终端」模式是真正的 xterm + PTY（增量写 VT 序列，只重绘变化的格子）。所以 agent 会话读不到 transcript 时不再抓屏，而是给一个「用终端打开（xterm）」按钮。
- **tmux 路径只探测一次**：探测到的绝对路径（例如 macmini 的 `/opt/homebrew/bin/tmux`）会存进主机记录（`remote_hosts.tmux_path`），之后所有命令、以及终端模式的 PTY attach 都直接用它——之前 macmini 切终端会报 `zsh:1:1 command not found: tmux`，就是因为 attach 那条命令没带上解析出的路径。主机管理页会显示这个路径，没有时提示「tmux 路径待首次连通时记录」。
- **传输窗口自动适应**：不需要配参数——每次「发现会话」的拉取耗时 >8s 就把该主机的 tail 窗口减半（4MB→1MB 下限），耗时 <2.5s 就加倍回 4MB；实测 oracle（弱网）从 14–30s 降到 5–7s，消息条数不变。
- **慢网络**：远端 transcript 单次拉取有超时（`TMUX_WEB_TRANSCRIPT_TIMEOUT`，默认 30s；超时提示是「网络较慢」而不是解析失败），tmux 路径探测也限时（`TMUX_WEB_PROBE_TIMEOUT`，默认 10s，超时就退回裸 `tmux` 而不是报错），单次传输字节可调（`TMUX_WEB_REMOTE_PAYLOAD_BYTES`）。实测 oracle（单条 ssh 命令 2–4s）：首次发现会话 ~29s，之后走增量探测 0.9–7.6s。
- **agent 会话不会退化成刷屏终端**：transcript 读不到内容时（会话刚开、或解析不出来），聊天区显示说明 + 「查看原始屏幕」按钮并自动重试，不再自动切去流式抓屏（Kimi/Claude 这类全屏 TUI 每秒重绘整屏，600 行 × 7000 个节点会把浏览器卡死）。shell pane 仍然照旧显示屏幕。
- **Kimi 会话挑选**：同一个目录/标题可能有多个 kimi session，只挑 `updatedAt` 最新的那个（旧的往往是中断的回合，里面没有消息）。
- **切回主页不等待**：`/home` 先用 `localStorage` 里的上一份看板（标「显示缓存，正在更新…」）立刻画出卡片和会话树，再拉最新数据替换；没有缓存就显示骨架屏（统计/卡片/树的占位），不会空白。接口侧 `/api/home` 最多等 1.2 秒——收藏卡片的实时状态和最后一条消息要按 pane 走 SSH，慢的话先返回占位（`pending`），后台读完写进 25 秒缓存，下一次轮询补上。
- **会话树默认折叠**：控制台和主页的服务器树都从「全部展开」改成**只展开你点过的分支**（记忆在 localStorage），树上多了一排小按钮 **展开全部 / 折叠全部**；点某台服务器只展开它自己，不会把整棵树铺开
- **卡片一律先预览，出口在预览框里**：点任意卡片（收藏 / 最近访问 / 看板）就在卡片旁（手机从底部弹出）显示预览，框里三个按钮 **完整预览 / 聊天 / 关闭**；点框外或 Esc 也会关闭。卡片上原来那个 ⤢ 快捷键删掉了（和框里的「完整预览」重复）。
  这样手机上不会再出现「点一下就跳走/只能预览进不去对话」的分歧：点卡片永远只是预览，进对话是框里的「聊天」。
- **手机点卡片直接进对话**：以前手机上点卡片只会弹出悬停预览（触摸会先触发 `mouseover`，预览面板盖住卡片把点击吃掉了），现在悬停预览只给有鼠标的设备（`(hover:hover) and (pointer:fine)`）；手机上点卡片进控制台对话，卡片右上角的 ⤢ 才是完整预览，而预览页顶栏本身就有聊天/终端两个入口
- **聊天/终端切换在顶栏**：两个视图的切换按钮从侧边栏移到了顶栏图标区、紧挨收藏星（现在是「停 / 聊天 / 终端 / 星 / 分享 / 全屏」）。切换是纯图标（气泡 / 终端框），带 `aria-pressed`，手机 390px 下一行放得下；侧边栏不再有那组按钮。`/preview` 页也有同样的两个入口（聊天 / 终端），取代了原来和聊天入口重复的「在控制台打开」按钮
- **手机终端输入**：终端模式在手机上有一条自己的输入框（按键条上方）。它是**键盘代理**而不是输入缓冲：敲进去的字符立刻发给 PTY、然后输入框清空 —— 所以不会出现"光标左移插入一段、整行被重发"这种重复。退格发 `\x7f`、回车发 `\r`、中文输入法按 `compositionend` 提交；方向键 / Tab / Esc / Home / End / Delete 直接发给 PTY（在 bash/vim 里就是真的移动光标），粘贴整段也只会发一次。socket 还没连上时先攒着，连上立刻补发，第一个字符不会丢。xterm 自己的 textarea 被设成 `inputmode=none`，点终端只会收起键盘、不会误触发重绘
- **影响范围只有一个 window**：tmux 的尺寸是**按 window** 记的。手机打开某个 pane 的终端，只会把**那一个 window** 调成手机尺寸；同 session 的其他 window、其他 session、其他服务器都不受影响（实测 3 window 的 session + 另一个 session，只有被打开的那个从 179×51 → 47×39）。
- **离开后不会留下小窗口**：手机是最后一个客户端时，tmux 断开后会**保留**那个 47×40，之后谁看这个 pane（桌面 tmux、下次手机、只看 transcript）都是被压扁的 47 列。现在进入终端前记录原尺寸，最后一个客户端离开后恢复，且**只动这一个 window**：`resize-window -x/-y` 把尺寸写回去（它会顺手把 `window-size` 变成 `manual`），紧接着把这个 window 的 `window-size` 模式还原成进入前的值（通常是 `latest`，于是它会继续跟随客户端）。不碰 session 级的 `default-size`，所以不会牵连别的 window。
  远端同理，只是多一步：attach 前把原尺寸和模式写进 `/tmp/.tmuxweb-geom-<pane>`，客户端断开后由服务端再开一条 ssh 执行同样的恢复（杀掉 ssh 时远端 shell 往往来不及跑自己的收尾）
- **改了尺寸，tmux 必须收到 SIGWINCH**：终端 PTY 是 `openpty()` 建的，子进程只是继承 fd（`start_new_session` 也没有取得控制终端），所以内核**不会**因为 `TIOCSWINSZ` 给它发 SIGWINCH。以前 `resize` 只改 winsize，tmux 一直以为自己是旧尺寸、也就从不重画——手机上收起键盘后终端只剩原有那几行（或干脆只剩状态栏）。现在设完 winsize 会显式 `os.kill(pid, SIGWINCH)`，tmux 立刻按新尺寸重画整屏。桌面端改窗口大小也一并修好了
- **手机聚焦输入框不再整页放大**：iOS 会给 `font-size` 小于 16px 的输入框自动放大整页，而且失焦后不会缩回去（看起来就是"窗口变大了、终端没复原"）。现在手机断点下所有 `input/textarea/select` 都是 16px（控制台输入框、终端输入框、主页搜索、主机管理、分享/会话弹窗都覆盖）
- **组合键**：手机软键盘本来就打不出 Ctrl/Alt。按键条上的 `Ctrl`/`Alt` 是**前缀**开关——点一下再敲字符，就发出对应的控制字符（`Ctrl` + `c` = `\x03`），发完自动复位；外接键盘在输入框里按真实的 Ctrl/Alt 组合也会被翻译成控制字符（`Ctrl+V` 留给粘贴）
- **键盘弹出时终端只收缩、不跳变**：终端模式的顶栏在输入时不隐藏（旧行为会让终端多出 58px，看起来像整体变大），关闭键盘（失焦）时除了恢复可视高度还会重新 `fit` 一次，行列随即复原
- **键盘弹出不再"重新渲染"**：以前 `visualViewport` 变化（键盘动画期间会连续触发）+ `ResizeObserver` 每帧都 `fit()` 一次并 `xterm.refresh(0, rows-1)` 全屏重绘，每次 `fit()` 还会无条件给 PTY 发 `resize` —— tmux 收到行列变化就整屏重画。现在改成：布局立刻跟随可视高度（`--visual-height`/`--visual-top`，所以是"抬高"而不是重画），`fit` 只在行列真的变了才做一次并通知 PTY，`ResizeObserver` 合并成 90ms 一次，滚到中间的读者位置不被打断。实测键盘弹出/收起各只发 1 条 `resize`（`rows 41→20`）
- **登录保留深链**：未登录访问 `/?pane=...`、`/preview?pane=...` 会跳 `?next=`，登录后回到原地址（`next` 只接受站内路径，`//evil` 之类直接回 `/`）
- **进入首页**：没有 `?pane=` 也没有「上次打开的 pane」时，`/` 自动跳到 `/home`（有上次 pane 时仍直接恢复到那个会话，刷新不会把你踢回首页）
- **最近访问**：主页在「收藏」下面列出**最近打开的 4 个 pane**（点一下就回控制台）。记录来自控制台的选中动作（`POST /api/home/recent`），标题/主机/session 取自 `/api/hosts` 快照，状态取自状态扫描，所以这一段自己不读 transcript、几乎瞬时
- **busy/idle 怎么判**：按可信度从高到低取第一个有结论的来源（`/api/panes/status` 会带 `status_source` 告诉你是哪一个）：
  1. **agent 自己的会话文件**（`session`，最准）：每个 agent 在回合结束时都会写一条明确记录，只读文件末尾 64KB 的一条记录，不解析整份 transcript ——
     Claude `{"type":"system","subtype":"turn_duration"}` = 回合结束；Codex `{"type":"event_msg","payload":{"type":"task_complete"}}` = 结束、`task_started` = 正在跑；
     Kimi `{"type":"prompt.completed"}` = 结束。取「最近一条有结论的记录」，所以 codex 在回合结束后追加的 `thread_settings_applied` 之类不会把结论盖掉；
     会话文件超过 `TMUX_WEB_STATUS_STALE_SECONDS`（默认 600s）没写入还判 busy 的，按 idle 处理（中断的会话会永远停在 `agent.message.appended`）。
  2. **tmux 输出活跃度**（`activity`）：`#{window_activity}` 距今 ≤3s 视为正在输出（`TMUX_WEB_ACTIVITY_BUSY_SECONDS`）。不用额外命令，跟着 list-panes 一起拿。
  3. **屏幕文本**（`screen`，兜底）：末 14 行里出现 `esc to interrupt` / 转轮 / 带省略号的进度词 → 忙；提示符或 agent 输入框 → 闲；都不是 → 灰点 unknown。
  实测这套改动之前，3 个 Kimi pane 因为状态栏写着 `Kimi K3 thinking` / `K3.1 Pro thinking: high` 被误判成 busy，现在全部 idle。
  远端主机：`/api/panes/status` 会用**每台主机一条 ssh** 让远端脚本按同一套规则（同样是 shell 实现，不需要远端 python3）回答所有 agent pane，各主机并行探测；72 个 pane 一次扫描 4.5s。
- **对话右上角的五角星**：收藏/取消收藏当前打开的 pane，状态与侧边栏列表里的星标、主页卡片联动；对话预览页（`/preview`）头部也有同一个按钮
- **搜索**：先匹配「服务器 / tmux session / pane 名」，再搜各 agent **最近的对话内容**，最后是看板卡片；结果按这四组分开显示并高亮命中片段
  - 支持**正则**：查询里有正则元字符（`. * + ? [ ] ( ) { } | ^ $ \`）就按正则跑，否则按字面匹配；正则写错会自动退回字面匹配（结果页会标出来）
  - 例：`^codex` 只看以 codex 开头的 session，`claude|codex` 两者都算，`eth.*iter` 匹配中间任意内容
  - 对话是逐个 pane 读的，所以有 4 秒预算 + 25 秒缓存；没扫完会标「对话还在扫，结果可能不全」
  - 顶栏只留搜索框；「远程主机 / 管理分享 / 刷新」和控制台一样放在侧边栏顶部（图标按钮）
- **统计条**：panes / busy / idle / 收藏数 / 看板卡片数
- 每 15 秒自动刷新；手机上单列布局

### 让 agent 自己往主页贴东西

MCP 新增三个工具（stdio 与 HTTP `/mcp` 都支持）：

| 工具 | 作用 |
| --- | --- |
| `publish_home_card` | `title`（必填）、`body`、`pane`、`kind`（note/result/alert/link）→ 在主页生成一张卡片 |
| `list_home` | 读取主页：收藏（含状态与最后一条消息）+ 卡片 |
| `remove_home_card` | 按 id 删除自己发布的卡片 |

```
publish_home_card(title="eth 任务已收敛", body="ITER5 结论已写入 notes，等 review", kind="result")
```

数据存在 `TMUX_WEB_HOME_DB`（默认 `home.db`），收藏与卡片都只写这一张表，不依赖 tmux。

## 分享会话

管理员可以在任意 pane 上创建分享：agent 会话按结构化消息渲染，纯 shell pane 退化成屏幕文本（可滚动、可继续输入）；分享在 `/shares` 管理：

- 过期时间
- 分享密码
- 续期
- 密码重置或清空
- 提前停止
- 永久删除

创建时可勾选「允许访客继续对话」（默认勾选）。不勾选就是**只读分享**：分享页仍能看到完整历史和后续更新，但输入框被替换为只读说明，`POST /share/<slug>/api/input` 直接返回 403，无法向该 pane 写入任何内容。`/shares` 列表会标注该分享是「可对话」还是「只读分享」。

访客只获得绑定会话的 transcript（以及勾选时可发送消息的权利），不能访问主机树、终端或管理 API。

## MCP 邮箱（可选）

```bash
chmod +x install.sh mailbox.py mcp_server.py
./install.sh
```

它为 Claude Code 和 Codex 安装固定身份的 `hermes-mailbox` MCP 服务，提供：

- `send_message`
- `receive_messages`
- `reply_message`

本地命令示例：

```bash
./mailbox.py send claude '请审查当前方案' --topic review
./mailbox.py inbox
```

## 跨服务器 agent 中继 MCP

TmuxWeb 自带一个 MCP 端点 `POST /mcp`（Streamable HTTP）。客户端只需 URL + token，**不需要** python3、
不需要本仓库的路径，升级只要重启服务端：

```
URL:     http://<host>:8787/mcp
Header:  Authorization: Bearer <TMUX_WEB_API_TOKEN>
```

覆盖所有受管主机（含 SSH 远端）：

| 工具 | 作用 |
| --- | --- |
| `list_agents` | 列出所有主机上的 agent pane（target / 主机 / CLI / 空闲或忙碌 / 目录 / 标题） |
| `ask_agent` | 向某个 pane 提问并等待回答（busy 快速失败、可 `force`、timeout 5–600s） |
| `send_agent` | 只输入文字，不等回答（`submit=false` 就只打字不回车） |
| `send_file` | 把本机文件送到对方 pane 目录的 `upload_files/`，并把绝对路径告诉它 |
| `create_session` | 在任意主机开 tmux 会话：`name` + 可选 `host`/`path`/`agent`/`agent_args`，目录不存在会建，返回 `#N` 编号 |
| `kill_session` | `pane=#125` 只关那个 pane（会话还有别的 pane 就活着）；`session=名字` 才关整个会话 |
| `read_agent` | 读它的屏幕（默认）或结构化 transcript（`transcript=true`） |

**回车这件事**：`send_agent` / `ask_agent` 先用 `paste-buffer` 写入，再单独发 `C-m`。粘贴大的
prompt 时 TUI 可能还在处理，第一个回车会被吃掉——表现就是「文字确实进了输入框，但没发出去」。
现在的做法是**看屏幕**：按下回车后 0.45s 比对一次抓屏，屏幕没动就再按一次（最多 3 次；空输入
框上多按一次回车是空操作）。等待时间本身也按文本长度缩放（0.12s + 1 字符 / 2500，上限 1.8s），
远端走 ssh 时再多给一点。

**开会话/关会话**：`create_session` 就是控制台那个「新建会话」，`agent` 选了就直接把 agent 拉起来
（可带 `agent_args`、`keep_open`），返回里带 `pane` 与新分配的 `#N`，后面 `send_agent`/`read_agent`
直接用这个 `#N` 就行。`host` 既接受主机 id 也接受主机名（`oracle`、`macmini`）。

网页端也有入口：顶栏垃圾桶图标「关闭这个窗格」，点了先弹二次确认（会显示 `#N` 和 `session:window`），
确认后调 `DELETE /api/panes/{pane}`，只关这一个；关完如果同会话还有别的 pane 会自动落到它上面，
最后一个则显示「窗格已关闭，会话也随之结束」。

**聊天为什么不刷新，以及现在怎么解决**：聊天视图读的是 agent 自己的会话记录（hermes 的 `state.db`、
claude/codex/kimi 的会话文件），而**一条助手消息是这一轮跑完才写进去的**。agent 在中间调了十几个工具、
跑了几分钟，聊天里自然什么都没有——看着就是"卡住不刷新"。

现在补了两件事：

- **实时尾巴**：只要最后一条是用户消息（这一轮还没结束），聊天底部显示一块淡色的
  `agent 工作中 · 实时输出`，内容是 pane 最后 12 行，1.5 秒刷一次，**只有 digest 变了才重绘**。
  这样能看到它正在干什么，而不是干等。
- **探活不重发**：轮询带上 `sig=`，服务端算出签名一样就回 `{"unchanged": true}`——实测 300 条消息
  的完整响应 85KB，探活只有 **127B**（省 99.9%）。轮询节奏也分层：跑任务时 2s、空闲 30 秒内 1.5s、
  之后 3s、5 分钟以上 5s（原来是固定 1s 永不放手）。

**上传附件是分块的**：4MB 一块 `PUT /api/panes/{pane}/files/upload?offset=&total=&token=`，服务端先写
`.<token>-<名>.part`，最后一块落地才改名成目标文件——别人永远看不到半个文件。好处三条：

- 条带上显示真实百分比（`上传中 42%`），不再是干等；
- 断线只丢一块，同一块自动重试 3 次（退避 0.5s/1s/1.5s）；彻底失败时附件留在条带上显示
  `上传失败 · 点这里重试`，已传的块还在服务端，点一下从断点继续（服务端用 `409 + offset` 告诉客户端它手上有多少字节）；
- 单请求最大 4MB，天然绕开 Cloudflare 代理域名的 100MB 请求体上限，走 `tmux.122990.xyz` 也能传大文件。

文件面板上传走的是同一个 `TmuxUpload`，所以行为一致，只是多一条目录刷新。

**发送失败不会吃掉你打的字**：消息 POST 失败时文字会放回输入框，状态显示「发送失败，点发送重试」，
修好网络直接再点发送即可。

**切换模型就在对话里**：附件按钮右边有个齿轮下拉，按 provider 分组显示这个 agent 现在用的模型
（`OpenAI · gpt-6-luna`、`ctyun · gpt-5.5`…），选中后控制台替你把 agent 自己的 `/model <id>`
敲进去——不是新协议，就是一行文本。

- 当前模型和 provider 都是**读出来的**，不是猜的：claude / codex / kimi 从各自会话文件最后一条
  记录里取，hermes 读 `state.db` 的 `sessions.model`，provider 取 `config.yaml` 的 `model.provider`
  （远端由探测脚本随 header 回传 `TMUXWEB_MODEL` / `TMUXWEB_MODEL_PROVIDER`）。
- **Hermes 的模型清单是真清单，而且按 provider 分组**：`config.yaml` 里每个 provider 都是 OpenAI
  兼容端点，控制台逐个问 `/v1/models`（本地直连、远端在对方机器上跑同一段脚本），缓存 15 分钟。
  菜单里每组可折叠，默认只展开当前用的那组（本机 axera 19 个 + ctyun 50 个）；顶部有筛选框。
- 跨组切换发的是 `/model <id> --provider <name>`（Hermes 自己的语法），同组切换就是 `/model <id>`。
  拿不到清单时退回「当前 + 自定义」，不会卡住界面。
- claude / codex 用别名/模型 id 作为预设，其余用「自定义…」直接输入。
- **Kimi Code 例外**：`/model <任何东西>` 都回 `Unknown model alias`，它只认无参的 `/model` 打开自己的菜单，
  所以那里显示「打开模型菜单…」，点了只是把 `/model` 敲进去，选择在 agent 里用方向键完成。
- 接口：`GET /api/panes/{pane}/models`（`provider`/`current`/`presets`/`picker_only`）、
  `POST /api/panes/{pane}/model`（`{"model": "opus"}`，kimi 传空即可）。
- 没有模型的证据就留空，不硬编一个默认值——显示空总比显示错的好。

**关的时候先想 pane，不要整会话**：一个会话里常有好几个 agent，`kill_session` 传 `pane`
（`#125` 或 `host~%12` 都行）只关那一个，返回 `session_alive` 告诉你会话是否还在；传 `session`
才是整会话连锅端。对应的 HTTP 接口是 `DELETE /api/panes/{pane}` 和 `DELETE /api/sessions/{name}?host=`。

**传文件**跨主机设计：字节经控制台转一次，落到目标 pane 当前目录的 `upload_files/`，然后给那个
agent 打一行 `[file] <绝对路径> (N bytes)`。两台机器**不需要**共享挂载，也不需要互相信任 ssh。

```json
{"tool": "send_file", "arguments": {"target": "#125", "path": "/home/me/report.md",
                                    "note": "先看这份", "notify": true}}
```

上限默认 64MB（`TMUXWEB_SEND_FILE_MAX`），文件名不允许带路径分隔符，`notify=false` 就只落盘不打扰对方。

安装：

```bash
./install_relay_mcp.sh --dry-run   # 预览（token 打码）
./install_relay_mcp.sh             # 默认 auto：能连上就 HTTP（claude/codex/hermes/kimi 全走 URL）
./install_relay_mcp.sh --http      # 强制 HTTP
./install_relay_mcp.sh --stdio     # 兼容模式：本机 stdio，客户端各自 spawn python3 mcp_relay.py
./install_relay_mcp.sh --hub user@robo   # 连不上 8787 的机器：自动改成 stdio over ssh
./install_relay_mcp.sh --uninstall # 卸载（带 --hub 时连 hub 上的 wrapper 一起清）
```

默认是 `auto`：先探测 URL（`curl ${URL}/`，303/401 都算通）——通就用 HTTP；不通且给了 `--hub`，就把
wrapper + 0600 的 `relay.env` 装到 hub 上，本机改成 `ssh <hub> ~/.local/bin/tmuxweb-relay-stdio`
这种 stdio over ssh 注册，一个端口都不开（token 走 stdin 落到 hub 的 0600 文件，不进 argv）。

脚本做的事（也可手工）：

```bash
# Claude Code
claude mcp add -s user tmuxweb --transport http http://host:8787/mcp --header "Authorization: Bearer <token>"
# Codex（CLI 只写 url，脚本随后把 http_headers 合并进 ~/.codex/config.toml）
codex mcp add tmuxweb --url http://host:8787/mcp
# Kimi Code（~/.kimi-code/mcp.json）
{"mcpServers": {"tmuxweb": {"url": "http://host:8787/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
# Hermes（HTTP）：token 先写进 ~/.hermes/.env 的 MCP_TMUXWEB_API_KEY，add 就不会追问 token
printf 'y\ny\n' | hermes mcp add tmuxweb --url http://host:8787/mcp --auth header
```

**多台机器/多个 MCP 时先看网关**：这台机器上跑着一个 [MCPHub](https://github.com/samanhappy/mcphub)
（`~/mcphub`，端口 3300），TmuxWeb 的 relay 作为它的一条上游。各家 agent 只配一次
`http://<本机>:3300/mcp`，以后新增别的 MCP（fetch、数据库、别的内部服务）在 hub 里加一条即可，
客户端不用改；只想给某个服务器单独开路由时用 `/mcp/<server>`（工具名保持原样）。

**探测一律测直连**：有 HTTP 代理的机器上，代理会把别的网段答成 200/502，把真正可达的地址
掩盖掉（实测 ctyun：直连 tailnet `100.123.123.3` 通、直连局域网不通，而代理恰好反过来）。
所以 `relay-endpoint.sh` / `install_relay_mcp.sh` 的探测都带 `--noproxy '*'`，按 tailnet →
物理局域网 → 127.0.0.1 的顺序取第一个**直连**能通的。客户端自己带代理（比如 Hermes 的 httpx
读 `http_proxy`）时，把目标 IP 加进 `NO_PROXY` 即可；改不动客户端环境才退到 `mcp_relay.py`
这类 stdio 桥。

**地址约定**：用的不是"Tailscale"，而是**两台机器之间有没有一条互通的私有网段**。Tailscale、EasyTier、
ZeroTier、WireGuard、子网代理、一个物理局域网——对 TmuxWeb 都是同一件事：能不能双向访问到 `8787`。
所以配置里写的是那个网段里的地址（`http://100.x.y.z:8787`、`http://10.x.y.z:8787` 都行）。

`scripts/relay-endpoint.sh` 负责这件事，按「显式 `TMUXWEB_URL` → overlay 网卡（tailscale / easytier /
zerotier / wireguard）→ 物理局域网默认路由地址 → `127.0.0.1`」的顺序**逐个探测**，取第一个真能通的；
`install_relay_mcp.sh` 直接用它，所以装了 EasyTier 而没装 Tailscale 的机器一样是零配置。

只有**必须经跳板机**的主机才另说（例如 `oracle` 走 `aliyun` 跳，两个网段互不可见）：那种情况在
`remote_hosts` 里配 `jump_host_id`，relay 侧仍是同一条 URL；如果连"从它自己的网络里连回来"都做不到，
就退到 `--hub`（stdio over ssh）或 `scripts/reverse_tunnel.sh`。

**装在另一台机器上**（比如 Mac mini 的 Hermes）：只要那台机器能访问到这个 URL 就行，不需要本仓库、不需要 python3。

```bash
# 1) token 落到 ~/.hermes/.env（0600，别贴进 shell 历史）
printf 'MCP_TMUXWEB_API_KEY=%s\n' '<token>' >> ~/.hermes/.env && chmod 600 ~/.hermes/.env
# 2) 注册（两个提示都答 y：requires auth / enable all）
hermes mcp add tmuxweb --url http://100.x.y.z:8787/mcp --auth header
# 3) 验证
hermes mcp test tmuxweb        # ✓ Connected · 13 tools
```

判断通不通：`curl http://<那个地址>:8787/` 回 303（未登录跳转）就是通，`/mcp` 不带 token 回 401 说明
鉴权正常。别用 MagicDNS 短名（实测 `http://robo:8787/` 回 502），写 IP。

### 机器连不回来时：反向隧道 / stdio over SSH

tailnet 是首选。**如果那台机器既不在 tailnet、又从外面连不进来**（出站 NAT、只有它主动 dial 的权限），
有两条路，都不需要改远端 sshd 配置：

**1) 反向端口转发**（本机主动 dial 远端，把本机的 8787 挂到远端的 127.0.0.1:8787）：

```bash
./scripts/reverse_tunnel.sh marscrazy@远端地址            # 默认 -R 8787:127.0.0.1:8787
./scripts/reverse_tunnel.sh marscrazy@远端 --ssh-port 2222 --identity ~/.ssh/id_ed25519
./scripts/reverse_tunnel.sh marscrazy@远端 --print-systemd   # 本机常驻用
./scripts/reverse_tunnel.sh marscrazy@远端 --print-launchd   # macOS 常驻用
./scripts/reverse_tunnel.sh oracle-embedded@host --jump root@aliyun   # 只能经跳板机进的
```

`--jump` 可以重复（多级跳板按顺序拼成 ProxyJump），systemd / launchd 模板里也会带上 `-J`。

- 脚本有 `autossh` 就用它，没有就自己循环重连（指数退避，最长 30 秒）；`ExitOnForwardFailure=yes`
  保证转发没建起来时立刻报错，`ServerAliveInterval=20`、`CountMax=3` 保证假死能发现。
- 远端监听地址默认 `127.0.0.1`（构建在远端 loopback 上），**不需要 `GatewayPorts`**，端口也不暴露给
  远端的局域网；改成 `0.0.0.0` 才需要。
- 远端 MCP 客户端这时写 `http://127.0.0.1:8787/mcp`，token 照旧必需（隧道只解决可达性，不解锁鉴权）。

**2) stdio over SSH**（一个端口都不开）：MCP 的 stdio 传输本身就是 stdin/stdout，让客户端把
`ssh` 当传输层，relay 在**本机**执行、只把 JSON-RPC 通过 SSH 管道送回去：

```bash
# 本机：wrapper 里读 0600 的 env，别把 token 写进 argv（argv 在 ps 里人人可见）
install -d -m 700 ~/.config/tmuxweb
printf 'TMUXWEB_URL=http://127.0.0.1:8787\nTMUXWEB_TOKEN=%s\n' '<token>' > ~/.config/tmuxweb/relay.env
chmod 600 ~/.config/tmuxweb/relay.env
cat > ~/.local/bin/tmuxweb-relay-stdio <<'SH'
#!/bin/sh
set -a; . "$HOME/.config/tmuxweb/relay.env"; set +a
exec python3 "$HOME/workspace/TmuxWeb/mcp_relay.py"
SH
chmod +x ~/.local/bin/tmuxweb-relay-stdio

# 远端（连不回来的那台）：客户端 spawn ssh，relay 在本机跑
hermes mcp add tmuxweb --command /usr/bin/ssh --args <user>@<本机看得到的地址> /home/<user>/.local/bin/tmuxweb-relay-stdio
```

这条路适合“远端只有 ssh 出站权限”的场景，好处是不开监听端口、不用管隧道守护；代价是每次
MCP 调用都走一条 SSH 会话（Hermes 会复用连接，实测握手一次十几毫秒）。

没有 MCP 支持的场景（脚本、shell）可以用 `relay_client.py`，或用 stdio 版 `mcp_relay.py`（`--selftest` 自检）。
内网/Tailscale 部署可以彻底免掉 header：在 `.env` 设 `TMUX_WEB_MCP_AUTH=off`（默认 `on`），`/mcp` 不再校验 token，客户端只填 URL；此时任何能访问该端口的人都能操作 pane，启动日志会打一条警告，`/api/*` 与网页登录仍按原样校验。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

完整的自动化测试、真实 tmux、远程 SSH、移动端和公网反向代理验收方法见 [TESTING.md](TESTING.md)。

## 放在 Cloudflare / 反向代理后面

分享链接**不需要**配置域名：`/api/shares` 返回的是相对路径（`/share/<slug>`），分享页里的请求也都是相对地址，
浏览器会自动用你访问时的域名。所以绑 CF 域名后不用往 `.env` 里写域名。

但走代理后有两件事值得显式设置（都在 `.env`）：

```bash
# 1) 让登录限速按真实客户端计数，并让 cookie 依据 X-Forwarded-Proto 判定 Secure
TMUX_WEB_TRUST_PROXY=1
# 2) 可选：只允许自己的域名访问（127.0.0.1 / localhost 始终放行），防止别人的域名指到这台机器
TMUX_WEB_ALLOWED_HOSTS=chat.example.com
```

- 不开 `TMUX_WEB_TRUST_PROXY` 时，所有请求的客户端地址都是 CF 的出口 IP：登录限速会变成"全站共享 5 次失败"，
  几个人同时试错就会把所有人锁 60 秒；cookie 也可能因为没有 `Secure` 而在 HTTPS 下仍可被明文回传。
- 如果反代终止 TLS 但没传 `X-Forwarded-Proto`，可以退一步直接 `TMUX_WEB_SECURE_COOKIES=true`。
- WebSocket（实时聊天/终端）CF 默认支持；`/ws/*` 不需要额外配置。
- MCP 端点 `POST /mcp` 同理走同一域名，客户端 URL 写 `https://chat.example.com/mcp`。

### 直连：同一局域网 / Tailscale 里绕开 Cloudflare

CF 隧道对单请求限制 100MB 和 100 秒；同一网段或同一个 tailnet 里的人其实可以直连这台机器，传输不走 CF，
文件也不离开内网。控制台只负责把地址列出来，**要不要用由你决定**：

1. 打开 设置 › 直连地址，里面是本机所有可用入口（局域网 IP、Tailscale IP，以及起了 `tailscale serve` 之后的
   `https://<node>.<tailnet>.ts.net`），每行一个复制按钮。
2. 想给 tailnet 一个带真证书的 https 入口（浏览器不会报混合内容）：

```bash
sudo tailscale set --operator=$USER        # 只需一次
./scripts/enable_direct.sh serve           # tailscale serve --bg 8787
```

   tailnet 管理台里要先把 HTTPS 证书打开（DNS 页 → Enable HTTPS）。
3. 局域网内直接 `http://<本机IP>:8787` 即可（明文 http，只在可信内网用）。

`.env` 里的 `TMUX_WEB_PUBLIC_URL` 写公网入口（例如 `https://chat.example.com`），设置面板就能判断并提示
"当前页面走 Cloudflare"，同时把内网地址排在前面。

#### 用户还是只记一个地址：传输自己挑路

让人记第二个域名是不现实的（cookie 是 host-only，换了域名还得重新登录），所以页面永远从公网域名加载，
**只有文件字节**会改走直连源站：

1. 页面探测一遍可用的 https 直连源站（`/t/ping`，1.5 秒超时），通了就记住 24 小时；
2. 上传/下载前向控制台要一张票据：`POST /api/transfer/ticket`（需要登录），
   票据绑定 pane、路径、方向、大小上限，10 分钟过期；
3. 字节走 `PUT /t/upload?t=…`、`GET /t/download?t=…` —— 这两条路由只认票据，不认 cookie；
4. 探测不到、票据拿不到、直连断了：一律静默回落公网路径，用户看到的还是同一个下载按钮。

票据路由只挂 `/t/*`，其余接口在直连源站上一样要登录；用量也记账（下载 8 次、上传按块数），
所以链接即使泄露也只能动那一个文件，而且很快失效。

```bash
# 可选：额外的传输中转（aliyun 反代之类），逗号分隔；只有 https 会被采用
TMUX_WEB_TRANSFER_ORIGINS=https://transfer.example.com
# 可选：票据签名密钥，默认由 TMUX_WEB_SESSION_SECRET 派生
TMUX_WEB_TRANSFER_SECRET=…
```

注意 https 页面没法向 `http://` 的源站发 fetch 或下载（混合内容会被浏览器直接拦掉），
所以直连源站必须是 https —— 要么 `tailscale serve`，要么给内网地址签一张真证书。

## 数据与隐私

以下内容不会进入仓库，并且不应手工提交：

- `.env`
- `*.db`、`*.db-wal`、`*.db-shm`
- `.remote_hosts.key`、私钥、密码
- `tmux-panes.json`
- Cookie、日志、终端 capture、AI transcript

结构化 transcript 默认从各 CLI 自己的本地状态读取，不会复制进项目目录。

## 已知限制

- 远端 transcript 不依赖 python3；Hermes 需要 `~/.hermes/sessions/<id>.jsonl` 或 `sqlite3`（PATH、conda 常见路径、`bash -lc` 都会尝试），两者都没有时用 Hermes 自带的 python3 runtime（`~/.hermes/tools/python-*/bin/python3`）通过 stdlib `sqlite3` 导出，仍不行才回退 capture。
- 中继是同步请求：调用方要等目标 agent 回答，最长 300 秒；同一 pane 同时只允许一个中继请求。
- 中继依赖目标 agent 遵守标记协议；不遵守时超时并返回 pane 尾部，便于人工判断。
- 分享要求可稳定识别 provider session；仅 capture 的 pane 不能创建分享。
- 管理员登录目前是单用户共享密码。
- 第三方前端依赖保存在 `web/vendor/`，便于无 CDN 部署；其版权声明保留在文件中。

## 许可证

[MIT](LICENSE)
