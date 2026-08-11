from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.calibration_metrics import CalibrationCollector
from kpo.cli import main
from kpo.evaluator_calibration_report import (
    build_calibration_documents,
    persist_calibration_artifacts,
)
from kpo.evaluator_drift import compare_evaluator_drift
from test_campaign_profile import _campaign_profile
from test_evaluator_calibration import _add_evaluator_audit


ANCHORS = (
    ("a" * 64, {"alignment": 0.25}),
    ("b" * 64, {"alignment": 0.50}),
)


def _collector(
    primary: tuple[float | None, float | None],
    observer: tuple[float | None, float | None] = (None, None),
) -> CalibrationCollector:
    collector = CalibrationCollector(
        anchors=ANCHORS,
        slots=("primary", "observer:a"),
        dimensions=("alignment",),
    )
    for index, (anchor_digest, _) in enumerate(ANCHORS):
        for slot, values in (("primary", primary), ("observer:a", observer)):
            value = values[index]
            if value is None:
                collector.record_unavailable(anchor_digest, slot, "timeout")
            else:
                collector.record_scores(
                    anchor_digest, slot, {"alignment": value}
                )
    return collector


def _persist(
    data_home: Path,
    campaign_id: str,
    collector: CalibrationCollector,
    *,
    threshold: float,
    primary_config: str,
    anchor_set_digest: str = "4" * 64,
    profile_id: str = "profile-a",
) -> None:
    slots = (
        {
            "slot": "primary",
            "identity": f"primary-{campaign_id}",
            "configuration_digest": primary_config,
        },
        {
            "slot": "observer:a",
            "identity": "observer-stable",
            "configuration_digest": "2" * 64,
        },
    )
    report, observations = build_calibration_documents(
        collector,
        campaign_id=campaign_id,
        profile_id=profile_id,
        profile_snapshot_digest=("3" if campaign_id == "baseline" else "7") * 64,
        anchor_set_digest=anchor_set_digest,
        anchor_request_set_digest="5" * 64,
        approved_record_digest="6" * 64,
        rubric_version="rubric-v1",
        drift_thresholds={"alignment": threshold},
        slot_records=slots,
        collection_state="complete",
    )
    binding = persist_calibration_artifacts(
        data_home, campaign_id, report, observations
    )
    state = {
        "campaign_id": campaign_id,
        "state": "promotion_previewed",
        "profile_snapshot_digest": report["profile_snapshot_digest"],
        **binding,
    }
    state_path = data_home / "campaigns" / campaign_id / "campaign.json"
    state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")


def test_drift_recovers_known_delta_and_uses_target_historical_threshold(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    _persist(
        data_home,
        "baseline",
        _collector((0.25, 0.50), (0.30, None)),
        threshold=0.20,
        primary_config="1" * 64,
    )
    _persist(
        data_home,
        "target",
        _collector((0.375, 0.625), (0.30, None)),
        threshold=0.125,
        primary_config="9" * 64,
    )

    result = compare_evaluator_drift(
        data_home, "baseline", "target", expected_profile_id="profile-a"
    )
    primary = result["slots"]["primary"]
    alignment = primary["dimensions"]["alignment"]

    assert result["protocol"] == "kpo.evaluator-drift/v1"
    assert result["baseline_thresholds"] == {"alignment": 0.20}
    assert result["target_thresholds"] == {"alignment": 0.125}
    assert result["threshold_changed"] is True
    assert result["profile_snapshot_changed"] is True
    assert primary["configuration_changed"] is True
    assert alignment == {
        "matched_anchor_count": 2,
        "mean_signed_score_delta": pytest.approx(0.125),
        "mean_absolute_score_delta": pytest.approx(0.125),
        "max_absolute_score_delta": pytest.approx(0.125),
        "baseline_unavailable_totals": {},
        "target_unavailable_totals": {},
        "threshold": 0.125,
        "drift_detected": False,
    }
    assert result["drift_detected"] is False

    target_report = (
        data_home / "campaigns" / "target" / "evaluator-calibration.json"
    )
    raw = json.loads(target_report.read_text())
    fields = dict(raw)
    fields["drift_thresholds"] = {"alignment": 0.124}
    fields.pop("report_digest")
    from kpo.digest import canonical_digest

    changed = fields | {"report_digest": canonical_digest(fields)}
    target_report.write_text(json.dumps(changed, sort_keys=True, indent=2) + "\n")
    result = compare_evaluator_drift(
        data_home, "baseline", "target", expected_profile_id="profile-a"
    )
    assert result["drift_detected"] is True


def test_drift_handles_missing_pairs_without_inventing_behavior_change(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    _persist(
        data_home,
        "baseline",
        _collector((None, None), (None, None)),
        threshold=0.1,
        primary_config="1" * 64,
    )
    _persist(
        data_home,
        "target",
        _collector((None, None), (None, None)),
        threshold=0.1,
        primary_config="9" * 64,
    )

    result = compare_evaluator_drift(data_home, "baseline", "target")
    metric = result["slots"]["primary"]["dimensions"]["alignment"]

    assert result["slots"]["primary"]["configuration_changed"] is True
    assert metric["matched_anchor_count"] == 0
    assert metric["mean_signed_score_delta"] is None
    assert metric["mean_absolute_score_delta"] is None
    assert metric["max_absolute_score_delta"] is None
    assert metric["drift_detected"] is False
    assert result["drift_detected"] is False


def test_drift_rejects_incompatible_anchor_sets_and_never_exposes_rows(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    _persist(
        data_home,
        "baseline",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
    )
    _persist(
        data_home,
        "target",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
        anchor_set_digest="8" * 64,
    )

    with pytest.raises(ValueError, match="anchor set"):
        compare_evaluator_drift(data_home, "baseline", "target")

    # Restore a compatible target and ensure aggregate-only output.
    _persist(
        data_home,
        "target",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
    )
    serialized = json.dumps(
        compare_evaluator_drift(data_home, "baseline", "target"), sort_keys=True
    )
    assert "anchor_digest" not in serialized
    assert "expected_scores" not in serialized
    assert "a" * 64 not in serialized


def test_evaluator_drift_cli_is_read_only_and_ignores_current_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    data_home = profile_path.parent / "runtime"
    _persist(
        data_home,
        "baseline",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
        profile_id="example",
    )
    _persist(
        data_home,
        "target",
        _collector((0.375, 0.625)),
        threshold=0.2,
        primary_config="1" * 64,
        profile_id="example",
    )
    profile_path.write_text(
        profile_path.read_text().replace("alignment = 0.10", "alignment = 0.01"),
        encoding="utf-8",
    )
    protected = {
        path: path.read_bytes()
        for path in (data_home / "campaigns").rglob("*.json")
    }

    assert main(
        [
            "evaluator-drift",
            "--profile",
            str(profile_path),
            "--baseline",
            "baseline",
            "--target",
            "target",
            "--checkout",
            str(Path(__file__).parents[1]),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["target_thresholds"] == {"alignment": 0.2}
    assert result["drift_detected"] is False
    assert all(path.read_bytes() == content for path, content in protected.items())

    state_path = data_home / "campaigns" / "target" / "campaign.json"
    state = json.loads(state_path.read_text())
    state["evaluator_calibration_path"] = "../escape.json"
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="path"):
        main(
            [
                "evaluator-drift",
                "--profile",
                str(profile_path),
                "--baseline",
                "baseline",
                "--target",
                "target",
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )


def test_drift_rejects_rehashed_but_out_of_range_historical_threshold(
    tmp_path: Path,
) -> None:
    from kpo.digest import canonical_digest

    data_home = tmp_path / "runtime"
    _persist(
        data_home,
        "baseline",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
    )
    _persist(
        data_home,
        "target",
        _collector((0.25, 0.50)),
        threshold=0.1,
        primary_config="1" * 64,
    )
    report_path = data_home / "campaigns" / "target" / "evaluator-calibration.json"
    raw = json.loads(report_path.read_text())
    fields = dict(raw)
    fields.pop("report_digest")
    fields["drift_thresholds"] = {"alignment": -0.1}
    report_path.write_text(
        json.dumps(
            fields | {"report_digest": canonical_digest(fields)},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="threshold.*range"):
        compare_evaluator_drift(data_home, "baseline", "target")
