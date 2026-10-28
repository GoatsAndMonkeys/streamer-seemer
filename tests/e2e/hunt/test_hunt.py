"""pytest entry for the Sendspin bug hunt (slow; skipped unless MA_REPO is set).

Each test runs one scenario from ``scenarios.py`` in MA's interpreter and asserts the
bug is absent. Bugs that reproduce on MA dev cdff463ec are marked ``xfail(strict=True)``,
so a fix turns them into an XPASS failure that asks for the mark to be dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MA_REPO = os.environ.get("MA_REPO")

pytestmark = pytest.mark.skipif(not MA_REPO, reason="set MA_REPO to a Music Assistant tree")

# scenario -> issue / quirk the xfail stands for (reproduced 3/3 on MA dev cdff463ec)
REPRODUCED: dict[str, str] = {
    "ss26_shutdown_manual_ip": "#6888 / SS26: no redial after goodbye 'shutdown'",
    "ss36_available_never": "SS36: MA ignores client/state available:false",
    "ss56_offline_redial_gap": "SS56: manual-IP backoff keeps a returning device out ~2 min",
}

NOT_REPRODUCED = [
    "ss26_shutdown_mdns_update",
    "ss26_shutdown_mdns_readd",
    "ss27_another_server",
    "ss24_volume_steps_echo",
    "ss44_group_volume_unmutes",
    "ss29_blackhole_group",
    "ss55_idle_60s_watchdog",
    "ss53_mixed_g0_g2_sync",
    "ss30_wifi_stall_group",
    "ma_restart_players_return",
    "ss28_hung_then_recovers",
    "ss35_volume_latency_big_buffer",
    "ss48_pcm24_track_end",
    "ss5_opus_plain_esp32",
]


def _run(name: str, timeout: float = 600) -> dict:
    assert MA_REPO is not None
    python = os.environ.get("MA_PYTHON") or str(Path(MA_REPO) / ".venv" / "bin" / "python")
    env = dict(os.environ, PYTHONPATH=f"{MA_REPO}{os.pathsep}{REPO}")
    proc = subprocess.run(
        [python, str(HERE / "scenarios.py"), name],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"{name}: no result\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1].removeprefix("RESULT "))


def _params() -> list:
    out = []
    for name, why in REPRODUCED.items():
        out.append(pytest.param(name, marks=pytest.mark.xfail(reason=why, strict=True)))
    out.extend(NOT_REPRODUCED)
    return out


@pytest.mark.parametrize("name", _params())
def test_hunt_scenario(name: str) -> None:
    r = _run(name)
    assert "error" not in r, r
    assert r["valid"], ("the scenario's control did not work", r)
    assert not r["bug_present"], r
