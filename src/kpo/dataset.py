from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from kpo.digest import canonical_digest
from kpo.models import Partition, TaskCase


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    case_id: str
    partition: Partition
    weight: float
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    entries: tuple[DatasetEntry, ...]
    by_partition: Mapping[Partition, tuple[DatasetEntry, ...]]
    digest: str
    holdout_digest: str


@dataclass(frozen=True, slots=True)
class HoldoutInitPreview:
    dataset_digest: str
    holdout_digest: str
    approval_digest: str


@dataclass(frozen=True, slots=True)
class HoldoutInitResult:
    state: str
    holdout_digest: str
    journal_path: Path


class DatasetGrowthInterrupted(RuntimeError):
    """Raised by fault injection after a dataset manifest replacement."""


@dataclass(frozen=True, slots=True)
class DatasetGrowthPreview:
    current_dataset_digest: str
    proposed_dataset_digest: str
    holdout_digest: str
    manifest_path: Path
    original_digest: str
    new_digest: str
    new_content: bytes
    added_case_ids: tuple[str, ...]
    approval_digest: str


@dataclass(frozen=True, slots=True)
class DatasetGrowthResult:
    state: str
    approval_digest: str
    journal_path: Path


def _strict(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    return value


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _case_contract(case: TaskCase, entry: DatasetEntry) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "prompt": case.prompt,
        "visible_context": case.visible_context,
        "hidden_reference_ids": case.hidden_reference_ids,
        "tags": case.tags,
        "information_cutoff": case.information_cutoff,
        "partition": entry.partition,
        "weight": entry.weight,
        "provenance": entry.provenance,
    }


def load_dataset(path: Path, cases: Mapping[str, TaskCase]) -> DatasetManifest:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid dataset JSON on line {number}") from error
        records.append(_strict(raw, {"case_id", "partition", "weight", "provenance"}, "dataset entry"))
    if not records:
        raise ValueError("dataset must not be empty")

    entries: list[DatasetEntry] = []
    seen_ids: set[str] = set()
    for record in records:
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("dataset case_id is required")
        normalized_id = _normalized(case_id)
        if normalized_id in seen_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_ids.add(normalized_id)
        if case_id not in cases:
            raise ValueError(f"unknown dataset case_id: {case_id}")
        try:
            partition = Partition(record.get("partition"))
        except ValueError as error:
            raise ValueError("invalid dataset partition") from error
        weight = record.get("weight")
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight <= 0
        ):
            raise ValueError("dataset weight must be finite and positive")
        provenance = record.get("provenance")
        if (
            not isinstance(provenance, list)
            or not provenance
            or any(not isinstance(item, str) or not item.strip() for item in provenance)
        ):
            raise ValueError("dataset provenance must be a non-empty string array")
        entries.append(
            DatasetEntry(case_id, partition, float(weight), tuple(provenance))
        )

    grouped: dict[Partition, list[DatasetEntry]] = {
        partition: [] for partition in Partition
    }
    for entry in entries:
        grouped[entry.partition].append(entry)
    for partition in Partition:
        if not grouped[partition]:
            raise ValueError(f"dataset {partition.value} partition is required")

    prompt_owners: dict[str, Partition] = {}
    context_owners: dict[str, Partition] = {}
    content_digests: set[str] = set()
    for entry in entries:
        case = cases[entry.case_id]
        content_digest = canonical_digest(
            {
                "prompt": _normalized(case.prompt),
                "visible_context": tuple(_normalized(item) for item in case.visible_context),
            }
        )
        if content_digest in content_digests:
            raise ValueError("exact duplicate case content detected")
        content_digests.add(content_digest)
        prompt = _normalized(case.prompt)
        owner = prompt_owners.setdefault(prompt, entry.partition)
        if owner is not entry.partition:
            raise ValueError("cross-partition prompt contamination detected")
        for context in case.visible_context:
            normalized = _normalized(context)
            owner = context_owners.setdefault(normalized, entry.partition)
            if owner is not entry.partition:
                raise ValueError("cross-partition context contamination detected")

    ordered = tuple(sorted(entries, key=lambda entry: entry.case_id))
    holdout_contract = [
        _case_contract(cases[entry.case_id], entry)
        for entry in ordered
        if entry.partition is Partition.HOLDOUT
    ]
    return DatasetManifest(
        entries=ordered,
        by_partition=MappingProxyType(
            {
                partition: tuple(sorted(values, key=lambda item: item.case_id))
                for partition, values in grouped.items()
            }
        ),
        digest=canonical_digest(
            [_case_contract(cases[entry.case_id], entry) for entry in ordered]
        ),
        holdout_digest=canonical_digest(holdout_contract),
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def _bytes_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class DatasetManager:
    def __init__(self, data_home: Path) -> None:
        self.root = data_home.expanduser().resolve() / "dataset"
        self.lock_path = self.root / "holdout-lock.json"
        self.journal_dir = self.root / "journals"

    def preview_initialize(self, dataset: DatasetManifest) -> HoldoutInitPreview:
        if self.lock_path.exists():
            raise ValueError("holdout is already initialized")
        fields = {
            "operation": "initialize_holdout",
            "dataset_digest": dataset.digest,
            "holdout_digest": dataset.holdout_digest,
        }
        return HoldoutInitPreview(
            dataset.digest,
            dataset.holdout_digest,
            canonical_digest(fields),
        )

    def apply_initialize(
        self, preview: HoldoutInitPreview, *, approval_digest: str
    ) -> HoldoutInitResult:
        if approval_digest != preview.approval_digest:
            raise ValueError("approval digest does not match holdout preview")
        if self.lock_path.exists():
            raise ValueError("holdout is already initialized")
        journal = self.journal_dir / f"{preview.approval_digest}.json"
        value = {
            "state": "initialized",
            "dataset_digest": preview.dataset_digest,
            "holdout_digest": preview.holdout_digest,
            "approval_digest": preview.approval_digest,
        }
        _atomic_json(self.lock_path, value)
        _atomic_json(journal, value)
        return HoldoutInitResult("initialized", preview.holdout_digest, journal)

    def verify_holdout(self, dataset: DatasetManifest) -> None:
        if not self.lock_path.exists():
            raise ValueError("frozen holdout lock is not initialized")
        lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        if lock.get("holdout_digest") != dataset.holdout_digest:
            raise ValueError("frozen holdout digest does not match dataset")

    def preview_growth(
        self,
        current: DatasetManifest,
        proposed: DatasetManifest,
        *,
        manifest_path: Path,
        proposed_manifest_path: Path,
    ) -> DatasetGrowthPreview:
        self.verify_holdout(current)
        self.verify_holdout(proposed)
        current_by_id = {entry.case_id: entry for entry in current.entries}
        proposed_by_id = {entry.case_id: entry for entry in proposed.entries}
        removed = set(current_by_id) - set(proposed_by_id)
        if removed:
            raise ValueError("dataset growth cannot remove existing cases")
        changed = [
            case_id
            for case_id, entry in current_by_id.items()
            if proposed_by_id[case_id] != entry
        ]
        if changed:
            raise ValueError("dataset growth cannot edit existing cases")
        added = tuple(sorted(set(proposed_by_id) - set(current_by_id)))
        if not added:
            raise ValueError("dataset growth has no additions")
        if any(proposed_by_id[case_id].partition is Partition.HOLDOUT for case_id in added):
            raise ValueError("dataset growth cannot add holdout cases")

        target = manifest_path.expanduser().resolve(strict=True)
        proposed_path = proposed_manifest_path.expanduser().resolve(strict=True)
        original = target.read_bytes()
        updated = proposed_path.read_bytes()
        fields = {
            "operation": "grow_dataset",
            "current_dataset_digest": current.digest,
            "proposed_dataset_digest": proposed.digest,
            "holdout_digest": current.holdout_digest,
            "manifest_path": str(target),
            "original_digest": _bytes_digest(original),
            "new_digest": _bytes_digest(updated),
            "added_case_ids": added,
        }
        return DatasetGrowthPreview(
            current_dataset_digest=current.digest,
            proposed_dataset_digest=proposed.digest,
            holdout_digest=current.holdout_digest,
            manifest_path=target,
            original_digest=fields["original_digest"],
            new_digest=fields["new_digest"],
            new_content=updated,
            added_case_ids=added,
            approval_digest=canonical_digest(fields),
        )

    def apply_growth(
        self,
        preview: DatasetGrowthPreview,
        *,
        approval_digest: str,
        fault_after_replace: bool = False,
    ) -> DatasetGrowthResult:
        if approval_digest != preview.approval_digest:
            raise ValueError("approval digest does not match dataset growth preview")
        current = preview.manifest_path.read_bytes()
        if _bytes_digest(current) != preview.original_digest:
            raise ValueError("stale dataset growth preview")
        if _bytes_digest(preview.new_content) != preview.new_digest:
            raise ValueError("dataset growth content digest mismatch")
        snapshot_dir = self.root / "snapshots" / preview.approval_digest
        snapshot_path = snapshot_dir / "original.bin"
        reviewed_path = snapshot_dir / "reviewed.jsonl"
        _atomic_bytes(snapshot_path, current)
        _atomic_bytes(reviewed_path, preview.new_content)
        journal = self.journal_dir / f"growth-{preview.approval_digest}.json"
        value = {
            "state": "prepared",
            "approval_digest": preview.approval_digest,
            "manifest_path": str(preview.manifest_path),
            "snapshot_path": str(snapshot_path),
            "original_digest": preview.original_digest,
            "new_digest": preview.new_digest,
        }
        _atomic_json(journal, value)
        _atomic_bytes(preview.manifest_path, preview.new_content)
        if fault_after_replace:
            raise DatasetGrowthInterrupted("fault injected after dataset replacement")
        value["state"] = "committed"
        _atomic_json(journal, value)
        return DatasetGrowthResult("committed", preview.approval_digest, journal)

    def recover_growth(self, approval_digest: str) -> DatasetGrowthResult:
        journal = self.journal_dir / f"growth-{approval_digest}.json"
        if not journal.exists():
            raise KeyError(f"unknown dataset growth journal: {approval_digest}")
        value = json.loads(journal.read_text(encoding="utf-8"))
        if value.get("state") != "prepared":
            raise ValueError("dataset growth journal is not recoverable")
        manifest = Path(value["manifest_path"])
        snapshot = Path(value["snapshot_path"])
        _atomic_bytes(manifest, snapshot.read_bytes())
        value["state"] = "rolled_back"
        _atomic_json(journal, value)
        return DatasetGrowthResult("rolled_back", approval_digest, journal)
