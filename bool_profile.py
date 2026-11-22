"""Record operand truth values of boolean conditions in unmodified sqlglot."""
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
index = {t["id"]: i for i, t in enumerate(tests)}
current = 0
rows = defaultdict(dict)
pure_sites = {}


def safe(node):
    if isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, ast.Not) and safe(node.operand)
    if isinstance(node, ast.Attribute):
        return node.attr in ("args", "this", "expression", "expressions", "arg_key", "parent",
                             "is_string", "name", "type", "key", "value") and safe(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(map(safe, node.elts))
    if isinstance(node, ast.Compare):
        return safe(node.left) and all(map(safe, node.comparators))
    if isinstance(node, ast.Call) and not node.keywords and all(map(safe, node.args)):
        if isinstance(node.func, ast.Name):
            return node.func.id in {"len", "bool", "type", "isinstance", "issubclass"}
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            owner = node.func.value
            return isinstance(owner, ast.Attribute) and owner.attr == "args" and safe(owner)
    return False


def record(site, is_and, pure, *terms):
    vals = []
    ended = False
    result = is_and
    for i, term in enumerate(terms):
        if ended and not (pure & (1 << i)):
            vals.append("?")
            continue
        try:
            b = bool(term())
        except Exception:
            if not ended:
                raise
            vals.append("!")
            continue
        vals.append("1" if b else "0")
        if not ended:
            result = b
            ended = b != is_and
    if current:
        pattern = "".join(vals)
        d = rows[site]
        d[pattern] = d.get(pattern, 0) | current
        pure_sites[site] = pure
    return result


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def instrument(self, node):
        if not isinstance(node, ast.BoolOp) or len(node.values) > 5:
            return node
        if any(isinstance(x, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda))
               or isinstance(x, ast.Name) and x.id in {"super", "locals", "globals"}
               for x in ast.walk(node)):
            return node
        site = f"{self.module}:{node.lineno}:{node.col_offset}"
        pure = sum(1 << i for i, v in enumerate(node.values) if safe(v))
        if not pure:
            return node
        args = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
        call = ast.Call(ast.Name("_rwx_bool_record", ast.Load()),
                        [ast.Constant(site), ast.Constant(isinstance(node.op, ast.And)), ast.Constant(pure),
                         *(ast.Lambda(args, n) for n in node.values)], [])
        return ast.copy_location(call, node)

    def visit_If(self, node):
        self.generic_visit(node)
        node.test = self.instrument(node.test)
        return node

    visit_While = visit_If
    visit_IfExp = visit_If


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        parsed = Transform(fullname).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(parsed), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("sqlglot.") or fullname == "sqlglot":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
                return spec


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_bool_record = record
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    masks = {}
    data = {site: [pure_sites[site], {p: masks.setdefault(m, len(masks)) for p, m in pats.items()}]
            for site, pats in rows.items()}
    payload = json.dumps({"sites": data, "masks": [hex(m) for m in masks]}, separators=(",", ":")).encode()
    Path(os.environ["BOOL_OUT"]).write_bytes(zlib.compress(payload, 9))
