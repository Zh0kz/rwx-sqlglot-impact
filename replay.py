"""Run a bounded group of ordinary unittest methods on one in-memory patch."""
import contextlib
import builtins
import io
import importlib.abc
import importlib.machinery
import json
import os
import resource
import sys
import unittest
import time

import case_hooks

skipped_cases = set()


def keep_case(key):
    return key not in skipped_cases


class PatchedImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, fullname, path, target=None):
        if fullname not in self.sources:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None:
            return None
        spec.loader = self
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.sources[module.__name__], module.__spec__.origin, "exec")
        exec(code, module.__dict__)


def run(data, report, warm_state=None):
    global skipped_cases
    if warm_state is not None:
        from warm import replace
        if not replace(data, warm_state):
            return False
    resource.setrlimit(resource.RLIMIT_AS, (3000 * 1024 ** 2, 3000 * 1024 ** 2))
    sys.path.insert(0, "/src")
    os.chdir("/src")
    sys.meta_path.insert(0, PatchedImport(data["sources"]))
    if data.get("fixtures"):
        fixtures = {"/src/" + path: text for path, text in data["fixtures"].items()}
        original = builtins.open
        def overlay(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                text = fixtures.get(os.path.abspath(file))
                if text is not None and "r" in mode:
                    return io.BytesIO(text.encode()) if "b" in mode else io.StringIO(text)
            return original(file, mode, *args, **kwargs)
        builtins.open = io.open = overlay
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        import concurrent.futures
        from cold_map import InlinePool
        concurrent.futures.ProcessPoolExecutor = InlinePool
        if warm_state is None:
            case_hooks.install(lambda _: None, lambda _: None, keep_case)
        if any(node.startswith("tests/test_executor.py::") for _, node, _ in data["tests"]):
            case_hooks.install_executor(lambda _: None, lambda _: None, keep_case)
        if any(node.endswith("::test_generator_memory") for _, node, _ in data["tests"]):
            case_hooks.install_memory(lambda _: None, lambda _: None, keep_case)
        for number, node, skip_cases in data["tests"]:
            skipped_cases = set(skip_cases)
            file, cls, method = node.split("::")
            path = file[:-3].replace("/", ".") + "." + cls + "." + method
            suite = unittest.defaultTestLoader.loadTestsFromName(path)
            result = unittest.TestResult()
            result.failfast = True
            suite.run(result)
            if result.testsRun == 1 and result.wasSuccessful() and not result.skipped:
                report(number)


if __name__ == "__main__":
    target = sys.stdout
    run(json.load(sys.stdin), lambda n: print(n, file=target, flush=True))
