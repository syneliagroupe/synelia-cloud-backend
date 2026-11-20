#!/usr/bin/env python3
"""Vérifie qu'un compte SFTP peut lister son dossier (API + connexion réelle).

  API=https://api.cloud.dev01.ovh.smile.ci/v1 \\
  DEMO_WEB_ORG_ID=... PROBE_MDP=... \\
  uv run python scripts/lab/test-sftp-compte.py

Optionnel : HEBERGEMENT_ID, SFTP_HOST, SFTP_PORT (sinon lus depuis GET hébergement).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("API", "https://api.cloud.dev01.ovh.smile.ci/v1").rstrip("/")


def _req(
    method: str,
    path: str,
    *,
    token: str | None = None,
    org_id: str | None = None,
    body: dict | None = None,
):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org_id:
        headers["X-Organisation-Id"] = org_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw else {}


def _connexion() -> tuple[str, str]:
    org = os.environ["DEMO_WEB_ORG_ID"]
    admin = os.environ.get("PROBE_EMAIL", "admin@synelia.cloud")
    mdp = (
        os.environ.get("PROBE_MDP")
        or os.environ.get("SYNELIA_SEED_ADMIN_MOT_DE_PASSE")
        or ""
    )
    if not mdp:
        sys.exit("PROBE_MDP ou SYNELIA_SEED_ADMIN_MOT_DE_PASSE requis")
    _, corps = _req("POST", "/auth/connexion", body={"email": admin, "motDePasse": mdp})
    adm = corps["accessToken"]
    _, emprunt = _req(
        "POST",
        f"/organisations/{org}/emprunt-identite",
        token=adm,
        org_id=org,
        body={"motif": "lab test sftp", "dureeMin": 30, "ecriture": True},
    )
    return emprunt["accessToken"], emprunt.get("organisationActive") or org


def main() -> int:
    token, org = _connexion()
    hid = os.environ.get("HEBERGEMENT_ID")
    if not hid:
        _, page = _req("GET", "/web/hebergements", token=token, org_id=org)
        donnees = page.get("donnees") or []
        en_ligne = [h for h in donnees if h.get("statut") == "en_ligne"]
        if not en_ligne:
            print("aucun hébergement en_ligne", file=sys.stderr)
            return 1
        hid = en_ligne[0]["id"]
    _, h = _req("GET", f"/web/hebergements/{hid}", token=token, org_id=org)
    acces = h.get("acces") or {}
    host = os.environ.get("SFTP_HOST") or acces.get("hoteTransfert")
    port = int(os.environ.get("SFTP_PORT") or acces.get("portSftp") or 0)
    if not host:
        # FIP de gestion SSH (lab)
        host = os.environ["SFTP_SSH_FIP"]
    if not port:
        port = 2222

    user = f"lab-{secrets.token_hex(3)}"
    mdp = secrets.token_urlsafe(12)
    racine = os.environ.get("SFTP_RACINE", "/srv/synelia/www")
    _req(
        "PUT",
        f"/web/hebergements/{hid}/acces",
        token=token,
        org_id=org,
        body={"sftp": True, "ftp": False, "ftps": False},
    )
    _, compte = _req(
        "POST",
        f"/web/hebergements/{hid}/comptes-fichiers",
        token=token,
        org_id=org,
        body={"utilisateur": user, "protocoles": ["sftp"], "racine": racine, "motDePasse": mdp},
    )
    print(f"compte créé {user} → {host}:{port}")
    time.sleep(4)

    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=mdp, timeout=30, allow_agent=False, look_for_keys=False)
    sftp = client.open_sftp()
    names = sftp.listdir(".")
    sftp.close()
    client.close()
    print(f"OK listdir({user}@): {names[:20]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        raise SystemExit(1) from e
