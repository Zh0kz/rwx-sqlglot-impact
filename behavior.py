"""Locate changed expressions and evaluate them on recorded baseline operands."""
import ast
import operator


OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


def changes(left, right):
    """Return the paired ancestors of each structural difference, ignoring locations."""
    found = []
    def visit(a, b, parents):
        if type(a) is not type(b):
            found.append(parents + [(a, b)])
        elif isinstance(a, ast.AST):
            chain = parents + [(a, b)]
            for field in a._fields:
                visit(getattr(a, field, None), getattr(b, field, None), chain)
        elif isinstance(a, list) and len(a) == len(b):
            for x, y in zip(a, b):
                visit(x, y, parents)
        elif a != b:
            found.append(parents)
    visit(left, right, [])
    return found


def literal(node):
    value = ast.literal_eval(node)
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if type(value) is tuple and all(type(x) in (str, int, float, bool, type(None)) for x in value):
        return value
    raise ValueError("unsupported operand")


def compare_mask(module, edits, profile):
    matches = {}
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain) if isinstance(a, ast.Compare)
                     and isinstance(b, ast.Compare)), None)
        if not pair:
            return None
        a, b = pair
        if len(a.ops) != 1 or len(b.ops) != 1 or type(b.ops[0]) not in OPS:
            return None
        if ast.dump(a.left) != ast.dump(b.left):
            return None
        site = f"{module}:{a.lineno}:{a.col_offset}"
        record = profile["sites"].get(site)
        if record is None:
            return None
        try:
            literal(a.comparators[0])
            rhs = literal(b.comparators[0])
        except (ValueError, TypeError, SyntaxError):
            return None
        matches[site] = (OPS[type(b.ops[0])], rhs, record)
    if not matches:
        return None
    affected = 0
    for op, rhs, (unknown, rows) in matches.values():
        affected |= profile["masks"][unknown]
        for left, _, observed, index in rows:
            if isinstance(left, list):
                left = tuple(left)
            try:
                differs = op(left, rhs) != observed
            except (TypeError, ValueError, ArithmeticError):
                differs = True
            if differs:
                affected |= profile["masks"][index]
    return affected


def lookup_mask(module, edits, profile):
    masks = profile["masks"]
    affected = 0
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain) if isinstance(a, ast.Call) and isinstance(b, ast.Call)), None)
        if pair is None:
            return None
        old, new = pair
        if not isinstance(old.func, ast.Attribute) or old.func.attr != "get" or new.keywords:
            return None
        if not old.args or len(old.args) != len(new.args) or ast.dump(old.func) != ast.dump(new.func):
            return None
        if any(ast.dump(a) != ast.dump(b) for a, b in zip(old.args[1:], new.args[1:])):
            return None
        before, after = old.args[0], new.args[0]
        if not (isinstance(before, ast.Constant) and isinstance(after, ast.Constant)
                and type(before.value) is str and type(after.value) is str and before.value != after.value):
            return None
        site = f"{module}:{old.lineno}:{old.col_offset}"
        if site not in profile["sites"]:
            return None
        unknown, rows = profile["sites"][site]
        affected |= masks[unknown]
        for matches, different, default_differs, index in rows:
            if after.value in different or (default_differs and after.value not in matches):
                affected |= masks[index]
    return affected if edits else None


def enum_mask(module, edits, profile):
    masks = profile["masks"]
    affected = 0
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain) if isinstance(a, ast.Compare)
                     and isinstance(b, ast.Compare)), None)
        if pair is None:
            return None
        old, new = pair
        if len(old.ops) != 1 or len(new.ops) != 1 or ast.dump(old.left) != ast.dump(new.left):
            return None
        if type(old.ops[0]) not in (ast.Is, ast.IsNot, ast.Eq, ast.NotEq) or type(new.ops[0]) not in (
            ast.Is, ast.IsNot, ast.Eq, ast.NotEq
        ):
            return None
        before, after = old.comparators[0], new.comparators[0]
        if not isinstance(before, ast.Attribute) or not isinstance(after, ast.Attribute):
            return None
        if not before.attr.isupper() or not after.attr.isupper() or ast.dump(before.value) != ast.dump(after.value):
            return None
        path = f"{module}:{old.lineno}:{old.col_offset}"
        record = profile["sites"].get(path)
        if record is None:
            return None
        other, rows = record
        affected |= masks[other]
        for (left_type, left), (right_type, right), result, index in rows:
            members = profile["enums"][right_type]
            if members.get(before.attr) != right or after.attr not in members:
                return None
            if left_type != right_type and isinstance(new.ops[0], (ast.Eq, ast.NotEq)):
                affected |= masks[index]
                continue
            equal = left_type == right_type and left == members[after.attr]
            actual = not equal if isinstance(new.ops[0], (ast.IsNot, ast.NotEq)) else equal
            if actual != result:
                affected |= masks[index]
    return affected if edits else None


def parser_match_mask(module, edits, profile, tokens):
    seen = {}
    matcher_names = {"_match", "_match_pair", "_match_set", "_match_texts"}
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain) if isinstance(a, ast.Call) and isinstance(b, ast.Call)
                     and isinstance(a.func, ast.Attribute) and a.func.attr in matcher_names), None)
        if pair is None:
            return None
        old, new = pair
        if ast.dump(old.func) != ast.dump(new.func) or len(old.args) != len(new.args):
            return None
        if list(map(ast.dump, old.keywords)) != list(map(ast.dump, new.keywords)):
            return None
        method = old.func.attr
        leading = 2 if method == "_match_pair" else 1
        if len(new.args) < leading or list(map(ast.dump, old.args[leading:])) != list(map(ast.dump, new.args[leading:])):
            return None
        def token(node):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "TokenType":
                return tokens.get(node.attr)
        if method in ("_match", "_match_pair"):
            arguments = [token(arg) for arg in new.args[:leading]]
            if not all(arguments):
                return None
        else:
            collection = new.args[0]
            if not isinstance(collection, (ast.List, ast.Tuple, ast.Set)):
                return None
            if method == "_match_set":
                arguments = [token(arg) for arg in collection.elts]
                if not all(arguments):
                    return None
            else:
                arguments = [arg.value for arg in collection.elts
                             if isinstance(arg, ast.Constant) and type(arg.value) is str]
                if len(arguments) != len(collection.elts):
                    return None
        site = f"{module}:{old.lineno}:{old.col_offset}"
        if site not in profile["sites"]:
            return None
        seen[site] = method, arguments
    mask = 0
    masks = profile["masks"]
    for site, (method, arguments) in seen.items():
        uncertain, rows = profile["sites"][site]
        mask |= masks[uncertain]
        for values, result, index in rows:
            if method in ("_match", "_match_pair"):
                outcome = values == arguments
            elif method == "_match_set":
                outcome = values[0] in arguments
            else:
                outcome = values[0] and values[1] in arguments
            if result != outcome:
                mask |= masks[index]
    return mask if seen else None


def bool_mask(module, edits, profile):
    matches = {}
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain)
                     if isinstance(a, ast.BoolOp) and isinstance(b, ast.BoolOp)), None)
        if pair is None:
            return None
        a, b = pair
        if ast.dump(a) == ast.dump(b):
            return None
        if ast.dump(ast.Tuple(a.values, ast.Load())) != ast.dump(ast.Tuple(b.values, ast.Load())):
            return None
        site = f"{module}:{a.lineno}:{a.col_offset}"
        record = profile["sites"].get(site)
        if record is None:
            return None
        matches[site] = (isinstance(a.op, ast.And), record)
    if not matches:
        return None
    affected = 0
    for was_and, (pure, patterns) in matches.values():
        for pattern, index in patterns.items():
            before = after = None
            extra = False
            for i, value in enumerate(pattern):
                if after is None and before is not None and not pure & (1 << i):
                    extra = True
                if after is not None and before is None and not pure & (1 << i):
                    extra = True
                if value in "?!":
                    if after is None or before is None:
                        extra = True
                    continue
                truth = value == "1"
                if before is None and truth != was_and:
                    before = truth
                if after is None and truth == was_and:
                    after = truth
            before = was_and if before is None else before
            after = not was_and if after is None else after
            if extra or before != after:
                affected |= profile["masks"][index]
    return affected


def order_mask(module, edits, profile):
    result = 0
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain) if isinstance(a, ast.Compare)
                     and isinstance(b, ast.Compare)), None)
        if not pair:
            return None
        a, b = pair
        if len(a.ops) != 1 or len(b.ops) != 1:
            return None
        if {type(a.ops[0]), type(b.ops[0])} not in ({ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}):
            return None
        if ast.dump(a.left) != ast.dump(b.left) or ast.dump(a.comparators[0]) != ast.dump(b.comparators[0]):
            return None
        site = f"{module}:{a.lineno}:{a.col_offset}"
        if site not in profile:
            return None
        result |= int(profile[site], 16)
    return result if edits else None


def argument_mask(module, edits, profile):
    affected = 0
    for chain in edits:
        pair = next(((a, b) for a, b in reversed(chain)
                     if isinstance(a, ast.Dict) and isinstance(b, ast.Dict)), None)
        if not pair:
            return None
        a, b = pair
        classes = [n.name for n, _ in chain if isinstance(n, ast.ClassDef)]
        assignment = next((n for n, _ in reversed(chain) if isinstance(n, (ast.Assign, ast.AnnAssign))), None)
        if not classes or assignment is None or assignment.value is not a:
            return None
        names = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if len(names) != 1 or not isinstance(names[0], ast.Name) or names[0].id != "arg_types":
            return None
        if len(a.values) != len(b.values) or ast.dump(ast.Tuple(a.keys, ast.Load())) != ast.dump(ast.Tuple(b.keys, ast.Load())):
            return None
        fields = profile.get(module + ":" + ".".join(classes) + ".arg_types")
        if fields is None:
            return None
        for key, old, new in zip(a.keys, a.values, b.values):
            if ast.dump(old) == ast.dump(new):
                continue
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and isinstance(old, ast.Constant) and type(old.value) is bool
                    and isinstance(new, ast.Constant) and type(new.value) is bool):
                return None
            affected |= int(fields.get(key.value, "0"), 16)
    return affected if edits else None


def instruction_mask(path, edits, profiles, line_masks):
    file = profiles["files"].get(path)
    if not file:
        return None
    spans = {}
    for line, start, end, index in file:
        spans.setdefault(line, []).append((start, end, profiles["masks"][index]))
    affected = 0
    for chain in edits:
        if not any(isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                   any(chain[i + 1][0] is stmt for stmt in parent.body)
                   for i, (parent, _) in enumerate(chain[:-1])):
            return None
        node = next((a for a, _ in reversed(chain) if isinstance(a, ast.expr)), None)
        if node is None or node.lineno != node.end_lineno:
            return None
        if not isinstance(node, (ast.Constant, ast.Name, ast.Attribute, ast.UnaryOp,
                                 ast.Compare, ast.BinOp, ast.Call, ast.Subscript)):
            return None
        candidates = [(start, end, mask) for start, end, mask in spans.get(node.lineno, ())
                      if node.col_offset <= start < end <= node.end_col_offset]
        exact = [mask for start, end, mask in candidates
                 if start == node.col_offset and end == node.end_col_offset]
        if not exact:
            return None
        witnessed = 0
        for start, end, mask in spans.get(node.lineno, ()):
            if start < end:
                witnessed |= mask
        # Keep coverage recorded by the isolated test runs but not witnessed
        # on this source line during the instruction-profile pass.
        affected |= line_masks.get(node.lineno, 0) & ~witnessed
        for _, _, mask in candidates:
            affected |= mask
    return affected if edits else None


def dictionary_changes(module, edits):
    result = []
    inspected = set()
    for chain in edits:
        location = next((i for i, (a, b) in enumerate(chain[:-1])
                         if isinstance(a, ast.Dict) and isinstance(b, ast.Dict)
                         and any(chain[i + 1][0] is v for v in a.values)), None)
        if location is None:
            return None
        old, new = chain[location]
        if old in inspected:
            continue
        inspected.add(old)
        parent, _ = chain[location - 1]
        if isinstance(parent, ast.Assign):
            targets = parent.targets
        elif isinstance(parent, ast.AnnAssign):
            targets = [parent.target]
        else:
            return None
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or parent.value is not old:
            return None
        if any(isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) for a, _ in chain[:location]):
            return None
        qualified = ".".join([*(a.name for a, _ in chain[:location] if isinstance(a, ast.ClassDef)),
                              targets[0].id])
        if len(old.keys) != len(new.keys):
            return None
        for k, k2, v, v2 in zip(old.keys, new.keys, old.values, new.values):
            if (k is None) != (k2 is None) or k is not None and ast.dump(k) != ast.dump(k2):
                return None
            if ast.dump(v) == ast.dump(v2):
                continue
            result.append((f"{module}:{qualified}", k, v, v2))
    return result


def tokenizer_mask(module, edits, profile, token_names):
    affected = 0
    changes = dictionary_changes(module, edits)
    if not changes:
        return None
    for name, k, v, v2 in changes:
        if name.endswith(".UNESCAPED_SEQUENCES") or name not in profile:
            return None
        if not isinstance(k, ast.Constant) or type(k.value) is not str:
            return None
        try:
            if isinstance(v, ast.Attribute) and isinstance(v2, ast.Attribute):
                if not (isinstance(v.value, ast.Name) and isinstance(v2.value, ast.Name)
                        and v.value.id == v2.value.id == "TokenType" and v2.attr in token_names):
                    return None
            elif type(literal(v)) is not type(literal(v2)):
                return None
        except (ValueError, TypeError, SyntaxError):
            return None
        affected |= int(profile[name].get(k.value, "0"), 16)
    return affected


def registry_mask(module, edits, profile):
    changes = dictionary_changes(module, edits)
    if not changes:
        return None
    mask = 0
    for name, key, old, new in changes:
        if name not in profile or not name.endswith(".TYPE_MAPPING"):
            return None
        if not (isinstance(old, ast.Constant) and type(old.value) is str
                and isinstance(new, ast.Constant) and type(new.value) is str):
            return None
        if isinstance(key, ast.Constant) and type(key.value) is str:
            lookup = "str:" + key.value
        elif isinstance(key, ast.Attribute) and getattr(key.value, "attr", None) == "DType":
            lookup = "dtype:" + key.attr
        else:
            return None
        rows = profile[name]
        mask |= int(rows.get(lookup, "0"), 16) | int(rows.get("*", "0"), 16)
    return mask


def dispatch_sites(module, edits):
    locations = set()
    for chain in edits:
        match = None
        for i, (a, b) in enumerate(chain[:-1]):
            if not isinstance(a, ast.Dict) or not isinstance(b, ast.Dict):
                continue
            old, new = chain[i + 1]
            if not any(old is v for v in a.values):
                continue
            if isinstance(old, ast.Lambda) and isinstance(new, ast.Lambda):
                if ast.dump(old.args) != ast.dump(new.args):
                    continue
            elif isinstance(old, ast.Call) and isinstance(new, ast.Call):
                if ast.dump(old.func) != ast.dump(new.func) or len(old.args) != len(new.args):
                    continue
                if [k.arg for k in old.keywords] != [k.arg for k in new.keywords]:
                    continue
                # Only constant substitutions in an already registered callback factory.
                if not any(isinstance(x, ast.Constant) and isinstance(y, ast.Constant)
                           and type(x.value) is type(y.value) for x, y in chain[i + 2:]):
                    continue
            else:
                continue
            match = f"dispatch:{module}:{old.lineno}:{old.col_offset}"
        if match is None:
            return None
        locations.add(match)
    return locations or None


def defaults(args):
    positional = args.posonlyargs + args.args
    return zip(positional[len(positional) - len(args.defaults):] + args.kwonlyargs,
               args.defaults + args.kw_defaults)


def default_changes(edits):
    found = []
    for chain in edits:
        definition = next(((i, a) for i, (a, b) in enumerate(chain[:-2])
                           if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and isinstance(b, type(a)) and chain[i + 1] == (a.args, b.args)), None)
        if not definition:
            return None
        i, fn = definition
        old, new = chain[i + 2]
        parameter = next((arg.arg for arg, default in defaults(fn.args) if old is default), None)
        if parameter is None:
            return None
        try:
            literal(old)
            literal(new)
        except (ValueError, TypeError, SyntaxError):
            return None
        found.append((fn, parameter))
    return found


def default_mask(module, edits, profile):
    changes = default_changes(edits)
    if not changes:
        return None
    mask = 0
    for fn, parameter in changes:
        site = f"{module}:{fn.lineno}:{parameter}"
        if site not in profile:
            return None
        mask |= int(profile[site], 16)
    return mask


def default_sites(path, edits):
    changes = default_changes(edits)
    if not changes:
        return None
    sites = set()
    for fn, _ in changes:
        first = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
        sites.add(f"{path}:{first}")
    return sites or None
