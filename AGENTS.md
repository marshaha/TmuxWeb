# TmuxWeb releases

For completed changes to the running application, bump `VERSION` according to SemVer, move the release notes into a matching numbered section in `CHANGELOG.md`, run the isolated checks, then deploy to both configured production nodes in the same task. Report the version, package ID and per-node verification. A request for review only, a deployment pause or an explicit user scope takes precedence.

The local maintenance fleet uses the workflow in [docs/fleet-releases.md](docs/fleet-releases.md). Package the named, tested files. Keep unrelated edits, runtime databases, credentials and uploads out of deployments. Use a fake pane or terminal for interaction tests. A production release does not require a Git commit, tag or push when the session has not authorized those operations.
