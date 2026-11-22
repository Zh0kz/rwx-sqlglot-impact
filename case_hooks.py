"""Scope individual independent sqlglot test cases for profiling and replay."""
import ast
import functools
import hashlib
import inspect
import pickle
import sys
from pathlib import Path


@functools.cache
def standalone(filename):
    tree = ast.parse(Path(filename).read_bytes())
    return {n.value.lineno for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)}


def label(parts):
    return hashlib.blake2b(repr(parts).encode("utf-8"), digest_size=12).hexdigest()


def install(enter, leave, keep):
    from tests.dialects.test_dialect import Validator
    from tests import helpers

    def validator(name):
        original = getattr(Validator, name)
        @functools.wraps(original)
        def wrapped(self, *args, **kwargs):
            frame = sys._getframe(1)
            if frame.f_lineno not in standalone(frame.f_code.co_filename):
                return original(self, *args, **kwargs)
            key = label((name, frame.f_code.co_name, frame.f_lineno, self.dialect, args, kwargs))
            if not keep(key):
                return None
            token = enter(key)
            try:
                return original(self, *args, **kwargs)
            finally:
                leave(token)
        setattr(Validator, name, wrapped)

    for name in ("validate_identity", "validate_all", "validate_transpile", "assert_duckdb_sql"):
        validator(name)

    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        original = getattr(helpers, name)
        def make_generator(fn):
            @functools.wraps(fn)
            def scoped(filename):
                for item in fn(filename):
                    key = label(("fixture", filename, item))
                    if keep(key):
                        token = enter(key)
                        try:
                            yield item
                        finally:
                            leave(token)
            return scoped
        setattr(helpers, name, make_generator(original))


def install_executor(enter, leave, keep):
    from tests import test_executor as executor
    # The original method dispatches independent queries to workers and then
    # checks the results in submission order. Preserve the pickle boundary and
    # the original query index when running one of those cases locally.
    def per_query(self, schema, tables, sqls, tpch):
        executor.initializer(schema, tables)
        for i, (meta, sql, _) in enumerate(sqls):
            key = label(("executor", tpch, i, sql))
            if keep(key):
                token = enter(key)
                try:
                    expression = pickle.loads(pickle.dumps(executor.parse_one(sql)))
                    table = executor.mp_execute(expression, meta)
                    if table is not None:
                        self.subtestHelper(i, pickle.loads(pickle.dumps(table)), tpch=tpch)
                finally:
                    leave(token)
    executor.TestExecutor._mp_execute = per_query


def install_memory(enter, leave, keep):
    from tests import test_generator
    original = test_generator.TestGenerator.test_generator_memory
    @functools.wraps(original)
    def scoped(self):
        dialects = test_generator.Dialects
        def cases():
            for dialect in dialects:
                key = label(("generator-memory", dialect.value))
                if keep(key):
                    token = enter(key)
                    try:
                        yield dialect
                    finally:
                        leave(token)
        test_generator.Dialects = cases()
        try:
            return original(self)
        finally:
            test_generator.Dialects = dialects
    test_generator.TestGenerator.test_generator_memory = scoped
