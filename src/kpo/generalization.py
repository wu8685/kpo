from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from kpo.dataset import DatasetManifest
from kpo.digest import canonical_digest
from kpo.models import TaskCase


@dataclass(frozen=True, slots=True)
class GeneralizationEntry:
    case_id: str
    weight: float
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneralizationManifest:
    path: Path
    entries: tuple[GeneralizationEntry, ...]
    digest: str
    source_digest: str


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _strict_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("generalization entry must be an object")
    allowed = {"case_id", "weight", "provenance"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"unknown generalization entry fields: {sorted(unknown)}"
        )
    if set(value) != allowed:
        raise ValueError("generalization entry fields are required")
    return value


def _case_contract(case: TaskCase, entry: GeneralizationEntry) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "prompt": case.prompt,
        "visible_context": case.visible_context,
        "hidden_reference_ids": case.hidden_reference_ids,
        "tags": case.tags,
        "information_cutoff": case.information_cutoff,
        "weight": entry.weight,
        "provenance": entry.provenance,
    }


def _content_digest(case: TaskCase) -> str:
    return canonical_digest(
        {
            "prompt": _normalized(case.prompt),
            "visible_context": tuple(
                _normalized(item) for item in case.visible_context
            ),
        }
    )


def load_generalization_manifest(
    path: Path,
    cases: Mapping[str, TaskCase],
    dataset: DatasetManifest,
) -> GeneralizationManifest:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid generalization JSON on line {number}"
            ) from error
        records.append(_strict_entry(raw))
    if not records:
        raise ValueError("generalization manifest must not be empty")

    campaign_ids = {_normalized(entry.case_id) for entry in dataset.entries}
    seen_ids: set[str] = set()
    entries: list[GeneralizationEntry] = []
    for record in records:
        case_id = record["case_id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("generalization case_id is required")
        normalized_id = _normalized(case_id)
        if normalized_id in seen_ids:
            raise ValueError(f"duplicate generalization case_id: {case_id}")
        seen_ids.add(normalized_id)
        if normalized_id in campaign_ids:
            raise ValueError("generalization case overlaps campaign dataset")
        if case_id not in cases:
            raise ValueError(f"unknown generalization case_id: {case_id}")
        weight = record["weight"]
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight <= 0
        ):
            raise ValueError("generalization weight must be finite and positive")
        provenance = record["provenance"]
        if (
            not isinstance(provenance, list)
            or not provenance
            or any(
                not isinstance(item, str) or not item.strip()
                for item in provenance
            )
        ):
            raise ValueError(
                "generalization provenance must be a non-empty string array"
            )
        entries.append(
            GeneralizationEntry(case_id, float(weight), tuple(provenance))
        )

    campaign_cases = [cases[entry.case_id] for entry in dataset.entries]
    campaign_prompts = {_normalized(case.prompt) for case in campaign_cases}
    campaign_context = {
        _normalized(item)
        for case in campaign_cases
        for item in case.visible_context
    }
    campaign_content = {_content_digest(case) for case in campaign_cases}
    seen_prompts: set[str] = set()
    seen_context: set[str] = set()
    seen_content: set[str] = set()
    for entry in entries:
        case = cases[entry.case_id]
        prompt = _normalized(case.prompt)
        context = {_normalized(item) for item in case.visible_context}
        content = _content_digest(case)
        if (
            prompt in campaign_prompts
            or bool(context & campaign_context)
            or content in campaign_content
        ):
            raise ValueError("generalization contamination detected")
        if (
            prompt in seen_prompts
            or bool(context & seen_context)
            or content in seen_content
        ):
            raise ValueError("duplicate generalization case content detected")
        seen_prompts.add(prompt)
        seen_context.update(context)
        seen_content.add(content)

    immutable_entries = tuple(entries)
    return GeneralizationManifest(
        path=path,
        entries=immutable_entries,
        digest=canonical_digest(
            [_case_contract(cases[entry.case_id], entry) for entry in entries]
        ),
        source_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
