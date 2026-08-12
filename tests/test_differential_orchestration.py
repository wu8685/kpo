from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kpo.models import (
    DiagnosisKind,
    Evaluation,
    FailureSignal,
    Partition,
    ReferenceArtifact,
    Rollout,
    ScoreDimension,
)
from kpo.profile import CommandProviderConfig
from kpo.reasoning_differential import run_reasoning_differential


@dataclass
class RecordingExecutor:
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(
        default_factory=list
    )

    def invoke(self, config, role, request, *, context):
        self.calls.append((role, request, dict(context)))
        if role == "reasoning_extractor":
            provenance = request["source_kind"]
            source_id = request["sources"][0]["source_id"]
            claim_id = "reference-claim" if provenance == "reference_artifact" else "agent-claim"
            return {
                "schema_version": "v1",
                "claims": [
                    {
                        "claim_id": claim_id,
                        "claim_type": "analytical",
                        "text": f"private {provenance}",
                        "evidence_citations": [source_id],
                        "prerequisite_claim_ids": [],
                        "confidence": 0.8,
                        "salience": 0.7,
                        "provenance": provenance,
                    }
                ],
            }
        if role == "claim_differ":
            return {
                "schema_version": "v1",
                "entries": [
                    {
                        "kind": "matched",
                        "reference_claim_id": "reference-claim",
                        "agent_claim_id": "agent-claim",
                        "summary": "same structure",
                        "confidence": 0.9,
                    }
                ],
            }
        assert role == "differential_diagnostician"
        return {
            "schema_version": "v1",
            "reference_is_standard": False,
            "conclusion_validity": {
                "assessment": "challenged",
                "confidence": 0.8,
                "summary": "Evaluation challenges it.",
            },
            "reasoning_fidelity": {
                "assessment": "supported",
                "confidence": 0.9,
                "summary": "Structure matches.",
            },
            "reasoning_soundness": {
                "assessment": "supported",
                "confidence": 0.7,
                "summary": "Chain is supported.",
            },
            "gaps": [],
        }


def _config(identity: str) -> CommandProviderConfig:
    return CommandProviderConfig(("/bin/true",), identity, 2, ())


def _rollout() -> Rollout:
    return Rollout.create(
        case_id="train-case",
        policy_digest="p" * 64,
        model_id="actor-v1",
        output="private agent output",
        retrieved_policy_ids=("base",),
        actor_payload_digest="c" * 64,
    )


def _evaluation(rollout: Rollout) -> Evaluation:
    return Evaluation.create(
        rollout_digest=rollout.digest,
        evaluator_id="evaluator-v1",
        rubric_version="rubric-v1",
        dimensions=(ScoreDimension("quality", 0.2, "weak", ("ref-1",)),),
        failure_signals=(
            FailureSignal(DiagnosisKind.MISSING_POLICY_KNOWLEDGE, "gap", ("ref-1",)),
        ),
        aggregate_score=0.2,
    )


def _run(partition: Partition, executor: RecordingExecutor):
    rollout = _rollout()
    return run_reasoning_differential(
        partition=partition,
        rollout=rollout,
        references=(
            ReferenceArtifact("ref-1", "analysis", "private reference one"),
            ReferenceArtifact("ref-2", "analysis", "private reference two"),
        ),
        evaluation=_evaluation(rollout),
        extractor=_config("extractor-v1"),
        differ=_config("differ-v1"),
        diagnostician=_config("diagnostician-v1"),
        executor=executor,
        context={
            "case_digest": "case",
            "policy_digest": "policy",
            "reference_set_digest": "refs",
            "rubric_digest": "rubric",
            "decoding_configuration_digest": "decoding",
            "random_seed": 0,
        },
    )


@pytest.mark.parametrize(
    "partition",
    [Partition.VALIDATION, Partition.HOLDOUT, Partition.REGRESSION],
)
def test_differential_orchestration_rejects_non_train_before_calls(
    partition: Partition,
) -> None:
    executor = RecordingExecutor()

    with pytest.raises(ValueError, match="train"):
        _run(partition, executor)

    assert executor.calls == []


def test_train_orchestration_makes_four_calls_and_preserves_reference_order() -> None:
    executor = RecordingExecutor()

    analysis = _run(Partition.TRAIN, executor)

    assert [role for role, _, _ in executor.calls] == [
        "reasoning_extractor",
        "reasoning_extractor",
        "claim_differ",
        "differential_diagnostician",
    ]
    agent_request = executor.calls[0][1]
    reference_request = executor.calls[1][1]
    assert agent_request["sources"] == [
        {"source_id": "agent-rollout", "content": "private agent output"}
    ]
    assert [source["source_id"] for source in reference_request["sources"]] == [
        "ref-1",
        "ref-2",
    ]
    assert "private agent output" not in str(reference_request)
    assert "private reference" not in str(agent_request)
    assert analysis.diagnosis.reference_is_standard is False
