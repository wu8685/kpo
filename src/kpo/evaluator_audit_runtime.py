from __future__ import annotations

import json
import logging
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from kpo.digest import canonical_digest
from kpo.calibration_metrics import CalibrationCollector
from kpo.profile import CommandProviderConfig
from kpo.provider import _invoke
from kpo.provider import CommandEvaluatorProvider, ProviderFailure
from kpo.evaluator_calibration import (
    build_anchor_evaluator_request,
    load_anchor_approval,
)
from kpo.evaluator_calibration_report import (
    EvaluatorCalibrationPersistenceError,
    build_calibration_documents,
    calibration_summary,
    load_calibration_artifacts,
    persist_calibration_artifacts,
)

if TYPE_CHECKING:
    from kpo.campaign_profile import CampaignProfile
    from kpo.integrity import IntegrityMonitor


_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_BUDGET_FIELDS = {
    "protocol",
    "campaign_id",
    "max_calls",
    "call_count",
    "budget_digest",
}
_CHECKPOINT_FIELDS = {
    "protocol",
    "campaign_id",
    "profile_snapshot_digest",
    "anchor_set_digest",
    "rubric_version",
    "dimensions",
    "slots",
    "max_provider_calls",
    "observations",
    "checkpoint_digest",
}
_OBSERVATION_FIELDS = {"anchor_digest", "expected_scores", "slots"}
_SLOT_RECORD_FIELDS = {"slot", "identity", "configuration_digest"}
_LOGGER = logging.getLogger(__name__)


class EvaluatorAuditBudgetExhausted(RuntimeError):
    pass


class EvaluatorAuditBudgetError(ValueError):
    pass


class EvaluatorCalibrationCheckpointError(ValueError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
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


def _budget_document(
    *, campaign_id: str, max_calls: int, call_count: int
) -> dict[str, Any]:
    fields = {
        "protocol": "kpo.evaluator-audit-budget/v1",
        "campaign_id": campaign_id,
        "max_calls": max_calls,
        "call_count": call_count,
    }
    return fields | {"budget_digest": canonical_digest(fields)}


class EvaluatorAuditCallExecutor:
    def __init__(
        self,
        data_home: Path,
        campaign_id: str,
        *,
        max_calls: int,
        integrity_check: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(campaign_id, str) or not _CAMPAIGN_ID.fullmatch(campaign_id):
            raise ValueError("campaign ID must be a safe path component")
        if (
            not isinstance(max_calls, int)
            or isinstance(max_calls, bool)
            or max_calls < 1
        ):
            raise ValueError("max_calls must be positive")
        self.root = data_home.expanduser().resolve() / "campaigns" / campaign_id
        self.state_path = self.root / "evaluator-audit-call-budget.json"
        self.campaign_id = campaign_id
        self.max_calls = max_calls
        self.integrity_check = integrity_check
        if self.state_path.exists():
            self.call_count = self._load()
        else:
            self.call_count = 0
            self._persist()

    def _load(self) -> int:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvaluatorAuditBudgetError("invalid evaluator audit budget JSON") from error
        if not isinstance(raw, dict) or set(raw) != _BUDGET_FIELDS:
            raise EvaluatorAuditBudgetError(
                "evaluator audit budget fields do not match protocol"
            )
        if raw["protocol"] != "kpo.evaluator-audit-budget/v1":
            raise EvaluatorAuditBudgetError("evaluator audit budget protocol mismatch")
        if raw["campaign_id"] != self.campaign_id:
            raise EvaluatorAuditBudgetError("evaluator audit budget campaign mismatch")
        if raw["max_calls"] != self.max_calls:
            raise EvaluatorAuditBudgetError(
                "evaluator audit budget max_calls changed during resume"
            )
        call_count = raw["call_count"]
        if (
            not isinstance(call_count, int)
            or isinstance(call_count, bool)
            or not 0 <= call_count <= self.max_calls
        ):
            raise EvaluatorAuditBudgetError("invalid evaluator audit call_count")
        fields = dict(raw)
        digest = fields.pop("budget_digest")
        if digest != canonical_digest(fields):
            raise EvaluatorAuditBudgetError("evaluator audit budget digest mismatch")
        return call_count

    def _persist(self) -> None:
        try:
            _atomic_json(
                self.state_path,
                _budget_document(
                    campaign_id=self.campaign_id,
                    max_calls=self.max_calls,
                    call_count=self.call_count,
                ),
            )
        except OSError as error:
            raise EvaluatorAuditBudgetError(
                "evaluator audit budget persistence failed"
            ) from error

    def invoke(
        self,
        config: CommandProviderConfig,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if self.call_count >= self.max_calls:
            raise EvaluatorAuditBudgetExhausted(
                "evaluator audit provider call budget exhausted"
            )
        self.call_count += 1
        self._persist()
        if self.integrity_check is not None:
            self.integrity_check()
        try:
            return _invoke(config, "evaluator", request)
        finally:
            if self.integrity_check is not None:
                self.integrity_check()


def _checkpoint_path(data_home: Path, campaign_id: str) -> Path:
    return (
        data_home.expanduser().resolve()
        / "campaigns"
        / campaign_id
        / "evaluator-calibration.checkpoint.json"
    )


def _checkpoint_document(
    campaign_id: str,
    collector: CalibrationCollector,
    *,
    profile_snapshot_digest: str,
    anchor_set_digest: str,
    rubric_version: str,
    slot_records: tuple[dict[str, str], ...],
    max_provider_calls: int,
) -> dict[str, Any]:
    configuration_digests = {
        record["slot"]: record["configuration_digest"] for record in slot_records
    }
    observations: list[dict[str, Any]] = []
    for raw in collector.observations():
        slots = raw["slots"]
        assert isinstance(slots, dict)
        observations.append(
            {
                "anchor_digest": raw["anchor_digest"],
                "expected_scores": raw["expected_scores"],
                "slots": {
                    slot: value
                    | {"configuration_digest": configuration_digests[slot]}
                    for slot, value in slots.items()
                },
            }
        )
    fields: dict[str, Any] = {
        "protocol": "kpo.evaluator-calibration-checkpoint/v1",
        "campaign_id": campaign_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "anchor_set_digest": anchor_set_digest,
        "rubric_version": rubric_version,
        "dimensions": list(collector.dimensions),
        "slots": list(slot_records),
        "max_provider_calls": max_provider_calls,
        "observations": observations,
    }
    return fields | {"checkpoint_digest": canonical_digest(fields)}


def persist_calibration_checkpoint(
    data_home: Path,
    campaign_id: str,
    collector: CalibrationCollector,
    *,
    profile_snapshot_digest: str,
    anchor_set_digest: str,
    rubric_version: str,
    slot_records: tuple[dict[str, str], ...],
    max_provider_calls: int,
) -> Path:
    path = _checkpoint_path(data_home, campaign_id)
    try:
        _atomic_json(
            path,
            _checkpoint_document(
                campaign_id,
                collector,
                profile_snapshot_digest=profile_snapshot_digest,
                anchor_set_digest=anchor_set_digest,
                rubric_version=rubric_version,
                slot_records=slot_records,
                max_provider_calls=max_provider_calls,
            ),
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint persistence failed"
        ) from error
    return path


def _validate_slot_records(
    raw: Any, expected: tuple[dict[str, str], ...]
) -> None:
    if not isinstance(raw, list) or raw != list(expected):
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint identity mismatch"
        )
    for record in raw:
        if not isinstance(record, dict) or set(record) != _SLOT_RECORD_FIELDS:
            raise EvaluatorCalibrationCheckpointError(
                "evaluator calibration checkpoint slot fields mismatch"
            )


def load_calibration_checkpoint(
    data_home: Path,
    campaign_id: str,
    *,
    anchors: tuple[tuple[str, dict[str, float]], ...]
    | tuple[tuple[str, Mapping[str, float]], ...],
    dimensions: tuple[str, ...],
    profile_snapshot_digest: str,
    anchor_set_digest: str,
    rubric_version: str,
    slot_records: tuple[dict[str, str], ...],
    max_provider_calls: int,
) -> CalibrationCollector:
    path = _checkpoint_path(data_home, campaign_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluatorCalibrationCheckpointError(
            "invalid evaluator calibration checkpoint JSON"
        ) from error
    if not isinstance(raw, dict) or set(raw) != _CHECKPOINT_FIELDS:
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint fields do not match protocol"
        )
    fields = dict(raw)
    digest = fields.pop("checkpoint_digest")
    if digest != canonical_digest(fields):
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint digest mismatch"
        )
    expected_identity = {
        "protocol": "kpo.evaluator-calibration-checkpoint/v1",
        "campaign_id": campaign_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "anchor_set_digest": anchor_set_digest,
        "rubric_version": rubric_version,
        "dimensions": sorted(dimensions),
        "max_provider_calls": max_provider_calls,
    }
    if any(raw[key] != value for key, value in expected_identity.items()):
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint identity mismatch"
        )
    _validate_slot_records(raw["slots"], slot_records)

    slot_names = tuple(record["slot"] for record in slot_records)
    configuration_digests = {
        record["slot"]: record["configuration_digest"] for record in slot_records
    }
    collector = CalibrationCollector(
        anchors=anchors,
        slots=slot_names,
        dimensions=dimensions,
    )
    observations = raw["observations"]
    if not isinstance(observations, list):
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint observations must be an array"
        )
    expected_rows = collector.observations()
    if len(observations) != len(expected_rows):
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint anchor identity mismatch"
        )
    had_pending = False
    try:
        for raw_row, expected_row in zip(observations, expected_rows, strict=True):
            if not isinstance(raw_row, dict) or set(raw_row) != _OBSERVATION_FIELDS:
                raise EvaluatorCalibrationCheckpointError(
                    "evaluator calibration checkpoint observation fields mismatch"
                )
            if (
                raw_row["anchor_digest"] != expected_row["anchor_digest"]
                or raw_row["expected_scores"] != expected_row["expected_scores"]
            ):
                raise EvaluatorCalibrationCheckpointError(
                    "evaluator calibration checkpoint anchor identity mismatch"
                )
            raw_slots = raw_row["slots"]
            if not isinstance(raw_slots, dict) or not set(raw_slots) <= set(slot_names):
                raise EvaluatorCalibrationCheckpointError(
                    "evaluator calibration checkpoint evaluator slot mismatch"
                )
            for slot in slot_names:
                if slot not in raw_slots:
                    continue
                value = raw_slots[slot]
                if not isinstance(value, dict):
                    raise EvaluatorCalibrationCheckpointError(
                        "evaluator calibration checkpoint slot must be an object"
                    )
                if value.get("configuration_digest") != configuration_digests[slot]:
                    raise EvaluatorCalibrationCheckpointError(
                        "evaluator calibration checkpoint configuration mismatch"
                    )
                state = value.get("state")
                anchor_digest_value = str(raw_row["anchor_digest"])
                if state == "pending" and set(value) == {
                    "state",
                    "configuration_digest",
                }:
                    collector.mark_pending(anchor_digest_value, slot)
                    collector.record_unavailable(
                        anchor_digest_value, slot, "interrupted_unknown"
                    )
                    had_pending = True
                elif state == "completed" and set(value) == {
                    "state",
                    "scores",
                    "configuration_digest",
                }:
                    collector.record_scores(
                        anchor_digest_value, slot, value["scores"]
                    )
                elif state == "unavailable" and set(value) == {
                    "state",
                    "failure_kind",
                    "configuration_digest",
                }:
                    collector.record_unavailable(
                        anchor_digest_value, slot, value["failure_kind"]
                    )
                else:
                    raise EvaluatorCalibrationCheckpointError(
                        "evaluator calibration checkpoint slot state mismatch"
                    )
    except (TypeError, ValueError) as error:
        if isinstance(error, EvaluatorCalibrationCheckpointError):
            raise
        raise EvaluatorCalibrationCheckpointError(
            "evaluator calibration checkpoint observation mismatch"
        ) from error
    if had_pending:
        persist_calibration_checkpoint(
            data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=anchor_set_digest,
            rubric_version=rubric_version,
            slot_records=slot_records,
            max_provider_calls=max_provider_calls,
        )
    return collector


def cleanup_calibration_checkpoint(data_home: Path, campaign_id: str) -> None:
    path = _checkpoint_path(data_home, campaign_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning(
            "could not remove completed calibration checkpoint: %s", path
        )


def _configuration_digest(
    profile: CampaignProfile, slot: str, config: CommandProviderConfig
) -> str:
    working_directory: str | None = None
    if config.working_directory is not None:
        try:
            working_directory = config.working_directory.resolve().relative_to(
                profile.base.data_home.resolve()
            ).as_posix()
        except ValueError as error:
            raise ValueError(
                "evaluator working directory must be inside data_home"
            ) from error
    return canonical_digest(
        {
            "slot": slot,
            "identity": config.identity,
            "command": config.command,
            "timeout_seconds": config.timeout_seconds,
            "pass_env_names": config.pass_env,
            "working_directory": working_directory,
            "executable_digest": hashlib.sha256(
                Path(config.command[0]).read_bytes()
            ).hexdigest(),
        }
    )


def evaluator_slot_records(
    profile: CampaignProfile,
) -> tuple[dict[str, str], ...]:
    records = [
        {
            "slot": "primary",
            "identity": profile.base.evaluator.identity,
            "configuration_digest": _configuration_digest(
                profile, "primary", profile.base.evaluator
            ),
        }
    ]
    records.extend(
        {
            "slot": f"observer:{observer.name}",
            "identity": observer.config.identity,
            "configuration_digest": _configuration_digest(
                profile, f"observer:{observer.name}", observer.config
            ),
        }
        for observer in profile.base.observer_evaluators
    )
    return tuple(records)


class _AuditInvocationAdapter:
    def __init__(self, executor: EvaluatorAuditCallExecutor) -> None:
        self.executor = executor

    def invoke(
        self,
        config: CommandProviderConfig,
        role: str,
        request: dict[str, Any],
        *,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if role != "evaluator" or context:
            raise ValueError("anchor audit supports only isolated evaluator calls")
        return self.executor.invoke(config, request)


def _artifact_binding(
    profile: CampaignProfile,
    campaign_id: str,
    report: dict[str, Any],
    observations: dict[str, Any],
) -> dict[str, object]:
    data_home = profile.base.data_home.resolve()
    root = data_home / "campaigns" / campaign_id
    report_path = root / "evaluator-calibration.json"
    observation_path = root / "evaluator-calibration-observations.json"
    return {
        "evaluator_calibration_path": report_path.relative_to(data_home).as_posix(),
        "evaluator_calibration_digest": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
        "evaluator_calibration_observations_path": observation_path.relative_to(
            data_home
        ).as_posix(),
        "evaluator_calibration_observations_digest": hashlib.sha256(
            observation_path.read_bytes()
        ).hexdigest(),
        "evaluator_calibration_report_digest": report["report_digest"],
        "evaluator_calibration_summary": calibration_summary(report),
    }


def run_evaluator_calibration(
    profile: CampaignProfile,
    campaign_id: str,
    *,
    profile_snapshot_digest: str,
    integrity_monitor: IntegrityMonitor,
) -> dict[str, object]:
    audit = profile.evaluator_audit
    if audit is None:
        return {}
    _, approved_record_digest = load_anchor_approval(profile)
    slot_records = evaluator_slot_records(profile)
    slots = tuple(record["slot"] for record in slot_records)
    anchors = tuple(
        (anchor.blinded_digest, anchor.expected_scores) for anchor in audit.anchors
    )
    report_path = (
        profile.base.data_home
        / "campaigns"
        / campaign_id
        / "evaluator-calibration.json"
    )
    observation_path = report_path.with_name(
        "evaluator-calibration-observations.json"
    )
    if report_path.exists() and observation_path.exists():
        report, observations = load_calibration_artifacts(
            profile.base.data_home, campaign_id
        )
        expected_identity = {
            "campaign_id": campaign_id,
            "profile_id": profile.base.profile_id,
            "profile_snapshot_digest": profile_snapshot_digest,
            "anchor_set_digest": audit.anchor_set_digest,
            "anchor_request_set_digest": audit.anchor_request_set_digest,
            "approved_record_digest": approved_record_digest,
            "rubric_version": profile.base.rubric.rubric_version,
            "dimensions": sorted(profile.base.rubric.dimension_names),
            "drift_thresholds": dict(sorted(audit.drift_thresholds.items())),
            "slots": list(slot_records),
        }
        if any(report[key] != value for key, value in expected_identity.items()):
            raise EvaluatorCalibrationPersistenceError(
                "persisted evaluator calibration identity mismatch"
            )
        return _artifact_binding(profile, campaign_id, report, observations)

    checkpoint_path = _checkpoint_path(profile.base.data_home, campaign_id)
    if checkpoint_path.exists():
        collector = load_calibration_checkpoint(
            profile.base.data_home,
            campaign_id,
            anchors=anchors,
            dimensions=profile.base.rubric.dimension_names,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=audit.anchor_set_digest,
            rubric_version=profile.base.rubric.rubric_version,
            slot_records=slot_records,
            max_provider_calls=audit.max_provider_calls,
        )
    else:
        collector = CalibrationCollector(
            anchors=anchors,
            slots=slots,
            dimensions=profile.base.rubric.dimension_names,
        )
        persist_calibration_checkpoint(
            profile.base.data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=audit.anchor_set_digest,
            rubric_version=profile.base.rubric.rubric_version,
            slot_records=slot_records,
            max_provider_calls=audit.max_provider_calls,
        )

    executor = EvaluatorAuditCallExecutor(
        profile.base.data_home,
        campaign_id,
        max_calls=audit.max_provider_calls,
        integrity_check=integrity_monitor.verify,
    )
    configs = (profile.base.evaluator,) + tuple(
        observer.config for observer in profile.base.observer_evaluators
    )
    coordinates = tuple(
        (anchor, slot, config)
        for anchor in audit.anchors
        for slot, config in zip(slots, configs, strict=True)
    )
    budget_exhausted = False
    for index, (anchor, slot, config) in enumerate(coordinates):
        if collector.observation(anchor.blinded_digest, slot) is not None:
            continue
        if executor.call_count >= audit.max_provider_calls:
            for remaining_anchor, remaining_slot, _ in coordinates[index:]:
                if (
                    collector.observation(
                        remaining_anchor.blinded_digest, remaining_slot
                    )
                    is None
                ):
                    collector.record_unavailable(
                        remaining_anchor.blinded_digest,
                        remaining_slot,
                        "not_attempted_budget_exhausted",
                    )
            persist_calibration_checkpoint(
                profile.base.data_home,
                campaign_id,
                collector,
                profile_snapshot_digest=profile_snapshot_digest,
                anchor_set_digest=audit.anchor_set_digest,
                rubric_version=profile.base.rubric.rubric_version,
                slot_records=slot_records,
                max_provider_calls=audit.max_provider_calls,
            )
            budget_exhausted = True
            break
        collector.mark_pending(anchor.blinded_digest, slot)
        persist_calibration_checkpoint(
            profile.base.data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=audit.anchor_set_digest,
            rubric_version=profile.base.rubric.rubric_version,
            slot_records=slot_records,
            max_provider_calls=audit.max_provider_calls,
        )
        request = build_anchor_evaluator_request(profile, anchor)
        try:
            result = CommandEvaluatorProvider(
                config,
                profile.base.rubric,
                executor=_AuditInvocationAdapter(executor),
                context={},
            ).evaluate(request)
        except EvaluatorAuditBudgetExhausted:
            collector.record_unavailable(
                anchor.blinded_digest, slot, "not_attempted_budget_exhausted"
            )
            budget_exhausted = True
        except ProviderFailure as error:
            collector.record_unavailable(
                anchor.blinded_digest, slot, error.kind.value
            )
        else:
            collector.record_scores(
                anchor.blinded_digest,
                slot,
                {dimension.name: dimension.score for dimension in result.dimensions},
            )
        persist_calibration_checkpoint(
            profile.base.data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=audit.anchor_set_digest,
            rubric_version=profile.base.rubric.rubric_version,
            slot_records=slot_records,
            max_provider_calls=audit.max_provider_calls,
        )
        if budget_exhausted:
            for remaining_anchor, remaining_slot, _ in coordinates[index + 1 :]:
                if (
                    collector.observation(
                        remaining_anchor.blinded_digest, remaining_slot
                    )
                    is None
                ):
                    collector.record_unavailable(
                        remaining_anchor.blinded_digest,
                        remaining_slot,
                        "not_attempted_budget_exhausted",
                    )
            break

    if budget_exhausted:
        persist_calibration_checkpoint(
            profile.base.data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=profile_snapshot_digest,
            anchor_set_digest=audit.anchor_set_digest,
            rubric_version=profile.base.rubric.rubric_version,
            slot_records=slot_records,
            max_provider_calls=audit.max_provider_calls,
        )
    report, observations = build_calibration_documents(
        collector,
        campaign_id=campaign_id,
        profile_id=profile.base.profile_id,
        profile_snapshot_digest=profile_snapshot_digest,
        anchor_set_digest=audit.anchor_set_digest,
        anchor_request_set_digest=audit.anchor_request_set_digest,
        approved_record_digest=approved_record_digest,
        rubric_version=profile.base.rubric.rubric_version,
        drift_thresholds=audit.drift_thresholds,
        slot_records=slot_records,
        collection_state="budget_exhausted" if budget_exhausted else "complete",
    )
    return persist_calibration_artifacts(
        profile.base.data_home, campaign_id, report, observations
    )
