"""Relais SMTP réel (`synelia.relais_smtp`) : cache d'identifiants, AUTH, quota, audit.

Aucun réseau réel : `_relayer_vers_amont` (le seul point qui ouvre une socket SMTP) est
toujours simulé ici, conformément à la convention de la suite rapide."""

from __future__ import annotations

from types import SimpleNamespace

from synelia import relais_smtp as rs


async def _activer_relais(client, quota_jour: int | None = None):
    corps = {"domainesAutorises": ["exemple.ci"]}
    if quota_jour is not None:
        corps["quotaJour"] = quota_jour
    r = await client.post("/v1/web/smtp", json=corps)
    assert r.status_code == 202, r.text
    r = await client.post("/v1/web/smtp/identifiants", json={"confirmation": "regenerer"})
    assert r.status_code == 200, r.text
    return r.json()  # {hote, ports, identifiant, motDePasse}


def _login_password(identifiant: str, mot_de_passe: str) -> SimpleNamespace:
    return SimpleNamespace(login=identifiant.encode(), password=mot_de_passe.encode())


async def test_cache_et_authentification(client):
    creds = await _activer_relais(client)

    await rs._rafraichir_cache()
    assert creds["identifiant"] in rs._CACHE
    assert rs._CACHE[creds["identifiant"]]["mot_de_passe"] == creds["motDePasse"]

    ok = rs._authentifier(
        None, None, None, "PLAIN", _login_password(creds["identifiant"], creds["motDePasse"])
    )
    assert ok.success is True
    assert ok.auth_data["identifiant"] == creds["identifiant"]

    mauvais = rs._authentifier(
        None, None, None, "PLAIN", _login_password(creds["identifiant"], "faux-mot-de-passe")
    )
    # `handled` doit être False : sinon aiosmtpd ne renvoie aucun code SMTP et le client
    # reste bloqué en attente d'une réponse qui ne vient jamais (régression réelle observée
    # lors du test de bout en bout).
    assert mauvais.success is False and mauvais.handled is False

    inconnu = rs._authentifier(None, None, None, "PLAIN", _login_password("inconnu@x", "x"))
    assert inconnu.success is False and inconnu.handled is False


async def test_quota_relais_depasse(client):
    creds = await _activer_relais(client, quota_jour=2)
    await rs._rafraichir_cache()
    cred = rs._CACHE[creds["identifiant"]]

    ok1, _ = await rs._verifier_et_incrementer_quota(cred)
    ok2, _ = await rs._verifier_et_incrementer_quota(cred)
    ok3, motif3 = await rs._verifier_et_incrementer_quota(cred)

    assert (ok1, ok2, ok3) == (True, True, False)
    assert motif3 == "quota_depasse"


async def test_quota_cle_depasse_independant_du_relais(client):
    await _activer_relais(client, quota_jour=1000)
    r = await client.post("/v1/web/smtp/cles", json={"nom": "app-test", "quotaJour": 1})
    assert r.status_code == 201, r.text
    cle = r.json()

    await rs._rafraichir_cache()
    cred = rs._CACHE[cle["cle"]["identifiant"]]

    ok1, _ = await rs._verifier_et_incrementer_quota(cred)
    ok2, motif2 = await rs._verifier_et_incrementer_quota(cred)
    assert ok1 is True
    assert ok2 is False and motif2 == "quota_depasse"


async def test_cle_revoquee_refusee(client):
    await _activer_relais(client)
    r = await client.post("/v1/web/smtp/cles", json={"nom": "app-a-revoquer"})
    cle = r.json()["cle"]
    r = await client.delete(
        f"/v1/web/smtp/cles/{cle['id']}", params={"confirmation": cle["nom"]}
    )
    assert r.status_code == 204

    await rs._rafraichir_cache()
    cred = rs._CACHE[cle["identifiant"]]
    ok, motif = await rs._verifier_et_incrementer_quota(cred)
    assert ok is False and motif == "clé révoquée"


async def test_handle_data_relaye_et_journalise(client, monkeypatch):
    creds = await _activer_relais(client)
    await rs._rafraichir_cache()
    cred = rs._CACHE[creds["identifiant"]]

    monkeypatch.setattr(
        rs, "_relayer_vers_amont", lambda *a, **k: ("remis", "250", "relayé (simulé)")
    )

    handler = rs.GestionnaireRelais()
    session = SimpleNamespace(auth_data=cred)
    envelope = SimpleNamespace(
        mail_from="expediteur@exemple.ci",
        rcpt_tos=["destinataire@exemple.ci"],
        original_content=b"Subject: test\r\n\r\nCorps.\r\n",
    )
    reponse = await handler.handle_DATA(None, session, envelope)
    assert reponse.startswith("250")

    r = await client.get("/v1/web/smtp/messages")
    assert r.status_code == 200
    messages = r.json()["donnees"]
    assert len(messages) == 1
    assert messages[0]["statut"] == "remis"
    assert messages[0]["vers"] == "destinataire@exemple.ci"


async def test_handle_data_quota_depasse_rejette(client, monkeypatch):
    creds = await _activer_relais(client, quota_jour=1)
    await rs._rafraichir_cache()
    cred = rs._CACHE[creds["identifiant"]]

    monkeypatch.setattr(
        rs, "_relayer_vers_amont", lambda *a, **k: ("remis", "250", "relayé (simulé)")
    )

    handler = rs.GestionnaireRelais()
    session = SimpleNamespace(auth_data=cred)
    envelope = SimpleNamespace(
        mail_from="expediteur@exemple.ci",
        rcpt_tos=["destinataire@exemple.ci"],
        original_content=b"Subject: test\r\n\r\nCorps.\r\n",
    )
    premiere = await handler.handle_DATA(None, session, envelope)
    assert premiere.startswith("250")
    deuxieme = await handler.handle_DATA(None, session, envelope)
    assert deuxieme.startswith("552")


async def test_handle_data_sans_auth_refuse():
    handler = rs.GestionnaireRelais()
    session = SimpleNamespace(auth_data=None)
    reponse = await handler.handle_DATA(None, session, SimpleNamespace())
    assert reponse.startswith("530")


def test_relayer_vers_amont_gere_amont_indisponible(monkeypatch):
    def _smtp_qui_echoue(*a, **k):
        raise OSError("connexion refusée")

    monkeypatch.setattr(rs.smtplib, "SMTP", _smtp_qui_echoue)
    monkeypatch.setenv("SYNELIA_SMTP_RELAIS_AMONT_HOTE", "hote-inexistant.invalide")
    statut, code, detail = rs._relayer_vers_amont("a@x.ci", ["b@x.ci"], b"contenu")
    assert statut == "differe" and code == "451"
    assert "hote-inexistant.invalide" in detail


def test_contexte_tls_genere_un_certificat_valide(tmp_path, monkeypatch):
    monkeypatch.setattr(rs.tempfile, "gettempdir", lambda: str(tmp_path))
    contexte = rs._contexte_tls()
    assert contexte is not None
    assert (tmp_path / "synelia-relais-smtp" / "cert.pem").exists()
    assert (tmp_path / "synelia-relais-smtp" / "cle.pem").exists()
