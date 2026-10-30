# 插件（plugins）

TmuxWeb 的插件是「一个目录 + 一段 Python」，在服务进程内运行，用来加**功能接口**，而不是改核心。
第一个搬进去的是**跨服务器通信（relay）**：它的四个 HTTP 端点现在就在 `plugins/relay/`，关掉这个插件，
这些端点就不存在了——这正是插件系统的意义：功能可以整个不要。

```
plugins/
  relay/          # 跨服务器 agent 通信（默认启用，第一方）
    plugin.toml
    plugin.py
  example/        # 模板 / 复制起点（默认不启用）
```

## 写一个插件

1. 建目录 `plugins/<名字>/`，`plugin.toml`：

   ```toml
   name = "my-thing"          # 必须和目录名一致
   version = "1.0.0"
   api = 1                     # 编写时对应的插件 API 版本
   entry = "plugin.py"
   description = "一句话说明，会出现在 /api/plugins"
   default_enabled = false     # 只有第一方插件才设为 true
   ```

2. `plugin.py` 里给一个 `Plugin` 子类，类名随意（也可以导出一个叫 `PLUGIN` 的实例/类）：

   ```python
   from fastapi import Request
   from plugin_api import Plugin

   class MyThing(Plugin):
       def setup(self, ctx):
           @ctx.app.get("/api/my-thing/ping")
           async def ping(request: Request):          # 记得标注类型，否则会被当成查询参数
               ctx.require_auth(request)          # 一定要调，插件有完整权限
               return {"ok": True, "cfg": ctx.config}
   ```

3. 启用：`TMUX_WEB_PLUGINS=my-thing`（见下），重启服务。`GET /api/plugins` 能看到它的状态。

可用的东西（`ctx`）：

| 字段 | 说明 |
|---|---|
| `ctx.app` | FastAPI 应用，直接挂路由 |
| `ctx.require_auth(request)` | 与控制台相同的会话校验 |
| `ctx.config` | 本目录 `config.json` + `TMUX_WEB_PLUGIN_<NAME>_CONFIG`（JSON）合并结果 |
| `ctx.data_dir` | 本插件可写目录（自动创建） |
| `ctx.log` | 带插件名前缀的 logger |
| `ctx.on_shutdown(fn)` | 注册退出钩子 |
| `ctx.services` | 核心能力：`tmux` / `tmux_error` / `remote_hosts` / `home_store` / `relay_log` / `relay_ask` / `relay_target` / `collect_pane_states` |

`ctx.services` 之外的任何内部实现都可能变动——插件只应该依赖上面这张表。

## 在设置面板里开关（推荐）

设置 → 插件：每个插件一行（名称、版本、描述、错误），右侧开关即时生效，状态存在仓库根的 `plugins.json`
（0600，已 gitignore）：

- **关掉**：模块不再重载，但它的端点立刻返回 404，其它功能不受影响。
- **打开**：如果是第一次打开，会当场 import + `setup()` 并挂上路由。
- 面板里的选择**优先于** `TMUX_WEB_PLUGINS`。

## 用环境变量启用与关闭

`TMUX_WEB_PLUGINS` 环境变量（放 `.env` 里）：

| 值 | 含义 |
|---|---|
| 不设 / 空 | 只加载 `default_enabled = true` 的插件（第一方，目前只有 relay） |
| `all` | 加载磁盘上所有插件 |
| `a,b` | **只**加载这两个（不含第一方默认；要连 relay 一起就写 `relay,a` 或 `all`） |
| `all,-a` | 除 a 以外全部 |
| `none` | 一个都不加载 |

插件是**进程内代码**，权限和你启动服务的用户一样。默认只开第一方插件、默认不开未知代码，就是出于这个原因。

## 出错了怎么办

- 单个插件加载失败**不会**影响服务启动：错误记在日志里，并在 `GET /api/plugins` 的 `error` 字段里显示。
- 启动日志：`plugins` 子 logger，形如 `plugin relay 1.0.0 loaded`。

## 两个容易踩的坑

1. **处理函数的参数要有类型注解**：`async def ping(request: Request)` —— 写成 `async def ping(request)`
   会被 FastAPI 当成必填的查询参数，请求直接 422。
2. **别在插件模块顶部写 `from __future__ import annotations` 再从函数里局部导入模型**：注解会变成字符串，
   FastAPI 用模块全局去解析，局部名字找不到，同样变 422。要么把模型放在模块级导入，要么别用 future 注解。

## 目前还没有的

- 插件往 MCP 里注册工具、往侧边栏加按钮、独立的静态资源目录。
- 沙箱隔离：插件和主进程同权限、同内存空间。
