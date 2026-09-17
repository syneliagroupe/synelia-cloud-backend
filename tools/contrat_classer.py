"""Classe les échecs d'un rapport JUnit Schemathesis par type et par opération.

Usage : uv run python tools/contrat_classer.py tests-rapports/contrat/junit.xml [--json]
Types : serveur_500, schema_reponse, code_non_declare, requete_invalide_acceptee,
requete_valide_rejetee, methode_non_supportee, en_tete_allow, content_type, en_tetes_reponse,
en_tete_requis_manquant, auth_ignoree, use_after_free, ressource_indisponible, autre.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

TITRES = {
    "Server error": "serveur_500",
    "Response violates schema": "schema_reponse",
    "Undocumented HTTP status code": "code_non_declare",
    "API accepted schema-violating request": "requete_invalide_acceptee",
    "API rejected schema-compliant request": "requete_valide_rejetee",
    "Unsupported methods": "methode_non_supportee",
    "Allow header": "en_tete_allow",
    "Undocumented Content-Type": "content_type",
    "Response headers": "en_tetes_reponse",
    "Missing required header": "en_tete_requis_manquant",
    "Ignored authentication": "auth_ignoree",
    "Use after free": "use_after_free",
    "Resource is not available": "ressource_indisponible",
}


def classer_titre(titre: str) -> str:
    for cle, typ in TITRES.items():
        if titre.lower().startswith(cle.lower()):
            return typ
    return "autre"


def analyser(chemin: str) -> list[dict]:
    racine = ET.parse(chemin).getroot()
    echecs: list[dict] = []
    for tc in racine.iter("testcase"):
        op = tc.attrib.get("name", "?")
        for f in tc.findall("failure"):
            texte = f.text or ""
            # Un bloc par « N. Test Case ID: xxx »
            blocs = re.split(r"\n(?=\d+\. Test Case ID: )", "\n" + texte)
            for bloc in blocs:
                bloc = bloc.strip()
                if not bloc:
                    continue
                m_id = re.match(r"\d+\. Test Case ID: (\S+)", bloc)
                titres = re.findall(r"^- (.+)$", bloc, flags=re.M)
                m_code = re.search(r"^\[(\d{3})\]", bloc, flags=re.M)
                m_curl = re.search(r"^\s*(curl .+)$", bloc, flags=re.M)
                m_detail = re.search(r"^- .+\n\n((?:    .*\n?)+)", bloc, flags=re.M)
                # La méthode réellement envoyée (TRACE, etc.) prime sur l'opération du contrat
                m_meth = re.search(r"curl -X (\w+)", bloc)
                methode_reelle = m_meth.group(1) if m_meth else op.split(" ")[0]
                for titre in titres or ["?"]:
                    echecs.append(
                        {
                            "operation": op,
                            "methode_envoyee": methode_reelle,
                            "type": classer_titre(titre),
                            "titre": titre,
                            "code": int(m_code.group(1)) if m_code else None,
                            "cas": m_id.group(1) if m_id else None,
                            "detail": (m_detail.group(1).strip() if m_detail else "")[:400],
                            "curl": m_curl.group(1).strip() if m_curl else None,
                        }
                    )
    return echecs


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    echecs = analyser(sys.argv[1])
    if "--json" in sys.argv:
        print(json.dumps(echecs, ensure_ascii=False, indent=1))
        return 0
    racine = ET.parse(sys.argv[1]).getroot()
    print(
        f"Opérations testées : {racine.attrib.get('tests')} ; en échec : {racine.attrib.get('failures')} ; "
        f"erreurs : {racine.attrib.get('errors')} ; échecs uniques : {len(echecs)}"
    )
    par_type = Counter(e["type"] for e in echecs)
    print("\nPar type :")
    for typ, n in par_type.most_common():
        print(f"  {n:4d}  {typ}")
    print("\nPar type puis opération (code HTTP obtenu) :")
    groupes: dict[str, list[dict]] = defaultdict(list)
    for e in echecs:
        groupes[e["type"]].append(e)
    for typ, liste in sorted(groupes.items(), key=lambda kv: -len(kv[1])):
        print(f"\n== {typ} ({len(liste)}) ==")
        ops = Counter((e["operation"], e["code"]) for e in liste)
        for (op, code), n in ops.most_common():
            detail = next((e["detail"].splitlines()[0] for e in liste if e["operation"] == op and e["detail"]), "")
            print(f"  {op:<60} -> {code}  x{n}  {detail[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
