"""Reuse an unmodified executor result when input and downstream code agree."""
import hashlib
import ast
import inspect
import json
import pickle
import functools
import textwrap
import zlib
from pathlib import Path


def fingerprint(expression, tables):
    raw = json.dumps(expression.dump(), sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode("utf-8"))
    digest.update(table_fingerprint(tables.mapping))
    return digest.hexdigest()


@functools.lru_cache(maxsize=70)
def table_contents(table):
    raw = json.dumps([table.columns, table.rows], separators=(",", ":"), default=repr)
    return hashlib.sha256(raw.encode("utf-8")).digest()


def table_fingerprint(mapping):
    digest = hashlib.sha256()
    for name, value in sorted(mapping.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(table_fingerprint(value) if isinstance(value, dict) else table_contents(value))
    return digest.digest()


def optimizer_input(args, kwargs):
    expression, schema = args[:2]
    cache_input = [expression.dump(), schema.mapping, schema.visible, kwargs]
    raw = json.dumps(cache_input, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExecutorCache:
    def __init__(self, path, changed, edited_functions=None):
        with Path(path).open("rb") as source:
            data = pickle.loads(zlib.decompress(source.read()))
        self.entries = {}
        self.optimized = {}
        self.expected = {}
        for key, row in data.items():
            if key == "expected":
                self.expected = row
                continue
            digest, value, files, *details = row
            if changed.isdisjoint(files):
                self.entries[key] = digest, value
            if details and edited_functions is not None:
                input_digest, expression, called = details
                if edited_functions.isdisjoint(called):
                    self.optimized[key] = input_digest, expression
        self.key = None
        self.expression = None

    def begin(self, key):
        self.key = key if key in self.entries or key in self.optimized else None
        self.expression = None

    def install(self):
        import sqlglot.executor as executor
        optimize = executor.optimize
        execute = executor.PythonExecutor.execute

        def track(*args, **kwargs):
            trackable = self.key is not None and kwargs.get("leave_tables_isolated")
            previous = self.optimized.get(self.key) if trackable else None
            expression = None
            if previous:
                try:
                    if optimizer_input(args, kwargs) == previous[0]:
                        expression = pickle.loads(previous[1])
                except (AttributeError, TypeError, ValueError):
                    pass
            if expression is None:
                expression = optimize(*args, **kwargs)
            if trackable:
                self.expression = expression
            return expression

        def run(engine, plan, outer_scope=None):
            key, self.key = self.key, None
            if key in self.entries and self.expression is not None and outer_scope is None:
                digest, value = self.entries[key]
                try:
                    matches = fingerprint(self.expression, engine.tables) == digest
                except (TypeError, ValueError, AttributeError):
                    matches = False
                if matches:
                    return pickle.loads(value)
            return execute(engine, plan, outer_scope=outer_scope)

        executor.optimize = track
        executor.PythonExecutor.execute = run

        if self.expected:
            from tests import test_executor
            def cached_execute(test, sql, tpch=True):
                if sql not in test.cache:
                    translated = test_executor.transpile(sql, write="duckdb")[0]
                    previous = self.expected.get((tpch, sql))
                    if previous and previous[0] == translated:
                        test.cache[sql] = pickle.loads(previous[1])
                    else:
                        conn = test.tpch_conn if tpch else test.tpcds_conn
                        test.cache[sql] = conn.execute(translated).fetchdf()
                return test.cache[sql]
            test_executor.TestExecutor.cached_execute = cached_execute


def restore_fixture_tables(path):
    from tests import test_executor
    cls = test_executor.TestExecutor
    original = cls.setUpClass.__func__
    tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
    method = tree.body[0]
    method.decorator_list = []
    setup = next(n for n in method.body if isinstance(n, ast.FunctionDef) and n.name == "setup")
    # Keep the upstream filename and DuckDB view. The rest of setup reads the
    # same CSV into a Python Table, which can be restored from disk.
    setup.body[2:] = ast.parse("tables[table] = _rwx_tables[directory][table]").body
    with Path(path).open("rb") as source:
        tpch, tpcds = pickle.loads(zlib.decompress(source.read()))
    namespace = dict(original.__globals__, _rwx_tables={test_executor.DIR_TPCH: tpch, test_executor.DIR_TPCDS: tpcds})
    exec(compile(ast.fix_missing_locations(tree), test_executor.__file__, "exec"), namespace)
    cls.setUpClass = classmethod(namespace[method.name])
