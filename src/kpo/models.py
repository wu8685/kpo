from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from kpo.digest import canonical_digest


@dataclass(frozen=True, slots=True)
class TaskCase:
    case_id: str
    prompt: str
    visible_context: tuple[str, ...] = ()
    hidden_reference_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    information_cutoff: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id is required")
        if not self.prompt.strip():
            raise ValueError("prompt is required")
        leaked = set(self.visible_context) & set(self.hidden_reference_ids)
        if leaked:
            raise ValueError(
                "hidden reference leaked into visible context: "
                + ", ".join(sorted(leaked))
            )

    def actor_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "visible_context": list(self.visible_context),
            "tags": list(self.tags),
        }
        if self.information_cutoff is not None:
            payload["information_cutoff"] = self.information_cutoff
        return payload


@dataclass(frozen=True, slots=True)
class PolicyArtifact:
    artifact_id: str
    content: str
    version: int = 1

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("artifact_id is required")
        if not self.content.strip():
            raise ValueError("artifact content is required")
        if self.version < 1:
            raise ValueError("artifact version must be positive")


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    artifact_ids: tuple[str, ...]
    digest: str
    artifacts: tuple[PolicyArtifact, ...]

    @classmethod
    def from_artifacts(
        cls, artifacts: tuple[PolicyArtifact, ...]
    ) -> "PolicySnapshot":
        ordered = tuple(sorted(artifacts, key=lambda artifact: artifact.artifact_id))
        artifact_ids = tuple(artifact.artifact_id for artifact in ordered)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("policy artifact IDs must be unique")
        digest = canonical_digest(
            [
                {
                    "artifact_id": artifact.artifact_id,
                    "content": artifact.content,
                    "version": artifact.version,
                }
                for artifact in ordered
            ]
        )
        return cls(artifact_ids=artifact_ids, digest=digest, artifacts=ordered)


@dataclass(frozen=True, slots=True)
class ReferenceArtifact:
    artifact_id: str
    kind: str
    content: str

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("reference artifact_id is required")
        if not self.kind.strip():
            raise ValueError("reference kind is required")
        if not self.content.strip():
            raise ValueError("reference content is required")


@dataclass(frozen=True, slots=True)
class Rollout:
    case_id: str
    policy_digest: str
    model_id: str
    output: str
    retrieved_policy_ids: tuple[str, ...]
    actor_payload_digest: str
    digest: str

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        policy_digest: str,
        model_id: str,
        output: str,
        retrieved_policy_ids: tuple[str, ...],
        actor_payload_digest: str,
    ) -> "Rollout":
        fields = {
            "case_id": case_id,
            "policy_digest": policy_digest,
            "model_id": model_id,
            "output": output,
            "retrieved_policy_ids": retrieved_policy_ids,
            "actor_payload_digest": actor_payload_digest,
        }
        return cls(**fields, digest=canonical_digest(fields))


@dataclass(frozen=True, slots=True)
class ScoreDimension:
    name: str
    score: float
    explanation: str
    citations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("score dimension name is required")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        if not self.explanation.strip():
            raise ValueError("score explanation is required")
        if not self.citations:
            raise ValueError("score citations are required")


class DiagnosisKind(str, Enum):
    MISSING_POLICY_KNOWLEDGE = "missing_policy_knowledge"
    RETRIEVAL_FAILURE = "retrieval_failure"
    POLICY_APPLICATION_FAILURE = "policy_application_failure"
    INVALID_OR_MISAPPLIED_RULE = "invalid_or_misapplied_rule"
    MISSING_APPLICABILITY_BOUNDARY = "missing_applicability_boundary"
    INSUFFICIENT_TASK_EVIDENCE = "insufficient_task_evidence"
    REFERENCE_INCONSISTENCY = "reference_inconsistency"
    EVALUATOR_UNCERTAINTY = "evaluator_uncertainty"
    RUNTIME_OR_PROVIDER_FAILURE = "runtime_or_provider_failure"


@dataclass(frozen=True, slots=True)
class FailureSignal:
    kind: DiagnosisKind
    message: str
    citations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("failure signal message is required")
        if not self.citations:
            raise ValueError("failure signal citations are required")


@dataclass(frozen=True, slots=True)
class EvaluatorResult:
    dimensions: tuple[ScoreDimension, ...]
    failure_signals: tuple[FailureSignal, ...] = ()
    aggregate_score: float | None = None

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("evaluator result dimensions are required")
        if self.aggregate_score is not None and not 0.0 <= self.aggregate_score <= 1.0:
            raise ValueError("aggregate score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Evaluation:
    rollout_digest: str
    evaluator_id: str
    rubric_version: str
    dimensions: tuple[ScoreDimension, ...]
    failure_signals: tuple[FailureSignal, ...]
    aggregate_score: float | None
    digest: str

    @classmethod
    def create(
        cls,
        *,
        rollout_digest: str,
        evaluator_id: str,
        rubric_version: str,
        dimensions: tuple[ScoreDimension, ...],
        failure_signals: tuple[FailureSignal, ...] = (),
        aggregate_score: float | None = None,
    ) -> "Evaluation":
        if not dimensions:
            raise ValueError("evaluation dimensions are required")
        if aggregate_score is not None and not 0.0 <= aggregate_score <= 1.0:
            raise ValueError("aggregate score must be between 0 and 1")
        fields = {
            "rollout_digest": rollout_digest,
            "evaluator_id": evaluator_id,
            "rubric_version": rubric_version,
            "dimensions": dimensions,
            "failure_signals": failure_signals,
            "aggregate_score": aggregate_score,
        }
        return cls(**fields, digest=canonical_digest(fields))


@dataclass(frozen=True, slots=True)
class Diagnosis:
    evaluation_digest: str
    kind: DiagnosisKind
    message: str
    citations: tuple[str, ...]
    patchable: bool
    digest: str


class MutationKind(str, Enum):
    ADD = "add"
    EDIT = "edit"
    SPLIT = "split"
    MERGE = "merge"
    REPRIORITIZE = "reprioritize"
    DOWNVOTE = "downvote"
    DEPRECATE = "deprecate"
    ADD_APPLICABILITY_BOUNDARY = "add_applicability_boundary"


@dataclass(frozen=True, slots=True)
class CandidatePatch:
    parent_policy_digest: str
    diagnosis_digest: str
    mutation: MutationKind
    artifact_id: str
    content: str
    provenance: tuple[str, ...]
    digest: str


class Partition(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"
    REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class RegressionCase:
    case_id: str
    partition: Partition

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("regression case_id is required")


@dataclass(frozen=True, slots=True)
class RegressionMeasurement:
    case_id: str
    partition: Partition
    parent_score: float
    candidate_score: float
    ablated_score: float

    def __post_init__(self) -> None:
        for name, score in (
            ("parent", self.parent_score),
            ("candidate", self.candidate_score),
            ("ablated", self.ablated_score),
        ):
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"{name} score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RegressionReport:
    candidate_digest: str
    parent_policy_digest: str
    candidate_policy_digest: str
    measurements: tuple[RegressionMeasurement, ...]
    partition_gains: Mapping[Partition, float]
    ablation_gain: float
    passed: bool
    digest: str

    @classmethod
    def create(
        cls,
        *,
        candidate_digest: str,
        parent_policy_digest: str,
        candidate_policy_digest: str,
        measurements: tuple[RegressionMeasurement, ...],
        partition_gains: Mapping[Partition, float],
        ablation_gain: float,
        passed: bool,
    ) -> "RegressionReport":
        immutable_gains = MappingProxyType(dict(partition_gains))
        fields = {
            "candidate_digest": candidate_digest,
            "parent_policy_digest": parent_policy_digest,
            "candidate_policy_digest": candidate_policy_digest,
            "measurements": measurements,
            "partition_gains": immutable_gains,
            "ablation_gain": ablation_gain,
            "passed": passed,
        }
        return cls(**fields, digest=canonical_digest(fields))
