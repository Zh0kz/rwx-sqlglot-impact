"""Compare the original fixture readers' inputs after a SQL fixture edit."""
import functools
import os
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
    if [(m.get("execute"), sql) for m, sql, _ in old_pairs] != [
        (m.get("execute"), sql) for m, sql, _ in new_pairs
    ]:
        selected.add(execute)
    if optimized and [(sql, exp) for _, sql, exp in old_pairs] != [
        (sql, exp) for _, sql, exp in new_pairs
    ]:
        selected.add(optimized)
    return sum(1 << i for i, name in enumerate(coverage.ids) if name in selected)
