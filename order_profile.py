"""Observe boundary equality for ordered primitive comparisons in the base suite."""
import ast
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
index = {t["id"]: i for i, t in enumerate(tests)}
current = 0
results = defaultdict(int)
sites = set()
OPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
LOOKUP = {op.__name__: method for op, method in OPS.items()}


def primitive(value):
    if type(value) in (int, bool, str, bytes):
        return True
    return type(value) is tuple and len(value) <= 20 and all(type(x) in (int, bool, str, bytes) for x in value)


def compare(site, op, a, b):
    if current:
        sites.add(site)
        if not (primitive(a) and primitive(b)) or a == b:
            results[site] |= current
    return LOOKUP[op](a, b)


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = f"{self.module}:{node.lineno}:{node.col_offset}"
        call = ast.Call(ast.Name("_rwx_order_boundary", ast.Load()),
                        [ast.Constant(site), ast.Constant(type(node.ops[0]).__name__),
                         node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)


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
    builtins._rwx_order_boundary = compare
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    rows = {site: hex(results[site]) for site in sites}
    Path(os.environ["ORDER_OUT"]).write_bytes(zlib.compress(json.dumps(rows,separators=(",", ":")).encode(), 9))
