"""Choose low-cost real test replays and retain unfinished tests on timeout."""
import os
import select
import signal
import time
from pathlib import Path

import replay
import warm
from case_hooks import label
from fixtures import TPC, filtered, pairs


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
    python_cache = bool(sources) and not fixtures and all(
        module in coverage.warm_functions
        and (module.startswith(("sqlglot.optimizer.", "sqlglot.parsers.", "sqlglot.generators.", "sqlglot.expressions."))
             or module in {"sqlglot.parser", "sqlglot.generator"}) for module in sources
    )
    fixture_cache = not sources and len(fixtures) == 1 and next(iter(fixtures)) in TPC
    use_cache = Path("/app/executor.z").is_file() and (python_cache or fixture_cache)
    edited_functions = set()
    if python_cache:
        for path in delta:
            module = path[:-3].removesuffix("/__init__").replace("/", ".")
            affected = {line for ancestry in coverage.warm_functions[module] for line in ancestry}
            for fn in case_index.functions(coverage, path):
                if fn.lineno in affected:
                    first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
                    edited_functions.add((path, first))
    forced = {}
    if fixture_cache:
        path, changed = next(iter(fixtures.items()))
        _, method, _ = TPC[path]
        try:
            before = pairs(filtered((Path("/src") / path).read_text("utf-8")))
            after = pairs(filtered(changed))
        except (OSError, ValueError):
            return chosen
        tpch = path.endswith("tpc-h.sql")
        forced[method] = {label(("executor", tpch, i, sql)) for i, (meta, sql, _) in enumerate(after)
                          if i >= len(before) or (meta.get("execute"), sql) != (before[i][0].get("execute"), before[i][1])}
    options = []
    for n in chosen:
        name = catalog[n]["id"]
        skips, estimate = case_index.estimate(name, scope_mask, durations[n])
        if name in forced:
            skips = [key for key in skips if key not in forced[name]]
            estimate = max(estimate, 300 + 600 * len(forced[name]))
        if use_cache and name.endswith(("::test_execute_tpcds", "::test_execute_tpch")):
            estimate *= 0.28
        options.append((n, name, skips, estimate))
    total = sum(row[3] for row in options)
    heavy_only = os.environ.get("RUNTIME_REPLAY") == "heavy"
    if heavy_only:
        options = [r for r in options if durations[r[0]] >= 2500 and r[3] < 7500]
    elif total > 1500:
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
    if improvement <= (4000 if heavy_only else 350) + 1.5 * predicted:
        return chosen
    options.sort(key=lambda r: (durations[r[0]] / max(5, r[3]), durations[r[0]]), reverse=True)
    request = {"sources": sources, "fixtures": fixtures, "tests": [r[:3] for r in options],
               "paths": list(delta), "executor_cache": use_cache, "edited_functions": edited_functions}
    long_replay = use_cache and any(r[1].endswith("::test_execute_tpcds") for r in options)
    seconds = min((9.0 if heavy_only else 7.0) if long_replay else 2.2,
                  deadline - time.monotonic(), improvement / 1250)
    request["warm"] = coverage.warm_functions
    passed = warm.check(request, seconds)
    if passed is None:
        passed = check_group(request, seconds)
    return [n for n in chosen if n not in passed]
