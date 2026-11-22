"""Track which reference calls receive each function's declared default."""
import ast
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import json
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool
from behavior import defaults


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
indices = {test["id"]: i for i, test in enumerate(tests)}
current = 0
rows = defaultdict(int)
observed = set()
at_import = set()


def observe(site, value, default):
    observed.add(site)
    if type(value) is type(default) and value == default:
        if current:
            rows[site] |= current
        else:
            at_import.add(site)


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        diagnostics = []
        for parameter, default in defaults(node.args):
            if not isinstance(default, ast.Constant) or type(default.value) not in (str, int, float, bool, type(None)):
                continue
            site = f"{self.module}:{node.lineno}:{parameter.arg}"
            call = ast.Call(ast.Name("_rwx_function_default", ast.Load()),
                            [ast.Constant(site), ast.Name(parameter.arg, ast.Load()), ast.Constant(default.value)], [])
            diagnostics.append(ast.copy_location(ast.Expr(call), node))
        first = node.body[0]
        position = int(isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                       and isinstance(first.value.value, str))
        node.body[position:position] = diagnostics
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = Transform(fullname).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("sqlglot.") or fullname == "sqlglot":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_function_default = observe
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << indices[item.nodeid] if item.nodeid in indices else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    data = {site: hex(rows[site]) for site in observed - at_import}
    Path(os.environ["DEFAULT_OUT"]).write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
