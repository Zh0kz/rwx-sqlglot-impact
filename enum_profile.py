"""Observe actual enum operands at comparisons in the unmodified suite."""
import ast
import builtins
import concurrent.futures
import enum
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
indices = {test["id"]: i for i, test in enumerate(tests)}
current = 0
rows = defaultdict(dict)
unknown = defaultdict(int)
enums = {}
ops = {"Eq": operator.eq, "NotEq": operator.ne, "Is": operator.is_, "IsNot": operator.is_not}


def scalar(value):
    kind = type(value)
    if kind in (str, int, bool, type(None)):
        return ("literal", value)
    if not isinstance(value, enum.Enum) or not kind.__module__.startswith("sqlglot"):
        return None
    type_name = kind.__module__ + ":" + kind.__qualname__
    if type_name not in enums:
        enums[type_name] = {k: v.name for k, v in kind.__members__.items()}
    return type_name, value.name


def observe(site, op, left, right):
    result = ops[op](left, right)
    if current and isinstance(right, enum.Enum):
        lhs, rhs = scalar(left), scalar(right)
        if lhs is None or rhs is None or type(result) is not bool:
            unknown[site] |= current
        else:
            # When the operand types differ, a numeric or string mixin could
            # affect equality. Only identity is modelled for that situation.
            signature = lhs, rhs, result
            rows[site][signature] = rows[site].get(signature, 0) | current
    elif current:
        unknown[site] |= current
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (len(node.ops) != 1 or type(node.ops[0]).__name__ not in ops
                or not isinstance(node.comparators[0], ast.Attribute)):
            return node
        site = f"{self.module}:{node.lineno}:{node.col_offset}"
        call = ast.Call(ast.Name("_rwx_enum_compare", ast.Load()),
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
    builtins._rwx_enum_compare = observe
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
    sites = {key: [ix(unknown.get(key, 0)), [[a, b, result, ix(mask)] for (a, b, result), mask in values.items()]]
             for key, values in rows.items()}
    data = {"sites": sites, "masks": [hex(mask) for mask in masks], "enums": enums}
    Path(os.environ["ENUM_OUT"]).write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
