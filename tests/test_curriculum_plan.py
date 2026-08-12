from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import initialize_series
from kpo.curriculum import apply_curriculum, preview_curriculum
from kpo.dataset import DatasetManager, load_dataset
from kpo.integrity import IntegrityViolation
from kpo.models import Partition
from test_curriculum_profile import _configured_profile
from test_curriculum_signal import _attach_signal_campaign


def _ready_profile(tmp_path: Path) -> object:
    profile = load_campaign_profile(
        _configured_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    manager = DatasetManager(profile.base.data_home)
    holdout = manager.preview_initialize(profile.dataset)
    manager.apply_initialize(holdout, approval_digest=holdout.approval_digest)
    initialize_series(profile, "series-a")
    _attach_signal_campaign(
        profile,
        series_id="series-a",
        campaign_id="campaign-a",
        index=0,
        kind="missing",
    )
    return profile


def test_curriculum_preview_is_private_immutable_and_non_mutating(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    original = profile.dataset_path.read_bytes()

    preview = preview_curriculum(profile, "series-a")

    assert profile.dataset_path.read_bytes() == original
    assert preview.plan.protocol == "kpo.curriculum-selection/v1"
    assert preview.plan.selected_case_ids == ("candidate-1",)
    assert preview.plan.selected_gap_coverage == {"missing": 1}
    assert preview.plan.approval_digest
    assert preview.plan.growth_approval_digest
    assert preview.selection_path.is_file()
    assert preview.proposed_path.is_file()
    persisted = json.loads(preview.selection_path.read_text(encoding="utf-8"))
    assert persisted["approval_digest"] == preview.plan.approval_digest
    assert "campaign-a" not in preview.selection_path.read_text(encoding="utf-8")

    proposed = load_dataset(preview.proposed_path, profile.base.cases)
    assert "candidate-1" in {
        entry.case_id for entry in proposed.by_partition[Partition.TRAIN]
    }
    repeated = preview_curriculum(profile, "series-a")
    assert repeated.plan == preview.plan
    assert repeated.selection_path == preview.selection_path


def test_curriculum_apply_requires_exact_approval_and_commits_train_growth(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")

    with pytest.raises(ValueError, match="approval"):
        apply_curriculum(profile, "series-a", approval_digest="0" * 64)

    result = apply_curriculum(
        profile,
        "series-a",
        approval_digest=preview.plan.approval_digest,
    )

    assert result.state == "committed"
    updated = load_dataset(profile.dataset_path, profile.base.cases)
    assert {entry.case_id for entry in updated.entries} == {
        "case-1",
        "case-2",
        "case-3",
        "case-4",
        "candidate-1",
    }
    assert "candidate-1" in {
        entry.case_id for entry in updated.by_partition[Partition.TRAIN]
    }
    assert updated.holdout_digest == profile.dataset.holdout_digest


def test_curriculum_apply_rejects_stale_inbox_bytes(tmp_path: Path) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")
    assert profile.curriculum is not None
    with profile.curriculum.inbox.open("a", encoding="utf-8") as stream:
        stream.write("\n")

    with pytest.raises(ValueError, match="stale|approval"):
        apply_curriculum(
            profile,
            "series-a",
            approval_digest=preview.plan.approval_digest,
        )


def test_curriculum_apply_rejects_profile_source_drift(tmp_path: Path) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")
    reference = profile.base.path.parent / "references" / "ref-1.md"
    reference.write_text("Changed after preview.\n", encoding="utf-8")

    with pytest.raises(IntegrityViolation, match="changed"):
        apply_curriculum(
            profile,
            "series-a",
            approval_digest=preview.plan.approval_digest,
        )


def test_curriculum_approval_cannot_escape_preview_root(tmp_path: Path) -> None:
    profile = _ready_profile(tmp_path)
    with pytest.raises(ValueError, match="approval digest"):
        apply_curriculum(
            profile,
            "series-a",
            approval_digest="../outside",
        )


def test_curriculum_runtime_symlink_cannot_escape_data_home(tmp_path: Path) -> None:
    profile = _ready_profile(tmp_path)
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    curriculum_root = profile.base.data_home / "curriculum"
    try:
        curriculum_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="runtime.*escapes"):
        preview_curriculum(profile, "series-a")
