"""Collect bounded observations at simple baseline Python conditions."""
import ast
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import json
import math
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool
import decisions


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
indices = {test["id"]: i for i, test in enumerate(tests)}
current = 0
contents = {}
rows = defaultdict(dict)
unknown = defaultdict(int)
before_tests = set()
missing = object()


def primitive(obj):
    kind = type(obj)
    if obj is None or kind is bool:
        return kind.__name__, obj
    if kind is int and obj.bit_length() <= 64:
        return "int", obj
    if kind is float and math.isfinite(obj):
        return "float", obj
    if kind is str and len(obj) <= 90:
        return "str", obj
    if kind is tuple and len(obj) <= 8 and all(type(x) in (int, str) for x in obj):
        return "tuple", obj
    return None


def observe(site, outcome):
    result = bool(outcome)
    if not current:
        before_tests.add(site)
        return result
    frame = sys._getframe(1)
    scope = frame.f_locals
    encoded = []
    for name in contents[site]:
        if name.startswith("len:"):
            if scope.get("len", frame.f_globals.get("len", len)) is not len:
                break
            value = scope.get(name[4:], missing)
            if type(value) not in (list, tuple, dict, set, frozenset, str, bytes):
                break
            value = len(value)
        else:
            value = scope.get(name, frame.f_globals.get(name, missing))
        snapshot = primitive(value)
        if snapshot is None:
            break
        encoded.append(snapshot)
    else:
        states = rows[site]
        key = tuple(encoded), result
        if key in states or len(states) < 96:
            states[key] = states.get(key, 0) | current
            return result
    unknown[site] |= current
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_If(self, node):
        self.generic_visit(node)
        test = node.test
        names = set()
        try:
            decisions.expression(test, names)
        except ValueError:
            return node
        if len(names) > 6:
            return node
        site = f"{self.module}:{test.lineno}:{test.col_offset}"
        contents[site] = sorted(names)
        call = ast.Call(ast.Name("_rwx_profile_condition", ast.Load()), [ast.Constant(site), test], [])
        node.test = ast.copy_location(call, test)
        return node

    visit_While = visit_If
    visit_IfExp = visit_If


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
    builtins._rwx_profile_condition = observe
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << indices[item.nodeid] if item.nodeid in indices else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    masks = {}
    def ix(mask):
        return masks.setdefault(mask, len(masks))
    sites = {site: [contents[site], ix(unknown[site]), [[a, b, ix(mask)] for (a, b), mask in states.items()]]
             for site, states in rows.items() if site not in before_tests}
    data = {"sites": sites, "masks": [hex(mask) for mask in masks]}
    Path(os.environ["DECISION_OUT"]).write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
