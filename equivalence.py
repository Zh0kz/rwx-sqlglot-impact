"""Check whether an identity/equality substitution preserves a Python comparison."""
import ast


def agrees(left, right):
    if "unknown" in (left, right):
        return False
    if "none" in (left, right):
        return True
    if left == right:
        return left in ("bool", "identity") or left.startswith("enum:")
    return False


def same_comparison_behavior(old, new, module, profiles):
    """Allow only positively verified operator changes in otherwise identical ASTs."""
    substitutions = 0
    replacements = []
    for before, after in zip(ast.walk(old), ast.walk(new)):
        if not isinstance(before, ast.Compare) or not isinstance(after, ast.Compare):
            continue
        if len(before.ops) != 1 or len(after.ops) != 1:
            continue
        kinds = {type(before.ops[0]), type(after.ops[0])}
        if kinds not in ({ast.Eq, ast.Is}, {ast.NotEq, ast.IsNot}):
            continue
        key = f"{module}:{before.lineno}:{before.col_offset}"
        pairs = profiles.get(key)
        replacements.append((after, after.ops))
        if pairs and all(agrees(left, right) for left, right in pairs):
            after.ops = before.ops
            substitutions += 1
    try:
        return substitutions > 0 and ast.dump(old) == ast.dump(new)
    finally:
        for after, operators in replacements:
            after.ops = operators
