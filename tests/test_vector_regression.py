from __future__ import annotations

from pathlib import Path

import pytest

from kpo.dataset import load_dataset
from kpo.models import (
    CandidatePatch,
    MutationKind,
    Partition,
    PolicyArtifact,
    PolicySnapshot,
)
from kpo.vector_regression import VectorGates, run_vector_regression
from test_dataset import _cases, _manifest


def _candidate(parent: PolicySnapshot) -> CandidatePatch:
    return CandidatePatch(
        parent.digest,
        "diagnosis",
        MutationKind.ADD,
        "new-rule",
        "Improved rule.",
        ("evaluation", "reference"),
        "candidate",
    )


def test_vector_gates_are_independent_and_train_is_excluded(tmp_path: Path) -> None:
    dataset = load_dataset(_manifest(tmp_path / "dataset.jsonl"), _cases())
    parent = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Base."),))
    candidate = _candidate(parent)
    gates = VectorGates(
        validation={"alignment": 0.4, "validity": 0.0},
        holdout={"alignment": 0.4, "validity": 0.0},
        regression={"alignment": 0.0, "validity": 0.0},
        ablation={"alignment": 0.4, "validity": 0.0},
    )

    def scorer(case_id: str, policy: PolicySnapshot) -> dict[str, float]:
        if case_id == "case-0":
            raise AssertionError("train case must not be measured")
        improved = "new-rule" in policy.artifact_ids
        return {
            "alignment": 0.9 if improved else 0.4,
            "validity": 0.5,
        }

    report = run_vector_regression(parent, candidate, dataset, scorer, gates)

    assert report.passed is True
    assert report.partition_gains[Partition.HOLDOUT]["alignment"] == pytest.approx(0.5)
    assert report.partition_gains[Partition.HOLDOUT]["validity"] == 0.0
    assert report.ablation_gains["alignment"] == pytest.approx(0.5)


def test_one_failing_dimension_rejects_whole_candidate(tmp_path: Path) -> None:
    dataset = load_dataset(_manifest(tmp_path / "dataset.jsonl"), _cases())
    parent = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Base."),))
    gates = VectorGates(
        validation={"alignment": 0.1, "validity": 0.1},
        holdout={"alignment": 0.1, "validity": 0.1},
        regression={"alignment": 0.0, "validity": 0.0},
        ablation={"alignment": 0.1, "validity": 0.1},
    )

    def scorer(case_id: str, policy: PolicySnapshot) -> dict[str, float]:
        improved = "new-rule" in policy.artifact_ids
        return {
            "alignment": 0.8 if improved else 0.5,
            "validity": 0.4 if improved else 0.5,
        }

    report = run_vector_regression(parent, _candidate(parent), dataset, scorer, gates)
    assert report.passed is False
    assert report.partition_gains[Partition.VALIDATION]["validity"] < 0


def test_gate_dimension_set_must_match_score_vector(tmp_path: Path) -> None:
    dataset = load_dataset(_manifest(tmp_path / "dataset.jsonl"), _cases())
    parent = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Base."),))
    gates = VectorGates(
        validation={"alignment": 0.0},
        holdout={"alignment": 0.0},
        regression={"alignment": 0.0},
        ablation={"alignment": 0.0},
    )

    with pytest.raises(ValueError, match="dimension"):
        run_vector_regression(
            parent,
            _candidate(parent),
            dataset,
            lambda case_id, policy: {"alignment": 0.5, "extra": 0.5},
            gates,
        )


@pytest.mark.parametrize("case_count", [4, 16, 64, 256])
def test_vector_regression_scales_across_synthetic_dataset_sizes(
    tmp_path: Path, case_count: int
) -> None:
    dataset = load_dataset(
        _manifest(tmp_path / f"dataset-{case_count}.jsonl", case_count),
        _cases(case_count),
    )
    parent = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Base."),))
    gates = VectorGates(
        validation={"alignment": 0.4},
        holdout={"alignment": 0.4},
        regression={"alignment": 0.4},
        ablation={"alignment": 0.4},
    )

    report = run_vector_regression(
        parent,
        _candidate(parent),
        dataset,
        lambda case_id, policy: {
            "alignment": 1.0 if "new-rule" in policy.artifact_ids else 0.5
        },
        gates,
    )

    assert report.passed is True
    assert len(report.measurements) == case_count - case_count // 4
