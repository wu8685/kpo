from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from kpo.diagnosis import diagnose, propose_patch
from kpo.digest import canonical_json
from kpo.models import (
    DiagnosisKind,
    Evaluation,
    FailureSignal,
    MutationKind,
    Partition,
    PolicyArtifact,
    PolicySnapshot,
    ReferenceArtifact,
    RegressionCase,
    ScoreDimension,
    TaskCase,
)
from kpo.pipeline import ActorRequest, EvaluatorRequest, evaluate_rollout, run_actor
from kpo.promotion import PromotionManager
from kpo.regression import apply_candidate, run_regression
from kpo.runtime import RunState, RuntimeStore, resolve_data_home


class SyntheticActor:
    def generate(self, request: ActorRequest) -> str:
        policy_text = "\n".join(
            artifact.content for artifact in request.policy_artifacts
        ).casefold()
        if "abstain" in policy_text:
            return "Evidence conflicts, so abstain and request verification."
        return "Proceed using the first available signal."


class SyntheticEvaluator:
    def evaluate(self, request: EvaluatorRequest) -> tuple[ScoreDimension, ...]:
        aligned = "abstain" in request.rollout.output.casefold()
        score = 1.0 if aligned else 0.2
        explanation = (
            "The conclusion follows the hidden reference."
            if aligned
            else "The conclusion ignores the required conflict boundary."
        )
        return (
            ScoreDimension(
                name="conclusion_alignment",
                score=score,
                explanation=explanation,
                citations=(
                    request.rollout.digest,
                    request.references[0].artifact_id,
                ),
            ),
        )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_synthetic_demo(
    *, checkout: Path, data_home: Path, target_root: Path
) -> dict[str, Any]:
    runtime_root = resolve_data_home(checkout, data_home)
    store = RuntimeStore(runtime_root)
    target = target_root.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    source_digest_before = _tree_digest(target)

    case = TaskCase(
        case_id="synthetic-conflict-1",
        prompt="Decide whether to proceed when two signals conflict.",
        visible_context=(
            "One sensor says proceed; an independent sensor says stop.",
        ),
        hidden_reference_ids=("synthetic-reference-1",),
        tags=("synthetic", "conflict"),
    )
    reference = ReferenceArtifact(
        artifact_id="synthetic-reference-1",
        kind="expected_result",
        content="Abstain until the conflicting evidence is verified.",
    )
    parent = PolicySnapshot.from_artifacts(
        (PolicyArtifact("base-rule", "Use available evidence."),)
    )
    actor = SyntheticActor()
    evaluator = SyntheticEvaluator()

    run_id = "synthetic-demo"
    store.create_run(run_id, case_id=case.case_id, policy_digest=parent.digest)
    store.transition_run(run_id, RunState.RUNNING)
    baseline_rollout = run_actor(case, parent, actor, model_id="synthetic-actor")
    store.put_artifact("rollout", canonical_json(baseline_rollout))
    store.transition_run(run_id, RunState.COMPLETED)

    base_evaluation = evaluate_rollout(
        case,
        baseline_rollout,
        (reference,),
        evaluator,
        evaluator_id="synthetic-evaluator",
        rubric_version="v1",
    )
    baseline_score = base_evaluation.dimensions[0].score
    evaluation = Evaluation.create(
        rollout_digest=base_evaluation.rollout_digest,
        evaluator_id=base_evaluation.evaluator_id,
        rubric_version=base_evaluation.rubric_version,
        dimensions=base_evaluation.dimensions,
        failure_signals=(
            FailureSignal(
                kind=DiagnosisKind.MISSING_APPLICABILITY_BOUNDARY,
                message="The policy has no rule for conflicting evidence.",
                citations=(baseline_rollout.digest, reference.artifact_id),
            ),
        ),
    )
    store.put_artifact("evaluation", canonical_json(evaluation))
    store.transition_run(run_id, RunState.EVALUATED)

    diagnosis = diagnose(evaluation)
    store.put_artifact("diagnosis", canonical_json(diagnosis))
    store.transition_run(run_id, RunState.DIAGNOSED)
    candidate = propose_patch(
        diagnosis,
        parent,
        mutation=MutationKind.ADD,
        artifact_id="conflict-rule",
        content="When evidence conflicts, state the conflict and abstain.\n",
    )
    store.put_artifact("candidate", canonical_json(candidate))
    store.transition_run(run_id, RunState.CANDIDATE)

    regression_cases = (
        RegressionCase("synthetic-train-1", Partition.TRAIN),
        RegressionCase("synthetic-validation-1", Partition.VALIDATION),
        RegressionCase("synthetic-holdout-1", Partition.HOLDOUT),
        RegressionCase("synthetic-regression-1", Partition.REGRESSION),
    )

    def scorer(regression_case: RegressionCase, policy: PolicySnapshot) -> float:
        scoring_case = TaskCase(
            case_id=regression_case.case_id,
            prompt=case.prompt,
            visible_context=case.visible_context,
            tags=("synthetic", regression_case.partition.value),
        )
        rollout = run_actor(
            scoring_case, policy, actor, model_id="synthetic-actor"
        )
        return 1.0 if "abstain" in rollout.output.casefold() else 0.2

    report = run_regression(parent, candidate, regression_cases, scorer)
    store.put_artifact("regression", canonical_json(report))
    store.transition_run(run_id, RunState.REGRESSED)

    candidate_policy = apply_candidate(parent, candidate)
    candidate_score = scorer(regression_cases[2], candidate_policy)
    manager = PromotionManager(runtime_root / "promotion")
    preview = manager.preview(
        candidate,
        report,
        target_root=target,
        relative_path="policies/conflict-rule.md",
        allowlist=("policies",),
    )
    store.put_artifact("promotion_preview", preview.diff)
    store.transition_run(run_id, RunState.PROMOTION_PREVIEWED)

    source_digest_after = _tree_digest(target)
    return {
        "run_id": run_id,
        "baseline_score": baseline_score,
        "candidate_score": candidate_score,
        "validation_gain": report.partition_gains[Partition.VALIDATION],
        "holdout_gain": report.partition_gains[Partition.HOLDOUT],
        "regression_gain": report.partition_gains[Partition.REGRESSION],
        "ablation_gain": report.ablation_gain,
        "regression_passed": report.passed,
        "source_unchanged": source_digest_before == source_digest_after,
        "promotion_state": "preview_only",
        "promotion_diff_digest": preview.diff_digest,
        "promotion_diff": preview.diff,
        "parent_policy_digest": parent.digest,
        "candidate_policy_digest": report.candidate_policy_digest,
    }
