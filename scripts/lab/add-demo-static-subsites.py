#!/usr/bin/env python3
"""Deux sous-domaines statiques + compte SFTP partagé (sites/ + www/) sur le demo*.com lab.

  set -a && . .env && set +a
  DEMO_WEB_ORG_ID=... HEBERGEMENT_ID=... DEMO_DOMAIN=demo-28d14e3a.com \\
  uv run python scripts/lab/add-demo-static-subsites.py
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
SUBS = [s.strip() for s in os.environ.get("STATIC_SUBS", "pages,docs").split(",") if s.strip()]
SFTP_USER = os.environ.get("SFTP_USER", "sftp-verify")
SFTP_PWD = os.environ.get("SFTP_PWD", "SftpVerify!2026")
SFTP_HOST = os.environ["SFTP_HOST"]
SFTP_PORT = int(os.environ.get("SFTP_PORT", "2222"))


def _req(method: str, path: str, *, token: str, org_id: str, body: dict | None = None):
    url = f"{API}{path}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Organisation-Id": org_id,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _connexion() -> tuple[str, str]:
    org = os.environ["DEMO_WEB_ORG_ID"]
    admin = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
    mdp = os.environ.get("SYNELIA_SEED_ADMIN_MOT_DE_PASSE") or os.environ.get("PROBE_MDP", "")
    if not mdp:
        sys.exit("SYNELIA_SEED_ADMIN_MOT_DE_PASSE requis")
    a = _req("POST", "/auth/connexion", token="", org_id=org, body={"email": admin, "motDePasse": mdp})
    e = _req(
        "POST",
        f"/organisations/{org}/emprunt-identite",
        token=a["accessToken"],
        org_id=org,
        body={"motif": "lab static subs", "dureeMin": 60, "ecriture": True},
    )
    return e["accessToken"], org


def _attendre(token: str, org: str, tid: str) -> None:
    for _ in range(120):
        t = _req("GET", f"/travaux/{tid}", token=token, org_id=org)
        if t.get("statut") in ("done", "failed", "rolled_back"):
            if t.get("statut") != "done":
                print(json.dumps(t, indent=2), file=sys.stderr)
                sys.exit(1)
            return
        time.sleep(5)
    sys.exit("timeout travail")


def main() -> int:
    token, org = _connexion()
    hid = os.environ.get("HEBERGEMENT_ID")
    if not hid:
        hebs = _req("GET", "/web/hebergements", token=token, org_id=org)
        hid = next(
            h["id"] for h in (hebs.get("donnees") or []) if h.get("domaine") == DOMAIN
        )

    sites = _req("GET", "/web/sites", token=token, org_id=org)
    hosts = {s.get("hote"): s for s in (sites.get("donnees") or [])}

    for sub in SUBS:
        hote = f"{sub}.{DOMAIN}"
        if hote in hosts:
            print("existe", hote, hosts[hote]["id"])
            continue
        travail = _req(
            "POST",
            "/web/sites",
            token=token,
            org_id=org,
            body={"hebergementId": hid, "site": {"hote": hote, "type": "statique", "ssl": True}},
        )
        if "id" not in travail:
            print("site", hote, travail, file=sys.stderr)
            sys.exit(1)
        _attendre(token, org, travail["id"])
        print("créé", hote)

    sites = _req("GET", "/web/sites", token=token, org_id=org)
    by_host = {
        s["hote"]: s
        for s in (sites.get("donnees") or [])
        if s.get("hebergementId") == hid
    }

    comptes = _req("GET", f"/web/hebergements/{hid}/comptes-fichiers", token=token, org_id=org)
    cid = None
    for c in comptes if isinstance(comptes, list) else []:
        if c.get("utilisateur") == SFTP_USER:
            cid = c["id"]
            break
    racine = "/srv/synelia/sites"
    if cid:
        _req(
            "PATCH",
            f"/web/hebergements/{hid}/comptes-fichiers/{cid}",
            token=token,
            org_id=org,
            body={
                "utilisateur": SFTP_USER,
                "protocoles": ["sftp"],
                "racine": racine,
                "motDePasse": SFTP_PWD,
            },
        )
    else:
        _req(
            "POST",
            f"/web/hebergements/{hid}/comptes-fichiers",
            token=token,
            org_id=org,
            body={
                "utilisateur": SFTP_USER,
                "protocoles": ["sftp"],
                "racine": racine,
                "motDePasse": SFTP_PWD,
            },
        )
    print("compte SFTP", SFTP_USER, "racine", racine)
    time.sleep(6)

    import paramiko

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        SFTP_HOST,
        SFTP_PORT,
        SFTP_USER,
        SFTP_PWD,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = c.open_sftp()
    racine = set(sftp.listdir("."))

    index_php = """<?php
header('Content-Type: text/html; charset=utf-8');
$host = $_SERVER['HTTP_HOST'] ?? 'hebergement';
echo '<!doctype html><html><head><title>' . htmlspecialchars($host) . '</title></head><body>';
echo '<h1>' . htmlspecialchars($host) . '</h1>';
echo '<p>Synelia Web Cloud — PHP partage (SFTP multi-sites).</p>';
echo '<p>Serveur : ' . phpversion() . '</p></body></html>';
""".encode()
    php_path = "www/index.php" if "www" in racine else "index.php"
    with sftp.file(php_path, "w") as f:
        f.write(index_php)
    print("mis à jour", php_path)

    if "sites" not in racine:
        print(
            "AVERTISSEMENT: le compte SFTP ne voit pas encore sites/ — redeploy worker "
            "puis PATCH compte racine /srv/synelia/sites",
            file=sys.stderr,
        )
    else:
        for sub in SUBS:
            hote = f"{sub}.{DOMAIN}"
            site = by_host.get(hote)
            if not site:
                continue
            sid = site["id"]
            html = (
                f"<!doctype html><html><head><title>{hote}</title></head><body>"
                f"<h1>{hote}</h1><p>Site statique (sites/{sid}/www).</p>"
                f'<p><a href="https://{DOMAIN}/">Apex</a></p></body></html>\n'
            ).encode()
            path = f"sites/{sid}/www/index.html"
            with sftp.file(path, "w") as f:
                f.write(html)
            print("écrit", path)

        apex = by_host.get(DOMAIN)
        if apex:
            html = (
                f"<!doctype html><html><head><title>{DOMAIN}</title></head><body>"
                f"<h1>{DOMAIN}</h1><p>Apex + pages. + docs.</p></body></html>\n"
            ).encode()
            with sftp.file(f"sites/{apex['id']}/www/index.html", "w") as f:
                f.write(html)
            print("apex index.html mis à jour")

    sftp.close()
    c.close()
    print("OK — SFTP voit sites/ et www/ ; index.php modifié")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        raise SystemExit(1) from e
