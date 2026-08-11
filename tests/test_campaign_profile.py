from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign_profile import load_campaign_profile, resolve_candidate_destination
from kpo.models import CandidatePatch, MutationKind
from test_external_cli import _operational_profile


def _campaign_profile(root: Path) -> Path:
    profile = _operational_profile(root)
    proposer = root / "bin" / "proposer"
    proposer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proposer.chmod(0o755)
    rows = [
        {
            "case_id": "case-1",
            "partition": partition,
            "weight": 1.0,
            "provenance": [f"source-{partition}"],
        }
        for partition in ("train", "validation", "holdout", "regression")
    ]
    # The base v0.2 fixture has one case; create distinct cases for all partitions.
    case_rows = []
    reference_rows = []
    for index, row in enumerate(rows):
        case_id = f"case-{index + 1}"
        reference_id = f"ref-{index + 1}"
        row["case_id"] = case_id
        case_rows.append(
            {
                "case_id": case_id,
                "prompt": f"Analyze scenario {index}.",
                "visible_context": [f"Visible fact {index}."],
                "hidden_reference_ids": [reference_id],
                "tags": [row["partition"]],
                "information_cutoff": None,
            }
        )
        reference_path = root / "references" / f"{reference_id}.md"
        reference_path.write_text(f"Expected {index}.\n", encoding="utf-8")
        reference_rows.append(
            {"artifact_id": reference_id, "kind": "expected", "path": f"references/{reference_id}.md"}
        )
    (root / "cases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in case_rows), encoding="utf-8"
    )
    (root / "references.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in reference_rows), encoding="utf-8"
    )
    (root / "dataset.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    with profile.open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[dataset]
manifest = "./dataset.jsonl"

[proposer]
adapter = "command"
command = ["./bin/proposer"]
model_id = "proposer-model"
timeout_seconds = 10
pass_env = []

[campaign]
max_iterations = 2
max_provider_calls = 100

[campaign.gates.validation]
alignment = 0.1
[campaign.gates.holdout]
alignment = 0.0
[campaign.gates.regression]
alignment = 0.0
[campaign.gates.ablation]
alignment = 0.1

[promotion]
target_root = "."
allowlist = ["policy", "policies"]
add_directory = "policies"
'''
        )
    return profile


def test_campaign_profile_loads_dataset_gates_and_promotion_paths(tmp_path: Path) -> None:
    path = _campaign_profile(tmp_path / "external")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    assert len(profile.dataset.entries) == 4
    assert profile.max_iterations == 2
    assert profile.gates.validation == {"alignment": 0.1}
    assert profile.policy_paths["base"] == "policy/base.md"
    assert profile.add_directory == "policies"
    assert profile.sandbox_type == "none"


def test_campaign_profile_requires_complete_gate_dimensions_and_add_directory(
    tmp_path: Path,
) -> None:
    path = _campaign_profile(tmp_path / "external")
    text = path.read_text(encoding="utf-8").replace(
        'add_directory = "policies"\n', ""
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="add_directory"):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_candidate_destination_is_deterministic_and_safe(tmp_path: Path) -> None:
    profile = load_campaign_profile(
        _campaign_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    add = CandidatePatch("p", "d", MutationKind.ADD, "new-rule", "content", ("x",), "c")
    edit = CandidatePatch("p", "d", MutationKind.EDIT, "base", "content", ("x",), "c")
    unsafe = CandidatePatch("p", "d", MutationKind.ADD, "../escape", "content", ("x",), "c")

    assert resolve_candidate_destination(profile, add).target_relative_path == "policies/new-rule.md"
    assert resolve_candidate_destination(profile, add).manifest_relative_path == "policies/new-rule.md"
    assert resolve_candidate_destination(profile, edit).target_relative_path == "policy/base.md"
    assert resolve_candidate_destination(profile, edit).manifest_relative_path == "policy/base.md"
    with pytest.raises(ValueError, match="artifact_id"):
        resolve_candidate_destination(profile, unsafe)


def test_candidate_destination_maps_nested_target_coordinates(tmp_path: Path) -> None:
    path = _campaign_profile(tmp_path / "external")
    nested = path.parent / "knowledge"
    nested.mkdir()
    text = path.read_text(encoding="utf-8").replace(
        'target_root = "."', 'target_root = "./knowledge"'
    )
    path.write_text(text, encoding="utf-8")
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    add = CandidatePatch("p", "d", MutationKind.ADD, "new-rule", "content", ("x",), "c")

    destination = resolve_candidate_destination(profile, add)

    assert destination.target_relative_path == "policies/new-rule.md"
    assert destination.manifest_relative_path == "knowledge/policies/new-rule.md"
    assert (profile.target_root / destination.target_relative_path).resolve() == (
        profile.base.path.parent / destination.manifest_relative_path
    ).resolve()


def test_campaign_profile_rejects_target_outside_profile_or_symlink_escape(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_path = _campaign_profile(tmp_path / "outside-profile")
    outside_path.write_text(
        outside_path.read_text(encoding="utf-8").replace(
            'target_root = "."', f'target_root = "{outside}"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="inside profile"):
        load_campaign_profile(outside_path, checkout=Path(__file__).parents[1])

    symlink_path = _campaign_profile(tmp_path / "symlink-profile")
    link = symlink_path.parent / "linked-target"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    symlink_path.write_text(
        symlink_path.read_text(encoding="utf-8").replace(
            'target_root = "."', 'target_root = "./linked-target"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="inside profile"):
        load_campaign_profile(symlink_path, checkout=Path(__file__).parents[1])


def test_edit_candidate_must_resolve_below_target_root(tmp_path: Path) -> None:
    path = _campaign_profile(tmp_path / "external")
    nested = path.parent / "knowledge"
    nested.mkdir()
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'target_root = "."', 'target_root = "./knowledge"'
        ),
        encoding="utf-8",
    )
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    edit = CandidatePatch("p", "d", MutationKind.EDIT, "base", "content", ("x",), "c")

    with pytest.raises(ValueError, match="below promotion target_root"):
        resolve_candidate_destination(profile, edit)


def test_unavailable_hard_sandbox_is_rejected(tmp_path: Path) -> None:
    path = _campaign_profile(tmp_path / "external")
    with path.open("a", encoding="utf-8") as stream:
        stream.write('\n[sandbox]\ntype = "container"\n')
    with pytest.raises(ValueError, match="sandbox.*unavailable"):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])
