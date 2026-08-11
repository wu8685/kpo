from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from kpo.digest import canonical_digest


_LOGGER = logging.getLogger(__name__)


class AgreementPersistenceError(RuntimeError):
    pass


class AgreementCheckpointError(ValueError):
    pass


def evaluation_event_id(
    *,
    case_digest: str,
    rollout_digest: str,
    policy_digest: str,
    partition: str,
    rubric_digest: str,
) -> str:
    return canonical_digest(
        {
            "case_digest": case_digest,
            "rollout_digest": rollout_digest,
            "policy_digest": policy_digest,
            "partition": partition,
            "rubric_digest": rubric_digest,
        }
    )


@dataclass(slots=True)
class _Event:
    partition: str
    primary: dict[str, float]
    observers: dict[str, dict[str, float]] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


class AgreementCollector:
    def __init__(
        self,
        *,
        primary_evaluator_id: str,
        observers: tuple[tuple[str, str], ...],
        rubric_version: str,
        dimensions: tuple[str, ...],
    ) -> None:
        if not primary_evaluator_id:
            raise ValueError("primary evaluator identity is required")
        if not rubric_version:
            raise ValueError("rubric version is required")
        if not dimensions or len(set(dimensions)) != len(dimensions):
            raise ValueError("dimensions must be non-empty and unique")
        names = [name for name, _ in observers]
        identities = [identity for _, identity in observers]
        if len(set(names)) != len(names):
            raise ValueError("observer names must be unique")
        if len(set(identities)) != len(identities) or primary_evaluator_id in identities:
            raise ValueError("evaluator identities must be unique")
        self.primary_evaluator_id = primary_evaluator_id
        self.observers = tuple(sorted(observers))
        self.observer_names = frozenset(names)
        self.rubric_version = rubric_version
        self.dimensions = tuple(sorted(dimensions))
        self.events: dict[str, _Event] = {}

    def _scores(self, raw: Mapping[str, float]) -> dict[str, float]:
        if set(raw) != set(self.dimensions):
            raise ValueError("score dimension set does not match rubric")
        result: dict[str, float] = {}
        for dimension in self.dimensions:
            score = raw[dimension]
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise ValueError(f"score for {dimension} must be finite")
            if not 0 <= score <= 1:
                raise ValueError(f"score for {dimension} must be between 0 and 1")
            result[dimension] = float(score)
        return result

    def record_primary(
        self, event_id: str, partition: str, scores: Mapping[str, float]
    ) -> None:
        normalized = self._scores(scores)
        event = self.events.get(event_id)
        if event is None:
            self.events[event_id] = _Event(partition, normalized)
            return
        if event.partition != partition or event.primary != normalized:
            raise ValueError("conflicting duplicate primary observation")

    def _event(self, event_id: str, observer_name: str) -> _Event:
        if observer_name not in self.observer_names:
            raise ValueError(f"unknown observer: {observer_name}")
        try:
            return self.events[event_id]
        except KeyError as error:
            raise ValueError("observer observation requires a primary event") from error

    def record_observer(
        self, event_id: str, observer_name: str, scores: Mapping[str, float]
    ) -> None:
        event = self._event(event_id, observer_name)
        normalized = self._scores(scores)
        if observer_name in event.unavailable:
            raise ValueError("conflicting observer availability observation")
        existing = event.observers.get(observer_name)
        if existing is not None and existing != normalized:
            raise ValueError("conflicting duplicate observer observation")
        event.observers[observer_name] = normalized

    def record_unavailable(
        self, event_id: str, observer_name: str, failure_kind: str
    ) -> None:
        if not isinstance(failure_kind, str) or not failure_kind:
            raise ValueError("observer failure kind is required")
        event = self._event(event_id, observer_name)
        if observer_name in event.observers:
            raise ValueError("conflicting observer availability observation")
        existing = event.unavailable.get(observer_name)
        if existing is not None and existing != failure_kind:
            raise ValueError("conflicting duplicate observer failure")
        event.unavailable[observer_name] = failure_kind

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    def _metric(
        self, events: list[_Event], observer_name: str, dimension: str
    ) -> dict[str, object]:
        primary: list[float] = []
        observer: list[float] = []
        unavailable: Counter[str] = Counter()
        for event in events:
            if observer_name in event.observers:
                primary.append(event.primary[dimension])
                observer.append(event.observers[observer_name][dimension])
            elif observer_name in event.unavailable:
                unavailable[event.unavailable[observer_name]] += 1
        offsets = [right - left for left, right in zip(primary, observer, strict=True)]
        absolute = [abs(value) for value in offsets]
        return {
            "comparable_count": len(primary),
            "primary_mean": self._mean(primary),
            "observer_mean": self._mean(observer),
            "signed_offset_mean": self._mean(offsets),
            "mean_absolute_delta": self._mean(absolute),
            "max_absolute_delta": max(absolute) if absolute else None,
            "unavailable_count": dict(sorted(unavailable.items())),
        }

    def _panel(self, events: list[_Event], dimension: str) -> dict[str, object]:
        ranges: list[float] = []
        for event in events:
            scores = [event.primary[dimension]] + [
                event.observers[name][dimension]
                for name, _ in self.observers
                if name in event.observers
            ]
            if len(scores) >= 2:
                ranges.append(max(scores) - min(scores))
        return {
            "panel_event_count": len(ranges),
            "mean_event_range": self._mean(ranges),
            "max_event_range": max(ranges) if ranges else None,
        }

    def report(
        self,
        *,
        campaign_id: str,
        profile_id: str,
        profile_snapshot_digest: str,
        collection_state: str,
    ) -> dict[str, object]:
        if collection_state not in {"complete", "budget_exhausted"}:
            raise ValueError("invalid agreement collection state")
        partitions = sorted({event.partition for event in self.events.values()})
        event_counts: dict[str, int] = {}
        metrics: dict[str, object] = {}
        for partition in partitions:
            events = [
                self.events[event_id]
                for event_id in sorted(self.events)
                if self.events[event_id].partition == partition
            ]
            event_counts[partition] = len(events)
            metrics[partition] = {
                dimension: {
                    "observers": {
                        name: self._metric(events, name, dimension)
                        for name, _ in self.observers
                    },
                    "panel": self._panel(events, dimension),
                }
                for dimension in self.dimensions
            }
        availability: dict[str, dict[str, int]] = {}
        for name, _ in self.observers:
            counts = Counter(
                event.unavailable[name]
                for event in self.events.values()
                if name in event.unavailable
            )
            if counts:
                availability[name] = dict(sorted(counts.items()))
        fields: dict[str, object] = {
            "protocol": "kpo.evaluator-agreement/v1",
            "campaign_id": campaign_id,
            "profile_id": profile_id,
            "profile_snapshot_digest": profile_snapshot_digest,
            "primary_evaluator_id": self.primary_evaluator_id,
            "observers": [
                {"name": name, "evaluator_id": identity}
                for name, identity in self.observers
            ],
            "rubric_version": self.rubric_version,
            "dimensions": list(self.dimensions),
            "collection_state": collection_state,
            "event_count": len(self.events),
            "event_counts": event_counts,
            "availability": availability,
            "metrics": metrics,
        }
        return fields | {"report_digest": canonical_digest(fields)}


def agreement_summary(report: Mapping[str, object]) -> dict[str, object]:
    dimensions = report["dimensions"]
    assert isinstance(dimensions, list)
    maxima: dict[str, float | None] = {str(dimension): None for dimension in dimensions}
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    for partition_metrics in metrics.values():
        assert isinstance(partition_metrics, dict)
        for dimension, dimension_metrics in partition_metrics.items():
            assert isinstance(dimension_metrics, dict)
            panel = dimension_metrics["panel"]
            assert isinstance(panel, dict)
            value = panel["max_event_range"]
            if value is not None:
                current = maxima[str(dimension)]
                maxima[str(dimension)] = (
                    value if current is None else max(current, value)
                )
    availability = report["availability"]
    assert isinstance(availability, dict)
    return {
        "observer_count": len(report["observers"]),  # type: ignore[arg-type]
        "event_count": report["event_count"],
        "maximum_panel_range": dict(sorted(maxima.items())),
        "unavailable_totals": availability,
    }


def persist_agreement_report(
    data_home: Path,
    campaign_id: str,
    report: dict[str, object],
) -> dict[str, object]:
    path = data_home.resolve() / "campaigns" / campaign_id / "evaluator-agreement.json"
    relative = path.relative_to(data_home.resolve()).as_posix()
    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            temporary = Path(tmp.name)
        os.replace(temporary, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if loaded != report:
            raise ValueError("agreement report reload mismatch")
        fields = dict(loaded)
        digest = fields.pop("report_digest", None)
        if digest != canonical_digest(fields):
            raise ValueError("agreement report canonical digest mismatch")
    except (OSError, ValueError, TypeError, KeyError) as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise AgreementPersistenceError("agreement report persistence failed") from error
    return {
        "evaluator_agreement_path": relative,
        "evaluator_agreement_digest": hashlib.sha256(payload).hexdigest(),
        "evaluator_agreement_summary": agreement_summary(report),
    }


_EVENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_FIELDS = {
    "protocol",
    "campaign_id",
    "profile_snapshot_digest",
    "primary_evaluator_id",
    "observers",
    "rubric_version",
    "dimensions",
    "events",
    "checkpoint_digest",
}
_CHECKPOINT_EVENT_FIELDS = {"partition", "primary", "observers"}


def _checkpoint_document(
    collector: AgreementCollector,
    *,
    campaign_id: str,
    profile_snapshot_digest: str,
) -> dict[str, object]:
    events: dict[str, object] = {}
    for event_id in sorted(collector.events):
        event = collector.events[event_id]
        observer_values: dict[str, object] = {}
        for name, _ in collector.observers:
            if name in event.observers:
                observer_values[name] = {
                    "state": "completed",
                    "scores": event.observers[name],
                }
            elif name in event.unavailable:
                observer_values[name] = {
                    "state": "unavailable",
                    "failure_kind": event.unavailable[name],
                }
            else:
                raise AgreementCheckpointError(
                    "agreement event has an incomplete observer slot"
                )
        events[event_id] = {
            "partition": event.partition,
            "primary": event.primary,
            "observers": observer_values,
        }
    fields: dict[str, object] = {
        "protocol": "kpo.evaluator-agreement-checkpoint/v1",
        "campaign_id": campaign_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "primary_evaluator_id": collector.primary_evaluator_id,
        "observers": [
            {"name": name, "evaluator_id": identity}
            for name, identity in collector.observers
        ],
        "rubric_version": collector.rubric_version,
        "dimensions": list(collector.dimensions),
        "events": events,
    }
    return fields | {"checkpoint_digest": canonical_digest(fields)}


def agreement_checkpoint_path(data_home: Path, campaign_id: str) -> Path:
    return (
        data_home.resolve()
        / "campaigns"
        / campaign_id
        / "evaluator-agreement.checkpoint.json"
    )


def cleanup_agreement_checkpoint(data_home: Path, campaign_id: str) -> None:
    path = agreement_checkpoint_path(data_home, campaign_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.warning(
            "could not remove completed agreement checkpoint for campaign %s: %s",
            campaign_id,
            type(error).__name__,
        )


def persist_agreement_checkpoint(
    data_home: Path,
    campaign_id: str,
    collector: AgreementCollector,
    *,
    profile_snapshot_digest: str,
) -> Path:
    path = agreement_checkpoint_path(data_home, campaign_id)
    document = _checkpoint_document(
        collector,
        campaign_id=campaign_id,
        profile_snapshot_digest=profile_snapshot_digest,
    )
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            temporary = Path(tmp.name)
        os.replace(temporary, path)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise AgreementCheckpointError(
            "agreement checkpoint persistence failed"
        ) from error
    return path


def load_agreement_checkpoint(
    data_home: Path,
    campaign_id: str,
    *,
    primary_evaluator_id: str,
    observers: tuple[tuple[str, str], ...],
    rubric_version: str,
    dimensions: tuple[str, ...],
    profile_snapshot_digest: str,
) -> AgreementCollector:
    from kpo.models import Partition

    path = agreement_checkpoint_path(data_home, campaign_id)
    if not path.is_file():
        raise AgreementCheckpointError("agreement checkpoint is missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgreementCheckpointError("invalid agreement checkpoint JSON") from error
    try:
        checkpoint = _strict_object(
            raw, _CHECKPOINT_FIELDS, "agreement checkpoint"
        )
        fields = dict(checkpoint)
        digest = fields.pop("checkpoint_digest")
        if digest != canonical_digest(fields):
            raise AgreementCheckpointError("agreement checkpoint digest mismatch")
        expected_observers = [
            {"name": name, "evaluator_id": identity}
            for name, identity in sorted(observers)
        ]
        if (
            checkpoint["protocol"]
            != "kpo.evaluator-agreement-checkpoint/v1"
            or checkpoint["campaign_id"] != campaign_id
            or checkpoint["profile_snapshot_digest"] != profile_snapshot_digest
            or checkpoint["primary_evaluator_id"] != primary_evaluator_id
            or checkpoint["observers"] != expected_observers
            or checkpoint["rubric_version"] != rubric_version
            or checkpoint["dimensions"] != sorted(dimensions)
        ):
            raise AgreementCheckpointError("agreement checkpoint identity mismatch")
        collector = AgreementCollector(
            primary_evaluator_id=primary_evaluator_id,
            observers=observers,
            rubric_version=rubric_version,
            dimensions=dimensions,
        )
        events = checkpoint["events"]
        if not isinstance(events, dict):
            raise AgreementCheckpointError("agreement checkpoint events are invalid")
        valid_partitions = {partition.value for partition in Partition}
        for event_id in sorted(events):
            if not isinstance(event_id, str) or not _EVENT_DIGEST.fullmatch(event_id):
                raise AgreementCheckpointError("agreement checkpoint event ID is invalid")
            event = _strict_object(
                events[event_id], _CHECKPOINT_EVENT_FIELDS, "agreement checkpoint event"
            )
            partition = event["partition"]
            if partition not in valid_partitions:
                raise AgreementCheckpointError(
                    "agreement checkpoint partition is invalid"
                )
            primary = event["primary"]
            if not isinstance(primary, dict):
                raise AgreementCheckpointError(
                    "agreement checkpoint primary scores are invalid"
                )
            collector.record_primary(event_id, str(partition), primary)
            observer_values = event["observers"]
            if not isinstance(observer_values, dict) or set(observer_values) != {
                name for name, _ in observers
            }:
                raise AgreementCheckpointError(
                    "agreement checkpoint observer slots are invalid"
                )
            for name, _ in sorted(observers):
                value = observer_values[name]
                if not isinstance(value, dict):
                    raise AgreementCheckpointError(
                        "agreement checkpoint observer slot is invalid"
                    )
                if value.get("state") == "completed":
                    completed = _strict_object(
                        value, {"state", "scores"}, "agreement checkpoint observer"
                    )
                    scores = completed["scores"]
                    if not isinstance(scores, dict):
                        raise AgreementCheckpointError(
                            "agreement checkpoint observer scores are invalid"
                        )
                    collector.record_observer(event_id, name, scores)
                elif value.get("state") == "unavailable":
                    unavailable = _strict_object(
                        value,
                        {"state", "failure_kind"},
                        "agreement checkpoint observer",
                    )
                    kind = unavailable["failure_kind"]
                    if not isinstance(kind, str):
                        raise AgreementCheckpointError(
                            "agreement checkpoint failure kind is invalid"
                        )
                    collector.record_unavailable(event_id, name, kind)
                else:
                    raise AgreementCheckpointError(
                        "agreement checkpoint observer state is invalid"
                    )
        return collector
    except AgreementCheckpointError:
        raise
    except ValueError as error:
        raise AgreementCheckpointError(str(error)) from error
    except (KeyError, TypeError) as error:
        raise AgreementCheckpointError("agreement checkpoint is invalid") from error


_REPORT_FIELDS = {
    "protocol",
    "campaign_id",
    "profile_id",
    "profile_snapshot_digest",
    "primary_evaluator_id",
    "observers",
    "rubric_version",
    "dimensions",
    "collection_state",
    "event_count",
    "event_counts",
    "availability",
    "metrics",
    "report_digest",
}
_OBSERVER_FIELDS = {"name", "evaluator_id"}
_DIMENSION_FIELDS = {"observers", "panel"}
_OBSERVER_METRIC_FIELDS = {
    "comparable_count",
    "primary_mean",
    "observer_mean",
    "signed_offset_mean",
    "mean_absolute_delta",
    "max_absolute_delta",
    "unavailable_count",
}
_PANEL_FIELDS = {"panel_event_count", "mean_event_range", "max_event_range"}


def _strict_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing {label} fields: {sorted(missing)}")
    return value


def _count(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_number(
    value: object, label: str, *, minimum: float, maximum: float
) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} is outside its finite range")
    return float(value)


def _validate_failure_counts(value: object, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    for kind, count in value.items():
        if not isinstance(kind, str) or not kind or _count(count, label) < 1:
            raise ValueError(f"{label} contains an invalid failure count")


def _validate_report_schema(report: object) -> dict[str, object]:
    value = _strict_object(report, _REPORT_FIELDS, "agreement report")
    if value["protocol"] != "kpo.evaluator-agreement/v1":
        raise ValueError("unsupported agreement report protocol")
    for field in (
        "campaign_id",
        "profile_id",
        "profile_snapshot_digest",
        "primary_evaluator_id",
        "rubric_version",
        "report_digest",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"agreement report {field} is invalid")
    if value["collection_state"] not in {"complete", "budget_exhausted"}:
        raise ValueError("agreement report collection_state is invalid")
    dimensions = value["dimensions"]
    if (
        not isinstance(dimensions, list)
        or not dimensions
        or any(not isinstance(item, str) or not item for item in dimensions)
        or dimensions != sorted(set(dimensions))
    ):
        raise ValueError("agreement report dimensions are invalid")
    observers = value["observers"]
    if not isinstance(observers, list) or not observers:
        raise ValueError("agreement report observers are invalid")
    observer_names: list[str] = []
    observer_ids: list[str] = []
    for raw in observers:
        observer = _strict_object(raw, _OBSERVER_FIELDS, "agreement observer")
        name = observer["name"]
        identity = observer["evaluator_id"]
        if not isinstance(name, str) or not isinstance(identity, str):
            raise ValueError("agreement observer identity is invalid")
        observer_names.append(name)
        observer_ids.append(identity)
    if observer_names != sorted(set(observer_names)) or len(set(observer_ids)) != len(
        observer_ids
    ):
        raise ValueError("agreement observers are not unique and sorted")
    event_count = _count(value["event_count"], "event_count")
    event_counts = value["event_counts"]
    if not isinstance(event_counts, dict) or any(
        not isinstance(partition, str) for partition in event_counts
    ):
        raise ValueError("agreement event_counts are invalid")
    counted_events = sum(
        _count(count, "partition event count") for count in event_counts.values()
    )
    if counted_events != event_count:
        raise ValueError("agreement event counts do not sum to event_count")
    availability = value["availability"]
    if not isinstance(availability, dict) or not set(availability) <= set(observer_names):
        raise ValueError("agreement availability observers are invalid")
    for name, counts in availability.items():
        _validate_failure_counts(counts, f"availability for {name}")
    metrics = value["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(event_counts):
        raise ValueError("agreement metric partitions are invalid")
    for partition, raw_partition in metrics.items():
        if not isinstance(raw_partition, dict) or set(raw_partition) != set(dimensions):
            raise ValueError(f"agreement dimensions for {partition} are invalid")
        for dimension, raw_dimension in raw_partition.items():
            dimension_value = _strict_object(
                raw_dimension, _DIMENSION_FIELDS, "agreement dimension metric"
            )
            raw_observer_metrics = dimension_value["observers"]
            if not isinstance(raw_observer_metrics, dict) or set(
                raw_observer_metrics
            ) != set(observer_names):
                raise ValueError("agreement observer metrics are invalid")
            for name, raw_metric in raw_observer_metrics.items():
                metric = _strict_object(
                    raw_metric, _OBSERVER_METRIC_FIELDS, "agreement observer metric"
                )
                comparable = _count(metric["comparable_count"], "comparable_count")
                values = (
                    _optional_number(
                        metric["primary_mean"], "primary_mean", minimum=0, maximum=1
                    ),
                    _optional_number(
                        metric["observer_mean"], "observer_mean", minimum=0, maximum=1
                    ),
                    _optional_number(
                        metric["signed_offset_mean"],
                        "signed_offset_mean",
                        minimum=-1,
                        maximum=1,
                    ),
                    _optional_number(
                        metric["mean_absolute_delta"],
                        "mean_absolute_delta",
                        minimum=0,
                        maximum=1,
                    ),
                    _optional_number(
                        metric["max_absolute_delta"],
                        "max_absolute_delta",
                        minimum=0,
                        maximum=1,
                    ),
                )
                if comparable == 0 and any(item is not None for item in values):
                    raise ValueError("agreement empty comparison null contract is invalid")
                if comparable > 0 and any(item is None for item in values):
                    raise ValueError("agreement comparison evidence is incomplete")
                _validate_failure_counts(
                    metric["unavailable_count"], f"unavailable_count for {name}"
                )
            panel = _strict_object(
                dimension_value["panel"], _PANEL_FIELDS, "agreement panel metric"
            )
            panel_count = _count(panel["panel_event_count"], "panel_event_count")
            panel_values = (
                _optional_number(
                    panel["mean_event_range"],
                    "mean_event_range",
                    minimum=0,
                    maximum=1,
                ),
                _optional_number(
                    panel["max_event_range"],
                    "max_event_range",
                    minimum=0,
                    maximum=1,
                ),
            )
            if panel_count == 0 and any(item is not None for item in panel_values):
                raise ValueError("agreement empty panel null contract is invalid")
            if panel_count > 0 and any(item is None for item in panel_values):
                raise ValueError("agreement panel evidence is incomplete")
    fields = dict(value)
    report_digest = fields.pop("report_digest")
    if report_digest != canonical_digest(fields):
        raise ValueError("agreement report canonical digest mismatch")
    return value


def load_agreement_report(profile: object, campaign_id: str) -> dict[str, object]:
    from kpo.campaign_profile import validate_campaign_id

    validate_campaign_id(campaign_id)
    data_home = profile.base.data_home.resolve()
    state_path = data_home / "campaigns" / campaign_id / "campaign.json"
    if not state_path.is_file():
        raise KeyError(f"unknown campaign: {campaign_id}")
    if not profile.base.observer_evaluators:
        raise ValueError("campaign profile has no observer evaluators")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("campaign_id") != campaign_id:
        raise ValueError("agreement campaign state identity mismatch")
    relative_raw = state.get("evaluator_agreement_path")
    if not isinstance(relative_raw, str):
        raise ValueError("agreement report path is missing")
    relative = Path(relative_raw)
    expected = Path("campaigns") / campaign_id / "evaluator-agreement.json"
    if relative.is_absolute() or ".." in relative.parts or relative != expected:
        raise ValueError("agreement report path is invalid")
    path = (data_home / relative).resolve()
    if data_home not in path.parents or not path.is_file():
        raise ValueError("agreement report path is missing or escapes data_home")
    payload = path.read_bytes()
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid agreement report JSON") from error
    report = _validate_report_schema(raw)
    byte_digest = hashlib.sha256(payload).hexdigest()
    if state.get("evaluator_agreement_digest") != byte_digest:
        raise ValueError("agreement report state digest binding mismatch")
    expected_observers = [
        {"name": observer.name, "evaluator_id": observer.config.identity}
        for observer in profile.base.observer_evaluators
    ]
    if (
        report["campaign_id"] != campaign_id
        or report["profile_id"] != profile.base.profile_id
        or report["profile_snapshot_digest"] != state.get("profile_snapshot_digest")
        or report["primary_evaluator_id"] != profile.base.evaluator.identity
        or report["observers"] != expected_observers
        or report["rubric_version"] != profile.base.rubric.rubric_version
        or report["dimensions"] != sorted(profile.base.rubric.dimension_names)
    ):
        raise ValueError("agreement report profile identity mismatch")
    terminal_state = state.get("state")
    expected_collection_state = (
        "budget_exhausted"
        if terminal_state == "budget_exhausted"
        else "complete"
        if terminal_state in {"promotion_previewed", "no_patchable_diagnosis"}
        else None
    )
    if (
        expected_collection_state is None
        or report["collection_state"] != expected_collection_state
    ):
        raise ValueError("agreement report campaign terminal state mismatch")
    if state.get("evaluator_agreement_summary") != agreement_summary(report):
        raise ValueError("agreement report state summary binding mismatch")
    return report
