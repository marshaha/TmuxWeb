# TmuxWeb 原生客户端（Flutter）

Android / iOS 原生客户端，直接调用 TmuxWeb 的 HTTP API —— **不是 WebView 套壳**。
登录、主机/pane 列表、对话、消息与块级复制、分享（含只读）都在 Flutter 里实现；网页版继续独立迭代。

## 实现了什么

| 能力 | 说明 |
| --- | --- |
| 登录 | 服务器地址 + 密码（`POST /login` 换 cookie），也支持 `TMUX_WEB_API_TOKEN` 走 Bearer；地址记住、密码不落盘 |
| 主机 / pane | 按主机分组，标注 provider 与「有结构化记录 / 纯终端」，下拉刷新 |
| 对话 | 2 秒轮询 transcript，发送后乐观占位；纯终端 pane 按屏幕文本渲染 |
| 消息级复制 | 每条消息「复制全部」，复制整条原文（含代码与表格 markdown 源码） |
| 块级控件 | 代码 / mermaid / 表格各有工具条：复制（表格复制为 TSV、mermaid 复制源码），表格可「源码 / 渲染」互切 |
| Markdown | 正文段落用 flutter_markdown 渲染（标题、列表、引用、行内代码、链接） |
| 分享链接 | 输入分享路径（可带密码）直接打开；`allow_input=false` 时隐藏输入框、显示只读横幅 |
| 分享密码 | 401 时自动切到密码表单，解锁后进对话 |

## 有意不做的（网页版继续提供）

- 终端模式（PTY attach）、文件浏览器（上传/下载/打包）、新建 tmux session、主机与分享管理
- mermaid 图形渲染：客户端只显示源码 + 复制（渲染需要额外 WebView/渲染引擎）

## 代码结构

```
lib/
  main.dart                    # MaterialApp → LoginScreen
  theme.dart                   # 与网页版一致的暖色浅色主题
  api/api_client.dart          # Api 接口 + ApiClient（cookie/Bearer、错误映射、pane 引用编码）
  models/models.dart           # Pane / Host / ChatMessage / Transcript / ShareMeta
  models/blocks.dart           # 消息切块（代码/mermaid/表格）与表格 TSV/markdown 互转（纯函数）
  models/chat_source.dart      # PaneChatSource / ShareChatSource（只读标志来自服务端 meta）
  screens/login_screen.dart    # 地址 + 密码/Token、记住地址、错误提示
  screens/hosts_screen.dart    # 主机树、打开分享链接、ShareScreen（分享解锁）
  screens/chat_screen.dart     # 轮询、消息列表、输入框、只读横幅、screen 模式
  widgets/message_tile.dart    # 用户气泡 / 助手卡片 + 复制全部
  widgets/block_view.dart      # 块工具条 + 代码/表格/源码渲染
test/
  blocks_test.dart             # 切块顺序、转义、表格解析与 TSV
  api_client_test.dart         # URL 规范化、cookie、pane 引用编码、403 只读、错误信息
  chat_screen_test.dart        # 渲染、复制、块切换、只读、发送成功/失败
  screens_test.dart            # 登录校验/错误/成功、Token 登录、主机树、分享只读
```

测试 44 个，全部离线：HTTP 用 `package:http/testing.dart` 的 `MockClient`，界面用 `FakeApi` / `FakeSource`，
剪贴板用 `SystemChannels.platform` 的 mock handler。

```bash
cd client
flutter pub get
flutter test
flutter analyze
```

## 构建产物（本机已出包）

| 文件 | 用途 |
| --- | --- |
| `build/app/outputs/flutter-apk/app-arm64-v8a-release.apk`（18.8MB） | 绝大多数手机 |
| `build/app/outputs/flutter-apk/app-armeabi-v7a-release.apk`（16.2MB） | 老 32 位机型 |
| `build/app/outputs/flutter-apk/app-x86_64-release.apk`（20.2MB） | 模拟器 |
| `build/app/outputs/flutter-apk/app-release.apk`（53.6MB） | 三 ABI 通用包 |
| `build/app/outputs/bundle/release/app-release.aab`（52.6MB） | Google Play 上传 |

签名用本地 keystore：`android/tmuxweb-release.jks` + `android/key.properties`（口令 600 权限，均不入库），
`apksigner verify` 显示 `CN=TmuxWeb`。正式发布请换自己的密钥。

```bash
cd client
flutter build apk --release --split-per-abi
flutter build appbundle --release
```

应用已声明 `INTERNET` 权限与 `usesCleartextTraffic="true"`（自建服务常是内网 http）。

## 构建 / 发布 iOS（GitHub Actions）

iOS 不能交叉编译，也不需要 Mac：`.github/workflows/mobile.yml` 在 GitHub 的 `macos-latest` 上编译。

- push / PR 改到 `client/`：`flutter test` → Android APK/AAB → iOS IPA
- 没有签名密钥时产出**未签名 IPA**（`tmuxweb-unsigned.ipa`），可人工校验或侧载（工具会重签）
- 打 tag（`v1.0.0`）或手动 `workflow_dispatch` 也会出包

配签名后自动上传 TestFlight，仓库 Settings → Secrets 需要：

| Secret | 内容 |
| --- | --- |
| `APPLE_CERTIFICATE_P12` / `APPLE_CERTIFICATE_PASSWORD` | Apple Distribution 证书（base64）与密码 |
| `APPLE_PROVISIONING_PROFILE` | App Store `.mobileprovision`（base64） |
| `APPLE_KEYCHAIN_PASSWORD` | CI 临时 keychain 口令 |
| `APPLE_TEAM_ID` | Apple Developer Team ID |
| `APP_STORE_CONNECT_API_KEY_ID` / `APP_STORE_CONNECT_API_ISSUER_ID` / `APP_STORE_CONNECT_API_KEY_P8` | App Store Connect API Key |

缺任一 iOS 签名 secret 时会自动走未签名路径，不会让 workflow 失败。

审核注意：`ios/Runner/Info.plist` 默认开了 `NSAllowsArbitraryLoads`（连自建内网 http 需要）；
只面向公网用户时建议上 HTTPS 并删掉这个例外。

## 与服务端的约定

```
POST /login                              密码换 cookie（303 + Set-Cookie）
GET  /api/hosts                          主机 + pane 树
GET  /api/panes/{pane}/transcript        pane 的结构化记录（screen 模式为屏幕文本）
POST /api/panes/{pane}/input             发送（text, submit）
GET  /share/{slug}/api/meta              { mode, allow_input, title, ... }
POST /share/{slug}/login                 分享密码换 cookie
GET  /share/{slug}/api/transcript        分享历史
POST /share/{slug}/api/input             分享输入（只读分享返回 403）
```

`{pane}` 里的 `%` 必须编码：`%7` → `%257`，跨主机 `abc~%7` → `abc~%257`。
