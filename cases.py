"""Estimate the cost of replaying affected cases using baseline call coverage."""
import ast
import functools
import json
import zlib
from pathlib import Path


class CaseIndex:
    def __init__(self, path):
        data = json.loads(zlib.decompress(Path(path).read_bytes()))
        self.masks = [int(raw, 16) for raw in data["masks"]]
        self.sites = data["sites"]
        self.contexts = data["contexts"]
        self.times = data["times"]
        self.test_contexts = {name: sum(1 << i for i in scopes.values()) for name, scopes in self.contexts.items()}
        self.has_lines = any(site.startswith("line:") for site in self.sites)
        self.line_mask = None

    def tests_for(self, sites, ids):
        if not sites or any(key not in self.sites for key in sites):
            return None
        mask = 0
        for key in sites:
            mask |= self.masks[self.sites[key]]
        return sum(1 << i for i, name in enumerate(ids) if self.test_contexts.get(name, 0) & mask)

    def affected(self, delta, cov, rewrite):
        mask = 0
        fine = 0 if self.has_lines else None
        self.line_mask = None
        for path, hunks in delta.items():
            special = cov.scope_keys.get(path)
            if special and all(key in self.sites for key in special):
                for key in special:
                    mask |= self.masks[self.sites[key]]
                    if fine is not None:
                        fine |= self.masks[self.sites[key]]
                continue
            if path.startswith("tests/fixtures/optimizer/") and path.endswith(".sql"):
                # Changed fixture inputs produce new case fingerprints.
                continue
            if not path.startswith("sqlglot/") or path not in cov.sources:
                return None
            try:
                _, positions = rewrite(cov.sources[path], hunks)
            except ValueError:
                return None
            functions = self.functions(cov, path)
            for line in positions:
                enclosing = [n for n in functions if n.lineno < line <= n.end_lineno]
                if not enclosing:
                    return None
                fn = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
                first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
                index = self.sites.get(f"{path}:{first}")
                if index is None:
                    return None
                mask |= self.masks[index]
                if fine is None:
                    continue
                stmt = min((s for s in ast.walk(fn) if isinstance(s, ast.stmt)
                            and s.lineno <= line <= s.end_lineno),
                           key=lambda n: n.end_lineno - n.lineno, default=fn)
                lines = {line, stmt.lineno}
                condition = getattr(stmt, "test", None)
                if condition and condition.lineno <= line <= condition.end_lineno:
                    lines.update(range(condition.lineno, condition.end_lineno + 1))
                observed = [self.sites[k] for n in lines if (k := f"line:{path}:{n}") in self.sites]
                if not observed:
                    fine |= self.masks[index]
                for ix in observed:
                    fine |= self.masks[ix]
        self.line_mask = fine
        return mask

    @functools.cache
    def functions(self, cov, path):
        return [n for n in ast.walk(cov.syntax(path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def estimate(self, name, mask, recorded_ms):
        if self.line_mask is not None and recorded_ms >= 200:
            mask = self.line_mask
        contexts = self.contexts.get(name)
        if mask is None or not contexts or len(contexts) == 1 or mask & (1 << contexts["outside"]):
            return [], recorded_ms
        skip = [key for key, i in contexts.items() if key != "outside" and not mask & (1 << i)]
        total = sum(self.times[i] for i in contexts.values())
        saved = sum(self.times[contexts[key]] for key in skip)
        estimate = recorded_ms * (1 - saved / max(total, 1))
        return skip, max(1, min(recorded_ms, estimate))
