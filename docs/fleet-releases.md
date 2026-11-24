# Local fleet releases

Ubuntu drives the release and reaches the second installation through SSH. The release command uses each installation's existing `scripts/run_service.sh` supervisor and login configuration. Configure the controller's private `~/.config/tmuxweb-release/fleet.json` with a `nodes` array. Each node has a unique `name`, an absolute checkout `root` and, for a remote node, an SSH destination in `ssh`.

Update `VERSION` and `CHANGELOG.md`. Sync the intended files into the isolated `/tmp/tw-review` worktree. Include added files and remove intentionally deleted files from the copy. The local node in the fleet configuration supplies the baseline checkout and its Python virtual environment. Override with `TMUXWEB_BASELINE` or `TMUXWEB_TEST_PYTHON` when needed. Put the project's Python and Node on PATH and make the Playwright installation available to the test environment. The controller can keep Playwright and its Node binary in `~/.local/share/tmuxweb-release/test-tools/python` and the matching browser binaries in the adjacent `browsers` directory. The publisher loads them before it isolates HOME. From the maintenance checkout, run:

```bash
scripts/publish_release.sh --browser terminal_keyboard web/index.html web/assets/console-base.css
```

Pass all changed files intended for the release, including updated tests. Repeat `--browser` for additional UI checks. VERSION and CHANGELOG.md are included automatically. The script runs the Python and Node suites with a temporary HOME, isolated tmux socket directory, and a PATH containing the selected Python, Node, and system utilities. Set `TMUXWEB_TEST_PATH` if a fixture needs additional utilities. Browser suites must pass at 1440, 1180, 1024, 900, 834, 768 and 390 px. It compares the selected source files with the test copy, builds the package, preflights every node, applies the release sequentially, and verifies each booted version and served console. In a managed sandbox the preparation and deployment can run separately using `scripts/release_fleet.py prepare` and `deploy`. Only deployment needs live host access.

Every node retains the archive, file digests, test summary and pre-deployment copies in `~/.local/state/tmuxweb/releases/<version>/`. The controller records the most recent successful rollout in `last-deployment.json` in the releases directory. An interrupted rollout can resume from the original archive. A different build cannot replace an activated numbered release. If preflight stopped before activation, a corrected package can reuse the proposed number. The rejected package is retained in a separate preflight directory. If only release notes differ, merge them and pass each reviewed node copy with `prepare --prior-changelog PATH`. The manifest records the accepted prior digests and rollback restores the node-specific original. Failed health checks initiate rollback. For an operator rollback of the currently installed release, run from each production checkout with its normal service environment:

```bash
set -a
. ./.env
set +a
./.venv/bin/python "$HOME/.local/state/tmuxweb/releases/<version>/release_node.py" rollback
```

The authenticated `/api/version` reports the semantic release and the release-plus-revision marker used by the browser. Reloading a service at the same release keeps the marker stable.
