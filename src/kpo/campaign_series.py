from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from kpo.campaign_profile import CampaignProfile, validate_campaign_id
from kpo.digest import canonical_digest, canonical_json
from kpo.evaluator_agreement import load_agreement_report
from kpo.evaluator_calibration_report import calibration_summary
from kpo.models import Partition


@dataclass(frozen=True, slots=True)
class SeriesContract:
    components: Mapping[str, Any]
    digest: str


@dataclass(frozen=True, slots=True)
class SeriesEntry:
    index: int
    campaign_id: str
    revision: int
    campaign_state: str
    parent_policy_digest: str
    candidate_policy_digest: str | None
    dataset_digest: str
    profile_snapshot_digest: str
    evidence_path: str
    evidence_digest: str
    evidence_record_digest: str


_SERIES_FIELDS = {
    "protocol",
    "series_id",
    "profile_id",
    "state",
    "contract",
    "series_contract_digest",
    "initial_policy_digest",
    "holdout_digest",
    "max_campaigns",
    "stopping",
    "entries",
    "state_digest",
}
_ENTRY_FIELDS = {
    "index",
    "campaign_id",
    "revision",
    "campaign_state",
    "parent_policy_digest",
    "candidate_policy_digest",
    "dataset_digest",
    "profile_snapshot_digest",
    "evidence_path",
    "evidence_digest",
    "evidence_record_digest",
}
_TERMINAL_CAMPAIGN_STATES = {
    "promotion_previewed",
    "budget_exhausted",
    "no_patchable_diagnosis",
    "failed",
    "stopped",
}
_HEX_DIGEST = frozenset("0123456789abcdef")
_EVIDENCE_FIELDS = {
    "protocol",
    "campaign_id",
    "series_id",
    "series_index",
    "revision",
    "profile_id",
    "profile_snapshot_digest",
    "series_contract_digest",
    "dataset_digest",
    "holdout_digest",
    "terminal_state",
    "parent_policy_digest",
    "candidate_policy_digest",
    "dimensions",
    "iteration_count",
    "call_count",
    "rejected_candidate_count",
    "scores",
    "vector_report_digest",
    "agreement_byte_digest",
    "agreement_report_digest",
    "calibration_byte_digest",
    "calibration_observations_byte_digest",
    "calibration_report_digest",
    "record_digest",
}
_SCORE_FIELDS = {
    "validation_gain",
    "holdout_gain",
    "regression_gain",
    "ablation_gain",
}
_SERIES_DECIMAL_QUANTUM = Decimal("0.000000000001")


def _json_value(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def _atomic_new_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(
                f"immutable artifact already exists: {path.name}"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _series_path(profile: CampaignProfile, series_id: str) -> Path:
    validate_campaign_id(series_id)
    return profile.base.data_home / "series" / series_id / "series.json"


def _series_lock_path(profile: CampaignProfile, series_id: str) -> Path:
    validate_campaign_id(series_id)
    return profile.base.data_home / "series" / ".locks" / f"{series_id}.json"


@contextmanager
def series_mutation(
    profile: CampaignProfile,
    series_id: str,
    *,
    operation: str,
    campaign_id: str | None = None,
    series_index: int | None = None,
    revision: int | None = None,
    initial_state_digest: str | None = None,
) -> Iterator[Callable[..., None]]:
    path = _series_lock_path(profile, series_id)
    record = {
        "protocol": "kpo.campaign-series-lock/v1",
        "series_id": series_id,
        "operation": operation,
        "campaign_id": campaign_id,
        "series_index": series_index,
        "revision": revision,
        "initial_state_digest": initial_state_digest,
        "pid": os.getpid(),
    }
    try:
        _atomic_new_json(path, record)
    except FileExistsError as error:
        raise ValueError("series concurrent mutation") from error
    try:
        def update(**changes: Any) -> None:
            record.update(changes)
            _atomic_json(path, record)

        yield update
    finally:
        path.unlink(missing_ok=True)


def _with_state_digest(fields: dict[str, Any]) -> dict[str, Any]:
    return fields | {"state_digest": canonical_digest(fields)}


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGEST for character in value)
    )


def _strict(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid {label} fields")
    return value


def _file_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _provider_component(role: str, config: Any, *, name: str | None = None) -> dict[str, Any]:
    semantic_configuration = {
        "command": ("<executable>", *config.command[1:]),
        "identity": config.identity,
        "timeout_seconds": config.timeout_seconds,
        "pass_env": config.pass_env,
    }
    value: dict[str, Any] = {
        "role": role,
        "identity": config.identity,
        "configuration_digest": canonical_digest(semantic_configuration),
        "executable_digest": _file_digest(config.command[0]),
    }
    if name is not None:
        value["name"] = name
    return value


def build_series_contract(profile: CampaignProfile) -> SeriesContract:
    if profile.series is None:
        raise ValueError("campaign profile has no series configuration")
    profile_root = profile.base.path.parent
    target_relative = profile.target_root.relative_to(profile_root).as_posix()
    audit = profile.evaluator_audit
    audit_contract = (
        None
        if audit is None
        else canonical_digest(
            {
                "anchor_set_digest": audit.anchor_set_digest,
                "anchor_request_set_digest": audit.anchor_request_set_digest,
                "max_provider_calls": audit.max_provider_calls,
                "drift_thresholds": audit.drift_thresholds,
                "source_digests": audit.source_digests,
            }
        )
    )
    components: dict[str, Any] = {
        "profile_id": profile.base.profile_id,
        "rubric_version": profile.base.rubric.rubric_version,
        "dimensions": tuple(profile.base.rubric.dimension_names),
        "rubric_digest": canonical_digest(profile.base.rubric),
        "gate_digests": {
            "validation": canonical_digest(profile.gates.validation),
            "holdout": canonical_digest(profile.gates.holdout),
            "regression": canonical_digest(profile.gates.regression),
            "ablation": canonical_digest(profile.gates.ablation),
        },
        "providers": (
            _provider_component("actor", profile.base.actor),
            _provider_component("evaluator", profile.base.evaluator),
            _provider_component("proposer", profile.proposer),
            *(
                _provider_component(
                    "observer", observer.config, name=observer.name
                )
                for observer in profile.base.observer_evaluators
            ),
        ),
        "holdout_digest": profile.dataset.holdout_digest,
        "promotion_digest": canonical_digest(
            {
                "target_relative": target_relative,
                "allowlist": profile.allowlist,
                "add_directory": profile.add_directory,
            }
        ),
        "evaluator_audit_digest": audit_contract,
        "series_config_digest": canonical_digest(profile.series),
    }
    return SeriesContract(
        components=components,
        digest=canonical_digest(components),
    )


def build_initial_series_state(
    profile: CampaignProfile, series_id: str
) -> dict[str, Any]:
    validate_campaign_id(series_id)
    if profile.series is None:
        raise ValueError("campaign profile has no series configuration")
    contract = build_series_contract(profile)
    fields = {
        "protocol": "kpo.campaign-series/v1",
        "series_id": series_id,
        "profile_id": profile.base.profile_id,
        "state": "active",
        "contract": _json_value(contract.components),
        "series_contract_digest": contract.digest,
        "initial_policy_digest": profile.base.policy.digest,
        "holdout_digest": profile.dataset.holdout_digest,
        "max_campaigns": profile.series.max_campaigns,
        "stopping": {
            "min_improvement": profile.series.min_improvement,
            "patience": profile.series.patience,
            "holdout_degradation": profile.series.holdout_degradation,
        },
        "entries": [],
    }
    return _with_state_digest(fields)


def initialize_series(
    profile: CampaignProfile, series_id: str, *, _locked: bool = False
) -> dict[str, Any]:
    if not _locked:
        with series_mutation(profile, series_id, operation="initialize"):
            return initialize_series(profile, series_id, _locked=True)
    state = build_initial_series_state(profile, series_id)
    path = _series_path(profile, series_id)
    try:
        path.parent.mkdir(parents=True)
    except FileExistsError as error:
        raise FileExistsError(f"campaign series already exists: {series_id}") from error
    _atomic_json(path, state)
    return load_series_state(profile, series_id)


def _entry_dict(entry: SeriesEntry) -> dict[str, Any]:
    validate_campaign_id(entry.campaign_id)
    if not isinstance(entry.index, int) or isinstance(entry.index, bool) or entry.index < 0:
        raise ValueError("series index must be a non-negative integer")
    if (
        not isinstance(entry.revision, int)
        or isinstance(entry.revision, bool)
        or entry.revision < 0
    ):
        raise ValueError("campaign revision must be a non-negative integer")
    if entry.campaign_state not in _TERMINAL_CAMPAIGN_STATES:
        raise ValueError("series campaign state is not terminal")
    for label, digest in (
        ("parent policy", entry.parent_policy_digest),
        ("dataset", entry.dataset_digest),
        ("profile snapshot", entry.profile_snapshot_digest),
        ("evidence", entry.evidence_digest),
        ("evidence record", entry.evidence_record_digest),
    ):
        if not _valid_digest(digest):
            raise ValueError(f"invalid {label} digest")
    if entry.candidate_policy_digest is not None and not _valid_digest(
        entry.candidate_policy_digest
    ):
        raise ValueError("invalid candidate policy digest")
    suffix = "" if entry.revision == 0 else f".{entry.revision}"
    expected_path = (
        f"campaigns/{entry.campaign_id}/series-evidence{suffix}.json"
    )
    if entry.evidence_path != expected_path:
        raise ValueError("series evidence path does not match campaign")
    return {
        "index": entry.index,
        "campaign_id": entry.campaign_id,
        "revision": entry.revision,
        "campaign_state": entry.campaign_state,
        "parent_policy_digest": entry.parent_policy_digest,
        "candidate_policy_digest": entry.candidate_policy_digest,
        "dataset_digest": entry.dataset_digest,
        "profile_snapshot_digest": entry.profile_snapshot_digest,
        "evidence_path": entry.evidence_path,
        "evidence_digest": entry.evidence_digest,
        "evidence_record_digest": entry.evidence_record_digest,
    }


def _validate_series_state(
    profile: CampaignProfile, series_id: str, raw: Any
) -> dict[str, Any]:
    state = _strict(raw, _SERIES_FIELDS, "campaign series")
    digest = state["state_digest"]
    fields = {key: value for key, value in state.items() if key != "state_digest"}
    if not _valid_digest(digest) or digest != canonical_digest(fields):
        raise ValueError("campaign series state digest mismatch")
    if (
        state["protocol"] != "kpo.campaign-series/v1"
        or state["series_id"] != series_id
        or state["profile_id"] != profile.base.profile_id
        or state["state"] not in {"active", "stopped_by_owner"}
    ):
        raise ValueError("campaign series identity or state mismatch")
    if profile.series is None:
        raise ValueError("campaign profile has no series configuration")
    contract = build_series_contract(profile)
    if (
        state["contract"] != _json_value(contract.components)
        or state["series_contract_digest"] != contract.digest
        or state["holdout_digest"] != profile.dataset.holdout_digest
        or state["max_campaigns"] != profile.series.max_campaigns
        or state["stopping"]
        != {
            "min_improvement": profile.series.min_improvement,
            "patience": profile.series.patience,
            "holdout_degradation": profile.series.holdout_degradation,
        }
    ):
        raise ValueError("campaign series contract mismatch")
    if not _valid_digest(state["initial_policy_digest"]):
        raise ValueError("invalid initial policy digest")
    if not isinstance(state["entries"], list):
        raise ValueError("campaign series entries must be an array")
    seen_campaigns: set[str] = set()
    previous: dict[str, Any] | None = None
    for value in state["entries"]:
        entry = _strict(value, _ENTRY_FIELDS, "campaign series entry")
        parsed = _entry_dict(
            SeriesEntry(
                index=entry["index"],
                campaign_id=entry["campaign_id"],
                revision=entry["revision"],
                campaign_state=entry["campaign_state"],
                parent_policy_digest=entry["parent_policy_digest"],
                candidate_policy_digest=entry["candidate_policy_digest"],
                dataset_digest=entry["dataset_digest"],
                profile_snapshot_digest=entry["profile_snapshot_digest"],
                evidence_path=entry["evidence_path"],
                evidence_digest=entry["evidence_digest"],
                evidence_record_digest=entry["evidence_record_digest"],
            )
        )
        if parsed != entry:
            raise ValueError("campaign series entry order mismatch")
        if previous is not None and entry["campaign_id"] == previous["campaign_id"]:
            if (
                entry["index"] != previous["index"]
                or entry["revision"] != previous["revision"] + 1
                or previous["campaign_state"] != "failed"
            ):
                raise ValueError("campaign revision chain is invalid")
        else:
            if entry["campaign_id"] in seen_campaigns:
                raise ValueError("campaign revisions are interleaved")
            if entry["index"] != len(seen_campaigns) or entry["revision"] != 0:
                raise ValueError("campaign series entry order mismatch")
            seen_campaigns.add(entry["campaign_id"])
        previous = entry
    return state


def load_series_state(profile: CampaignProfile, series_id: str) -> dict[str, Any]:
    path = _series_path(profile, series_id)
    if not path.is_file():
        raise KeyError(f"unknown campaign series: {series_id}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid campaign series JSON") from error
    return _validate_series_state(profile, series_id, raw)


def append_series_entry(
    profile: CampaignProfile,
    series_id: str,
    entry: SeriesEntry,
    *,
    _locked: bool = False,
) -> dict[str, Any]:
    if not _locked:
        with series_mutation(
            profile,
            series_id,
            operation="append",
            campaign_id=entry.campaign_id,
            series_index=entry.index,
            revision=entry.revision,
        ):
            return append_series_entry(
                profile, series_id, entry, _locked=True
            )
    state = load_series_state(profile, series_id)
    if state["state"] != "active":
        raise ValueError(f"campaign series is {state['state']}")
    new_entry = _entry_dict(entry)
    for existing in state["entries"]:
        if (
            existing["campaign_id"] == entry.campaign_id
            and existing["revision"] == entry.revision
        ):
            if existing == new_entry:
                return state
            raise ValueError("campaign revision is already attached with different evidence")
        if existing["index"] == entry.index and existing["campaign_id"] != entry.campaign_id:
            raise ValueError("series index is already attached to another campaign")
    campaign_entries = [
        existing
        for existing in state["entries"]
        if existing["campaign_id"] == entry.campaign_id
    ]
    if campaign_entries:
        latest = campaign_entries[-1]
        if state["entries"][-1]["campaign_id"] != entry.campaign_id:
            raise ValueError("campaign revisions cannot be interleaved")
        if entry.index != latest["index"]:
            raise ValueError("campaign revision changed series index")
        if entry.revision != latest["revision"] + 1:
            raise ValueError("entry does not use the next campaign revision")
        if latest["campaign_state"] != "failed":
            raise ValueError("campaign revision must follow failed state")
    else:
        campaign_count = len({item["campaign_id"] for item in state["entries"]})
        if entry.revision != 0:
            raise ValueError("first campaign entry must use revision zero")
        if entry.index != campaign_count:
            raise ValueError("entry does not use the next series index")
        if campaign_count >= state["max_campaigns"]:
            raise ValueError("campaign series max_campaigns is exhausted")
    fields = {
        key: value for key, value in state.items() if key != "state_digest"
    }
    fields["entries"] = [*state["entries"], new_entry]
    updated = _with_state_digest(fields)
    _atomic_json(_series_path(profile, series_id), updated)
    return load_series_state(profile, series_id)


def stop_series(
    profile: CampaignProfile, series_id: str, *, _locked: bool = False
) -> dict[str, Any]:
    if not _locked:
        with series_mutation(profile, series_id, operation="stop"):
            return stop_series(profile, series_id, _locked=True)
    state = load_series_state(profile, series_id)
    expected_series_parent(profile, state)
    if state["state"] == "stopped_by_owner":
        return state
    fields = {
        key: value for key, value in state.items() if key != "state_digest"
    }
    fields["state"] = "stopped_by_owner"
    updated = _with_state_digest(fields)
    _atomic_json(_series_path(profile, series_id), updated)
    return load_series_state(profile, series_id)


def effective_series_entries(state: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    effective: list[dict[str, Any]] = []
    for entry in state["entries"]:
        if effective and effective[-1]["campaign_id"] == entry["campaign_id"]:
            effective[-1] = entry
        else:
            effective.append(entry)
    return tuple(effective)


def expected_series_parent(
    profile: CampaignProfile, state: Mapping[str, Any]
) -> str:
    expected = state["initial_policy_digest"]
    for entry in effective_series_entries(state):
        campaign_id = entry["campaign_id"]
        if entry["parent_policy_digest"] != expected:
            raise ValueError("series policy lineage mismatch")
        path = profile.base.data_home / "campaigns" / campaign_id / "campaign.json"
        if not path.is_file():
            raise ValueError("series campaign state is missing")
        try:
            campaign = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid series campaign state") from error
        if (
            not isinstance(campaign, dict)
            or campaign.get("campaign_id") != campaign_id
            or campaign.get("series_id") != state["series_id"]
            or campaign.get("series_index") != entry["index"]
            or campaign.get("series_evidence_revision") != entry["revision"]
            or campaign.get("state") != entry["campaign_state"]
            or campaign.get("parent_policy_digest") != entry["parent_policy_digest"]
            or campaign.get("candidate_policy_digest")
            != entry["candidate_policy_digest"]
            or campaign.get("profile_snapshot_digest")
            != entry["profile_snapshot_digest"]
            or campaign.get("series_evidence_path") != entry["evidence_path"]
            or campaign.get("series_evidence_digest") != entry["evidence_digest"]
            or campaign.get("series_evidence_record_digest")
            != entry["evidence_record_digest"]
        ):
            raise ValueError("series campaign binding mismatch")
        promotion_state = campaign.get("promotion_state")
        if promotion_state == "applying":
            raise ValueError("series promotion recovery required")
        if promotion_state == "applied":
            candidate = campaign.get("candidate_policy_digest")
            if candidate is None:
                raise ValueError("series applied promotion has no candidate policy")
            expected = candidate
        elif promotion_state not in {None, "previewed", "recovered"}:
            raise ValueError("invalid series promotion state")
    return expected


_LOCK_FIELDS = {
    "protocol",
    "series_id",
    "operation",
    "campaign_id",
    "series_index",
    "revision",
    "initial_state_digest",
    "pid",
}


def _process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recover_stale_series_lock(
    profile: CampaignProfile, series_id: str
) -> None:
    lock_path = _series_lock_path(profile, series_id)
    if not lock_path.is_file():
        return
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("series manual recovery required") from error
    lock = _strict(raw, _LOCK_FIELDS, "campaign series lock")
    if (
        lock["protocol"] != "kpo.campaign-series-lock/v1"
        or lock["series_id"] != series_id
    ):
        raise ValueError("series manual recovery required")
    if _process_is_alive(lock["pid"]):
        raise ValueError("series concurrent mutation")
    operation = lock["operation"]
    if operation == "append":
        state = load_series_state(profile, series_id)
        attached = any(
            entry["campaign_id"] == lock["campaign_id"]
            and entry["index"] == lock["series_index"]
            and entry["revision"] == lock["revision"]
            for entry in state["entries"]
        )
        if not attached:
            raise ValueError("series manual recovery required")
        expected_series_parent(profile, state)
        lock_path.unlink()
        return
    if operation in {"initialize", "reconcile", "stop"}:
        state = load_series_state(profile, series_id)
        _require_no_applying_promotion(profile, state)
        lock_path.unlink()
        return
    if operation != "allocation":
        raise ValueError("series manual recovery required")
    campaign_id = lock["campaign_id"]
    index = lock["series_index"]
    initial_digest = lock["initial_state_digest"]
    if (
        not isinstance(campaign_id, str)
        or not isinstance(index, int)
        or isinstance(index, bool)
        or not _valid_digest(initial_digest)
    ):
        raise ValueError("series manual recovery required")
    campaign_path = (
        profile.base.data_home / "campaigns" / campaign_id / "campaign.json"
    )
    if not campaign_path.exists():
        lock_path.unlink()
        return
    campaign_directory = campaign_path.parent
    if sorted(item.name for item in campaign_directory.iterdir()) != [
        "campaign.json"
    ]:
        raise ValueError("series manual recovery required")
    try:
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("series manual recovery required") from error
    if (
        not isinstance(campaign, dict)
        or canonical_digest(campaign) != initial_digest
        or campaign.get("campaign_id") != campaign_id
        or campaign.get("series_id") != series_id
        or campaign.get("series_index") != index
        or campaign.get("series_evidence_revision") != 0
        or campaign.get("state") != "running"
        or campaign.get("iteration") != 0
    ):
        raise ValueError("series manual recovery required")
    campaign.update(
        state="failed",
        failure_kind="series_initialization_interrupted",
        call_count=0,
        rejected_candidate_count=0,
    )
    binding = persist_campaign_series_evidence(
        profile,
        series_id=series_id,
        series_index=index,
        campaign_id=campaign_id,
        revision=0,
        terminal_state="failed",
        profile_snapshot_digest=campaign["profile_snapshot_digest"],
        iteration_count=0,
        call_count=0,
        rejected_candidate_count=0,
        report=None,
        report_bindings=campaign,
    )
    campaign.update(
        series_evidence_path=binding["path"],
        series_evidence_digest=binding["byte_digest"],
        series_evidence_record_digest=binding["record_digest"],
    )
    _atomic_json(campaign_path, campaign)
    append_series_entry(
        profile,
        series_id,
        SeriesEntry(
            index=index,
            campaign_id=campaign_id,
            revision=0,
            campaign_state="failed",
            parent_policy_digest=campaign["parent_policy_digest"],
            candidate_policy_digest=None,
            dataset_digest=profile.dataset.digest,
            profile_snapshot_digest=campaign["profile_snapshot_digest"],
            evidence_path=binding["path"],
            evidence_digest=binding["byte_digest"],
            evidence_record_digest=binding["record_digest"],
        ),
        _locked=True,
    )
    lock_path.unlink()


def _require_no_applying_promotion(
    profile: CampaignProfile, state: Mapping[str, Any]
) -> None:
    for entry in effective_series_entries(state):
        path = (
            profile.base.data_home
            / "campaigns"
            / entry["campaign_id"]
            / "campaign.json"
        )
        if not path.is_file():
            raise ValueError("series campaign state is missing")
        try:
            campaign = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid series campaign state") from error
        if campaign.get("promotion_state") == "applying":
            raise ValueError("series promotion recovery required")


def reconcile_series(
    profile: CampaignProfile, series_id: str, *, _locked: bool = False
) -> dict[str, Any]:
    if not _locked:
        _recover_stale_series_lock(profile, series_id)
        with series_mutation(profile, series_id, operation="reconcile"):
            return reconcile_series(profile, series_id, _locked=True)
    state = load_series_state(profile, series_id)
    if state["state"] != "active":
        raise ValueError(f"campaign series is {state['state']}")
    _require_no_applying_promotion(profile, state)
    attached_count = 0
    already_present_count = 0
    ignored_count = 0
    candidates: list[tuple[int, dict[str, Any], dict[str, Any], str]] = []
    campaigns_root = profile.base.data_home / "campaigns"
    for path in sorted(campaigns_root.glob("*/campaign.json")):
        try:
            campaign = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            ignored_count += 1
            continue
        if not isinstance(campaign, dict) or campaign.get("series_id") != series_id:
            ignored_count += 1
            continue
        if campaign.get("state") not in _TERMINAL_CAMPAIGN_STATES:
            raise ValueError("series has a bound non-terminal campaign")
        campaign_id = campaign.get("campaign_id")
        index = campaign.get("series_index")
        if not isinstance(campaign_id, str) or not isinstance(index, int):
            raise ValueError("series campaign binding is invalid")
        revision = campaign.get("series_evidence_revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("series campaign revision is invalid")
        evidence = load_campaign_series_evidence(
            profile,
            campaign_id,
            revision=revision,
            expected_series_id=series_id,
        )
        evidence_path = _evidence_path(profile, campaign_id, revision)
        byte_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        if (
            evidence["series_index"] != index
            or evidence["revision"] != revision
            or evidence["terminal_state"] != campaign["state"]
            or evidence["parent_policy_digest"]
            != campaign.get("parent_policy_digest")
            or evidence["candidate_policy_digest"]
            != campaign.get("candidate_policy_digest")
            or evidence["profile_snapshot_digest"]
            != campaign.get("profile_snapshot_digest")
            or campaign.get("series_evidence_path")
            != _relative_evidence_path(campaign_id, revision)
            or campaign.get("series_evidence_digest") != byte_digest
            or campaign.get("series_evidence_record_digest")
            != evidence["record_digest"]
        ):
            raise ValueError("series campaign evidence binding mismatch")
        candidates.append((index, revision, campaign, evidence, byte_digest))
    for index, revision, campaign, evidence, byte_digest in sorted(candidates):
        entry = SeriesEntry(
            index=index,
            campaign_id=campaign["campaign_id"],
            revision=revision,
            campaign_state=campaign["state"],
            parent_policy_digest=campaign["parent_policy_digest"],
            candidate_policy_digest=campaign.get("candidate_policy_digest"),
            dataset_digest=evidence["dataset_digest"],
            profile_snapshot_digest=campaign["profile_snapshot_digest"],
            evidence_path=campaign["series_evidence_path"],
            evidence_digest=byte_digest,
            evidence_record_digest=evidence["record_digest"],
        )
        before = load_series_state(profile, series_id)
        if any(
            existing["campaign_id"] == campaign["campaign_id"]
            and existing["revision"] == revision
            for existing in before["entries"]
        ):
            append_series_entry(profile, series_id, entry, _locked=True)
            already_present_count += 1
        else:
            append_series_entry(profile, series_id, entry, _locked=True)
            attached_count += 1
    final_state = load_series_state(profile, series_id)
    expected_series_parent(profile, final_state)
    return {
        "series_id": series_id,
        "attached_count": attached_count,
        "already_present_count": already_present_count,
        "ignored_count": ignored_count,
        "campaign_count": len(effective_series_entries(final_state)),
        "revision_count": len(final_state["entries"]),
    }


def _relative_evidence_path(campaign_id: str, revision: int) -> str:
    suffix = "" if revision == 0 else f".{revision}"
    return f"campaigns/{campaign_id}/series-evidence{suffix}.json"


def _evidence_path(
    profile: CampaignProfile, campaign_id: str, revision: int = 0
) -> Path:
    validate_campaign_id(campaign_id)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("campaign revision must be a non-negative integer")
    filename = (
        "series-evidence.json"
        if revision == 0
        else f"series-evidence.{revision}.json"
    )
    return (
        profile.base.data_home
        / "campaigns"
        / campaign_id
        / filename
    )


def _optional_binding(
    bindings: Mapping[str, Any], key: str
) -> str | None:
    value = bindings.get(key)
    if value is not None and not _valid_digest(value):
        raise ValueError(f"invalid {key}")
    return value


def _score_map(value: Any, dimensions: tuple[str, ...], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(dimensions):
        raise ValueError(f"{label} dimensions do not match rubric")
    result: dict[str, float] = {}
    for name in dimensions:
        score = value[name]
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
        ):
            raise ValueError(f"{label}.{name} must be finite")
        result[name] = float(score)
    return result


def persist_campaign_series_evidence(
    profile: CampaignProfile,
    *,
    series_id: str,
    series_index: int,
    campaign_id: str,
    revision: int = 0,
    terminal_state: str,
    profile_snapshot_digest: str,
    iteration_count: int,
    call_count: int,
    rejected_candidate_count: int,
    report: Any | None,
    report_bindings: Mapping[str, Any],
) -> dict[str, str]:
    validate_campaign_id(series_id)
    validate_campaign_id(campaign_id)
    contract = build_series_contract(profile)
    if terminal_state not in _TERMINAL_CAMPAIGN_STATES:
        raise ValueError("campaign evidence state is not terminal")
    if terminal_state == "promotion_previewed":
        if report is None or report.passed is not True:
            raise ValueError("promotion preview requires a passing report")
        if report.parent_policy_digest != profile.base.policy.digest:
            raise ValueError("passing report parent policy mismatch")
        dimensions = tuple(profile.base.rubric.dimension_names)
        scores = {
            "validation_gain": _score_map(
                report.partition_gains[Partition.VALIDATION],
                dimensions,
                "validation_gain",
            ),
            "holdout_gain": _score_map(
                report.partition_gains[Partition.HOLDOUT],
                dimensions,
                "holdout_gain",
            ),
            "regression_gain": _score_map(
                report.partition_gains[Partition.REGRESSION],
                dimensions,
                "regression_gain",
            ),
            "ablation_gain": _score_map(
                report.ablation_gains, dimensions, "ablation_gain"
            ),
        }
        candidate_policy_digest = report.candidate_policy_digest
        vector_report_digest = report.digest
        if not _valid_digest(candidate_policy_digest) or not _valid_digest(
            vector_report_digest
        ):
            raise ValueError("passing report digests are invalid")
    else:
        if report is not None:
            raise ValueError("reports are allowed only promotion_previewed evidence")
        dimensions = tuple(profile.base.rubric.dimension_names)
        scores = None
        candidate_policy_digest = None
        vector_report_digest = None
    for label, count in (
        ("series_index", series_index),
        ("revision", revision),
        ("iteration_count", iteration_count),
        ("call_count", call_count),
        ("rejected_candidate_count", rejected_candidate_count),
    ):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if not _valid_digest(profile_snapshot_digest):
        raise ValueError("invalid profile snapshot digest")
    fields = {
        "protocol": "kpo.campaign-series-evidence/v1",
        "campaign_id": campaign_id,
        "series_id": series_id,
        "series_index": series_index,
        "revision": revision,
        "profile_id": profile.base.profile_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "series_contract_digest": contract.digest,
        "dataset_digest": profile.dataset.digest,
        "holdout_digest": profile.dataset.holdout_digest,
        "terminal_state": terminal_state,
        "parent_policy_digest": profile.base.policy.digest,
        "candidate_policy_digest": candidate_policy_digest,
        "dimensions": list(dimensions),
        "iteration_count": iteration_count,
        "call_count": call_count,
        "rejected_candidate_count": rejected_candidate_count,
        "scores": scores,
        "vector_report_digest": vector_report_digest,
        "agreement_byte_digest": _optional_binding(
            report_bindings, "evaluator_agreement_digest"
        ),
        "agreement_report_digest": _optional_binding(
            report_bindings, "evaluator_agreement_report_digest"
        ),
        "calibration_byte_digest": _optional_binding(
            report_bindings, "evaluator_calibration_digest"
        ),
        "calibration_observations_byte_digest": _optional_binding(
            report_bindings, "evaluator_calibration_observations_digest"
        ),
        "calibration_report_digest": _optional_binding(
            report_bindings, "evaluator_calibration_report_digest"
        ),
    }
    record = fields | {"record_digest": canonical_digest(fields)}
    path = _evidence_path(profile, campaign_id, revision)
    if path.exists():
        existing = load_campaign_series_evidence(
            profile,
            campaign_id,
            revision=revision,
            expected_series_id=series_id,
        )
        if existing != record:
            raise FileExistsError("campaign series evidence already exists with different content")
    else:
        _atomic_new_json(path, record)
    payload = path.read_bytes()
    return {
        "path": _relative_evidence_path(campaign_id, revision),
        "byte_digest": hashlib.sha256(payload).hexdigest(),
        "record_digest": record["record_digest"],
    }


def _validate_scores(value: Any, dimensions: tuple[str, ...]) -> dict[str, Any] | None:
    if value is None:
        return None
    scores = _strict(value, _SCORE_FIELDS, "campaign series scores")
    return {
        name: _score_map(scores[name], dimensions, name)
        for name in (
            "validation_gain",
            "holdout_gain",
            "regression_gain",
            "ablation_gain",
        )
    }


def load_campaign_series_evidence(
    profile: CampaignProfile,
    campaign_id: str,
    *,
    revision: int = 0,
    expected_series_id: str | None = None,
) -> dict[str, Any]:
    path = _evidence_path(profile, campaign_id, revision)
    if not path.is_file():
        raise KeyError(f"missing campaign series evidence: {campaign_id}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid campaign series evidence JSON") from error
    record = _strict(raw, _EVIDENCE_FIELDS, "campaign series evidence")
    digest = record["record_digest"]
    fields = {key: value for key, value in record.items() if key != "record_digest"}
    if not _valid_digest(digest) or digest != canonical_digest(fields):
        raise ValueError("campaign series evidence record digest mismatch")
    if (
        record["protocol"] != "kpo.campaign-series-evidence/v1"
        or record["campaign_id"] != campaign_id
        or record["revision"] != revision
        or record["profile_id"] != profile.base.profile_id
        or (
            expected_series_id is not None
            and record["series_id"] != expected_series_id
        )
    ):
        raise ValueError("campaign series evidence identity mismatch")
    validate_campaign_id(record["series_id"])
    contract = build_series_contract(profile)
    if (
        record["series_contract_digest"] != contract.digest
        or record["holdout_digest"] != profile.dataset.holdout_digest
    ):
        raise ValueError("campaign series evidence contract mismatch")
    dimensions = tuple(profile.base.rubric.dimension_names)
    if record["dimensions"] != list(dimensions):
        raise ValueError("campaign series evidence dimensions mismatch")
    scores = _validate_scores(record["scores"], dimensions)
    if record["terminal_state"] == "promotion_previewed":
        if (
            scores is None
            or not _valid_digest(record["candidate_policy_digest"])
            or not _valid_digest(record["vector_report_digest"])
        ):
            raise ValueError("promotion evidence is incomplete")
    elif record["terminal_state"] in _TERMINAL_CAMPAIGN_STATES:
        if (
            scores is not None
            or record["candidate_policy_digest"] is not None
            or record["vector_report_digest"] is not None
        ):
            raise ValueError("non-promotion evidence contains scores")
    else:
        raise ValueError("campaign series evidence state is invalid")
    for key in (
        "profile_snapshot_digest",
        "dataset_digest",
        "parent_policy_digest",
    ):
        if not _valid_digest(record[key]):
            raise ValueError(f"invalid campaign series evidence {key}")
    for key in (
        "agreement_byte_digest",
        "agreement_report_digest",
        "calibration_byte_digest",
        "calibration_observations_byte_digest",
        "calibration_report_digest",
    ):
        if record[key] is not None and not _valid_digest(record[key]):
            raise ValueError(f"invalid campaign series evidence {key}")
    for key in ("series_index", "revision", "iteration_count", "call_count", "rejected_candidate_count"):
        if (
            not isinstance(record[key], int)
            or isinstance(record[key], bool)
            or record[key] < 0
        ):
            raise ValueError(f"invalid campaign series evidence {key}")
    record["scores"] = scores
    return record


def derive_series_metrics(
    evidence: list[dict[str, Any]],
    *,
    applied_campaign_ids: set[str],
    max_campaigns: int,
    min_improvement: float | None,
    patience: int | None,
    holdout_degradation: float | None,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    current_digest: str | None = None
    streak = 0
    dimensions: tuple[str, ...] = ()
    applied_values: dict[str, list[float]] = {}
    for index, record in enumerate(evidence):
        record_dimensions = tuple(record["dimensions"])
        if not dimensions:
            dimensions = record_dimensions
            applied_values = {name: [] for name in dimensions}
        elif record_dimensions != dimensions:
            raise ValueError("series evidence dimensions changed")
        dataset_digest = record["dataset_digest"]
        if dataset_digest != current_digest:
            if segments:
                segments[-1]["end"] = index - 1
            segments.append(
                {
                    "index": len(segments),
                    "start": index,
                    "end": index,
                    "dataset_digest": dataset_digest,
                }
            )
            current_digest = dataset_digest
            streak = 0
        else:
            segments[-1]["end"] = index
        state = record["terminal_state"]
        scores = record["scores"]
        if state != "failed" and min_improvement is not None:
            no_improvement = scores is None or all(
                scores["validation_gain"][name] < min_improvement
                for name in dimensions
            )
            streak = streak + 1 if no_improvement else 0
        if record["campaign_id"] in applied_campaign_ids:
            if scores is None:
                raise ValueError("applied campaign has no passing scores")
            for name in dimensions:
                applied_values[name].append(scores["holdout_gain"][name])
    cumulative = {
        name: float(
            sum(
                (Decimal(str(value)) for value in values), Decimal(0)
            ).quantize(_SERIES_DECIMAL_QUANTUM)
        )
        for name, values in applied_values.items()
    }
    indicators = {
        "diminishing_returns": (
            min_improvement is not None
            and patience is not None
            and streak >= patience
        ),
        "holdout_degradation": (
            holdout_degradation is not None
            and any(value < -holdout_degradation for value in cumulative.values())
        ),
        "max_campaigns": len(evidence) >= max_campaigns,
    }
    return {
        "dataset_segments": segments,
        "no_improvement_streak": streak,
        "cumulative_applied_holdout_gain": cumulative,
        "indicators": indicators,
    }


_CALIBRATION_REPORT_FIELDS = {
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


def _agreement_trend_item(
    profile: CampaignProfile,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    campaign_id = evidence["campaign_id"]
    if evidence["agreement_byte_digest"] is None:
        return {
            "index": evidence["series_index"],
            "campaign_id": campaign_id,
            "state": "unavailable",
            "reason": "no_agreement_report",
        }
    report = load_agreement_report(profile, campaign_id)
    path = (
        profile.base.data_home
        / "campaigns"
        / campaign_id
        / "evaluator-agreement.json"
    )
    if (
        hashlib.sha256(path.read_bytes()).hexdigest()
        != evidence["agreement_byte_digest"]
        or report["report_digest"] != evidence["agreement_report_digest"]
    ):
        raise ValueError("series agreement report binding mismatch")
    return {
        "index": evidence["series_index"],
        "campaign_id": campaign_id,
        "state": "available",
        "collection_state": report["collection_state"],
        "event_counts": report["event_counts"],
        "availability": report["availability"],
        "metrics": report["metrics"],
    }


def _calibration_trend_item(
    profile: CampaignProfile,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    campaign_id = evidence["campaign_id"]
    if evidence["calibration_byte_digest"] is None:
        return {
            "index": evidence["series_index"],
            "campaign_id": campaign_id,
            "state": "unavailable",
            "reason": "no_calibration_report",
        }
    root = profile.base.data_home / "campaigns" / campaign_id
    report_path = root / "evaluator-calibration.json"
    observations_path = root / "evaluator-calibration-observations.json"
    report_bytes = report_path.read_bytes()
    observation_bytes = observations_path.read_bytes()
    try:
        raw = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid series calibration report JSON") from error
    report = _strict(raw, _CALIBRATION_REPORT_FIELDS, "series calibration report")
    fields = {key: value for key, value in report.items() if key != "report_digest"}
    if (
        report["protocol"] != "kpo.evaluator-calibration/v1"
        or report["report_digest"] != canonical_digest(fields)
        or report["campaign_id"] != campaign_id
        or report["profile_id"] != profile.base.profile_id
        or report["profile_snapshot_digest"]
        != evidence["profile_snapshot_digest"]
        or report["rubric_version"] != profile.base.rubric.rubric_version
        or report["dimensions"] != sorted(profile.base.rubric.dimension_names)
        or hashlib.sha256(report_bytes).hexdigest()
        != evidence["calibration_byte_digest"]
        or report["report_digest"] != evidence["calibration_report_digest"]
        or hashlib.sha256(observation_bytes).hexdigest()
        != evidence["calibration_observations_byte_digest"]
        or report["observation_file_digest"]
        != evidence["calibration_observations_byte_digest"]
    ):
        raise ValueError("series calibration report binding mismatch")
    return {
        "index": evidence["series_index"],
        "campaign_id": campaign_id,
        "state": "available",
        "summary": calibration_summary(report),
        "metrics": report["metrics"],
    }


def series_evidence_report(
    profile: CampaignProfile, series_id: str, *, full: bool = False
) -> dict[str, Any]:
    state = load_series_state(profile, series_id)
    expected_series_parent(profile, state)
    all_evidence: list[dict[str, Any]] = []
    applied: set[str] = set()
    counts = {name: 0 for name in sorted(_TERMINAL_CAMPAIGN_STATES)}
    for entry in state["entries"]:
        campaign_id = entry["campaign_id"]
        evidence = load_campaign_series_evidence(
            profile,
            campaign_id,
            revision=entry["revision"],
            expected_series_id=series_id,
        )
        evidence_path = _evidence_path(profile, campaign_id, entry["revision"])
        if (
            hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            != entry["evidence_digest"]
            or evidence["record_digest"] != entry["evidence_record_digest"]
            or evidence["series_index"] != entry["index"]
            or evidence["revision"] != entry["revision"]
            or evidence["terminal_state"] != entry["campaign_state"]
            or evidence["dataset_digest"] != entry["dataset_digest"]
        ):
            raise ValueError("series evidence entry binding mismatch")
        all_evidence.append(evidence)
    effective_entries = effective_series_entries(state)
    effective_evidence: list[dict[str, Any]] = []
    for entry in effective_entries:
        evidence = next(
            record
            for record in reversed(all_evidence)
            if record["campaign_id"] == entry["campaign_id"]
            and record["revision"] == entry["revision"]
        )
        campaign_path = (
            profile.base.data_home
            / "campaigns"
            / entry["campaign_id"]
            / "campaign.json"
        )
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        if campaign.get("promotion_state") == "applied":
            applied.add(entry["campaign_id"])
        effective_evidence.append(evidence)
        counts[entry["campaign_state"]] += 1
    stopping = state["stopping"]
    metrics = derive_series_metrics(
        effective_evidence,
        applied_campaign_ids=applied,
        max_campaigns=state["max_campaigns"],
        min_improvement=stopping["min_improvement"],
        patience=stopping["patience"],
        holdout_degradation=stopping["holdout_degradation"],
    )
    segment_by_index: dict[int, int] = {}
    for segment in metrics["dataset_segments"]:
        for index in range(segment["start"], segment["end"] + 1):
            segment_by_index[index] = segment["index"]
    learning_curve: dict[str, list[dict[str, Any]]] = {
        name: [] for name in profile.base.rubric.dimension_names
    }
    cumulative: dict[str, Decimal] = {
        name: Decimal(0) for name in profile.base.rubric.dimension_names
    }
    for index, evidence in enumerate(effective_evidence):
        scores = evidence["scores"]
        is_applied = evidence["campaign_id"] in applied
        for name in profile.base.rubric.dimension_names:
            if is_applied and scores is not None:
                cumulative[name] += Decimal(str(scores["holdout_gain"][name]))
            learning_curve[name].append(
                {
                    "index": index,
                    "campaign_id": evidence["campaign_id"],
                    "state": evidence["terminal_state"],
                    "dataset_segment": segment_by_index[index],
                    "validation_gain": (
                        None if scores is None else scores["validation_gain"][name]
                    ),
                    "holdout_gain": (
                        None if scores is None else scores["holdout_gain"][name]
                    ),
                    "regression_gain": (
                        None if scores is None else scores["regression_gain"][name]
                    ),
                    "ablation_gain": (
                        None if scores is None else scores["ablation_gain"][name]
                    ),
                    "applied": is_applied,
                    "cumulative_applied_holdout_gain": float(
                        cumulative[name].quantize(_SERIES_DECIMAL_QUANTUM)
                    ),
                }
            )
    result = {
        "protocol": "kpo.campaign-series-report/v1",
        "series_id": series_id,
        "profile_id": profile.base.profile_id,
        "series_contract_digest": state["series_contract_digest"],
        "state": state["state"],
        "max_campaigns": state["max_campaigns"],
        "stopping": stopping,
        "campaign_count": len(effective_entries),
        "revision_count": len(state["entries"]),
        "campaign_counts": counts,
        "promotion_count": len(applied),
        "dataset_segments": metrics["dataset_segments"],
        "indicators": metrics["indicators"],
        "no_improvement_streak": metrics["no_improvement_streak"],
        "cumulative_applied_holdout_gain": metrics[
            "cumulative_applied_holdout_gain"
        ],
        "learning_curve": learning_curve,
        "agreement_trend": [
            _agreement_trend_item(profile, evidence)
            for evidence in effective_evidence
        ],
        "calibration_trend": [
            _calibration_trend_item(profile, evidence)
            for evidence in effective_evidence
        ],
    }
    if full:
        result["revision_history"] = [
            {
                "index": record["series_index"],
                "campaign_id": record["campaign_id"],
                "revision": record["revision"],
                "state": record["terminal_state"],
                "dataset_digest": record["dataset_digest"],
                "scores": record["scores"],
            }
            for record in all_evidence
        ]
    return result


def series_status(profile: CampaignProfile, series_id: str) -> dict[str, Any]:
    report = series_evidence_report(profile, series_id)
    state = load_series_state(profile, series_id)
    return {
        "series_id": series_id,
        "state": report["state"],
        "campaign_count": report["campaign_count"],
        "revision_count": report["revision_count"],
        "latest_campaign_id": (
            state["entries"][-1]["campaign_id"] if state["entries"] else None
        ),
        "dataset_segment_count": len(report["dataset_segments"]),
        "indicators": report["indicators"],
        "series_contract_digest": report["series_contract_digest"],
    }
