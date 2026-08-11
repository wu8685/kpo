from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign import _add_observers, _initialize, _working_profile
from test_evaluator_calibration import _add_evaluator_audit
from test_evaluator_calibration_campaign import _approve, _capture_providers

from kpo.campaign import CampaignInterrupted, campaign_status, run_campaign
from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import (
    initialize_series,
    load_series_state,
    series_evidence_report,
    series_mutation,
    stop_series,
)
from kpo.external_promotion import (
    apply_campaign_promotion,
    preview_campaign_promotion,
)


def _series_working_profile(root: Path, *, max_campaigns: int = 2) -> Path:
    path = _working_profile(root)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[series]
max_campaigns = {max_campaigns}

[series.stopping]
min_improvement = 0.01
patience = 2
holdout_degradation = 0.05
"""
        )
    return path


def test_passing_campaign_attaches_privacy_safe_series_evidence(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")

    result = run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    assert result["state"] == "promotion_previewed"
    assert result["series_id"] == "series-a"
    assert result["series_index"] == 0
    assert result["series_evidence_path"] == "campaigns/campaign-a/series-evidence.json"
    assert result["series_evidence_digest"]
    evidence_path = profile.base.data_home / result["series_evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["scores"]["validation_gain"] == {"alignment": 0.8}
    assert evidence["scores"]["holdout_gain"] == {"alignment": 0.8}
    assert "case_id" not in json.dumps(evidence)
    series = load_series_state(profile, "series-a")
    assert len(series["entries"]) == 1
    assert series["entries"][0]["campaign_id"] == "campaign-a"
    assert series["entries"][0]["evidence_digest"] == result["series_evidence_digest"]


def test_series_campaign_adds_zero_provider_calls(tmp_path: Path) -> None:
    control_path = _working_profile(tmp_path / "control")
    series_path = _series_working_profile(tmp_path / "series")
    _initialize(control_path)
    _initialize(series_path)
    series_profile = load_campaign_profile(
        series_path, checkout=Path(__file__).parents[1]
    )
    initialize_series(series_profile, "series-a")

    control = run_campaign(
        control_path,
        checkout=Path(__file__).parents[1],
        campaign_id="control-campaign",
    )
    attached = run_campaign(
        series_path,
        checkout=Path(__file__).parents[1],
        campaign_id="series-campaign",
        series_id="series-a",
    )

    assert attached["call_count"] == control["call_count"]
    assert attached["candidate_digest"] == control["candidate_digest"]
    assert attached["candidate_policy_digest"] == control["candidate_policy_digest"]


def test_max_campaigns_rejects_before_creating_or_calling_campaign(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external", max_campaigns=1)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    with pytest.raises(ValueError, match="max_campaigns"):
        run_campaign(
            path,
            checkout=Path(__file__).parents[1],
            campaign_id="campaign-b",
            series_id="series-a",
        )

    assert not (profile.base.data_home / "campaigns" / "campaign-b").exists()


def test_concurrent_allocation_rejects_before_campaign_creation(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")

    with series_mutation(profile, "series-a", operation="test-holder"):
        with pytest.raises(ValueError, match="series concurrent mutation"):
            run_campaign(
                path,
                checkout=checkout,
                campaign_id="campaign-a",
                series_id="series-a",
            )

    assert not (
        profile.base.data_home / "campaigns" / "campaign-a"
    ).exists()


def test_series_campaign_requires_profile_series_configuration(tmp_path: Path) -> None:
    path = _working_profile(tmp_path / "external")
    _initialize(path)

    with pytest.raises(ValueError, match="no series configuration"):
        run_campaign(
            path,
            checkout=Path(__file__).parents[1],
            campaign_id="campaign-a",
            series_id="series-a",
        )


@pytest.mark.parametrize(
    ("terminal", "candidate_score", "max_calls"),
    [
        ("no_patchable_diagnosis", 0.25, 100),
        ("budget_exhausted", 1.0, 1),
    ],
)
def test_non_promotion_terminal_campaigns_attach_unavailable_evidence(
    tmp_path: Path, terminal: str, candidate_score: float, max_calls: int
) -> None:
    path = _working_profile(tmp_path / terminal, candidate_score=candidate_score)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n[series]\nmax_campaigns = 2\n")
    if max_calls != 100:
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "max_provider_calls = 100", f"max_provider_calls = {max_calls}"
            ),
            encoding="utf-8",
        )
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")

    result = run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    assert result["state"] == terminal
    evidence = json.loads(
        (profile.base.data_home / result["series_evidence_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert evidence["scores"] is None
    assert load_series_state(profile, "series-a")["entries"][0][
        "campaign_state"
    ] == terminal


def test_failed_campaign_is_attached_without_quality_scores(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    actor = path.parent / "bin" / "actor"
    actor.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    actor.chmod(0o755)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")

    result = run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    assert result["state"] == "failed"
    evidence = json.loads(
        (profile.base.data_home / result["series_evidence_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert evidence["terminal_state"] == "failed"
    assert evidence["scores"] is None
    assert load_series_state(profile, "series-a")["entries"][0][
        "campaign_state"
    ] == "failed"


def test_failed_series_campaign_resumes_as_new_immutable_revision(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external", max_campaigns=1)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")

    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=Path(__file__).parents[1],
            campaign_id="campaign-a",
            series_id="series-a",
            fault_after_train=True,
        )
    failed = campaign_status(
        path, "campaign-a", checkout=Path(__file__).parents[1]
    )
    resumed = run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
        resume=True,
    )

    assert failed["state"] == "failed"
    assert failed["series_evidence_revision"] == 0
    assert resumed["state"] == "promotion_previewed"
    assert resumed["series_evidence_revision"] == 1
    assert resumed["series_evidence_path"].endswith("series-evidence.1.json")
    series = load_series_state(profile, "series-a")
    assert [
        (entry["index"], entry["revision"], entry["campaign_state"])
        for entry in series["entries"]
    ] == [(0, 0, "failed"), (0, 1, "promotion_previewed")]
    report = series_evidence_report(profile, "series-a")
    full_report = series_evidence_report(profile, "series-a", full=True)
    assert report["campaign_count"] == 1
    assert report["revision_count"] == 2
    assert report["campaign_counts"]["promotion_previewed"] == 1
    assert report["campaign_counts"]["failed"] == 0
    assert len(report["learning_curve"]["alignment"]) == 1
    assert "revision_history" not in report
    assert [item["revision"] for item in full_report["revision_history"]] == [0, 1]


def test_series_resume_finalizes_installed_bundle_as_new_revision_without_calls(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external", max_campaigns=1)
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")

    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=checkout,
            campaign_id="campaign-a",
            series_id="series-a",
            fault_after_bundle=True,
        )
    budget_path = (
        profile.base.data_home
        / "campaigns"
        / "campaign-a"
        / "call-budget.json"
    )
    budget_before = budget_path.read_bytes()

    resumed = run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-a",
        series_id="series-a",
        resume=True,
    )

    assert budget_path.read_bytes() == budget_before
    assert resumed["state"] == "promotion_previewed"
    assert resumed["series_evidence_revision"] == 1
    assert resumed["series_evidence_path"].endswith("series-evidence.1.json")
    assert [
        entry["campaign_state"]
        for entry in load_series_state(profile, "series-a")["entries"]
    ] == ["failed", "promotion_previewed"]


def test_series_report_loads_only_privacy_reviewed_agreement_aggregates(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external")
    _add_observers(path)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    report = series_evidence_report(profile, "series-a")

    trend = report["agreement_trend"][0]
    assert trend["state"] == "available"
    assert trend["event_counts"]
    assert trend["metrics"]
    serialized = json.dumps(report, sort_keys=True)
    assert "case_id" not in serialized
    assert str(tmp_path) not in serialized


def test_series_report_hashes_private_calibration_observations_without_leaking(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(
        _series_working_profile(tmp_path / "external")
    )
    _capture_providers(path)
    _approve(path)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    report = series_evidence_report(profile, "series-a")

    trend = report["calibration_trend"][0]
    assert trend["state"] == "available"
    assert trend["summary"]["anchor_count"] == 2
    assert trend["metrics"]
    serialized = json.dumps(report, sort_keys=True)
    assert "anchor_digest" not in serialized
    assert "expected_scores" not in serialized
    assert str(tmp_path) not in serialized


def test_series_rejects_policy_lineage_divergence_before_provider_calls(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )
    (path.parent / "policy" / "base.md").write_text(
        "Unrelated external policy change.\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="series policy lineage mismatch"):
        run_campaign(
            path,
            checkout=Path(__file__).parents[1],
            campaign_id="campaign-b",
            series_id="series-a",
        )

    assert not (profile.base.data_home / "campaigns" / "campaign-b").exists()


def test_series_requires_recovery_while_promotion_is_applying(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )
    state_path = profile.base.data_home / "campaigns" / "campaign-a" / "campaign.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["promotion_state"] = "applying"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="series promotion recovery required"):
        run_campaign(
            path,
            checkout=Path(__file__).parents[1],
            campaign_id="campaign-b",
            series_id="series-a",
        )

    with pytest.raises(ValueError, match="series promotion recovery required"):
        stop_series(profile, "series-a")
    assert load_series_state(profile, "series-a")["state"] == "active"


def test_applied_promotion_becomes_next_series_parent(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    first = run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-a",
        series_id="series-a",
    )
    preview = preview_campaign_promotion(path, "campaign-a", checkout=checkout)
    apply_campaign_promotion(
        path,
        "campaign-a",
        checkout=checkout,
        approval_digest=preview["approval_digest"],
    )

    second = run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-b",
        series_id="series-a",
    )

    assert second["parent_policy_digest"] == first["candidate_policy_digest"]
    assert second["series_index"] == 1
