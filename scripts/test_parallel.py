"""Run regression suites in disposable copies of the current test worktree.

Run from the worktree root with the test virtualenv's Python. Put a test Node
on PATH and configure Playwright as usual, then e.g. use
`python scripts/test_parallel.py --jobs 8 --browser agents_panel`.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


def discover():
    sys.path.insert(0, os.getcwd())
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    if loader.errors:
        raise RuntimeError("\n".join(loader.errors))

    def ids(group):
        for test in group:
            if isinstance(test, unittest.TestSuite):
                yield from ids(test)
            else:
                yield test.id()

    names = list(ids(suite))
    if not names or len(names) != len(set(names)):
        raise RuntimeError("Empty or duplicate unittest IDs")
    print(json.dumps(names))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4, help="parallel Python processes")
    parser.add_argument("--browser", action="append", default=[], metavar="SUITE")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    source = Path.cwd()
    node = shutil.which("node")
    if not node or not (source / "tests/js").is_dir():
        parser.error("run from the test worktree with node on PATH")
    if any(not name.isidentifier() or not (source / "tests/browser" / (name + ".py")).is_file()
           for name in args.browser):
        parser.error("unknown browser suite")

    start = time.monotonic()
    logs = Path(tempfile.mkdtemp(prefix="tw-parallel-"))
    print(f"Logs: {logs}", flush=True)
    git_files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"])
    files = [Path(os.fsdecode(s)) for s in git_files.split(b"\0") if s]
    browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(PATH=os.pathsep.join([str(Path(sys.executable).parent), str(Path(node).parent), os.defpath]),
               PYTHONNOUSERSITE="1", PLAYWRIGHT_BROWSERS_PATH=browser_cache)

    def sandbox(directory):
        home = directory / "home"
        temp = directory / "tmp"
        home.mkdir(parents=True)
        temp.mkdir()
        repo = directory / "repo"
        subprocess.run(["git", "clone", "--local", "--quiet", "--no-checkout",
                        str(source), str(repo)], check=True)
        for rel in files:
            if (source / rel).is_file():
                dest = repo / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, dest)
        return repo, dict(env, HOME=str(home), TMPDIR=str(temp), TMUX_TMPDIR=str(home))

    with tempfile.TemporaryDirectory(prefix="tw-runs-") as tmp:
        scratch = Path(tmp)
        repo, discovery_env = sandbox(scratch / "discovery")
        raw = subprocess.check_output([sys.executable, __file__, "--list-ids"],
                                      cwd=repo, env=discovery_env)
        names = json.loads(raw)
        count = min(args.jobs, len(names))
        groups = {}
        for index in range(count):
            groups[f"py-{index + 1}"] = [sys.executable, "-m", "unittest", *names[index::count]]
        js = sorted(repo.glob("tests/*.test.mjs")) + sorted(repo.glob("tests/js/*.test.mjs"))
        groups["node"] = [node, "--test", "--test-concurrency=2", *[str(p.relative_to(repo)) for p in js]]
        for name in args.browser:
            groups["ui-" + name] = [sys.executable, f"tests/browser/{name}.py"]
        print(f"{len(names)} Python cases in {count} groups, {len(js)} JS files", flush=True)

        def run(label, argv):
            repo, child_env = sandbox(scratch / label)
            if label.startswith("py-"):
                paths = [str(repo / "tests"), child_env.get("PYTHONPATH", "")]
                child_env["PYTHONPATH"] = os.pathsep.join(p for p in paths if p)
            before = time.monotonic()
            with (logs / (label + ".log")).open("w") as log:
                result = subprocess.run(argv, cwd=repo, env=child_env, stdout=log, stderr=log)
            return label, result.returncode, time.monotonic() - before

        failed = False
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            futures = [pool.submit(run, label, argv) for label, argv in groups.items()]
            for future in as_completed(futures):
                label, status, elapsed = future.result()
                print(f"{label}: exit={status}, {elapsed:.2f}s", flush=True)
                failed |= status != 0
    print(f"Total: {time.monotonic() - start:.2f}s", flush=True)
    return int(failed)


if __name__ == "__main__":
    if sys.argv[1:] == ["--list-ids"]:
        discover()
    else:
        sys.exit(main())
