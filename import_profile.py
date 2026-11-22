"""Collect pinned module imports for each test file in an isolated process."""
import importlib
import json
import multiprocessing
import os
import sys
import zlib
from collections import defaultdict
from pathlib import Path


def scan(path):
    try:
        importlib.import_module(path[:-3].replace("/", "."))
    except Exception:
        return path, None
    roots = set()
    for name, module in sys.modules.copy().items():
        if name.startswith(("sqlglot", "tests.")) and (file := getattr(module, "__file__", None)):
            root = str(Path.cwd()) + "/"
            if file.startswith(root):
                roots.add(file.removeprefix(root))
    return path, roots


def main():
    tests = json.loads(Path(os.environ["TEST_LIST"]).read_bytes())["tests"]
    files = defaultdict(int)
    for index, test in enumerate(tests):
        files[test["file"]] |= 1 << index
    imported_by = defaultdict(int)
    unknown = 0
    with multiprocessing.get_context("fork").Pool(3, maxtasksperchild=1) as pool:
        for item, roots in pool.imap_unordered(scan, sorted(files)):
            if roots is None:
                unknown |= files[item]
                continue
            for path in roots:
                imported_by[path] |= files[item]
    result = {"files": {path: hex(mask) for path, mask in imported_by.items()}, "unknown": hex(unknown)}
    Path(os.environ["IMPORT_OUT"]).write_bytes(zlib.compress(json.dumps(result, separators=(",", ":")).encode(), 9))


if __name__ == "__main__":
    main()
