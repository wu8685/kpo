from __future__ import annotations

import json
import sys

import pytest

from kpo.diagnosis import diagnose
from kpo.models import (
    Diagnosis,
    DiagnosisKind,
    Evaluation,
    FailureSignal,
    MutationKind,
    PolicyArtifact,
    PolicySnapshot,
    ReferenceArtifact,
    Rollout,
    ScoreDimension,
)
from kpo.pipeline import ProposerRequest, run_proposer
from kpo.profile import CommandProviderConfig
from kpo.provider import CommandProposerProvider, ProviderFailure
from kpo.reasoning_differential import (
    AxisAssessment,
    DifferentialAxis,
    DifferentialDiagnosis,
    DifferentialKind,
    GapSummary,
    ClaimType,
)


def _inputs() -> tuple[PolicySnapshot, Rollout, Evaluation, Diagnosis, tuple[ReferenceArtifact, ...]]:
    policy = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Base rule."),))
    rollout = Rollout.create(
        case_id="train-1",
        policy_digest=policy.digest,
        model_id="actor",
        output="baseline",
        retrieved_policy_ids=policy.artifact_ids,
        actor_payload_digest="payload",
    )
    evaluation = Evaluation.create(
        rollout_digest=rollout.digest,
        evaluator_id="evaluator",
        rubric_version="v1",
        dimensions=(
            ScoreDimension("alignment", 0.2, "Missing rule.", (rollout.digest, "ref-1")),
        ),
        failure_signals=(
            FailureSignal(
                DiagnosisKind.MISSING_POLICY_KNOWLEDGE,
                "A boundary rule is missing.",
                (rollout.digest, "ref-1"),
            ),
        ),
    )
    return (
        policy,
        rollout,
        evaluation,
        diagnose(evaluation),
        (ReferenceArtifact("ref-1", "expected", "Train reference excerpt."),),
    )


def test_command_proposer_is_isolated_and_returns_provenance_complete_candidate() -> None:
    response = {
        "mutation": "add",
        "artifact_id": "boundary-rule",
        "content": "Add the missing boundary.",
        "expected_benefit": "Improve boundary handling.",
        "possible_regressions": ["May abstain more often."],
    }
    code = (
        "import json,sys; r=json.load(sys.stdin); s=json.dumps(r).lower(); "
        "assert r['role']=='proposer'; assert 'holdout' not in s; "
        "assert 'approval' not in s; assert 'promotion' not in s; "
        f"json.dump({response!r},sys.stdout)"
    )
    config = CommandProviderConfig(
        (sys.executable, "-c", code), "proposer-model", 2, ()
    )
    policy, rollout, evaluation, diagnosis, references = _inputs()

    proposed = run_proposer(
        diagnosis,
        policy,
        rollout,
        evaluation,
        references,
        CommandProposerProvider(config),
    )

    assert proposed.candidate.mutation is MutationKind.ADD
    assert proposed.candidate.artifact_id == "boundary-rule"
    assert evaluation.digest in proposed.candidate.provenance
    assert "ref-1" in proposed.candidate.provenance
    assert proposed.expected_benefit.startswith("Improve")


def test_non_patchable_diagnosis_never_invokes_proposer() -> None:
    class MustNotRun:
        def propose(self, request: ProposerRequest) -> object:
            raise AssertionError("proposer must not be invoked")

    policy, rollout, evaluation, _, references = _inputs()
    diagnosis = Diagnosis(
        evaluation.digest,
        DiagnosisKind.RUNTIME_OR_PROVIDER_FAILURE,
        "provider failed",
        (rollout.digest,),
        False,
        "diagnosis",
    )
    with pytest.raises(ValueError, match="does not permit"):
        run_proposer(
            diagnosis, policy, rollout, evaluation, references, MustNotRun()
        )


def test_proposer_rejects_unsupported_mutation() -> None:
    response = {
        "mutation": "deprecate",
        "artifact_id": "base",
        "content": "deprecated",
        "expected_benefit": "remove",
        "possible_regressions": [],
    }
    code = f"import json,sys; json.load(sys.stdin); json.dump({response!r},sys.stdout)"
    config = CommandProviderConfig((sys.executable, "-c", code), "p", 2, ())
    policy, rollout, evaluation, diagnosis, references = _inputs()
    with pytest.raises(ProviderFailure, match="unsupported"):
        run_proposer(
            diagnosis,
            policy,
            rollout,
            evaluation,
            references,
            CommandProposerProvider(config),
        )


def test_proposer_requires_every_cited_reference_excerpt() -> None:
    class MustNotRun:
        proposer_id = "unused"

        def propose(self, request: ProposerRequest) -> object:
            raise AssertionError("proposer must not run with incomplete evidence")

    policy, rollout, evaluation, diagnosis, references = _inputs()
    incomplete = Diagnosis(
        diagnosis.evaluation_digest,
        diagnosis.kind,
        diagnosis.message,
        (*diagnosis.citations, "ref-2"),
        True,
        "incomplete",
    )
    with pytest.raises(ValueError, match="references"):
        run_proposer(
            incomplete, policy, rollout, evaluation, references, MustNotRun()
        )


def _differential_diagnosis(evaluation_digest: str) -> DifferentialDiagnosis:
    return DifferentialDiagnosis.create(
        evaluation_digest=evaluation_digest,
        claim_differential_digest="b" * 64,
        conclusion_validity=DifferentialAxis(
            AxisAssessment.CHALLENGED, 0.8, "Evaluation challenges the conclusion."
        ),
        reasoning_fidelity=DifferentialAxis(
            AxisAssessment.SUPPORTED, 0.9, "Structure is similar."
        ),
        reasoning_soundness=DifferentialAxis(
            AxisAssessment.CHALLENGED, 0.7, "A boundary step is absent."
        ),
        gaps=(
            GapSummary(
                DifferentialKind.MISSING,
                ClaimType.ANALYTICAL,
                None,
                "boundary-check",
                0.8,
                "add_boundary_rule",
            ),
        ),
    )


def test_legacy_proposer_wire_request_omits_differential_field() -> None:
    response = {
        "mutation": "add",
        "artifact_id": "boundary-rule",
        "content": "Add the boundary.",
        "expected_benefit": "Improve reasoning.",
        "possible_regressions": [],
    }
    code = (
        "import json,sys; r=json.load(sys.stdin); "
        "assert 'differential_diagnosis' not in r['request']; "
        f"json.dump({response!r},sys.stdout)"
    )
    policy, rollout, evaluation, diagnosis, references = _inputs()

    run_proposer(
        diagnosis,
        policy,
        rollout,
        evaluation,
        references,
        CommandProposerProvider(
            CommandProviderConfig((sys.executable, "-c", code), "p", 2, ())
        ),
    )


def test_configured_proposer_receives_only_public_differential_diagnosis() -> None:
    response = {
        "mutation": "add",
        "artifact_id": "boundary-rule",
        "content": "Add the boundary.",
        "expected_benefit": "Improve reasoning.",
        "possible_regressions": [],
    }
    code = (
        "import json,sys; r=json.load(sys.stdin)['request']; "
        "d=r['differential_diagnosis']; s=json.dumps(d); "
        "assert d['reference_is_standard'] is False; "
        "assert d['gaps'][0]['recommendation_category']=='add_boundary_rule'; "
        "assert 'Train reference excerpt' not in s; "
        "assert 'baseline' not in s; assert 'claim_id' not in s; "
        f"json.dump({response!r},sys.stdout)"
    )
    policy, rollout, evaluation, diagnosis, references = _inputs()

    run_proposer(
        diagnosis,
        policy,
        rollout,
        evaluation,
        references,
        CommandProposerProvider(
            CommandProviderConfig((sys.executable, "-c", code), "p", 2, ())
        ),
        differential_diagnosis=_differential_diagnosis(evaluation.digest),
    )
