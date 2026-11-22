"""Trace which declared argument requirements can affect validation in the base suite."""
import ast
import concurrent.futures
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
origins = defaultdict(set)
missing = defaultdict(dict)


def source_owner(cls):
    return next((c for c in cls.__mro__ if "arg_types" in vars(c)), cls)


def origin(cls):
    return cls.__module__ + ":" + cls.__qualname__ + ".arg_types"


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from sqlglot import exp
    from sqlglot.expressions import core
    # Inspect declared tables, including a class that extends an existing
    # literal with **Parent.arg_types. Inherited tables follow the actual MRO.
    for cls in vars(exp).values():
        if not isinstance(cls, type) or not issubclass(cls, exp.Expr):
            continue
        owner = source_owner(cls)
        for parent in owner.__mro__:
            if "arg_types" in vars(parent):
                origins[cls].add(origin(parent))
    original = core.Expr.error_messages
    def error_messages(self, *args, **kwargs):
        if current:
            for site in origins.get(type(self), (origin(source_owner(type(self))),)):
                fields = missing[site]
                for key in type(self).arg_types:
                    value = self.args.get(key)
                    if value is None or (isinstance(value, list) and not value):
                        fields[key] = fields.get(key, 0) | current
        return original(self, *args, **kwargs)
    core.Expr.error_messages = error_messages


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    # The pinned source uses arg_types values to compute required_args.
    # Key iteration, membership, and len(arg_types) depend only on key names.
    sites = {k for values in origins.values() for k in values}
    rows = {site: {k: hex(v) for k, v in missing[site].items()} for site in sites}
    Path(os.environ["ARG_OUT"]).write_bytes(zlib.compress(json.dumps(rows, separators=(",", ":")).encode(), 9))
