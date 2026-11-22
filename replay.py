"""Run a bounded group of ordinary unittest methods on one in-memory patch."""
import contextlib
import importlib.abc
import importlib.machinery
import json
import os
import resource
import sys
import unittest


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


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (3, 4))
    resource.setrlimit(resource.RLIMIT_AS, (1500 * 1024 ** 2, 1500 * 1024 ** 2))
    data = json.load(sys.stdin)
    sys.path.insert(0, "/src")
    sys.meta_path.insert(0, PatchedImport(data["sources"]))
    keep = []
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for number, node in data["tests"]:
            file, cls, method = node.split("::")
            path = file[:-3].replace("/", ".") + "." + cls + "." + method
            suite = unittest.defaultTestLoader.loadTestsFromName(path)
            result = unittest.TestResult()
            suite.run(result)
            if result.testsRun != 1 or not result.wasSuccessful() or result.skipped:
                keep.append(number)
    print(json.dumps(keep))


if __name__ == "__main__":
    main()
