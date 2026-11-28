# Shared Skills on WS clients

Publish trusted Skills in robo's MCPHub. A native MCP Skills client can discover the group directly. The TmuxWeb WebSocket (WS) client, installed as `tmuxweb-agent` version 1.3.0, also mirrors a group into per-user harness directories, so Chat can use the existing local slash menu and keep working through an outage. The sync runs in a background thread at startup and every ten minutes.

1. In MCPHub 1.3.2 create a private group owned by a regular service user. Import the Skill directory and enable publication for that group. Leave global publication off. Give each node a separate **user** bearer key owned by that user and restricted to the group. A system key lacks the user context required for a private Skill group.
2. Place the node's bearer in a mode-0600 file in its WS client account. Create `~/.config/tmuxweb-agent/skill-hub.json` in the same account, for example:

   ```json
   {
     "url": "http://100.123.123.3:3300/mcp/tmuxweb-node-skills",
     "token_file": "~/.config/tmuxweb-agent/skill-hub.token",
     "providers": ["codex", "hermes"]
   }
   ```

3. Run `python3 agent.py sync-skills` for an immediate check. Start the updated WS client with the configuration present for continuous sync. The hosts page shows the installed content revision, count and last sync outcome. A manual CLI sync changes files immediately. The daemon reports the result of its next check.
4. Codex and Kimi use `~/.agents/skills`, Claude uses its config directory's `skills`, and Hermes uses `$HERMES_HOME/skills` (default `~/.hermes/skills`). Choose only the harnesses managed by that account. How quickly a running harness reloads its own registry depends on the harness.

Each install verifies SHA-256 and size before switching the active snapshot. Personal skills with the same directory name block the update. A failed download leaves the installed version usable. Empty listings can also indicate loss of permission, so purging the last managed Skill requires explicit `"allow_empty": true`. To roll back, republish the previous source contents to the same group and sync. The content revision is stable across reimports. Keep versioned source outside the hub. Removing the local configuration stops polling after the WS client restarts. Revoke the node's key in MCPHub to end its access.
