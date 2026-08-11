from __future__ import annotations

import json
import hashlib
import math
import re
import tomllib
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from kpo.dataset import DatasetManifest, load_dataset
from kpo.digest import canonical_digest
from kpo.integrity import IntegrityMonitor
from kpo.models import CandidatePatch, MutationKind
from kpo.profile import (
    CommandProviderConfig,
    ExternalProfile,
    _command_config,
    _source_path,
    _strict,
    load_profile,
)
from kpo.vector_regression import VectorGates


_ARTIFACT_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")
_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class CampaignProfile:
    base: ExternalProfile
    dataset: DatasetManifest
    dataset_path: Path
    frozen_holdout_digest: str | None
    proposer: CommandProviderConfig
    max_iterations: int
    max_provider_calls: int
    gates: VectorGates
    target_root: Path
    allowlist: tuple[str, ...]
    add_directory: str
    policy_paths: Mapping[str, str]
    policy_manifest_path: Path
    sandbox_type: str


@dataclass(frozen=True, slots=True)
class PromotionDestination:
    target_relative_path: str
    manifest_relative_path: str


def validate_campaign_id(value: str) -> str:
    if not isinstance(value, str) or not _CAMPAIGN_ID.fullmatch(value):
        raise ValueError("campaign ID must be a safe path component")
    return value


def campaign_profile_snapshot(profile: CampaignProfile) -> str:
    commands = (profile.base.actor, profile.base.evaluator, profile.proposer)
    return canonical_digest(
        {
            "profile_sources": profile.base.source_digests,
            "dataset_digest": profile.dataset.digest,
            "commands": [
                {
                    "config": command,
                    "executable_digest": hashlib.sha256(
                        Path(command.command[0]).read_bytes()
                    ).hexdigest(),
                }
                for command in commands
            ],
            "observers": [
                {
                    "name": observer.name,
                    "config": observer.config,
                    "executable_digest": hashlib.sha256(
                        Path(observer.config.command[0]).read_bytes()
                    ).hexdigest(),
                }
                for observer in profile.base.observer_evaluators
            ],
            "gates": profile.gates,
            "promotion": {
                "target_root": profile.target_root,
                "allowlist": profile.allowlist,
                "add_directory": profile.add_directory,
            },
        }
    )


def campaign_integrity_monitor(profile: CampaignProfile) -> IntegrityMonitor:
    return IntegrityMonitor.from_relative_digests(
        profile.base.path.parent,
        profile.base.source_digests,
        extra_files=(
            profile.dataset_path,
            Path(profile.base.actor.command[0]),
            Path(profile.base.evaluator.command[0]),
            Path(profile.proposer.command[0]),
            *(
                Path(observer.config.command[0])
                for observer in profile.base.observer_evaluators
            ),
        ),
        target_root=profile.target_root,
        excluded_roots=(profile.base.data_home,),
    )


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _gate(raw: object, names: set[str], label: str) -> Mapping[str, float]:
    value = _strict(raw, names, label=label)
    if set(value) != names:
        raise ValueError(f"{label} gate dimensions must match rubric")
    result: dict[str, float] = {}
    for name, threshold in value.items():
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
        ):
            raise ValueError(f"{label}.{name} threshold must be finite")
        result[name] = float(threshold)
    return MappingProxyType(result)


def _relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a safe relative path")
    return path.as_posix()


def load_campaign_profile(profile_path: Path, *, checkout: Path) -> CampaignProfile:
    base = load_profile(profile_path, checkout=checkout)
    raw = tomllib.loads(base.path.read_text(encoding="utf-8"))
    root = base.path.parent

    dataset_section = _strict(
        raw.get("dataset"), {"manifest", "frozen_holdout_digest"}, label="dataset"
    )
    dataset_path = _source_path(
        root, dataset_section.get("manifest"), label="dataset.manifest"
    )
    frozen = dataset_section.get("frozen_holdout_digest")
    if frozen is not None and (not isinstance(frozen, str) or not frozen.startswith("sha256:")):
        raise ValueError("dataset.frozen_holdout_digest must use sha256 prefix")
    dataset = load_dataset(dataset_path, base.cases)

    proposer = _command_config(
        root,
        raw.get("proposer"),
        label="proposer",
        identity_key="model_id",
    )
    proposer = replace(
        proposer,
        working_directory=base.data_home / "provider-work" / "proposer",
    )
    campaign = _strict(
        raw.get("campaign"),
        {"max_iterations", "max_provider_calls", "gates"},
        label="campaign",
    )
    gate_sections = _strict(
        campaign.get("gates"),
        {"validation", "holdout", "regression", "ablation"},
        label="campaign.gates",
    )
    names = set(base.rubric.dimension_names)
    gates = VectorGates(
        validation=_gate(gate_sections.get("validation"), names, "validation"),
        holdout=_gate(gate_sections.get("holdout"), names, "holdout"),
        regression=_gate(gate_sections.get("regression"), names, "regression"),
        ablation=_gate(gate_sections.get("ablation"), names, "ablation"),
    )

    promotion = _strict(
        raw.get("promotion"),
        {"target_root", "allowlist", "add_directory"},
        label="promotion",
    )
    target_raw = promotion.get("target_root")
    if not isinstance(target_raw, str) or not target_raw:
        raise ValueError("promotion.target_root is required")
    target_path = Path(target_raw)
    target_root = (
        target_path.expanduser().resolve()
        if target_path.is_absolute()
        else (root / target_path).resolve()
    )
    if not target_root.is_dir():
        raise ValueError("promotion.target_root must exist")
    profile_root = root.resolve()
    if target_root != profile_root and profile_root not in target_root.parents:
        raise ValueError("promotion.target_root must resolve inside profile directory")
    allowlist_raw = promotion.get("allowlist")
    if not isinstance(allowlist_raw, list) or not allowlist_raw:
        raise ValueError("promotion.allowlist is required")
    allowlist = tuple(_relative_path(item, "promotion.allowlist entry") for item in allowlist_raw)
    add_directory = _relative_path(
        promotion.get("add_directory"), "promotion.add_directory"
    )
    add_path = Path(add_directory)
    if not any(
        add_path == Path(prefix) or Path(prefix) in add_path.parents
        for prefix in allowlist
    ):
        raise ValueError("promotion.add_directory must be inside allowlist")

    policy_paths: dict[str, str] = {}
    policy_manifest = _source_path(
        root, raw["policy"]["manifest"], label="policy.manifest"
    )
    for line in policy_manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        policy_paths[item["artifact_id"]] = _relative_path(item["path"], "policy.path")
    sandbox_raw = raw.get("sandbox", {"type": "none"})
    sandbox = _strict(sandbox_raw, {"type"}, label="sandbox")
    sandbox_type = sandbox.get("type", "none")
    if sandbox_type not in {"none", "container", "readonly_mount"}:
        raise ValueError("sandbox.type is invalid")
    if sandbox_type != "none":
        raise ValueError(f"sandbox type {sandbox_type} is unavailable in v0.3")
    return CampaignProfile(
        base=base,
        dataset=dataset,
        dataset_path=dataset_path,
        frozen_holdout_digest=frozen,
        proposer=proposer,
        max_iterations=_positive_integer(campaign.get("max_iterations"), "max_iterations"),
        max_provider_calls=_positive_integer(
            campaign.get("max_provider_calls"), "max_provider_calls"
        ),
        gates=gates,
        target_root=target_root,
        allowlist=allowlist,
        add_directory=add_directory,
        policy_paths=MappingProxyType(policy_paths),
        policy_manifest_path=policy_manifest,
        sandbox_type=sandbox_type,
    )


def resolve_candidate_destination(
    profile: CampaignProfile, candidate: CandidatePatch
) -> PromotionDestination:
    profile_root = profile.base.path.parent.resolve()
    target_root = profile.target_root.resolve()
    if candidate.mutation is MutationKind.EDIT:
        if candidate.artifact_id not in profile.policy_paths:
            raise ValueError("edit candidate artifact_id has no policy path")
        manifest_relative = Path(profile.policy_paths[candidate.artifact_id])
        resolved = (profile_root / manifest_relative).resolve()
        try:
            target_relative = resolved.relative_to(target_root)
        except ValueError as error:
            raise ValueError("edit candidate path is not below promotion target_root") from error
    if candidate.mutation is MutationKind.ADD:
        artifact_id = candidate.artifact_id
        if (
            not _ARTIFACT_FILENAME.fullmatch(artifact_id)
            or artifact_id in {".", ".."}
            or ".." in Path(artifact_id).parts
        ):
            raise ValueError("candidate artifact_id is not a safe filename")
        target_relative = Path(profile.add_directory) / f"{artifact_id}.md"
        target_prefix = target_root.relative_to(profile_root)
        manifest_relative = target_prefix / target_relative
    elif candidate.mutation is not MutationKind.EDIT:
        raise ValueError(f"unsupported candidate mutation: {candidate.mutation.value}")

    target_resolved = (target_root / target_relative).resolve()
    manifest_resolved = (profile_root / manifest_relative).resolve()
    if target_resolved != manifest_resolved:
        raise ValueError("promotion destination coordinate mismatch")
    if target_resolved != target_root and target_root not in target_resolved.parents:
        raise ValueError("promotion destination escapes target_root")
    return PromotionDestination(
        target_relative_path=target_relative.as_posix(),
        manifest_relative_path=manifest_relative.as_posix(),
    )
