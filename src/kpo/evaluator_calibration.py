from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from kpo.digest import canonical_digest
from kpo.models import Rollout
from kpo.pipeline import EvaluatorRequest
from kpo.profile import (
    ExternalProfile,
    _source_path,
    _strict,
)

if TYPE_CHECKING:
    from kpo.campaign_profile import CampaignProfile


_ANCHOR_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_ANCHOR_FIELDS = {
    "protocol",
    "anchor_id",
    "rollout_path",
    "reference_ids",
    "expected_scores",
    "provenance",
}


@dataclass(frozen=True, slots=True)
class EvaluatorAnchor:
    anchor_id: str
    rollout_path: Path
    rollout_content: str
    rollout_digest: str
    reference_ids: tuple[str, ...]
    expected_scores: Mapping[str, float]
    provenance: tuple[str, ...]
    blinded_digest: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class EvaluatorAuditConfig:
    manifest_path: Path
    manifest_digest: str
    anchors: tuple[EvaluatorAnchor, ...]
    max_provider_calls: int
    drift_thresholds: Mapping[str, float]
    anchor_set_digest: str
    anchor_request_set_digest: str
    source_digests: Mapping[str, str]


_APPROVAL_RECORD_FIELDS = {
    "protocol",
    "profile_id",
    "rubric_version",
    "rubric_dimensions",
    "anchor_count",
    "manifest_digest",
    "anchor_source_digests",
    "reference_digests",
    "expected_score_digest",
    "anchor_set_digest",
    "anchor_request_set_digest",
    "approval_digest",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _required_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"{label} must be a non-empty string array")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate {label}")
    return tuple(value)


def _score_map(
    value: Any,
    *,
    dimensions: tuple[str, ...],
    label: str,
) -> Mapping[str, float]:
    if not isinstance(value, dict) or set(value) != set(dimensions):
        raise ValueError(f"{label} dimensions must match rubric")
    scores: dict[str, float] = {}
    for dimension in dimensions:
        score = value[dimension]
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError(f"{label}.{dimension} must be finite and in [0, 1]")
        scores[dimension] = float(score)
    return MappingProxyType(scores)


def _reference_material(
    profile: ExternalProfile, reference_ids: tuple[str, ...]
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "artifact_id": reference_id,
            "kind": profile.references[reference_id].kind,
            "content_digest": _sha256(
                profile.references[reference_id].content.encode("utf-8")
            ),
        }
        for reference_id in reference_ids
    )


def _read_jsonl_bytes(content: bytes, *, label: str) -> tuple[dict[str, Any], ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must contain UTF-8") from error
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {label} JSON on line {number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {number} must be an object")
        records.append(value)
    if not records:
        raise ValueError(f"{label} must not be empty")
    return tuple(records)


def load_evaluator_audit(
    raw: Any,
    *,
    profile: ExternalProfile,
) -> EvaluatorAuditConfig | None:
    if raw is None:
        return None
    data = _strict(
        raw,
        {"anchor_manifest", "max_provider_calls", "drift_thresholds"},
        label="evaluator_audit",
    )
    max_provider_calls = data.get("max_provider_calls")
    if (
        not isinstance(max_provider_calls, int)
        or isinstance(max_provider_calls, bool)
        or max_provider_calls < 1
    ):
        raise ValueError("evaluator_audit.max_provider_calls must be a positive integer")

    dimensions = profile.rubric.dimension_names
    thresholds = _score_map(
        _strict(
            data.get("drift_thresholds"),
            set(dimensions),
            label="evaluator_audit.drift_thresholds",
        ),
        dimensions=dimensions,
        label="evaluator_audit.drift_thresholds",
    )
    root = profile.path.parent
    manifest_path = _source_path(
        root,
        data.get("anchor_manifest"),
        label="evaluator_audit.anchor_manifest",
    )
    manifest_bytes = manifest_path.read_bytes()

    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    source_bytes = {manifest_path: manifest_bytes}
    for raw_row in _read_jsonl_bytes(
        manifest_bytes, label="evaluator anchor manifest"
    ):
        row = _strict(raw_row, _ANCHOR_FIELDS, label="evaluator anchor")
        if set(row) != _ANCHOR_FIELDS:
            raise ValueError("evaluator anchor fields do not match the protocol")
        if row.get("protocol") != "kpo.evaluator-anchor/v1":
            raise ValueError("evaluator anchor protocol is invalid")
        anchor_id = _required_string(row.get("anchor_id"), label="anchor_id")
        if not _ANCHOR_ID.fullmatch(anchor_id):
            raise ValueError("anchor_id must be a safe path component")
        if anchor_id in seen_ids:
            raise ValueError(f"duplicate anchor_id: {anchor_id}")
        seen_ids.add(anchor_id)
        rollout_coordinate = _required_string(
            row.get("rollout_path"), label="anchor.rollout_path"
        )
        rollout_path = _source_path(
            root, rollout_coordinate, label="anchor.rollout_path"
        )
        if rollout_path not in source_bytes:
            source_bytes[rollout_path] = rollout_path.read_bytes()
        rollout_bytes = source_bytes[rollout_path]
        try:
            rollout_content = rollout_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("anchor.rollout_path must contain UTF-8") from error
        if not rollout_content.strip():
            raise ValueError("anchor rollout content must be non-empty")
        reference_ids = _string_list(
            row.get("reference_ids"), label="reference_ids"
        )
        missing = set(reference_ids) - set(profile.references)
        if missing:
            raise ValueError(f"missing anchor references: {sorted(missing)}")
        expected_scores = _score_map(
            row.get("expected_scores"),
            dimensions=dimensions,
            label="expected score",
        )
        provenance = _string_list(row.get("provenance"), label="provenance")
        parsed.append(
            {
                "anchor_id": anchor_id,
                "protocol": row["protocol"],
                "rollout_coordinate": rollout_coordinate,
                "rollout_path": rollout_path,
                "rollout_content": rollout_content,
                "rollout_digest": _sha256(rollout_bytes),
                "reference_ids": reference_ids,
                "reference_material": _reference_material(profile, reference_ids),
                "expected_scores": expected_scores,
                "provenance": provenance,
            }
        )

    parsed.sort(key=lambda row: row["anchor_id"])
    rubric_digest = canonical_digest(profile.rubric)
    request_rows = tuple(
        {
            "anchor_id": row["anchor_id"],
            "rollout_digest": row["rollout_digest"],
            "references": row["reference_material"],
        }
        for row in parsed
    )
    request_set_digest = canonical_digest(
        {
            "protocol": "kpo.evaluator-anchor-request-set/v1",
            "anchors": request_rows,
            "rubric_digest": rubric_digest,
            "random_seed": 0,
        }
    )
    complete_set_digest = canonical_digest(
        {
            "protocol": "kpo.evaluator-anchor-set/v1",
            "anchors": tuple(
                request_rows[index]
                | {
                    "protocol": row["protocol"],
                    "rollout_path": row["rollout_coordinate"],
                    "expected_scores": row["expected_scores"],
                    "provenance": row["provenance"],
                }
                for index, row in enumerate(parsed)
            ),
            "rubric_digest": rubric_digest,
            "random_seed": 0,
        }
    )

    anchors: list[EvaluatorAnchor] = []
    for row in parsed:
        blinded_digest = canonical_digest(
            {
                "protocol": "kpo.evaluator-anchor-blind/v1",
                "anchor_id": row["anchor_id"],
                "anchor_request_set_digest": request_set_digest,
            }
        )
        request_digest = canonical_digest(
            {
                "protocol": "kpo.evaluator-anchor-request/v1",
                "anchor_digest": blinded_digest,
                "rollout_digest": row["rollout_digest"],
                "references": row["reference_material"],
                "rubric_digest": rubric_digest,
                "random_seed": 0,
            }
        )
        anchors.append(
            EvaluatorAnchor(
                anchor_id=row["anchor_id"],
                rollout_path=row["rollout_path"],
                rollout_content=row["rollout_content"],
                rollout_digest=row["rollout_digest"],
                reference_ids=row["reference_ids"],
                expected_scores=row["expected_scores"],
                provenance=row["provenance"],
                blinded_digest=blinded_digest,
                request_digest=request_digest,
            )
        )

    return EvaluatorAuditConfig(
        manifest_path=manifest_path,
        manifest_digest=_sha256(manifest_bytes),
        anchors=tuple(anchors),
        max_provider_calls=max_provider_calls,
        drift_thresholds=thresholds,
        anchor_set_digest=complete_set_digest,
        anchor_request_set_digest=request_set_digest,
        source_digests=MappingProxyType(
            {
                str(path.relative_to(root)): _sha256(content)
                for path, content in sorted(
                    source_bytes.items(), key=lambda item: str(item[0])
                )
            }
        ),
    )


def _require_audit(profile: CampaignProfile) -> EvaluatorAuditConfig:
    if profile.evaluator_audit is None:
        raise ValueError("profile does not configure evaluator_audit")
    return profile.evaluator_audit


def _approval_material(profile: CampaignProfile) -> dict[str, Any]:
    audit = _require_audit(profile)
    reference_ids = sorted(
        {
            reference_id
            for anchor in audit.anchors
            for reference_id in anchor.reference_ids
        }
    )
    return {
        "protocol": "kpo.evaluator-anchor-approval/v1",
        "profile_id": profile.base.profile_id,
        "rubric_version": profile.base.rubric.rubric_version,
        "rubric_dimensions": list(profile.base.rubric.dimension_names),
        "anchor_count": len(audit.anchors),
        "manifest_digest": audit.manifest_digest,
        "anchor_source_digests": dict(audit.source_digests),
        "reference_digests": {
            reference_id: {
                "kind": profile.base.references[reference_id].kind,
                "source_digest": profile.base.reference_source_digests[reference_id],
                "content_digest": _sha256(
                    profile.base.references[reference_id].content.encode("utf-8")
                ),
            }
            for reference_id in reference_ids
        },
        "expected_score_digest": canonical_digest(
            tuple(
                {
                    "anchor_id": anchor.anchor_id,
                    "expected_scores": anchor.expected_scores,
                }
                for anchor in audit.anchors
            )
        ),
        "anchor_set_digest": audit.anchor_set_digest,
        "anchor_request_set_digest": audit.anchor_request_set_digest,
    }


def _approval_digest(material: Mapping[str, Any]) -> str:
    return canonical_digest(
        {
            "protocol": "kpo.evaluator-anchor-approval-decision/v1",
            "record": material,
        }
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def preview_anchor_approval(
    profile_path: Path, *, checkout: Path
) -> dict[str, Any]:
    from kpo.campaign_profile import load_campaign_profile

    profile = load_campaign_profile(profile_path, checkout=checkout)
    material = _approval_material(profile)
    return {
        "protocol": "kpo.evaluator-anchor-approval-preview/v1",
        "profile_id": material["profile_id"],
        "rubric_version": material["rubric_version"],
        "rubric_dimensions": material["rubric_dimensions"],
        "anchor_count": material["anchor_count"],
        "manifest_digest": material["manifest_digest"],
        "source_digests": material["anchor_source_digests"],
        "anchor_set_digest": material["anchor_set_digest"],
        "approval_digest": _approval_digest(material),
    }


def _approval_record_path(profile: CampaignProfile) -> Path:
    audit = _require_audit(profile)
    return (
        profile.base.data_home
        / "anchors"
        / "approved"
        / f"{audit.anchor_set_digest}.json"
    )


def apply_anchor_approval(
    profile_path: Path,
    *,
    checkout: Path,
    approval_digest: str,
) -> dict[str, Any]:
    from kpo.campaign_profile import load_campaign_profile

    # Reload every source immediately before comparing the human-reviewed digest.
    profile = load_campaign_profile(profile_path, checkout=checkout)
    material = _approval_material(profile)
    expected_approval = _approval_digest(material)
    if approval_digest != expected_approval:
        raise ValueError("anchor approval digest does not match current sources")
    record = material | {"approval_digest": expected_approval}
    content = _json_bytes(record)
    path = _approval_record_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".anchor-approval-",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        # A hard link publishes the already-complete inode atomically and fails
        # if an immutable record already occupies the final coordinate.
        os.link(temporary, path)
    except BaseException:
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    record_digest = _sha256(content)
    return {
        "state": "approved",
        "anchor_set_digest": material["anchor_set_digest"],
        "approval_digest": expected_approval,
        "record_path": str(path.relative_to(profile.base.data_home)),
        "record_digest": record_digest,
    }


def load_anchor_approval(
    profile: CampaignProfile,
) -> tuple[dict[str, Any], str]:
    path = _approval_record_path(profile)
    content = path.read_bytes()
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid anchor approval record JSON") from error
    if not isinstance(raw, dict) or set(raw) != _APPROVAL_RECORD_FIELDS:
        raise ValueError("anchor approval record fields do not match protocol")
    material = _approval_material(profile)
    expected = material | {"approval_digest": _approval_digest(material)}
    if raw != expected:
        raise ValueError("anchor approval record does not match current profile")
    if content != _json_bytes(expected):
        raise ValueError("anchor approval record bytes are not canonical")
    return raw, _sha256(content)


def build_anchor_evaluator_request(
    profile: CampaignProfile,
    anchor: EvaluatorAnchor,
) -> EvaluatorRequest:
    audit = _require_audit(profile)
    if anchor not in audit.anchors:
        raise ValueError("anchor does not belong to evaluator audit")
    actor_payload_digest = canonical_digest(
        {
            "protocol": "kpo.evaluator-anchor/v1",
            "anchor_request_digest": anchor.request_digest,
        }
    )
    rollout = Rollout.create(
        case_id=anchor.blinded_digest,
        policy_digest=anchor.request_digest,
        model_id="kpo-anchor-fixture/v1",
        output=anchor.rollout_content,
        retrieved_policy_ids=(),
        actor_payload_digest=actor_payload_digest,
    )
    return EvaluatorRequest(
        case_id=anchor.blinded_digest,
        rollout=rollout,
        references=tuple(
            profile.base.references[reference_id]
            for reference_id in anchor.reference_ids
        ),
        rubric_version=profile.base.rubric.rubric_version,
        random_seed=0,
    )
