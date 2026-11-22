"""Union independent baseline traces with matching test IDs."""
import json
import sys
import zlib
from pathlib import Path


def combine(output, paths):
    groups = {k: {} for k in ("files", "dialects", "classes", "attributes")}
    lines = {}
    names = None
    seen = 0
    for path in paths:
        data = json.loads(zlib.decompress(Path(path).read_bytes()))
        if names is not None and names != data["tests"]:
            raise ValueError("incompatible test lists")
        names = data["tests"]
        masks = [int(n, 16) for n in data["masks"]]
        seen |= masks[data["seen"]]
        for group, items in groups.items():
            for key, i in data.get(group, {}).items():
                items[key] = items.get(key, 0) | masks[i]
        for file, values in data["lines"].items():
            rows = lines.setdefault(file, {})
            for line, i in values.items():
                rows[line] = rows.get(line, 0) | masks[i]
    table = {}

    def index(mask):
        return table.setdefault(mask, len(table))

    merged = {k: {p: index(m) for p, m in mapping.items()} for k, mapping in groups.items()}
    merged["lines"] = {p: {n: index(m) for n, m in rows.items()} for p, rows in lines.items()}
    merged.update(tests=names, seen=index(seen))
    merged["masks"] = [hex(m) for m in table]
    Path(output).write_bytes(zlib.compress(json.dumps(merged, separators=(",", ":")).encode(), 9))


if __name__ == "__main__":
    combine(sys.argv[1], sys.argv[2:])
