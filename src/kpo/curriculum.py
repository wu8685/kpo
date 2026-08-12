from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from kpo.dataset import DatasetManifest
from kpo.dataset import (
    DatasetGrowthPreview,
    DatasetGrowthResult,
    DatasetManager,
    load_dataset,
)
from kpo.digest import canonical_digest
from kpo.generalization import GeneralizationManifest
from kpo.models import TaskCase

ACTIONABLE_KINDS = (
    "missing",
    "surplus",
    "contradicted",
    "frame_divergence",
)
_ACTIONABLE_KIND_SET = frozenset(ACTIONABLE_KINDS)
_SCORE_QUANTUM = Decimal("0.000000000001")


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _strict(value: Any, allowed: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    missing = allowed - set(value)
    if missing:
        raise ValueError(f"missing {label} fields: {sorted(missing)}")
    return value


def _unique_string_array(
    value: Any,
    *,
    label: str,
    require_normalized: bool = False,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"{label} must be a non-empty string array")
    normalized = tuple(_normalized(item) for item in value)
    if require_normalized and any(item != clean for item, clean in zip(value, normalized)):
        raise ValueError(f"{label} values must already be normalized")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} contains duplicate normalized values")
    return normalized


def _provenance_array(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError("curriculum provenance must be a non-empty string array")
    if len(set(value)) != len(value):
        raise ValueError("curriculum provenance contains duplicate values")
    return tuple(value)


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class CurriculumCandidate:
    case_id: str
    gap_kinds: tuple[str, ...]
    facets: tuple[str, ...]
    provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("curriculum case_id is required")
        if not self.gap_kinds or any(
            kind not in _ACTIONABLE_KIND_SET for kind in self.gap_kinds
        ):
            raise ValueError("curriculum gap kind is invalid")
        if len(set(self.gap_kinds)) != len(self.gap_kinds):
            raise ValueError("curriculum gap kinds must be unique")
        if not self.facets or len(set(self.facets)) != len(self.facets):
            raise ValueError("curriculum facets must be non-empty and unique")
        if not self.provenance or len(set(self.provenance)) != len(self.provenance):
            raise ValueError("curriculum provenance must be non-empty and unique")

    @property
    def features(self) -> frozenset[str]:
        return frozenset(
            (
                *(f"gap:{kind}" for kind in self.gap_kinds),
                *(f"facet:{facet}" for facet in self.facets),
            )
        )


@dataclass(frozen=True, slots=True)
class CurriculumInbox:
    path: Path
    candidates: tuple[CurriculumCandidate, ...]
    byte_digest: str
    record_digest: str


@dataclass(frozen=True, slots=True)
class CurriculumSelectionStep:
    case_id: str
    relevance: float
    diversity: float
    score: float


@dataclass(frozen=True, slots=True)
class CurriculumSelection:
    protocol: str
    selected_case_ids: tuple[str, ...]
    gap_weights: Mapping[str, float]
    steps: tuple[CurriculumSelectionStep, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class CurriculumSignal:
    campaign_id: str
    series_id: str
    series_index: int
    revision: int
    profile_snapshot_digest: str
    series_state_digest: str
    differential_summary_byte_digest: str
    differential_summary_record_digest: str
    source_campaign_identity_digest: str
    kind_counts: Mapping[str, int]
    gap_weights: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class CurriculumPlan:
    protocol: str
    profile_identity_digest: str
    profile_snapshot_digest: str
    series_identity_digest: str
    series_contract_digest: str
    series_state_digest: str
    current_dataset_digest: str
    current_dataset_byte_digest: str
    frozen_holdout_digest: str
    generalization_digest: str | None
    generalization_byte_digest: str | None
    inbox_byte_digest: str
    inbox_record_digest: str
    curriculum_config_digest: str
    differential_summary_byte_digest: str
    differential_summary_record_digest: str
    source_campaign_identity_digest: str
    kind_counts: Mapping[str, int]
    gap_weights: Mapping[str, float]
    selected_case_ids: tuple[str, ...]
    selected_steps: tuple[CurriculumSelectionStep, ...]
    selected_gap_coverage: Mapping[str, int]
    proposed_dataset_byte_digest: str
    proposed_dataset_record_digest: str
    growth_approval_digest: str
    approval_digest: str


@dataclass(frozen=True, slots=True)
class CurriculumPreview:
    plan: CurriculumPlan
    selection_path: Path
    proposed_path: Path


@dataclass(frozen=True, slots=True)
class _ComputedCurriculum:
    plan: CurriculumPlan
    proposed_content: bytes
    growth_preview: DatasetGrowthPreview


def load_curriculum_inbox(
    path: Path, *, allowed_facets: tuple[str, ...]
) -> CurriculumInbox:
    resolved = path.expanduser().resolve(strict=True)
    try:
        content = resolved.read_bytes()
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("curriculum inbox must contain UTF-8") from error

    registry = tuple(_normalized(facet) for facet in allowed_facets)
    if (
        not registry
        or len(set(registry)) != len(registry)
        or any(raw != clean for raw, clean in zip(allowed_facets, registry))
    ):
        raise ValueError("allowed facets must be non-empty, unique, and normalized")
    allowed = frozenset(registry)

    candidates: list[CurriculumCandidate] = []
    seen_case_ids: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, object_pairs_hook=_reject_duplicate_fields)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid curriculum inbox JSON on line {number}") from error
        data = _strict(
            raw,
            {"case_id", "gap_kinds", "facets", "provenance"},
            label="curriculum candidate",
        )
        case_id = data["case_id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("curriculum case_id is required")
        normalized_case_id = _normalized(case_id)
        if normalized_case_id in seen_case_ids:
            raise ValueError(f"duplicate curriculum case_id: {case_id}")
        seen_case_ids.add(normalized_case_id)

        gap_kinds = _unique_string_array(
            data["gap_kinds"], label="curriculum gap kind", require_normalized=True
        )
        if any(kind not in _ACTIONABLE_KIND_SET for kind in gap_kinds):
            raise ValueError("curriculum gap kind is not actionable")
        facets = _unique_string_array(data["facets"], label="curriculum facets")
        undeclared = set(facets) - allowed
        if undeclared:
            raise ValueError(f"undeclared facet: {sorted(undeclared)}")
        provenance = _provenance_array(data["provenance"])
        candidates.append(
            CurriculumCandidate(case_id, gap_kinds, facets, provenance)
        )

    if not candidates:
        raise ValueError("curriculum inbox must not be empty")
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.case_id))
    return CurriculumInbox(
        path=resolved,
        candidates=ordered,
        byte_digest=hashlib.sha256(content).hexdigest(),
        record_digest=canonical_digest(ordered),
    )


def _content_digest(case: TaskCase) -> str:
    return canonical_digest(
        {
            "prompt": _normalized(case.prompt),
            "visible_context": tuple(
                _normalized(item) for item in case.visible_context
            ),
        }
    )


def validate_curriculum_eligibility(
    inbox: CurriculumInbox,
    *,
    cases: Mapping[str, TaskCase],
    dataset: DatasetManifest,
    generalization: GeneralizationManifest | None,
) -> tuple[CurriculumCandidate, ...]:
    dataset_ids = {_normalized(entry.case_id) for entry in dataset.entries}
    generalization_ids = (
        set()
        if generalization is None
        else {_normalized(entry.case_id) for entry in generalization.entries}
    )
    existing_case_ids = {
        entry.case_id for entry in dataset.entries
    } | (
        set()
        if generalization is None
        else {entry.case_id for entry in generalization.entries}
    )
    existing_cases = [cases[case_id] for case_id in existing_case_ids]
    existing_prompts = {_normalized(case.prompt) for case in existing_cases}
    existing_contexts = {
        _normalized(item)
        for case in existing_cases
        for item in case.visible_context
    }
    existing_content = {_content_digest(case) for case in existing_cases}

    seen_prompts: set[str] = set()
    seen_contexts: set[str] = set()
    seen_content: set[str] = set()
    for candidate in inbox.candidates:
        case_id = _normalized(candidate.case_id)
        if case_id in dataset_ids:
            raise ValueError("curriculum candidate overlaps campaign dataset")
        if case_id in generalization_ids:
            raise ValueError("curriculum candidate overlaps generalization manifest")
        if candidate.case_id not in cases:
            raise ValueError("curriculum candidate is absent from case manifest")
        case = cases[candidate.case_id]
        prompt = _normalized(case.prompt)
        contexts = tuple(_normalized(item) for item in case.visible_context)
        context_set = set(contexts)
        content = _content_digest(case)
        if (
            prompt in existing_prompts
            or bool(context_set & existing_contexts)
            or content in existing_content
        ):
            raise ValueError("curriculum candidate content contamination detected")
        if (
            prompt in seen_prompts
            or bool(context_set & seen_contexts)
            or content in seen_content
            or len(context_set) != len(contexts)
        ):
            raise ValueError("duplicate candidate content detected")
        seen_prompts.add(prompt)
        seen_contexts.update(context_set)
        seen_content.add(content)
    return inbox.candidates


def load_curriculum_signal(profile: Any, series_id: str) -> CurriculumSignal:
    # Imports stay local so the metadata primitives above remain independent of
    # orchestration and can be reused by strict profile loaders.
    from kpo.campaign_series import (
        effective_series_entries,
        load_campaign_series_evidence,
        load_series_state,
    )
    from kpo.reasoning_differential import load_differential_summary

    if profile.curriculum is None:
        raise ValueError("campaign profile has no curriculum configuration")
    state = load_series_state(profile, series_id)
    if state["state"] != "active":
        raise ValueError(f"campaign series is {state['state']}")
    entries = tuple(reversed(effective_series_entries(state)))
    ordered = (
        *(entry for entry in entries if entry["campaign_state"] == "promotion_previewed"),
        *(entry for entry in entries if entry["campaign_state"] != "promotion_previewed"),
    )
    for entry in ordered:
        campaign_id = entry["campaign_id"]
        revision = entry["revision"]
        evidence = load_campaign_series_evidence(
            profile,
            campaign_id,
            revision=revision,
            expected_series_id=series_id,
        )
        evidence_path = (profile.base.data_home / entry["evidence_path"]).resolve()
        data_home = profile.base.data_home.resolve()
        if evidence_path != data_home and data_home not in evidence_path.parents:
            raise ValueError("campaign series evidence path escapes data home")
        evidence_bytes = evidence_path.read_bytes()
        if hashlib.sha256(evidence_bytes).hexdigest() != entry["evidence_digest"]:
            raise ValueError("campaign series evidence byte digest mismatch")
        if (
            evidence["record_digest"] != entry["evidence_record_digest"]
            or evidence["campaign_id"] != campaign_id
            or evidence["series_id"] != series_id
            or evidence["series_index"] != entry["index"]
            or evidence["revision"] != revision
            or evidence["terminal_state"] != entry["campaign_state"]
            or evidence["profile_snapshot_digest"]
            != entry["profile_snapshot_digest"]
            or evidence["dataset_digest"] != entry["dataset_digest"]
        ):
            raise ValueError("campaign series evidence binding mismatch")
        summary_byte_digest = evidence["differential_summary_byte_digest"]
        summary_record_digest = evidence["differential_summary_record_digest"]
        if summary_byte_digest is None or summary_record_digest is None:
            continue
        summary = load_differential_summary(
            profile.base.data_home,
            campaign_id,
            profile_snapshot_digest=evidence["profile_snapshot_digest"],
            expected_byte_digest=summary_byte_digest,
        )
        if summary["record_digest"] != summary_record_digest:
            raise ValueError("differential summary evidence binding mismatch")

        campaign_path = (
            profile.base.data_home / "campaigns" / campaign_id / "campaign.json"
        )
        try:
            campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid curriculum source campaign state") from error
        expected_campaign_values = {
            "campaign_id": campaign_id,
            "series_id": series_id,
            "series_index": entry["index"],
            "series_evidence_revision": revision,
            "state": entry["campaign_state"],
            "profile_snapshot_digest": entry["profile_snapshot_digest"],
            "series_evidence_path": entry["evidence_path"],
            "series_evidence_digest": entry["evidence_digest"],
            "series_evidence_record_digest": entry["evidence_record_digest"],
            "differential_analysis_digest": summary_byte_digest,
        }
        if not isinstance(campaign, dict) or any(
            campaign.get(key) != value
            for key, value in expected_campaign_values.items()
        ):
            raise ValueError("curriculum source campaign binding mismatch")
        bound_summary = campaign.get("differential_analysis_summary")
        if (
            not isinstance(bound_summary, dict)
            or bound_summary.get("record_digest") != summary_record_digest
            or bound_summary != summary
        ):
            raise ValueError("campaign differential summary binding mismatch")

        counts = {
            kind: summary["kind_counts"][kind] for kind in ACTIONABLE_KINDS
        }
        total = sum(counts.values())
        if summary["available_count"] < 1 or total == 0:
            continue
        weights = MappingProxyType(
            {kind: counts[kind] / total for kind in ACTIONABLE_KINDS}
        )
        identity = {
            "campaign_id": campaign_id,
            "profile_id": profile.base.profile_id,
            "series_id": series_id,
            "series_index": entry["index"],
            "revision": revision,
        }
        return CurriculumSignal(
            campaign_id=campaign_id,
            series_id=series_id,
            series_index=entry["index"],
            revision=revision,
            profile_snapshot_digest=entry["profile_snapshot_digest"],
            series_state_digest=state["state_digest"],
            differential_summary_byte_digest=summary_byte_digest,
            differential_summary_record_digest=summary_record_digest,
            source_campaign_identity_digest=canonical_digest(identity),
            kind_counts=MappingProxyType(counts),
            gap_weights=weights,
        )
    raise ValueError("curriculum signal unavailable")


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> Decimal:
    union = left | right
    return Decimal(len(left & right)) / Decimal(len(union))


def select_curriculum(
    candidates: tuple[CurriculumCandidate, ...],
    *,
    kind_counts: Mapping[str, int],
    max_selection: int,
    diversity_weight: float,
) -> CurriculumSelection:
    if set(kind_counts) != _ACTIONABLE_KIND_SET:
        raise ValueError("kind_counts must contain exactly the actionable kinds")
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in kind_counts.values()
    ):
        raise ValueError("kind_counts values must be non-negative integers")
    total = sum(kind_counts.values())
    if total == 0:
        raise ValueError("curriculum signal unavailable")
    if not isinstance(max_selection, int) or isinstance(max_selection, bool) or max_selection < 1:
        raise ValueError("max_selection must be a positive integer")
    if (
        not isinstance(diversity_weight, (int, float))
        or isinstance(diversity_weight, bool)
        or not math.isfinite(diversity_weight)
        or not 0 <= diversity_weight <= 1
    ):
        raise ValueError("diversity_weight must be finite and between zero and one")
    if not candidates:
        raise ValueError("curriculum candidates must not be empty")
    if len({_normalized(candidate.case_id) for candidate in candidates}) != len(candidates):
        raise ValueError("curriculum candidate case_id values must be unique")

    total_decimal = Decimal(total)
    weights_decimal = {
        kind: Decimal(kind_counts[kind]) / total_decimal for kind in ACTIONABLE_KINDS
    }
    public_weights = MappingProxyType(
        {kind: float(weights_decimal[kind]) for kind in ACTIONABLE_KINDS}
    )
    diversity_decimal = Decimal(str(diversity_weight))
    relevance_factor = Decimal(1) - diversity_decimal
    remaining = {candidate.case_id: candidate for candidate in candidates}
    selected: list[CurriculumCandidate] = []
    steps: list[CurriculumSelectionStep] = []

    while remaining and len(selected) < max_selection:
        ranked: list[tuple[Decimal, Decimal, str, CurriculumCandidate, Decimal]] = []
        for candidate in remaining.values():
            relevance = min(
                Decimal(1),
                sum(
                    (weights_decimal[kind] for kind in candidate.gap_kinds),
                    start=Decimal(0),
                ),
            )
            if not selected:
                diversity = Decimal(1)
            else:
                diversity = Decimal(1) - max(
                    _jaccard(candidate.features, item.features) for item in selected
                )
            score = _rounded(
                relevance_factor * relevance + diversity_decimal * diversity
            )
            ranked.append((score, relevance, candidate.case_id, candidate, diversity))
        score, relevance, _, chosen, diversity = sorted(
            ranked, key=lambda item: (-item[0], -item[1], item[2])
        )[0]
        selected.append(chosen)
        del remaining[chosen.case_id]
        steps.append(
            CurriculumSelectionStep(
                case_id=chosen.case_id,
                relevance=float(_rounded(relevance)),
                diversity=float(_rounded(diversity)),
                score=float(score),
            )
        )

    fields = {
        "protocol": "kpo.curriculum-selection/v1",
        "selected_case_ids": tuple(item.case_id for item in selected),
        "gap_weights": public_weights,
        "steps": tuple(steps),
    }
    return CurriculumSelection(**fields, digest=canonical_digest(fields))


def _step_value(step: CurriculumSelectionStep) -> dict[str, Any]:
    return {
        "case_id": step.case_id,
        "relevance": step.relevance,
        "diversity": step.diversity,
        "score": step.score,
    }


def _plan_fields(plan: CurriculumPlan) -> dict[str, Any]:
    return {
        "protocol": plan.protocol,
        "profile_identity_digest": plan.profile_identity_digest,
        "profile_snapshot_digest": plan.profile_snapshot_digest,
        "series_identity_digest": plan.series_identity_digest,
        "series_contract_digest": plan.series_contract_digest,
        "series_state_digest": plan.series_state_digest,
        "current_dataset_digest": plan.current_dataset_digest,
        "current_dataset_byte_digest": plan.current_dataset_byte_digest,
        "frozen_holdout_digest": plan.frozen_holdout_digest,
        "generalization_digest": plan.generalization_digest,
        "generalization_byte_digest": plan.generalization_byte_digest,
        "inbox_byte_digest": plan.inbox_byte_digest,
        "inbox_record_digest": plan.inbox_record_digest,
        "curriculum_config_digest": plan.curriculum_config_digest,
        "differential_summary_byte_digest": (
            plan.differential_summary_byte_digest
        ),
        "differential_summary_record_digest": (
            plan.differential_summary_record_digest
        ),
        "source_campaign_identity_digest": (
            plan.source_campaign_identity_digest
        ),
        "kind_counts": dict(plan.kind_counts),
        "gap_weights": dict(plan.gap_weights),
        "selected_case_ids": list(plan.selected_case_ids),
        "selected_steps": [_step_value(step) for step in plan.selected_steps],
        "selected_gap_coverage": dict(plan.selected_gap_coverage),
        "proposed_dataset_byte_digest": plan.proposed_dataset_byte_digest,
        "proposed_dataset_record_digest": plan.proposed_dataset_record_digest,
        "growth_approval_digest": plan.growth_approval_digest,
    }


def _plan_value(plan: CurriculumPlan) -> dict[str, Any]:
    return _plan_fields(plan) | {"approval_digest": plan.approval_digest}


def _proposed_manifest_content(
    current: bytes,
    *,
    selected: tuple[CurriculumCandidate, ...],
) -> bytes:
    content = current
    if content and not content.endswith(b"\n"):
        content += b"\n"
    for candidate in selected:
        row = {
            "case_id": candidate.case_id,
            "partition": "train",
            "weight": 1.0,
            "provenance": list(candidate.provenance),
        }
        content += (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    return content


def _curriculum_runtime_root(profile: Any, *, create: bool) -> Path:
    data_home = profile.base.data_home.expanduser().resolve()
    root = data_home / "curriculum"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        return root
    resolved = root.resolve()
    if resolved != data_home and data_home not in resolved.parents:
        raise ValueError("curriculum runtime root escapes data home")
    return resolved


def _authorized_runtime_child(parent: Path, name: str, *, create: bool) -> Path:
    child = parent / name
    if create:
        child.mkdir(parents=True, exist_ok=True)
    if not child.exists():
        return child
    resolved = child.resolve()
    if resolved != parent and parent not in resolved.parents:
        raise ValueError("curriculum runtime child escapes authorized root")
    return resolved


def _compute_curriculum(profile: Any, series_id: str) -> _ComputedCurriculum:
    from kpo.campaign_series import build_series_contract
    from kpo.campaign_profile import (
        campaign_integrity_monitor,
        campaign_profile_snapshot,
    )

    config = profile.curriculum
    if config is None:
        raise ValueError("campaign profile has no curriculum configuration")
    campaign_integrity_monitor(profile).verify()
    current_profile_snapshot = campaign_profile_snapshot(profile)
    signal = load_curriculum_signal(profile, series_id)
    inbox = load_curriculum_inbox(
        config.inbox, allowed_facets=config.allowed_facets
    )
    eligible = validate_curriculum_eligibility(
        inbox,
        cases=profile.base.cases,
        dataset=profile.dataset,
        generalization=(
            None if profile.generalization is None else profile.generalization.manifest
        ),
    )
    selection = select_curriculum(
        eligible,
        kind_counts=signal.kind_counts,
        max_selection=config.max_selection,
        diversity_weight=config.diversity_weight,
    )
    by_id = {candidate.case_id: candidate for candidate in eligible}
    selected = tuple(by_id[case_id] for case_id in selection.selected_case_ids)

    current_content = profile.dataset_path.read_bytes()
    current = load_dataset(profile.dataset_path, profile.base.cases)
    if current.digest != profile.dataset.digest:
        raise ValueError("stale campaign profile dataset")
    proposed_content = _proposed_manifest_content(
        current_content, selected=selected
    )
    runtime_root = _curriculum_runtime_root(profile, create=True)
    staging_root = _authorized_runtime_child(
        runtime_root, ".staging", create=True
    )
    handle, temporary_name = tempfile.mkstemp(
        prefix="proposed-", suffix=".jsonl", dir=staging_root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(proposed_content)
            stream.flush()
            os.fsync(stream.fileno())
        proposed = load_dataset(temporary, profile.base.cases)
        if profile.generalization is not None:
            from kpo.generalization import load_generalization_manifest

            loaded_generalization = load_generalization_manifest(
                profile.generalization.manifest.path,
                profile.base.cases,
                proposed,
            )
            if (
                loaded_generalization.digest
                != profile.generalization.manifest.digest
                or loaded_generalization.source_digest
                != profile.generalization.manifest.source_digest
            ):
                raise ValueError("stale generalization manifest")
        manager = DatasetManager(profile.base.data_home)
        growth = manager.preview_growth(
            current,
            proposed,
            manifest_path=profile.dataset_path,
            proposed_manifest_path=temporary,
        )
    finally:
        temporary.unlink(missing_ok=True)

    coverage = {
        kind: sum(kind in candidate.gap_kinds for candidate in selected)
        for kind in ACTIONABLE_KINDS
    }
    coverage = {kind: count for kind, count in coverage.items() if count}
    contract = build_series_contract(profile)
    config_digest = canonical_digest(
        {
            "protocol": "kpo.curriculum-selection/v1",
            "max_selection": config.max_selection,
            "diversity_weight": config.diversity_weight,
            "facet_registry_digest": canonical_digest(config.allowed_facets),
        }
    )
    generalization = (
        None if profile.generalization is None else profile.generalization.manifest
    )
    fields = {
        "protocol": "kpo.curriculum-selection/v1",
        "profile_identity_digest": canonical_digest(profile.base.profile_id),
        "profile_snapshot_digest": current_profile_snapshot,
        "series_identity_digest": canonical_digest(
            {"profile_id": profile.base.profile_id, "series_id": series_id}
        ),
        "series_contract_digest": contract.digest,
        "series_state_digest": signal.series_state_digest,
        "current_dataset_digest": current.digest,
        "current_dataset_byte_digest": hashlib.sha256(current_content).hexdigest(),
        "frozen_holdout_digest": current.holdout_digest,
        "generalization_digest": (
            None if generalization is None else generalization.digest
        ),
        "generalization_byte_digest": (
            None if generalization is None else generalization.source_digest
        ),
        "inbox_byte_digest": inbox.byte_digest,
        "inbox_record_digest": inbox.record_digest,
        "curriculum_config_digest": config_digest,
        "differential_summary_byte_digest": (
            signal.differential_summary_byte_digest
        ),
        "differential_summary_record_digest": (
            signal.differential_summary_record_digest
        ),
        "source_campaign_identity_digest": (
            signal.source_campaign_identity_digest
        ),
        "kind_counts": MappingProxyType(dict(signal.kind_counts)),
        "gap_weights": MappingProxyType(dict(signal.gap_weights)),
        "selected_case_ids": selection.selected_case_ids,
        "selected_steps": selection.steps,
        "selected_gap_coverage": MappingProxyType(coverage),
        "proposed_dataset_byte_digest": hashlib.sha256(
            proposed_content
        ).hexdigest(),
        "proposed_dataset_record_digest": proposed.digest,
        "growth_approval_digest": growth.approval_digest,
    }
    approval_fields = {
        key: (
            dict(value)
            if isinstance(value, Mapping)
            else [_step_value(item) for item in value]
            if key == "selected_steps"
            else list(value)
            if key == "selected_case_ids"
            else value
        )
        for key, value in fields.items()
    }
    plan = CurriculumPlan(
        **fields,
        approval_digest=canonical_digest(approval_fields),
    )
    return _ComputedCurriculum(plan, proposed_content, growth)


def _preview_paths(profile: Any, approval_digest: str) -> tuple[Path, Path, Path]:
    if (
        not isinstance(approval_digest, str)
        or len(approval_digest) != 64
        or any(
            character not in "0123456789abcdef" for character in approval_digest
        )
    ):
        raise ValueError("invalid curriculum approval digest")
    runtime_root = _curriculum_runtime_root(profile, create=False)
    previews = _authorized_runtime_child(runtime_root, "previews", create=False)
    root = previews / approval_digest
    if root.exists():
        resolved = root.resolve()
        if resolved != previews and previews not in resolved.parents:
            raise ValueError("curriculum runtime preview escapes authorized root")
        root = resolved
    return root, root / "selection.json", root / "proposed-dataset.jsonl"


def _persist_curriculum_preview(
    profile: Any, computed: _ComputedCurriculum
) -> CurriculumPreview:
    root, selection_path, proposed_path = _preview_paths(
        profile, computed.plan.approval_digest
    )
    selection_content = (
        json.dumps(
            _plan_value(computed.plan),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if root.exists():
        if (
            not selection_path.is_file()
            or not proposed_path.is_file()
            or selection_path.read_bytes() != selection_content
            or proposed_path.read_bytes() != computed.proposed_content
        ):
            raise FileExistsError("immutable curriculum preview differs")
        return CurriculumPreview(computed.plan, selection_path, proposed_path)

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="preview-", dir=root.parent))
    try:
        (staging / "selection.json").write_bytes(selection_content)
        (staging / "proposed-dataset.jsonl").write_bytes(computed.proposed_content)
        try:
            os.rename(staging, root)
        except OSError:
            if not root.exists():
                raise
            if (
                selection_path.read_bytes() != selection_content
                or proposed_path.read_bytes() != computed.proposed_content
            ):
                raise FileExistsError("immutable curriculum preview differs")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return CurriculumPreview(computed.plan, selection_path, proposed_path)


def preview_curriculum(profile: Any, series_id: str) -> CurriculumPreview:
    from kpo.campaign_series import load_series_state, series_mutation

    initial = load_series_state(profile, series_id)
    with series_mutation(
        profile,
        series_id,
        operation="curriculum_preview",
        initial_state_digest=initial["state_digest"],
    ):
        current = load_series_state(profile, series_id)
        if current["state_digest"] != initial["state_digest"]:
            raise ValueError("stale curriculum series state")
        return _persist_curriculum_preview(
            profile, _compute_curriculum(profile, series_id)
        )


def apply_curriculum(
    profile: Any,
    series_id: str,
    *,
    approval_digest: str,
) -> DatasetGrowthResult:
    from kpo.campaign_series import load_series_state, series_mutation

    root, selection_path, proposed_path = _preview_paths(profile, approval_digest)
    if not root.is_dir() or not selection_path.is_file() or not proposed_path.is_file():
        raise ValueError("curriculum approval preview does not exist")
    persisted_plan = _load_persisted_plan(profile, approval_digest)
    initial = load_series_state(profile, series_id)
    with series_mutation(
        profile,
        series_id,
        operation="curriculum_apply",
        initial_state_digest=initial["state_digest"],
        approval_digest=approval_digest,
        growth_approval_digest=persisted_plan["growth_approval_digest"],
    ) as update_lock:
        current = load_series_state(profile, series_id)
        if current["state_digest"] != initial["state_digest"]:
            raise ValueError("stale curriculum series state")
        computed = _compute_curriculum(profile, series_id)
        if computed.plan.approval_digest != approval_digest:
            raise ValueError("stale curriculum approval")
        update_lock(growth_approval_digest=computed.growth_preview.approval_digest)
        if persisted_plan != _plan_value(computed.plan):
            raise ValueError("curriculum selection plan binding mismatch")
        proposed_content = proposed_path.read_bytes()
        if (
            hashlib.sha256(proposed_content).hexdigest()
            != computed.plan.proposed_dataset_byte_digest
            or proposed_content != computed.proposed_content
        ):
            raise ValueError("curriculum proposed dataset binding mismatch")
        return DatasetManager(profile.base.data_home).apply_growth(
            computed.growth_preview,
            approval_digest=computed.plan.growth_approval_digest,
        )


_PERSISTED_PLAN_FIELDS = {
    "protocol",
    "profile_identity_digest",
    "profile_snapshot_digest",
    "series_identity_digest",
    "series_contract_digest",
    "series_state_digest",
    "current_dataset_digest",
    "current_dataset_byte_digest",
    "frozen_holdout_digest",
    "generalization_digest",
    "generalization_byte_digest",
    "inbox_byte_digest",
    "inbox_record_digest",
    "curriculum_config_digest",
    "differential_summary_byte_digest",
    "differential_summary_record_digest",
    "source_campaign_identity_digest",
    "kind_counts",
    "gap_weights",
    "selected_case_ids",
    "selected_steps",
    "selected_gap_coverage",
    "proposed_dataset_byte_digest",
    "proposed_dataset_record_digest",
    "growth_approval_digest",
    "approval_digest",
}


def _load_persisted_plan(profile: Any, approval_digest: str) -> dict[str, Any]:
    _, selection_path, proposed_path = _preview_paths(profile, approval_digest)
    if not selection_path.is_file() or not proposed_path.is_file():
        raise ValueError("curriculum approval preview does not exist")
    try:
        value = json.loads(selection_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid curriculum selection plan") from error
    if not isinstance(value, dict) or set(value) != _PERSISTED_PLAN_FIELDS:
        raise ValueError("curriculum selection plan fields do not match protocol")
    fields = {key: item for key, item in value.items() if key != "approval_digest"}
    if (
        value["protocol"] != "kpo.curriculum-selection/v1"
        or value["approval_digest"] != approval_digest
        or canonical_digest(fields) != approval_digest
    ):
        raise ValueError("curriculum selection plan approval mismatch")
    for key in (
        "profile_identity_digest",
        "profile_snapshot_digest",
        "series_identity_digest",
        "series_contract_digest",
        "series_state_digest",
        "current_dataset_digest",
        "current_dataset_byte_digest",
        "frozen_holdout_digest",
        "inbox_byte_digest",
        "inbox_record_digest",
        "curriculum_config_digest",
        "differential_summary_byte_digest",
        "differential_summary_record_digest",
        "source_campaign_identity_digest",
        "proposed_dataset_byte_digest",
        "proposed_dataset_record_digest",
        "growth_approval_digest",
    ):
        item = value[key]
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            raise ValueError(f"invalid curriculum plan {key}")
    if value["profile_identity_digest"] != canonical_digest(profile.base.profile_id):
        raise ValueError("curriculum plan profile mismatch")
    proposed = proposed_path.read_bytes()
    if hashlib.sha256(proposed).hexdigest() != value["proposed_dataset_byte_digest"]:
        raise ValueError("curriculum proposed dataset binding mismatch")
    return value


def _stopped_state_matches_plan(state: Mapping[str, Any], digest: str) -> bool:
    if state.get("state") != "stopped_by_owner":
        return False
    fields = {key: value for key, value in state.items() if key != "state_digest"}
    fields["state"] = "active"
    return canonical_digest(fields) == digest


def recover_curriculum(
    profile: Any, *, approval_digest: str
) -> DatasetGrowthResult:
    from kpo.campaign_series import load_series_state

    plan = _load_persisted_plan(profile, approval_digest)
    locks_root = profile.base.data_home / "series" / ".locks"
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(locks_root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("operation") == "curriculum_apply"
            and value.get("approval_digest") == approval_digest
        ):
            matches.append((path, value))
    if len(matches) != 1:
        raise ValueError("matching stale curriculum_apply lock is required")
    lock_path, lock = matches[0]
    expected_lock_fields = {
        "protocol",
        "series_id",
        "operation",
        "campaign_id",
        "series_index",
        "revision",
        "initial_state_digest",
        "pid",
        "approval_digest",
        "growth_approval_digest",
    }
    if set(lock) != expected_lock_fields:
        raise ValueError("curriculum recovery requires manual recovery")
    pid = lock["pid"]
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise ValueError("series concurrent mutation") from error
        else:
            raise ValueError("series concurrent mutation")
    series_id = lock["series_id"]
    if (
        lock["protocol"] != "kpo.campaign-series-lock/v1"
        or not isinstance(series_id, str)
        or lock["growth_approval_digest"] != plan["growth_approval_digest"]
        or lock["initial_state_digest"] != plan["series_state_digest"]
        or plan["series_identity_digest"]
        != canonical_digest(
            {"profile_id": profile.base.profile_id, "series_id": series_id}
        )
    ):
        raise ValueError("curriculum recovery requires manual recovery")
    state = load_series_state(profile, series_id)
    if (
        state["state_digest"] != plan["series_state_digest"]
        and not _stopped_state_matches_plan(state, plan["series_state_digest"])
    ):
        raise ValueError("curriculum recovery requires manual recovery")

    manager = DatasetManager(profile.base.data_home)
    growth_approval = plan["growth_approval_digest"]
    journal = manager.journal_dir / f"growth-{growth_approval}.json"
    manifest_digest = hashlib.sha256(profile.dataset_path.read_bytes()).hexdigest()
    if not journal.exists():
        if manifest_digest != plan["current_dataset_byte_digest"]:
            raise ValueError("curriculum recovery requires manual recovery")
        lock_path.unlink()
        return DatasetGrowthResult("unchanged", growth_approval, journal)
    try:
        journal_value = json.loads(journal.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("curriculum recovery requires manual recovery") from error
    expected_journal_fields = {
        "state",
        "approval_digest",
        "manifest_path",
        "snapshot_path",
        "original_digest",
        "new_digest",
    }
    if not isinstance(journal_value, dict):
        raise ValueError("curriculum recovery requires manual recovery")
    snapshot_path = Path(journal_value.get("snapshot_path", "")).resolve()
    expected_snapshot = (
        manager.root / "snapshots" / growth_approval / "original.bin"
    ).resolve()
    if (
        set(journal_value) != expected_journal_fields
        or journal_value["approval_digest"] != growth_approval
        or Path(journal_value["manifest_path"]).resolve()
        != profile.dataset_path.resolve()
        or journal_value["original_digest"] != plan["current_dataset_byte_digest"]
        or journal_value["new_digest"] != plan["proposed_dataset_byte_digest"]
        or snapshot_path != expected_snapshot
        or not snapshot_path.is_file()
        or hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        != plan["current_dataset_byte_digest"]
    ):
        raise ValueError("curriculum recovery requires manual recovery")
    if journal_value["state"] == "prepared":
        if manifest_digest != plan["proposed_dataset_byte_digest"]:
            raise ValueError("curriculum recovery requires manual recovery")
        result = manager.recover_growth(growth_approval)
        if (
            hashlib.sha256(profile.dataset_path.read_bytes()).hexdigest()
            != plan["current_dataset_byte_digest"]
        ):
            raise ValueError("curriculum recovery requires manual recovery")
    elif journal_value["state"] == "committed":
        if manifest_digest != plan["proposed_dataset_byte_digest"]:
            raise ValueError("curriculum recovery requires manual recovery")
        result = DatasetGrowthResult("committed", growth_approval, journal)
    elif journal_value["state"] == "rolled_back":
        if manifest_digest != plan["current_dataset_byte_digest"]:
            raise ValueError("curriculum recovery requires manual recovery")
        result = DatasetGrowthResult("rolled_back", growth_approval, journal)
    else:
        raise ValueError("curriculum recovery requires manual recovery")
    lock_path.unlink()
    return result
