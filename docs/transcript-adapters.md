# transcript 适配器

读一个 agent 的聊天记录，各个 CLI 的差异是实打实的：会话文件在哪、怎么认进程、
远端 tail 要不要反转行序、模型名写在文件里还是数据库里。以前这些散在
`transcript_reader.py` 的 if/elif 里，加一个 CLI 要改四处。

现在是一张表：`transcript_reader.ADAPTERS`。

```python
register(Adapter("kimi", read_kimi, parse_kimi,
                 command_prefixes=("kimi",), argv_names=("kimi",),
                 remote_reversed=True, takes_hint=False, takes_screen=False,
                 note="~/.kimi/sessions/**/wire.jsonl，解析器吃 newest-first"))
```

## 字段都是声明出来的

| 字段 | 作用 |
| --- | --- |
| `read` / `parse` | 本地读、以及解析远端拉下来的字节 |
| `command_prefixes` | `pane_current_command` 以什么开头算这个 agent（hermes 额外接受 `python`） |
| `argv_names` | 进程 argv 里的可执行名（`kimi-code`、`hermes-agent` 都算）；**名字不必等于 provider** |
| `payload_parsers` | 远端命名格式（hermes 的 `hermes-rows` 是整段 JSON 数组） |
| `remote_reversed` | 远端 tail 是 oldest-first，这个解析器要不要反转（claude / kimi 要） |
| `takes_title` / `takes_hint` / `takes_screen` | 读函数接受哪些参数 —— 声明式，不靠 try/except 猜 |
| `model_source` | `tail`（模型名在文件末尾）或 `sqlite`（hermes 在 `state.db`） |

## 加一个 CLI 要做什么

1. 在 `transcript_reader.py` 里写 `read_x` / `parse_x`；
2. 末尾 `register(Adapter("x", ...))`；
3. 在 `providers/x.py` 里补服务端那份事实（识别、idle/busy 标记等），
   `tests/test_provider_registry.py` 会盯着两边不许漂。

`provider_for_command` / `provider_from_argv` / `read_transcript` / `parse_remote_payload`
全都不认识具体 provider 名字了 —— 它们只查表。有一条测试专门证明这点：
注册一个 `fake` adapter 之后，识别、读取、远端解析立刻全都认它，没动任何分支。

## 接口

    GET /api/transcript/adapters

回一串纯数据（名字、命令前缀、argv 名、要不要反转、有哪些 payload 格式、模型来源）。
排障时先看它：远端读不出记录，八成是 `command_prefixes` 或 `remote_reversed` 对不上。

## 和 `providers/` 的关系

* `providers/` 是服务端的单一真相（识别、idle/busy、hook、resume）；
* `transcript_reader.py` 会被 `python3 -` 管道到远端执行，**必须自洽**，
  所以它保留一份表，而不是 import；
* 两者的命令前缀、provider 顺序、argv 匹配规则都有断言比对（`tests/test_transcript_adapters.py`
  和 `tests/test_provider_registry.py` 的 drift 用例）。
