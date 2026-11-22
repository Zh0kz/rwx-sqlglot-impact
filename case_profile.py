"""Measure baseline function coverage and elapsed time per test case."""
import concurrent.futures
import ast
import builtins
import inspect
import importlib.machinery
import importlib.abc
import functools
import json
import os
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool
import case_hooks

root = str(Path.cwd()) + "/"
node = None
active = -1
outside = -1
serial = {}
contexts = {}
durations = defaultdict(int)
calls = defaultdict(int)
targets = defaultdict(lambda: defaultdict(set))
registered = set()
keepalive = []
hot = {t["id"] for t in json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
       if t["seconds"] >= 0.20}


def line_event(code, line):
    if active >= 0 and node in hot and code.co_filename.startswith(root + "sqlglot/"):
        calls[f"line:{code.co_filename[len(root):]}:{line}"] |= 1 << active
    return sys.monitoring.DISABLE


def register(location, value):
    fn = value.__func__ if inspect.ismethod(value) else value
    if inspect.isfunction(fn):
        cells = tuple(id(c.cell_contents) for c in (fn.__closure__ or ()))
        targets[fn.__code__][cells].add(location)
        registered.add(location)
        keepalive.append(fn)
    return value


@functools.cache
def owner_key(cls, attr):
    for parent in cls.__mro__:
        if attr in vars(parent):
            return parent.__module__ + ":" + parent.__qualname__ + "." + attr


def record_receiver(receiver, attr):
    if active >= 0:
        cls = receiver if isinstance(receiver,type) else type(receiver)
        key = owner_key(cls,attr)
        if key:
            calls["attr:" + key] |= 1 << active
    return receiver


class Transform(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Attribute(self,node):
        self.generic_visit(node)
        if isinstance(node.ctx,ast.Load) and node.attr.isupper():
            wrap = ast.Call(ast.Name("_rwx_context_receiver",ast.Load()),[node.value,ast.Constant(node.attr)],[])
            node.value = ast.copy_location(wrap,node.value)
        return node

    def visit_Dict(self,node):
        self.generic_visit(node)
        for i, value in enumerate(node.values):
            if isinstance(value,(ast.Lambda,ast.Call)):
                loc = f"dispatch:{self.module}:{value.lineno}:{value.col_offset}"
                wrap = ast.Call(ast.Name("_rwx_context_dispatch",ast.Load()),[ast.Constant(loc),value],[])
                node.values[i] = ast.copy_location(wrap,value)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        tree = Transform(fullname).visit(ast.parse(self.get_data(self.path),self.path))
        return compile(ast.fix_missing_locations(tree),self.path,"exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path,target=None):
        if fullname.startswith("sqlglot.") or fullname == "sqlglot":
            spec = importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname,spec.origin)
                return spec


@functools.cache
def source_location(code):
    if code.co_filename.startswith(root + "sqlglot/") and code.co_flags & 1:
        return code.co_filename[len(root):] + ":" + str(code.co_firstlineno)


def visit(code, offset):
    if active >= 0:
        site = source_location(code)
        if site:
            calls[site] |= 1 << active
        if code in targets:
            frame = sys._getframe(1)
            values = frame.f_locals
            sig = tuple(id(values.get(k)) for k in code.co_freevars)
            for location in targets[code].get(sig, ()):
                calls[location] |= 1 << active
            if code.co_freevars:
                return None
    return sys.monitoring.DISABLE


def enter(key):
    global active
    if node is None or active != outside:
        return None
    keymap = contexts.setdefault(node, {})
    if key not in keymap:
        keymap[key] = len(serial)
        serial[len(serial)] = None
    prev = active
    active = keymap[key]
    sys.monitoring.restart_events()
    return prev, time.perf_counter_ns()


def leave(token):
    global active
    if token is not None:
        prev, start = token
        durations[active] += time.perf_counter_ns() - start
        active = prev
        sys.monitoring.restart_events()


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_context_receiver = record_receiver
    builtins._rwx_context_dispatch = register
    sys.meta_path.insert(0,Finder())
    case_hooks.install(enter, leave, lambda key: True)
    case_hooks.install_executor(enter, leave, lambda key: True)
    case_hooks.install_memory(enter, leave, lambda key: True)
    mon = sys.monitoring
    mon.use_tool_id(4, "case coverage")
    mon.register_callback(4, mon.events.PY_START, visit)
    mon.register_callback(4, mon.events.LINE, line_event)
    mon.set_events(4, mon.events.PY_START | mon.events.LINE)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global node, active, outside
    node = item.nodeid
    outside = active = len(serial)
    serial[active] = None
    contexts[node] = {"outside": outside}
    sys.monitoring.restart_events()
    start = time.perf_counter_ns()
    yield
    total = time.perf_counter_ns() - start
    durations[outside] = max(0, total - sum(durations[i] for k, i in contexts[node].items() if k != "outside"))
    node, active, outside = None, -1, -1


def pytest_sessionfinish(session, exitstatus):
    sys.monitoring.set_events(4, 0)
    masks = {}
    for key in registered:
        calls.setdefault(key,0)
    loc = {s: masks.setdefault(m, len(masks)) for s, m in calls.items()}
    raw = {"sites":loc, "masks":[hex(m) for m in masks], "contexts":contexts,
           "times":[durations[i] // 1000 for i in range(len(serial))]}
    Path(os.environ["CASE_OUT"]).write_bytes(zlib.compress(json.dumps(raw, separators=(",", ":")).encode(), 9))
