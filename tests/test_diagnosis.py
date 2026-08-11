import pytest

from kpo.diagnosis import diagnose, propose_patch
from kpo.models import (
    DiagnosisKind,
    Evaluation,
    FailureSignal,
    MutationKind,
    PolicyArtifact,
    PolicySnapshot,
    ScoreDimension,
)


def make_evaluation(signal: FailureSignal) -> Evaluation:
    return Evaluation.create(
        rollout_digest="rollout-1",
        evaluator_id="evaluator-1",
        rubric_version="v1",
        dimensions=(
            ScoreDimension(
                name="validity",
                score=0.2,
                explanation="The output missed a required rule.",
                citations=("rollout-1", "reference-1"),
            ),
        ),
        failure_signals=(signal,),
    )


def test_missing_policy_knowledge_can_produce_candidate_patch() -> None:
    evaluation = make_evaluation(
        FailureSignal(
            kind=DiagnosisKind.MISSING_POLICY_KNOWLEDGE,
            message="No rule covers conflicting evidence.",
            citations=("reference-1",),
        )
    )
    diagnosis = diagnose(evaluation)
    parent = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "Prefer verified evidence."),)
    )

    candidate = propose_patch(
        diagnosis,
        parent,
        mutation=MutationKind.ADD,
        artifact_id="rule-2",
        content="When evidence conflicts, state the conflict and abstain.",
    )

    assert diagnosis.patchable is True
    assert candidate.parent_policy_digest == parent.digest
    assert candidate.diagnosis_digest == diagnosis.digest
    assert candidate.mutation is MutationKind.ADD
    assert candidate.digest


def test_runtime_or_retrieval_failure_cannot_mutate_policy() -> None:
    evaluation = make_evaluation(
        FailureSignal(
            kind=DiagnosisKind.RETRIEVAL_FAILURE,
            message="The relevant rule existed but was not retrieved.",
            citations=("rule-1",),
        )
    )
    diagnosis = diagnose(evaluation)
    parent = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "Prefer verified evidence."),)
    )

    assert diagnosis.patchable is False
    with pytest.raises(ValueError, match="does not permit policy mutation"):
        propose_patch(
            diagnosis,
            parent,
            mutation=MutationKind.EDIT,
            artifact_id="rule-1",
            content="Changed content.",
        )


def test_diagnosis_preserves_evaluator_uncertainty() -> None:
    evaluation = make_evaluation(
        FailureSignal(
            kind=DiagnosisKind.EVALUATOR_UNCERTAINTY,
            message="References disagree.",
            citations=("reference-1", "reference-2"),
        )
    )

    diagnosis = diagnose(evaluation)

    assert diagnosis.kind is DiagnosisKind.EVALUATOR_UNCERTAINTY
    assert diagnosis.patchable is False
