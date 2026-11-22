"""Index when an immutable module setting is read by the reference suite."""
import ast
import dis
import json
import types
import zlib
from collections import defaultdict


def is_literal(value):
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return all(map(is_literal, value.elts))
    if isinstance(value, ast.Dict):
        return all(k is not None and is_literal(k) and is_literal(v) for k, v in zip(value.keys, value.values))
    if isinstance(value, ast.UnaryOp):
        return isinstance(value.op, (ast.USub, ast.UAdd)) and isinstance(value.operand, ast.Constant)
    return False


def changed_setting(old, new, edits):
    bindings = set()
    for chain in edits:
        if len(chain) < 3:
            return None
        before, after = chain[1]
        if not (isinstance(before, (ast.Assign, ast.AnnAssign)) and isinstance(after, type(before))):
            return None
        if before not in old.body or after not in new.body or chain[2] != (before.value, after.value):
            return None
        left = before.targets if isinstance(before, ast.Assign) else [before.target]
        right = after.targets if isinstance(after, ast.Assign) else [after.target]
        if len(left) != 1 or len(right) != 1 or not isinstance(left[0], ast.Name) or not isinstance(right[0], ast.Name):
            return None
        if not left[0].id.isupper() or left[0].id != right[0].id or not is_literal(before.value) or not is_literal(after.value):
            return None
        try:
            ast.literal_eval(after.value)
        except (ValueError, TypeError, SyntaxError):
            return None
        bindings.add(left[0].id)
    return bindings


class References:
    def __init__(self, sources):
        self.global_reads = defaultdict(dict)
        self.attr_reads = defaultdict(dict)
        self.imports = defaultdict(set)
        for path, source in sources.items():
            module = module_name(path)
            tree = ast.parse(source, filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    owner = node.module or ""
                    if node.level:
                        parts = module.split(".") if path.endswith("/__init__.py") else module.split(".")[:-1]
                        owner = ".".join([*parts[:len(parts) - node.level + 1], owner]).rstrip(".")
                    for name in node.names:
                        self.imports[owner, name.name].add((path, name.asname or name.name))
            self.walk(path, compile(tree, path, "exec"))

    def packed(self):
        by_global = {p: {n: sorted(lines) for n, lines in rows.items()} for p, rows in self.global_reads.items()}
        by_attr = {n: {p: sorted(lines) for p, lines in rows.items()} for n, rows in self.attr_reads.items()}
        imports = [[origin, name, sorted(rows)] for (origin, name), rows in self.imports.items()]
        return zlib.compress(json.dumps([by_global, by_attr, imports], separators=(",", ":")).encode(), 9)

    @classmethod
    def unpack(cls, data):
        glob, attrs, imports = json.loads(zlib.decompress(data))
        obj = cls.__new__(cls)
        obj.global_reads = glob
        obj.attr_reads = attrs
        obj.imports = {(origin, name): rows for origin, name, rows in imports}
        return obj

    def walk(self, path, code):
        function = bool(code.co_flags & 1)
        for instruction in dis.get_instructions(code):
            op = instruction.opname
            name = instruction.argval
            if not isinstance(name, str) or not name.isupper():
                continue
            line = instruction.positions.lineno if function else 0
            if op in ("LOAD_GLOBAL", "LOAD_NAME"):
                self.global_reads[path].setdefault(name, set()).add(line)
            elif op in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_CONST"):
                self.attr_reads[name].setdefault(path, set()).add(line)
        for constant in code.co_consts:
            if isinstance(constant, types.CodeType):
                self.walk(path, constant)

    def select(self, path, names, coverage):
        affected = 0
        work = [(path, name) for name in names]
        seen = set()
        while work:
            file, name = work.pop()
            if (file, name) in seen:
                continue
            seen.add((file, name))
            origin = module_name(file)
            if self.imports.get((origin, "*")):
                return None
            work.extend(self.imports.get((origin, name), ()))
            readers = {path: set(lines) for path, lines in self.attr_reads.get(name, {}).items()}
            readers.setdefault(file, set()).update(self.global_reads.get(file, {}).get(name, ()))
            for path, lines in readers.items():
                if 0 in lines:
                    return None
                for line in lines:
                    affected |= coverage.lines.get(path, {}).get(line, 0)
        return affected


def module_name(path):
    return path[:-3].removesuffix("/__init__").replace("/", ".")
