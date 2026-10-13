#!/usr/bin/env python3
"""Keeps the project's details in step with steward/pyproject.toml.

  python3 tools/metadata.py sync          copy version, author and links into
                                          CITATION.cff, the package version and the README
  python3 tools/metadata.py check         fail if any of those are out of step
  python3 tools/metadata.py about         print GitHub "About" fields as JSON
  python3 tools/metadata.py credit        print the credit line
  python3 tools/metadata.py notes         print this version's CHANGELOG section

Only standard library; Python 3.11+.
"""
import json
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = tomllib.loads((ROOT / "steward/pyproject.toml").read_text())
PROJ = DATA["project"]
CREDITS = DATA["tool"]["steward"]["credits"]
VERSION = PROJ["version"]
AUTHORS = [a["name"] for a in PROJ["authors"]]
REPO = PROJ["urls"]["Source"]
TITLE = "ZeroClaw + Steward"


def changelog_section(version):
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## {re.escape(version)}(?: \((\d{{4}}-\d{{2}}-\d{{2}})\))?\s*$(.*?)(?=^## |\Z)",
                  text, re.M | re.S)
    if not m:
        sys.exit(f"CHANGELOG.md has no '## {version} (YYYY-MM-DD)' section. Add one before releasing.")
    if not m.group(1):
        sys.exit(f"CHANGELOG.md section for {version} needs a date: '## {version} (YYYY-MM-DD)'.")
    return m.group(1), m.group(2).strip()


def citation():
    date, _ = changelog_section(VERSION)
    lines = [
        "# Made from steward/pyproject.toml by tools/metadata.py; edit that file, not this one.",
        "cff-version: 1.2.0",
        f'message: "If you use this software, please cite it as below. Idea by {CREDITS["idea"]}; code written with {CREDITS["code"]}."',
        "type: software",
        f'title: "{TITLE}"',
        f'abstract: "{PROJ["description"]}"',
        "authors:",
    ]
    for name in AUTHORS:
        given, _, family = name.rpartition(" ")
        lines += [f'  - given-names: "{given}"', f'    family-names: "{family}"'] if given else [f'  - name: "{name}"']
    lines += [
        f'version: "{VERSION}"',
        f"date-released: {date}",
        f'license: {PROJ["license"]["text"]}',
        f"repository-code: {REPO}",
        f'url: {PROJ["urls"]["Homepage"]}',
        "keywords:",
    ] + [f"  - {k}" for k in PROJ["keywords"]]
    return "\n".join(lines) + "\n"


def targets():
    """(path, new text) for every file whose content comes from pyproject.toml."""
    out = [(ROOT / "CITATION.cff", citation())]
    init = ROOT / "steward/steward/__init__.py"
    out.append((init, re.sub(r'__version__ = "[^"]*"', f'__version__ = "{VERSION}"', init.read_text())))
    readme = ROOT / "README.md"
    text = re.sub(r"zeroclaw-steward-\d+\.\d+\.\d+", f"zeroclaw-steward-{VERSION}", readme.read_text())
    credit = f"Idea and direction by {CREDITS['idea']}. Code written with {CREDITS['code']}."
    text = re.sub(r"(<!-- authors -->\n).*?(\n<!-- /authors -->)", rf"\g<1>{credit}\g<2>", text, flags=re.S)
    out.append((readme, text))
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sync":
        for path, new in targets():
            if not path.exists() or path.read_text() != new:
                path.write_text(new)
                print(f"updated {path.relative_to(ROOT)}")
    elif cmd == "check":
        stale = [str(p.relative_to(ROOT)) for p, new in targets() if not p.exists() or p.read_text() != new]
        if stale:
            sys.exit("Out of step with steward/pyproject.toml: " + ", ".join(stale) + ". Run: python3 tools/metadata.py sync")
        print("Project details are in step.")
    elif cmd == "about":
        topics = [k for k in PROJ["keywords"] if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", k)][:20]
        print(json.dumps({"description": PROJ["description"][:350], "homepage": PROJ["urls"]["Homepage"],
                          "topics": topics, "version": VERSION}))
    elif cmd == "credit":
        print(f"Idea and direction by {CREDITS['idea']}. Code written with {CREDITS['code']}.")
    elif cmd == "notes":
        _, body = changelog_section(VERSION)
        print(body)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
