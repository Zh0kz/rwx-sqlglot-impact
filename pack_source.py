import json
import sys
import zlib
from pathlib import Path
from bindings import References

root = Path(sys.argv[1])
sources = {
    p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
    for folder in ("sqlglot", "tests")
    for p in (root / folder).rglob("*.py")
}
Path(sys.argv[2]).write_bytes(zlib.compress(json.dumps(sources).encode(), 9))
if len(sys.argv) > 3:
    Path(sys.argv[3]).write_bytes(References(sources).packed())
