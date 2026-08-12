from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign_profile import campaign_profile_snapshot, load_campaign_profile
from kpo.campaign_series import build_series_contract
from test_campaign_profile import _campaign_profile
from test_differential_profile import _append_differential


def _append_curriculum(path: Path) -> Path:
    case_path = path.parent / "cases.jsonl"
    with case_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "case_id": "candidate-1",
                    "prompt": "Analyze a novel candidate scenario.",
                    "visible_context": ["Novel candidate evidence."],
                    "hidden_reference_ids": ["ref-1"],
                    "tags": ["candidate"],
                    "information_cutoff": None,
                }
            )
            + "\n"
        )
    (path.parent / "candidate-inbox.jsonl").write_text(
        json.dumps(
            {
                "case_id": "candidate-1",
                "gap_kinds": ["missing"],
                "facets": ["boundary"],
                "provenance": ["source:candidate-1"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[series]
max_campaigns = 4

[curriculum]
inbox = "./candidate-inbox.jsonl"
max_selection = 2
diversity_weight = 0.25
allowed_facets = ["boundary", "causal-order"]
'''
        )
    return path


def _configured_profile(root: Path) -> Path:
    return _append_curriculum(_append_differential(_campaign_profile(root)))


def test_curriculum_profile_loads_and_binds_stable_configuration(
    tmp_path: Path,
) -> None:
    path = _configured_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    snapshot = campaign_profile_snapshot(profile)
    contract = build_series_contract(profile)

    assert profile.curriculum is not None
    assert profile.curriculum.inbox.name == "candidate-inbox.jsonl"
    assert profile.curriculum.max_selection == 2
    assert profile.curriculum.diversity_weight == 0.25
    assert profile.curriculum.allowed_facets == ("boundary", "causal-order")
    assert contract.components["curriculum"]["protocol"] == (
        "kpo.curriculum-selection/v1"
    )

    # Mutable inbox bytes are plan-bound, not stable-contract-bound.
    with profile.curriculum.inbox.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    reloaded = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    assert campaign_profile_snapshot(reloaded) == snapshot
    assert build_series_contract(reloaded).digest == contract.digest


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda text: text.replace("[series]\nmax_campaigns = 4\n\n", ""),
            "requires series",
        ),
        (
            lambda text: text.replace(
                'allowed_facets = ["boundary", "causal-order"]',
                'allowed_facets = ["Boundary", "causal-order"]',
            ),
            "normalized",
        ),
        (
            lambda text: text.replace("max_selection = 2", "max_selection = 0"),
            "positive integer",
        ),
    ],
)
def test_curriculum_profile_rejects_invalid_configuration(
    tmp_path: Path, mutation: object, message: str
) -> None:
    path = _configured_profile(tmp_path / message.replace(" ", "-"))
    path.write_text(mutation(path.read_text(encoding="utf-8")), encoding="utf-8")  # type: ignore[operator]

    with pytest.raises(ValueError, match=message):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_curriculum_profile_requires_differential_analyzer(tmp_path: Path) -> None:
    path = _append_curriculum(_campaign_profile(tmp_path / "no-differential"))
    with pytest.raises(ValueError, match="requires differential_analyzer"):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_unconfigured_profile_preserves_absent_curriculum_component(
    tmp_path: Path,
) -> None:
    path = _campaign_profile(tmp_path / "legacy")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert profile.curriculum is None
