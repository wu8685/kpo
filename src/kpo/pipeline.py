from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kpo.digest import canonical_digest
from kpo.models import (
    Evaluation,
    PolicyArtifact,
    PolicySnapshot,
    ReferenceArtifact,
    Rollout,
    ScoreDimension,
    TaskCase,
)


@dataclass(frozen=True, slots=True)
class ActorRequest:
    case_payload: dict[str, Any]
    policy_artifacts: tuple[PolicyArtifact, ...]


@dataclass(frozen=True, slots=True)
class EvaluatorRequest:
    case_id: str
    rollout: Rollout
    references: tuple[ReferenceArtifact, ...]
    rubric_version: str


class ActorProvider(Protocol):
    def generate(self, request: ActorRequest) -> str: ...


class EvaluatorProvider(Protocol):
    def evaluate(self, request: EvaluatorRequest) -> tuple[ScoreDimension, ...]: ...


def run_actor(
    case: TaskCase,
    policy: PolicySnapshot,
    provider: ActorProvider,
    *,
    model_id: str,
) -> Rollout:
    payload = case.actor_payload()
    request = ActorRequest(case_payload=payload, policy_artifacts=policy.artifacts)
    output = provider.generate(request)
    if not output.strip():
        raise ValueError("actor output is required")
    return Rollout.create(
        case_id=case.case_id,
        policy_digest=policy.digest,
        model_id=model_id,
        output=output,
        retrieved_policy_ids=policy.artifact_ids,
        actor_payload_digest=canonical_digest(payload),
    )


def evaluate_rollout(
    case: TaskCase,
    rollout: Rollout,
    references: tuple[ReferenceArtifact, ...],
    provider: EvaluatorProvider,
    *,
    evaluator_id: str,
    rubric_version: str,
) -> Evaluation:
    expected = set(case.hidden_reference_ids)
    provided = {reference.artifact_id for reference in references}
    if expected != provided:
        raise ValueError(
            "hidden reference set does not match case contract: "
            f"expected={sorted(expected)}, provided={sorted(provided)}"
        )
    request = EvaluatorRequest(
        case_id=case.case_id,
        rollout=rollout,
        references=references,
        rubric_version=rubric_version,
    )
    dimensions = provider.evaluate(request)
    return Evaluation.create(
        rollout_digest=rollout.digest,
        evaluator_id=evaluator_id,
        rubric_version=rubric_version,
        dimensions=dimensions,
    )
