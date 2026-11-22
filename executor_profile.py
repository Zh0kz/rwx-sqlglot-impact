"""Record input fingerprints and results for original executor queries."""
import functools
import os
import pickle
import sys
import zlib
from pathlib import Path

from executor_cache import fingerprint, optimizer_input
import case_hooks


root = str(Path.cwd()) + "/"
monitoring = sys.monitoring
active = None
expression = None
files = set()
functions = set()
records = {}
expected = {}
optimization = None
optimize_stage = False


@functools.cache
def path(code):
    if code.co_filename.startswith(root + "sqlglot/"):
        return code.co_filename[len(root):]


def visit(code, offset):
    if active and (name := path(code)):
        if optimize_stage:
            functions.add((name, code.co_firstlineno))
        else:
            files.add(name)
    return monitoring.DISABLE


def select(key):
    global active, expression
    active = key
    expression = None


def pytest_sessionstart(session):
    import sqlglot.executor as executor
    from tests import test_executor
    optimize = executor.optimize
    execute = executor.PythonExecutor.execute
    monitoring.use_tool_id(4, "executor dependencies")
    monitoring.register_callback(4, monitoring.events.PY_START, visit)

    def track(*args, **kwargs):
        global expression, optimization, optimize_stage
        if not active or not kwargs.get("leave_tables_isolated"):
            return optimize(*args, **kwargs)
        digest = optimizer_input(args, kwargs)
        functions.clear()
        optimize_stage = True
        monitoring.restart_events()
        monitoring.set_events(4, monitoring.events.PY_START)
        try:
            result = optimize(*args, **kwargs)
        finally:
            optimize_stage = False
            monitoring.set_events(4, 0)
        if active:
            expression = result
            optimization = digest, pickle.dumps(result, protocol=5), tuple(sorted(functions))
            files.clear()
            monitoring.restart_events()
            monitoring.set_events(4, monitoring.events.PY_START)
        return result

    def run(engine, plan, outer_scope=None):
        global active
        key = active if active and expression is not None and outer_scope is None else None
        digest = fingerprint(expression, engine.tables) if key else None
        result = execute(engine, plan, outer_scope=outer_scope)
        if key:
            monitoring.set_events(4, 0)
            records[key] = (digest, pickle.dumps(result, protocol=5),
                               tuple(sorted(files)), *optimization)
            active = None
        return result

    def cached_execute(test, sql, tpch=True):
        if sql not in test.cache:
            translated = test_executor.transpile(sql, write="duckdb")[0]
            conn = test.tpch_conn if tpch else test.tpcds_conn
            test.cache[sql] = conn.execute(translated).fetchdf()
            expected[tpch, sql] = translated, pickle.dumps(test.cache[sql], protocol=5)
        return test.cache[sql]

    executor.optimize = track
    executor.PythonExecutor.execute = run
    test_executor.TestExecutor.cached_execute = cached_execute
    case_hooks.install_executor(lambda k: None, lambda _: None, lambda k: True, select)


def pytest_sessionfinish(session, exitstatus):
    monitoring.set_events(4, 0)
    records["expected"] = expected
    Path(os.environ["EXECUTOR_OUT"]).write_bytes(zlib.compress(pickle.dumps(records, protocol=5), 9))


if __name__ == "__main__":
    from tests.test_executor import TestExecutor
    TestExecutor.setUpClass()
    try:
        tables = [TestExecutor.tpch_tables, TestExecutor.tpcds_tables]
        Path(os.environ["TABLES_OUT"]).write_bytes(zlib.compress(pickle.dumps(tables, protocol=5), 9))
    finally:
        TestExecutor.tearDownClass()
