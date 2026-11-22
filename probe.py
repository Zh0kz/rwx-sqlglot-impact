"""Choose low-cost real test replays and retain unfinished tests on timeout."""
import os
import select
import signal
import time
from pathlib import Path

import replay
import warm


def check_group(request, seconds, warm_state=None):
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
            valid = replay.run(request, lambda n: os.write(wfd, (str(n) + "\n").encode("ascii")), warm_state)
            if valid is False:
                os.write(wfd, b"!\n")
        finally:
            os._exit(0)
    os.close(wfd)
    deadline = time.monotonic() + seconds
    data = bytearray()
    try:
        while (remaining := deadline - time.monotonic()) > 0 and select.select([rfd], [], [], remaining)[0]:
            chunk = os.read(rfd, 8192)
            if not chunk:
                break
            data.extend(chunk)
    finally:
        os.close(rfd)
        if os.waitpid(pid, os.WNOHANG)[0] == 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
    if b"!" in data.splitlines():
        return None
    return {int(line) for line in data.splitlines() if line.isdigit()}


def refine(delta, coverage, chosen, catalog, durations, case_index, rewrite, deadline):
    if not chosen or time.monotonic() + 1 >= deadline:
        return chosen
    delta = {p: h for p, h in delta.items() if p not in coverage.identical}
    # Fixture edits already map to the tests that consume their contents.
    # A replay of one inexpensive reader rarely pays its startup cost.
    if all(path.startswith("tests/fixtures/") for path in delta) and max(durations[n] for n in chosen) < 2000:
        return chosen
    sources = {}
    fixtures = {}
    for path, hunks in delta.items():
        try:
            if path.startswith("sqlglot/") and path in coverage.sources:
                module = path[:-3].removesuffix("/__init__").replace("/", ".")
                sources[module] = rewrite(coverage.sources[path], hunks)[0]
            elif path.startswith("tests/fixtures/optimizer/") and path.endswith(".sql") and ".." not in path.split("/"):
                base = (Path("/src") / path).read_text("utf-8")
                fixtures[path] = rewrite(base, hunks)[0] + ("\n" if base.endswith("\n") else "")
            else:
                return chosen
        except (ValueError, OSError):
            return chosen
    scope_mask = case_index.affected(delta, coverage, rewrite)
    options = []
    for n in chosen:
        name = catalog[n]["id"]
        skips, estimate = case_index.estimate(name, scope_mask, durations[n])
        options.append((n, name, skips, estimate))
    total = sum(row[3] for row in options)
    if total > 1700:
        # Partial replay pays when a single test dominates the schedule and
        # most of its baseline cases can be skipped. For other broad changes,
        # retain the static selection.
        options = [r for r in options if r[3] < 1100 and durations[r[0]] > max(750, 4 * r[3])]
    if not options:
        return chosen
    options.sort(key=lambda r: (durations[r[0]] / max(5, r[3]), durations[r[0]]), reverse=True)
    request = {"sources": sources, "fixtures": fixtures, "tests": [r[:3] for r in options]}
    seconds = min(2.5, deadline - time.monotonic())
    request["warm"] = coverage.warm_functions
    passed = warm.check(request, seconds)
    if passed is None:
        passed = check_group(request, seconds)
    return [n for n in chosen if n not in passed]
