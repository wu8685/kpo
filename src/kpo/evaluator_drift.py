from __future__ import annotations

import math
import json
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kpo.evaluator_calibration_report import (
    calibration_summary,
    load_calibration_artifacts,
)

if TYPE_CHECKING:
    from kpo.campaign_profile import CampaignProfile


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _slot_map(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = report["slots"]
    if not isinstance(raw, list):
        raise ValueError("evaluator calibration slots must be an array")
    result: dict[str, dict[str, str]] = {}
    for record in raw:
        if (
            not isinstance(record, dict)
            or set(record) != {"slot", "identity", "configuration_digest"}
            or not all(isinstance(value, str) and value for value in record.values())
        ):
            raise ValueError("invalid evaluator calibration slot record")
        slot = record["slot"]
        if slot in result:
            raise ValueError("duplicate evaluator calibration slot")
        result[slot] = record
    return result


def _successful_scores(
    observations: dict[str, Any], dimensions: tuple[str, ...]
) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    for row in observations["observations"]:
        anchor_digest = row["anchor_digest"]
        slots: dict[str, dict[str, Any]] = row["slots"]
        result[anchor_digest] = {}
        for slot, value in slots.items():
            state = value.get("state")
            if state == "completed":
                if set(value) != {"state", "scores"}:
                    raise ValueError("invalid completed calibration observation")
                scores = value["scores"]
                if not isinstance(scores, dict) or set(scores) != set(dimensions):
                    raise ValueError("calibration score dimensions do not match rubric")
                normalized: dict[str, float] = {}
                for dimension in dimensions:
                    score = scores[dimension]
                    if (
                        not isinstance(score, (int, float))
                        or isinstance(score, bool)
                        or not math.isfinite(score)
                        or not 0 <= score <= 1
                    ):
                        raise ValueError("calibration score must be finite and in [0, 1]")
                    normalized[dimension] = float(score)
                result[anchor_digest][slot] = normalized
            elif state == "unavailable":
                if set(value) != {"state", "failure_kind"}:
                    raise ValueError("invalid unavailable calibration observation")
                if not isinstance(value["failure_kind"], str) or not value[
                    "failure_kind"
                ]:
                    raise ValueError("calibration failure kind is required")
            else:
                raise ValueError("calibration observation is not terminal")
    return result


def _thresholds(value: Any, dimensions: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(dimensions):
        raise ValueError("evaluator drift threshold dimensions mismatch")
    result: dict[str, float] = {}
    for dimension in dimensions:
        threshold = value[dimension]
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("evaluator drift threshold is outside its finite range")
        result[dimension] = float(threshold)
    return result


def compare_evaluator_drift(
    data_home: Path,
    baseline_campaign_id: str,
    target_campaign_id: str,
    *,
    expected_profile_id: str | None = None,
) -> dict[str, object]:
    baseline, baseline_observations = load_calibration_artifacts(
        data_home, baseline_campaign_id
    )
    target, target_observations = load_calibration_artifacts(
        data_home, target_campaign_id
    )
    if baseline["profile_id"] != target["profile_id"]:
        raise ValueError("evaluator drift profile identity mismatch")
    if (
        expected_profile_id is not None
        and baseline["profile_id"] != expected_profile_id
    ):
        raise ValueError("evaluator drift profile does not match requested profile")
    if baseline["anchor_set_digest"] != target["anchor_set_digest"]:
        raise ValueError("evaluator drift anchor set mismatch")
    if baseline["rubric_version"] != target["rubric_version"]:
        raise ValueError("evaluator drift rubric version mismatch")
    baseline_dimensions = tuple(baseline["dimensions"])
    target_dimensions = tuple(target["dimensions"])
    if set(baseline_dimensions) != set(target_dimensions):
        raise ValueError("evaluator drift dimension set mismatch")
    dimensions = tuple(sorted(baseline_dimensions))

    baseline_slots = _slot_map(baseline)
    target_slots = _slot_map(target)
    matched_slots = [slot for slot in baseline_slots if slot in target_slots]
    added_slots = [slot for slot in target_slots if slot not in baseline_slots]
    removed_slots = [slot for slot in baseline_slots if slot not in target_slots]
    baseline_scores = _successful_scores(baseline_observations, dimensions)
    target_scores = _successful_scores(target_observations, dimensions)
    if set(baseline_scores) != set(target_scores):
        raise ValueError("evaluator drift observation anchor mismatch")

    baseline_thresholds = _thresholds(baseline["drift_thresholds"], dimensions)
    target_thresholds = _thresholds(target["drift_thresholds"], dimensions)
    slot_results: dict[str, object] = {}
    overall_drift = False
    for slot in matched_slots:
        dimension_results: dict[str, object] = {}
        for dimension in dimensions:
            deltas: list[float] = []
            for anchor_digest in sorted(baseline_scores):
                baseline_slot = baseline_scores[anchor_digest].get(slot)
                target_slot = target_scores[anchor_digest].get(slot)
                if baseline_slot is not None and target_slot is not None:
                    deltas.append(target_slot[dimension] - baseline_slot[dimension])
            signed = _mean(deltas)
            absolute = [abs(delta) for delta in deltas]
            threshold = float(target_thresholds[dimension])
            detected = signed is not None and abs(signed) > threshold
            overall_drift = overall_drift or detected
            dimension_results[dimension] = {
                "matched_anchor_count": len(deltas),
                "mean_signed_score_delta": signed,
                "mean_absolute_score_delta": _mean(absolute),
                "max_absolute_score_delta": max(absolute) if absolute else None,
                "baseline_unavailable_totals": dict(
                    baseline["unavailable_totals"].get(slot, {})
                ),
                "target_unavailable_totals": dict(
                    target["unavailable_totals"].get(slot, {})
                ),
                "threshold": threshold,
                "drift_detected": detected,
            }
        baseline_record = baseline_slots[slot]
        target_record = target_slots[slot]
        slot_results[slot] = {
            "baseline_identity": baseline_record["identity"],
            "target_identity": target_record["identity"],
            "configuration_changed": (
                baseline_record["configuration_digest"]
                != target_record["configuration_digest"]
            ),
            "dimensions": dimension_results,
        }

    return {
        "protocol": "kpo.evaluator-drift/v1",
        "profile_id": baseline["profile_id"],
        "baseline_campaign_id": baseline_campaign_id,
        "target_campaign_id": target_campaign_id,
        "anchor_set_digest": baseline["anchor_set_digest"],
        "rubric_version": baseline["rubric_version"],
        "dimensions": list(dimensions),
        "baseline_profile_snapshot_digest": baseline[
            "profile_snapshot_digest"
        ],
        "target_profile_snapshot_digest": target["profile_snapshot_digest"],
        "profile_snapshot_changed": (
            baseline["profile_snapshot_digest"]
            != target["profile_snapshot_digest"]
        ),
        "baseline_thresholds": baseline_thresholds,
        "target_thresholds": target_thresholds,
        "threshold_changed": baseline_thresholds != target_thresholds,
        "baseline_collection_state": baseline["collection_state"],
        "target_collection_state": target["collection_state"],
        "added_slots": added_slots,
        "removed_slots": removed_slots,
        "slots": slot_results,
        "drift_detected": overall_drift,
    }


def _validate_campaign_binding(
    profile: CampaignProfile, campaign_id: str
) -> None:
    data_home = profile.base.data_home.resolve()
    state_path = data_home / "campaigns" / campaign_id / "campaign.json"
    if not state_path.is_file():
        raise KeyError(f"unknown campaign: {campaign_id}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid evaluator calibration campaign state") from error
    if not isinstance(state, dict) or state.get("campaign_id") != campaign_id:
        raise ValueError("evaluator calibration campaign state identity mismatch")
    expected_paths = {
        "evaluator_calibration_path": (
            Path("campaigns") / campaign_id / "evaluator-calibration.json"
        ),
        "evaluator_calibration_observations_path": (
            Path("campaigns")
            / campaign_id
            / "evaluator-calibration-observations.json"
        ),
    }
    resolved: dict[str, Path] = {}
    for field, expected in expected_paths.items():
        raw = state.get(field)
        if not isinstance(raw, str):
            raise ValueError(f"evaluator calibration {field} is missing")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or relative != expected:
            raise ValueError(f"evaluator calibration {field} path is invalid")
        path = (data_home / relative).resolve()
        if data_home not in path.parents or not path.is_file():
            raise ValueError(f"evaluator calibration {field} path is missing")
        resolved[field] = path
    report, observations = load_calibration_artifacts(data_home, campaign_id)
    report_digest = hashlib.sha256(
        resolved["evaluator_calibration_path"].read_bytes()
    ).hexdigest()
    observation_digest = hashlib.sha256(
        resolved["evaluator_calibration_observations_path"].read_bytes()
    ).hexdigest()
    if (
        state.get("evaluator_calibration_digest") != report_digest
        or state.get("evaluator_calibration_observations_digest")
        != observation_digest
        or state.get("evaluator_calibration_report_digest")
        != report["report_digest"]
    ):
        raise ValueError("evaluator calibration campaign state digest binding mismatch")
    if (
        report["profile_id"] != profile.base.profile_id
        or report["profile_snapshot_digest"]
        != state.get("profile_snapshot_digest")
        or state.get("evaluator_calibration_summary")
        != calibration_summary(report)
    ):
        raise ValueError("evaluator calibration campaign state summary mismatch")


def compare_profile_evaluator_drift(
    profile: CampaignProfile,
    baseline_campaign_id: str,
    target_campaign_id: str,
) -> dict[str, object]:
    _validate_campaign_binding(profile, baseline_campaign_id)
    _validate_campaign_binding(profile, target_campaign_id)
    return compare_evaluator_drift(
        profile.base.data_home,
        baseline_campaign_id,
        target_campaign_id,
        expected_profile_id=profile.base.profile_id,
    )
