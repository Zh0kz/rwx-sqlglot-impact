"""Select affected tests from execution evidence for the unmodified revision."""
import ast
import functools
import json
import os
import re
import sys
import zlib
from pathlib import Path
from equivalence import same_comparison_behavior

HERE = Path(__file__).resolve().parent
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changes(diff):
    files = {}
    path = None
    active = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            files[path] = []
            active = None
        elif line.startswith("--- ") and active is None:
            old_name = line[4:].split("\t", 1)[0].removeprefix("a/")
            if old_name != "/dev/null":
                path = old_name
        elif line.startswith("+++ ") and active is None:
            new_name = line[4:].split("\t", 1)[0].removeprefix("b/")
            if new_name != "/dev/null":
                path = new_name
            if path:
                files.setdefault(path, [])
        elif match := HUNK.match(line):
            start = int(match[1]) - 1
            if match[2] == "0":
                start += 1
            active = []
            files[path].append((start, active))
        elif active is not None and line.startswith((" ", "+", "-")):
            active.append((line[0], line[1:]))
    return files


def rewrite(source, hunks):
    original = source.splitlines()
    updated = []
    affected = set()
    cursor = 0
    for start, chunk in hunks:
        updated.extend(original[cursor:start])
        cursor = start
        removed = False
        for sign, line in chunk:
            if sign != "+":
                if cursor >= len(original) or original[cursor] != line:
                    raise ValueError("unmatched hunk")
                cursor += 1
            if sign != "-":
                updated.append(line)
            if sign == "-":
                affected.add(cursor)
                removed = True
            elif sign == "+" and not removed:
                affected.update((max(1, cursor), cursor + 1))
            elif sign == " ":
                removed = False
    updated.extend(original[cursor:])
    return "\n".join(updated), affected


class Coverage:
    def __init__(self):
        data = json.loads(zlib.decompress((HERE / "map.z").read_bytes()))
        masks = [int(s, 16) for s in data["masks"]]
        self.lines = {p: {int(n): masks[i] for n, i in rows.items()} for p, rows in data["lines"].items()}
        self.files = {p: masks[i] for p, i in data["files"].items()}
        self.dialects = {p: masks[i] for p, i in data["dialects"].items()}
        self.classes = {p: masks[i] for p, i in data.get("classes", {}).items()}
        self.attributes = {p: masks[i] for p, i in data.get("attributes", {}).items()}
        self.ids = data["tests"]
        self.full = (1 << len(self.ids)) - 1
        self.sources = json.loads(zlib.decompress((HERE / "source.z").read_bytes()))
        self.type_profiles = json.loads((HERE / "types.json").read_bytes())

    @functools.cache
    def syntax(self, path):
        return ast.parse(self.sources[path])

    def select(self, path, hunks):
        if not path.endswith(".py"):
            return self.files.get(path, self.full if path.startswith("tests/fixtures/") else 0)
        if path not in self.sources or not hunks:
            return self.full
        try:
            new_source, affected = rewrite(self.sources[path], hunks)
            old = self.syntax(path)
            new = ast.parse(new_source)
            compile(new, path, "exec")
        except (ValueError, SyntaxError):
            return self.full
        edits = (line.lstrip() for _, chunk in hunks for sign, line in chunk if sign != " ")
        if all(not line or line.startswith("#") for line in edits):
            # A leading # inside a multiline string is data. Compare parsed
            # trees before excluding an edit that looks like a comment.
            if ast.dump(new) == ast.dump(old):
                return 0
        elif any(" is " in line for _, chunk in hunks for sign, line in chunk if sign != " "):
            if same_comparison_behavior(old, new, path[:-3].replace("/", "."), self.type_profiles):
                return 0
        rows = self.lines.get(path, {})
        selected = 0
        for number in affected:
            selected |= rows.get(number, 0)
        # Function bodies are observed by the tracer. Module-level assignments
        # run before test execution and need independent dependency evidence.
        functions = [n for n in ast.walk(old) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        source_lines = self.sources[path].splitlines()
        for number in affected:
            enclosing = [n for n in functions if n.lineno < number <= n.end_lineno]
            if enclosing:
                if number not in rows or source_lines[number - 1].lstrip().startswith(("and ", "or ")):
                    # Continuation lines may share a line event with the start of the statement.
                    spans = [n for n in ast.walk(min(enclosing, key=lambda n: n.end_lineno - n.lineno))
                             if isinstance(n, ast.stmt) and n.lineno <= number <= n.end_lineno]
                    if spans:
                        stmt = min(spans, key=lambda n: n.end_lineno - n.lineno)
                        selected |= rows.get(stmt.lineno, 0)
                        condition = getattr(stmt, "test", None)
                        if condition and condition.lineno <= number <= condition.end_lineno:
                            for line in range(condition.lineno, condition.end_lineno + 1):
                                selected |= rows.get(line, 0)
            elif path.startswith("tests/test") or "/test_" in path:
                selected |= sum(1 << i for i, name in enumerate(self.ids) if name.startswith(path + "::"))
            else:
                dialect = Path(path).stem
                enclosing_classes = [n for n in ast.walk(old) if isinstance(n, ast.ClassDef)
                                     and n.lineno < number <= n.end_lineno]
                scope = min(enclosing_classes, key=lambda c: c.end_lineno - c.lineno, default=None)
                field = next((s for s in scope.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                              and s.lineno <= number <= s.end_lineno), None) if scope else None
                target = field.targets[0] if isinstance(field, ast.Assign) else getattr(field, "target", None)
                if isinstance(target, ast.Name):
                    parents = ".".join(c.name for c in sorted(enclosing_classes, key=lambda c: c.lineno))
                    key = path[:-3].replace("/", ".") + ":" + parents + "." + target.id
                    if key in self.attributes:
                        selected |= self.attributes[key]
                        continue
                if path.startswith("sqlglot/expressions/") and enclosing_classes and self.classes:
                    name = scope.name
                    selected |= self.classes.get(name, self.full)
                else:
                    selected |= self.dialects.get(dialect, self.full)
        return selected


def schedule(numbers, durations):
    if not numbers:
        return []
    ordered = sorted(numbers, key=durations.__getitem__, reverse=True)
    best_cost = float("inf")
    best = []
    for count in range(1, min(8, len(ordered)) + 1):
        loads = [0] * count
        buckets = [[] for _ in loads]
        for number in ordered:
            index = loads.index(min(loads))
            buckets[index].append(number)
            loads[index] += durations[number]
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost = cost
            best = buckets
    return best


def main():
    directory = Path(os.environ.get("DATA_DIR", "/data"))
    tests = json.loads((directory / "tests.json").read_bytes())["tests"]
    durations = {t["number"]: round(1000 * t["seconds"]) for t in tests}
    by_name = {t["id"]: t["number"] for t in tests}
    coverage = Coverage()
    mapping = [by_name.get(name) for name in coverage.ids]
    unknown = set(by_name.values()) - set(mapping)
    for patch in sorted((directory / "commits").glob("*.patch")):
        delta = changes(patch.read_text("utf-8"))
        mask = 0 if delta else coverage.full
        for path, hunks in delta.items():
            mask |= coverage.select(path, hunks)
        chosen = [n for i, n in enumerate(mapping) if n is not None and mask & (1 << i)]
        chosen.extend(sorted(unknown))
        print(json.dumps({"commit": patch.stem, "workers": schedule(chosen, durations)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
