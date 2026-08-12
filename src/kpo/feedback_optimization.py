from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kpo.digest import canonical_digest
from kpo.models import DiagnosisKind


class OptimizationTarget(str, Enum):
    KNOWLEDGE_POLICY = "knowledge_policy"
    RETRIEVAL = "retrieval"
    ACTOR = "actor"
    EVALUATOR = "evaluator"
    RUBRIC = "rubric"
    PROPOSER = "proposer"
    DATASET = "dataset"
    ORCHESTRATION = "orchestration"
    RUNTIME = "runtime"


class FeedbackSymptom(str, Enum):
    MISSING_KNOWLEDGE = "missing_knowledge"
    WRONG_OR_OVERBROAD_KNOWLEDGE = "wrong_or_overbroad_knowledge"
    RETRIEVAL_MISS = "retrieval_miss"
    APPLICATION_FAILURE = "application_failure"
    UNSUPPORTED_REASONING = "unsupported_reasoning"
    REFERENCE_PROBLEM = "reference_problem"
    EVALUATOR_DISAGREEMENT = "evaluator_disagreement"
    EVALUATOR_CALIBRATION_DRIFT = "evaluator_calibration_drift"
    RUBRIC_DEFECT = "rubric_defect"
    PROPOSAL_FAILURE = "proposal_failure"
    COVERAGE_GAP = "coverage_gap"
    ROLE_OR_SEQUENCE_FAILURE = "role_or_sequence_failure"
    RUNTIME_FAILURE = "runtime_failure"


class FeedbackDisposition(str, Enum):
    POLICY_CANDIDATE = "policy_candidate"
    COMPONENT_CANDIDATE = "component_candidate"
    COLLECT_EVIDENCE = "collect_evidence"
    REFERENCE_REVIEW = "reference_review"
    RUNTIME_REPAIR = "runtime_repair"


class RetrievalReplayOutcome(str, Enum):
    NOT_RUN = "not_run"
    ARTIFACT_ABSENT = "artifact_absent"
    ARTIFACT_PRESENT_NOT_RETRIEVED = "artifact_present_not_retrieved"


_DIAGNOSIS_SYMPTOMS = {
    DiagnosisKind.MISSING_POLICY_KNOWLEDGE: FeedbackSymptom.MISSING_KNOWLEDGE,
    DiagnosisKind.RETRIEVAL_FAILURE: FeedbackSymptom.RETRIEVAL_MISS,
    DiagnosisKind.POLICY_APPLICATION_FAILURE: FeedbackSymptom.APPLICATION_FAILURE,
    DiagnosisKind.INVALID_OR_MISAPPLIED_RULE: (
        FeedbackSymptom.WRONG_OR_OVERBROAD_KNOWLEDGE
    ),
    DiagnosisKind.MISSING_APPLICABILITY_BOUNDARY: (
        FeedbackSymptom.WRONG_OR_OVERBROAD_KNOWLEDGE
    ),
    DiagnosisKind.INSUFFICIENT_TASK_EVIDENCE: FeedbackSymptom.COVERAGE_GAP,
    DiagnosisKind.REFERENCE_INCONSISTENCY: FeedbackSymptom.REFERENCE_PROBLEM,
    DiagnosisKind.EVALUATOR_UNCERTAINTY: FeedbackSymptom.EVALUATOR_DISAGREEMENT,
    DiagnosisKind.RUNTIME_OR_PROVIDER_FAILURE: FeedbackSymptom.RUNTIME_FAILURE,
}


def feedback_symptom_for_diagnosis(kind: DiagnosisKind) -> FeedbackSymptom:
    return _DIAGNOSIS_SYMPTOMS[kind]


@dataclass(frozen=True, slots=True)
class AttributionHypothesis:
    target: OptimizationTarget
    confidence: float
    supporting_evidence: tuple[str, ...]
    disconfirming_or_missing_evidence: tuple[str, ...]
    experiment_class: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("attribution confidence must be between 0 and 1")
        if not self.supporting_evidence:
            raise ValueError("supporting evidence is required")
        if not self.disconfirming_or_missing_evidence:
            raise ValueError("disconfirming or missing evidence is required")
        if not self.experiment_class.strip():
            raise ValueError("experiment class is required")
        object.__setattr__(self, "supporting_evidence", tuple(sorted(self.supporting_evidence)))
        object.__setattr__(
            self,
            "disconfirming_or_missing_evidence",
            tuple(sorted(self.disconfirming_or_missing_evidence)),
        )


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    protocol: str
    feedback_id: str
    campaign_id: str
    iteration_id: str
    case_id: str
    partition: str
    rollout_digest: str
    evaluation_digest: str
    policy_digest: str
    evidence_digests: tuple[str, ...]
    symptom: FeedbackSymptom
    hypotheses: tuple[AttributionHypothesis, ...]
    additional_evidence_required: bool
    digest: str

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        iteration_id: str,
        case_id: str,
        partition: str,
        rollout_digest: str,
        evaluation_digest: str,
        policy_digest: str,
        evidence_digests: tuple[str, ...],
        symptom: FeedbackSymptom,
        hypotheses: tuple[AttributionHypothesis, ...],
        additional_evidence_required: bool,
    ) -> "FeedbackRecord":
        required = {
            "campaign_id": campaign_id,
            "iteration_id": iteration_id,
            "case_id": case_id,
            "rollout_digest": rollout_digest,
            "evaluation_digest": evaluation_digest,
            "policy_digest": policy_digest,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"feedback fields are required: {', '.join(missing)}")
        if partition != "train":
            raise ValueError("mutation-driving feedback must come from train")
        if not evidence_digests or any(not value.strip() for value in evidence_digests):
            raise ValueError("feedback evidence digests are required")
        if len(set(evidence_digests)) != len(evidence_digests):
            raise ValueError("feedback evidence digests must be unique")
        if not hypotheses:
            raise ValueError("at least one attribution hypothesis is required")

        ordered_hypotheses = tuple(
            sorted(hypotheses, key=lambda item: (-item.confidence, item.target.value))
        )
        ordered_evidence = tuple(sorted(evidence_digests))
        base_fields = {
            "protocol": "kpo.feedback/v1",
            **required,
            "partition": partition,
            "evidence_digests": ordered_evidence,
            "symptom": symptom,
            "hypotheses": ordered_hypotheses,
            "additional_evidence_required": additional_evidence_required,
        }
        feedback_id = f"feedback-{canonical_digest(base_fields)[:16]}"
        fields = {**base_fields, "feedback_id": feedback_id}
        return cls(**fields, digest=canonical_digest(fields))


@dataclass(frozen=True, slots=True)
class FeedbackRouting:
    protocol: str
    feedback_digest: str
    ordered_hypotheses: tuple[AttributionHypothesis, ...]
    disposition: FeedbackDisposition
    target: OptimizationTarget | None
    rule: str
    evidence_digests: tuple[str, ...]
    digest: str


def _routing(
    feedback: FeedbackRecord,
    disposition: FeedbackDisposition,
    target: OptimizationTarget | None,
    rule: str,
) -> FeedbackRouting:
    fields = {
        "protocol": "kpo.feedback-routing/v1",
        "feedback_digest": feedback.digest,
        "ordered_hypotheses": feedback.hypotheses,
        "disposition": disposition,
        "target": target,
        "rule": rule,
        "evidence_digests": feedback.evidence_digests,
    }
    return FeedbackRouting(**fields, digest=canonical_digest(fields))


def route_feedback(
    feedback: FeedbackRecord,
    *,
    ambiguity_margin: float,
    retrieval_replay_outcome: RetrievalReplayOutcome = RetrievalReplayOutcome.NOT_RUN,
) -> FeedbackRouting:
    if not 0.0 <= ambiguity_margin <= 1.0:
        raise ValueError("ambiguity margin must be between 0 and 1")
    if feedback.partition != "train":
        raise ValueError("routing feedback must come from train")

    if feedback.symptom is FeedbackSymptom.RUNTIME_FAILURE:
        return _routing(
            feedback,
            FeedbackDisposition.RUNTIME_REPAIR,
            OptimizationTarget.RUNTIME,
            "runtime failures cannot mutate policy",
        )
    if feedback.symptom is FeedbackSymptom.REFERENCE_PROBLEM:
        return _routing(
            feedback,
            FeedbackDisposition.REFERENCE_REVIEW,
            None,
            "reference problems require independent review",
        )
    if feedback.symptom in {
        FeedbackSymptom.EVALUATOR_DISAGREEMENT,
        FeedbackSymptom.EVALUATOR_CALIBRATION_DRIFT,
    }:
        return _routing(
            feedback,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
            "disputed evaluator evidence cannot authorize mutation",
        )

    hypotheses = feedback.hypotheses
    if len(hypotheses) > 1:
        gap = hypotheses[0].confidence - hypotheses[1].confidence
        if gap < ambiguity_margin:
            return _routing(
                feedback,
                FeedbackDisposition.COLLECT_EVIDENCE,
                None,
                "top attribution hypotheses are inside ambiguity margin",
            )

    target = hypotheses[0].target
    if feedback.additional_evidence_required:
        return _routing(
            feedback,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
            "feedback explicitly requires additional evidence",
        )

    if feedback.symptom is FeedbackSymptom.RETRIEVAL_MISS:
        if retrieval_replay_outcome is RetrievalReplayOutcome.NOT_RUN:
            return _routing(
                feedback,
                FeedbackDisposition.COLLECT_EVIDENCE,
                None,
                "retrieval replay is required before mutation",
            )
        if retrieval_replay_outcome is RetrievalReplayOutcome.ARTIFACT_ABSENT:
            return _routing(
                feedback,
                FeedbackDisposition.POLICY_CANDIDATE,
                OptimizationTarget.KNOWLEDGE_POLICY,
                "replay proved required policy knowledge absent",
            )
        return _routing(
            feedback,
            FeedbackDisposition.COMPONENT_CANDIDATE,
            OptimizationTarget.RETRIEVAL,
            "replay proved policy existed but retrieval missed it",
        )

    if feedback.symptom in {
        FeedbackSymptom.MISSING_KNOWLEDGE,
        FeedbackSymptom.WRONG_OR_OVERBROAD_KNOWLEDGE,
    }:
        disposition = (
            FeedbackDisposition.POLICY_CANDIDATE
            if target is OptimizationTarget.KNOWLEDGE_POLICY
            else FeedbackDisposition.COMPONENT_CANDIDATE
        )
        return _routing(feedback, disposition, target, "top supported attribution")

    required_targets = {
        FeedbackSymptom.RUBRIC_DEFECT: OptimizationTarget.RUBRIC,
        FeedbackSymptom.PROPOSAL_FAILURE: OptimizationTarget.PROPOSER,
        FeedbackSymptom.COVERAGE_GAP: OptimizationTarget.DATASET,
        FeedbackSymptom.ROLE_OR_SEQUENCE_FAILURE: OptimizationTarget.ORCHESTRATION,
    }
    required_target = required_targets.get(feedback.symptom)
    if required_target is not None:
        if target is not required_target:
            return _routing(
                feedback,
                FeedbackDisposition.COLLECT_EVIDENCE,
                None,
                "symptom and attributed target disagree",
            )
        return _routing(
            feedback,
            FeedbackDisposition.COMPONENT_CANDIDATE,
            target,
            "symptom has a registered component target",
        )

    if feedback.symptom is FeedbackSymptom.APPLICATION_FAILURE and target not in {
        OptimizationTarget.ACTOR,
        OptimizationTarget.RETRIEVAL,
    }:
        return _routing(
            feedback,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
            "application failure requires actor or retrieval experiment",
        )
    if target is OptimizationTarget.KNOWLEDGE_POLICY:
        return _routing(
            feedback,
            FeedbackDisposition.COLLECT_EVIDENCE,
            None,
            "only confirmed knowledge symptoms can mutate policy",
        )
    return _routing(
        feedback,
        FeedbackDisposition.COMPONENT_CANDIDATE,
        target,
        "top supported component attribution",
    )


@dataclass(frozen=True, slots=True)
class ComponentCandidate:
    protocol: str
    target: OptimizationTarget
    component_instance_id: str
    parent_version: str
    parent_digest: str
    candidate_version: str
    configuration_digest: str
    feedback_digests: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    mutation_hypothesis: str
    expected_benefit: str
    possible_regressions: tuple[str, ...]
    replay_plan_digest: str
    rollback_descriptor: str
    generator_identity: str
    digest: str

    @classmethod
    def create(cls, **values: object) -> "ComponentCandidate":
        if not isinstance(values.get("target"), OptimizationTarget):
            raise ValueError("component candidate target is invalid")
        strings = (
            "component_instance_id",
            "parent_version",
            "parent_digest",
            "candidate_version",
            "configuration_digest",
            "mutation_hypothesis",
            "expected_benefit",
            "replay_plan_digest",
            "rollback_descriptor",
            "generator_identity",
        )
        for field in strings:
            value = values.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"component candidate {field} is required")
        for field in ("feedback_digests", "evidence_digests", "possible_regressions"):
            value = values.get(field)
            if (
                not isinstance(value, tuple)
                or not value
                or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                raise ValueError(f"component candidate {field} is required")
        if values["parent_version"] == values["candidate_version"]:
            raise ValueError("component candidate version must differ from parent")
        fields = {"protocol": "kpo.component-candidate/v1", **values}
        return cls(**fields, digest=canonical_digest(fields))  # type: ignore[arg-type]

    def validate_approver(self, approver_identity: str) -> None:
        if not approver_identity.strip():
            raise ValueError("approver identity is required")
        if approver_identity == self.generator_identity:
            raise ValueError("component candidate cannot approve itself")
