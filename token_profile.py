"""Record baseline lookups in each tokenizer's effective configuration."""
import concurrent.futures
import functools
import json
import os
import zlib
from collections import defaultdict
from pathlib import Path

import pytest
from cold_map import InlinePool


tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
index = {t["id"]: i for i, t in enumerate(tests)}
current = 0
accesses = defaultdict(dict)
TOKENS = {"KEYWORDS": "keywords", "SINGLE_TOKENS": "single_tokens",
          "NUMERIC_LITERALS": "numeric_literals", "NUMERIC_ESCAPES": "numeric_escapes",
          "UNESCAPED_SEQUENCES": "unescaped_sequences"}


@functools.cache
def configuration_sites(cls, attr):
    # Dialect subclasses often copy a parent dictionary and override a few
    # entries. Attribute readers are recorded against all potential owners.
    return tuple(parent.__module__ + ":" + parent.__qualname__ + "." + attr
                 for parent in cls.__mro__ if attr in vars(parent))


class Map(dict):
    def __init__(self, original, sites):
        super().__init__(original)
        self.rows = [accesses[site] for site in sites]

    def mark(self, key):
        if current and type(key) is str and len(key) < 160:
            for row in self.rows:
                row[key] = row.get(key, 0) | current

    def __getitem__(self, key):
        self.mark(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.mark(key)
        return super().__contains__(key)

    def get(self, key, default=None):
        self.mark(key)
        return super().get(key, default)


def pytest_sessionstart(session):
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from sqlglot.tokens import Tokenizer
    original = Tokenizer._init_core
    def init_core(self):
        core = original(self)
        for name, field in TOKENS.items():
            receiver = type(self.dialect) if name == "UNESCAPED_SEQUENCES" else type(self)
            value = getattr(core, field)
            if isinstance(value, dict):
                setattr(core, field, Map(value, configuration_sites(receiver, name)))
        return core
    Tokenizer._init_core = init_core


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    rows = {site: {key: hex(mask) for key, mask in values.items()} for site, values in accesses.items()}
    Path(os.environ["TOKEN_OUT"]).write_bytes(zlib.compress(json.dumps(rows,separators=(",", ":")).encode(), 9))
