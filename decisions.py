"""Evaluate simple conditions on observations from the reference test suite."""
import ast
import operator


COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
UNARY = {ast.Not: operator.not_, ast.UAdd: operator.pos, ast.USub: operator.neg}
ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mod: operator.mod,
              ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor}


def expression(node, names):
    kind = type(node)
    if kind is ast.Name:
        names.add(node.id)
        return lambda values: values[node.id]
    if kind is ast.Constant and type(node.value) in (type(None), bool, int, float, str):
        return lambda values: node.value
    if kind is ast.Tuple:
        items = [expression(item, names) for item in node.elts]
        return lambda values: tuple(item(values) for item in items)
    if kind is ast.Call and isinstance(node.func, ast.Name) and node.func.id == "len":
        if node.keywords or len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
            raise ValueError("unsupported len expression")
        name = "len:" + node.args[0].id
        names.add(name)
        return lambda values: values[name]
    if kind is ast.UnaryOp and type(node.op) in UNARY:
        op = UNARY[type(node.op)]
        operand = expression(node.operand, names)
        return lambda values: op(operand(values))
    if kind is ast.BoolOp:
        items = [expression(item, names) for item in node.values]
        is_and = isinstance(node.op, ast.And)
        def short_circuit(values):
            for item in items:
                value = item(values)
                if bool(value) != is_and:
                    return value
            return value
        return short_circuit
    if kind is ast.Compare and len(node.ops) == 1 and type(node.ops[0]) in COMPARE:
        op = COMPARE[type(node.ops[0])]
        left, right = node.left, node.comparators[0]
        if isinstance(node.ops[0], (ast.Is, ast.IsNot)) and not any(
            isinstance(v, ast.Constant) and (v.value is None or type(v.value) is bool) for v in (left, right)
        ):
            raise ValueError("unsupported identity comparison")
        a, b = expression(left, names), expression(right, names)
        return lambda values: op(a(values), b(values))
    if kind is ast.BinOp and type(node.op) in ARITHMETIC:
        op = ARITHMETIC[type(node.op)]
        a, b = expression(node.left, names), expression(node.right, names)
        def apply(values):
            left, right = a(values), b(values)
            if type(left) not in (int, bool, float) or type(right) not in (int, bool, float):
                raise ValueError("unsupported arithmetic operands")
            return op(left, right)
        return apply
    raise ValueError("unsupported expression")


def mask(module, edits, profile, coverage):
    sites = {}
    for chain in edits:
        pair = next(((a.test, b.test) for i, (a, b) in enumerate(chain[:-1])
                     if isinstance(a, (ast.If, ast.IfExp, ast.While)) and isinstance(b, type(a))
                     and chain[i + 1] == (a.test, b.test)), None)
        if pair is None:
            return None
        old, new = pair
        location = f"{module}:{old.lineno}:{old.col_offset}"
        if location not in profile["sites"]:
            return None
        names, unknown, rows = profile["sites"][location]
        used = set()
        try:
            before = expression(old, used)
            after = expression(new, used)
        except ValueError:
            return None
        if not used.issubset(names):
            return None
        sites[location] = (old.lineno, names, unknown, rows, before, after)
    affected = 0
    masks = profile["masks"]
    for line, names, unknown, rows, before, after in sites.values():
        affected |= masks[unknown]
        observed_mask = masks[unknown]
        for observed, original, index in rows:
            observed_mask |= masks[index]
            values = {name: tuple(value) if kind == "tuple" else value
                      for name, (kind, value) in zip(names, observed)}
            try:
                changed = bool(before(values)) != original or bool(after(values)) != original
            except (LookupError, TypeError, ValueError, ArithmeticError):
                changed = True
            if changed:
                affected |= masks[index]
        affected |= coverage.get(line, 0) & ~observed_mask
    return affected if edits else None
