"""Select affected tests from execution evidence for the unmodified revision."""
import ast
import functools
import json
import os
import re
import time
import sys
import zlib
from pathlib import Path
from equivalence import same_comparison_behavior
import behavior

HERE = Path(__file__).resolve().parent
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def load_profile(name):
    location = HERE / (name + ".z")
    if not location.exists():
        return None
    profile = json.loads(zlib.decompress(location.read_bytes()))
    if "masks" in profile:
        profile["masks"] = [int(s, 16) for s in profile["masks"]]
    return profile


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
        self.scope_keys = {}
        self.identical = set()
        self.warm_functions = {}
        self.case_index = None
        self.values = load_profile("values")
        self.booleans = load_profile("bools")
        self.arguments = load_profile("args")
        self.type_profiles = json.loads((HERE / "types.json").read_bytes())
        dictionary_file = HERE / "dicts.z"
        self.dict_keys = (json.loads(zlib.decompress(dictionary_file.read_bytes()))
                          if dictionary_file.exists() else {})

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
        module = path[:-3].removesuffix("/__init__").replace("/", ".")
        edits_ast = behavior.changes(old, new)
        if not edits_ast:
            self.identical.add(path)
            return 0
        covered = set()
        for chain in edits_ast:
            ancestors = [a.lineno for i, (a, b) in enumerate(chain[:-1])
                         if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and isinstance(b, type(a)) and any(chain[i + 1][0] is s for s in a.body)]
            if not ancestors:
                break
            covered.add(tuple(ancestors))
        else:
            self.warm_functions[module] = sorted(covered)
        if self.values:
            reduced = behavior.compare_mask(module, edits_ast, self.values)
            if reduced is not None:
                return reduced
        if self.booleans:
            reduced = behavior.bool_mask(module, edits_ast, self.booleans)
            if reduced is not None:
                return reduced
        if self.arguments:
            reduced = behavior.argument_mask(module, edits_ast, self.arguments)
            if reduced is not None:
                return reduced
        if self.case_index:
            sites = behavior.dispatch_sites(module, edits_ast)
            mask = self.case_index.tests_for(sites, self.ids)
            if mask is not None:
                self.scope_keys[path] = sites
                return mask
        edits = (line.lstrip() for _, chunk in hunks for sign, line in chunk if sign != " ")
        if all(not line or line.startswith("#") for line in edits):
            # A leading # inside a multiline string is data. Compare parsed
            # trees before excluding an edit that looks like a comment.
            if ast.dump(new) == ast.dump(old):
                return 0
        elif any(" is " in line for _, chunk in hunks for sign, line in chunk if sign != " "):
            if same_comparison_behavior(old, new, path[:-3].replace("/", "."), self.type_profiles):
                return 0
        dictionary = self.dictionary_update(path, old, new, affected)
        if dictionary is not None:
            return dictionary
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
                        if field.lineno <= min(affected) <= max(affected) <= field.end_lineno:
                            self.scope_keys[path] = {"attr:" + key}
                        selected |= self.attributes[key]
                        continue
                if path.startswith("sqlglot/expressions/") and enclosing_classes and self.classes:
                    name = scope.name
                    selected |= self.classes.get(name, self.full)
                else:
                    selected |= self.dialects.get(dialect, self.full)
        return selected

    def dictionary_update(self, path, old, new, affected):
        if not self.dict_keys:
            return None
        module = path[:-3].replace("/", ".")
        # Only a replacement of values under existing literal keys can be
        # handled by key-read coverage. Confirm the rest of the syntax tree.
        before_classes = [n for n in ast.walk(old) if isinstance(n, ast.ClassDef)
                          and affected and n.lineno <= min(affected) <= max(affected) <= n.end_lineno]
        if not before_classes:
            return None
        scope = min(before_classes, key=lambda c: c.end_lineno - c.lineno)
        qualified = ".".join(c.name for c in sorted(before_classes, key=lambda c: c.lineno))
        for stmt in scope.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                continue
            if not stmt.lineno <= min(affected) <= max(affected) <= stmt.end_lineno:
                continue
            name = stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
            if not isinstance(name, ast.Name):
                continue
            records = self.dict_keys.get(f"{module}:{qualified}.{name.id}")
            if not records:
                continue
            other = next((n for n in ast.walk(new) if type(n) is type(stmt)
                          and n.lineno == stmt.lineno and isinstance(n.value, ast.Dict)), None)
            if other is None or len(stmt.value.keys) != len(other.value.keys):
                continue
            changed_keys = []
            for k, k2, v, v2 in zip(stmt.value.keys, other.value.keys, stmt.value.values, other.value.values):
                if not isinstance(k, ast.Constant) or not isinstance(k.value, str) or not isinstance(k2, ast.Constant) or k.value != k2.value:
                    return None
                if ast.dump(v) != ast.dump(v2):
                    try:
                        ast.literal_eval(v)
                        ast.literal_eval(v2)
                    except (ValueError, TypeError):
                        return None
                    changed_keys.append(k.value)
            saved = other.value
            other.value = stmt.value
            equal_elsewhere = ast.dump(old) == ast.dump(new)
            other.value = saved
            if equal_elsewhere and changed_keys:
                mask = int(records.get("*", "0"), 16)
                for k in changed_keys:
                    mask |= int(records.get(k, "0"), 16)
                return mask
        return None


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
    tests_by_number = {t["number"]: t for t in tests}
    coverage = Coverage()
    case_index = None
    deadline = time.monotonic() + 85
    if sys.platform == "linux" and (HERE / "cases.z").is_file() and Path("/src/tests").is_dir():
        from cases import CaseIndex
        from probe import refine
        case_index = CaseIndex(HERE / "cases.z")
        coverage.case_index = case_index
    mapping = [by_name.get(name) for name in coverage.ids]
    unknown = set(by_name.values()) - set(mapping)
    for patch in sorted((directory / "commits").glob("*.patch")):
        coverage.scope_keys.clear()
        coverage.identical.clear()
        coverage.warm_functions.clear()
        delta = changes(patch.read_text("utf-8"))
        mask = 0 if delta else coverage.full
        for path, hunks in delta.items():
            mask |= coverage.select(path, hunks)
        chosen = [n for i, n in enumerate(mapping) if n is not None and mask & (1 << i)]
        chosen.extend(sorted(unknown))
        if case_index is not None and os.environ.get("RUNTIME_REPLAY", "1") == "1":
            chosen = refine(delta, coverage, chosen, tests_by_number, durations, case_index, rewrite, deadline)
        print(json.dumps({"commit": patch.stem, "workers": schedule(chosen, durations)}, separators=(",", ":")))
    if case_index is not None:
        import warm
        warm.close()


if __name__ == "__main__":
    main()
