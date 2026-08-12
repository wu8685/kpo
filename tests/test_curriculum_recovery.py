from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign_series import _recover_stale_series_lock, load_series_state
from kpo.curriculum import preview_curriculum, recover_curriculum
from kpo.dataset import DatasetGrowthInterrupted, DatasetManager, load_dataset
from test_curriculum_plan import _ready_profile


def _stale_lock(
    profile: object,
    *,
    operation: str,
    approval_digest: str | None = None,
    growth_approval_digest: str | None = None,
) -> Path:
    state = load_series_state(profile, "series-a")  # type: ignore[arg-type]
    value = {
        "protocol": "kpo.campaign-series-lock/v1",
        "series_id": "series-a",
        "operation": operation,
        "campaign_id": None,
        "series_index": None,
        "revision": None,
        "initial_state_digest": state["state_digest"],
        "pid": 99999999,
    }
    if approval_digest is not None:
        value["approval_digest"] = approval_digest
    if growth_approval_digest is not None:
        value["growth_approval_digest"] = growth_approval_digest
    path = profile.base.data_home / "series" / ".locks" / "series-a.json"  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def test_stale_curriculum_preview_lock_is_safe_only_when_state_is_unchanged(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    lock = _stale_lock(profile, operation="curriculum_preview")

    _recover_stale_series_lock(profile, "series-a")

    assert not lock.exists()


def test_stale_curriculum_apply_lock_requires_explicit_recovery(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")
    lock = _stale_lock(
        profile,
        operation="curriculum_apply",
        approval_digest=preview.plan.approval_digest,
        growth_approval_digest=preview.plan.growth_approval_digest,
    )

    with pytest.raises(ValueError, match="manual recovery"):
        _recover_stale_series_lock(profile, "series-a")
    assert lock.exists()


def test_curriculum_recovery_rolls_back_matching_prepared_growth(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    original = profile.dataset_path.read_bytes()
    preview = preview_curriculum(profile, "series-a")
    proposed = load_dataset(preview.proposed_path, profile.base.cases)
    manager = DatasetManager(profile.base.data_home)
    growth = manager.preview_growth(
        profile.dataset,
        proposed,
        manifest_path=profile.dataset_path,
        proposed_manifest_path=preview.proposed_path,
    )
    assert growth.approval_digest == preview.plan.growth_approval_digest
    with pytest.raises(DatasetGrowthInterrupted):
        manager.apply_growth(
            growth,
            approval_digest=growth.approval_digest,
            fault_after_replace=True,
        )
    lock = _stale_lock(
        profile,
        operation="curriculum_apply",
        approval_digest=preview.plan.approval_digest,
        growth_approval_digest=preview.plan.growth_approval_digest,
    )

    result = recover_curriculum(
        profile, approval_digest=preview.plan.approval_digest
    )

    assert result.state == "rolled_back"
    assert profile.dataset_path.read_bytes() == original
    assert not lock.exists()
    with pytest.raises(ValueError, match="stale curriculum_apply lock"):
        recover_curriculum(profile, approval_digest=preview.plan.approval_digest)


def test_curriculum_recovery_finishes_after_rollback_before_lock_cleanup(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")
    proposed = load_dataset(preview.proposed_path, profile.base.cases)
    manager = DatasetManager(profile.base.data_home)
    growth = manager.preview_growth(
        profile.dataset,
        proposed,
        manifest_path=profile.dataset_path,
        proposed_manifest_path=preview.proposed_path,
    )
    with pytest.raises(DatasetGrowthInterrupted):
        manager.apply_growth(
            growth,
            approval_digest=growth.approval_digest,
            fault_after_replace=True,
        )
    manager.recover_growth(growth.approval_digest)
    lock = _stale_lock(
        profile,
        operation="curriculum_apply",
        approval_digest=preview.plan.approval_digest,
        growth_approval_digest=preview.plan.growth_approval_digest,
    )

    result = recover_curriculum(
        profile, approval_digest=preview.plan.approval_digest
    )

    assert result.state == "rolled_back"
    assert not lock.exists()


def test_curriculum_recovery_verifies_already_committed_growth(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    preview = preview_curriculum(profile, "series-a")
    proposed = load_dataset(preview.proposed_path, profile.base.cases)
    manager = DatasetManager(profile.base.data_home)
    growth = manager.preview_growth(
        profile.dataset,
        proposed,
        manifest_path=profile.dataset_path,
        proposed_manifest_path=preview.proposed_path,
    )
    manager.apply_growth(growth, approval_digest=growth.approval_digest)
    lock = _stale_lock(
        profile,
        operation="curriculum_apply",
        approval_digest=preview.plan.approval_digest,
        growth_approval_digest=preview.plan.growth_approval_digest,
    )

    result = recover_curriculum(
        profile, approval_digest=preview.plan.approval_digest
    )

    assert result.state == "committed"
    assert profile.dataset_path.read_bytes() == preview.proposed_path.read_bytes()
    assert not lock.exists()


def test_curriculum_recovery_clears_prewrite_apply_lock_without_journal(
    tmp_path: Path,
) -> None:
    profile = _ready_profile(tmp_path)
    original = profile.dataset_path.read_bytes()
    preview = preview_curriculum(profile, "series-a")
    lock = _stale_lock(
        profile,
        operation="curriculum_apply",
        approval_digest=preview.plan.approval_digest,
        growth_approval_digest=preview.plan.growth_approval_digest,
    )

    result = recover_curriculum(
        profile, approval_digest=preview.plan.approval_digest
    )

    assert result.state == "unchanged"
    assert profile.dataset_path.read_bytes() == original
    assert not lock.exists()
