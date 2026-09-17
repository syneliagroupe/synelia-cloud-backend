from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_hebergement import service
from synelia.modules.web_hebergement.service import (
    SERVICES_PARTAGES,
    VERSIONS_PHP,
    depot,
    depot_comptes,
    depot_domaines,
    depot_taches,
    mesurer_espace_utilise,
    reconcilier_statut,
)
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/hebergements", tags=["Web Cloud — hébergement"])


@router.get("", response_model=m.WebHebergementsGetResponse, response_model_exclude_none=True)
async def lister_hebergements(
    page: Page,
    site: str | None = None,
    statut: str | None = None,
    palier: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:  # noqa: N803, PLR0917
    resultat = await depot.lister(
        ctx,
        page,
        filtre=lambda h: (
            (not site or h.serveur.site == site)
            and (not statut or h.statut == statut)
            and (not palier or h.palier == palier)
        ),
        tri_defaut="domaineProvisoire",
    )
    # Reconcile-on-read : sans ça un hébergement resterait affiché `en_ligne` dans la liste même
    # après la disparition de sa VM Nova (supprimée hors bande — cf. `reconcilier_statut`).
    resultat["donnees"] = [await reconcilier_statut(ctx, h) for h in resultat["donnees"]]
    return resultat


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_hebergement(
    corps: m.HebergementCreation, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    if corps.installer and corps.installer.type:
        # Le champ existe dans le contrat et était accepté sans erreur : `corps.model_dump()`
        # partait bien dans `entree` du travail (visible dans l'audit), mais aucun exécuteur ne
        # l'a jamais lu — la VM se met en service avec une page d'accueil générique, sans
        # WordPress/PrestaShop/etc., et le travail se déclare quand même « done » sur ses 5
        # étapes : un « faux succès » silencieux constaté en direct (installer: wordpress à la
        # création, page générique "Synelia Web Hebergement" servie ensuite). Installer une
        # application sur la VM déjà en service (`POST /web/sites`, `site.installer`) est un
        # mécanisme différent et déjà réel (SSH + docker compose) — le rebrancher pour qu'il
        # tourne automatiquement à la création est un vrai travail d'intégration (séquencement
        # après la mise en ligne, choix du `hote`, réutilisation du pool LB existant plutôt que
        # d'en ouvrir un nouveau), pas un correctif ponctuel : on échoue franchement plutôt que
        # de continuer à ignorer la demande en silence.
        raise erreurs.validation(
            "L'installation d'une application à la création de l'hébergement n'est pas encore "
            "prise en charge. Créez d'abord l'hébergement, attendez qu'il soit en ligne, puis "
            "installez l'application via POST /web/sites.",
            champs={
                "installer": "Non pris en charge à la création ; utilisez POST /web/sites une "
                "fois l'hébergement en ligne."
            },
        )
    if corps.domaine:
        await depot.exiger_nom_libre(ctx, corps.domaine)
    hebergement = service.construire_hebergement(ctx, corps)
    await depot.creer(ctx, hebergement)
    await journaliser(
        ctx,
        action="hebergement.creation",
        cible_type="web_hebergement",
        cible_id=hebergement.id,
        cible=hebergement.domaineProvisoire,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "hebergement.creer",
        hebergement.domaineProvisoire,
        cible_type="web_hebergement",
        cible_id=hebergement.id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Réserver les quotas du palier", "dureeS": 4},
            {"nom": "Créer le serveur d'hébergement (OpenStack)", "dureeS": 40},
            {"nom": "Router le domaine sur le load balancer partagé (Octavia)", "dureeS": 10},
            {"nom": "Provisionner le serveur de bases", "dureeS": 20},
            {"nom": "Activer la surveillance", "dureeS": 6},
        ],
    )


@router.get("/{hebergementId}", response_model=m.Hebergement, response_model_exclude_none=True)
async def obtenir_hebergement(
    hebergementId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    h = await reconcilier_statut(ctx, await depot.obtenir(ctx, hebergementId))
    # Mesure réelle de l'espace disque uniquement sur la fiche détail : un SSH par ligne sur
    # la liste serait trop coûteux pour un chiffre qui n'a pas besoin d'être à jour à chaque
    # requête (cf. `mesurer_espace_utilise`).
    return await mesurer_espace_utilise(ctx, h)


@router.patch("/{hebergementId}", response_model=m.Hebergement, response_model_exclude_none=True)
async def modifier_hebergement(
    hebergementId: str,
    corps: m.HebergementCreation,
    ctx: Contexte = Depends(exige("marketplace.subscribe")),
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    if corps.domaine and corps.domaine != h.domaine:
        await depot.exiger_nom_libre(ctx, corps.domaine)
    await depot.modifier(ctx, hebergementId, corps)
    await journaliser(
        ctx,
        action="hebergement.modification",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, hebergementId)


@router.delete(
    "/{hebergementId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_hebergement(
    hebergementId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("marketplace.subscribe")),
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    exiger_confirmation(h.domaineProvisoire, confirmation)
    await journaliser(
        ctx,
        action="hebergement.suppression",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        cible=h.domaineProvisoire,
    )
    return await demarrer_travail(
        ctx,
        "hebergement.supprimer",
        h.domaineProvisoire,
        cible_type="web_hebergement",
        cible_id=hebergementId,
        etapes=service.ETAPES_SUPPRESSION,
    )


@router.put(
    "/{hebergementId}/acces", response_model=m.Hebergement, response_model_exclude_none=True
)
async def modifier_acces_hebergement(
    hebergementId: str, corps: m.ReglagesAcces, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    acces = h.acces.model_dump(exclude_none=True)
    acces.update(corps.model_dump(exclude_none=True))
    await depot.modifier(ctx, hebergementId, {"acces": acces})
    await journaliser(
        ctx,
        action="hebergement.acces",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, hebergementId)


@router.post(
    "/{hebergementId}/attachement-domaine",
    response_model=m.Hebergement,
    response_model_exclude_none=True,
)
async def attacher_domaine_hebergement(
    hebergementId: str,
    corps: m.WebHebergementsHebergementIdAttachementDomainePostRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    domaine = await depot_domaines.par_nom(ctx, corps.domaine)
    if domaine is None:
        raise erreurs.introuvable("Domaine", corps.domaine)
    if domaine.hebergementId and domaine.hebergementId != hebergementId:
        raise erreurs.conflit(
            "Un domaine, un serveur : ce domaine est déjà attaché à un autre hébergement.",
            code="domaine_deja_attache",
        )
    await depot_domaines.modifier(ctx, domaine.id, {"hebergementId": hebergementId})
    h = await depot.modifier(ctx, hebergementId, {"domaine": corps.domaine})
    await journaliser(
        ctx,
        action="hebergement.attachement_domaine",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        cible=corps.domaine,
    )
    return h


@router.get(
    "/{hebergementId}/comptes-fichiers",
    response_model=list[m.CompteFichiers],
    response_model_exclude_none=True,
)
async def lister_comptes_fichiers(hebergementId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    return await depot_comptes.tous(ctx, parent_id=hebergementId)


@router.post(
    "/{hebergementId}/comptes-fichiers",
    response_model=m.CompteFichiers,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_compte_fichiers(
    hebergementId: str,
    corps: m.CompteFichiersCreation,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    compte = m.CompteFichiers(
        id=nouvel_id(),
        hebergementId=hebergementId,
        utilisateur=corps.utilisateur,
        protocoles=corps.protocoles,
        racine=corps.racine,
        quotaGo=corps.quotaGo,
        utiliseGo=0.0,
        clesSsh=len(corps.clesSshPubliques or []),
        statut="actif",
    )
    await depot_comptes.creer(ctx, compte, parent_id=hebergementId)
    if corps.motDePasse:
        await depot_comptes.definir_secrets(ctx, compte.id, {"mot_de_passe": corps.motDePasse})
    await journaliser(
        ctx,
        action="hebergement.compte_fichiers.creation",
        cible_type="web_compte_fichiers",
        cible_id=compte.id,
        cible=corps.utilisateur,
    )
    return compte


@router.patch(
    "/{hebergementId}/comptes-fichiers/{compteId}",
    response_model=m.CompteFichiers,
    response_model_exclude_none=True,
)
async def modifier_compte_fichiers(
    hebergementId: str,
    compteId: str,
    corps: m.CompteFichiersCreation,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    compte = await depot_comptes.obtenir(ctx, compteId)
    patch = corps.model_dump(exclude_none=True)
    if "clesSshPubliques" in patch:
        patch["clesSsh"] = len(patch.pop("clesSshPubliques") or [])
        compte = compte.model_copy(update=patch)
        await depot_comptes.remplacer(ctx, compteId, compte)
    else:
        await depot_comptes.modifier(ctx, compteId, patch)
    await journaliser(
        ctx,
        action="hebergement.compte_fichiers.modification",
        cible_type="web_compte_fichiers",
        cible_id=compteId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_comptes.obtenir(ctx, compteId)


@router.delete(
    "/{hebergementId}/comptes-fichiers/{compteId}", status_code=status.HTTP_204_NO_CONTENT
)
async def supprimer_compte_fichiers(
    hebergementId: str,
    compteId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Response:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    compte = await depot_comptes.obtenir(ctx, compteId)
    exiger_confirmation(compte.utilisateur, confirmation)
    await depot_comptes.supprimer(ctx, compteId)
    await journaliser(
        ctx,
        action="hebergement.compte_fichiers.suppression",
        cible_type="web_compte_fichiers",
        cible_id=compteId,
        cible=compte.utilisateur,
    )
    return Response(status_code=204)


@router.get(
    "/{hebergementId}/metriques",
    response_model=m.WebHebergementsHebergementIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_hebergement(
    hebergementId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    f = fenetre if fenetre in ("24h", "7j", "30j") else "24h"
    return service.metriques(f)


@router.put("/{hebergementId}/php", response_model=m.Hebergement, response_model_exclude_none=True)
async def modifier_php(
    hebergementId: str, corps: m.ReglagesPhp, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    version = corps.versionDefaut
    if version is not None:
        if version not in VERSIONS_PHP:
            raise erreurs.validation(
                "Version PHP non prise en charge.",
                champs={"versionDefaut": "Versions acceptées : 8.1 à 8.4."},
            )
        if version == h.php.versionDefaut:
            raise erreurs.conflit(
                "Cette version PHP est déjà celle du serveur.", code="version_php_identique"
            )
    php = h.php.model_dump(exclude_none=True)
    if version is not None:
        php["versionDefaut"] = version
    if corps.limites is not None:
        lim = h.php.limites.model_dump(exclude_none=True)
        lim.update(corps.limites.model_dump(exclude_none=True))
        php["limites"] = lim
    if corps.extensionsActivees is not None:
        actives = set(corps.extensionsActivees)
        php["extensions"] = [
            m.Extension(nom=e["nom"], active=e["nom"] in actives, requisePar=e.get("requisePar"))
            for e in h.php.extensions
        ]
    await depot.modifier(ctx, hebergementId, {"php": php})
    await journaliser(
        ctx,
        action="hebergement.php",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, hebergementId)


@router.post(
    "/{hebergementId}/redemarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def redemarrer_hebergement(
    hebergementId: str,
    corps: m.WebHebergementsHebergementIdRedemarragePostRequest,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    h = await depot.obtenir(ctx, hebergementId)
    await depot.definir_statut(ctx, hebergementId, "maintenance")
    await journaliser(
        ctx,
        action="hebergement.redemarrage",
        cible_type="web_hebergement",
        cible_id=hebergementId,
        cible=h.domaineProvisoire,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "hebergement.redemarrer",
        h.domaineProvisoire,
        cible_type="web_hebergement",
        cible_id=hebergementId,
        etapes=[
            {"nom": "Arrêter les services", "dureeS": 6},
            {"nom": "Redémarrer le serveur", "dureeS": 30},
            {"nom": "Vérifier les services", "dureeS": 10},
        ],
    )


@router.get(
    "/{hebergementId}/services-partages",
    response_model=list[m.ServicePartage],
    response_model_exclude_none=True,
)
async def lister_services_partages(hebergementId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    return [s.model_copy(update={"hebergementId": hebergementId}) for s in SERVICES_PARTAGES]


@router.get(
    "/{hebergementId}/taches",
    response_model=list[m.TachePlanifieeWeb],
    response_model_exclude_none=True,
)
async def lister_taches_web(hebergementId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    return await depot_taches.tous(ctx, parent_id=hebergementId)


@router.post(
    "/{hebergementId}/taches",
    response_model=m.TachePlanifieeWeb,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_tache_web(
    hebergementId: str,
    corps: m.TachePlanifieeWebCreation,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    tache = m.TachePlanifieeWeb(
        id=nouvel_id(),
        hebergementId=hebergementId,
        libelle=corps.libelle,
        expression=corps.expression,
        lisible=service.traduire_cron(corps.expression),
        commande=corps.commande,
        siteId=corps.siteId,
        actif=bool(corps.actif if corps.actif is not None else True),
        prochaine=maintenant(),
        statut="ok",
    )
    await depot_taches.creer(ctx, tache, parent_id=hebergementId)
    await journaliser(
        ctx,
        action="hebergement.tache.creation",
        cible_type="web_tache",
        cible_id=tache.id,
        cible=corps.libelle,
    )
    return tache


@router.patch(
    "/{hebergementId}/taches/{tacheId}",
    response_model=m.TachePlanifieeWeb,
    response_model_exclude_none=True,
)
async def modifier_tache_web(
    hebergementId: str,
    tacheId: str,
    corps: m.TachePlanifieeWebCreation,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    await depot_taches.obtenir(ctx, tacheId)
    patch = corps.model_dump(exclude_none=True)
    if corps.expression:
        patch["lisible"] = service.traduire_cron(corps.expression)
    await depot_taches.modifier(ctx, tacheId, patch)
    await journaliser(
        ctx,
        action="hebergement.tache.modification",
        cible_type="web_tache",
        cible_id=tacheId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_taches.obtenir(ctx, tacheId)


@router.delete("/{hebergementId}/taches/{tacheId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_tache_web(
    hebergementId: str, tacheId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Response:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    await depot_taches.obtenir(ctx, tacheId)
    await depot_taches.supprimer(ctx, tacheId)
    await journaliser(
        ctx, action="hebergement.tache.suppression", cible_type="web_tache", cible_id=tacheId
    )
    return Response(status_code=204)


@router.post(
    "/{hebergementId}/taches/{tacheId}/execution",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def executer_tache_web(
    hebergementId: str, tacheId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, hebergementId)
    t = await depot_taches.obtenir(ctx, tacheId)
    await journaliser(
        ctx,
        action="hebergement.tache.execution",
        cible_type="web_tache",
        cible_id=tacheId,
        cible=t.libelle,
    )
    return await demarrer_travail(
        ctx,
        "tache.execution",
        t.libelle,
        cible_type="web_tache",
        cible_id=tacheId,
        etapes=[
            {"nom": "Préparer l'exécution", "dureeS": 2},
            {"nom": "Exécuter la commande", "dureeS": 5},
            {"nom": "Enregistrer le résultat", "dureeS": 2},
        ],
    )
