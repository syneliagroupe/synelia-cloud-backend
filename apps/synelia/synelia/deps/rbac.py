"""`exige("vm.create_delete")` : refuse en `403` avec `rolesRequis` et journalise le refus."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends
from synelia_contract import rbac
from synelia_kernel import erreurs

from synelia.audit import journaliser
from synelia.deps.contexte import Contexte, contexte


async def _refuser(ctx: Contexte, action: str, message: str) -> None:
    await journaliser(
        ctx,
        action="rbac.refus",
        cible_type="action",
        cible_id=action,
        resultat="refus",
        details={"role": ctx.role, "message": message},
    )
    raise erreurs.interdit(message, roles_requis=rbac.roles_requis(action))


def exige(
    action: str | None, *, lecture: bool = False
) -> Callable[..., Coroutine[Any, Any, Contexte]]:
    """Dépendance : le principal doit pouvoir exécuter `action` (ou la lire si `lecture`).

    Une clé d'API ne peut jamais dépasser sa portée déclarée."""

    async def _dep(ctx: Contexte = Depends(contexte)) -> Contexte:
        if action is None:
            return ctx
        p = ctx.principal
        assert p is not None
        if p.cle_api_id and p.portee and action not in p.portee:
            await _refuser(ctx, action, "Cette clé d'API n'a pas cette action dans sa portée.")
        if p.est_admin_plateforme and p.org_id:
            return ctx
        if not rbac.autorise(p.role, action, lecture=lecture):
            await _refuser(ctx, action, rbac.message_refus(action))
        return ctx

    return _dep


def exige_admin(action: str | None = None) -> Callable[..., Coroutine[Any, Any, Contexte]]:
    """`/admin/**` : réservé aux rôles `super_admin` et `platform_operator`."""

    async def _dep(ctx: Contexte = Depends(contexte)) -> Contexte:
        p = ctx.principal
        assert p is not None
        if not p.est_admin_plateforme:
            await _refuser(ctx, action or "admin", "Espace réservé à l'équipe Synelia.")
        if p.cle_api_id and p.portee and action and action not in p.portee:
            await _refuser(ctx, action, "Ce jeton n'a pas cette action dans sa portée.")
        if action and not rbac.autorise(p.role_equipe or p.role, action, lecture=True):
            await _refuser(ctx, action, rbac.message_refus(action))
        return ctx

    return _dep
