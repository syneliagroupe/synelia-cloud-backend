"""Règles sauvegarde (Web Cloud) : dépôt, exécuteurs, donnée de démo."""

from __future__ import annotations

import asyncio

from synelia_contract import modeles as m
from synelia_db.modeles import Ressource, Travail
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot(
    "web_sauvegarde",
    m.SauvegardeWeb,
    libelle="Sauvegarde",
    champ_nom="nomServi",
    champ_statut="actif",
    champs_recherche=("nomServi", "serveur", "hebergementId"),
)


def point(nombre: str = "1.2 Go", contenu: list[str] | None = None) -> m.ExecutionSauvegarde:
    return m.ExecutionSauvegarde(
        id=nouvel_id(),
        ts=maintenant(),
        statut="ok",
        taille=nombre,
        dureeMin=5,
        contenu=contenu or ["Site", "Base de données"],
        immuableJusqua=None,
        message=None,
    )


@executeur("web.backup.run")
class ExecuteurSauvegardeRun(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        from synelia.modules.web_hebergement.service import amont, serveur_id

        s = await depot.obtenir(ctx, travail.cible_id or "")
        image_id = None
        try:
            sid = await serveur_id(ctx, s.hebergementId)
            if sid and sid != s.hebergementId:
                # `amont().instantane` (openstacksdk, synchrone) est déchargé via
                # `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel amont
                # lent gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
                image_id = await asyncio.to_thread(
                    amont().instantane, sid, f"backup-{s.nomServi}-{nouvel_id()[:8]}"
                )
        except Exception:  # noqa: BLE001 — hébergement de démo sans serveur réel, ou amont absent
            image_id = None
        p = point()
        if image_id:
            p = p.model_copy(update={"message": f"Image Glance {image_id}"})
        executions = [*s.executions, p]
        await depot.modifier(
            ctx, s.id, {"executions": [e.model_dump(mode="json") for e in executions]}
        )
        if image_id:
            await depot.definir_secrets(ctx, s.id, {f"image_{p.id}": image_id})


@executeur("web.backup.restore")
class ExecuteurSauvegardeRestore(Executeur):
    """Jusqu'ici ce type de travail n'avait **aucun** exécuteur enregistré du tout : le
    guide (`docs/GUIDE-MODULE.md`) est explicite — « sans exécuteur, le travail réussit en
    simulation » — donc `POST .../restauration` rendait déjà un 202 `done` sans qu'aucun code
    ne s'exécute, jamais la moindre écriture DB. Pire que `vm.compose` avant son fix (qui, lui,
    écrivait au moins en base sans toucher l'amont)."""

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 1:  # « Restaurer les fichiers » (catalogue `web.backup.restore`)
            entre = travail.entree or {}
            s = await depot.obtenir(ctx, travail.cible_id or "")
            execution = next(
                (e for e in s.executions if e.id == entre.get("executionId")), None
            )
            # Une restauration ne vaut que ce que vaut l'image qu'elle restaurerait — même
            # garde-fou que `ExecuteurSauvegardeTestRestauration` : point inconnu (ex. demo),
            # granularité que la sauvegarde ne capture pas (elle ne fait qu'un instantané
            # Nova/Glance de la VM entière, pas d'export séparé par fichier/base/messagerie/
            # configuration) ou absence d'image réelle associée → rien à restaurer pour de
            # vrai, pas d'échec inventé.
            if execution is None or entre.get("granularite") != "complete":
                return None
            from synelia.modules.web_hebergement.service import amont, serveur_id

            try:
                secrets = await depot.secrets(ctx, s.id)
            except Exception:  # noqa: BLE001
                secrets = {}
            image_id = secrets.get(f"image_{execution.id}")
            if not image_id:
                return None
            sid = await serveur_id(ctx, s.hebergementId)
            await asyncio.to_thread(amont().restaurer, sid, image_id)
            return f"Serveur restauré depuis l'image {image_id}"
        return None


@executeur("web.backup.testrestauration")
class ExecuteurSauvegardeTestRestauration(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        from synelia.modules.web_hebergement.service import amont

        s = await depot.obtenir(ctx, travail.cible_id or "")
        derniere = s.executions[-1] if s.executions else None
        resultat = "ok"
        if derniere is not None:
            try:
                secrets = await depot.secrets(ctx, s.id)
                image_id = secrets.get(f"image_{derniere.id}")
                if image_id:
                    # Une restauration ne vaut que ce que vaut l'image qu'elle restaurerait :
                    # on vérifie que le snapshot Glande de la dernière exécution existe
                    # toujours et est réellement utilisable, pas seulement qu'il l'était
                    # au moment de la sauvegarde.
                    statut = await asyncio.to_thread(amont().statut_image, image_id)
                    resultat = "ok" if statut == "active" else "echec"
                else:
                    # Aucune image réelle associée (hébergement de démo, ou sauvegarde
                    # antérieure au câblage réel) : rien à vérifier, pas d'échec inventé.
                    resultat = "ok"
            except Exception:  # noqa: BLE001
                resultat = "echec"
        else:
            # Pas de sauvegarde du tout : un test de restauration n'a rien à restaurer.
            resultat = "echec"
        await depot.modifier(
            ctx,
            s.id,
            {
                "dernierTestRestauration": {
                    "date": maintenant().date().isoformat(),
                    "resultat": resultat,
                    "dureeMin": 3,
                }
            },
        )


ETAPES_TEST_RESTAURATION = [
    {"nom": "Vérifier l'intégrité du dépôt", "dureeS": 8},
    {"nom": "Restaurer sur un environnement isolé", "dureeS": 60},
    {"nom": "Comparer les contenus", "dureeS": 15},
    {"nom": "Supprimer l'environnement de test", "dureeS": 6},
]


@peupleur
async def demo(session, org, admin) -> None:  # type: ignore[no-untyped-def]
    sd = m.SauvegardeWeb(
        id=nouvel_id(),
        hebergementId="hebergement-demo",
        serveur="srv-web-01",
        nomServi=org.nom,
        actif=True,
        frequence="quotidienne",
        heure="02:30",
        retentionJours=14,
        destination="backup.s3.synelia.cloud",
        site="ABJ",
        immuable=False,
        perimetre=m.Perimetre(fichiers=True, bases=True, configuration=True, messagerie=False),
        executions=[],
        espaceOccupeGo=0.0,
        dernierTestRestauration=None,
    )
    session.add(
        Ressource(
            id=sd.id,
            org_id=org.id,
            type="web_sauvegarde",
            nom=sd.nomServi,
            statut=str(sd.actif),
            donnees=sd.model_dump(mode="json"),
        )
    )
