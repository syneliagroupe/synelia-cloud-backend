"""Mots de passe (argon2id), jetons d'accès (JWT EdDSA), TOTP."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jwt
from joserfc.jwk import OKPKey
from joserfc.jws import JWSRegistry
from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_kernel.dates import maintenant

_REGISTRE = JWSRegistry(algorithms=["EdDSA"])

_hasher = PasswordHasher()


def hacher_mot_de_passe(clair: str) -> str:
    return _hasher.hash(clair)


def verifier_mot_de_passe(clair: str, hache: str | None) -> bool:
    if not hache:
        return False
    try:
        return _hasher.verify(hache, clair)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001
        return False


@lru_cache
def cle_signature() -> OKPKey:
    r = reglages()
    if r.jwt_cle_privee:
        pem = r.jwt_cle_privee.encode()
    else:
        graine = hashlib.sha256(f"synelia-jwt:{r.secret}".encode()).digest()
        priv = Ed25519PrivateKey.from_private_bytes(graine)
        pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    cle = OKPKey.import_key(pem.decode())
    cle.ensure_kid()
    return cle


def jwks() -> dict[str, Any]:
    return {"keys": [cle_signature().as_dict(private=False)]}


def emettre_acces(claims: dict[str, Any], duree_s: int | None = None) -> str:
    r = reglages()
    now = int(maintenant().timestamp())
    corps = {
        "iss": r.jwt_emetteur,
        "iat": now,
        "exp": now + (duree_s or r.acces_duree_s),
        **claims,
    }
    return jwt.encode(
        {"alg": "EdDSA", "kid": cle_signature().kid}, corps, cle_signature(), registry=_REGISTRE
    )


def lire_acces(jeton: str) -> dict[str, Any]:
    try:
        decode = jwt.decode(jeton, cle_signature(), registry=_REGISTRE)
    except Exception as exc:  # noqa: BLE001
        raise erreurs.non_authentifie("Jeton invalide.") from exc
    claims = decode.claims
    if claims.get("exp", 0) < maintenant().timestamp():
        raise erreurs.non_authentifie("Jeton expiré.")
    return claims


def nouveau_secret_totp() -> str:
    return pyotp.random_base32()


def uri_totp(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Synelia Cloud")


def verifier_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code.replace(" ", ""), valid_window=1)


def hacher_jeton(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


def b64(octets: bytes) -> str:
    return base64.urlsafe_b64encode(octets).decode().rstrip("=")
