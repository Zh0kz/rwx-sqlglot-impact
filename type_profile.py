"""Collect operand types at simple comparisons on the pinned revision."""
import ast
import builtins
import enum
import functools
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys
from collections import defaultdict
from pathlib import Path

observations = defaultdict(set)
operations = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne}


@functools.cache
def type_info(t):
    if t is type(None):
        return "none"
    if t is bool:
        return "bool"
    if issubclass(t, enum.Enum) and t.__eq__ in (object.__eq__, str.__eq__, int.__eq__):
        # One canonical member per value within a single enumeration.
        return "enum:" + t.__module__ + "." + t.__qualname__
    if t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__:
        return "identity"
    if t in (int, str, bytes, tuple, list, dict, set, float):
        return "builtin"
    return "unknown"


def observe(site, operation, left, right):
    observations[site].add((type_info(type(left)), type_info(type(right))))
    return operations[operation](left, right)


class Transformer(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]).__name__ not in operations:
            return node
        rhs = node.comparators[0]
        if not (isinstance(rhs, ast.Constant) or isinstance(rhs, ast.Attribute) and rhs.attr.isupper()
                or isinstance(node.ops[0], (ast.Is, ast.IsNot))):
            return node
        site = f"{self.module}:{node.lineno}:{node.col_offset}"
        call = ast.Call(ast.Name("_rwx_observe_pair", ast.Load()),
                        [ast.Constant(site), ast.Constant(type(node.ops[0]).__name__), node.left, rhs], [])
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = Transformer(fullname).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
                return spec


def pytest_sessionstart(session):
    # Run workers in-process so their operand types are captured as well.
    import concurrent.futures
    class LocalPool(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, mp_context=None, **kwargs):
            super().__init__(max_workers=1, **kwargs)
    concurrent.futures.ProcessPoolExecutor = LocalPool
    builtins._rwx_observe_pair = observe
    sys.meta_path.insert(0, Finder())


def pytest_sessionfinish(session, exitstatus):
    output = {site: sorted(pairs) for site, pairs in observations.items()}
    Path(os.environ["TYPES_OUT"]).write_text(json.dumps(output, separators=(",", ":")))
