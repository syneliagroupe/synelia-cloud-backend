"""Cloud-init « intérieur » de la plateforme : clés SSH du compte, fusionné avec celui de l'utilisateur."""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml

ENTETE = "#cloud-config"


def construire(cles: list[str] | None, utilisateur: str | None) -> str | None:
    """User-data final : plateforme seule, utilisateur seul, ou les deux fusionnés."""
    utilisateur = (utilisateur or "").strip() or None
    cles = [c for c in dict.fromkeys(cles or []) if c]
    if not cles:
        return utilisateur
    plateforme = {"ssh_authorized_keys": cles}
    if utilisateur is None:
        return f"{ENTETE}\n{yaml.safe_dump(plateforme)}"
    if utilisateur.startswith(ENTETE):
        try:
            doc = yaml.safe_load(utilisateur) or {}
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict):
            doc["ssh_authorized_keys"] = list(
                dict.fromkeys([*cles, *(doc.get("ssh_authorized_keys") or [])])
            )
            return f"{ENTETE}\n{yaml.safe_dump(doc, sort_keys=False)}"
    # Script shell ou YAML illisible : deux parties MIME, cloud-init les exécute l'une après l'autre.
    msg = MIMEMultipart()
    msg.attach(MIMEText(f"{ENTETE}\n{yaml.safe_dump(plateforme)}", "cloud-config"))
    msg.attach(
        MIMEText(utilisateur, "cloud-config" if utilisateur.startswith(ENTETE) else "x-shellscript")
    )
    return msg.as_string()


def _demo() -> None:
    k = ["ssh-ed25519 AAAA a"]
    assert construire(None, "  ") is None
    assert construire([], "#!/bin/sh\necho") == "#!/bin/sh\necho"
    assert yaml.safe_load(construire(k, None))["ssh_authorized_keys"] == k
    fusion = yaml.safe_load(
        construire(k, "#cloud-config\npackages: [git]\nssh_authorized_keys: [ssh-rsa BBB b]")
    )
    assert fusion["packages"] == ["git"] and fusion["ssh_authorized_keys"] == [*k, "ssh-rsa BBB b"]
    mime = construire(k, "#!/bin/sh\necho hi")
    assert (
        "multipart/mixed" in mime and "text/x-shellscript" in mime and "ssh-ed25519 AAAA a" in mime
    )


if __name__ == "__main__":
    _demo()
