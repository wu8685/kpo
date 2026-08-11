from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign_profile import _campaign_profile

from kpo.campaign_profile import campaign_profile_snapshot, load_campaign_profile
from kpo.campaign_series import build_series_contract


def _series_profile(root: Path) -> Path:
    path = _campaign_profile(root)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            """

[series]
max_campaigns = 6

[series.stopping]
min_improvement = 0.01
patience = 2
holdout_degradation = 0.05
"""
        )
    return path


def test_profile_without_series_preserves_existing_snapshot_shape(tmp_path: Path) -> None:
    path = _campaign_profile(tmp_path / "external")
    before = campaign_profile_snapshot(
        load_campaign_profile(path, checkout=Path(__file__).parents[1])
    )

    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert profile.series is None
    assert campaign_profile_snapshot(profile) == before


def test_series_profile_parses_strict_stopping_configuration(tmp_path: Path) -> None:
    profile = load_campaign_profile(
        _series_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )

    assert profile.series is not None
    assert profile.series.max_campaigns == 6
    assert profile.series.min_improvement == 0.01
    assert profile.series.patience == 2
    assert profile.series.holdout_degradation == 0.05


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("max_campaigns = 0", "max_campaigns"),
        ("max_campaigns = 6\nunknown = true", "unknown series fields"),
        ("min_improvement = 0.01\npatience = 0", "patience"),
        ("min_improvement = 1.1\npatience = 2", "min_improvement"),
        ("min_improvement = 0.01", "min_improvement.*patience"),
        ("patience = 2", "min_improvement.*patience"),
        (
            "min_improvement = 0.01\npatience = 2\nholdout_degradation = -0.1",
            "holdout_degradation",
        ),
        (
            "min_improvement = 0.01\npatience = 2\nextra = 1",
            "unknown series.stopping fields",
        ),
    ],
)
def test_series_profile_rejects_malformed_configuration(
    tmp_path: Path, replacement: str, message: str
) -> None:
    path = _series_profile(tmp_path / "external")
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("max_campaigns"):
        text = text.replace("max_campaigns = 6", replacement)
    else:
        original = (
            "min_improvement = 0.01\n"
            "patience = 2\n"
            "holdout_degradation = 0.05"
        )
        text = text.replace(original, replacement)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_series_stopping_section_is_optional(tmp_path: Path) -> None:
    path = _series_profile(tmp_path / "external")
    text = path.read_text(encoding="utf-8")
    text = text[: text.index("[series.stopping]")]
    path.write_text(text, encoding="utf-8")

    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert profile.series is not None
    assert profile.series.max_campaigns == 6
    assert profile.series.min_improvement is None
    assert profile.series.patience is None
    assert profile.series.holdout_degradation is None


def test_series_contract_contains_only_safe_components(tmp_path: Path) -> None:
    profile = load_campaign_profile(
        _series_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )

    contract = build_series_contract(profile)
    serialized = json.dumps(contract.components, sort_keys=True)

    assert contract.digest
    assert contract.components["profile_id"] == profile.base.profile_id
    assert contract.components["holdout_digest"] == profile.dataset.holdout_digest
    assert str(tmp_path) not in serialized
    assert str(profile.base.path) not in serialized
    assert "provider-work" not in serialized
    assert "PATH" not in serialized


def test_series_contract_is_independent_of_profile_and_data_home_location(
    tmp_path: Path,
) -> None:
    first = load_campaign_profile(
        _series_profile(tmp_path / "first"),
        checkout=Path(__file__).parents[1],
    )
    second = load_campaign_profile(
        _series_profile(tmp_path / "nested" / "second"),
        checkout=Path(__file__).parents[1],
    )

    assert first.base.data_home != second.base.data_home
    assert first.base.actor.command[0] != second.base.actor.command[0]
    assert build_series_contract(first) == build_series_contract(second)


def test_series_contract_allows_policy_and_non_holdout_dataset_growth(
    tmp_path: Path,
) -> None:
    path = _series_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    baseline = build_series_contract(profile)

    policy_path = path.parent / "policy" / "base.md"
    policy_path.write_text("Changed through an approved promotion.\n", encoding="utf-8")
    case_rows = [
        json.loads(line)
        for line in (path.parent / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    case_rows[0]["prompt"] = "Expanded train scenario."
    (path.parent / "cases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in case_rows), encoding="utf-8"
    )

    changed = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert changed.dataset.digest != profile.dataset.digest
    assert changed.dataset.holdout_digest == profile.dataset.holdout_digest
    assert build_series_contract(changed) == baseline


@pytest.mark.parametrize("change", ["gate", "rubric", "executable", "holdout", "series"])
def test_series_contract_rejects_semantic_drift(tmp_path: Path, change: str) -> None:
    path = _series_profile(tmp_path / change)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    baseline = build_series_contract(profile)

    if change == "gate":
        path.write_text(
            path.read_text(encoding="utf-8").replace("alignment = 0.1", "alignment = 0.2", 1),
            encoding="utf-8",
        )
    elif change == "rubric":
        rubric_path = path.parent / "rubric.json"
        rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
        rubric["dimensions"][0]["description"] += " changed"
        rubric_path.write_text(json.dumps(rubric), encoding="utf-8")
    elif change == "executable":
        Path(profile.base.actor.command[0]).write_text(
            "#!/bin/sh\nexit 9\n", encoding="utf-8"
        )
    elif change == "holdout":
        case_rows = [
            json.loads(line)
            for line in (path.parent / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        case_rows[2]["prompt"] = "Changed frozen holdout scenario."
        (path.parent / "cases.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in case_rows), encoding="utf-8"
        )
    else:
        path.write_text(
            path.read_text(encoding="utf-8").replace("max_campaigns = 6", "max_campaigns = 7"),
            encoding="utf-8",
        )

    changed = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert build_series_contract(changed) != baseline
