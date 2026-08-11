from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kpo.digest import canonical_digest
from kpo.diagnosis import propose_patch
from kpo.models import (
    Diagnosis,
    Evaluation,
    EvaluatorResult,
    PolicyArtifact,
    PolicySnapshot,
    ProposalDraft,
    ProposedCandidate,
    ReferenceArtifact,
    RejectedCandidateSummary,
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
    random_seed: int | None = None


@dataclass(frozen=True, slots=True)
class ProposerRequest:
    diagnosis: Diagnosis
    parent_policy: PolicySnapshot
    rollout: Rollout
    evaluation: Evaluation
    references: tuple[ReferenceArtifact, ...]
    rejected_candidates: tuple[RejectedCandidateSummary, ...] = ()
    allowed_mutations: tuple[str, ...] = ("add", "edit")


class ActorProvider(Protocol):
    def generate(self, request: ActorRequest) -> str: ...


class EvaluatorProvider(Protocol):
    def evaluate(self, request: EvaluatorRequest) -> EvaluatorResult: ...


class ProposerProvider(Protocol):
    proposer_id: str

    def propose(self, request: ProposerRequest) -> ProposalDraft: ...


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
    result = provider.evaluate(request)
    return Evaluation.create(
        rollout_digest=rollout.digest,
        evaluator_id=evaluator_id,
        rubric_version=rubric_version,
        dimensions=result.dimensions,
        failure_signals=result.failure_signals,
        aggregate_score=result.aggregate_score,
    )


def run_proposer(
    diagnosis: Diagnosis,
    parent: PolicySnapshot,
    rollout: Rollout,
    evaluation: Evaluation,
    references: tuple[ReferenceArtifact, ...],
    provider: ProposerProvider,
    *,
    rejected_candidates: tuple[RejectedCandidateSummary, ...] = (),
) -> ProposedCandidate:
    if not diagnosis.patchable:
        raise ValueError(f"diagnosis {diagnosis.kind.value} does not permit proposer")
    if diagnosis.evaluation_digest != evaluation.digest:
        raise ValueError("diagnosis does not match evaluation")
    if evaluation.rollout_digest != rollout.digest:
        raise ValueError("evaluation does not match rollout")
    if rollout.policy_digest != parent.digest:
        raise ValueError("rollout does not match parent policy")
    expected_reference_ids = set(diagnosis.citations) - {
        rollout.digest,
        evaluation.digest,
        diagnosis.digest,
    }
    provided_reference_ids = {reference.artifact_id for reference in references}
    if not references or provided_reference_ids != expected_reference_ids:
        raise ValueError("proposer references must be cited by the diagnosis")
    draft = provider.propose(
        ProposerRequest(
            diagnosis,
            parent,
            rollout,
            evaluation,
            references,
            rejected_candidates,
        )
    )
    candidate = propose_patch(
        diagnosis,
        parent,
        mutation=draft.mutation,
        artifact_id=draft.artifact_id,
        content=draft.content,
    )
    fields = {
        "candidate": candidate,
        "expected_benefit": draft.expected_benefit,
        "possible_regressions": draft.possible_regressions,
        "proposer_id": provider.proposer_id,
    }
    return ProposedCandidate(**fields, digest=canonical_digest(fields))
