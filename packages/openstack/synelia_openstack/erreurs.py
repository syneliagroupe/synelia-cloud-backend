"""`openstack.exceptions.*` → `AppError` du contrat."""

from __future__ import annotations

from synelia_kernel import erreurs


def traduire(exc: Exception, ressource: str = "Ressource") -> erreurs.AppError:
    nom = type(exc).__name__
    message = str(exc)
    if nom == "ResourceNotFound":
        return erreurs.introuvable(ressource)
    if "No more IP addresses available" in message:
        # Pool d'IP flottantes du réseau externe épuisé (constaté en direct : Neutron 409
        # alors même que 0 FIP n'est allouée — bookkeeping IPAM, pas un conflit de nom).
        return erreurs.conflit(
            "Pool d'IP flottantes épuisé sur le réseau externe : aucune adresse disponible.",
            code="fip_epuise",
        )
    if nom == "ConflictException":
        return erreurs.conflit(message or "Conflit côté OpenStack.", code="nom_deja_pris")
    if "OverQuota" in message or "quota" in message.lower():
        return erreurs.quota_depasse(detail=message)
    if nom in {"HttpException", "SDKException", "ResourceTimeout"}:
        return erreurs.amont_indisponible("openstack", message)
    return erreurs.amont_indisponible("openstack", message)
