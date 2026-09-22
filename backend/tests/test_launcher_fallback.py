from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "start-dev.ps1"
MAIN = ROOT / "backend" / "app" / "main.py"


def test_launcher_falls_back_without_overwriting_runtime_configuration() -> None:
    source = LAUNCHER.read_text(encoding="utf-8-sig")

    assert "function Start-DockerDesktop" in source
    assert "$executionMode = 'local'" in source
    assert "$env:AGENT_TEAM_EXECUTION_MODE = 'local'" in source
    assert "$env:REDIS_URL = ''" in source
    assert "AGENT_TEAM_REDIS_REQUIRED" in source
    assert "Set-Content -LiteralPath $runtimeEnvFile" not in source


def test_local_mode_does_not_construct_a_redis_coordinator() -> None:
    source = MAIN.read_text(encoding="utf-8")

    assert (
        'run_coordinator = create_run_coordinator_from_env() '
        'if execution_mode == "redis" else None'
    ) in source
