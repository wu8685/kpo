from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign_series_profile import _series_profile
from test_generalization_manifest import _append_generalization_case, _manifest

from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import (
    build_series_contract,
    initialize_series,
    load_initial_policy_snapshot,
    load_series_state,
)


def _generalization_profile(root: Path, *, max_provider_calls: int = 40) -> Path:
    path = _series_profile(root)
    _append_generalization_case(root)
    _manifest(root)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            f'''

[series.generalization]
manifest = "./generalization.jsonl"
max_provider_calls = {max_provider_calls}
'''
        )
    return path


def test_profile_loads_optional_generalization_configuration(tmp_path: Path) -> None:
    path = _generalization_profile(tmp_path / "external")

    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert profile.generalization is not None
    assert profile.generalization.max_provider_calls == 40
    assert profile.generalization.manifest.digest
    assert profile.generalization.manifest.path == path.parent / "generalization.jsonl"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("max_provider_calls = 0", "max_provider_calls"),
        (
            "max_provider_calls = 40\nunknown = true",
            "unknown series.generalization fields",
        ),
        (
            'manifest = "../outside.jsonl"',
            "series.generalization.manifest",
        ),
    ],
)
def test_profile_rejects_invalid_generalization_configuration(
    tmp_path: Path, replacement: str, message: str
) -> None:
    path = _generalization_profile(tmp_path / "external")
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("manifest"):
        text = text.replace('manifest = "./generalization.jsonl"', replacement)
        (path.parent.parent / "outside.jsonl").write_text("{}\n", encoding="utf-8")
    else:
        text = text.replace("max_provider_calls = 40", replacement)
    path.write_text(text, encoding="utf-8")

    with pytest.raises((ValueError, FileNotFoundError), match=message):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_profile_without_generalization_preserves_v1_series_contract_and_state(
    tmp_path: Path,
) -> None:
    path = _series_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    contract = build_series_contract(profile)
    state = initialize_series(profile, "series-v1")

    assert profile.generalization is None
    assert "generalization" not in contract.components
    assert state["protocol"] == "kpo.campaign-series/v1"
    assert set(state) == {
        "protocol",
        "series_id",
        "profile_id",
        "state",
        "contract",
        "series_contract_digest",
        "initial_policy_digest",
        "holdout_digest",
        "max_campaigns",
        "stopping",
        "entries",
        "state_digest",
    }
    assert not (
        profile.base.data_home / "series" / "series-v1" / "initial-policy.json"
    ).exists()


def test_generalization_contract_is_safe_and_binds_content(tmp_path: Path) -> None:
    path = _generalization_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    contract = build_series_contract(profile)
    serialized = json.dumps(contract.components, sort_keys=True)

    assert contract.components["generalization"] == {
        "digest": profile.generalization.manifest.digest,
        "case_count": 1,
        "max_provider_calls": 40,
        "manifest_coordinate_digest": contract.components["generalization"][
            "manifest_coordinate_digest"
        ],
    }
    assert "future-1" not in serialized
    assert "future-only" not in serialized
    assert str(path.parent) not in serialized

    manifest_path = path.parent / "generalization.jsonl"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("synthetic:v1", "synthetic:v2"),
        encoding="utf-8",
    )
    changed = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    assert build_series_contract(changed) != contract


def test_v2_series_snapshots_initial_policy_and_detects_tampering(
    tmp_path: Path,
) -> None:
    path = _generalization_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    state = initialize_series(profile, "series-v2")
    snapshot_path = (
        profile.base.data_home / "series" / "series-v2" / "initial-policy.json"
    )
    snapshot = load_initial_policy_snapshot(profile, "series-v2", state)

    assert state["protocol"] == "kpo.campaign-series/v2"
    assert state["generalization_digest"] == profile.generalization.manifest.digest
    assert state["generalization_case_count"] == 1
    assert state["generalization_max_provider_calls"] == 40
    assert state["generalization_state"] == "available"
    assert state["initial_policy_snapshot_digest"]
    assert snapshot.digest == profile.base.policy.digest
    assert snapshot_path.is_file()

    snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="initial policy snapshot digest"):
        load_series_state(profile, "series-v2")


def test_v2_series_rejects_generalization_source_drift(tmp_path: Path) -> None:
    path = _generalization_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-v2")

    manifest_path = path.parent / "generalization.jsonl"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("synthetic:v1", "synthetic:v2"),
        encoding="utf-8",
    )
    changed = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    with pytest.raises(ValueError, match="series contract mismatch"):
        load_series_state(changed, "series-v2")
