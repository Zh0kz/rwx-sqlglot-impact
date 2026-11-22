"""Collect test coverage for source spans actually evaluated by CPython."""
import concurrent.futures
import functools
import json
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool


root = str(Path.cwd()) + "/"
index = {t["id"]: i for i, t in enumerate(json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"])}
current = 0
hits = defaultdict(dict)


@functools.cache
def locations(code):
    if not code.co_filename.startswith(root + "sqlglot/") or not code.co_flags & 1:
        return None
    return hits[code.co_filename[len(root):]], tuple(code.co_positions())


def visit(code, offset):
    if current and (data := locations(code)):
        spans, positions = data
        start, end, col, end_col = positions[offset // 2]
        if start is not None and col is not None and end_col is not None and start == end:
            key = (start, col, end_col)
            spans[key] = spans.get(key, 0) | current
    return sys.monitoring.DISABLE


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    monitoring = sys.monitoring
    monitoring.use_tool_id(5, "expression spans")
    monitoring.register_callback(5, monitoring.events.INSTRUCTION, visit)
    monitoring.set_events(5, monitoring.events.INSTRUCTION)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    sys.monitoring.restart_events()
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    sys.monitoring.set_events(5, 0)
    masks = {}
    data = {path: [[start, col, end, masks.setdefault(mask, len(masks))]
                   for (start, col, end), mask in sorted(spans.items())]
            for path, spans in hits.items()}
    raw = {"files": data, "masks": [hex(mask) for mask in masks]}
    Path(os.environ["INSTRUCTION_OUT"]).write_bytes(zlib.compress(json.dumps(raw, separators=(",", ":")).encode(), 9))
