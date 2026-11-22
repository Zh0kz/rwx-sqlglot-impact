"""Compare the original fixture readers' inputs after a SQL fixture edit."""
import functools
import json
import os
import subprocess
import sys
from pathlib import Path


TPC = {
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": (
        "tests/test_optimizer.py::TestOptimizer::test_tpch",
        "tests/test_executor.py::TestExecutor::test_execute_tpch",
        "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    ),
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": (
        "tests/test_optimizer.py::TestOptimizer::test_tpcds",
        "tests/test_executor.py::TestExecutor::test_execute_tpcds",
        None,
    ),
}


def filtered(source):
    return "\n".join(line for line in source.splitlines() if line and not line.startswith("--"))


def pairs(source):
    pieces = source.split(";")
    result = []
    for text, expected in zip(pieces[::2], pieces[1::2]):
        sql = text.strip().split("\n")
        meta = {}
        while sql and sql[0].startswith("#"):
            key, value = sql.pop(0).split(":", 1)
            meta[key.lstrip("#").strip()] = value.strip()
        if not sql:
            raise ValueError("fixture metadata has no SQL")
        result.append((meta, "\n".join(sql), expected.strip()))
    return result


@functools.cache
def original(path):
    return (Path(os.environ.get("SQLGLOT_ROOT", "/src")) / path).read_text(encoding="utf-8")


def executor_inputs_changed(old, new):
    baseline = {sql for _, sql, _ in old}
    executed = {sql for metadata, sql, _ in old if metadata.get("execute")}
    if any(m.get("execute") and sql not in executed for m, sql, _ in new):
        return True
    untested = {sql for m, sql, _ in new if not m.get("execute") and sql not in baseline}
    if not untested:
        return False
    if len(untested) > 8 or sum(map(len, untested)) > 60000:
        return True
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import json,sys; from sqlglot import parse_one; "
             "[parse_one(sql) for sql in json.load(sys.stdin)]"],
            cwd=os.environ.get("SQLGLOT_ROOT", "/src"), input=json.dumps(sorted(untested)), text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 0


def select(path, hunks, coverage, rewrite):
    if not path.startswith("tests/fixtures/optimizer/") or not path.endswith(".sql"):
        return None
    if ".." in path.split("/") or not hunks or path not in coverage.files:
        return None
    try:
        before = original(path)
        after, _ = rewrite(before, hunks)
        base, new = filtered(before), filtered(after)
        if base == new:
            return 0
        if path not in TPC:
            return None
        old_pairs, new_pairs = pairs(base), pairs(new)
    except (OSError, ValueError):
        return None
    if old_pairs == new_pairs:
        return 0
    optimizer, execute, optimized = TPC[path]
    selected = {optimizer}
    if executor_inputs_changed(old_pairs, new_pairs):
        selected.add(execute)
    if optimized and [(sql, exp) for _, sql, exp in old_pairs] != [
        (sql, exp) for _, sql, exp in new_pairs
    ]:
        selected.add(optimized)
    return sum(1 << i for i, name in enumerate(coverage.ids) if name in selected)
