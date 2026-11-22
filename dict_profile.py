"""Collect test coverage for accesses to string-keyed class dictionaries."""
import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import pytest

tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
index = {t["id"]: i for i, t in enumerate(tests)}
current = 0
import_depth = 0
escaped = set()
reads = defaultdict(dict)


class Watched(dict):
    def __init__(self, site, value):
        self.site = site
        super().__init__(value)

    def touch(self, key=None):
        if not current or import_depth:
            escaped.add(self.site)
        elif self.site not in escaped:
            key = key if type(key) is str else "*"
            rows = reads[self.site]
            rows[key] = rows.get(key, 0) | current

    def __getitem__(self, key):
        self.touch(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.touch(key)
        return super().get(key, default)

    def keys(self):
        self.touch()
        return super().keys()

    def items(self):
        self.touch()
        return super().items()

    def values(self):
        self.touch()
        return super().values()

    def copy(self):
        self.touch()
        return super().copy()

    def __iter__(self):
        self.touch()
        return super().__iter__()


class Transformer(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module
        self.scopes = []

    def visit_ClassDef(self, node):
        self.scopes.append(node.name)
        for statement in node.body:
            if isinstance(statement, ast.ClassDef):
                self.visit_ClassDef(statement)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                name = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
                value = statement.value
                if not isinstance(name, ast.Name) or not isinstance(value, ast.Dict) or not value.keys:
                    continue
                if not all(isinstance(k, ast.Constant) and type(k.value) is str for k in value.keys):
                    continue
                key = self.module + ":" + ".".join([*self.scopes, name.id])
                call = ast.Call(ast.Name("_rwx_watched_dict", ast.Load()), [ast.Constant(key), value], [])
                statement.value = ast.copy_location(call, value)
        self.scopes.pop()
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        parsed = Transformer(fullname).visit(ast.parse(self.get_data(self.path), self.path))
        return compile(ast.fix_missing_locations(parsed), self.path, "exec")

    def exec_module(self, module):
        global import_depth
        import_depth += 1
        try:
            super().exec_module(module)
        finally:
            import_depth -= 1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
                return spec


def pytest_sessionstart(session):
    import concurrent.futures
    from cold_map import InlinePool
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._rwx_watched_dict = Watched
    sys.meta_path.insert(0, Finder())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = 1 << index[item.nodeid] if item.nodeid in index else 0
    yield
    current = 0


def pytest_sessionfinish(session, exitstatus):
    result = {p: {k: hex(m) for k, m in rows.items()} for p, rows in reads.items() if p not in escaped}
    Path(os.environ["DICT_OUT"]).write_bytes(zlib.compress(json.dumps(result).encode(), 9))
