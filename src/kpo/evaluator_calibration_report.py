from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from kpo.calibration_metrics import CalibrationCollector
from kpo.digest import canonical_digest


_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_REPORT_FIELDS = {
    "protocol",
    "campaign_id",
    "profile_id",
    "profile_snapshot_digest",
    "anchor_set_digest",
    "anchor_request_set_digest",
    "approved_record_digest",
    "rubric_version",
    "dimensions",
    "drift_thresholds",
    "slots",
    "collection_state",
    "anchor_count",
    "coverage",
    "unavailable_totals",
    "metrics",
    "observation_file_digest",
    "report_digest",
}
_OBSERVATION_DOCUMENT_FIELDS = {
    "protocol",
    "campaign_id",
    "profile_id",
    "profile_snapshot_digest",
    "anchor_set_digest",
    "anchor_request_set_digest",
    "rubric_version",
    "dimensions",
    "slots",
    "observations",
}
_OBSERVATION_FIELDS = {"anchor_digest", "expected_scores", "slots"}


class EvaluatorCalibrationPersistenceError(RuntimeError):
    pass


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _coverage(
    collector: CalibrationCollector,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    coverage = {
        slot: {"completed": 0, "unavailable": 0} for slot in collector.slots
    }
    unavailable: dict[str, dict[str, int]] = {}
    for row in collector.observations():
        slots = row["slots"]
        assert isinstance(slots, dict)
        if set(slots) != set(collector.slots):
            raise ValueError("calibration observations must be terminal and complete")
        for slot in collector.slots:
            value = slots[slot]
            state = value.get("state")
            if state == "completed":
                coverage[slot]["completed"] += 1
            elif state == "unavailable":
                coverage[slot]["unavailable"] += 1
                kind = str(value["failure_kind"])
                counts = unavailable.setdefault(slot, {})
                counts[kind] = counts.get(kind, 0) + 1
            else:
                raise ValueError("calibration observations must be terminal and complete")
    return coverage, {
        slot: dict(sorted(counts.items()))
        for slot, counts in sorted(unavailable.items())
    }


def build_calibration_documents(
    collector: CalibrationCollector,
    *,
    campaign_id: str,
    profile_id: str,
    profile_snapshot_digest: str,
    anchor_set_digest: str,
    anchor_request_set_digest: str,
    approved_record_digest: str,
    rubric_version: str,
    drift_thresholds: Mapping[str, float],
    slot_records: tuple[dict[str, str], ...],
    collection_state: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if not _CAMPAIGN_ID.fullmatch(campaign_id):
        raise ValueError("campaign ID must be a safe path component")
    if collection_state not in {"complete", "budget_exhausted"}:
        raise ValueError("invalid calibration collection state")
    if set(drift_thresholds) != set(collector.dimensions):
        raise ValueError("drift threshold dimensions must match calibration rubric")
    if tuple(record["slot"] for record in slot_records) != collector.slots:
        raise ValueError("calibration slot records do not match collector")
    coverage, unavailable = _coverage(collector)
    observations: dict[str, object] = {
        "protocol": "kpo.evaluator-calibration-observations/v1",
        "campaign_id": campaign_id,
        "profile_id": profile_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "anchor_set_digest": anchor_set_digest,
        "anchor_request_set_digest": anchor_request_set_digest,
        "rubric_version": rubric_version,
        "dimensions": list(collector.dimensions),
        "slots": list(slot_records),
        "observations": collector.observations(),
    }
    observation_file_digest = _sha256(_json_bytes(observations))
    fields: dict[str, object] = {
        "protocol": "kpo.evaluator-calibration/v1",
        "campaign_id": campaign_id,
        "profile_id": profile_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "anchor_set_digest": anchor_set_digest,
        "anchor_request_set_digest": anchor_request_set_digest,
        "approved_record_digest": approved_record_digest,
        "rubric_version": rubric_version,
        "dimensions": list(collector.dimensions),
        "drift_thresholds": dict(sorted(drift_thresholds.items())),
        "slots": list(slot_records),
        "collection_state": collection_state,
        "anchor_count": len(collector.expected),
        "coverage": coverage,
        "unavailable_totals": unavailable,
        "metrics": collector.metrics(),
        "observation_file_digest": observation_file_digest,
    }
    report = fields | {"report_digest": canonical_digest(fields)}
    return report, observations


def calibration_summary(report: Mapping[str, object]) -> dict[str, object]:
    dimensions = report["dimensions"]
    metrics = report["metrics"]
    assert isinstance(dimensions, list) and isinstance(metrics, dict)
    maxima: dict[str, float | None] = {}
    for dimension in dimensions:
        values: list[float] = []
        for slot_metrics in metrics.values():
            assert isinstance(slot_metrics, dict)
            metric = slot_metrics[str(dimension)]
            assert isinstance(metric, dict)
            value = metric["max_absolute_error"]
            if value is not None:
                values.append(float(value))
        maxima[str(dimension)] = max(values) if values else None
    return {
        "anchor_count": report["anchor_count"],
        "collection_state": report["collection_state"],
        "coverage": report["coverage"],
        "maximum_absolute_calibration_error": maxima,
        "unavailable_totals": report["unavailable_totals"],
    }


def _paths(data_home: Path, campaign_id: str) -> tuple[Path, Path]:
    if not _CAMPAIGN_ID.fullmatch(campaign_id):
        raise ValueError("campaign ID must be a safe path component")
    root = data_home.expanduser().resolve() / "campaigns" / campaign_id
    return (
        root / "evaluator-calibration.json",
        root / "evaluator-calibration-observations.json",
    )


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _load_json(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evaluator calibration {label} JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"evaluator calibration {label} must be an object")
    return value


def _validate_observations(value: dict[str, Any]) -> None:
    if set(value) != _OBSERVATION_DOCUMENT_FIELDS:
        raise ValueError("evaluator calibration observation fields do not match protocol")
    if value["protocol"] != "kpo.evaluator-calibration-observations/v1":
        raise ValueError("evaluator calibration observation protocol mismatch")
    rows = value["observations"]
    if not isinstance(rows, list):
        raise ValueError("evaluator calibration observations must be an array")
    previous = ""
    for row in rows:
        if not isinstance(row, dict) or set(row) != _OBSERVATION_FIELDS:
            raise ValueError("evaluator calibration observation row fields mismatch")
        digest = row["anchor_digest"]
        if not isinstance(digest, str) or digest <= previous:
            raise ValueError("evaluator calibration observations are not sorted")
        previous = digest


def load_calibration_artifacts(
    data_home: Path, campaign_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    report_path, observation_path = _paths(data_home, campaign_id)
    report_content = report_path.read_bytes()
    observation_content = observation_path.read_bytes()
    report = _load_json(report_content, "report")
    observations = _load_json(observation_content, "observation")
    if set(report) != _REPORT_FIELDS:
        raise ValueError("evaluator calibration report fields do not match protocol")
    if report["protocol"] != "kpo.evaluator-calibration/v1":
        raise ValueError("evaluator calibration report protocol mismatch")
    fields = dict(report)
    report_digest = fields.pop("report_digest")
    if report_digest != canonical_digest(fields):
        raise ValueError("evaluator calibration report digest mismatch")
    _validate_observations(observations)
    if report["observation_file_digest"] != _sha256(observation_content):
        raise ValueError("evaluator calibration observation file digest mismatch")
    identity_fields = (
        "campaign_id",
        "profile_id",
        "profile_snapshot_digest",
        "anchor_set_digest",
        "anchor_request_set_digest",
        "rubric_version",
        "dimensions",
        "slots",
    )
    if any(report[field] != observations[field] for field in identity_fields):
        raise ValueError("evaluator calibration artifact identity mismatch")
    if report["campaign_id"] != campaign_id:
        raise ValueError("evaluator calibration campaign identity mismatch")
    return report, observations


def persist_calibration_artifacts(
    data_home: Path,
    campaign_id: str,
    report: dict[str, object],
    observations: dict[str, object],
) -> dict[str, object]:
    report_path, observation_path = _paths(data_home, campaign_id)
    report_content = _json_bytes(report)
    observation_content = _json_bytes(observations)
    try:
        _atomic_bytes(observation_path, observation_content)
        _atomic_bytes(report_path, report_content)
        loaded_report, loaded_observations = load_calibration_artifacts(
            data_home, campaign_id
        )
        if loaded_report != report or loaded_observations != observations:
            raise ValueError("evaluator calibration artifact reload mismatch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise EvaluatorCalibrationPersistenceError(
            "evaluator calibration artifact persistence failed"
        ) from error
    root = data_home.expanduser().resolve()
    return {
        "evaluator_calibration_path": report_path.relative_to(root).as_posix(),
        "evaluator_calibration_digest": _sha256(report_content),
        "evaluator_calibration_observations_path": observation_path.relative_to(
            root
        ).as_posix(),
        "evaluator_calibration_observations_digest": _sha256(observation_content),
        "evaluator_calibration_report_digest": report["report_digest"],
        "evaluator_calibration_summary": calibration_summary(report),
    }
