# Changelog

## 0.2.0

- Show native interaction notices and recorded Codex and Claude questions in Chat. Track synchronous Codex questions until their matching result or turn end. Manual screen previews are read-only.
- Populate Codex model suggestions from the session host's visible catalog. Show the native picker when no catalog is available.
- Host agent 1.2.1 adds batched reads of native interaction notices.

## 0.1.0

- Track a release version independently of the deployed build ID.
- Inspect live TUI interactions in chat. Show option descriptions, boxed menus and supported checklists, with a terminal fallback and a fresh screen check before sending choices.
- Share in-flight transcript reads, waits and prompt captures. Render up to 500 chat messages with keyed DOM updates.
