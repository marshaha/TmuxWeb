"""Harness provider registry: one module per agent CLI.

Every agent CLI TmuxWeb drives (claude, codex, kimi, hermes) is
described once, here, as a :class:`ProviderSpec`: how to spot it in a
pane, where its transcript lives, which session-file records mean
idle/busy, which tool calls can be outstanding, which hook events mean
waiting, and how to resume it. The server reads the registry; the
agent program and the SSH-piped transcript reader get the same facts
through generated code and drift tests instead of a second hand copy
(see ``providers/README.md``).

Adding a harness is one provider module + its tests + its hook
template — no if/elif chain anywhere else should need a new branch.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from providers import claude as _claude
from providers import codex as _codex
from providers import hermes as _hermes
from providers import kimi as _kimi

ORDER = ("claude", "codex", "kimi", "hermes")
"""Registry order. It is load-bearing: provider_from_argv tries names
in this order, matching transcript_reader.AGENT_NAMES."""


@dataclass(frozen=True)
class ProviderSpec:
    """One harness. ``resolve_session_file`` and ``outstanding`` are the
    code-shaped predicates; everything else is data."""

    name: str
    label: str
    bins: tuple
    command_prefixes: tuple
    model_presets: tuple
    model_switch_template: str
    provider_label: str
    hook_template: str
    hook_waiting: tuple
    hook_clearing: tuple
    gated: bool
    idle_markers: tuple
    busy_markers: tuple
    resume_template: tuple
    transcript_source: tuple
    resolve_session_file: object = field(repr=False)
    outstanding: object = field(repr=False)

    remote_native_form: bool = False
    native_form: object = field(default=None, repr=False)

    def resume_argv(self, session_id: str) -> list:
        """Restart/resume flags for a session id (interface only; the
        restart feature itself lives in PR #23, not here)."""
        return [part.replace("{session_id}", session_id)
                for part in self.resume_template]


def _spec(module) -> ProviderSpec:
    return ProviderSpec(
        name=module.NAME,
        remote_native_form=getattr(module, "REMOTE_NATIVE_FORM", False),
        native_form=getattr(module, "native_form", None),
        label=module.LABEL,
        bins=module.BINS,
        command_prefixes=module.COMMAND_PREFIXES,
        model_presets=module.MODEL_PRESETS,
        model_switch_template=module.MODEL_SWITCH_TEMPLATE,
        provider_label=module.PROVIDER_LABEL,
        hook_template=module.HOOK_TEMPLATE,
        hook_waiting=module.HOOK_WAITING,
        hook_clearing=module.HOOK_CLEARING,
        gated=module.GATED,
        idle_markers=module.IDLE_MARKERS,
        busy_markers=module.BUSY_MARKERS,
        resume_template=module.RESUME_TEMPLATE,
        transcript_source=module.TRANSCRIPT_SOURCE,
        resolve_session_file=getattr(module, "%s_resolve_session_file" % module.NAME),
        outstanding=getattr(module, "%s_outstanding" % module.NAME),
    )


SPECS = {module.NAME: _spec(module)
         for module in (_claude, _codex, _kimi, _hermes)}


def get(name: str):
    """The spec for ``name`` (case-insensitive), or None."""
    if not isinstance(name, str):
        return None
    return SPECS.get(name.strip().lower())


def all_specs() -> list:
    """Specs in registry order."""
    return [SPECS[name] for name in ORDER]


def names() -> tuple:
    return ORDER


def gated_providers() -> tuple:
    """Providers with a real decision signal. A provider opts in only
    when its session file carries one — hermes stays opted out."""
    return tuple(name for name in ORDER if SPECS[name].gated)


def provider_for_command(command) -> str | None:
    """Map a pane's foreground command to a provider (or None).

    Same rules transcript_reader.provider_for_command has always used:
    a command starting with the harness name, with hermes also matching
    a plain python interpreter (both expose history through
    ~/.hermes/state.db).
    """
    command = (command or "").strip().lower()
    for name in ORDER:
        for prefix in SPECS[name].command_prefixes:
            if command.startswith(prefix):
                return name
    return None


def _app_name(argv) -> str:
    """``/opt/homebrew/bin/codex --flag`` -> ``codex``."""
    if not argv:
        return ""
    first = str(argv).strip().split(None, 1)[0]
    return os.path.basename(first).lower()


def provider_from_argv(argv) -> str | None:
    """Which agent an argv belongs to: ``codex``,
    ``/x/bin/codex``, ``node .../@openai/codex/bin/codex.js`` all count.

    Same rules transcript_reader.provider_from_argv has always used:
    the first token's basename wins, else any token whose basename is
    the harness name or starts with ``<name>.`` / ``<name>-``
    (kimi-code / hermes-agent / claude-code: a wrapper shell counts
    as the same agent).
    """
    name = _app_name(argv)
    if name in SPECS:
        return name
    for token in str(argv or "").split():
        base = os.path.basename(token).lower()
        if not base or base.startswith("-"):
            continue
        for agent in ORDER:
            if base == agent or base.startswith(agent + ".") or base.startswith(agent + "-"):
                return agent
    return None


# ---------------------------------------------------------------------------
# transcript_source: how a transcript is read. The slot must express every
# shape a harness can have, not just the file-path shape the four current
# providers use:
#
#   {"kind": "jsonl", "root": ..., "layout": ...}   — a session file
#       (claude, codex, kimi; also the shape Gemini would need:
#       ~/.gemini/tmp/*/chats/session-*.jsonl, verified from real files
#       on ctyun 2026-11-16 — but Gemini is NOT a provider here: no
#       working binary exists on any host, see providers/README.md)
#   {"kind": "sqlite", "db": ..., "tables": {...}}  — a state database
#       (hermes ~/.hermes/state.db; also the shape OpenCode would need:
#       a single ~/.local/share/opencode/opencode.db, tables
#       session/message/part — 2.x session_v2/session_message/
#       session_part, payload JSON in a data column)
#   {"kind": "cli", "commands": [...]}              — a CLI export
#       (OpenCode's supported reads: `opencode export <id>`,
#       `opencode session list --format json`, `opencode db path`)
#
# A provider lists its sources in priority order (hermes: sqlite first,
# per-session jsonl as fallback). OpenCode is deliberately NOT a
# provider in this PR: its schema is undocumented/unstable and no host
# has the binary or the database to verify against — the slot only has
# to be able to express it, which validate_transcript_sources proves.
# ---------------------------------------------------------------------------

_TRANSCRIPT_SOURCE_KEYS = {
    "jsonl": ("root", "layout"),
    "sqlite": ("db", "tables"),
    "cli": ("commands",),
}


def validate_transcript_sources(sources) -> None:
    """A transcript_source list is well-formed, or ValueError.

    Used at import time for the four real providers, and by tests to
    prove a future shape (e.g. OpenCode's sqlite + CLI export) fits
    the slot without adding the provider.
    """
    if not isinstance(sources, (list, tuple)) or not sources:
        raise ValueError("transcript_source must be a non-empty list")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("transcript_source entries must be dicts: %r" % (source,))
        kind = source.get("kind")
        required = _TRANSCRIPT_SOURCE_KEYS.get(kind)
        if required is None:
            raise ValueError("unknown transcript_source kind: %r" % (kind,))
        for key in required:
            if not source.get(key):
                raise ValueError("transcript_source %r needs %r" % (kind, key))


for _spec_item in SPECS.values():
    validate_transcript_sources(_spec_item.transcript_source)
