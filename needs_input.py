""""Needs you": a pane whose agent is waiting for the user to pick something.

The whole point is the gate. Pure screen parsing misjudges — the user already
has an experimental project that renders interactive CLI info as popup options
from parsing alone, and it fires false positives. Unless you know the agent
state, parsing is not feasible. So ``needs_input`` is a conjunction of three
independent legs, and the gate fails closed:

    1. decision point — the agent's own session file says it is at a decision
                       point: the turn ended (``agent_status`` IDLE), OR a tool
                       call is outstanding (issued, no result yet). The second
                       disjunct is what a permission wait looks like: the
                       turn-end marker NEVER fires there — across two real
                       Claude files (10432 + 45645 records) 0 of 4008 tool_use
                       blocks are followed by a turn_duration before the next
                       user message carrying the tool_result. turn_duration is
                       written only after the tool result returns and the turn
                       completes, so while the agent sits at a permission
                       prompt the file's last decisive record is the assistant
                       message and the verdict is BUSY. The first version of
                       this gate used "turn ended" alone and would only have
                       fired for menus after a turn fully ends — never for
                       approval prompts. That was the wrong design; the
                       outstanding call is the signal instead.
    2. screen stable — the same menu parses identically on ``STABLE_POLLS``
                       consecutive observations. A transient render mid-stream
                       (or a cursor still moving) never fires.
    3. menu present  — ``agent_prompts.parse_screen`` returns structured options.

False-positive modes each leg prevents:

    - streaming transient: the menu flashes up mid-stream while the agent is
      still writing — leg 2 (screen still changing) blocks it, and a turn with
      no outstanding call fails leg 1 too.
    - stale prompt left on screen: last turn's confirm box is still visible
      while the next turn is already running — leg 1 blocks it unless a call
      is genuinely outstanding, and the stability count is only banked while
      the pane is at a decision point, so a stale menu cannot ride in on
      observations made while the agent is merely executing.
    - menu-like text in normal output: a numbered list in chat, echoed source
      quoting "press Enter to continue" — ``parse_screen`` already refuses
      those, and even if it ever accepted one, legs 1 and 2 still have to hold.

Providers without a decision signal in ``agent_status`` (hermes keeps its
state in ``~/.hermes/state.db`` with no usable wait signal — its
``verification_required`` finish_reason appears twice in 43314 rows and never
as a session's latest message; a plain shell has no session file at all) never
raise ``needs_input`` — the leg is simply unavailable, and an unavailable leg
means "no", never "yes".

Why not parse alone, and why not ask the agent like T3 does: T3 Code never
parses screens to detect prompts — a repo-wide grep for parseScreen / ANSI
prompt detection in ``apps/server/src`` and ``packages/`` returns nothing.
Instead the agent TELLS it: ACP ``session/request_permission`` is a first-class
JSON-RPC method (``packages/effect-acp/src/client.ts:934`` handles it,
``agent.ts:409`` dispatches it), thread status is owned by the orchestrator
(``listedThreadStatus``, ``thread.ts:654``: ended -> completed/failed, else
"waiting"), and ``awaitingApproval`` is a structured list in state. TmuxWeb
cannot have that — it drives TUIs in tmux panes with no protocol channel — so
the session file's decision point plus screen stability is the closest
equivalent: the agent's own record, not our guess about its pixels.

Honest limits: the gate is a heuristic. The outstanding-call leg cannot
distinguish an approval wait from a tool that is executing but happens to show
a stable menu — during execution the screen usually keeps changing (leg 2
fails), but a long-running tool whose TUI sits frozen on a menu-like screen
would light up. That is the known false-positive mode of this design, and it
is why the card never answers on its own: a human still has to tap. A pane
whose harness writes no decision signal can only ever use the stable-screen
leg, and this gate refuses to do even that — so hermes and shell panes never
light up. And the sweep only re-observes a pane about once a minute (see
``TMUX_WEB_SWEEP_MIN_INTERVAL``), so a menu takes at least two sweeps — a
minute or two — to light up. That delay is the price of not crying wolf.
"""
from __future__ import annotations

import copy
import threading

import agent_prompts

# How many consecutive identical parses, all at a decision point, it takes to
# light up. Two is the smallest count that kills a one-frame transient; the
# sweep's own cadence (about a minute) already makes three feel sluggish.
STABLE_POLLS = 2

# Providers whose session file carries a turn boundary (agent_status.py).
# Anything else — hermes, shell, unknown — has no leg 1 and never fires.
GATED_PROVIDERS = ("claude", "codex", "kimi")


def prompt_signature(prompt: dict | None) -> tuple | None:
    """Canonical form of a parsed menu: two parses are "the same menu" iff
    these are equal. The selected row counts — a cursor still moving means
    the screen is not stable yet."""
    if not prompt:
        return None
    options = tuple(
        (option.get("label", ""), option.get("meta", ""), option.get("number", ""))
        for option in prompt.get("options") or []
    )
    toggles = tuple(
        (toggle.get("label", ""), toggle.get("active"), tuple(toggle.get("options") or []))
        for toggle in prompt.get("toggles") or []
    )
    return (prompt.get("kind", ""), prompt.get("title", ""), options, toggles,
            prompt.get("selected"))


def enrich_prompt(prompt: dict | None) -> dict | None:
    """Copy a parsed menu and attach the key sequence for every choice.

    The console only sends keys it was handed: each option gets the
    ``agent_prompts.choice_keys`` sequence (arrows + Enter, or a digit +
    Enter, or y/n + Enter), each toggle value gets ``toggle_keys``. Every key
    in every sequence is one ``POST /api/panes/{id}/key`` accepts, so the
    console needs no send path of its own. A translation that raises (no
    options, no toggles) leaves that entry without ``keys`` instead of
    failing the whole card.
    """
    if not prompt:
        return None
    enriched = copy.deepcopy(prompt)
    for index, option in enumerate(enriched.get("options") or []):
        try:
            option["keys"] = agent_prompts.choice_keys(enriched, index)
        except (ValueError, KeyError, IndexError):
            continue
    for toggle in enriched.get("toggles") or []:
        sequences: list[list[str]] = []
        for value in range(len(toggle.get("options") or [])):
            try:
                sequences.append(agent_prompts.toggle_keys(
                    enriched, toggle.get("index", 0), value))
            except (ValueError, KeyError, IndexError):
                sequences.append([])
        toggle["keys_by_value"] = sequences
    return enriched


class NeedsInputGate:
    """Per-pane stability tracking for the needs_input conjunction.

    ``observe`` is called once per sweep per pane, with the session-file
    verdict and that sweep's capture. It returns the enriched prompt once all
    three legs hold, else ``None``. Thread-safe: the sweep calls it from a
    worker pool.
    """

    def __init__(self, stable_polls: int = STABLE_POLLS):
        self._stable_polls = max(1, int(stable_polls))
        self._lock = threading.Lock()
        # pane id -> {"signature": tuple, "stable": int, "prompt": dict}
        self._seen: dict[str, dict] = {}

    def observe(self, pane_id: str, *, decision_point, screen_text: str | None) -> dict | None:
        # Leg 1 is a hard requirement, not a hint: anything but a confirmed
        # decision point resets the count. Observations made while the agent is
        # merely executing must not be banked towards a later lighting — that
        # is exactly how a stale prompt would otherwise ride in.
        if decision_point is not True:
            self.reset(pane_id)
            return None
        prompt = agent_prompts.parse_screen(screen_text or "")
        if prompt is None:
            self.reset(pane_id)
            return None
        signature = prompt_signature(prompt)
        with self._lock:
            entry = self._seen.get(pane_id)
            if entry is not None and entry["signature"] == signature:
                entry["stable"] += 1
                entry["prompt"] = prompt
            else:
                self._seen[pane_id] = {"signature": signature, "stable": 1,
                                       "prompt": prompt}
                entry = self._seen[pane_id]
            if entry["stable"] < self._stable_polls:
                return None
            return enrich_prompt(entry["prompt"])

    def reset(self, pane_id: str) -> None:
        with self._lock:
            self._seen.pop(pane_id, None)

    def prune(self, pane_ids) -> None:
        """Forget panes that no longer exist: pane ids (%N) get reused, and a
        new pane must not inherit the old one's stability."""
        wanted = set(pane_ids or ())
        with self._lock:
            for pane_id in list(self._seen):
                if pane_id not in wanted:
                    del self._seen[pane_id]
