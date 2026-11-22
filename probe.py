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


def refine(delta, coverage, chosen, catalog, durations, case_index, rewrite, deadline, scheduler):
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
    use_cache = (Path("/app/executor.z").is_file() and bool(sources) and not fixtures
                 and all(module in coverage.warm_functions
                         and (module.startswith(("sqlglot.optimizer.", "sqlglot.parsers.",
                                                  "sqlglot.generators.", "sqlglot.expressions."))
                              or module in {"sqlglot.parser", "sqlglot.generator"}) for module in sources))
    edited_functions = set()
    if use_cache:
        for path in delta:
            module = path[:-3].removesuffix("/__init__").replace("/", ".")
            affected = {line for ancestry in coverage.warm_functions[module] for line in ancestry}
            for fn in case_index.functions(coverage, path):
                if fn.lineno in affected:
                    first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
                    edited_functions.add((path, first))
    options = []
    for n in chosen:
        name = catalog[n]["id"]
        skips, estimate = case_index.estimate(name, scope_mask, durations[n])
        if use_cache and name.endswith(("::test_execute_tpcds", "::test_execute_tpch")):
            estimate *= 0.28
        options.append((n, name, skips, estimate))
    total = sum(row[3] for row in options)
    if total > 1500:
        # Prefer short replays that can remove a dominant test from the schedule.
        options = [r for r in options if (r[3] < 1400 and durations[r[0]] > max(1250, 3 * r[3]))
                   or (use_cache and r[1].endswith("::test_execute_tpcds"))]
    if not options:
        return chosen
    if len(options) < len(chosen) and max(durations[r[0]] for r in options) < 1800:
        return chosen
    def cost(tests):
        workers = scheduler(tests, durations)
        return 2000 * len(workers) + max((sum(durations[n] for n in g) for g in workers), default=0)

    attempted = {row[0] for row in options}
    improvement = cost(chosen) - cost([n for n in chosen if n not in attempted])
    predicted = sum(row[3] for row in options)
    if improvement <= 350 + 1.5 * predicted:
        return chosen
    options.sort(key=lambda r: (durations[r[0]] / max(5, r[3]), durations[r[0]]), reverse=True)
    request = {"sources": sources, "fixtures": fixtures, "tests": [r[:3] for r in options],
               "paths": list(delta), "executor_cache": use_cache, "edited_functions": edited_functions}
    long_replay = use_cache and any(r[1].endswith("::test_execute_tpcds") for r in options)
    seconds = min(7.0 if long_replay else 2.2, deadline - time.monotonic(), improvement / 1250)
    request["warm"] = coverage.warm_functions
    passed = warm.check(request, seconds)
    if passed is None:
        passed = check_group(request, seconds)
    return [n for n in chosen if n not in passed]
