from __future__ import annotations

import sys

import pytest

from kpo.models import (
    DiagnosisKind,
    Evaluation,
    FailureSignal,
    ScoreDimension,
)
from kpo.profile import CommandProviderConfig
from kpo.provider import ProviderFailure, ProviderFailureKind
from kpo.reasoning_differential import (
    AxisAssessment,
    ClaimType,
    CommandClaimDifferProvider,
    CommandDifferentialDiagnosticianProvider,
    CommandReasoningExtractorProvider,
    DifferentialKind,
)


def _config(response: str) -> CommandProviderConfig:
    code = f"import sys; sys.stdout.write({response!r})"
    return CommandProviderConfig(
        (sys.executable, "-c", code), "test-provider", 2, ()
    )


def _claims(provenance: str, claim_id: str = "claim-1") -> str:
    return (
        '{"schema_version":"v1","claims":[{'
        f'"claim_id":"{claim_id}","claim_type":"analytical",'
        f'"text":"private {claim_id}","evidence_citations":["source-1"],'
        '"prerequisite_claim_ids":[],"confidence":0.8,"salience":0.7,'
        f'"provenance":"{provenance}"'
        '}]}'
    )


def _graph(provenance: str, claim_id: str = "claim-1"):
    return CommandReasoningExtractorProvider(
        _config(_claims(provenance, claim_id))
    ).extract(
        source_kind=provenance,
        sources=(("source-1", "private source content"),),
    )


def _evaluation() -> Evaluation:
    return Evaluation.create(
        rollout_digest="a" * 64,
        evaluator_id="evaluator-v1",
        rubric_version="rubric-v1",
        dimensions=(ScoreDimension("quality", 0.2, "weak", ("ref-1",)),),
        failure_signals=(
            FailureSignal(DiagnosisKind.MISSING_POLICY_KNOWLEDGE, "gap", ("ref-1",)),
        ),
        aggregate_score=0.2,
    )


def test_extractor_parses_strict_claim_graph_and_authorized_citations() -> None:
    graph = _graph("agent_rollout")

    assert graph.claims[0].claim_type is ClaimType.ANALYTICAL
    assert graph.claims[0].provenance == "agent_rollout"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('{"schema_version":"v2","claims":[]}', "schema"),
        (_claims("reference_artifact"), "provenance"),
        (
            _claims("agent_rollout").replace("source-1", "unauthorized"),
            "citation",
        ),
        (_claims("agent_rollout")[:-1] + ',"extra":true}', "fields"),
    ],
)
def test_extractor_rejects_malformed_or_unauthorized_response(
    response: str, message: str
) -> None:
    provider = CommandReasoningExtractorProvider(_config(response))

    with pytest.raises(ProviderFailure, match=message):
        provider.extract(
            source_kind="agent_rollout",
            sources=(("source-1", "private source content"),),
        )


def test_differ_parses_and_binds_both_claim_graphs() -> None:
    reference = _graph("reference_artifact", "reference-1")
    agent = _graph("agent_rollout", "agent-1")
    response = (
        '{"schema_version":"v1","entries":['
        '{"kind":"contradicted","reference_claim_id":"reference-1",'
        '"agent_claim_id":"agent-1","summary":"opposed",'
        '"confidence":0.9}]}'
    )

    differential = CommandClaimDifferProvider(_config(response)).compare(
        reference, agent
    )

    assert differential.reference_claims_digest == reference.digest
    assert differential.agent_claims_digest == agent.digest
    assert differential.entries[0].kind is DifferentialKind.CONTRADICTED


def test_diagnostician_uses_evaluation_summary_and_parses_independent_axes() -> None:
    reference = _graph("reference_artifact", "reference-1")
    agent = _graph("agent_rollout", "agent-1")
    differential = CommandClaimDifferProvider(
        _config(
            '{"schema_version":"v1","entries":['
            '{"kind":"matched","reference_claim_id":"reference-1",'
            '"agent_claim_id":"agent-1","summary":"same",'
            '"confidence":0.9}]}'
        )
    ).compare(reference, agent)
    response = (
        '{"schema_version":"v1","reference_is_standard":false,'
        '"conclusion_validity":{"assessment":"challenged","confidence":0.8,'
        '"summary":"Evaluation evidence challenges it."},'
        '"reasoning_fidelity":{"assessment":"supported","confidence":0.9,'
        '"summary":"Structure matches."},'
        '"reasoning_soundness":{"assessment":"unavailable","confidence":0.4,'
        '"summary":"Evidence is incomplete."},'
        '"gaps":[{"kind":"matched","reference_claim_type":"analytical",'
        '"agent_claim_type":"analytical","frame_label":"causal",'
        '"confidence":0.9,"recommendation_category":"retain_structure"}]}'
    )

    diagnosis = CommandDifferentialDiagnosticianProvider(
        _config(response)
    ).diagnose(differential, _evaluation())

    assert diagnosis.evaluation_digest == _evaluation().digest
    assert diagnosis.conclusion_validity.assessment is AxisAssessment.CHALLENGED
    assert diagnosis.reasoning_fidelity.assessment is AxisAssessment.SUPPORTED
    assert diagnosis.reasoning_soundness.assessment is AxisAssessment.UNAVAILABLE


def test_differential_provider_process_failure_remains_typed() -> None:
    code = "import sys; sys.exit(7)"
    provider = CommandReasoningExtractorProvider(
        CommandProviderConfig((sys.executable, "-c", code), "failed", 2, ())
    )

    with pytest.raises(ProviderFailure) as captured:
        provider.extract(
            source_kind="agent_rollout",
            sources=(("source-1", "content"),),
        )

    assert captured.value.kind is ProviderFailureKind.NON_ZERO_EXIT
