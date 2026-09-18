"""Vérification d'email à l'inscription : 202 sans session, login bloqué tant que
non vérifié, code à 6 chiffres (5 essais, 15 min), renvoi limité, session à la fin."""

from synelia_testing import CODE_VERIFICATION_TEST, forcer_code_verification

CORPS = {
    "email": "nouveau@verif.ci",
    "nom": "Nouveau",
    "motDePasse": "Nouveau!2026",
    "accepteConditions": True,
}


async def test_inscription_exige_verification_email(client):
    r = await client.post("/v1/auth/inscription", json=CORPS)
    assert r.status_code == 202, r.text
    assert r.json()["email"] == CORPS["email"] and r.json()["essaisRestants"] == 5
    assert "accessToken" not in r.json()

    # Tant que l'email n'est pas vérifié, la connexion est refusée franchement.
    r = await client.post(
        "/v1/auth/connexion",
        json={"email": CORPS["email"], "motDePasse": CORPS["motDePasse"]},
    )
    assert r.status_code == 403 and r.json()["erreur"]["code"] == "email_non_verifie"

    # Code faux → 403 ; le compteur d'essais est consommé.
    await forcer_code_verification(CORPS["email"])
    r = await client.post(
        "/v1/auth/verification-email",
        json={"email": CORPS["email"], "code": "000000"},
    )
    assert r.status_code == 403 and r.json()["erreur"]["code"] == "code_invalide"

    # Bon code → session ouverte, puis la connexion passe aussi.
    r = await client.post(
        "/v1/auth/verification-email",
        json={"email": CORPS["email"], "code": CODE_VERIFICATION_TEST},
    )
    assert r.status_code == 200 and r.json()["utilisateur"]["email"] == CORPS["email"]

    r = await client.post(
        "/v1/auth/connexion",
        json={"email": CORPS["email"], "motDePasse": CORPS["motDePasse"]},
    )
    assert r.status_code == 200, r.text

    # Re-vérifier un email déjà vérifié → 409 franc.
    r = await client.post(
        "/v1/auth/verification-email",
        json={"email": CORPS["email"], "code": CODE_VERIFICATION_TEST},
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "email_deja_verifie"


async def test_renvoi_limite(client):
    r = await client.post(
        "/v1/auth/inscription",
        json={**CORPS, "email": "renvoi@verif.ci", "nom": "Renvoi"},
    )
    assert r.status_code == 202, r.text

    # Renvoi immédiat (moins de 60 s après l'émission) → 403 `renvoi_trop_tot`.
    r = await client.post("/v1/auth/verification-email/renvoi", json={"email": "renvoi@verif.ci"})
    assert r.status_code == 403 and r.json()["erreur"]["code"] == "renvoi_trop_tot"


async def test_inscription_sans_conditions_refusee(client):
    r = await client.post(
        "/v1/auth/inscription",
        json={**CORPS, "email": "nocgv@verif.ci", "accepteConditions": False},
    )
    assert r.status_code == 422


async def test_inscription_email_deja_utilise(client):
    r = await client.post("/v1/auth/inscription", json=CORPS)
    assert r.status_code == 202, r.text
    r = await client.post("/v1/auth/inscription", json=CORPS)
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "email_deja_utilise"
