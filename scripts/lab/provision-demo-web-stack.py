#!/usr/bin/env python3
"""Lab dev01 : hébergement + Drive (Nextcloud) + site statique + WordPress sur un demo*.com OVH.

Usage (vm-admin, API déjà configurée avec SYNELIA_DOMAINE_DNS_* et registrar OVH) :
  uv run python scripts/lab/prune-vps-zone-project-vms.py --hebergement-test
  API=https://api.cloud.dev01.ovh.smile.ci/v1 uv run python scripts/lab/provision-demo-web-stack.py
  API=... DEMO_DOMAIN=demo-28d14e3a.com uv run python scripts/lab/provision-demo-web-stack.py

Crée ou réutilise un compte org dédié (`DEMO_WEB_EMAIL`), enregistre le domaine (sans rachat OVH),
provisionne le VPS, active Drive sur `drive.<domaine>`, installe un site statique sur le apex et
WordPress sur `blog.<domaine>`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("API", "https://api.cloud.dev01.ovh.smile.ci/v1").rstrip("/")
DOMAIN = os.environ["DEMO_DOMAIN"]
EMAIL = os.environ.get("DEMO_WEB_EMAIL", "demo-web-stack@synelia.cloud")
MDP = os.environ.get("DEMO_WEB_MDP", "DemoWeb!2026")
ORG_NOM = os.environ.get("DEMO_WEB_ORG", "Demo Web Stack")

TITULAIRE = {
    "nom": "Synelia Demo Web",
    "email": "demo@synelia.cloud",
    "telephone": "+22500000000",
    "adresse": "Abidjan",
    "ville": "Abidjan",
    "pays": "CI",
}


def _req(
    method: str,
    path: str,
    *,
    token: str | None = None,
    org_id: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict | list | str]:
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org_id:
        headers["X-Organisation-Id"] = org_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _connexion() -> tuple[str, str]:
    preset_token = os.environ.get("DEMO_WEB_TOKEN")
    preset_org = os.environ.get("DEMO_WEB_ORG_ID")
    if preset_token and preset_org:
        return preset_token, preset_org
    admin_email = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
    admin_mdp = os.environ.get("PROBE_MDP") or os.environ.get("SYNELIA_SEED_ADMIN_MOT_DE_PASSE", "")
    emprunt_org = os.environ.get("DEMO_WEB_ORG_ID")
    if admin_mdp and emprunt_org:
        code, corps = _req(
            "POST",
            "/auth/connexion",
            body={"email": admin_email, "motDePasse": admin_mdp},
        )
        if code == 200 and isinstance(corps, dict) and corps.get("accessToken"):
            adm = corps["accessToken"]
            code, emprunt = _req(
                "POST",
                f"/organisations/{emprunt_org}/emprunt-identite",
                token=adm,
                body={
                    "motif": "lab provision demo web stack",
                    "dureeMin": 180,
                    "ecriture": True,
                },
            )
            if code in (200, 201) and isinstance(emprunt, dict) and emprunt.get("accessToken"):
                return emprunt["accessToken"], emprunt.get("organisationActive") or emprunt_org
    code, corps = _req(
        "POST",
        "/auth/connexion",
        body={"email": EMAIL, "motDePasse": MDP},
    )
    if code == 200 and isinstance(corps, dict) and corps.get("accessToken"):
        org = corps.get("organisationActive") or ""
        return corps["accessToken"], org
    return "", ""


def _inscrire() -> tuple[str, str]:
    code, _ = _req(
        "POST",
        "/auth/inscription",
        body={
            "email": EMAIL,
            "nom": "Demo Web Stack",
            "motDePasse": MDP,
            "accepteConditions": True,
            "organisation": {"nom": ORG_NOM, "pays": "CI"},
        },
    )
    if code != 202:
        print("inscription:", code, _, file=sys.stderr)
        sys.exit(1)
    # Forcer vérif email en lab : même code que synelia_testing si disponible
    try:
        import asyncio

        from synelia_testing import CODE_VERIFICATION_TEST, forcer_code_verification

        asyncio.run(forcer_code_verification(EMAIL))
        code_v = CODE_VERIFICATION_TEST
    except Exception:
        print(
            "Impossible de forcer le code email (DB locale requise). "
            "Utilisez un compte existant (connexion) ou DEMO_WEB_VERIFY_CODE.",
            file=sys.stderr,
        )
        code_v = os.environ.get("DEMO_WEB_VERIFY_CODE", "")
        if not code_v:
            sys.exit(1)
    code, corps = _req(
        "POST",
        "/auth/verification-email",
        body={"email": EMAIL, "code": code_v},
    )
    if code != 200:
        print("verification:", code, corps, file=sys.stderr)
        sys.exit(1)
    return corps["accessToken"], corps.get("organisationActive") or ""


def _attendre_travail(token: str, org_id: str, travail: dict, label: str) -> dict:
    statut = travail.get("statut")
    if statut in ("done", "rolled_back", "failed"):
        if statut != "done":
            print(f"ÉCHEC {label}:", json.dumps(travail, indent=2), file=sys.stderr)
            sys.exit(1)
        return travail
    tid = travail.get("id")
    for _ in range(120):
        time.sleep(5)
        code, corps = _req("GET", f"/travaux/{tid}", token=token, org_id=org_id)
        if code != 200:
            continue
        if corps.get("statut") in ("done", "rolled_back", "failed"):
            if corps.get("statut") != "done":
                print(f"ÉCHEC {label}:", json.dumps(corps, indent=2), file=sys.stderr)
                sys.exit(1)
            return corps
    print(f"timeout travail {label}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    token, org_id = _connexion()
    if not token:
        token, org_id = _inscrire()
    print("org", org_id, "domaine", DOMAIN)

    code, doms = _req("GET", "/web/domaines", token=token, org_id=org_id)
    assert code == 200
    if not any(d.get("nom") == DOMAIN for d in (doms.get("donnees") or [])):
        code, travail = _req(
            "POST",
            "/web/domaines",
            token=token,
            org_id=org_id,
            body={"nom": DOMAIN, "dureeAnnees": 1, "titulaire": TITULAIRE},
        )
        print("domaine", code, travail.get("statut") if isinstance(travail, dict) else travail)
        if code == 202 and isinstance(travail, dict):
            _attendre_travail(token, org_id, travail, "domaine")

    code, hebs = _req("GET", "/web/hebergements", token=token, org_id=org_id)
    hid = None
    for h in (hebs.get("donnees") or [] if isinstance(hebs, dict) else []):
        if h.get("domaine") == DOMAIN:
            hid = h["id"]
            break
    if not hid:
        code, travail = _req(
            "POST",
            "/web/hebergements",
            token=token,
            org_id=org_id,
            body={"palier": "pro", "site": "ABJ", "domaine": DOMAIN},
        )
        print("hebergement", code, travail.get("statut") if isinstance(travail, dict) else travail)
        if code != 202:
            sys.exit(1)
        _attendre_travail(token, org_id, travail, "hebergement")
        code, hebs = _req("GET", "/web/hebergements", token=token, org_id=org_id)
        hid = next(h["id"] for h in hebs["donnees"] if h.get("domaine") == DOMAIN)
    else:
        for _ in range(60):
            code, hebs = _req("GET", "/web/hebergements", token=token, org_id=org_id)
            row = next(
                (h for h in (hebs.get("donnees") or []) if h.get("domaine") == DOMAIN),
                None,
            )
            if row and row.get("statut") == "en_ligne":
                break
            time.sleep(10)
        else:
            print("hébergement pas en_ligne (SSH drive/sites)", file=sys.stderr)
            sys.exit(1)

    code, drives = _req("GET", "/web/drive", token=token, org_id=org_id)
    drive_rows = drives.get("donnees") or [] if isinstance(drives, dict) else []
    drive_row = next((d for d in drive_rows if d.get("domaine") == DOMAIN), None)
    if not drive_row:
        code, travail = _req(
            "POST",
            "/web/drive",
            token=token,
            org_id=org_id,
            body={"domaine": DOMAIN, "palier": "pro", "sieges": 5},
        )
        print("drive", code, travail.get("statut") if isinstance(travail, dict) else travail)
        if code == 202:
            _attendre_travail(token, org_id, travail, "drive")
    elif not drive_row.get("actif"):
        code, travs = _req(
            "GET",
            "/travaux?type=web.drive.activate&statut=rolled_back",
            token=token,
            org_id=org_id,
        )
        tid = None
        for t in (travs.get("donnees") or [] if isinstance(travs, dict) else []):
            if DOMAIN in (t.get("label") or ""):
                tid = t.get("id")
                break
        if tid:
            code, travail = _req("POST", f"/travaux/{tid}/relance", token=token, org_id=org_id)
            print("drive relance", code, travail.get("statut") if isinstance(travail, dict) else travail)
            if code in (200, 202) and isinstance(travail, dict):
                _attendre_travail(token, org_id, travail, "drive")

    code, sites = _req("GET", "/web/sites", token=token, org_id=org_id)
    hosts = {s.get("hote") for s in (sites.get("donnees") or [])}
    apex = DOMAIN
    blog = f"blog.{DOMAIN}"

    if apex not in hosts:
        code, travail = _req(
            "POST",
            "/web/sites",
            token=token,
            org_id=org_id,
            body={
                "hebergementId": hid,
                "site": {"hote": apex, "type": "statique", "ssl": True},
            },
        )
        print("site statique", code, travail.get("statut") if isinstance(travail, dict) else travail)
        if code == 202:
            _attendre_travail(token, org_id, travail, "site-statique")

    if blog not in hosts:
        code, travail = _req(
            "POST",
            "/web/sites",
            token=token,
            org_id=org_id,
            body={
                "hebergementId": hid,
                "site": {
                    "hote": blog,
                    "type": "wordpress",
                    "ssl": True,
                    "phpVersion": "8.3",
                    "creerBase": True,
                },
            },
        )
        print("wordpress", code, travail.get("statut") if isinstance(travail, dict) else travail)
        if code == 202:
            _attendre_travail(token, org_id, travail, "wordpress")

    entree_sh = os.path.join(os.path.dirname(__file__), "..", "dev01", "install-vps-web-entree-domaine.sh")
    if os.path.isfile(entree_sh) and os.environ.get("SKIP_DEV01_APACHE") != "1":
        import subprocess

        env = os.environ.copy()
        env.setdefault("DEMO_DOMAIN", DOMAIN)
        print("dev01 Apache (entrée domaine client)…")
        subprocess.run(["/bin/bash", entree_sh], env=env, check=False)

    print("\n--- Résumé ---")
    print(f"Domaine      : https://{DOMAIN}/")
    print(f"WordPress    : https://{blog}/")
    print(f"Nextcloud    : https://drive.{DOMAIN}/")
    print(f"Hébergement  : {hid}")
    print(f"Portail      : app.cloud.dev01.ovh.smile.ci → Web Cloud")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
