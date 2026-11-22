import { Plugin } from "@opencode/plugin/tui"
import { spawn } from "node:child_process"
import { homedir } from "node:os"
import { join } from "node:path"

export default Plugin.define({
  id: "tmuxweb.observer",
  setup(context) {
    if (!process.env.TMUX_PANE) return
    const receiver = join(homedir(), ".config/tmuxweb-agent/hooks/observe.py")
    let last = "", written = 0, writing = false
    function update() {
      if (writing) return
      const route = context.ui.router.current()
      const session = route?.type === "session" ? route.sessionID : ""
      const items = []
      if (session) {
        for (const req of context.data.session.permission.list(session) ?? []) {
          if (req.id) items.push({id: req.id, kind: "permission"})
        }
        for (const form of context.data.session.form.list(session, context.location) ?? []) {
          if (form.id) items.push({id: form.id, kind: "form"})
        }
      }
      const body = JSON.stringify({session_id: session, items})
      if ((!last && !items.length) ||
          (body === last && (!items.length || Date.now() - written < 15000))) return
      writing = true
      const child = spawn("python3", [receiver, "opencode"], {stdio: ["pipe", "ignore", "ignore"], timeout: 2500})
      child.on("error", () => {})
      child.stdin.on("error", () => {})
      child.on("close", code => {
        writing = false
        if (code === 0) { last = body; written = Date.now() }
      })
      child.stdin.end(body)
    }
    const timer = setInterval(update, 1000)
    return () => clearInterval(timer)
  },
})
