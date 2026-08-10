#!/usr/bin/env python3
"""
add_dynamic_routes.py - append dynamic-route URLs to routes.txt

Your router declares patterns, not URLs:
    /help/article/:slug        (~31 help articles)
    /help/category/:categoryId
    /guides/:guideId
    /blog/:slug

Only your data knows the actual values. This script pulls them out and appends
real URLs to routes.txt, deduping against what is already there.

THREE INPUT MODES
-----------------
1. From a JS/JSON data file (most common):

     python add_dynamic_routes.py --from-file ../../frontend/src/data/helpArticles.js \
            --field slug --prefix /help/article --priority 0.7

2. From a plain list you paste into a text file (one slug per line):

     python add_dynamic_routes.py --from-list slugs.txt \
            --prefix /help/article --priority 0.7

3. From a live API endpoint returning JSON:

     python add_dynamic_routes.py --from-url https://api.pixelperfectapi.net/help/articles \
            --field slug --prefix /help/article --priority 0.7

Always run with --dry-run first to see what it found.
Then rebuild:  python generate_sitemap.py
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROUTES_FILE = Path("routes.txt")


def from_file(path: Path, field: str) -> list[str]:
    """Extract  field: "value"  or  "field": "value"  from JS/JSON source."""
    if not path.is_file():
        sys.exit(f"ERROR: file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="ignore")

    # Try strict JSON first - cleanest result when it works.
    try:
        data = json.loads(text)
        return collect(data, field)
    except json.JSONDecodeError:
        pass

    # Fall back to regex for JS object literals.
    pat = re.compile(rf'["\']?{re.escape(field)}["\']?\s*:\s*["\']([^"\']+)["\']')
    return list(dict.fromkeys(pat.findall(text)))


def from_url(url: str, field: str) -> list[str]:
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return collect(data, field)


def collect(node, field: str, out=None) -> list[str]:
    """Walk arbitrarily nested JSON collecting every value of `field`."""
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == field and isinstance(v, str):
                out.append(v)
            else:
                collect(v, field, out)
    elif isinstance(node, list):
        for item in node:
            collect(item, field, out)
    return list(dict.fromkeys(out))


def from_list(path: Path) -> list[str]:
    if not path.is_file():
        sys.exit(f"ERROR: file not found: {path}")
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def existing_paths() -> set[str]:
    if not ROUTES_FILE.exists():
        return set()
    out = set()
    for raw in ROUTES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line.split()[0])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-file", help="JS or JSON file containing the data")
    src.add_argument("--from-list", help="text file, one slug per line")
    src.add_argument("--from-url", help="API endpoint returning JSON")
    ap.add_argument("--field", default="slug",
                    help="key holding the slug (default: slug)")
    ap.add_argument("--prefix", required=True,
                    help="URL prefix, e.g. /help/article")
    ap.add_argument("--priority", default="0.7")
    ap.add_argument("--changefreq", default="monthly")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be added, change nothing")
    args = ap.parse_args()

    if args.from_file:
        slugs = from_file(Path(args.from_file), args.field)
    elif args.from_url:
        slugs = from_url(args.from_url, args.field)
    else:
        slugs = from_list(Path(args.from_list))

    if not slugs:
        sys.exit("ERROR: no values found. Check --field, or open the file and "
                 "confirm what the slug key is actually called.")

    prefix = "/" + args.prefix.strip("/")
    have = existing_paths()

    new, dupes, bad = [], [], []
    for s in slugs:
        s = s.strip().strip("/")
        # A slug with a slash or space is almost certainly not a slug.
        if not s or "/" in s or " " in s:
            bad.append(s)
            continue
        path = f"{prefix}/{s}"
        (dupes if path in have else new).append(path)

    print(f"  found {len(slugs)} value(s) for field '{args.field}'")
    if bad:
        print(f"  skipped {len(bad)} that don't look like slugs: {bad[:5]}")
    if dupes:
        print(f"  skipped {len(dupes)} already in routes.txt")
    print(f"  {'would add' if args.dry_run else 'adding'} {len(new)} URL(s)")
    for p in new[:10]:
        print(f"      {p}")
    if len(new) > 10:
        print(f"      ... and {len(new) - 10} more")

    if args.dry_run or not new:
        return

    block = [f"\n# --- {prefix}/* (added by add_dynamic_routes.py) ---"]
    block += [f"{p}  changefreq={args.changefreq} priority={args.priority}"
              for p in new]
    with ROUTES_FILE.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")

    print(f"\n  appended to {ROUTES_FILE}")
    print("  NEXT: python generate_sitemap.py")


if __name__ == "__main__":
    main()