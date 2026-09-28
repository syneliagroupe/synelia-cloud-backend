#!/usr/bin/env python3
"""Smoke GET super-admin (/admin/**, organisations) on dev01. Usage:
API=https://api.cloud.dev01.ovh.smile.ci/v1 uv run python scripts/lab/probe_admin_univers.py
"""
# ruff: noqa: S310

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("API", "https://api.cloud.dev01.ovh.smile.ci/v1").rstrip("/")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("PROBE_MDP", "Synelia!2026")
ORG = os.environ.get("PROBE_ORG", "01a0b10b-8f4d-7633-bd9a-75379da461a3")

GETS: list[tuple[str, str]] = [
    ("tableau-de-bord", "/admin/tableau-de-bord"),
    ("audit", "/admin/audit?parPage=5"),
    ("travaux", "/admin/travaux?parPage=5"),
    ("sante", "/admin/sante"),
    ("sites", "/admin/sites"),
    ("backends", "/admin/backends?parPage=20"),
    ("capacite", "/admin/capacite"),
    ("espaces plateforme", "/admin/espaces"),
    ("conformite", "/admin/conformite"),
    ("fenetres patching", "/admin/conformite/fenetres-patching?parPage=5"),
    ("organisations", "/organisations?parPage=20"),
    ("leads", "/admin/leads?parPage=5"),
    ("tickets admin", "/admin/tickets?parPage=5"),
    ("equipe", "/admin/equipe"),
    ("incidents", "/admin/statut/incidents"),
    ("marketplace", "/admin/marketplace/campagnes?parPage=5"),
    ("migration", "/admin/migration/campagnes?parPage=5"),
    ("catalogue offres", "/admin/catalogue/offres?parPage=20"),
    ("catalogue familles", "/admin/catalogue/familles"),
    ("marges", "/admin/facturation/marges"),
    ("impayes", "/admin/facturation/impayes?parPage=5"),
    ("jetons", "/admin/jetons?parPage=5"),
]


def _get(path: str, token: str, org: str | None = ORG) -> tuple[int, dict | str]:
    url = f"{API}{path}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if org:
        headers["X-Organisation-Id"] = org
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _login() -> str:
    data = json.dumps({"email": EMAIL, "motDePasse": MDP}).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    req = urllib.request.Request(f"{API}/auth/connexion", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())["accessToken"]


def main() -> int:
    token = _login()
    fails = 0
    org_id = None
    backend_id = None
    for label, path in GETS:
        code, body = _get(path, token)
        ok = 200 <= code < 300
        if not ok:
            fails += 1
        print(f"{'OK' if ok else 'FAIL'}\t{code}\t{label}\t{path}", flush=True)
        if ok and isinstance(body, dict):
            if path.startswith("/organisations"):
                don = body.get("donnees") or []
                if don:
                    org_id = don[0]["id"]
            if "backends" in path:
                don = body.get("donnees") or []
                if don:
                    backend_id = don[0]["id"]

    extra = 0
    if backend_id:
        extra += 1
        code, _ = _get(f"/admin/backends/{backend_id}", token)
        ok = code == 200
        fails += 0 if ok else 1
        print(
            f"{'OK' if ok else 'FAIL'}\t{code}\tbackend detail\t/admin/backends/{{id}}", flush=True
        )
    if org_id:
        for suffix in ("espaces", "membres", "tickets"):
            extra += 1
            code, _ = _get(f"/admin/organisations/{org_id}/{suffix}?parPage=5", token)
            ok = code == 200
            fails += 0 if ok else 1
            print(
                f"{'OK' if ok else 'FAIL'}\t{code}\torg {suffix}\t/admin/organisations/{{id}}/{suffix}",
                flush=True,
            )

    total = len(GETS) + extra
    print(f"\n{total - fails}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
