"""Record whether reference dictionary lookups would observe a different value."""
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


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
indices = {test["id"]: i for i, test in enumerate(tests)}
current = 0
rows = defaultdict(dict)
unknown = defaultdict(int)


def observe(mapping, site, key, default=None):
    result = mapping.get(key, default)
    if not current:
        return result
    if type(mapping) is not dict or len(mapping) > 50 or type(key) is not str:
        unknown[site] |= current
        return result
    keys = sorted(k for k in mapping if type(k) is str)
    # All non-string keys are left unmodelled since __eq__ or __hash__ could
    # affect a lookup by a new string key.
    if len(keys) != len(mapping):
        unknown[site] |= current
        return result
    matching = tuple(k for k in keys if mapping[k] is result)
    different = tuple(k for k in keys if mapping[k] is not result)
    differs_by_default = result is not default
    cache = rows[site]
    signature = matching, different, differs_by_default
    if signature in cache or len(cache) < 128:
        cache[signature] = cache.get(signature, 0) | current
    else:
        unknown[site] |= current
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Call(self, node):
        self.generic_visit(node)
        if (not isinstance(node.func, ast.Attribute) or node.func.attr != "get"
                or node.keywords or len(node.args) not in (1, 2)
                or not isinstance(node.args[0], ast.Constant) or type(node.args[0].value) is not str):
            return node
        site = ast.Constant(f"{self.module}:{node.lineno}:{node.col_offset}")
        call = ast.Call(ast.Name("_rwx_dictionary_lookup", ast.Load()), [node.func.value, site, *node.args], [])
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
    builtins._rwx_dictionary_lookup = observe
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
    sites = {site: [ix(unknown[site]), [[a, b, diff, ix(mask)] for (a, b, diff), mask in rows[site].items()]]
             for site in set(rows) | set(unknown)}
    data = {"sites": sites, "masks": [hex(mask) for mask in masks]}
    Path(os.environ["LOOKUP_OUT"]).write_bytes(zlib.compress(json.dumps(data,separators=(",", ":")).encode(), 9))
