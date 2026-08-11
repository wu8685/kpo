from __future__ import annotations

from kpo.digest import canonical_digest
from kpo.models import (
    CandidatePatch,
    Diagnosis,
    DiagnosisKind,
    Evaluation,
    MutationKind,
    PolicySnapshot,
)


_PATCHABLE_KINDS = frozenset(
    {
        DiagnosisKind.MISSING_POLICY_KNOWLEDGE,
        DiagnosisKind.INVALID_OR_MISAPPLIED_RULE,
        DiagnosisKind.MISSING_APPLICABILITY_BOUNDARY,
    }
)

_DIAGNOSIS_PRIORITY = {
    DiagnosisKind.RUNTIME_OR_PROVIDER_FAILURE: 0,
    DiagnosisKind.REFERENCE_INCONSISTENCY: 1,
    DiagnosisKind.EVALUATOR_UNCERTAINTY: 2,
    DiagnosisKind.INSUFFICIENT_TASK_EVIDENCE: 3,
    DiagnosisKind.RETRIEVAL_FAILURE: 4,
    DiagnosisKind.POLICY_APPLICATION_FAILURE: 5,
    DiagnosisKind.INVALID_OR_MISAPPLIED_RULE: 6,
    DiagnosisKind.MISSING_APPLICABILITY_BOUNDARY: 7,
    DiagnosisKind.MISSING_POLICY_KNOWLEDGE: 8,
}


def diagnose(evaluation: Evaluation) -> Diagnosis:
    if not evaluation.failure_signals:
        raise ValueError("evaluation has no failure signals to diagnose")
    signal = min(
        evaluation.failure_signals,
        key=lambda item: _DIAGNOSIS_PRIORITY[item.kind],
    )
    patchable = signal.kind in _PATCHABLE_KINDS
    fields = {
        "evaluation_digest": evaluation.digest,
        "kind": signal.kind,
        "message": signal.message,
        "citations": signal.citations,
        "patchable": patchable,
    }
    return Diagnosis(**fields, digest=canonical_digest(fields))


def propose_patch(
    diagnosis: Diagnosis,
    parent: PolicySnapshot,
    *,
    mutation: MutationKind,
    artifact_id: str,
    content: str,
) -> CandidatePatch:
    if not diagnosis.patchable:
        raise ValueError(
            f"diagnosis {diagnosis.kind.value} does not permit policy mutation"
        )
    if not artifact_id.strip():
        raise ValueError("candidate artifact_id is required")
    if not content.strip():
        raise ValueError("candidate content is required")
    if mutation is MutationKind.ADD and artifact_id in parent.artifact_ids:
        raise ValueError("add mutation requires a new artifact ID")
    if mutation is MutationKind.EDIT and artifact_id not in parent.artifact_ids:
        raise ValueError("edit mutation requires an existing artifact ID")
    provenance = (diagnosis.evaluation_digest, *diagnosis.citations)
    fields = {
        "parent_policy_digest": parent.digest,
        "diagnosis_digest": diagnosis.digest,
        "mutation": mutation,
        "artifact_id": artifact_id,
        "content": content,
        "provenance": provenance,
    }
    return CandidatePatch(**fields, digest=canonical_digest(fields))
