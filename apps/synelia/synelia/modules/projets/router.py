from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.facturation.metrologie import PRIX
from synelia.modules.modeles import service as modeles_service
from synelia.modules.projets import service as s
from synelia.travaux import demarrer_travail

router = APIRouter(tags=["Projets applicatifs"])

router_projets = APIRouter(prefix="/projets")
router_domaines = APIRouter(prefix="/domaines-applicatifs")
router_zone = APIRouter(prefix="/zone-applicative")
router_routage = APIRouter(prefix="/routage")

HEURES_MOIS = 730
JOURS_MOIS = 30
PORT_DEFAUT = 8080


def _mot_de_passe() -> str:
    # `random` n'est pas un générateur cryptographique : un mot de passe de service réel
    # doit sortir de `secrets` (via `jeton_opaque`, même convention que `bases`/`web_smtp`).
    return jeton_opaque(16)


def _cout(ressources: m.Ressources2) -> int:
    cpu = int(ressources.cpu * PRIX["vcpu_heure"] * HEURES_MOIS)
    ram = int((ressources.ramMo / 1024) * PRIX["ram_go_heure"] * HEURES_MOIS)
    disk = int(ressources.diskGo * PRIX["stockage_to_jour"] * JOURS_MOIS)
    return cpu + ram + disk


# ─────────────── Projets ───────────────


@router_projets.get("/synthese", response_model=m.SyntheseProjets, response_model_exclude_none=True)
async def obtenir_synthese_projets(ctx: Contexte = Depends(exige(None))) -> Any:
    projets = await s.depot_projet.tous(ctx)
    services = []
    for p in projets:
        services += await s.depot_service.tous(ctx, parent_id=p.id)
    domaines = await s.depot_domaine.tous(ctx)
    en_echec = len([x for x in services if x.statut == "failed"])
    a_verifier = len([d for d in domaines if d.certificat.etat != "actif"])
    cout = sum(x.coutMensuel or 0 for x in services)
    return m.SyntheseProjets(
        projets=len(projets),
        services=len(services),
        enEchec=en_echec,
        domaines=len(domaines),
        domainesAVerifier=a_verifier,
        coutMensuel=cout,
    )


@router_projets.get("", response_model=m.ProjetsGetResponse, response_model_exclude_none=True)
async def lister_projets(
    page: Page,
    espaceId: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:  # noqa: N803
    return await s.depot_projet.lister(
        ctx, page, filtre=lambda p: not espaceId or p.espaceId == espaceId, tri_defaut="nom"
    )


@router_projets.post(
    "",
    response_model=m.Projet,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_projet(
    corps: m.ProjetCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:
    await Depot("espace", m.EspaceCloud).obtenir(ctx, corps.espaceId)
    await s.depot_projet.exiger_nom_libre(ctx, corps.nom)
    projet = m.Projet(
        id=nouvel_id(),
        nom=corps.nom,
        description=corps.description or "",
        espaceId=corps.espaceId,
        cree=maintenant(),
        environnements=corps.environnements or ["production"],
        etiquettes=corps.etiquettes or [],
        clusterId=corps.clusterId or "",
        variables=[],
        cible=corps.cible or "k8s",
    )
    await s.depot_projet.creer(ctx, projet, parent_id=corps.espaceId)
    if projet.cible == "vm":
        # Provisionnement paresseux : contrairement à un namespace Kubernetes (objet API sans
        # coût réel propre, créé ici sans attendre un premier service), la VM Nova dédiée
        # d'un projet en cible `vm` a un coût réel — elle n'est provisionnée qu'au premier
        # service ayant réellement quelque chose à exécuter (`_assurer_vm_projet`).
        pass
    else:
        # `k8s_client` est synchrone/bloquant : déchargé dans un thread pour ne pas geler la
        # boucle asyncio (même garde que `synelia.modules.vms.service`).
        await asyncio.to_thread(s.k8s().creer_namespace, s.namespace_projet(projet))
    await journaliser(
        ctx, action="projet.creation", cible_type="projet", cible_id=projet.id, cible=projet.nom
    )
    return projet


@router_projets.get("/{projetId}", response_model=m.Projet, response_model_exclude_none=True)
async def obtenir_projet(
    projetId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await s.depot_projet.obtenir(ctx, projetId)


@router_projets.patch("/{projetId}", response_model=m.Projet, response_model_exclude_none=True)
async def modifier_projet(
    projetId: str, corps: m.ProjetCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    p = await s.depot_projet.obtenir(ctx, projetId)
    if corps.nom and corps.nom != p.nom:
        await s.depot_projet.exiger_nom_libre(ctx, corps.nom)
    changements: dict[str, Any] = {}
    if corps.nom:
        changements["nom"] = corps.nom
    if corps.description is not None:
        changements["description"] = corps.description
    if corps.espaceId:
        changements["espaceId"] = corps.espaceId
    if corps.environnements is not None:
        changements["environnements"] = corps.environnements
    await s.depot_projet.modifier(ctx, projetId, changements)
    await journaliser(
        ctx, action="projet.modification", cible_type="projet", cible_id=projetId, cible=p.nom
    )
    return await s.depot_projet.obtenir(ctx, projetId)


@router_projets.delete(
    "/{projetId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_projet(
    projetId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    p = await s.depot_projet.obtenir(ctx, projetId)
    exiger_confirmation(p.nom, confirmation)
    if await s.depot_service.compter(ctx, parent_id=projetId):
        raise erreurs.conflit("Le projet contient encore des services.", code="projet_non_vide")
    await journaliser(
        ctx, action="projet.suppression", cible_type="projet", cible_id=projetId, cible=p.nom
    )
    return await demarrer_travail(
        ctx,
        "projet.delete",
        p.nom,
        cible_type="projet",
        cible_id=projetId,
        etapes=[
            {"nom": "Décommissionner les environnements", "dureeS": 8},
            {"nom": "Libérer les ressources du projet", "dureeS": 20},
        ],
    )


def _source(from_: m.Source2 | None) -> m.Source1 | None:
    if from_ is None or not from_.type or not from_.ref:
        return None
    return m.Source1(type=from_.type, ref=from_.ref, branche=from_.branche)


def _cron(from_: m.Cron1 | None) -> m.Cron | None:
    if from_ is None:
        return None
    return m.Cron(expression=from_.expression, commande=from_.commande)


def _file(from_: m.File1 | None) -> m.File | None:
    if from_ is None:
        return None
    return m.File(nom=from_.nom, concurrence=from_.concurrence)


async def _projet(ctx: Contexte, projet_id: str) -> m.Projet:
    return await s.depot_projet.obtenir(ctx, projet_id)


@router_projets.get("/{projetId}/services", response_model=list[m.ServiceProjet])
async def lister_services_projet(
    projetId: str,
    environnement: str | None = None,
    type: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: A002,N803
    await _projet(ctx, projetId)
    return await s.depot_service.tous(
        ctx,
        parent_id=projetId,
        filtre=lambda sv: (
            (not environnement or sv.environnement == environnement)
            and (not type or sv.type == type)
        ),
    )


async def _ressources(
    corps: m.ServiceProjetCreation, modele: m.ModeleApplicatif | None, site: Literal["ABJ", "GBM"]
) -> tuple[m.Ressources2, m.Emplacement1]:
    if corps.ressources and corps.ressources.cpu:
        ressources = m.Ressources2(
            cpu=corps.ressources.cpu,
            ramMo=corps.ressources.ramMo or 512,
            diskGo=corps.ressources.diskGo or 10,
        )
    elif modele:
        ressources = m.Ressources2(
            cpu=modele.ressources.cpu,
            ramMo=modele.ressources.ramMo,
            diskGo=modele.ressources.diskGo,
        )
    else:
        ressources = m.Ressources2(cpu=0.5, ramMo=512, diskGo=10)
    emplacement = m.Emplacement1(
        site=site, backend="openstack-abj" if site == "ABJ" else "openstack-gbm"
    )
    return ressources, emplacement


@router_projets.post(
    "/{projetId}/services",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_service_projet(
    projetId: str, corps: m.ServiceProjetCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    projet = await _projet(ctx, projetId)
    await s.depot_service.exiger_nom_libre(ctx, corps.nom, parent_id=projetId)
    espace = await Depot("espace", m.EspaceCloud).obtenir(ctx, projet.espaceId)
    site: Literal["ABJ", "GBM"] = "ABJ" if espace.site == "ABJ" else "GBM"
    modele = await modeles_service.obtenir(ctx, corps.modeleSlug) if corps.modeleSlug else None
    ressources, emplacement = await _ressources(corps, modele, site)
    port = (
        corps.portConteneur
        or (
            next((pt.conteneur for pt in modele.ports if pt.protocole == "http"), None)
            if modele
            else None
        )
        or (s.MOTEUR_PORT.get(corps.moteur or "") if corps.type == "base" else None)
        or PORT_DEFAUT
    )
    service_id = nouvel_id()
    service = m.ServiceProjet(
        id=service_id,
        projetId=projetId,
        nom=corps.nom,
        type=corps.type,
        environnement=corps.environnement,
        statut="building",
        ressources=ressources,
        emplacement=emplacement,
        derniereMaj=maintenant(),
        coutMensuel=_cout(ressources),
        modeleSlug=corps.modeleSlug,
        sieges=m.Sieges(attribues=0, souscrits=0),
        source=_source(corps.source),
        portConteneur=port,
        moteur=corps.moteur,
        version=corps.version or (modele.version if modele else None),
        base=m.Base1(
            nom=corps.nom,
            utilisateur=s.utilisateur_base(corps.nom),
            hoteInterne=s.hote_interne(
                m.ServiceProjet(
                    id=service_id,
                    projetId=projetId,
                    nom=corps.nom,
                    type=corps.type,
                    environnement=corps.environnement,
                    statut="building",
                    ressources=ressources,
                    emplacement=emplacement,
                    derniereMaj=maintenant(),
                    coutMensuel=0,
                ),
                projet,
            ),
            port=s.MOTEUR_PORT.get(corps.moteur or "", 5432),
        )
        if corps.type == "base" and corps.moteur
        else None,
        exposeExterne=corps.exposeExterne,
        cron=_cron(corps.cron),
        file=_file(corps.file),
    )
    secrets = {"motDePasse": _mot_de_passe()}
    if corps.type == "base":
        utilisateur_bd = s.utilisateur_base(corps.nom)
        secrets.update(
            {
                "utilisateur": utilisateur_bd,
                "base": corps.nom,
                "uri": f"{corps.moteur or 'postgresql'}://{utilisateur_bd}:{secrets['motDePasse']}@{service.base.hoteInterne}:{service.base.port}/{corps.nom}"
                if service.base
                else "",
            }
        )
    await s.depot_service.creer(ctx, service, parent_id=projetId, secrets=secrets)
    await journaliser(
        ctx,
        action="service.creation",
        cible_type="projet_service",
        cible_id=service_id,
        cible=corps.nom,
    )
    return await demarrer_travail(
        ctx,
        "projet_service.create",
        corps.nom,
        cible_type="projet_service",
        cible_id=service_id,
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Construire l'image", "dureeS": 5},
            {"nom": "Déployer l'instance", "dureeS": 8},
            {"nom": "Router et vérifier les sondes", "dureeS": 4},
        ],
    )


@router_projets.get(
    "/{projetId}/services/{serviceId}",
    response_model=m.ServiceProjet,
    response_model_exclude_none=True,
)
async def obtenir_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    return await s.depot_service.obtenir(ctx, serviceId)


@router_projets.patch(
    "/{projetId}/services/{serviceId}",
    response_model=m.ServiceProjet,
    response_model_exclude_none=True,
)
async def modifier_service_projet(
    projetId: str,
    serviceId: str,
    corps: m.ServiceProjetCreation,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    svc = await s.depot_service.obtenir(ctx, serviceId)
    changements: dict[str, Any] = {}
    if corps.nom and corps.nom != svc.nom:
        await s.depot_service.exiger_nom_libre(ctx, corps.nom, parent_id=projetId)
        changements["nom"] = corps.nom
    if corps.ressources and corps.ressources.cpu:
        ressources = m.Ressources2(
            cpu=corps.ressources.cpu,
            ramMo=corps.ressources.ramMo or svc.ressources.ramMo,
            diskGo=corps.ressources.diskGo or svc.ressources.diskGo,
        )
        changements["ressources"] = ressources
        changements["coutMensuel"] = _cout(ressources)
    if corps.source is not None:
        changements["source"] = _source(corps.source)
    for champ in (
        "type",
        "environnement",
        "portConteneur",
        "version",
        "moteur",
        "exposeExterne",
    ):
        valeur = getattr(corps, champ, None)
        if valeur is not None:
            changements[champ] = valeur
    if corps.cron is not None:
        changements["cron"] = _cron(corps.cron)
    if corps.file is not None:
        changements["file"] = _file(corps.file)
    changements["derniereMaj"] = maintenant()
    await s.depot_service.modifier(ctx, serviceId, changements)
    await journaliser(
        ctx,
        action="service.modification",
        cible_type="projet_service",
        cible_id=serviceId,
        cible=svc.nom,
    )
    return await s.depot_service.obtenir(ctx, serviceId)


@router_projets.delete(
    "/{projetId}/services/{serviceId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_service_projet(
    projetId: str,
    serviceId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("app.deploy")),
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    svc = await s.depot_service.obtenir(ctx, serviceId)
    exiger_confirmation(svc.nom, confirmation)
    await journaliser(
        ctx,
        action="service.suppression",
        cible_type="projet_service",
        cible_id=serviceId,
        cible=svc.nom,
    )
    return await demarrer_travail(
        ctx,
        "projet_service.delete",
        svc.nom,
        cible_type="projet_service",
        cible_id=serviceId,
        etapes=[
            {"nom": "Retirer les instances", "dureeS": 4},
            {"nom": "Nettoyer les ressources", "dureeS": 6},
        ],
    )


async def _operation_service(
    ctx: Contexte, projet_id: str, service_id: str, type_travail: str, label: str
) -> dict[str, Any]:
    await _projet(ctx, projet_id)
    svc = await s.depot_service.obtenir(ctx, service_id)
    await journaliser(
        ctx,
        action=f"service.{label}",
        cible_type="projet_service",
        cible_id=service_id,
        cible=svc.nom,
    )
    return await demarrer_travail(
        ctx,
        type_travail,
        svc.nom,
        cible_type="projet_service",
        cible_id=service_id,
        etapes=[
            {"nom": "Appliquer l'opération", "dureeS": 3},
            {"nom": "Vérifier le service", "dureeS": 3},
        ],
    )


@router_projets.post(
    "/{projetId}/services/{serviceId}/arret",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def arreter_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    return await _operation_service(ctx, projetId, serviceId, "projet_service.stopped", "arret")


@router_projets.post(
    "/{projetId}/services/{serviceId}/demarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def demarrer_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    return await _operation_service(ctx, projetId, serviceId, "projet_service.create", "demarrage")


@router_projets.post(
    "/{projetId}/services/{serviceId}/execution",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def executer_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    svc = await s.depot_service.obtenir(ctx, serviceId)
    if svc.type not in ("cron", "worker"):
        raise erreurs.conflit(
            "L'exécution manuelle n'est possible que pour les services cron ou worker.",
            code="execution_non_supportee",
        )
    await journaliser(
        ctx,
        action="service.execution",
        cible_type="projet_service",
        cible_id=serviceId,
        cible=svc.nom,
    )
    return await demarrer_travail(
        ctx,
        "projet_service.create",
        svc.nom,
        cible_type="projet_service",
        cible_id=serviceId,
        etapes=[
            {"nom": "Lancer l'exécution", "dureeS": 4},
            {"nom": "Collecter les résultats", "dureeS": 6},
        ],
    )


@router_projets.post(
    "/{projetId}/services/{serviceId}/redemarrage",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def redemarrer_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige("component.restart"))
) -> Any:  # noqa: N803
    return await _operation_service(
        ctx, projetId, serviceId, "projet_service.create", "redemarrage"
    )


@router_projets.get(
    "/{projetId}/services/{serviceId}/identifiants",
    response_model=m.ProjetsProjetIdServicesServiceIdIdentifiantsGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_identifiants_service_projet(
    projetId: str, serviceId: str, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:  # noqa: N803
    projet = await _projet(ctx, projetId)
    svc = await s.depot_service.obtenir(ctx, serviceId)
    secrets = await s.depot_service.secrets(ctx, serviceId)
    port = svc.portConteneur or (svc.base.port if svc.base else None) or PORT_DEFAUT
    return m.ProjetsProjetIdServicesServiceIdIdentifiantsGetResponse(
        hoteInterne=s.hote_interne(svc, projet),
        port=port,
        utilisateur=svc.base.utilisateur if svc.base else secrets.get("utilisateur"),
        motDePasse=secrets.get("motDePasse"),
        base=secrets.get("base"),
        uri=secrets.get("uri"),
        variablesInjectees=[v.cle for v in projet.variables if v.portee == "runtime"],
    )


@router_projets.get(
    "/{projetId}/services/{serviceId}/journaux",
    response_model=m.ExtraitLogs,
    response_model_exclude_none=True,
)
async def obtenir_journaux_service_projet(
    projetId: str, serviceId: str, niveau: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    await s.depot_service.obtenir(ctx, serviceId)
    return m.ExtraitLogs(lignes=[], tronque=False)


@router_projets.get(
    "/{projetId}/services/{serviceId}/metriques",
    response_model=m.ProjetsProjetIdServicesServiceIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_service_projet(
    projetId: str, serviceId: str, fenetre: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await _projet(ctx, projetId)
    await s.depot_service.obtenir(ctx, serviceId)
    return m.ProjetsProjetIdServicesServiceIdMetriquesGetResponse(series=[])


@router_projets.get("/{projetId}/variables", response_model=m.ProjetsProjetIdVariablesGetResponse)
async def lister_variables_projet(
    projetId: str, ctx: Contexte = Depends(exige("secrets.update"))
) -> Any:  # noqa: N803
    p = await _projet(ctx, projetId)
    return [v.model_copy(update={"valeur": None}) if v.secret else v for v in p.variables]


@router_projets.put(
    "/{projetId}/variables",
    response_model=m.ProjetsProjetIdVariablesPutResponse,
    response_model_exclude_none=True,
)
async def modifier_variables_projet(
    projetId: str,
    corps: m.ProjetsProjetIdVariablesPutRequest,
    ctx: Contexte = Depends(exige("secrets.update")),
) -> Any:  # noqa: N803
    p = await _projet(ctx, projetId)
    existantes: dict[tuple[str, str], m.Variable1] = {(v.cle, v.portee): v for v in p.variables}
    for v in corps.variables:
        portee = v.portee or "runtime"
        cle = (v.cle, portee)
        if v.supprimer:
            existantes.pop(cle, None)
            continue
        existantes[cle] = m.Variable1(
            cle=v.cle,
            valeur=v.valeur
            if v.valeur is not None
            else (existantes[cle].valeur if cle in existantes else None),
            secret=v.secret
            if v.secret is not None
            else (existantes[cle].secret if cle in existantes else False),
            portee=portee,
            environnements=[e for e in (v.environnements or ["production"])],
        )
    await s.depot_projet.modifier(
        ctx, projetId, {"variables": [e for _, e in sorted(existantes.items())]}
    )
    await journaliser(
        ctx,
        action="projet.variables",
        cible_type="projet",
        cible_id=projetId,
        details={"appliquees": len(existantes)},
    )
    return m.ProjetsProjetIdVariablesPutResponse(appliquees=len(existantes))


# ─────────────── Domaines applicatifs ───────────────


def _est_verifie(d: m.DomaineApplicatif) -> bool:
    return bool(d.verification and d.verification.etat == "ok")


def _enregistrement(hote: str) -> m.Enregistrement:
    apex = hote.startswith("@" + s.ZONE) or hote == s.ZONE
    return m.Enregistrement(
        type=("A" if apex else "CNAME"),
        nom=hote,
        valeur=s.INGRESS[0].ip if apex else f"ingress.{s.ZONE}",
    )


@router_domaines.get(
    "", response_model=m.DomainesApplicatifsGetResponse, response_model_exclude_none=True
)
async def lister_domaines_applicatifs(
    page: Page,
    serviceId: str | None = None,
    origine: str | None = None,
    verification: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803
    def filtre(d: m.DomaineApplicatif) -> bool:
        if serviceId and d.serviceId != serviceId:
            return False
        if origine and d.origine != origine:
            return False
        if verification:
            attendu = verification == "verifie"
            if _est_verifie(d) != attendu:
                return False
        return True

    return await s.depot_domaine.lister(ctx, page, filtre=filtre, tri_defaut="hote")


@router_domaines.post(
    "",
    response_model=m.DomaineApplicatif,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_domaine_applicatif(
    corps: m.DomaineApplicatifCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:
    svc = await Depot("projet_service", m.ServiceProjet).obtenir(ctx, corps.serviceId)
    await s.depot_domaine.exiger_nom_libre(ctx, corps.hote)
    origine = "genere" if corps.hote.endswith("." + s.ZONE) else "personnalise"
    port = corps.portConteneur or svc.portConteneur or PORT_DEFAUT
    https = corps.https if corps.https is not None else True
    domaine = m.DomaineApplicatif(
        id=nouvel_id(),
        hote=corps.hote,
        origine=origine,
        serviceId=corps.serviceId,
        chemin=corps.chemin or "/",
        portConteneur=port,
        https=https,
        certificat=m.Certificat1(etat="en_emission" if https else "aucun"),
        verification=m.Verification(etat="attente", enregistrement=_enregistrement(corps.hote)),
        redirections=corps.redirections,
    )
    await s.depot_domaine.creer(ctx, domaine, parent_id=corps.serviceId)
    await s.depot_domaine.definir_statut(ctx, domaine.id, "en_verification")
    await journaliser(
        ctx,
        action="domaine.creation",
        cible_type="domaine_applicatif",
        cible_id=domaine.id,
        cible=corps.hote,
    )
    return await s.depot_domaine.obtenir(ctx, domaine.id)


@router_domaines.get(
    "/{domaineId}", response_model=m.DomaineApplicatif, response_model_exclude_none=True
)
async def obtenir_domaine_applicatif(domaineId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await s.depot_domaine.obtenir(ctx, domaineId)


@router_domaines.patch(
    "/{domaineId}", response_model=m.DomaineApplicatif, response_model_exclude_none=True
)
async def modifier_domaine_applicatif(
    domaineId: str, corps: m.DomaineApplicatifCreation, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    d = await s.depot_domaine.obtenir(ctx, domaineId)
    changements: dict[str, Any] = {}
    if corps.hote and corps.hote != d.hote:
        await s.depot_domaine.exiger_nom_libre(ctx, corps.hote)
        changements["hote"] = corps.hote
    for champ in ("chemin", "portConteneur", "https", "redirections"):
        valeur = getattr(corps, champ, None)
        if valeur is not None:
            changements[champ] = valeur
    await s.depot_domaine.modifier(ctx, domaineId, changements)
    await journaliser(
        ctx,
        action="domaine.modification",
        cible_type="domaine_applicatif",
        cible_id=domaineId,
        cible=d.hote,
    )
    return await s.depot_domaine.obtenir(ctx, domaineId)


@router_domaines.delete("/{domaineId}", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_domaine_applicatif(
    domaineId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("app.deploy"))
) -> Response:  # noqa: N803
    d = await s.depot_domaine.obtenir(ctx, domaineId)
    exiger_confirmation(d.hote, confirmation)
    await journaliser(
        ctx,
        action="domaine.suppression",
        cible_type="domaine_applicatif",
        cible_id=domaineId,
        cible=d.hote,
    )
    await s.depot_domaine.supprimer(ctx, domaineId, logique=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router_domaines.post(
    "/{domaineId}/verification",
    response_model=m.DomaineApplicatif,
    response_model_exclude_none=True,
)
async def verifier_domaine_applicatif(
    domaineId: str, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    d = await s.depot_domaine.obtenir(ctx, domaineId)
    verification = m.Verification(
        etat="ok",
        enregistrement=_enregistrement(d.hote),
        verifieLe=maintenant(),
        detail="Enregistrement trouvé en simulation.",
    )
    await s.depot_domaine.modifier(
        ctx, domaineId, {"verification": verification.model_dump(mode="json")}
    )
    await s.depot_domaine.definir_statut(ctx, domaineId, "verifie")
    await journaliser(
        ctx,
        action="domaine.verification",
        cible_type="domaine_applicatif",
        cible_id=domaineId,
        cible=d.hote,
    )
    return await s.depot_domaine.obtenir(ctx, domaineId)


@router_domaines.post(
    "/{domaineId}/certificat",
    response_model=m.DomaineApplicatif,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def emettre_certificat_domaine_applicatif(
    domaineId: str, ctx: Contexte = Depends(exige("app.deploy"))
) -> Any:  # noqa: N803
    d = await s.depot_domaine.obtenir(ctx, domaineId)
    if not _est_verifie(d):
        raise erreurs.conflit(
            "Le domaine doit être vérifié avant l'émission du certificat.",
            code="domaine_non_verifie",
        )
    await demarrer_travail(
        ctx,
        "domaine_certificat.emission",
        d.hote,
        cible_type="domaine_applicatif",
        cible_id=domaineId,
        etapes=[
            {"nom": "Publier la validation DNS", "dureeS": 3},
            {"nom": "Émettre le certificat", "dureeS": 4},
        ],
    )
    await journaliser(
        ctx,
        action="domaine.certificat",
        cible_type="domaine_applicatif",
        cible_id=domaineId,
        cible=d.hote,
    )
    return await s.depot_domaine.obtenir(ctx, domaineId)


# ─────────────── Zone applicative ───────────────


@router_zone.get("", response_model=m.ZoneApplicative, response_model_exclude_none=True)
async def obtenir_zone_applicative(ctx: Contexte = Depends(exige(None))) -> Any:
    domaines = await s.depot_domaine.tous(ctx)
    return m.ZoneApplicative(
        zone=s.ZONE,
        wildcard=f"*.{s.ZONE}",
        ingress=s.INGRESS,
        certificat=m.Certificat3(
            emetteur="Let's Encrypt",
            renouvellementAuto=True,
            expire=maintenant() + timedelta(days=90),
        ),
        quotaDomaines=m.QuotaDomaines(utilises=len(domaines), total=20),
    )


# ─────────────── Routage ───────────────


@router_routage.get("", response_model=m.RoutageGetResponse, response_model_exclude_none=True)
async def lister_regles_routage(
    page: Page,
    hote: str | None = None,
    environnement: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803
    domaines = await s.depot_domaine.tous(ctx)
    regles: list[m.RegleRoutage] = []
    for d in domaines:
        svc = await s.depot_service.trouver(ctx, d.serviceId)
        regles.append(
            m.RegleRoutage(
                id=d.id,
                hote=d.hote,
                chemin=d.chemin,
                serviceId=d.serviceId,
                serviceNom=svc.nom if svc else None,
                environnement=svc.environnement if svc else None,
                portConteneur=d.portConteneur,
                https=d.https,
                actif=True,
            )
        )
    regles = [
        r
        for r in regles
        if (not hote or r.hote == hote) and (not environnement or r.environnement == environnement)
    ]
    total = len(regles)
    debut = (page.page - 1) * page.par_page
    return m.RoutageGetResponse(
        donnees=regles[debut : debut + page.par_page],
        pagination=m.Pagination(
            page=page.page,
            parPage=page.par_page,
            total=total,
            totalPages=max(1, -(-total // page.par_page)) if total else 0,
        ),
    )


routers = [router_projets, router_domaines, router_zone, router_routage]
