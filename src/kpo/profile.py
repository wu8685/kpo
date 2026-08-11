from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from kpo.models import PolicyArtifact, PolicySnapshot, ReferenceArtifact, TaskCase


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OBSERVER_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class RubricDimension:
    name: str
    description: str
    weight: float


@dataclass(frozen=True, slots=True)
class Rubric:
    rubric_version: str
    dimensions: tuple[RubricDimension, ...]

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(dimension.name for dimension in self.dimensions)


@dataclass(frozen=True, slots=True)
class CommandProviderConfig:
    command: tuple[str, ...]
    identity: str
    timeout_seconds: float
    pass_env: tuple[str, ...]
    working_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class ObserverEvaluatorConfig:
    name: str
    config: CommandProviderConfig


@dataclass(frozen=True, slots=True)
class ExternalProfile:
    path: Path
    profile_id: str
    data_home: Path
    policy: PolicySnapshot
    cases: Mapping[str, TaskCase]
    references: Mapping[str, ReferenceArtifact]
    reference_source_digests: Mapping[str, str]
    rubric: Rubric
    actor: CommandProviderConfig
    evaluator: CommandProviderConfig
    observer_evaluators: tuple[ObserverEvaluatorConfig, ...]
    source_digests: Mapping[str, str]


def _strict(value: Any, allowed: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    return value


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _source_path(root: Path, raw: Any, *, label: str) -> Path:
    value = _require_string(raw, label=label)
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=True)
    else:
        resolved = (root / candidate).resolve(strict=True)
    if not _inside(resolved, root):
        raise ValueError(f"{label} resolves outside profile directory")
    if not resolved.is_file():
        raise ValueError(f"{label} must resolve to a file")
    return resolved


def _read_jsonl(path: Path, *, label: str) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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


def _command_config(
    root: Path, raw: Any, *, label: str, identity_key: str
) -> CommandProviderConfig:
    data = _strict(
        raw,
        {"adapter", "command", identity_key, "timeout_seconds", "pass_env"},
        label=label,
    )
    if data.get("adapter") != "command":
        raise ValueError(f"{label}.adapter must be command")
    command_raw = data.get("command")
    if (
        not isinstance(command_raw, list)
        or not command_raw
        or any(not isinstance(item, str) or not item for item in command_raw)
    ):
        raise ValueError(f"{label}.command must be a non-empty string array")
    executable = Path(command_raw[0])
    if executable.is_absolute():
        resolved_executable = executable.resolve(strict=True)
    elif len(executable.parts) > 1:
        resolved_executable = (root / executable).resolve(strict=True)
    else:
        found = shutil.which(command_raw[0])
        if found is None:
            raise ValueError(f"{label}.command executable was not found")
        resolved_executable = Path(found).resolve(strict=True)
    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise ValueError(f"{label}.command executable is not executable")
    timeout = data.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"{label}.timeout_seconds must be positive")
    pass_env = data.get("pass_env")
    if not isinstance(pass_env, list) or any(
        not isinstance(name, str) or not _ENV_NAME.fullmatch(name)
        for name in pass_env
    ):
        raise ValueError(f"{label}.pass_env contains an invalid environment name")
    if len(set(pass_env)) != len(pass_env):
        raise ValueError(f"{label}.pass_env contains duplicate names")
    return CommandProviderConfig(
        command=(str(resolved_executable), *command_raw[1:]),
        identity=_require_string(data.get(identity_key), label=f"{label}.{identity_key}"),
        timeout_seconds=float(timeout),
        pass_env=tuple(pass_env),
    )


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile(profile_path: Path, *, checkout: Path) -> ExternalProfile:
    checkout_root = checkout.expanduser().resolve()
    path = profile_path.expanduser().resolve(strict=True)
    if _inside(path, checkout_root):
        raise ValueError("profile must live outside the code checkout")
    root = path.parent
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    data = _strict(
        raw,
        {
            "schema_version",
            "profile_id",
            "data_home",
            "policy",
            "cases",
            "references",
            "rubric",
            "actor",
            "evaluator",
            "observer_evaluators",
            "evaluator_audit",
            "dataset",
            "proposer",
            "campaign",
            "promotion",
            "sandbox",
        },
        label="profile",
    )
    if data.get("schema_version") != 1:
        raise ValueError("profile schema_version must be 1")
    profile_id = _require_string(data.get("profile_id"), label="profile_id")

    section_paths: dict[str, Path] = {}
    for section, key in (
        ("policy", "manifest"),
        ("cases", "manifest"),
        ("references", "manifest"),
        ("rubric", "path"),
    ):
        section_data = _strict(data.get(section), {key}, label=section)
        section_paths[section] = _source_path(
            root, section_data.get(key), label=f"{section}.{key}"
        )

    policy_artifacts: list[PolicyArtifact] = []
    source_paths = {path, *section_paths.values()}
    policy_ids: set[str] = set()
    for record in _read_jsonl(section_paths["policy"], label="policy manifest"):
        item = _strict(record, {"artifact_id", "path", "version"}, label="policy")
        artifact_id = _require_string(item.get("artifact_id"), label="artifact_id")
        if artifact_id in policy_ids:
            raise ValueError(f"duplicate policy artifact_id: {artifact_id}")
        policy_ids.add(artifact_id)
        version = item.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("policy version must be a positive integer")
        content_path = _source_path(root, item.get("path"), label="policy.path")
        source_paths.add(content_path)
        policy_artifacts.append(
            PolicyArtifact(
                artifact_id=artifact_id,
                content=content_path.read_text(encoding="utf-8"),
                version=version,
            )
        )

    cases: dict[str, TaskCase] = {}
    for record in _read_jsonl(section_paths["cases"], label="case manifest"):
        item = _strict(
            record,
            {
                "case_id",
                "prompt",
                "visible_context",
                "hidden_reference_ids",
                "tags",
                "information_cutoff",
            },
            label="case",
        )
        case_id = _require_string(item.get("case_id"), label="case_id")
        if case_id in cases:
            raise ValueError(f"duplicate case_id: {case_id}")
        for field in ("visible_context", "hidden_reference_ids", "tags"):
            value = item.get(field, [])
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ValueError(f"case.{field} must be a string array")
        cutoff = item.get("information_cutoff")
        if cutoff is not None and not isinstance(cutoff, str):
            raise ValueError("case.information_cutoff must be a string or null")
        cases[case_id] = TaskCase(
            case_id=case_id,
            prompt=_require_string(item.get("prompt"), label="case.prompt"),
            visible_context=tuple(item.get("visible_context", [])),
            hidden_reference_ids=tuple(item.get("hidden_reference_ids", [])),
            tags=tuple(item.get("tags", [])),
            information_cutoff=cutoff,
        )

    references: dict[str, ReferenceArtifact] = {}
    reference_source_digests: dict[str, str] = {}
    for record in _read_jsonl(
        section_paths["references"], label="reference manifest"
    ):
        item = _strict(record, {"artifact_id", "kind", "path"}, label="reference")
        artifact_id = _require_string(item.get("artifact_id"), label="artifact_id")
        if artifact_id in references:
            raise ValueError(f"duplicate reference artifact_id: {artifact_id}")
        content_path = _source_path(root, item.get("path"), label="reference.path")
        source_paths.add(content_path)
        content_bytes = content_path.read_bytes()
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("reference.path must contain UTF-8") from error
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        reference_source_digests[artifact_id] = hashlib.sha256(
            content_bytes
        ).hexdigest()
        references[artifact_id] = ReferenceArtifact(
            artifact_id=artifact_id,
            kind=_require_string(item.get("kind"), label="reference.kind"),
            content=content,
        )
    missing = {
        reference_id
        for case in cases.values()
        for reference_id in case.hidden_reference_ids
        if reference_id not in references
    }
    if missing:
        raise ValueError(f"missing hidden references: {sorted(missing)}")

    rubric_raw = json.loads(section_paths["rubric"].read_text(encoding="utf-8"))
    rubric_data = _strict(
        rubric_raw, {"rubric_version", "dimensions"}, label="rubric"
    )
    dimensions_raw = rubric_data.get("dimensions")
    if not isinstance(dimensions_raw, list) or not dimensions_raw:
        raise ValueError("rubric dimensions are required")
    dimensions: list[RubricDimension] = []
    dimension_names: set[str] = set()
    for raw_dimension in dimensions_raw:
        dimension = _strict(
            raw_dimension, {"name", "description", "weight"}, label="dimension"
        )
        name = _require_string(dimension.get("name"), label="dimension.name")
        if name in dimension_names:
            raise ValueError(f"duplicate rubric dimension: {name}")
        dimension_names.add(name)
        weight = dimension.get("weight")
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError("dimension.weight must be finite and non-negative")
        dimensions.append(
            RubricDimension(
                name=name,
                description=_require_string(
                    dimension.get("description"), label="dimension.description"
                ),
                weight=float(weight),
            )
        )
    if not any(dimension.weight > 0 for dimension in dimensions):
        raise ValueError("at least one rubric weight must be positive")

    actor = _command_config(root, data.get("actor"), label="actor", identity_key="model_id")
    evaluator = _command_config(
        root,
        data.get("evaluator"),
        label="evaluator",
        identity_key="evaluator_id",
    )
    raw_observers = data.get("observer_evaluators", [])
    if not isinstance(raw_observers, list):
        raise ValueError("observer_evaluators must be an array")
    observers: list[ObserverEvaluatorConfig] = []
    observer_names: set[str] = set()
    evaluator_identities = {evaluator.identity}
    for raw_observer in raw_observers:
        observer_data = _strict(
            raw_observer,
            {
                "name",
                "adapter",
                "command",
                "evaluator_id",
                "timeout_seconds",
                "pass_env",
            },
            label="observer evaluator",
        )
        name = _require_string(observer_data.get("name"), label="observer name")
        if not _OBSERVER_NAME.fullmatch(name):
            raise ValueError("observer name must be a safe path component")
        if name in observer_names:
            raise ValueError(f"duplicate observer name: {name}")
        observer_names.add(name)
        config = _command_config(
            root,
            {key: value for key, value in observer_data.items() if key != "name"},
            label=f"observer evaluator {name}",
            identity_key="evaluator_id",
        )
        if config.identity in evaluator_identities:
            raise ValueError(f"duplicate evaluator identity: {config.identity}")
        evaluator_identities.add(config.identity)
        observers.append(ObserverEvaluatorConfig(name, config))
    data_home_raw = _require_string(data.get("data_home"), label="data_home")
    data_home_path = Path(data_home_raw)
    data_home = (
        data_home_path.expanduser().resolve()
        if data_home_path.is_absolute()
        else (root / data_home_path).resolve()
    )
    if _inside(data_home, checkout_root):
        raise ValueError("data_home must live outside the code checkout")
    if any(
        data_home == source or source in data_home.parents or data_home in source.parents
        for source in source_paths
    ):
        raise ValueError("data_home overlaps a source file")

    actor = replace(
        actor, working_directory=data_home / "provider-work" / "actor"
    )
    evaluator = replace(
        evaluator, working_directory=data_home / "provider-work" / "evaluator"
    )
    observers = [
        ObserverEvaluatorConfig(
            observer.name,
            replace(
                observer.config,
                working_directory=(
                    data_home / "provider-work" / "evaluators" / observer.name
                ),
            ),
        )
        for observer in sorted(observers, key=lambda item: item.name)
    ]

    digests = MappingProxyType(
        {str(source.relative_to(root)): _digest_file(source) for source in source_paths}
    )
    return ExternalProfile(
        path=path,
        profile_id=profile_id,
        data_home=data_home,
        policy=PolicySnapshot.from_artifacts(tuple(policy_artifacts)),
        cases=MappingProxyType(cases),
        references=MappingProxyType(references),
        reference_source_digests=MappingProxyType(reference_source_digests),
        rubric=Rubric(
            rubric_version=_require_string(
                rubric_data.get("rubric_version"), label="rubric_version"
            ),
            dimensions=tuple(dimensions),
        ),
        actor=actor,
        evaluator=evaluator,
        observer_evaluators=tuple(observers),
        source_digests=digests,
    )
