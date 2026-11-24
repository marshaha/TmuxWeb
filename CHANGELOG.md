# Changelog

## Unreleased

- The remote agent raises its own descriptor limit at startup (`ulimit -n 4096` in the keep-alive script, `LimitNOFILE=8192` in the systemd unit). A macOS agent left on the 256-descriptor default eventually failed every command with `cannot start command: [Errno 24] Too many open files`, which made the host look offline with no panes.

## 0.7.1

- Unify the Chat, Home and Grid sidebar headers and active pane styling. Distinguish conversation turns, theme the chat header, and keep keyboard focus visible in the composer and navigation.

## 0.7.0

- Browse installed skill manifests with `/` in Chat. The current pane supplies the skill catalog, including across agent, SSH and federation transports. Choosing a skill inserts its harness-specific invocation into the draft.

## 0.6.6

- Keep the selected pane or prompt visible when navigating composer suggestions with the arrow keys. Label slash suggestions as prompt templates.

## 0.6.5

- Keep server icons, status dots and actions aligned at the minimum console sidebar width. Long host names truncate in the remaining space.

## 0.6.4

- Keep the sub-agent and operation lists separate when switching tabs or panes. Make long lists scroll with readable rows on phones.

## 0.6.3

- Use the Primer Octicons git-branch-24 icon for the Git panel and share-24 for share controls in the console, home and grid.

## 0.6.2

- Fold the Chat terminal keyboard into a bottom-left button and reveal its keys to the right on demand. Round the left scroll shadow in the Chat header.
- Show the interaction card placement selector only when manual interaction inspection is enabled in experimental settings.

## 0.6.1

- Place four arrows, Tab, Enter and Esc in the first view of the live Chat keyboard and both terminal keyboards. The single Chat row scrolls to Shift+Tab, Space, Ctrl+C and Ctrl+D. Each tap sends its native key to the selected pane.

## 0.6.0

- Show a collapsible interaction keyboard directly below live terminal output. Verified menus share the card's selection and submission checks. Screen-only arrow keys move the terminal cursor after a fresh screen check, with confirmation in the native terminal, and the native keyboard can also be opened manually.

## 0.5.1

- Make manual interaction inspection an opt-in account preference under experimental settings. Verified choices and navigation remain available in Chat by default.

## 0.5.0

- Compact the mobile update prompt and move the Chat composer closer to the bottom safe area. Sending a Chat message submits it directly.
- Bring interaction inspection into the live terminal header. Verified native menus show touch navigation and confirmation, using the same revision checks as option clicks.

## 0.4.0

- Package tested changes with a versioned manifest, verify both running nodes, and retain per-release rollback files and deployment receipts.

- Limit the mobile web terminal keyboard to two rows. Expand navigation, control or symbol keys and swipe within each group. The letter keyboard pages sideways and the inline reply terminal keeps one row.
- Retire completed Codex approval cards. Verify the live command and native request before submitting a one-time choice from Chat.
- Expand the color terminal preview by default, keep the sidebar collapse control pinned, and compact the mobile composer toolbar.
- Expose the semantic release in the browser update marker so a deployment from the same Git revision still prompts open clients to refresh.

- Use bracketed paste for multiline messages and verify Enter against Kimi’s framed input. A queue item whose text was pasted but not submitted records the failure without pasting it again.

- Search browser and server caches before reading hosts. Sidebar progress shows completed sessions, elapsed time and the transports used, preferring a connected host agent before SSH.

- Share local and federated panes with separate controls for model selection, terminal view and guest input. Owners can update an existing link. Child nodes still enforce their own permissions.

- Search recent conversation text from the sidebar across local and remote panes. Results include a matching excerpt.

- Merge manual inspection and native interaction notices in one Chat card. Require an outstanding Hermes clarify hook and matching live questions for direct answers. Retire the experimental screen poll and automatic responses.

- Choose whether live interaction cards appear above the composer or after the latest Chat message. Each browser remembers its selection.

## 0.3.1

- Answer multi-question Hermes clarify dialogs in Chat with the mouse or keyboard. Switching questions refreshes the available choices. Reopening inspection dismisses the inline terminal and restores focus to the options.

## 0.3.0

- Answer a live Hermes single-choice question in Chat using its numbered shortcuts. Recheck the visible question before sending.
- Open the native interactive pane inside Chat for typed answers, approvals and multi-step forms.

## 0.2.0

- Show native interaction notices and recorded Codex and Claude questions in Chat. Track synchronous Codex questions until their matching result or turn end. Manual screen previews are read-only.
- Populate Codex model suggestions from the session host's visible catalog. Show the native picker when no catalog is available.
- Host agent 1.2.1 adds batched reads of native interaction notices.

## 0.1.0

- Track a release version independently of the deployed build ID.
- Inspect live TUI interactions in chat. Show option descriptions, boxed menus and supported checklists, with a terminal fallback and a fresh screen check before sending choices.
- Share in-flight transcript reads, waits and prompt captures. Render up to 500 chat messages with keyed DOM updates.
