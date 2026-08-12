from __future__ import annotations

import json

import pytest

from kpo.reasoning_differential import (
    AxisAssessment,
    ClaimDifferential,
    ClaimGraph,
    ClaimType,
    DifferentialAxis,
    DifferentialDiagnosis,
    DifferentialEntry,
    DifferentialKind,
    GapSummary,
    ReasoningClaim,
)


def _claim(
    claim_id: str,
    *,
    provenance: str,
    prerequisites: tuple[str, ...] = (),
    claim_type: ClaimType = ClaimType.ANALYTICAL,
) -> ReasoningClaim:
    return ReasoningClaim(
        claim_id=claim_id,
        claim_type=claim_type,
        text=f"Private text for {claim_id}.",
        evidence_citations=(f"source-{claim_id}",),
        prerequisite_claim_ids=prerequisites,
        confidence=0.8,
        salience=0.7,
        provenance=provenance,
    )


def test_claim_graph_is_deterministic_and_supports_a_dag() -> None:
    graph = ClaimGraph.create(
        (
            _claim("frame", provenance="reference_artifact"),
            _claim(
                "conclusion",
                provenance="reference_artifact",
                prerequisites=("frame",),
                claim_type=ClaimType.CONCLUSIVE,
            ),
        )
    )

    assert graph.protocol == "kpo.reasoning-claims/v1"
    assert graph.digest == ClaimGraph.create(graph.claims).digest
    assert graph.claims[1].prerequisite_claim_ids == ("frame",)


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ((), "must not be empty"),
        (
            (
                _claim("same", provenance="agent_rollout"),
                _claim("same", provenance="agent_rollout"),
            ),
            "duplicate claim_id",
        ),
        (
            (
                _claim(
                    "a",
                    provenance="agent_rollout",
                    prerequisites=("missing",),
                ),
            ),
            "unknown prerequisite",
        ),
        (
            (
                _claim("a", provenance="agent_rollout", prerequisites=("b",)),
                _claim("b", provenance="agent_rollout", prerequisites=("a",)),
            ),
            "acyclic",
        ),
    ],
)
def test_claim_graph_rejects_invalid_structure(
    claims: tuple[ReasoningClaim, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ClaimGraph.create(claims)


def test_claim_rejects_invalid_scalar_and_provenance() -> None:
    with pytest.raises(ValueError, match="confidence"):
        ReasoningClaim(
            "a",
            ClaimType.FACTUAL,
            "text",
            (),
            (),
            1.1,
            0.5,
            "agent_rollout",
        )
    with pytest.raises(ValueError, match="provenance"):
        _claim("a", provenance="unknown")


def test_claim_differential_validates_all_five_kinds_and_accounting() -> None:
    reference = ClaimGraph.create(
        tuple(
            _claim(name, provenance="reference_artifact")
            for name in ("match-r", "contradict-r", "missing-r", "frame-r")
        )
    )
    agent = ClaimGraph.create(
        tuple(
            _claim(name, provenance="agent_rollout")
            for name in ("match-a", "contradict-a", "surplus-a", "frame-a")
        )
    )
    entries = (
        DifferentialEntry(DifferentialKind.MATCHED, "match-r", "match-a", "same", 0.9),
        DifferentialEntry(
            DifferentialKind.CONTRADICTED,
            "contradict-r",
            "contradict-a",
            "opposed",
            0.8,
        ),
        DifferentialEntry(DifferentialKind.MISSING, "missing-r", None, "gap", 0.7),
        DifferentialEntry(DifferentialKind.SURPLUS, None, "surplus-a", "leap", 0.7),
        DifferentialEntry(
            DifferentialKind.FRAME_DIVERGENCE,
            "frame-r",
            "frame-a",
            "different frames",
            0.8,
        ),
    )

    differential = ClaimDifferential.create(reference, agent, entries)

    assert differential.protocol == "kpo.claim-differential/v1"
    assert differential.reference_claims_digest == reference.digest
    assert differential.agent_claims_digest == agent.digest
    assert {entry.kind for entry in differential.entries} == set(DifferentialKind)


@pytest.mark.parametrize(
    "entry",
    [
        DifferentialEntry(DifferentialKind.MATCHED, "r", None, "bad", 0.5),
        DifferentialEntry(DifferentialKind.MISSING, "r", "a", "bad", 0.5),
        DifferentialEntry(DifferentialKind.SURPLUS, "r", "a", "bad", 0.5),
        DifferentialEntry(
            DifferentialKind.FRAME_DIVERGENCE, "r", None, "bad", 0.5
        ),
    ],
)
def test_differential_entry_rejects_invalid_coordinates(
    entry: DifferentialEntry,
) -> None:
    reference = ClaimGraph.create((_claim("r", provenance="reference_artifact"),))
    agent = ClaimGraph.create((_claim("a", provenance="agent_rollout"),))
    with pytest.raises(ValueError, match="coordinates"):
        ClaimDifferential.create(reference, agent, (entry,))


def test_claim_differential_requires_complete_claim_accounting() -> None:
    reference = ClaimGraph.create((_claim("r", provenance="reference_artifact"),))
    agent = ClaimGraph.create((_claim("a", provenance="agent_rollout"),))
    with pytest.raises(ValueError, match="account for every claim"):
        ClaimDifferential.create(reference, agent, ())


def test_three_axis_diagnosis_is_independent_and_privacy_safe() -> None:
    diagnosis = DifferentialDiagnosis.create(
        evaluation_digest="a" * 64,
        claim_differential_digest="b" * 64,
        conclusion_validity=DifferentialAxis(
            AxisAssessment.SUPPORTED, 0.9, "Supported by evaluation aaaa."
        ),
        reasoning_fidelity=DifferentialAxis(
            AxisAssessment.CHALLENGED, 0.8, "Structure differs."
        ),
        reasoning_soundness=DifferentialAxis(
            AxisAssessment.SUPPORTED, 0.7, "Internal chain is sound."
        ),
        gaps=(
            GapSummary(
                DifferentialKind.FRAME_DIVERGENCE,
                ClaimType.ANALYTICAL,
                ClaimType.ANALYTICAL,
                "causal-frame",
                0.8,
                "review_applicability_boundary",
            ),
        ),
    )

    assert diagnosis.protocol == "kpo.differential-diagnosis/v1"
    assert diagnosis.reference_is_standard is False
    assert diagnosis.conclusion_validity.assessment is AxisAssessment.SUPPORTED
    assert diagnosis.reasoning_fidelity.assessment is AxisAssessment.CHALLENGED
    serialized = json.dumps(diagnosis.to_public_value(), sort_keys=True)
    assert "Private text" not in serialized
    assert "claim_id" not in serialized
    assert "reference is comparison material" in serialized
    assert diagnosis.digest
