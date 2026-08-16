"""
Page ka CSS structurally valid hai ya nahi.

Ye do baar chahiye pada: ek edit ne comment ke beech me text daal diya, comment
jaldi band ho gaya, aur bache hue shabd bare CSS ban gaye.  Browser aise me
chup rehta hai — parser garbage se ubarne ke liye AGLA RULE nigal jaata hai.
Dono baar jo rule gaya wo dikhne wala tha (`.sect-box` jaisa), aur symptom
"rang nahi dikh raha" tha, "CSS toota hai" nahi.

`/*` aur `*/` ginna kaafi nahi tha — dono baar count barabar tha.  Isliye ye
comment hata kar dekhta hai ki bacha hua sab kuch sach me rule/declaration
jaisa hai.

    python test_css.py
"""
from __future__ import annotations

import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import frontend_heatmap as fe


def main() -> int:
    html = fe._HTML
    css = html.split("<style>")[1].split("</style>")[0]

    bad = []
    if css.count("/*") != css.count("*/"):
        bad.append(f"comment count mismatch: {css.count('/*')} open, "
                   f"{css.count('*/')} close")
    #  Nesting: a second /* before the first */ means a comment was reopened
    #  inside itself, which is how the stray text got in both times.
    depth, i = 0, 0
    while i < len(css) - 1:
        if css[i:i + 2] == "/*":
            depth += 1
            if depth > 1:
                bad.append(f"nested /* at offset {i}: ...{css[i-60:i+40]!r}")
            i += 2
        elif css[i:i + 2] == "*/":
            depth -= 1
            if depth < 0:
                bad.append(f"stray */ at offset {i}: ...{css[i-80:i+20]!r}")
                depth = 0
            i += 2
        else:
            i += 1

    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    if stripped.count("{") != stripped.count("}"):
        bad.append(f"brace mismatch: {stripped.count('{')} open, "
                   f"{stripped.count('}')} close")

    #  Whatever sits between a '}' and the next '{' is a SELECTOR.  Prose that
    #  escaped a comment lands exactly there, and prose contains characters a
    #  selector never does.  Deliberately narrow — a loose line-by-line grammar
    #  produced nothing but false alarms on wrapped declarations.
    for seg in re.findall(r"\}([^{}]*)\{", stripped):
        s = seg.strip()
        if not s:
            continue
        if re.search(r"[;!?()]|\.\s|,\s\s", s) or len(s) > 160:
            bad.append(f"selector jagah pe prose: {s[:70]!r}")

    #  The rules that actually paint something the user asked for.  If one of
    #  these vanishes the page still loads and simply looks wrong.
    must = [".sect-box{", ".sect-lbl{", ".tile{", ".tile .s{", ".tile .p{",
            ".legend{", ".pill.chk{"]
    for m in must:
        if m not in css:
            bad.append(f"rule missing: {m}")

    if bad:
        print("\n  CSS TOOTA HUA:\n")
        for b in bad[:12]:
            print(f"    ✗ {b}")
        print()
        return 1
    print(f"\n  CSS OK — {len(css.splitlines())} lines, "
          f"{css.count('/*')} comments, {len(must)} zaroori rules maujood\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
