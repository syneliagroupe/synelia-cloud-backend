"""`Depot[T]` : persistance typée par une classe du contrat, scellée par organisation.

    depot_vms = Depot("vm", Vm)
    vm = await depot_vms.creer(ctx, Vm(...))
    vms, pagination = await depot_vms.lister(ctx, page)

Le module écrit ses routes et ses règles ; le dépôt ne fait que ranger et retrouver."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from synelia_db.modeles import Ressource
from synelia_kernel import erreurs
from synelia_kernel.chiffrement import chiffrer, dechiffrer
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.deps.contexte import Contexte
from synelia.deps.pagination import PageParams, filtrer_trier_paginer


class Depot[T: BaseModel]:
    def __init__(
        self,
        type_: str,
        modele: type[T],
        *,
        plateforme: bool = False,
        libelle: str | None = None,
        champ_nom: str = "nom",
        champ_statut: str = "statut",
        champs_recherche: tuple[str, ...] = ("nom",),
    ) -> None:
        self.type = type_
        self.modele = modele
        self.plateforme = plateforme  # sans org_id : catalogue, backends, offres…
        self.libelle = libelle or type_.capitalize()
        self.champ_nom = champ_nom
        self.champ_statut = champ_statut
        self.champs_recherche = champs_recherche

    # ── lecture ──────────────────────────────────────────────────────────
    def _org(self, ctx: Contexte, org_id: str | None) -> str | None:
        if self.plateforme:
            return None
        return org_id or ctx.org_id

    def _requete(self, ctx: Contexte, org_id: str | None, inclure_supprimes: bool = False):
        q = select(Ressource).where(Ressource.type == self.type)
        if not self.plateforme:
            q = q.where(Ressource.org_id == self._org(ctx, org_id))
        if not inclure_supprimes:
            q = q.where(Ressource.supprime_le.is_(None))
        return q

    def _vers_modele(self, r: Ressource) -> T:
        return self.modele.model_validate(r.donnees)

    async def lignes(
        self,
        ctx: Contexte,
        *,
        org_id: str | None = None,
        parent_id: str | None = None,
        statut: str | None = None,
        inclure_supprimes: bool = False,
    ) -> list[Ressource]:
        q = self._requete(ctx, org_id, inclure_supprimes)
        if parent_id is not None:
            q = q.where(Ressource.parent_id == parent_id)
        if statut is not None:
            q = q.where(Ressource.statut == statut)
        q = q.order_by(Ressource.cree_le.desc())
        return list((await ctx.session.execute(q)).scalars().all())

    async def tous(
        self,
        ctx: Contexte,
        *,
        org_id: str | None = None,
        parent_id: str | None = None,
        statut: str | None = None,
        filtre: Callable[[T], bool] | None = None,
    ) -> list[T]:
        items = [
            self._vers_modele(r)
            for r in await self.lignes(ctx, org_id=org_id, parent_id=parent_id, statut=statut)
        ]
        return [x for x in items if filtre(x)] if filtre else items

    async def lister(
        self,
        ctx: Contexte,
        page: PageParams,
        *,
        org_id: str | None = None,
        parent_id: str | None = None,
        statut: str | None = None,
        filtre: Callable[[T], bool] | None = None,
        tri_defaut: str | None = None,
    ) -> dict[str, Any]:
        """`{ donnees, pagination }` du contrat."""
        items = await self.tous(
            ctx, org_id=org_id, parent_id=parent_id, statut=statut, filtre=filtre
        )
        return filtrer_trier_paginer(
            items, page, champs_recherche=self.champs_recherche, tri_defaut=tri_defaut
        )

    async def compter(
        self, ctx: Contexte, *, org_id: str | None = None, parent_id: str | None = None
    ) -> int:
        q = (
            select(func.count())
            .select_from(Ressource)
            .where(Ressource.type == self.type, Ressource.supprime_le.is_(None))
        )
        if not self.plateforme:
            q = q.where(Ressource.org_id == self._org(ctx, org_id))
        if parent_id is not None:
            q = q.where(Ressource.parent_id == parent_id)
        return int((await ctx.session.execute(q)).scalar_one())

    async def ligne(
        self, ctx: Contexte, id_: str, *, org_id: str | None = None, inclure_supprimes: bool = False
    ) -> Ressource | None:
        q = self._requete(ctx, org_id, inclure_supprimes).where(Ressource.id == id_)
        return (await ctx.session.execute(q)).scalar_one_or_none()

    async def trouver(self, ctx: Contexte, id_: str, *, org_id: str | None = None) -> T | None:
        r = await self.ligne(ctx, id_, org_id=org_id)
        return self._vers_modele(r) if r else None

    async def obtenir(self, ctx: Contexte, id_: str, *, org_id: str | None = None) -> T:
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        return self._vers_modele(r)

    async def par_nom(
        self, ctx: Contexte, nom: str, *, org_id: str | None = None, parent_id: str | None = None
    ) -> T | None:
        q = self._requete(ctx, org_id).where(Ressource.nom == nom)
        if parent_id is not None:
            q = q.where(Ressource.parent_id == parent_id)
        r = (await ctx.session.execute(q)).scalars().first()
        return self._vers_modele(r) if r else None

    async def exiger_nom_libre(
        self, ctx: Contexte, nom: str, *, org_id: str | None = None, parent_id: str | None = None
    ) -> None:
        if await self.par_nom(ctx, nom, org_id=org_id, parent_id=parent_id) is not None:
            raise erreurs.nom_deja_pris(nom)

    # ── écriture ─────────────────────────────────────────────────────────
    def _colonnes(self, modele: T) -> dict[str, Any]:
        d = modele.model_dump(mode="json")
        return {"nom": d.get(self.champ_nom), "statut": d.get(self.champ_statut)}

    async def creer(
        self,
        ctx: Contexte,
        modele: T,
        *,
        org_id: str | None = None,
        parent_id: str | None = None,
        secrets: dict[str, str] | None = None,
        id_: str | None = None,
    ) -> T:
        ident = id_ or getattr(modele, "id", None) or nouvel_id()
        if hasattr(modele, "id") and getattr(modele, "id", None) != ident:
            modele = modele.model_copy(update={"id": ident})
        cols = self._colonnes(modele)
        r = Ressource(
            id=ident,
            org_id=self._org(ctx, org_id),
            type=self.type,
            nom=str(cols["nom"]) if cols["nom"] is not None else None,
            statut=str(cols["statut"]) if cols["statut"] is not None else None,
            parent_id=parent_id,
            cree_par_id=ctx.utilisateur_id,
            donnees=modele.model_dump(mode="json"),
            secrets={k: chiffrer(v) for k, v in (secrets or {}).items()},
        )
        ctx.session.add(r)
        await ctx.session.flush()
        return modele

    async def remplacer(
        self, ctx: Contexte, id_: str, modele: T, *, org_id: str | None = None
    ) -> T:
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        cols = self._colonnes(modele)
        r.donnees = modele.model_dump(mode="json")
        r.nom = str(cols["nom"]) if cols["nom"] is not None else r.nom
        r.statut = str(cols["statut"]) if cols["statut"] is not None else r.statut
        r.modifie_le = maintenant()
        await ctx.session.flush()
        return modele

    async def modifier(
        self,
        ctx: Contexte,
        id_: str,
        changements: dict[str, Any] | BaseModel,
        *,
        org_id: str | None = None,
    ) -> T:
        """Fusion superficielle des champs non nuls d'un `Patch` du contrat, puis revalidation."""
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        patch = (
            changements.model_dump(mode="json", exclude_unset=True)
            if isinstance(changements, BaseModel)
            else changements
        )
        donnees = {**r.donnees, **{k: v for k, v in patch.items() if v is not None}}
        modele = self.modele.model_validate(donnees)
        return await self.remplacer(ctx, id_, modele, org_id=org_id)

    async def definir_statut(
        self, ctx: Contexte, id_: str, statut: str, *, org_id: str | None = None, **autres: Any
    ) -> T:
        return await self.modifier(ctx, id_, {self.champ_statut: statut, **autres}, org_id=org_id)

    async def supprimer(
        self, ctx: Contexte, id_: str, *, org_id: str | None = None, logique: bool = False
    ) -> None:
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        if logique:
            r.supprime_le = maintenant()
        else:
            await ctx.session.delete(r)
        await ctx.session.flush()

    async def supprimer_enfants(
        self, ctx: Contexte, parent_id: str, *, org_id: str | None = None
    ) -> int:
        lignes = await self.lignes(ctx, org_id=org_id, parent_id=parent_id)
        for r in lignes:
            await ctx.session.delete(r)
        await ctx.session.flush()
        return len(lignes)

    # ── secrets (chiffrés, jamais dans `donnees`) ─────────────────────────
    async def secrets(
        self, ctx: Contexte, id_: str, *, org_id: str | None = None
    ) -> dict[str, str]:
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        return {k: dechiffrer(v) for k, v in (r.secrets or {}).items()}

    async def definir_secrets(
        self, ctx: Contexte, id_: str, secrets: dict[str, str], *, org_id: str | None = None
    ) -> None:
        r = await self.ligne(ctx, id_, org_id=org_id)
        if r is None:
            raise erreurs.introuvable(self.libelle, id_)
        r.secrets = {**(r.secrets or {}), **{k: chiffrer(v) for k, v in secrets.items()}}
        await ctx.session.flush()
