from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.dataset import DatasetGrowthInterrupted, DatasetManager, load_dataset
from kpo.models import Partition, TaskCase


def _cases(count: int = 4) -> dict[str, TaskCase]:
    partitions = ("train", "validation", "holdout", "regression")
    return {
        f"case-{index}": TaskCase(
            case_id=f"case-{index}",
            prompt=f"Analyze distinct scenario {index}.",
            visible_context=(f"Distinct visible fact {index}.",),
            hidden_reference_ids=(f"ref-{index}",),
            tags=(partitions[index % 4],),
        )
        for index in range(count)
    }


def _manifest(path: Path, count: int = 4) -> Path:
    partitions = ("train", "validation", "holdout", "regression")
    rows = [
        {
            "case_id": f"case-{index}",
            "partition": partitions[index % 4],
            "weight": 1.0,
            "provenance": [f"source-{index}"],
        }
        for index in range(count)
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def test_dataset_loads_strict_partitions_and_stable_holdout_digest(
    tmp_path: Path,
) -> None:
    cases = _cases()
    first = load_dataset(_manifest(tmp_path / "dataset.jsonl"), cases)
    second = load_dataset(tmp_path / "dataset.jsonl", cases)

    assert first.digest == second.digest
    assert first.holdout_digest == second.holdout_digest
    assert first.by_partition[Partition.HOLDOUT][0].case_id == "case-2"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda rows: rows.append(dict(rows[0])), "duplicate case_id"),
        (lambda rows: rows[0].update(weight=0), "weight"),
        (lambda rows: rows[0].update(provenance=[]), "provenance"),
        (lambda rows: rows.pop(1), "validation"),
        (lambda rows: rows.pop(2), "holdout"),
    ],
)
def test_dataset_rejects_invalid_contract(
    tmp_path: Path, change: object, message: str
) -> None:
    path = _manifest(tmp_path / "dataset.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    change(rows)  # type: ignore[operator]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_dataset(path, _cases())


def test_dataset_rejects_cross_partition_prompt_or_context_contamination(
    tmp_path: Path,
) -> None:
    cases = _cases()
    cases["case-1"] = TaskCase(
        "case-1",
        cases["case-0"].prompt.lower(),
        visible_context=("another fact",),
        hidden_reference_ids=("ref-1",),
    )
    with pytest.raises(ValueError, match="contamination"):
        load_dataset(_manifest(tmp_path / "dataset.jsonl"), cases)

    cases = _cases()
    cases["case-1"] = TaskCase(
        "case-1",
        "another prompt",
        visible_context=(cases["case-0"].visible_context[0].upper(),),
        hidden_reference_ids=("ref-1",),
    )
    with pytest.raises(ValueError, match="contamination"):
        load_dataset(tmp_path / "dataset.jsonl", cases)


def test_holdout_initialization_is_preview_approved_and_immutable(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path / "dataset.jsonl")
    dataset = load_dataset(path, _cases())
    manager = DatasetManager(tmp_path / "runtime")

    preview = manager.preview_initialize(dataset)
    assert not manager.lock_path.exists()
    with pytest.raises(ValueError, match="approval"):
        manager.apply_initialize(preview, approval_digest="wrong")

    result = manager.apply_initialize(preview, approval_digest=preview.approval_digest)
    assert result.state == "initialized"
    manager.verify_holdout(dataset)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[2]["weight"] = 2.0
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    changed = load_dataset(path, _cases())
    with pytest.raises(ValueError, match="frozen holdout"):
        manager.verify_holdout(changed)
    with pytest.raises(ValueError, match="already initialized"):
        manager.preview_initialize(changed)


def test_dataset_growth_is_preview_approved_and_recoverable(tmp_path: Path) -> None:
    cases = _cases(8)
    current_path = _manifest(tmp_path / "dataset.jsonl", 4)
    current = load_dataset(current_path, cases)
    manager = DatasetManager(tmp_path / "runtime")
    init = manager.preview_initialize(current)
    manager.apply_initialize(init, approval_digest=init.approval_digest)

    proposed_path = _manifest(tmp_path / "proposed.jsonl", 8)
    proposed_rows = [
        json.loads(line)
        for line in proposed_path.read_text(encoding="utf-8").splitlines()
    ]
    proposed_rows[6]["partition"] = "regression"
    proposed_path.write_text(
        "".join(json.dumps(row) + "\n" for row in proposed_rows),
        encoding="utf-8",
    )
    proposed = load_dataset(proposed_path, cases)
    original = current_path.read_bytes()
    preview = manager.preview_growth(
        current,
        proposed,
        manifest_path=current_path,
        proposed_manifest_path=proposed_path,
    )
    assert current_path.read_bytes() == original
    assert preview.added_case_ids == ("case-4", "case-5", "case-6", "case-7")

    with pytest.raises(DatasetGrowthInterrupted):
        manager.apply_growth(
            preview,
            approval_digest=preview.approval_digest,
            fault_after_replace=True,
        )
    assert current_path.read_bytes() == proposed_path.read_bytes()
    recovery = manager.recover_growth(preview.approval_digest)
    assert recovery.state == "rolled_back"
    assert current_path.read_bytes() == original


def test_dataset_growth_cannot_change_or_add_holdout(tmp_path: Path) -> None:
    cases = _cases(8)
    current_path = _manifest(tmp_path / "dataset.jsonl", 4)
    current = load_dataset(current_path, cases)
    manager = DatasetManager(tmp_path / "runtime")
    init = manager.preview_initialize(current)
    manager.apply_initialize(init, approval_digest=init.approval_digest)

    proposed_path = _manifest(tmp_path / "proposed.jsonl", 8)
    proposed = load_dataset(proposed_path, cases)
    with pytest.raises(ValueError, match="holdout"):
        manager.preview_growth(
            current,
            proposed,
            manifest_path=current_path,
            proposed_manifest_path=proposed_path,
        )
