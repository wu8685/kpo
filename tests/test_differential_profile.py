from __future__ import annotations

from pathlib import Path

import pytest

from kpo.campaign_profile import (
    campaign_integrity_monitor,
    campaign_profile_snapshot,
    load_campaign_profile,
)
from kpo.integrity import IntegrityViolation
from test_campaign_profile import _campaign_profile
from test_generalization_profile import _generalization_profile
from kpo.campaign_series import build_series_contract, initialize_series


def _append_differential(path: Path) -> Path:
    root = path.parent
    for name in ("extractor", "differ", "diagnostician"):
        executable = root / "bin" / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[differential_analyzer.extractor]
adapter = "command"
command = ["./bin/extractor"]
model_id = "extractor-v1"
timeout_seconds = 10
pass_env = []

[differential_analyzer.differ]
adapter = "command"
command = ["./bin/differ"]
model_id = "differ-v1"
timeout_seconds = 10
pass_env = []

[differential_analyzer.diagnostician]
adapter = "command"
command = ["./bin/diagnostician"]
model_id = "diagnostician-v1"
timeout_seconds = 10
pass_env = []
'''
        )
    return path


def _configured_profile(root: Path) -> Path:
    return _append_differential(_campaign_profile(root))


def test_campaign_profile_loads_all_or_no_differential_providers(
    tmp_path: Path,
) -> None:
    legacy = load_campaign_profile(
        _campaign_profile(tmp_path / "legacy"), checkout=Path(__file__).parents[1]
    )
    configured = load_campaign_profile(
        _configured_profile(tmp_path / "configured"),
        checkout=Path(__file__).parents[1],
    )

    assert legacy.differential_analyzer is None
    assert configured.differential_analyzer is not None
    assert configured.differential_analyzer.extractor.identity == "extractor-v1"
    assert configured.differential_analyzer.differ.identity == "differ-v1"
    assert configured.differential_analyzer.diagnostician.identity == "diagnostician-v1"
    assert campaign_profile_snapshot(legacy) != campaign_profile_snapshot(configured)


@pytest.mark.parametrize("section", ["extractor", "differ", "diagnostician"])
def test_differential_profile_rejects_partial_configuration(
    tmp_path: Path, section: str
) -> None:
    path = _configured_profile(tmp_path / section)
    text = path.read_text(encoding="utf-8")
    start = text.index(f"[differential_analyzer.{section}]")
    next_section = text.find("\n[differential_analyzer.", start + 1)
    if next_section == -1:
        next_section = len(text)
    path.write_text(text[:start] + text[next_section:], encoding="utf-8")

    with pytest.raises(ValueError, match="all three"):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_differential_profile_rejects_unknown_fields(tmp_path: Path) -> None:
    path = _configured_profile(tmp_path / "external")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'model_id = "extractor-v1"',
            'model_id = "extractor-v1"\nunknown = true',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown"):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_differential_executable_is_integrity_bound(tmp_path: Path) -> None:
    profile = load_campaign_profile(
        _configured_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    monitor = campaign_integrity_monitor(profile)

    Path(profile.differential_analyzer.extractor.command[0]).write_text(
        "#!/bin/sh\nexit 1\n", encoding="utf-8"
    )

    with pytest.raises(IntegrityViolation):
        monitor.verify()


def test_differential_and_generalization_contracts_coexist_independently(
    tmp_path: Path,
) -> None:
    path = _append_differential(_generalization_profile(tmp_path / "external"))
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    contract = build_series_contract(profile)
    state = initialize_series(profile, "combined-series")

    assert "differential_analyzer" in contract.components
    assert "generalization" in contract.components
    assert state["protocol"] == "kpo.campaign-series/v2"
    assert state["generalization_state"] == "available"
