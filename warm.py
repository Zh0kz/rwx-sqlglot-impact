"""Fork pristine, import-warmed interpreters and replace verified function bodies."""
import ast
import gc
import os
import sys
import types
from collections import defaultdict
from multiprocessing import Pipe


connection = None
server_pid = None


def code_tree(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from code_tree(value)


def prepare():
    import concurrent.futures
    import case_hooks
    import replay
    from cold_map import InlinePool
    import duckdb
    import numpy
    import pandas
    seen = set()
    mon = sys.monitoring
    def entered(code, offset):
        if code.co_filename.startswith("/src/sqlglot/"):
            seen.add((code.co_filename, code.co_firstlineno, code.co_qualname))
        return mon.DISABLE
    mon.use_tool_id(3, "initialization coverage")
    mon.register_callback(3, mon.events.PY_START, entered)
    mon.set_events(3, mon.events.PY_START)
    sys.path.insert(0, "/src")
    os.chdir("/src")
    concurrent.futures.ProcessPoolExecutor = InlinePool
    case_hooks.install(lambda _: None, lambda _: None, replay.keep_case)
    from tests import test_executor, test_generator, test_optimizer
    mon.set_events(3, 0)
    mon.free_tool_id(3)
    functions = defaultdict(list)
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            code = obj.__code__
            if code.co_filename.startswith("/src/sqlglot/"):
                functions[(code.co_filename, code.co_qualname, code.co_firstlineno)].append(obj)
    return seen, functions


def replace(request, state):
    seen, functions = state
    for module, source in request["sources"].items():
        if module not in sys.modules:
            continue
        path = sys.modules[module].__file__
        starts = request["warm"].get(module)
        if not starts:
            return False
        with open(path, "rb") as stream:
            original = stream.read()
        before = list(code_tree(compile(original, path, "exec")))
        after = list(code_tree(compile(source, path, "exec")))
        nodes = {n.lineno: n for n in ast.walk(ast.parse(original))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for ancestry in starts:
            parents = []
            for line in ancestry:
                node = nodes.get(line)
                if node is None:
                    return False
                first = min([line, *(d.lineno for d in node.decorator_list)])
                roots = [c for c in before if c.co_firstlineno == first and c.co_name == node.name]
                if len(roots) != 1:
                    return False
                parents.append(roots[0])
            changed = list(code_tree(parents[-1]))
            if any((path, c.co_firstlineno, c.co_qualname) in seen for c in changed):
                return False
            for old in [*parents[:-1], *changed]:
                live = functions.get((path, old.co_qualname, old.co_firstlineno))
                if not live:
                    continue
                matches = [c for c in after if c.co_qualname == old.co_qualname]
                if len(matches) != 1 or matches[0].co_freevars != old.co_freevars:
                    return False
                for fn in live:
                    fn.__code__ = matches[0]
    return True


def serve(pipe):
    from probe import check_group
    try:
        state = prepare()
    except Exception:
        state = None
    snapshots_ready = False
    snapshot_imports = set()
    while True:
        try:
            item = pipe.recv()
        except EOFError:
            return
        if item is None:
            return
        request, seconds = item
        if state is None or any(module in snapshot_imports or module in sys.modules and module not in request["warm"]
                                for module in request["sources"]):
            passed = None
        else:
            if request.get("executor_cache") and not snapshots_ready:
                snapshots_ready = True
                loaded = set(sys.modules)
                try:
                    from executor_cache import read_snapshot, table_contents
                    read_snapshot("/app/executor.z")
                    for tables in read_snapshot("/app/executor_tables.z"):
                        for table in tables.values():
                            table_contents(table)
                except (OSError, ValueError, TypeError):
                    pass
                snapshot_imports = set(sys.modules) - loaded
                if snapshot_imports.intersection(request["sources"]):
                    pipe.send(None)
                    continue
            passed = check_group(request, seconds, state)
        pipe.send(passed)


def check(request, seconds):
    global connection, server_pid
    if connection is None:
        connection, child = Pipe()
        server_pid = os.fork()
        if server_pid == 0:
            connection.close()
            try:
                serve(child)
            finally:
                os._exit(0)
        child.close()
    try:
        connection.send((request, seconds))
        return connection.recv()
    except (EOFError, OSError):
        return set()


def close():
    if connection:
        try:
            connection.send(None)
        except OSError:
            pass
        connection.close()
        os.waitpid(server_pid, 0)
