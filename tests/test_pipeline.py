from dataclasses import dataclass
from typing import Any

import pytest

from kpo.models import (
    Evaluation,
    PolicyArtifact,
    PolicySnapshot,
    ReferenceArtifact,
    ScoreDimension,
    TaskCase,
)
from kpo.pipeline import ActorRequest, EvaluatorRequest, evaluate_rollout, run_actor


@dataclass
class RecordingActor:
    request: ActorRequest | None = None

    def generate(self, request: ActorRequest) -> str:
        self.request = request
        return "Take the verified route and state uncertainty."


@dataclass
class RecordingEvaluator:
    request: EvaluatorRequest | None = None

    def evaluate(self, request: EvaluatorRequest) -> tuple[ScoreDimension, ...]:
        self.request = request
        return (
            ScoreDimension(
                name="conclusion_alignment",
                score=1.0,
                explanation="The route matches the hidden reference.",
                citations=(request.rollout.digest, request.references[0].artifact_id),
            ),
        )


def make_case() -> TaskCase:
    return TaskCase(
        case_id="case-1",
        prompt="Choose a route.",
        visible_context=("The north route is verified.",),
        hidden_reference_ids=("reference-1",),
        tags=("synthetic",),
    )


def make_policy() -> PolicySnapshot:
    return PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "Prefer independently verified routes."),)
    )


def test_actor_never_receives_hidden_references() -> None:
    actor = RecordingActor()
    case = make_case()

    rollout = run_actor(case, make_policy(), actor, model_id="fake-actor")

    assert actor.request is not None
    actor_payload: dict[str, Any] = actor.request.case_payload
    assert "hidden_reference_ids" not in actor_payload
    assert "reference-1" not in repr(actor.request)
    assert rollout.output.startswith("Take the verified route")
    assert rollout.retrieved_policy_ids == ("rule-1",)


def test_evaluator_receives_hidden_reference_only_after_rollout() -> None:
    actor = RecordingActor()
    evaluator = RecordingEvaluator()
    case = make_case()
    rollout = run_actor(case, make_policy(), actor, model_id="fake-actor")
    reference = ReferenceArtifact(
        artifact_id="reference-1",
        kind="expected_result",
        content="Use the verified north route.",
    )

    evaluation = evaluate_rollout(
        case,
        rollout,
        (reference,),
        evaluator,
        evaluator_id="fake-evaluator",
        rubric_version="v1",
    )

    assert evaluator.request is not None
    assert evaluator.request.references == (reference,)
    assert evaluation.rollout_digest == rollout.digest
    assert evaluation.dimensions[0].score == 1.0


def test_evaluator_requires_exact_hidden_reference_set() -> None:
    case = make_case()
    rollout = run_actor(case, make_policy(), RecordingActor(), model_id="fake-actor")

    with pytest.raises(ValueError, match="hidden reference set"):
        evaluate_rollout(
            case,
            rollout,
            (),
            RecordingEvaluator(),
            evaluator_id="fake-evaluator",
            rubric_version="v1",
        )


def test_score_dimension_rejects_out_of_range_score() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        ScoreDimension(
            name="validity",
            score=1.1,
            explanation="Invalid score.",
            citations=("artifact-1",),
        )


def test_evaluation_preserves_score_vector_without_requiring_aggregate() -> None:
    dimensions = (
        ScoreDimension("fidelity", 0.8, "Close match.", ("a",)),
        ScoreDimension("validity", 0.6, "One unsupported claim.", ("b",)),
    )

    evaluation = Evaluation.create(
        rollout_digest="rollout",
        evaluator_id="evaluator",
        rubric_version="v1",
        dimensions=dimensions,
    )

    assert evaluation.dimensions == dimensions
    assert evaluation.aggregate_score is None
