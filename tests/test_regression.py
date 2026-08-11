import pytest

from kpo.models import (
    CandidatePatch,
    MutationKind,
    Partition,
    PolicyArtifact,
    PolicySnapshot,
    RegressionCase,
)
from kpo.regression import apply_candidate, run_regression


def parent_policy() -> PolicySnapshot:
    return PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "Use available evidence."),)
    )


def candidate_for(parent: PolicySnapshot, content: str) -> CandidatePatch:
    fields = {
        "parent_policy_digest": parent.digest,
        "diagnosis_digest": "diagnosis-1",
        "mutation": MutationKind.ADD,
        "artifact_id": "rule-2",
        "content": content,
        "provenance": ("evaluation-1", "reference-1"),
        "digest": "candidate-1",
    }
    return CandidatePatch(**fields)


def cases() -> tuple[RegressionCase, ...]:
    return (
        RegressionCase("train-1", Partition.TRAIN),
        RegressionCase("validation-1", Partition.VALIDATION),
        RegressionCase("holdout-1", Partition.HOLDOUT),
        RegressionCase("regression-1", Partition.REGRESSION),
    )


def test_candidate_is_evaluated_against_parent_and_ablation() -> None:
    parent = parent_policy()
    candidate = candidate_for(
        parent, "When evidence conflicts, state the conflict and abstain."
    )

    def scorer(case: RegressionCase, policy: PolicySnapshot) -> float:
        del case
        contents = "\n".join(artifact.content for artifact in policy.artifacts)
        return 1.0 if "abstain" in contents else 0.2

    report = run_regression(parent, candidate, cases(), scorer)

    assert report.passed is True
    assert report.partition_gains[Partition.VALIDATION] == pytest.approx(0.8)
    assert report.partition_gains[Partition.HOLDOUT] == pytest.approx(0.8)
    assert report.ablation_gain == pytest.approx(0.8)
    assert all(
        measurement.candidate_score > measurement.parent_score
        for measurement in report.measurements
    )


def test_candidate_that_hurts_regression_partition_is_rejected() -> None:
    parent = parent_policy()
    candidate = candidate_for(parent, "Always guess immediately.")

    def scorer(case: RegressionCase, policy: PolicySnapshot) -> float:
        contents = "\n".join(artifact.content for artifact in policy.artifacts)
        if case.partition is Partition.REGRESSION and "Always guess" in contents:
            return 0.0
        if "Always guess" in contents:
            return 0.8
        return 0.6

    report = run_regression(parent, candidate, cases(), scorer)

    assert report.passed is False
    assert report.partition_gains[Partition.REGRESSION] == pytest.approx(-0.6)


def test_candidate_parent_digest_must_match() -> None:
    parent = parent_policy()
    candidate = candidate_for(parent, "A useful rule.")
    mismatched = CandidatePatch(
        parent_policy_digest="different",
        diagnosis_digest=candidate.diagnosis_digest,
        mutation=candidate.mutation,
        artifact_id=candidate.artifact_id,
        content=candidate.content,
        provenance=candidate.provenance,
        digest=candidate.digest,
    )

    with pytest.raises(ValueError, match="parent policy digest"):
        apply_candidate(parent, mismatched)
