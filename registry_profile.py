"""Measure individual entry reads in pinned generator type mappings."""
import ast
import builtins
import concurrent.futures
import functools
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


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
index = {t["id"]: i for i, t in enumerate(tests)}
current = 0
rows = defaultdict(dict)
proxies = {}


@functools.cache
def sites(cls, name):
    return [c.__module__ + ":" + c.__qualname__ + "." + name for c in cls.__mro__ if name in vars(c)]


def key_name(key):
    if isinstance(key, str):
        return "str:" + key
    if type(key).__name__ == "DType":
        return "dtype:" + key.name
    return "*"


class Reads(dict):
    def __init__(self, value, keys):
        super().__init__(value)
        self.original = value
        self.records = [rows[site] for site in keys]

    def mark(self, key):
        for records in self.records:
            records[key] = records.get(key, 0) | current

    def __contains__(self, item):
        self.mark(key_name(item))
        return self.original.__contains__(item)

    def __getitem__(self, item):
        self.mark(key_name(item))
        return self.original[item]

    def get(self, item, default=None):
        self.mark(key_name(item))
        return self.original.get(item, default)

    def items(self):
        self.mark("*")
        return self.original.items()

    def keys(self):
        self.mark("*")
        return self.original.keys()

    def values(self):
        self.mark("*")
        return self.original.values()

    def __iter__(self):
        self.mark("*")
        return iter(self.original)

    def copy(self):
        self.mark("*")
        return self.original.copy()

    def __eq__(self, other):
        self.mark("*")
        return self.original == other


def read(receiver, attr):
    value = getattr(receiver, attr)
    if not current or type(value) is not dict:
        return value
    cls = receiver if isinstance(receiver, type) else type(receiver)
    key = id(value), cls
    if key not in proxies:
        proxies[key] = Reads(value, sites(cls, attr))
    return proxies[key]


class Transform(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr == "TYPE_MAPPING" and isinstance(node.ctx, ast.Load):
            call = ast.Call(ast.Name("_rwx_registry_read", ast.Load()),
                            [node.value, ast.Constant(node.attr)], [])
            return ast.copy_location(call, node)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = Transform().visit(ast.parse(self.get_data(self.path), self.path))
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
    builtins._rwx_registry_read = read
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    data = {name: {key: hex(m) for key, m in records.items()} for name, records in rows.items()}
    Path(os.environ["REGISTRY_OUT"]).write_bytes(zlib.compress(json.dumps(data,separators=(",", ":")).encode(), 9))
