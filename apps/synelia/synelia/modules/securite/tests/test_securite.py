"""Sécurité & accès : clés d'API, politiques, sessions actives, SSO."""

from datetime import timedelta

from synelia_db.modeles import SessionAuth
from synelia_db.session import fabrique
from synelia_kernel.dates import maintenant

from synelia.securite import lire_acces


async def test_cycle_cle_api(client):
    r = await client.post(
        "/v1/securite/cles-api", json={"nom": "ci-cd", "portee": ["vm.create_delete", "vm.power"]}
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret.startswith(r.json()["cle"]["prefixe"])
    cle = r.json()["cle"]
    assert cle["portee"] == ["vm.create_delete", "vm.power"]
    cid = cle["id"]

    r = await client.get("/v1/securite/cles-api")
    assert r.status_code == 200 and any(c["id"] == cid for c in r.json()["donnees"])

    r = await client.get(f"/v1/securite/cles-api/{cid}")
    assert r.status_code == 200 and r.json()["statut"] == "active"

    r = await client.patch(
        f"/v1/securite/cles-api/{cid}", json={"nom": "ci-cd-v2", "portee": ["vm.create_delete"]}
    )
    assert r.status_code == 200 and r.json()["nom"] == "ci-cd-v2"

    r = await client.post(f"/v1/securite/cles-api/{cid}/rotation", json={})
    assert r.status_code == 200
    assert r.json()["secret"] != secret and r.json()["secret"].startswith(
        r.json()["cle"]["prefixe"]
    )

    r = await client.delete(f"/v1/securite/cles-api/{cid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"

    r = await client.delete(f"/v1/securite/cles-api/{cid}", params={"confirmation": "ci-cd-v2"})
    assert r.status_code == 204

    r = await client.get(f"/v1/securite/cles-api/{cid}")
    assert r.status_code == 200 and r.json()["statut"] == "revoquee"


async def test_cle_api_portee_invalide(client):
    r = await client.post(
        "/v1/securite/cles-api", json={"nom": "bad", "portee": ["audit.nonexistant"]}
    )
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "validation"


async def test_politiques(client):
    r = await client.get("/v1/securite/politiques")
    assert r.status_code == 200
    pol = r.json()
    assert pol["mfa"]["obligatoire"] is False
    assert pol["session"]["dureeMaxMin"] == 720

    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": True, "methodes": ["totp"]},
            "session": {"dureeMaxMin": 60, "inactiviteMin": 30},
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200
    assert r.json()["politiques"]["mfa"]["obligatoire"] is True
    assert r.json()["sessionsInvalidees"] == 1


async def test_sessions(client):
    r = await client.get("/v1/securite/sessions")
    assert r.status_code == 200
    donnees = r.json()["donnees"]
    assert len(donnees) >= 1
    assert any(s["courante"] for s in donnees)
    sid = donnees[0]["id"]

    r = await client.delete(f"/v1/securite/sessions/{sid}")
    assert r.status_code in (204, 409)

    r = await client.delete("/v1/securite/sessions", params={"confirmation": "mauvais"})
    assert r.status_code == 422


async def test_sessions_recente_en_tete_sans_tri_explicite(client):
    """`GET /securite/sessions` sans `tri`/`ordre` (ce que fait `useCollection` côté
    front) doit rester triée par récence décroissante, sinon la session courante — la
    plus récente — disparaît derrière d'anciennes sessions dès que leur nombre dépasse
    une page."""
    infos = lire_acces(client.jeton)
    async with fabrique()() as s:
        for i in range(5):
            s.add(
                SessionAuth(
                    id=f"01test-ancienne-{i:04d}",
                    org_id=infos["org"],
                    utilisateur_id=infos["sub"],
                    famille=f"01test-famille-{i:04d}",
                    rafraichissement_hash=f"hash-ancienne-{i:04d}",
                    cree_le=maintenant() - timedelta(days=30 + i),
                    derniere_activite_le=maintenant() - timedelta(days=30 + i),
                    expire_le=maintenant() + timedelta(days=1),
                )
            )
        await s.commit()

    r = await client.get("/v1/securite/sessions", params={"parPage": 3})
    assert r.status_code == 200
    donnees = r.json()["donnees"]
    assert any(s["courante"] for s in donnees), (
        "la session courante doit rester en tête par défaut, pas enterrée par le tri"
    )


async def test_sso(client):
    r = await client.get("/v1/securite/sso")
    assert r.status_code == 200
    assert r.json()["actif"] is False

    r = await client.post("/v1/securite/sso/test")
    assert r.status_code == 200
    assert r.json()["succes"] is False
    assert r.json()["etapes"]

    r = await client.put(
        "/v1/securite/sso",
        json={
            "actif": True,
            "protocole": "oidc",
            "emetteur": "https://idp.example.com",
            "clientId": "syn-app",
        },
    )
    assert r.status_code == 200
    assert r.json()["actif"] is True
    assert r.json()["secretDefini"] is False

    r = await client.post("/v1/securite/sso/test")
    assert r.status_code == 200 and r.json()["succes"] is True


async def test_mfa_obligatoire_bloque_la_connexion_sans_enrolement(client):
    """`mfa.obligatoire` : bloque réellement la connexion (défi jamais résolu sans TOTP)."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": True, "methodes": ["totp"]},
            "session": {"dureeMaxMin": 720, "inactiviteMin": 60},
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200
    assert r.json()["sessionsInvalidees"] == 0  # durée inchangée : la session courante survit

    r = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    assert r.status_code == 200
    corps = r.json()
    assert corps["mfaRequis"] is True
    assert corps.get("accessToken") is None
    assert corps["defiMfa"]


async def test_politique_duree_session_appliquee(client):
    """`session.dureeMaxMin` doit réellement fixer `expire_le`, pas le défaut global (30j)."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {"dureeMaxMin": -5, "inactiviteMin": 60},
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200

    r = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    assert r.status_code == 200
    jeton = r.json()["accessToken"]

    r = await client.get("/v1/moi", headers={"Authorization": f"Bearer {jeton}"})
    assert r.status_code == 401
    assert r.json()["erreur"]["code"] == "non_authentifie"


async def test_politique_inactivite_appliquee(client):
    """`session.inactiviteMin` doit réellement expirer une session inactive."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {"dureeMaxMin": 720, "inactiviteMin": 30},
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200

    r = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    assert r.status_code == 200
    jeton = r.json()["accessToken"]
    sid = lire_acces(jeton)["sid"]

    async with fabrique()() as s:
        session_auth = await s.get(SessionAuth, sid)
        session_auth.derniere_activite_le = maintenant() - timedelta(minutes=45)
        await s.commit()

    r = await client.get("/v1/moi", headers={"Authorization": f"Bearer {jeton}"})
    assert r.status_code == 401
    assert r.json()["erreur"]["code"] == "non_authentifie"


async def test_politique_session_unique_par_utilisateur(client):
    """`sessionUniqueParUtilisateur` : une nouvelle connexion révoque les précédentes."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {
                "dureeMaxMin": 720,
                "inactiviteMin": 60,
                "sessionUniqueParUtilisateur": True,
            },
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200

    r1 = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    jeton1 = r1.json()["accessToken"]
    r2 = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    jeton2 = r2.json()["accessToken"]

    r = await client.get("/v1/moi", headers={"Authorization": f"Bearer {jeton1}"})
    assert r.status_code == 401  # révoquée par la connexion suivante

    r = await client.get("/v1/moi", headers={"Authorization": f"Bearer {jeton2}"})
    assert r.status_code == 200


async def test_politique_restriction_ip_refuse_hors_plage(client):
    """`restrictionIp` : une requête hors des plages autorisées est réellement refusée."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {"dureeMaxMin": 720, "inactiviteMin": 60},
            "restrictionIp": {
                "actif": True,
                "plages": [{"cidr": "10.0.0.0/8", "portee": "les_deux"}],
            },
        },
    )
    assert r.status_code == 200

    r = await client.get("/v1/moi")
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "ip_non_autorisee"


async def test_politique_restriction_ip_autorise_plage_couvrante(client):
    """`restrictionIp` : une plage couvrant l'IP réelle du client laisse passer la requête."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {"dureeMaxMin": 720, "inactiviteMin": 60},
            "restrictionIp": {
                "actif": True,
                "plages": [{"cidr": "127.0.0.1/32", "portee": "les_deux"}],
            },
        },
    )
    assert r.status_code == 200

    r = await client.get("/v1/moi")
    assert r.status_code == 200


async def test_politique_reauthentification_actions_sensibles(client):
    """`reauthentificationActionsSensibles` : rotation de clé refusée si la session est ancienne."""
    r = await client.put(
        "/v1/securite/politiques",
        json={
            "mfa": {"obligatoire": False, "methodes": ["totp"]},
            "session": {
                "dureeMaxMin": 720,
                "inactiviteMin": 60,
                "reauthentificationActionsSensibles": True,
            },
            "restrictionIp": {"actif": False, "plages": []},
        },
    )
    assert r.status_code == 200

    r = await client.post(
        "/v1/securite/cles-api", json={"nom": "ci-cd", "portee": ["sso.configure"]}
    )
    assert r.status_code == 201
    cid = r.json()["cle"]["id"]

    # session tout juste ouverte (fixture) : dans la fenêtre de réauth, rotation autorisée
    r = await client.post(f"/v1/securite/cles-api/{cid}/rotation", json={})
    assert r.status_code == 200

    sid = lire_acces(client.jeton)["sid"]
    async with fabrique()() as s:
        session_auth = await s.get(SessionAuth, sid)
        session_auth.cree_le = maintenant() - timedelta(minutes=30)
        await s.commit()

    r = await client.post(f"/v1/securite/cles-api/{cid}/rotation", json={})
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "reauthentification_requise"


async def test_cle_api_authentifie_une_requete_reelle(client):
    """Une `CleApi` doit réellement authentifier une requête via `X-Api-Key`, portée respectée."""
    r = await client.post(
        "/v1/securite/cles-api", json={"nom": "script-ci", "portee": ["sso.configure"]}
    )
    assert r.status_code == 201
    secret = r.json()["secret"]

    r = await client.get(
        "/v1/securite/cles-api", headers={"Authorization": "", "X-Api-Key": secret}
    )
    assert r.status_code == 200

    r = await client.get("/v1/audit", headers={"Authorization": "", "X-Api-Key": secret})
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "interdit"
