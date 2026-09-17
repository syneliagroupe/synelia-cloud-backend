"""Définitions Temporal : le worker doit pouvoir les enregistrer, sans serveur Temporal.

Régression : le workflow était déclaré dans `definitions()`, or le SDK refuse `@workflow.run`
sur une classe locale et le bac à sable réimporte le module qui porte la classe."""

import pytest

pytest.importorskip("temporalio", reason="extra `temporal` non installé")

from temporalio import activity, workflow  # noqa: E402
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner  # noqa: E402

from synelia.travaux import temporal  # noqa: E402


def _definition_workflow():
    wfs, _ = temporal.definitions()
    assert len(wfs) == 1
    return workflow._Definition.must_from_class(wfs[0])  # pas d'introspection publique côté SDK


def test_workflow_et_activite_conservent_leurs_noms():
    wfs, acts = temporal.definitions()
    assert _definition_workflow().name == "TravailWorkflow"
    assert [activity._Definition.must_from_callable(a).name for a in acts] == ["executer_etapes"]
    assert temporal.FILE == "synelia-travaux"
    assert "relancer" in _definition_workflow().signals
    assert wfs[0].__module__ == "synelia.flux_travaux"


def test_workflow_defini_au_niveau_module():
    """Le SDK exige une classe globale : `<locals>` dans le qualname ferait échouer le worker."""
    definition = _definition_workflow()
    assert "<locals>" not in definition.cls.__qualname__
    assert "<locals>" not in definition.run_fn.__qualname__
    module = __import__(definition.cls.__module__, fromlist=["*"])
    assert getattr(module, definition.cls.__name__) is definition.cls


async def test_bac_a_sable_prepare_le_workflow():
    """Le worker prépare chaque workflow dans le bac à sable : le module doit y être importable."""
    SandboxedWorkflowRunner().prepare_workflow(_definition_workflow())
