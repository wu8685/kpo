from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from statistics import fmean

from kpo.models import (
    CandidatePatch,
    MutationKind,
    Partition,
    PolicyArtifact,
    PolicySnapshot,
    RegressionCase,
    RegressionMeasurement,
    RegressionReport,
)


ScoreFunction = Callable[[RegressionCase, PolicySnapshot], float]


def apply_candidate(
    parent: PolicySnapshot, candidate: CandidatePatch
) -> PolicySnapshot:
    if candidate.parent_policy_digest != parent.digest:
        raise ValueError("candidate parent policy digest does not match snapshot")

    artifacts = list(parent.artifacts)
    by_id = {artifact.artifact_id: index for index, artifact in enumerate(artifacts)}
    if candidate.mutation is MutationKind.ADD:
        if candidate.artifact_id in by_id:
            raise ValueError("add mutation requires a new artifact ID")
        artifacts.append(PolicyArtifact(candidate.artifact_id, candidate.content))
    elif candidate.mutation is MutationKind.EDIT:
        if candidate.artifact_id not in by_id:
            raise ValueError("edit mutation requires an existing artifact ID")
        index = by_id[candidate.artifact_id]
        current = artifacts[index]
        artifacts[index] = PolicyArtifact(
            current.artifact_id,
            candidate.content,
            version=current.version + 1,
        )
    else:
        raise NotImplementedError(
            f"mutation {candidate.mutation.value} is not implemented in v0.1"
        )
    return PolicySnapshot.from_artifacts(tuple(artifacts))


def run_regression(
    parent: PolicySnapshot,
    candidate: CandidatePatch,
    cases: tuple[RegressionCase, ...],
    scorer: ScoreFunction,
    *,
    min_validation_gain: float = 0.0,
    min_holdout_gain: float = 0.0,
    max_regression_drop: float = 0.0,
) -> RegressionReport:
    if not cases:
        raise ValueError("regression cases are required")
    partitions = {case.partition for case in cases}
    required = {Partition.VALIDATION, Partition.HOLDOUT}
    if not required.issubset(partitions):
        raise ValueError("validation and holdout partitions are required")

    candidate_policy = apply_candidate(parent, candidate)
    measurements: list[RegressionMeasurement] = []
    gains: dict[Partition, list[float]] = defaultdict(list)
    ablation_deltas: list[float] = []

    for case in cases:
        parent_score = scorer(case, parent)
        candidate_score = scorer(case, candidate_policy)
        ablated_score = scorer(case, parent)
        measurement = RegressionMeasurement(
            case_id=case.case_id,
            partition=case.partition,
            parent_score=parent_score,
            candidate_score=candidate_score,
            ablated_score=ablated_score,
        )
        measurements.append(measurement)
        gains[case.partition].append(candidate_score - parent_score)
        ablation_deltas.append(candidate_score - ablated_score)

    partition_gains = {
        partition: fmean(partition_values)
        for partition, partition_values in gains.items()
    }
    ablation_gain = fmean(ablation_deltas)
    validation_passed = (
        partition_gains[Partition.VALIDATION] > min_validation_gain
    )
    holdout_passed = partition_gains[Partition.HOLDOUT] >= min_holdout_gain
    regression_passed = (
        Partition.REGRESSION not in partition_gains
        or partition_gains[Partition.REGRESSION] >= -max_regression_drop
    )
    passed = (
        validation_passed
        and holdout_passed
        and regression_passed
        and ablation_gain > 0.0
    )
    return RegressionReport.create(
        candidate_digest=candidate.digest,
        parent_policy_digest=parent.digest,
        candidate_policy_digest=candidate_policy.digest,
        measurements=tuple(measurements),
        partition_gains=partition_gains,
        ablation_gain=ablation_gain,
        passed=passed,
    )
