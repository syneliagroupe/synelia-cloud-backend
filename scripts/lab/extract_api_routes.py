#!/usr/bin/env python3
"""List FastAPI routes from module router.py files (rough static extract)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "apps/synelia/synelia/modules"
METHOD = re.compile(r"@router(?:_\w+)?\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)")
PREFIX = re.compile(r"router(?:_\w+)?\s*=\s*APIRouter\([^)]*prefix=[\"']([^\"']+)")

for path in sorted(ROOT.glob("**/router*.py")):
    text = path.read_text(encoding="utf-8")
    prefixes = PREFIX.findall(text) or [""]
    default_prefix = prefixes[0]
    mod = path.parent.name
    for m in METHOD.finditer(text):
        meth, sub = m.group(1).upper(), m.group(2)
        full = f"{default_prefix}{sub}" if sub.startswith("/") or not sub else f"{default_prefix}/{sub}"
        print(f"{mod}\t{meth}\t{full}")
