from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Depends, Query
from synelia_contract.modeles import Pagination

T = TypeVar("T")


@dataclass
class PageParams:
    page: int = 1
    par_page: int = 20
    tri: str | None = None
    ordre: Literal["asc", "desc"] = "asc"
    q: str | None = None

    @property
    def debut(self) -> int:
        return (self.page - 1) * self.par_page


def _page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    parPage: Annotated[int, Query(ge=1, le=200)] = 20,  # noqa: N803
    tri: str | None = None,
    ordre: Literal["asc", "desc"] = "asc",
    q: str | None = None,
) -> PageParams:
    return PageParams(page=page, par_page=parPage, tri=tri, ordre=ordre, q=q)


Page = Annotated[PageParams, Depends(_page_params)]


def pagine(elements: list[Any], total: int, p: PageParams) -> dict[str, Any]:
    return {
        "donnees": elements,
        "pagination": Pagination(
            page=p.page,
            parPage=p.par_page,
            total=total,
            totalPages=max(1, math.ceil(total / p.par_page)) if total else 0,
        ),
    }


def _valeur_tri(x: Any, cle: str) -> Any:
    v = x.get(cle) if isinstance(x, dict) else getattr(x, cle, None)
    return (v is None, v if v is not None else "")


def filtrer_trier_paginer(
    elements: list[Any],
    p: PageParams,
    *,
    champs_recherche: tuple[str, ...] = ("nom",),
    tri_defaut: str | None = None,
    ordre_defaut: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    """Recherche `q` (insensible à la casse), tri, pagination en mémoire sur une liste déjà scellée à l'org.

    `ordre_defaut` ne joue que quand `p.tri` n'est pas fourni (on trie alors sur
    `tri_defaut`) : un client qui précise `tri` garde la main sur `ordre`. Sans ça, une
    liste triée par défaut sur un champ de récence (`derniereActivite`…) retombait
    silencieusement en ordre croissant — la session courante, la plus récente,
    disparaissait derrière des centaines de sessions expirées plus anciennes dès que le
    total dépassait une page.
    """
    if p.q:
        q = p.q.lower()

        def _correspond(x: Any) -> bool:
            for c in champs_recherche:
                v = x.get(c) if isinstance(x, dict) else getattr(x, c, None)
                if v is not None and q in str(v).lower():
                    return True
            return False

        elements = [x for x in elements if _correspond(x)]
    cle = p.tri or tri_defaut
    if cle:
        ordre = p.ordre if p.tri else ordre_defaut
        try:
            elements = sorted(
                elements, key=lambda x: _valeur_tri(x, cle), reverse=(ordre == "desc")
            )
        except TypeError:
            pass
    total = len(elements)
    return pagine(elements[p.debut : p.debut + p.par_page], total, p)
