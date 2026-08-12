from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kpo.campaign_profile import campaign_profile_snapshot, load_campaign_profile
from kpo.campaign_series import (
    SeriesEntry,
    append_series_entry,
    initialize_series,
    persist_campaign_series_evidence,
)
from kpo.curriculum import load_curriculum_signal
from kpo.digest import canonical_digest
from kpo.reasoning_differential import persist_differential_summary
from test_curriculum_profile import _configured_profile


def _private_differential_record(path: Path, *, kind: str) -> None:
    fields = {
        "protocol": "kpo.differential-analysis-private/v1",
        "iteration": 0,
        "state": "available",
        "failure_kind": None,
        "agent_claims": {},
        "reference_claims": {},
        "claim_differential": {"entries": [{"kind": kind}]},
        "diagnosis": {
            "conclusion_validity": {"assessment": "supported"},
            "reasoning_fidelity": {"assessment": "challenged"},
            "reasoning_soundness": {"assessment": "supported"},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(fields | {"record_digest": canonical_digest(fields)}) + "\n",
        encoding="utf-8",
    )


def _attach_signal_campaign(
    profile: object,
    *,
    series_id: str,
    campaign_id: str,
    index: int,
    kind: str,
) -> None:
    root = profile.base.data_home / "campaigns" / campaign_id  # type: ignore[attr-defined]
    _private_differential_record(
        root / "differential" / "iteration-0000.json", kind=kind
    )
    snapshot = campaign_profile_snapshot(profile)  # type: ignore[arg-type]
    analyzer = profile.differential_analyzer  # type: ignore[attr-defined]
    summary = persist_differential_summary(
        profile.base.data_home,  # type: ignore[attr-defined]
        campaign_id,
        profile_snapshot_digest=snapshot,
        extractor=analyzer.extractor,
        differ=analyzer.differ,
        diagnostician=analyzer.diagnostician,
    )
    summary_record = summary["differential_analysis_summary"]
    evidence = persist_campaign_series_evidence(
        profile,  # type: ignore[arg-type]
        series_id=series_id,
        series_index=index,
        campaign_id=campaign_id,
        terminal_state="budget_exhausted",
        profile_snapshot_digest=snapshot,
        iteration_count=1,
        call_count=0,
        rejected_candidate_count=0,
        report=None,
        report_bindings=summary,
    )
    append_series_entry(
        profile,  # type: ignore[arg-type]
        series_id,
        SeriesEntry(
            index=index,
            campaign_id=campaign_id,
            revision=0,
            campaign_state="budget_exhausted",
            parent_policy_digest=profile.base.policy.digest,  # type: ignore[attr-defined]
            candidate_policy_digest=None,
            dataset_digest=profile.dataset.digest,  # type: ignore[attr-defined]
            profile_snapshot_digest=snapshot,
            evidence_path=evidence["path"],
            evidence_digest=evidence["byte_digest"],
            evidence_record_digest=evidence["record_digest"],
        ),
    )
    campaign_state = {
        "campaign_id": campaign_id,
        "series_id": series_id,
        "series_index": index,
        "series_evidence_revision": 0,
        "state": "budget_exhausted",
        "profile_snapshot_digest": snapshot,
        "series_evidence_path": evidence["path"],
        "series_evidence_digest": evidence["byte_digest"],
        "series_evidence_record_digest": evidence["record_digest"],
        "differential_analysis_digest": summary["differential_analysis_digest"],
        "differential_analysis_summary": summary_record,
    }
    (root / "campaign.json").write_text(
        json.dumps(campaign_state) + "\n", encoding="utf-8"
    )


def test_curriculum_signal_loads_latest_strict_actionable_evidence(
    tmp_path: Path,
) -> None:
    profile = load_campaign_profile(
        _configured_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    initialize_series(profile, "series-a")
    _attach_signal_campaign(
        profile,
        series_id="series-a",
        campaign_id="campaign-a",
        index=0,
        kind="missing",
    )

    signal = load_curriculum_signal(profile, "series-a")

    assert signal.campaign_id == "campaign-a"
    assert signal.kind_counts == {
        "missing": 1,
        "surplus": 0,
        "contradicted": 0,
        "frame_divergence": 0,
    }
    assert signal.gap_weights["missing"] == 1.0
    assert signal.source_campaign_identity_digest == canonical_digest(
        {
            "campaign_id": "campaign-a",
            "profile_id": profile.base.profile_id,
            "series_id": "series-a",
            "series_index": 0,
            "revision": 0,
        }
    )


def test_curriculum_signal_fails_closed_when_bound_summary_is_tampered(
    tmp_path: Path,
) -> None:
    profile = load_campaign_profile(
        _configured_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    initialize_series(profile, "series-a")
    _attach_signal_campaign(
        profile,
        series_id="series-a",
        campaign_id="campaign-a",
        index=0,
        kind="missing",
    )
    path = (
        profile.base.data_home
        / "campaigns"
        / "campaign-a"
        / "differential-analysis-summary.json"
    )
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="byte digest"):
        load_curriculum_signal(profile, "series-a")


def test_curriculum_signal_rejects_all_zero_actionable_vector(
    tmp_path: Path,
) -> None:
    profile = load_campaign_profile(
        _configured_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )
    initialize_series(profile, "series-a")
    _attach_signal_campaign(
        profile,
        series_id="series-a",
        campaign_id="campaign-a",
        index=0,
        kind="matched",
    )

    with pytest.raises(ValueError, match="signal unavailable"):
        load_curriculum_signal(profile, "series-a")
