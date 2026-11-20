#!/usr/bin/env python3
"""Supprime projets applicatifs de test sur le lab (API publique).

Ne touche pas à l'Espace plateforme `vps-zone` ni aux secrets zone VPS.

Usage:
  API=https://api.cloud.dev01.ovh.smile.ci/v1 PROBE_MDP='…' \\
    uv run python scripts/lab/cleanup-applications-lab.py
  uv run python scripts/lab/cleanup-applications-lab.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import quote

API = os.environ.get("API", "https://api.cloud.dev01.ovh.smile.ci/v1").rstrip("/")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("PROBE_MDP", "Synelia!2026")

# Projets à conserver explicitement (vide = tout supprimer sauf si --keep-running)
KEEP_NOMS: set[str] = set()


def _req(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    url = f"{API}{path}" if path.startswith("/") else f"{API}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}


def login() -> str:
    code, data = _req("POST", "/auth/connexion", "", body={"email": EMAIL, "motDePasse": MDP})
    if code != 200:
        raise SystemExit(f"login {code}: {data}")
    return data.get("accessToken")  # type: ignore[union-attr]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-running", action="store_true", help="Garde le projet qui a un service running")
    args = parser.parse_args()
    token = login()

    code, doms = _req("GET", "/domaines-applicatifs?parPage=100", token)
    if code == 200 and doms:
        for d in (doms.get("donnees") or []):  # type: ignore[union-attr]
            h = d["hote"]
            if args.dry_run:
                print("dry-run delete domaine", h)
            else:
                c, _ = _req(
                    "DELETE",
                    f"/domaines-applicatifs/{d['id']}?confirmation={quote(h)}",
                    token,
                )
                print("delete domaine", h, c)

    code, projets = _req("GET", "/projets?parPage=100", token)
    if code != 200:
        print("list projets", code, projets)
        return 1
    liste = projets.get("donnees") or []  # type: ignore[union-attr]

    keep_id: str | None = None
    if args.keep_running:
        for p in liste:
            c2, svcs = _req("GET", f"/projets/{p['id']}/services", token)
            if c2 == 200 and any(s.get("statut") == "running" for s in (svcs or [])):
                keep_id = p["id"]
                print("keep project", p["nom"], keep_id)
                break

    for p in liste:
        if p["nom"] in KEEP_NOMS or p["id"] == keep_id:
            continue
        pid = p["id"]
        nom = p["nom"]
        c2, svcs = _req("GET", f"/projets/{pid}/services", token)
        for s in (svcs or []):
            sn = s["nom"]
            if args.dry_run:
                print("dry-run delete service", sn, "from", nom)
            else:
                c, travail = _req(
                    "DELETE",
                    f"/projets/{pid}/services/{s['id']}?confirmation={quote(sn)}",
                    token,
                )
                print("delete service", sn, c)
                if c == 202 and isinstance(travail, dict) and travail.get("id"):
                    for _ in range(60):
                        cj, j = _req("GET", f"/travaux/{travail['id']}", token)
                        if cj == 200 and j and j.get("statut") in ("done", "failed"):
                            break
                        time.sleep(2)
        if args.dry_run:
            print("dry-run delete projet", nom)
        else:
            c, _ = _req(
                "DELETE",
                f"/projets/{pid}?confirmation={quote(nom)}",
                token,
            )
            print("delete projet", nom, c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
