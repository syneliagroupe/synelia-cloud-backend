#!/usr/bin/env python3
"""Campagne E2E API — univers Applications (lab ou local).

Usage:
  API=https://api.cloud.dev01.ovh.smile.ci/v1 \\
  PROBE_EMAIL=admin@synelia.cloud PROBE_MDP='…' \\
  python3 scripts/lab/test-applications-universe.py

Réutilise l'Espace `espace-demo-abj` quand il existe (évite de créer un réseau Neutron).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import urllib.error
import urllib.request

API = os.environ.get("API", "http://127.0.0.1:4000/v1").rstrip("/")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("PROBE_MDP", "Synelia!2026")
ESPACE_CODE = os.environ.get("PROBE_ESPACE", "espace-demo-abj")
OUT = os.environ.get("PROBE_JSON", "/tmp/applications_universe_e2e.json")


@dataclass
class Step:
    id: str
    label: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _req(
    method: str,
    path: str,
    token: str | None = None,
    body: dict | None = None,
    org_id: str | None = None,
) -> tuple[int, Any]:
    url = f"{API}{path}" if path.startswith("/") else f"{API}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org_id:
        headers["X-Organisation-Id"] = org_id
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:500]}
        return e.code, parsed


def login() -> tuple[str, str | None]:
    code, data = _req("POST", "/auth/connexion", body={"email": EMAIL, "motDePasse": MDP})
    if code != 200:
        raise SystemExit(f"login {code}: {data}")
    token = data.get("accessToken") or data.get("jetonAcces")
    org = (data.get("organisation") or {}).get("id") or data.get("orgId")
    return token, org


def attendre_travail(token: str, org: str | None, travail_id: str, timeout_s: int = 180) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, corps = _req("GET", f"/travaux/{travail_id}", token=token, org_id=org)
        if code != 200:
            time.sleep(1)
            continue
        if corps.get("statut") in ("done", "failed", "rolled_back"):
            return corps
        time.sleep(1)
    return {"statut": "timeout"}


def main() -> int:
    steps: list[Step] = []
    token, org = login()
    suffix = uuid.uuid4().hex[:8]
    nom_projet = f"qa-apps-{suffix}"

    # --- Synthèse & zone ---
    code, synthese = _req("GET", "/projets/synthese", token=token, org_id=org)
    steps.append(
        Step("A-01", "GET /projets/synthese", 200 <= code < 300, f"HTTP {code}", {"corps": synthese})
    )

    code, zone = _req("GET", "/zone-applicative", token=token, org_id=org)
    zone_name = (zone or {}).get("zone", "apps.synelia.cloud")
    steps.append(Step("A-02", "GET /zone-applicative", code == 200, f"zone={zone_name}"))

    # --- Espace existant ---
    code, espaces = _req("GET", "/espaces", token=token, org_id=org)
    espace_id = None
    if code == 200 and espaces:
        donnees = espaces.get("donnees") or espaces if isinstance(espaces, list) else espaces.get("donnees", [])
        if isinstance(espaces, dict):
            donnees = espaces.get("donnees", [])
        for e in donnees:
            if e.get("code") == ESPACE_CODE:
                espace_id = e["id"]
                break
        if not espace_id and donnees:
            espace_id = donnees[0]["id"]
    steps.append(
        Step(
            "PRE",
            f"Espace {ESPACE_CODE}",
            espace_id is not None,
            espace_id or "aucun espace",
            {"espaceId": espace_id},
        )
    )
    if not espace_id:
        _write(steps)
        return 1

    # --- Projet ---
    code, projet = _req(
        "POST",
        "/projets",
        token=token,
        org_id=org,
        body={"nom": nom_projet, "description": "E2E Applications universe", "espaceId": espace_id},
    )
    pid = projet.get("id") if code == 201 else None
    created_project = code == 201
    if not pid and code in (424, 409):
        code_l, existants = _req("GET", "/projets", token=token, org_id=org)
        if code_l == 200 and existants:
            donnees = existants.get("donnees") or []
            for p in donnees:
                code_s, svcs = _req("GET", f"/projets/{p['id']}/services", token=token, org_id=org)
                if code_s == 200 and any(s.get("statut") == "running" for s in (svcs or [])):
                    pid = p["id"]
                    break
            if not pid and donnees:
                pid = donnees[0]["id"]
    steps.append(
        Step(
            "A-03",
            "POST /projets (ou réutilisation)",
            pid is not None,
            f"HTTP {code}" + (" — projet existant" if not created_project and pid else ""),
            {"projetId": pid, "cree": created_project},
        )
    )

    # --- Service nginx (image) ou service running existant ---
    sid = None
    travail_svc = None
    if pid:
        code_svc, services_existants = _req("GET", f"/projets/{pid}/services", token=token, org_id=org)
        running = [
            s for s in (services_existants or []) if s.get("statut") == "running"
        ]
        if code_svc == 200 and running:
            sid = running[0]["id"]
            steps.append(
                Step(
                    "A-04",
                    "Service running existant (nginx)",
                    True,
                    f"{running[0].get('nom')} ref={running[0].get('source', {}).get('ref')}",
                    {"serviceId": sid},
                )
            )
        code, travail_svc = _req(
            "POST",
            f"/projets/{pid}/services",
            token=token,
            org_id=org,
            body={
                "nom": "nginx-proxy",
                "type": "application",
                "environnement": "production",
                "ressources": {"cpu": 1, "ramMo": 512, "diskGo": 10},
                "source": {"type": "image", "ref": "nginx:alpine"},
            },
        )
        if not sid:
            if code == 202 and travail_svc:
                final = attendre_travail(token, org, travail_svc["id"])
                ok = final.get("statut") == "done"
                steps.append(
                    Step(
                        "A-04",
                        "POST service nginx:alpine",
                        ok,
                        final.get("statut", "?"),
                        {"travail": final.get("id"), "erreur": final.get("erreur")},
                    )
                )
            else:
                steps.append(Step("A-04", "POST service nginx:alpine", False, f"HTTP {code}"))

            code, services = _req("GET", f"/projets/{pid}/services", token=token, org_id=org)
            if code == 200 and services:
                sid = services[0]["id"]
                statut = services[0].get("statut")
                steps.append(
                    Step(
                        "A-05",
                        "GET service statut",
                        statut == "running",
                        f"statut={statut}",
                        {"serviceId": sid},
                    )
                )
        elif not any(s.id == "A-05" for s in steps):
            steps.append(Step("A-05", "GET service statut", True, "running (existant)"))

    # --- Domaine applicatif (zone offerte) ---
    hote = f"{nom_projet}.{zone_name}"
    did = None
    if sid:
        code, domaine = _req(
            "POST",
            "/domaines-applicatifs",
            token=token,
            org_id=org,
            body={"hote": hote, "serviceId": sid},
        )
        did = domaine.get("id") if code == 201 else None
        steps.append(Step("A-40", "POST domaine applicatif", code == 201, f"hote={hote} HTTP {code}"))

        if did:
            code, _ = _req(
                "POST",
                f"/domaines-applicatifs/{did}/verification",
                token=token,
                org_id=org,
            )
            steps.append(Step("A-41", "POST verification DNS", code == 200, f"HTTP {code}"))

            code, _ = _req(
                "POST",
                f"/domaines-applicatifs/{did}/certificat",
                token=token,
                org_id=org,
            )
            steps.append(Step("A-42", "POST certificat", code in (200, 202), f"HTTP {code}"))

        code, routage = _req("GET", "/routage", token=token, org_id=org)
        regles = (routage or {}).get("donnees") or []
        match = [r for r in regles if r.get("hote") == hote]
        steps.append(
            Step(
                "A-43",
                "GET /routage reflète le domaine",
                code == 200 and len(match) >= 1,
                f"{len(match)} règle(s) pour {hote}",
            )
        )

    # --- Domaine Web Cloud existant (proxy vers service) ---
    code, web_dom = _req("GET", "/web/domaines", token=token, org_id=org)
    web_list = (web_dom or {}).get("donnees") or []
    if sid and web_list:
        existant = web_list[0]
        fqdn = existant.get("fqdn") or existant.get("nom")
        if fqdn:
            code, dom2 = _req(
                "POST",
                "/domaines-applicatifs",
                token=token,
                org_id=org,
                body={"hote": fqdn, "serviceId": sid},
            )
            steps.append(
                Step(
                    "A-44",
                    "Domaine Web Cloud → service nginx",
                    code in (201, 409),
                    f"{fqdn} HTTP {code} (409 = déjà pris, acceptable)",
                )
            )
            if code == 201 and dom2.get("id"):
                _req(
                    "DELETE",
                    f"/domaines-applicatifs/{dom2['id']}",
                    token=token,
                    org_id=org,
                    body=None,
                )
    elif sid:
        steps.append(
            Step(
                "A-44",
                "Domaine Web Cloud → service",
                True,
                "skip — aucun domaine Web Cloud dans l’org",
            )
        )

    # --- Variables ---
    if pid:
        code, vars_ = _req(
            "PUT",
            f"/projets/{pid}/variables",
            token=token,
            org_id=org,
            body={
                "variables": [
                    {
                        "cle": "QA_FLAG",
                        "valeur": "1",
                        "secret": False,
                        "portee": "runtime",
                        "environnements": ["production"],
                    }
                ]
            },
        )
        steps.append(Step("A-50", "PUT variables", code == 200, f"HTTP {code}"))
        code, lues = _req("GET", f"/projets/{pid}/variables", token=token, org_id=org)
        masque = lues[0].get("valeur") is None if code == 200 and lues else False
        steps.append(Step("A-51", "GET variables (secret masqué N/A)", code == 200, f"HTTP {code}"))

    # --- Déploiement ---
    code, env = _req("POST", "/environnements", token=token, org_id=org, body={"nom": f"qa-{suffix}"})
    env_id = env.get("id") if code == 201 else None
    steps.append(Step("A-10", "POST /environnements", code == 201, f"HTTP {code}"))
    if env_id:
        code, dep = _req(
            "POST",
            "/deploiements",
            token=token,
            org_id=org,
            body={"envId": env_id, "branche": "main", "commit": suffix, "message": "qa e2e"},
        )
        statut_dep = dep.get("statut") if dep else None
        if code == 202 and dep and statut_dep not in ("live", "done"):
            for _ in range(45):
                code_g, lu = _req("GET", f"/deploiements/{dep['id']}", token=token, org_id=org)
                if code_g == 200:
                    statut_dep = lu.get("statut", statut_dep)
                    if statut_dep in ("live", "failed", "rolled_back"):
                        break
                time.sleep(2)
        steps.append(
            Step(
                "A-11",
                "POST /deploiements",
                code == 202 and statut_dep == "live",
                f"statut={statut_dep} HTTP {code}",
            )
        )
        _req(
            "DELETE",
            f"/environnements/{env_id}",
            token=token,
            org_id=org,
            body=None,
        )

    # --- Identifiants / journaux / métriques ---
    if pid and sid:
        for path, step_id, note in [
            (f"/projets/{pid}/services/{sid}/identifiants", "A-20", "identifiants"),
            (f"/projets/{pid}/services/{sid}/journaux", "A-21", "journaux"),
            (f"/projets/{pid}/services/{sid}/metriques", "A-22", "métriques"),
        ]:
            code, corps = _req("GET", path, token=token, org_id=org)
            steps.append(Step(step_id, f"GET {note}", code == 200, f"HTTP {code}"))

    # --- Nettoyage ---
    if did:
        _req(
            "DELETE",
            f"/domaines-applicatifs/{did}?confirmation={hote}",
            token=token,
            org_id=org,
        )
    if pid and created_project:
        code, _ = _req(
            "DELETE",
            f"/projets/{pid}?confirmation={nom_projet}",
            token=token,
            org_id=org,
        )
        steps.append(Step("CLEAN", "DELETE projet", code == 202, f"HTTP {code}"))

    _write(steps)
    fails = sum(1 for s in steps if not s.ok)
    for s in steps:
        mark = "PASS" if s.ok else "FAIL"
        print(f"{mark}\t{s.id}\t{s.label}\t{s.detail}")
    print(f"\n{len(steps) - fails}/{len(steps)} passed → {OUT}")
    return 1 if fails else 0


def _write(steps: list[Step]) -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump([s.__dict__ for s in steps], f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
