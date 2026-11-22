"""Measure line and fixture coverage with each unittest in a fresh fork (Linux)."""
import concurrent.futures
import functools
import importlib
import json
import multiprocessing
import os
import pickle
import signal
import sys
import unittest
import zlib
from pathlib import Path
from collections import defaultdict

ROOT = str(Path.cwd()) + "/"


class InlinePool:
    def __init__(self, *, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def shutdown(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except Exception as exc:
            future.set_exception(exc)
        return future


@functools.cache
def relative(filename):
    return filename.removeprefix(ROOT) if filename.startswith(ROOT) else ""


def measure(item):
    idx, node = item
    paths = defaultdict(set)
    fixture_paths = set()
    mon = sys.monitoring
    def visit(code, number):
        path = relative(code.co_filename)
        if code.co_flags & 1 and path.startswith(("sqlglot/", "tests/")):
            paths[path].add(number)
        return mon.DISABLE

    def opened(event, args):
        if event == "open" and isinstance(args[0], str) and "tests/fixtures/" in args[0]:
            fixture_paths.add("tests/fixtures/" + args[0].split("tests/fixtures/", 1)[1])

    def timed_out(*args):
        raise TimeoutError("single test coverage budget")

    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(110)
    mon.use_tool_id(3, "cold test")
    mon.register_callback(3, mon.events.LINE, visit)
    sys.addaudithook(opened)
    try:
        file, *names = node.split("::")
        suite = unittest.defaultTestLoader.loadTestsFromName(".".join([file[:-3].replace("/", "."), *names]))
        mon.set_events(3, mon.events.LINE)
        result = unittest.TestResult()
        suite.run(result)
        ok = result.wasSuccessful() and result.testsRun == 1
    except Exception:
        ok = False
    finally:
        mon.set_events(3, 0)
        signal.alarm(0)
    return idx, ok, {p: list(rows) for p, rows in paths.items()}, fixture_paths


def main():
    tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
    concurrent.futures.ProcessPoolExecutor = InlinePool
    # Only import modules before forking. Class setup, tests and cleanup run in
    # their own processes so a cache populated by another test cannot mask a read.
    for file in sorted({t["file"] for t in tests}):
        try:
            importlib.import_module(file[:-3].replace("/", "."))
        except ImportError:
            pass
    paths = defaultdict(dict)
    fixtures = defaultdict(int)
    seen = 0
    failures = []
    with multiprocessing.get_context("fork").Pool(4, maxtasksperchild=1) as pool:
        for count, (i, ok, rows, files) in enumerate(pool.imap_unordered(measure, enumerate(t["id"] for t in tests)), 1):
            mask = 1 << i
            if ok:
                seen |= mask
            else:
                failures.append(tests[i]["id"])
            for p, numbers in rows.items():
                for n in numbers:
                    paths[p][n] = paths[p].get(n, 0) | mask
            for p in files:
                fixtures[p] |= mask
            if count % 200 == 0:
                print(count, flush=True)
    table = {}
    def ix(mask):
        return table.setdefault(mask, len(table))
    output = {"tests": [t["id"] for t in tests], "seen": ix(seen), "files": {p: ix(m) for p, m in fixtures.items()},
              "lines": {p: {n: ix(m) for n, m in values.items()} for p, values in paths.items()}}
    output["masks"] = [hex(m) for m in table]
    Path(os.environ["MAP_OUT"]).write_bytes(zlib.compress(json.dumps(output, separators=(",", ":")).encode(), 9))
    print("Incomplete tests:", failures)


if __name__ == "__main__":
    main()
