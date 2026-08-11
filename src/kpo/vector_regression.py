from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from kpo.dataset import DatasetManifest
from kpo.digest import canonical_digest
from kpo.models import CandidatePatch, Partition, PolicySnapshot
from kpo.regression import apply_candidate


VectorScoreFunction = Callable[[str, PolicySnapshot], Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class VectorGates:
    validation: Mapping[str, float]
    holdout: Mapping[str, float]
    regression: Mapping[str, float]
    ablation: Mapping[str, float]

    def __post_init__(self) -> None:
        groups = (
            set(self.validation),
            set(self.holdout),
            set(self.regression),
            set(self.ablation),
        )
        if not groups[0] or any(group != groups[0] for group in groups[1:]):
            raise ValueError("gate dimension sets must be non-empty and identical")

    @property
    def dimensions(self) -> frozenset[str]:
        return frozenset(self.validation)


@dataclass(frozen=True, slots=True)
class VectorMeasurement:
    case_id: str
    partition: Partition
    weight: float
    parent_scores: Mapping[str, float]
    candidate_scores: Mapping[str, float]
    ablated_scores: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class VectorRegressionReport:
    candidate_digest: str
    parent_policy_digest: str
    candidate_policy_digest: str
    measurements: tuple[VectorMeasurement, ...]
    partition_gains: Mapping[Partition, Mapping[str, float]]
    ablation_gains: Mapping[str, float]
    passed: bool
    digest: str


def _scores(
    raw: Mapping[str, float], dimensions: frozenset[str]
) -> Mapping[str, float]:
    if set(raw) != set(dimensions):
        raise ValueError("score dimension set does not match gate dimensions")
    values: dict[str, float] = {}
    for name, score in raw.items():
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
            raise ValueError(f"score for dimension {name} must be between 0 and 1")
        values[name] = float(score)
    return MappingProxyType(values)


def _weighted(values: list[tuple[float, float]]) -> float:
    denominator = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / denominator


def run_vector_regression(
    parent: PolicySnapshot,
    candidate: CandidatePatch,
    dataset: DatasetManifest,
    scorer: VectorScoreFunction,
    gates: VectorGates,
) -> VectorRegressionReport:
    candidate_policy = apply_candidate(parent, candidate)
    measurements: list[VectorMeasurement] = []
    partition_values: dict[Partition, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    ablation_values: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for entry in dataset.entries:
        if entry.partition is Partition.TRAIN:
            continue
        parent_scores = _scores(scorer(entry.case_id, parent), gates.dimensions)
        candidate_scores = _scores(
            scorer(entry.case_id, candidate_policy), gates.dimensions
        )
        ablated_scores = _scores(scorer(entry.case_id, parent), gates.dimensions)
        measurements.append(
            VectorMeasurement(
                entry.case_id,
                entry.partition,
                entry.weight,
                parent_scores,
                candidate_scores,
                ablated_scores,
            )
        )
        for dimension in gates.dimensions:
            partition_values[entry.partition][dimension].append(
                (candidate_scores[dimension] - parent_scores[dimension], entry.weight)
            )
            ablation_values[dimension].append(
                (candidate_scores[dimension] - ablated_scores[dimension], entry.weight)
            )

    partition_gains = {
        partition: MappingProxyType(
            {
                dimension: _weighted(values)
                for dimension, values in dimensions.items()
            }
        )
        for partition, dimensions in partition_values.items()
    }
    ablation_gains = {
        dimension: _weighted(values) for dimension, values in ablation_values.items()
    }
    thresholds = {
        Partition.VALIDATION: gates.validation,
        Partition.HOLDOUT: gates.holdout,
        Partition.REGRESSION: gates.regression,
    }
    passed = all(
        partition_gains[partition][dimension] >= threshold
        for partition, dimension_thresholds in thresholds.items()
        for dimension, threshold in dimension_thresholds.items()
    ) and all(
        ablation_gains[dimension] >= threshold
        for dimension, threshold in gates.ablation.items()
    )
    immutable_partition_gains = MappingProxyType(partition_gains)
    immutable_ablation = MappingProxyType(ablation_gains)
    fields = {
        "candidate_digest": candidate.digest,
        "parent_policy_digest": parent.digest,
        "candidate_policy_digest": candidate_policy.digest,
        "measurements": tuple(measurements),
        "partition_gains": immutable_partition_gains,
        "ablation_gains": immutable_ablation,
        "passed": passed,
    }
    return VectorRegressionReport(**fields, digest=canonical_digest(fields))
