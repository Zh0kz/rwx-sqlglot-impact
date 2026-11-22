"""Observe the reference parser's lookahead at token matching call sites."""
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
MATCHERS = {"_match", "_match_pair", "_match_set", "_match_texts"}


def observe(method, site, *args, **kwargs):
    if not current:
        return method(*args, **kwargs)
    if getattr(method, "__module__", None) != "sqlglot.parser" or method.__name__ not in MATCHERS:
        unknown[site] |= current
        return method(*args, **kwargs)
    owner, name = method.__self__, method.__name__
    if name == "_match_texts":
        token = owner._curr
        value = (token.token_type not in owner.TEXT_MATCH_EXCLUDED_TOKENS, token.text.upper())
    elif name == "_match_pair":
        value = owner._curr.token_type.name, owner._next.token_type.name
    else:
        value = (owner._curr.token_type.name,)
    result = method(*args, **kwargs)
    recorded = rows[site]
    key = value, result
    if key in recorded or len(recorded) < 128:
        recorded[key] = recorded.get(key, 0) | current
    else:
        unknown[site] |= current
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Call(self, node):
        self.generic_visit(node)
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in MATCHERS:
            return node
        site = ast.Constant(f"{self.module}:{node.lineno}:{node.col_offset}")
        wrapped = ast.Call(ast.Name("_rwx_parser_lookahead", ast.Load()), [node.func, site, *node.args], node.keywords)
        return ast.copy_location(wrapped, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = Transform(fullname).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in ("sqlglot.parser", "sqlglot.parsers") or fullname.startswith("sqlglot.parsers."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_parser_lookahead = observe
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
    sites = {site: [ix(unknown[site]), [[value, result, ix(mask)] for (value, result), mask in hits.items()]]
             for site, hits in rows.items()}
    data = {"sites": sites, "masks": [hex(mask) for mask in masks]}
    Path(os.environ["MATCH_OUT"]).write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))
