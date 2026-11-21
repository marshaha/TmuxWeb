# 零配置公网地址

手机在别的网络里要打开这个控制台，以前得三选一：`tailscale serve`（要证书、要 sudo）、
Cloudflare 账号 + 自建 tunnel（要配置）、路由器端口映射（要动网络）。现在点一下就好。

## 怎么用

设置 › **公网地址（零配置）** › 「开一个临时地址」。几秒后出现一个
`https://<随机词>.trycloudflare.com`，手机直接打开它。

**这只改变可达性，不改变认证** —— 登录页照旧要密码，API 照旧要 token。

## 服务端

    GET  /api/tunnel                  {enabled, binary, running, url, error, target, tailscale}
    POST /api/tunnel/start            起 cloudflared 快速隧道，等它吐出 URL（默认 25 秒）
    POST /api/tunnel/stop             关掉并收掉进程

* 用 `cloudflared tunnel --url http://127.0.0.1:8787`，URL 从它自己的输出里读，不猜；
  正则要求域名真的结尾（`x.trycloudflare.com.attacker.net` 不算）。
* 进程归服务管：服务停了隧道跟着停（退出时收掉），不留挂着旧地址的孤儿。
* 端口取 `TMUX_WEB_PORT` / `PORT` / 8787。
* 关掉这个功能：`TMUX_WEB_TUNNEL=off`（`/api/tunnel/start` 返回 409）。
* 二进制不在 PATH 上：`TMUX_WEB_CLOUDFLARED=/path/to/cloudflared`。

## tailnet 优先

状态里同时报 `tailscale`：如果是本机在 tailnet 里，`http://100.x.x.x:8787` **不需要任何隧道**，
比绕 Cloudflare 快得多。设置面板里直接把那个地址列出来，先试它。

只列本机地址（`Self.TailscaleIPs`）——把对端机器的 IP 列出来会让人以为能从那些地址连进来。

## 为什么默认不自动开

公网可达是一个安全决定，不该由"启动服务"替你做出。默认状态是关着、不占资源、不暴露；
要开是人的一次明确动作。
