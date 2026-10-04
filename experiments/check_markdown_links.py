"""Check relative links and #anchors in Markdown files (GitHub slug rules).

Usage: python3 experiments/check_markdown_links.py [files...]
Defaults to README.md and all files in "Архитектура мышления/".
"""
import os
import re
import sys
import unicodedata
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def slugify(heading):
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "-_" or cat[0] in "LN" or cat == "Mn":
            out.append(ch)
        elif ch == " ":
            out.append("-")
    return "".join(out)


def anchors(path):
    result, seen, in_code = set(), {}, False
    for line in open(path, encoding="utf-8"):
        if line.startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            slug = slugify(m.group(2))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            result.add(slug if n == 0 else f"{slug}-{n}")
    return result


def links(path):
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    # allow one level of balanced parentheses inside URL
    return re.findall(r"\]\(((?:[^()\s]|\([^()\s]*\))+)\)", text)


def main(files):
    errors = 0
    for f in files:
        base = os.path.dirname(f)
        for link in links(f):
            if re.match(r"^[a-z]+://", link) or link.startswith("mailto:"):
                continue
            target, _, frag = link.partition("#")
            target_path = os.path.normpath(os.path.join(base, unquote(target))) if target else f
            if not os.path.exists(target_path):
                print(f"MISSING FILE  {f}: {link}")
                errors += 1
                continue
            if frag and target_path.endswith(".md"):
                if unquote(frag) not in anchors(target_path):
                    print(f"MISSING ANCHOR {f}: {link}")
                    errors += 1
    print(f"checked {len(files)} files, {errors} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        folder = os.path.join(ROOT, "Архитектура мышления")
        args = [os.path.join(ROOT, "README.md")] + sorted(
            os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".md"))
    sys.exit(main(args))
