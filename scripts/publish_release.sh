#!/usr/bin/env bash
# Run from the maintenance checkout with an isolated copy already synced to it.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
tested="${TMUXWEB_TEST_TREE:-/tmp/tw-review}"
config="${TMUXWEB_FLEET_CONFIG:-$HOME/.config/tmuxweb-release/fleet.json}"
baseline="${TMUXWEB_BASELINE:-}"
if [[ -z "$baseline" ]]; then
  baseline=$(python3 -c 'import json,sys; print(next(n["root"] for n in json.load(open(sys.argv[1]))["nodes"] if not n.get("ssh")))' "$config")
fi
python_bin="${TMUXWEB_TEST_PYTHON:-$baseline/.venv/bin/python}"

tools="${XDG_DATA_HOME:-$HOME/.local/share}/tmuxweb-release/test-tools"
if [[ -x "$tools/python/playwright/driver/node" ]]; then
  export PATH="$tools/python/playwright/driver:$PATH"
  export PYTHONPATH="$tools/python${PYTHONPATH:+:$PYTHONPATH}"
  export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$tools/browsers}"
fi
python_bin="$(command -v "$python_bin")"
node_bin="$(command -v node)"
ui=()
while [[ "${1:-}" == --browser ]]; do
  [[ "$2" =~ ^[a-z_]+$ ]] || { echo "Invalid browser test name" >&2; exit 2; }
  ui+=("$2")
  shift 2
done
[[ $# -gt 0 ]] || { echo "Usage: publish_release.sh [--browser NAME]... FILE..." >&2; exit 2; }
tested="$(cd "$tested" && pwd -P)"
[[ "$tested" == /tmp/* && "$tested" != "$root" && "$tested" != "$(cd "$baseline" && pwd -P)" ]] || exit 2
checks=$(mktemp -d /tmp/tmuxweb-release-checks.XXXXXXXX)
test_home=$(mktemp -d /tmp/tmuxweb-release-home.XXXXXXXX)
echo "Release checks: $checks"
(
  cd "$tested"
  unset TMUX TMUX_PANE
  export HOME="$test_home" TMUX_TMPDIR="$test_home"
  # Keep the installer dry-run from discovering the operator's interactive CLIs.
  export PATH="$(dirname "$python_bin"):$(dirname "$node_bin"):${TMUXWEB_TEST_PATH:-$(getconf PATH)}"
  "$python_bin" -m unittest discover -s tests >"$checks/python.log" 2>&1
  node --test tests/*.test.mjs tests/js/*.test.mjs >"$checks/node.log" 2>&1
  for suite in "${ui[@]}"; do
    "$python_bin" "tests/browser/$suite.py" >"$checks/$suite.log" 2>&1
  done
  touch "$checks/PASS"
)
options=()
for suite in "${ui[@]}"; do options+=(--browser "$suite"); done
archive=$(python3 "$root/scripts/release_fleet.py" prepare --baseline "$baseline" --tested "$tested" \
          --checks "$checks" "${options[@]}" "$@")
echo "Verified package: $archive"
exec python3 "$root/scripts/release_fleet.py" deploy --config "$config" "$archive"
