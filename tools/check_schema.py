"""Check that arkiv/schema.md documents every attribute the writer sets and every query fragment the
page builds (contest gate: schema.md lists entity types, attributes, expiry and the queries the app runs).

    python tools/check_schema.py      # exit 0 when everything is documented
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def main() -> int:
    schema = read("arkiv/schema.md")
    engine, app, tools = read("writer/engine.py"), read("web/app.js"), read("tools/burn_check.py")
    attrs = set(re.findall(r'ak\.(?:text|u64)\("([a-z][a-z0-9_.-]*)"', engine + tools))
    kinds = set(re.findall(r'ak\.text\("kind", "([a-z]+)"\)', engine + tools))
    fragments = set(re.findall(r"(\w+ (?:=|>=|<) (?:str|u64)\()", app))
    missing = []
    for a in sorted(attrs):
        if "`%s`" % a not in schema:
            missing.append("attribute %s" % a)
    for k in sorted(kinds):
        if "`%s`" % k not in schema:
            missing.append("entity kind %s" % k)
    for f in sorted(fragments):
        if f not in schema:
            missing.append("query fragment %r" % f)
    for m in missing:
        print("not in schema.md:", m)
    print("checked %d attributes, %d kinds, %d query fragments: %s" % (
        len(attrs), len(kinds), len(fragments), "OK" if not missing else "%d missing" % len(missing)))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
