"""Observe bounded primitive operand values at comparison sites in the base suite."""
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
idx = {t["id"]: i for i,t in enumerate(tests)}
current = 0
values = defaultdict(dict)
unmodeled = defaultdict(int)
operations = {"Eq":operator.eq, "NotEq":operator.ne, "Lt":operator.lt, "LtE":operator.le, "Gt":operator.gt,
              "GtE":operator.ge, "In":lambda a,b:a in b, "NotIn":lambda a,b:a not in b}
BAD = object()


def primitive(obj):
    if obj is None or type(obj) in (int, bool):
        return obj
    if type(obj) is str and len(obj) <= 80:
        return obj
    if type(obj) is tuple and len(obj) <= 8 and all(type(v) in (int,bool,str) for v in obj):
        return obj
    return BAD


def observe(site, op, left, right):
    result = operations[op](left, right)
    if current:
        a, b = primitive(left), primitive(right)
        if a is BAD or b is BAD or type(result) is not bool:
            unmodeled[site] |= current
        else:
            key = a, b, result
            rows = values[site]
            if key in rows or len(rows) < 64:
                rows[key] = rows.get(key, 0) | current
            else:
                unmodeled[site] |= current
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]).__name__ not in operations:
            return node
        rhs = node.comparators[0]
        if not isinstance(rhs, (ast.Constant, ast.Tuple)):
            return node
        site = f"{self.module}:{node.lineno}:{node.col_offset}"
        call = ast.Call(ast.Name("_rwx_primitive_comparison", ast.Load()),
                        [ast.Constant(site), ast.Constant(type(node.ops[0]).__name__),node.left,rhs],[])
        return ast.copy_location(call,node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        parsed = Transform(fullname).visit(ast.parse(self.get_data(self.path),self.path))
        return compile(ast.fix_missing_locations(parsed),self.path,"exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname,spec.origin)
                return spec


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_primitive_comparison = observe
    sys.meta_path.insert(0,Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item,nextitem):
    global current
    current = 1 << idx[item.nodeid] if item.nodeid in idx else 0
    yield
    current = 0


def pytest_sessionfinish(session,exitstatus):
    masks = {}
    def ix(mask):
        return masks.setdefault(mask,len(masks))
    sites = {k:[ix(unmodeled.get(k,0)), [[a,b,r,ix(mask)] for (a,b,r),mask in rows.items()]]
             for k,rows in values.items()}
    payload = json.dumps({"sites":sites,"masks":[hex(m) for m in masks]},separators=(",",":")).encode()
    Path(os.environ["VALUE_OUT"]).write_bytes(zlib.compress(payload,9))
