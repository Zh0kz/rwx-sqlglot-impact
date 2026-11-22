"""Trace an unmodified sqlglot checkout under CPython 3.13 and pytest."""
import functools
import json
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
import concurrent.futures
import ast
import builtins
import importlib.abc
import importlib.machinery
import pickle

ROOT = Path.cwd().resolve()
DEST = Path(os.environ["MAP_OUT"])
tests = json.loads(Path(os.environ["TEST_LIST"]).read_text())["tests"]
index = {t["id"]: i for i, t in enumerate(tests)}
lines = defaultdict(dict)
files = defaultdict(int)
languages = defaultdict(int)
classes = defaultdict(int)
attributes = defaultdict(int)
seen = 0
current = 0


@functools.lru_cache(None)
def relative(filename):
    if not filename.startswith(str(ROOT)):
        return ""
    return filename[len(str(ROOT)) + 1:].replace("\\", "/")


def visit(code, line):
    path = relative(code.co_filename)
    if current and path.startswith(("sqlglot/", "tests/")) and code.co_flags & 1:
        locations = lines[path]
        locations[line] = locations.get(line, 0) | current
    return sys.monitoring.DISABLE


def opened(event, args):
    if current and event == "open" and isinstance(args[0], str):
        name = args[0].replace("\\", "/")
        if "tests/fixtures/" in name:
            files["tests/fixtures/" + name.split("tests/fixtures/", 1)[1]] |= current


@functools.cache
def owner_key(cls, attr):
    for owner in cls.__mro__:
        if attr in vars(owner):
            return owner.__module__ + ":" + owner.__qualname__ + "." + attr
    return None


def record_receiver(receiver, attr):
    if current:
        cls = receiver if isinstance(receiver, type) else type(receiver)
        key = owner_key(cls, attr)
        if key:
            attributes[key] |= current
    return receiver


class ObserveReads(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr.isupper():
            wrapper = ast.Call(ast.Name("_rwx_record_receiver", ast.Load()),
                               [node.value, ast.Constant(node.attr)], [])
            node.value = ast.copy_location(wrapper, node.value)
        return node


class SourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ObserveReads().visit(ast.parse(source, self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class ObserveImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("sqlglot.") or fullname == "sqlglot":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = SourceLoader(fullname, spec.origin)
                return spec


def pytest_sessionstart(session):
    builtins._rwx_record_receiver = record_receiver
    sys.meta_path.insert(0, ObserveImports())
    from sqlglot import Dialect
    from sqlglot.expressions.core import Expr
    # These tests spawn workers for fixture queries. A thread keeps their
    # execution inside the traced interpreter and preserves Future semantics.
    class TracedPool(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, mp_context=None, **kwargs):
            super().__init__(max_workers=1, **kwargs)

        def submit(self, fn, /, *args, **kwargs):
            packet = pickle.dumps((fn, args, kwargs))
            def call():
                function, arguments, keywords = pickle.loads(packet)
                return pickle.loads(pickle.dumps(function(*arguments, **keywords)))
            return super().submit(call)

    concurrent.futures.ProcessPoolExecutor = TracedPool
    init_expr = Expr.__init__

    def track_expr(self, **kwargs):
        if current:
            for name in expr_ancestry(type(self)):
                classes[name] |= current
        init_expr(self, **kwargs)

    @functools.cache
    def expr_ancestry(cls):
        return tuple(c.__name__ for c in cls.__mro__ if issubclass(c, Expr))

    Expr.__init__ = track_expr
    original = Dialect.get_or_raise.__func__

    def tracked(cls, dialect):
        result = original(cls, dialect)
        if current:
            for parent in type(result).__mro__:
                languages[parent.__module__.rsplit(".", 1)[-1]] |= current
        return result

    Dialect.get_or_raise = classmethod(tracked)
    sys.addaudithook(opened)
    mon = sys.monitoring
    mon.use_tool_id(4, "rwx coverage")
    mon.register_callback(4, mon.events.LINE, visit)
    mon.set_events(4, mon.events.LINE)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global current, seen
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    seen |= current
    sys.monitoring.restart_events()
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    sys.monitoring.set_events(4, 0)
    masks = {}

    def intern(mask):
        return masks.setdefault(mask, len(masks))

    result = {
        "tests": [t["id"] for t in tests],
        "lines": {p: {n: intern(m) for n, m in rows.items()} for p, rows in lines.items()},
        "files": {p: intern(m) for p, m in files.items()},
        "dialects": {p: intern(m) for p, m in languages.items()},
        "classes": {p: intern(m) for p, m in classes.items()},
        "attributes": {p: intern(m) for p, m in attributes.items()},
        "seen": intern(seen),
    }
    result["masks"] = [hex(m) for m in masks]
    DEST.write_bytes(zlib.compress(json.dumps(result, separators=(",", ":")).encode(), 9))
