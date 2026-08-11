from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.calibration_metrics import CalibrationCollector
from kpo.evaluator_calibration_report import (
    EvaluatorCalibrationPersistenceError,
    build_calibration_documents,
    calibration_summary,
    load_calibration_artifacts,
    persist_calibration_artifacts,
)


ANCHORS = (
    ("a" * 64, {"alignment": 0.2, "validity": 0.5}),
    ("b" * 64, {"alignment": 0.6, "validity": 0.7}),
)
SLOT_RECORDS = (
    {
        "slot": "primary",
        "identity": "primary-evaluator",
        "configuration_digest": "1" * 64,
    },
    {
        "slot": "observer:a",
        "identity": "observer-evaluator",
        "configuration_digest": "2" * 64,
    },
)


def _collector() -> CalibrationCollector:
    collector = CalibrationCollector(
        anchors=ANCHORS,
        slots=("primary", "observer:a"),
        dimensions=("alignment", "validity"),
    )
    collector.record_scores(
        "a" * 64, "primary", {"alignment": 0.3, "validity": 0.5}
    )
    collector.record_scores(
        "b" * 64, "primary", {"alignment": 0.7, "validity": 0.8}
    )
    collector.record_unavailable("a" * 64, "observer:a", "timeout")
    collector.record_scores(
        "b" * 64, "observer:a", {"alignment": 0.8, "validity": 0.7}
    )
    return collector


def _documents(
    collector: CalibrationCollector | None = None,
    *,
    state: str = "complete",
) -> tuple[dict[str, object], dict[str, object]]:
    return build_calibration_documents(
        collector or _collector(),
        campaign_id="campaign-a",
        profile_id="profile-a",
        profile_snapshot_digest="3" * 64,
        anchor_set_digest="4" * 64,
        anchor_request_set_digest="5" * 64,
        approved_record_digest="6" * 64,
        rubric_version="rubric-v1",
        drift_thresholds={"alignment": 0.1, "validity": 0.2},
        slot_records=SLOT_RECORDS,
        collection_state=state,
    )


def test_calibration_documents_separate_aggregate_and_private_observations() -> None:
    report, observations = _documents()
    serialized_report = json.dumps(report, sort_keys=True)
    serialized_observations = json.dumps(observations, sort_keys=True)

    assert report["protocol"] == "kpo.evaluator-calibration/v1"
    assert observations["protocol"] == "kpo.evaluator-calibration-observations/v1"
    assert report["collection_state"] == "complete"
    assert report["anchor_count"] == 2
    assert report["coverage"] == {
        "primary": {"completed": 2, "unavailable": 0},
        "observer:a": {"completed": 1, "unavailable": 1},
    }
    assert report["metrics"]["primary"]["alignment"][
        "signed_error_mean"
    ] == pytest.approx(0.1)
    assert report["observation_file_digest"]
    assert report["report_digest"]
    assert "expected_scores" not in serialized_report
    assert "anchor_digest" not in serialized_report
    assert "expected_scores" in serialized_observations
    assert "anchor_digest" in serialized_observations
    assert "anchor_id" not in serialized_observations
    assert "provenance" not in serialized_observations


def test_calibration_artifacts_persist_reload_and_produce_safe_summary(
    tmp_path: Path,
) -> None:
    report, observations = _documents()

    binding = persist_calibration_artifacts(
        tmp_path, "campaign-a", report, observations
    )
    loaded_report, loaded_observations = load_calibration_artifacts(
        tmp_path, "campaign-a"
    )

    assert loaded_report == report
    assert loaded_observations == observations
    assert binding["evaluator_calibration_path"] == (
        "campaigns/campaign-a/evaluator-calibration.json"
    )
    assert binding["evaluator_calibration_observations_path"] == (
        "campaigns/campaign-a/evaluator-calibration-observations.json"
    )
    assert binding["evaluator_calibration_summary"] == calibration_summary(report)
    assert binding["evaluator_calibration_summary"] == {
        "anchor_count": 2,
        "collection_state": "complete",
        "coverage": report["coverage"],
        "maximum_absolute_calibration_error": {
            "alignment": pytest.approx(0.2),
            "validity": pytest.approx(0.1),
        },
        "unavailable_totals": {"observer:a": {"timeout": 1}},
    }
    assert "expected_scores" not in json.dumps(binding, sort_keys=True)


def test_calibration_artifact_loader_rejects_unknown_fields_and_byte_substitution(
    tmp_path: Path,
) -> None:
    report, observations = _documents()
    persist_calibration_artifacts(tmp_path, "campaign-a", report, observations)
    root = tmp_path / "campaigns" / "campaign-a"
    report_path = root / "evaluator-calibration.json"
    observation_path = root / "evaluator-calibration-observations.json"
    original_report = report_path.read_bytes()
    original_observations = observation_path.read_bytes()

    raw = json.loads(original_report)
    report_path.write_text(json.dumps(raw | {"unknown": True}))
    with pytest.raises(ValueError, match="report fields"):
        load_calibration_artifacts(tmp_path, "campaign-a")

    report_path.write_bytes(original_report)
    observation_path.write_bytes(original_observations + b" ")
    with pytest.raises(ValueError, match="observation.*digest"):
        load_calibration_artifacts(tmp_path, "campaign-a")

    observation_path.write_bytes(original_observations)
    raw_observations = json.loads(original_observations)
    raw_observations["observations"][0]["expected_scores"]["alignment"] = 0.9
    observation_path.write_text(json.dumps(raw_observations))
    with pytest.raises(ValueError, match="observation.*digest"):
        load_calibration_artifacts(tmp_path, "campaign-a")


def test_calibration_documents_require_terminal_complete_coordinates() -> None:
    incomplete = CalibrationCollector(
        anchors=ANCHORS,
        slots=("primary", "observer:a"),
        dimensions=("alignment", "validity"),
    )
    incomplete.mark_pending("a" * 64, "primary")

    with pytest.raises(ValueError, match="terminal"):
        _documents(incomplete)


def test_calibration_persistence_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, observations = _documents()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr("kpo.evaluator_calibration_report.os.replace", fail_replace)
    with pytest.raises(
        EvaluatorCalibrationPersistenceError, match="persistence"
    ):
        persist_calibration_artifacts(tmp_path, "campaign-a", report, observations)
