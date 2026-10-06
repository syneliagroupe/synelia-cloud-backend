from synelia.modules.vms.cloudinit import _demo


def test_cloudinit_plateforme_et_utilisateur():
    _demo()


async def test_os_lisible_traduit_l_uuid_glance(monkeypatch):
    import importlib

    r = importlib.import_module("synelia.modules.vms.router")
    from synelia_contract import modeles as m

    class Amont:
        def images(self):
            return [{"id": "816f25d1-73ec-45f3-b199-2df4440b0c55", "nom": "ubuntu-24.04"}]

    monkeypatch.setattr(r, "amont", lambda: Amont())
    vm = m.Vm.model_construct(os="816f25d1-73ec-45f3-b199-2df4440b0c55")
    autre = m.Vm.model_construct(os="debian-12")
    sortie = await r._os_lisible([vm, autre])
    assert [v.os for v in sortie] == ["ubuntu-24.04", "debian-12"]
