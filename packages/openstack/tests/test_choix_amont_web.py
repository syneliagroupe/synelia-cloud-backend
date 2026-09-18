"""Choix d'amont de l'univers web : le « réel » seulement quand son backend est
configuré ET joignable — partout ailleurs le simulé, explicitement (jamais de
faux « réel »).

- ACME / relais SMTP / Zimbra / registrar : URL-gatés (`SYNELIA_*_URL/HOTE`) ;
- SSH Drive/hébergement : `SYNELIA_FOURNISSEUR=openstack` (lab), prouvé ailleurs.
Ces tests sont offline (aucune connexion établie : les constructeurs `*Reel` ne
font que lire l'environnement) et tournent donc partout, CI incluse."""

from __future__ import annotations


def test_acme_simule_sans_url(monkeypatch):
    from synelia_openstack import acme

    monkeypatch.delenv(acme.ENV_URL, raising=False)
    assert isinstance(acme.choisir_acme(), acme.AcmeSimule)
    assert not isinstance(acme.choisir_acme(), acme.AcmeReel)
    monkeypatch.setenv(acme.ENV_URL, "https://acme.example.ci")
    assert isinstance(acme.choisir_acme(), acme.AcmeReel)


def test_relais_smtp_simule_sans_hote(monkeypatch):
    from synelia_openstack import relais_smtp

    monkeypatch.delenv(relais_smtp.ENV_HOTE, raising=False)
    assert isinstance(relais_smtp.choisir_relais_smtp(), relais_smtp.RelaisSmtpSimule)
    assert not isinstance(relais_smtp.choisir_relais_smtp(), relais_smtp.RelaisSmtpReel)
    monkeypatch.setenv(relais_smtp.ENV_HOTE, "relais-smtp")
    assert isinstance(relais_smtp.choisir_relais_smtp(), relais_smtp.RelaisSmtpReel)


def test_zimbra_simule_sans_url(monkeypatch):
    from synelia_openstack import zimbra

    monkeypatch.delenv(zimbra.ENV_URL, raising=False)
    assert isinstance(zimbra.choisir_zimbra(), zimbra.ZimbraSimule)
    assert not isinstance(zimbra.choisir_zimbra(), zimbra.ZimbraReel)
    monkeypatch.setenv(zimbra.ENV_URL, "https://zimbra:7071")
    monkeypatch.setenv(zimbra.ENV_USER, "admin")
    monkeypatch.setenv(zimbra.ENV_PASSWORD, "x")
    assert isinstance(zimbra.choisir_zimbra(), zimbra.ZimbraReel)


def test_registrar_simule_sans_partenaire(monkeypatch):
    # Aucun partenaire câblé : `RegistrarOpenStack` reste le simulé sous un autre
    # nom — le choix explicite évite le faux « réel » que `fournisseur()` donnait
    # sur lab au seul motif `SYNELIA_FOURNISSEUR=openstack`.
    from synelia_openstack import registrar

    monkeypatch.delenv(registrar.ENV_URL, raising=False)
    assert type(registrar.choisir_registrar()) is registrar.RegistrarSimule
    monkeypatch.setenv(registrar.ENV_URL, "https://registrar.example.ci")
    assert isinstance(registrar.choisir_registrar(), registrar.RegistrarOpenStack)
