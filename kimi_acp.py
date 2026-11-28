"""TmuxWeb-owned Kimi ACP session. One stdio connection owns each live request."""
import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading


from chat_ipc import MAX_FORM, MAX_FRAME, available, encode, foreground_owner, listener_paths, peer, query, string


def present(method, params):
    """Keep ACP option identities separate from their human-readable labels."""
    fields, values = [], {}
    if method == "session/request_permission":
        tool = params["toolCall"]
        string(tool["toolCallId"])
        details = []
        for block in tool.get("content", []):
            if block.get("type") == "content" and block.get("content", {}).get("type") == "text":
                details.append(string(block["content"]["text"], MAX_FORM))
            else:
                details.append(json.dumps(block, ensure_ascii=False))
        for key in ("rawInput", "locations"):
            if tool.get(key):
                details.append(json.dumps(tool[key], ensure_ascii=False))
        options = params["options"]
        if not isinstance(options,list) or not 1 <= len(options) <= 24:
            raise ValueError("unsupported permission choices")
        labels = [string(o["name"]) for o in options]
        ids = [string(o["optionId"]) for o in options]
        if len(set(ids)) != len(ids) or len(set(labels)) != len(labels):
            raise ValueError("ambiguous choices")
        # Scope must remain visible even if an agent uses a short option label.
        scopes = {"allow_once":"允许一次", "allow_always":"持续允许",
                  "reject_once":"拒绝一次", "reject_always":"持续拒绝"}
        is_question = tool["title"] == "AskUserQuestion"
        choices = labels if is_question else [label + " · " + scopes[o["kind"]] for label,o in zip(labels,options)]
        fields.append(dict(id="permission",title=string(tool["title"]),description="\n".join(details),
                           choices=choices,multi=False,other=False))
        values["permission"] = ids
        kind = "question" if is_question else "approval"
    elif method == "elicitation/create" and params.get("mode") == "form":
        schema = params["requestedSchema"]
        properties = schema["properties"]
        if (schema.get("type") != "object" or not 1 <= len(properties) <= 5
                or set(schema.get("required", [])) != set(properties)):
            raise ValueError("unsupported question schema")
        for key, prop in properties.items():
            if not re.fullmatch(r"q[0-9]+", key):
                raise ValueError("unknown Kimi question identifier")
            multi = prop["type"] == "array"
            if not multi and prop["type"] != "string":
                raise ValueError("unsupported property")
            options = prop["items"]["anyOf"] if multi else prop["oneOf"]
            if not 1 <= len(options) <= 24:
                raise ValueError("unsupported question choices")
            labels = [string(o.get("title") or o["const"]) for o in options]
            choices = [string(o["const"]) for o in options]
            if len(set(choices)) != len(choices) or len(set(labels)) != len(labels):
                raise ValueError("ambiguous choices")
            desc = [str(prop["description"])] if prop.get("description") else []
            desc += [label+" — "+string(o["description"]) for label,o in zip(labels,options) if o.get("description")]
            if not fields and params.get("message"):
                desc.insert(0, string(params["message"]))
            fields.append(dict(id=key,title=string(prop.get("title") or key),description="\n".join(desc),
                               choices=labels,multi=multi,other=False))
            values[key] = choices
        kind = "question"
    else:
        raise ValueError("unsupported ACP request")
    form = dict(provider="Kimi",kind=kind,revision=secrets.token_hex(32),questions=fields)
    if len(encode(form)) > MAX_FORM:
        raise ValueError("request too large")
    return form, values


class Owner:
    def __init__(self):
        self.session = ""
        self.pending = OrderedDict()
        self.completed = {}

    def register(self, ident, method, params):
        if (not self.session or params.get("sessionId") != self.session or len(self.pending) >= 16
                or any(item[0] == ident for item in self.pending.values())):
            raise ValueError("unbound request")
        form, values = present(method,params)
        self.pending[form["revision"]] = ident, form, values

    def handle(self, action, write):
        if action is None:
            result = {"status":"closed","session":self.session}
            if self.pending:
                result.update(status="open",form=next(iter(self.pending.values()))[1])
            return result
        revision = action.get("revision")
        if revision in self.completed:
            return {"status":"resolved" if self.completed[revision] == action["answers"] else "closed"}
        if not self.pending or revision != next(iter(self.pending)):
            return {"status":"closed"}
        ident, form, values = self.pending[revision]
        answers = action.get("answers", {})
        if set(answers) != set(values):
            return {"status":"invalid"}
        content = {}
        for q in form["questions"]:
            key = q["id"]
            answer = answers[key]
            indices = answer.get("indices")
            if (not isinstance(indices,list) or not indices or answer.get("other") is not None
                    or any(type(i) is not int or not 0 <= i < len(values[key]) for i in indices)
                    or len(set(indices)) != len(indices) or not q["multi"] and len(indices) != 1):
                return {"status":"invalid"}
            selected = [values[key][i] for i in indices]
            content[key] = selected if q["multi"] else selected[0]
        result = ({"outcome":{"outcome":"selected","optionId":content["permission"]}}
                  if "permission" in content else {"action":"accept","content":content})
        write({"jsonrpc":"2.0","id":ident,"result":result})
        del self.pending[revision]
        self.completed[revision] = answers
        if len(self.completed) > 64:
            del self.completed[next(iter(self.completed))]
        return {"status":"resolved"}


async def run(binary):
    pane, pid, path, listener = listener_paths("kimi")
    child = await asyncio.create_subprocess_exec(binary,"acp",stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE,limit=MAX_FRAME,
                                                 env=dict(os.environ,KIMI_CODE_NO_AUTO_UPDATE="1"))
    owner = Owner()
    def write(message):
        child.stdin.write(encode(message))
    turn = None

    async def prompt(text):
        try:
            await call("session/prompt",{"sessionId":owner.session,"prompt":[{"type":"text","text":text}]})
        except (ValueError,OSError) as exc:
            print(str(exc),file=sys.stderr,flush=True)
        finally:
            owner.pending.clear()
            print(flush=True)

    def start_prompt(text):
        nonlocal turn
        if not owner.session or turn is not None and not turn.done():
            return False
        turn = asyncio.create_task(prompt(text))
        return True

    async def serve(reader,writer):
        try:
            peer(writer.get_extra_info("socket"))
            msg = json.loads(await asyncio.wait_for(reader.readline(),2))
            if msg.get("pane") == pane and foreground_owner(pid,os.getpid()):
                action = msg.get("action")
                if action == {"binding":True}:
                    state_dir = os.environ.get("KIMI_CODE_HOME") or Path.home()/".kimi-code"
                    result = {"session":owner.session,"home":str(Path(state_dir).resolve())}
                elif isinstance(action,dict) and "prompt" in action:
                    text = string(action["prompt"],100000)
                    if text.strip() == "/model":
                        result = {"status":"unsupported","detail":"此 ACP 会话使用 Kimi 配置的模型。交互式 /model 需要终端模式。"}
                    else:
                        result = {"status":"submitted" if start_prompt(text) else "busy"}
                else:
                    result = owner.handle(action,write)
                await child.stdin.drain()
                writer.write(encode(result))
                await writer.drain()
        except (ValueError,OSError,KeyError,TypeError,AttributeError,asyncio.TimeoutError):
            pass
        finally:
            writer.close()

    pending = {}
    counter = 0
    async def call(method,params):
        nonlocal counter
        counter += 1
        ident = counter
        future = asyncio.get_running_loop().create_future()
        pending[ident] = future
        try:
            write({"jsonrpc":"2.0","id":ident,"method":method,"params":params})
            await child.stdin.drain()
            return await future
        finally:
            pending.pop(ident,None)

    async def messages():
        try:
            while line := await child.stdout.readline():
                msg = json.loads(line)
                method, ident = msg.get("method"), msg.get("id")
                if "method" not in msg:
                    future = pending.get(ident)
                    if future and not future.done():
                        if "error" in msg:
                            future.set_exception(ValueError("Kimi ACP: "+str(msg["error"].get("message","请求失败"))))
                        else:
                            future.set_result(msg["result"])
                elif ident is not None:
                    try:
                        owner.register(ident,method,msg["params"])
                        print("\nKimi 等待确认，请在本会话的 Chat 中作答。",flush=True)
                    except (KeyError,TypeError,ValueError,AttributeError):
                        write({"jsonrpc":"2.0","id":ident,"error":{"code":-32602,"message":"Unsupported ACP request"}})
                elif method == "session/update" and msg["params"].get("sessionId") == owner.session:
                    update = msg["params"].get("update",{})
                    block = update.get("content",{})
                    if update.get("sessionUpdate") == "agent_message_chunk" and block.get("type") == "text":
                        print("".join(c for c in block["text"] if c.isprintable() or c in "\n\t"),end="",flush=True)
        finally:
            owner.pending.clear()
            for future in pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Kimi ACP 已断开"))

    async def interact():
        init = await asyncio.wait_for(call("initialize",{"protocolVersion":1,
            "clientCapabilities":{"elicitation":{"form":{}}},"clientInfo":{"name":"tmuxweb","version":"1"}}),30)
        if init.get("protocolVersion") != 1:
            raise ValueError("不支持此 ACP 版本")
        session = await asyncio.wait_for(call("session/new",{"cwd":os.getcwd(),"mcpServers":[]}),45)
        owner.session = string(session["sessionId"],160)
        print("Kimi Chat 已连接。可在 Chat 或此处输入消息，/exit 退出。",flush=True)
        loop = asyncio.get_running_loop()
        lines = asyncio.Queue()
        def read_input():
            import readline  # Enable bracketed paste for multiline tmux input.
            while True:
                try:
                    value = input("› ")
                except (EOFError,OSError):
                    value = None
                if loop.is_closed():
                    return
                loop.call_soon_threadsafe(lines.put_nowait,value)
                if value is None:
                    return
        threading.Thread(target=read_input,daemon=True).start()
        try:
            while True:
                line = await lines.get()
                if line is None or line.strip() == "/exit":
                    return
                if line.strip() == "/cancel":
                    owner.pending.clear()
                    write({"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":owner.session}})
                elif line.strip() and not start_prompt(line):
                    print("Kimi 正在处理当前回合。",flush=True)
        finally:
            if turn and not turn.done():
                turn.cancel()

    jobs = []
    try:
        async with await asyncio.start_unix_server(serve,path=listener,limit=MAX_FRAME):
            path.symlink_to(listener.name)
            jobs = [asyncio.create_task(messages()),asyncio.create_task(interact())]
            done,_ = await asyncio.wait(jobs,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        if path.is_symlink() and os.readlink(path) == listener.name:
            path.unlink(missing_ok=True)
        listener.unlink(missing_ok=True)
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(),3)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
