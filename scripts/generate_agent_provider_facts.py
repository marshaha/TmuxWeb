#!/usr/bin/env python3
"""Render the providers/ registry into the agent program.

The agent (``plugins/agent/agent.py``) is deployed as a single file,
so it cannot import ``providers/``. Its provider facts therefore
travel *inside* it as a generated block, between the
``BEGIN GENERATED PROVIDER FACTS`` / ``END GENERATED PROVIDER FACTS``
markers: the data (markers, hook sets, bins, gating, resume templates,
transcript sources) plus the code-shaped functions
(``<name>_outstanding``, ``<name>_resolve_session_file`` and their
helpers), copied verbatim from the provider modules.

Run from the repo root (or anywhere; paths are resolved from this
file): ``python3 scripts/generate_agent_provider_facts.py``. It
rewrites the block in place and prints whether anything changed.

The drift test (``tests/test_provider_registry.py``) calls
:func:`render_block` and fails if the block embedded in ``agent.py``
differs, so a hand edit to either side cannot silently drift.
"""
import ast
import inspect
import pprint
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import providers  # noqa: E402
from providers import claude as _claude  # noqa: E402
from providers import codex as _codex  # noqa: E402
from providers import hermes as _hermes  # noqa: E402
from providers import kimi as _kimi  # noqa: E402

AGENT_FILE = REPO_ROOT / "plugins" / "agent" / "agent.py"
BEGIN = "# --- BEGIN GENERATED PROVIDER FACTS (do not edit by hand) ---"
END = "# --- END GENERATED PROVIDER FACTS ---"

# Functions copied verbatim from each provider module, in order.
# Their names are already provider-prefixed in the modules, so the
# concatenation has no collisions and needs no renaming.
COPIED_FUNCTIONS = {
    "claude": ["claude_file_contains", "claude_outstanding",
               "claude_resolve_session_file"],
    "codex": ["codex_mtime", "codex_outstanding",
              "codex_resolve_session_file"],
    "kimi": ["kimi_outstanding", "kimi_resolve_session_file"],
    "hermes": ["hermes_outstanding", "hermes_resolve_session_file"],
}

_MODULES = {"claude": _claude, "codex": _codex, "kimi": _kimi, "hermes": _hermes}

# Names a copied function may use: builtins, the stdlib names the agent
# program imports (json, os, re, Path), and the other copied functions.
# Anything else means the function is not self-contained and must not
# be copied verbatim.
_ALLOWED_GLOBALS = {
    "json", "os", "re", "Path", "OSError", "ValueError", "TypeError",
    "Exception", "RuntimeError", "StopIteration",
    "float", "int", "str", "bool", "len", "list", "dict", "set", "tuple",
    "sorted", "max", "min", "isinstance", "callable", "enumerate", "range",
    "reversed", "all", "any", "open", "print", "next", "iter", "sum",
    "repr", "getattr", "hasattr", "zip", "map", "filter", "abs", "round",
    "bytes", "bytearray",
}
_ALLOWED_GLOBALS |= {name for names in COPIED_FUNCTIONS.values() for name in names}


def _check_self_contained(module, func_name: str) -> None:
    """The copied source must not reach outside its own names."""
    source = inspect.getsource(getattr(module, func_name))
    tree = ast.parse(source)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                bound.add(arg.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            raise ValueError("%s.%s imports %s: copied functions must be "
                             "self-contained" % (module.NAME, func_name, node.module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in _ALLOWED_GLOBALS and node.id not in bound:
                # A Load of an unknown name is a module global the agent
                # program would not have.
                raise ValueError(
                    "%s.%s uses global %r, which the agent program does "
                    "not provide" % (module.NAME, func_name, node.id))


def _facts_literal() -> str:
    """PROVIDER_FACTS as a data literal, one entry per provider."""
    facts = {}
    for name in providers.ORDER:
        spec = providers.SPECS[name]
        facts[name] = {
            "label": spec.label,
            "bins": spec.bins,
            "command_prefixes": spec.command_prefixes,
            "model_presets": spec.model_presets,
            "model_switch_template": spec.model_switch_template,
            "provider_label": spec.provider_label,
            "hook_template": spec.hook_template,
            "hook_waiting": spec.hook_waiting,
            "hook_clearing": spec.hook_clearing,
            "gated": spec.gated,
            "idle_markers": spec.idle_markers,
            "busy_markers": spec.busy_markers,
            "resume_template": spec.resume_template,
            "transcript_source": [dict(source) for source in spec.transcript_source],
        }
    return "PROVIDER_FACTS = " + pprint.pformat(facts, width=100, sort_dicts=False)


def render_block() -> str:
    """The full generated block, markers included."""
    for name in providers.ORDER:
        module = _MODULES[name]
        for func_name in COPIED_FUNCTIONS[name]:
            _check_self_contained(module, func_name)
    lines = [BEGIN,
             "# Generated from providers/ by scripts/generate_agent_provider_facts.py.",
             "# Regenerate with: python3 scripts/generate_agent_provider_facts.py",
             "# The drift test (tests/test_provider_registry.py) fails if this",
             "# block differs from what the generator renders from the registry.",
             ""]
    lines.append(_facts_literal())
    lines.append("")
    for name in providers.ORDER:
        module = _MODULES[name]
        for func_name in COPIED_FUNCTIONS[name]:
            lines.append(inspect.getsource(getattr(module, func_name)).rstrip("\n"))
            lines.append("")
    lines.append("PROVIDER_OUTSTANDING = {")
    for name in providers.ORDER:
        lines.append('    %r: %s_outstanding,' % (name, name))
    lines.append("}")
    lines.append("")
    lines.append("PROVIDER_RESOLVERS = {")
    for name in providers.ORDER:
        lines.append('    %r: %s_resolve_session_file,' % (name, name))
    lines.append("}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def extract_block(text: str):
    """The generated block in ``text`` (markers included), or None."""
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        return None
    return text[start:end + len(END)]


def update_agent_file(path: Path = AGENT_FILE) -> bool:
    """Replace the generated block in ``agent.py``; True if it changed."""
    text = path.read_text(encoding="utf-8")
    old = extract_block(text)
    if old is None:
        raise ValueError("%s has no generated provider-facts block" % path)
    new = render_block().rstrip("\n")
    if old == new:
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def main() -> int:
    changed = update_agent_file()
    print("agent provider facts: %s" % ("updated" if changed else "already current"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
