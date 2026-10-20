#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
SERVER="$DIR/mcp_server.py"
DB="$DIR/mailbox.db"
NAME="hermes-mailbox"

if claude mcp get "$NAME" >/dev/null 2>&1; then
  claude mcp remove -s user "$NAME" >/dev/null 2>&1 || claude mcp remove "$NAME" >/dev/null
fi
claude mcp add -s user "$NAME" -- "$PYTHON" "$SERVER" --identity claude --db "$DB"

if codex mcp get "$NAME" >/dev/null 2>&1; then
  codex mcp remove "$NAME" >/dev/null
fi
codex mcp add "$NAME" -- "$PYTHON" "$SERVER" --identity codex --db "$DB"

printf '\nInstalled %s for Claude and Codex.\n' "$NAME"
claude mcp get "$NAME"
printf '\n'
codex mcp get "$NAME"
