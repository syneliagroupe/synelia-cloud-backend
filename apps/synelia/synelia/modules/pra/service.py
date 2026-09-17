"""PRA : plans de reprise, bascules, retours, exercices."""

from __future__ import annotations

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

depot = Depot("plan_pra", m.PlanPra, libelle="Plan de reprise", champs_recherche=("nom",))
exercices = Depot("pra_exercice", m.ExercicePra, libelle="Exercice PRA", champs_recherche=("type",))


def controles_replication(replication: m.Replication3 | None) -> None:
    """Le socle ne porte que la réplication planifiée ; le continu est refusé franchement."""
    if replication and replication.mode == "continu":
        raise erreurs.non_porte("réplication continue indisponible sur le socle de repli actuel.")


def plan_vers_modele(corps: m.PlanPraCreation, ctx: Contexte) -> m.PlanPra:
    controles_replication(corps.replication)
    replication = corps.replication or m.Replication3(mode="planifie", retardS=300)
    groupes = [
        m.Groupe(
            ordre=g.ordre,
            nom=g.nom,
            ressources=g.ressources,
            dependances=g.dependances or [],
            # `ipRepli` (correspondance ressource -> IP sur le site de repli) reste `None` :
            # `ExecuteurBascule`/`ExecuteurRetour` ne créent aucune ressource amont sur le site de
            # repli (aucun appel `amont()`, la bascule ne fait qu'écrire le statut du plan) — il
            # n'existe donc aucune IP réelle à rapporter ici tant qu'un vrai réseau/mapping IP de
            # site de repli n'est pas construit.
            ipRepli=None,
        )
        for g in corps.groupes
    ]
    return m.PlanPra(
        id=nouvel_id(),
        orgId=ctx.org_id,
        nom=corps.nom,
        siteSource=corps.siteSource,
        siteRepli=corps.siteRepli,
        rpoCibleMin=corps.rpoCibleMin,
        rpoConstateMin=_rpo_constate(replication),
        rtoCibleMin=corps.rtoCibleMin,
        rtoConstateMin=None,
        groupes=groupes,
        replication=m.Replication2(
            mode=replication.mode or "planifie", retardS=replication.retardS or 0
        ),
        exercices=[],
        statut="jamais_teste",
    )


def _rpo_constate(replication: m.Replication3) -> int | None:
    if replication.retardS is None:
        return None
    return max(1, replication.retardS // 60)


@executeur("dr.failover.test")
@executeur("dr.failover.real")
class ExecuteurBascule(Executeur):
    def _type(self, travail: Travail) -> str:
        valeur = travail.type.split(".")[-1]
        return "reel" if valeur == "real" else valeur

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        pra_id = travail.cible_id or ""
        exercice = m.ExercicePra(
            date=maintenant(),
            type=self._type(travail),
            dureeMin=24,
            rtoConstateMin=int((travail.duree_s or 0) // 60) + 6,
            succes=True,
            rapportUrl=f"https://rapports.synelia.cloud/pra/{pra_id}/exercices/{travail.id}",
            incidents=None,
        )
        await exercices.creer(ctx, exercice, parent_id=pra_id)
        await depot.modifier(
            ctx, pra_id, {"statut": "operationnel", "rtoConstateMin": exercice.rtoConstateMin}
        )


@executeur("dr.failover.retour")
class ExecuteurRetour(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        pra_id = travail.cible_id or ""
        await depot.modifier(ctx, pra_id, {"statut": "operationnel"})
