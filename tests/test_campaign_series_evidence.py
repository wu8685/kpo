from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_campaign_series_profile import _series_profile

from kpo.campaign_profile import campaign_profile_snapshot, load_campaign_profile
from kpo.campaign_series import (
    derive_series_metrics,
    load_campaign_series_evidence,
    persist_campaign_series_evidence,
)
from kpo.models import Partition


def _profile(tmp_path: Path):
    return load_campaign_profile(
        _series_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )


def _report(parent_digest: str, *, validation: float = 0.05, holdout: float = 0.04):
    gains = {
        Partition.VALIDATION: {"alignment": validation},
        Partition.HOLDOUT: {"alignment": holdout},
        Partition.REGRESSION: {"alignment": 0.03},
    }
    return SimpleNamespace(
        parent_policy_digest=parent_digest,
        candidate_policy_digest="2" * 64,
        partition_gains=gains,
        ablation_gains={"alignment": 0.06},
        digest="7" * 64,
        passed=True,
    )


def test_campaign_series_evidence_persists_only_aggregate_scores(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    binding = persist_campaign_series_evidence(
        profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        terminal_state="promotion_previewed",
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=2,
        call_count=17,
        rejected_candidate_count=1,
        report=_report(profile.base.policy.digest),
        report_bindings={
            "evaluator_agreement_digest": "8" * 64,
            "evaluator_agreement_report_digest": "9" * 64,
            "evaluator_calibration_digest": "a" * 64,
            "evaluator_calibration_observations_digest": "b" * 64,
            "evaluator_calibration_report_digest": "c" * 64,
        },
    )
    loaded = load_campaign_series_evidence(
        profile, "campaign-a", expected_series_id="series-a"
    )

    assert binding["path"] == "campaigns/campaign-a/series-evidence.json"
    assert binding["byte_digest"]
    assert binding["record_digest"] == loaded["record_digest"]
    assert loaded["revision"] == 0
    assert loaded["scores"] == {
        "validation_gain": {"alignment": 0.05},
        "holdout_gain": {"alignment": 0.04},
        "regression_gain": {"alignment": 0.03},
        "ablation_gain": {"alignment": 0.06},
    }
    serialized = json.dumps(loaded, sort_keys=True)
    for prohibited in (
        "case_id",
        "artifact_id",
        "rollout",
        "reference",
        "provenance",
        "expected_score",
        "stderr",
        str(tmp_path),
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    "terminal_state", ["no_patchable_diagnosis", "budget_exhausted", "failed"]
)
def test_non_promotion_evidence_has_explicit_unavailable_scores(
    tmp_path: Path, terminal_state: str
) -> None:
    profile = _profile(tmp_path)
    persist_campaign_series_evidence(
        profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        terminal_state=terminal_state,
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=1,
        call_count=3,
        rejected_candidate_count=0,
        report=None,
        report_bindings={},
    )

    loaded = load_campaign_series_evidence(profile, "campaign-a")

    assert loaded["scores"] is None
    assert loaded["candidate_policy_digest"] is None
    assert loaded["vector_report_digest"] is None


def test_evidence_requires_report_exactly_for_promotion_preview(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    common = dict(
        profile=profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=1,
        call_count=3,
        rejected_candidate_count=0,
        report_bindings={},
    )

    with pytest.raises(ValueError, match="passing report"):
        persist_campaign_series_evidence(
            **common, terminal_state="promotion_previewed", report=None
        )
    with pytest.raises(ValueError, match="only promotion_previewed"):
        persist_campaign_series_evidence(
            **common,
            terminal_state="failed",
            report=_report(profile.base.policy.digest),
        )


def test_evidence_persist_is_idempotent_and_rejects_different_content(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    common = dict(
        profile=profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        terminal_state="promotion_previewed",
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=1,
        call_count=3,
        rejected_candidate_count=0,
        report_bindings={},
    )

    first = persist_campaign_series_evidence(
        **common, report=_report(profile.base.policy.digest)
    )
    repeated = persist_campaign_series_evidence(
        **common, report=_report(profile.base.policy.digest)
    )

    assert repeated == first
    with pytest.raises(FileExistsError, match="different content"):
        persist_campaign_series_evidence(
            **common,
            report=_report(profile.base.policy.digest, validation=0.06),
        )


def test_evidence_revisions_are_separate_and_immutable(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    common = dict(
        profile=profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=1,
        call_count=3,
        rejected_candidate_count=0,
        report_bindings={},
    )
    revision_zero = persist_campaign_series_evidence(
        **common,
        revision=0,
        terminal_state="failed",
        report=None,
    )
    revision_one = persist_campaign_series_evidence(
        **common,
        revision=1,
        terminal_state="promotion_previewed",
        report=_report(profile.base.policy.digest),
    )

    assert revision_zero["path"] == "campaigns/campaign-a/series-evidence.json"
    assert revision_one["path"] == "campaigns/campaign-a/series-evidence.1.json"
    assert load_campaign_series_evidence(profile, "campaign-a", revision=0)[
        "terminal_state"
    ] == "failed"
    assert load_campaign_series_evidence(profile, "campaign-a", revision=1)[
        "terminal_state"
    ] == "promotion_previewed"
    with pytest.raises(FileExistsError, match="different content"):
        persist_campaign_series_evidence(
            **common,
            revision=1,
            terminal_state="promotion_previewed",
            report=_report(profile.base.policy.digest, validation=0.06),
        )


def test_missing_campaign_series_evidence_is_explicit(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    with pytest.raises(KeyError, match="missing campaign series evidence"):
        load_campaign_series_evidence(profile, "missing")


@pytest.mark.parametrize("mutation", ["unknown", "record", "identity", "scores"])
def test_evidence_strict_reload_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    profile = _profile(tmp_path)
    persist_campaign_series_evidence(
        profile,
        series_id="series-a",
        series_index=0,
        campaign_id="campaign-a",
        terminal_state="promotion_previewed",
        profile_snapshot_digest=campaign_profile_snapshot(profile),
        iteration_count=1,
        call_count=3,
        rejected_candidate_count=0,
        report=_report(profile.base.policy.digest),
        report_bindings={},
    )
    path = profile.base.data_home / "campaigns" / "campaign-a" / "series-evidence.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "unknown":
        raw["extra"] = True
    elif mutation == "record":
        raw["record_digest"] = "0" * 64
    elif mutation == "identity":
        raw["campaign_id"] = "campaign-b"
    else:
        raw["scores"]["holdout_gain"]["alignment"] = 0.9
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        load_campaign_series_evidence(profile, "campaign-a")


def _evidence(
    campaign_id: str,
    dataset_digest: str,
    state: str,
    validation: float | None,
    holdout: float | None,
) -> dict:
    scores = (
        None
        if validation is None
        else {
            "validation_gain": {"alignment": validation},
            "holdout_gain": {"alignment": holdout},
            "regression_gain": {"alignment": 0.0},
            "ablation_gain": {"alignment": 0.0},
        }
    )
    return {
        "campaign_id": campaign_id,
        "terminal_state": state,
        "dataset_digest": dataset_digest,
        "dimensions": ["alignment"],
        "scores": scores,
    }


def test_series_metrics_segment_patience_and_cumulative_holdout_math() -> None:
    evidence = [
        _evidence("c1", "1" * 64, "promotion_previewed", 0.20, 0.10),
        _evidence("c2", "1" * 64, "promotion_previewed", 0.10, 0.02),
        _evidence("c3", "2" * 64, "promotion_previewed", 0.005, -0.01),
        _evidence("c4", "2" * 64, "no_patchable_diagnosis", None, None),
        _evidence("c5", "2" * 64, "promotion_previewed", 0.007, -0.18),
    ]

    result = derive_series_metrics(
        evidence,
        applied_campaign_ids={"c1", "c2", "c5"},
        max_campaigns=6,
        min_improvement=0.01,
        patience=2,
        holdout_degradation=0.05,
    )

    assert result["dataset_segments"] == [
        {"index": 0, "start": 0, "end": 1, "dataset_digest": "1" * 64},
        {"index": 1, "start": 2, "end": 4, "dataset_digest": "2" * 64},
    ]
    assert result["no_improvement_streak"] == 3
    assert result["cumulative_applied_holdout_gain"] == {"alignment": -0.06}
    assert result["indicators"] == {
        "diminishing_returns": True,
        "holdout_degradation": True,
        "max_campaigns": False,
    }


def test_failed_campaign_is_skipped_and_thresholds_are_strict() -> None:
    evidence = [
        _evidence("c1", "1" * 64, "promotion_previewed", 0.01, -0.05),
        _evidence("c2", "1" * 64, "failed", None, None),
    ]

    result = derive_series_metrics(
        evidence,
        applied_campaign_ids={"c1"},
        max_campaigns=2,
        min_improvement=0.01,
        patience=1,
        holdout_degradation=0.05,
    )

    assert result["no_improvement_streak"] == 0
    assert result["indicators"] == {
        "diminishing_returns": False,
        "holdout_degradation": False,
        "max_campaigns": True,
    }


def test_empty_series_metrics_are_explicit() -> None:
    result = derive_series_metrics(
        [],
        applied_campaign_ids=set(),
        max_campaigns=2,
        min_improvement=0.01,
        patience=1,
        holdout_degradation=0.05,
    )

    assert result == {
        "dataset_segments": [],
        "no_improvement_streak": 0,
        "cumulative_applied_holdout_gain": {},
        "indicators": {
            "diminishing_returns": False,
            "holdout_degradation": False,
            "max_campaigns": False,
        },
    }


def test_cumulative_holdout_normalizes_binary_float_noise() -> None:
    evidence = [
        _evidence("c1", "1" * 64, "promotion_previewed", 0.2, 0.09999999999999998),
        _evidence("c2", "1" * 64, "promotion_previewed", 0.1, 0.020000000000000018),
        _evidence("c3", "1" * 64, "promotion_previewed", 0.007, -0.18),
    ]

    result = derive_series_metrics(
        evidence,
        applied_campaign_ids={"c1", "c2", "c3"},
        max_campaigns=3,
        min_improvement=0.01,
        patience=2,
        holdout_degradation=0.05,
    )

    assert result["cumulative_applied_holdout_gain"] == {"alignment": -0.06}
