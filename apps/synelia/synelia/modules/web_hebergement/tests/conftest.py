"""Lab réel : libère le tenant zone VPS avant les tests hébergement (Nova NoValidHost)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from synelia_testing import sur_lab_reel


@pytest.fixture(scope="session", autouse=True)
def _epurer_tenant_zone_vps_avant_hebergement():
    if not sur_lab_reel():
        return
    racine = Path(__file__).resolve().parents[6]
    script = racine / "scripts" / "lab" / "prune-vps-zone-project-vms.py"
    if not script.is_file():
        return
    subprocess.run(
        [sys.executable, str(script), "--hebergement-test"],
        cwd=racine,
        check=False,
        timeout=600,
    )
