from __future__ import annotations

from dataclasses import replace

import pytest

from kpo.feedback_optimization import (
    AttributionHypothesis,
    ComponentCandidate,
    FeedbackDisposition,
    FeedbackRecord,
    FeedbackSymptom,
    OptimizationTarget,
    RetrievalReplayOutcome,
    feedback_symptom_for_diagnosis,
    route_feedback,
)
from kpo.models import DiagnosisKind


def _hypothesis(
    target: OptimizationTarget,
    confidence: float,
    *,
    evidence: tuple[str, ...] = ("evidence-1",),
) -> AttributionHypothesis:
    return AttributionHypothesis(
        target=target,
        confidence=confidence,
        supporting_evidence=evidence,
        disconfirming_or_missing_evidence=("missing-counterexample",),
        experiment_class="matched-replay",
    )


def _feedback(
    symptom: FeedbackSymptom,
    *hypotheses: AttributionHypothesis,
) -> FeedbackRecord:
    return FeedbackRecord.create(
        campaign_id="campaign-1",
        iteration_id="iteration-1",
        case_id="case-train-1",
        partition="train",
        rollout_digest="rollout-1",
        evaluation_digest="evaluation-1",
        policy_digest="policy-1",
        evidence_digests=("evidence-1",),
        symptom=symptom,
        hypotheses=tuple(hypotheses),
        additional_evidence_required=False,
    )


def test_routable_feedback_is_train_only_and_time_free() -> None:
    first = _feedback(
        FeedbackSymptom.MISSING_KNOWLEDGE,
        _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),
    )
    second = _feedback(
        FeedbackSymptom.MISSING_KNOWLEDGE,
        _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),
    )

    assert first.digest == second.digest
    assert not hasattr(first, "created_at")

    with pytest.raises(ValueError, match="train"):
        FeedbackRecord.create(
            campaign_id="campaign-1",
            iteration_id="iteration-1",
            case_id="case-holdout-1",
            partition="holdout",
            rollout_digest="rollout-1",
            evaluation_digest="evaluation-1",
            policy_digest="policy-1",
            evidence_digests=("evidence-1",),
            symptom=FeedbackSymptom.MISSING_KNOWLEDGE,
            hypotheses=(_hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),),
            additional_evidence_required=False,
        )


@pytest.mark.parametrize(
    ("symptom", "disposition", "target"),
    [
        (
            FeedbackSymptom.RUNTIME_FAILURE,
            FeedbackDisposition.RUNTIME_REPAIR,
            OptimizationTarget.RUNTIME,
        ),
        (
            FeedbackSymptom.REFERENCE_PROBLEM,
            FeedbackDisposition.REFERENCE_REVIEW,
            None,
        ),
        (
            FeedbackSymptom.EVALUATOR_DISAGREEMENT,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
        ),
        (
            FeedbackSymptom.EVALUATOR_CALIBRATION_DRIFT,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
        ),
    ],
)
def test_safety_symptoms_cannot_mutate_policy(
    symptom: FeedbackSymptom,
    disposition: FeedbackDisposition,
    target: OptimizationTarget | None,
) -> None:
    feedback = _feedback(
        symptom,
        _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.99),
    )

    routing = route_feedback(feedback, ambiguity_margin=0.15)

    assert routing.disposition is disposition
    assert routing.target is target


def test_missing_knowledge_routes_to_policy_candidate() -> None:
    routing = route_feedback(
        _feedback(
            FeedbackSymptom.MISSING_KNOWLEDGE,
            _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),
            _hypothesis(OptimizationTarget.ACTOR, 0.4),
        ),
        ambiguity_margin=0.15,
    )

    assert routing.disposition is FeedbackDisposition.POLICY_CANDIDATE
    assert routing.target is OptimizationTarget.KNOWLEDGE_POLICY


def test_ambiguous_attribution_collects_evidence() -> None:
    routing = route_feedback(
        _feedback(
            FeedbackSymptom.UNSUPPORTED_REASONING,
            _hypothesis(OptimizationTarget.ACTOR, 0.75),
            _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.65),
        ),
        ambiguity_margin=0.15,
    )

    assert routing.disposition is FeedbackDisposition.COLLECT_EVIDENCE
    assert routing.target is None


def test_retrieval_miss_requires_replay_before_any_policy_candidate() -> None:
    feedback = _feedback(
        FeedbackSymptom.RETRIEVAL_MISS,
        _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),
    )

    before_replay = route_feedback(feedback, ambiguity_margin=0.15)
    after_replay = route_feedback(
        feedback,
        ambiguity_margin=0.15,
        retrieval_replay_outcome=RetrievalReplayOutcome.ARTIFACT_ABSENT,
    )

    assert before_replay.disposition is FeedbackDisposition.COLLECT_EVIDENCE
    assert after_replay.disposition is FeedbackDisposition.POLICY_CANDIDATE


def test_retrieval_replay_proves_present_routes_retrieval_component() -> None:
    feedback = _feedback(
        FeedbackSymptom.RETRIEVAL_MISS,
        _hypothesis(OptimizationTarget.RETRIEVAL, 0.9),
    )

    routing = route_feedback(
        feedback,
        ambiguity_margin=0.15,
        retrieval_replay_outcome=(
            RetrievalReplayOutcome.ARTIFACT_PRESENT_NOT_RETRIEVED
        ),
    )

    assert routing.disposition is FeedbackDisposition.COMPONENT_CANDIDATE
    assert routing.target is OptimizationTarget.RETRIEVAL


def test_rubric_defect_routes_to_single_target_component_candidate() -> None:
    feedback = _feedback(
        FeedbackSymptom.RUBRIC_DEFECT,
        _hypothesis(OptimizationTarget.RUBRIC, 0.92),
    )
    routing = route_feedback(feedback, ambiguity_margin=0.15)

    assert routing.disposition is FeedbackDisposition.COMPONENT_CANDIDATE
    assert routing.target is OptimizationTarget.RUBRIC

    candidate = ComponentCandidate.create(
        target=routing.target,
        component_instance_id="rubric-main",
        parent_version="v1",
        parent_digest="parent-1",
        candidate_version="v2",
        configuration_digest="configuration-2",
        feedback_digests=(feedback.digest,),
        evidence_digests=("evidence-1",),
        mutation_hypothesis="split conflated dimension",
        expected_benefit="better discrimination",
        possible_regressions=("lower repeatability",),
        replay_plan_digest="replay-1",
        rollback_descriptor="restore:v1",
        generator_identity="proposer-1",
    )

    assert candidate.target is OptimizationTarget.RUBRIC


def test_component_candidate_rejects_self_approval() -> None:
    candidate = ComponentCandidate.create(
        target=OptimizationTarget.ACTOR,
        component_instance_id="actor-main",
        parent_version="v1",
        parent_digest="parent-1",
        candidate_version="v2",
        configuration_digest="configuration-2",
        feedback_digests=("feedback-1",),
        evidence_digests=("evidence-1",),
        mutation_hypothesis="make state labels mandatory",
        expected_benefit="fewer application failures",
        possible_regressions=("longer output",),
        replay_plan_digest="replay-1",
        rollback_descriptor="restore:v1",
        generator_identity="proposer-1",
    )

    with pytest.raises(ValueError, match="approve itself"):
        candidate.validate_approver("proposer-1")

    candidate.validate_approver("independent-reviewer-1")


def test_router_defensively_rejects_non_train_record() -> None:
    feedback = replace(
        _feedback(
            FeedbackSymptom.MISSING_KNOWLEDGE,
            _hypothesis(OptimizationTarget.KNOWLEDGE_POLICY, 0.9),
        ),
        partition="holdout",
    )

    with pytest.raises(ValueError, match="train"):
        route_feedback(feedback, ambiguity_margin=0.15)


def test_feedback_digest_sorts_evidence_semantically() -> None:
    first = FeedbackRecord.create(
        campaign_id="campaign-1",
        iteration_id="iteration-1",
        case_id="case-train-1",
        partition="train",
        rollout_digest="rollout-1",
        evaluation_digest="evaluation-1",
        policy_digest="policy-1",
        evidence_digests=("evidence-b", "evidence-a"),
        symptom=FeedbackSymptom.MISSING_KNOWLEDGE,
        hypotheses=(
            _hypothesis(
                OptimizationTarget.KNOWLEDGE_POLICY,
                0.9,
                evidence=("evidence-a",),
            ),
        ),
        additional_evidence_required=False,
    )
    second = FeedbackRecord.create(
        campaign_id="campaign-1",
        iteration_id="iteration-1",
        case_id="case-train-1",
        partition="train",
        rollout_digest="rollout-1",
        evaluation_digest="evaluation-1",
        policy_digest="policy-1",
        evidence_digests=("evidence-a", "evidence-b"),
        symptom=FeedbackSymptom.MISSING_KNOWLEDGE,
        hypotheses=(
            _hypothesis(
                OptimizationTarget.KNOWLEDGE_POLICY,
                0.9,
                evidence=("evidence-a",),
            ),
        ),
        additional_evidence_required=False,
    )

    assert first.digest == second.digest


def test_component_candidate_rejects_invalid_target_and_digest_elements() -> None:
    values = {
        "target": OptimizationTarget.ACTOR,
        "component_instance_id": "actor-main",
        "parent_version": "v1",
        "parent_digest": "parent-1",
        "candidate_version": "v2",
        "configuration_digest": "configuration-2",
        "feedback_digests": ("feedback-1",),
        "evidence_digests": ("evidence-1",),
        "mutation_hypothesis": "make labels mandatory",
        "expected_benefit": "fewer failures",
        "possible_regressions": ("longer output",),
        "replay_plan_digest": "replay-1",
        "rollback_descriptor": "restore:v1",
        "generator_identity": "proposer-1",
    }

    with pytest.raises(ValueError, match="target"):
        ComponentCandidate.create(**{**values, "target": "actor"})
    with pytest.raises(ValueError, match="evidence_digests"):
        ComponentCandidate.create(**{**values, "evidence_digests": ("",)})


@pytest.mark.parametrize(
    ("kind", "symptom"),
    [
        (DiagnosisKind.MISSING_POLICY_KNOWLEDGE, FeedbackSymptom.MISSING_KNOWLEDGE),
        (DiagnosisKind.RETRIEVAL_FAILURE, FeedbackSymptom.RETRIEVAL_MISS),
        (DiagnosisKind.POLICY_APPLICATION_FAILURE, FeedbackSymptom.APPLICATION_FAILURE),
        (
            DiagnosisKind.INVALID_OR_MISAPPLIED_RULE,
            FeedbackSymptom.WRONG_OR_OVERBROAD_KNOWLEDGE,
        ),
        (
            DiagnosisKind.MISSING_APPLICABILITY_BOUNDARY,
            FeedbackSymptom.WRONG_OR_OVERBROAD_KNOWLEDGE,
        ),
        (DiagnosisKind.INSUFFICIENT_TASK_EVIDENCE, FeedbackSymptom.COVERAGE_GAP),
        (DiagnosisKind.REFERENCE_INCONSISTENCY, FeedbackSymptom.REFERENCE_PROBLEM),
        (
            DiagnosisKind.EVALUATOR_UNCERTAINTY,
            FeedbackSymptom.EVALUATOR_DISAGREEMENT,
        ),
        (DiagnosisKind.RUNTIME_OR_PROVIDER_FAILURE, FeedbackSymptom.RUNTIME_FAILURE),
    ],
)
def test_legacy_diagnosis_mapping_is_complete(
    kind: DiagnosisKind, symptom: FeedbackSymptom
) -> None:
    assert feedback_symptom_for_diagnosis(kind) is symptom
