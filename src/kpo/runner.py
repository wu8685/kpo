from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from kpo.digest import canonical_digest, canonical_json
from kpo.models import Rollout
from kpo.integrity import IntegrityMonitor, IntegrityViolation
from kpo.pipeline import evaluate_rollout, run_actor
from kpo.profile import ExternalProfile, load_profile
from kpo.provider import CommandActorProvider, CommandEvaluatorProvider, ProviderFailure
from kpo.runtime import RunState, RuntimeStore


def _profile_digest(profile: ExternalProfile) -> str:
    return canonical_digest(
        {"profile_id": profile.profile_id, "source_digests": profile.source_digests}
    )


def profile_summary(profile: ExternalProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "policy_digest": profile.policy.digest,
        "policy_artifact_count": len(profile.policy.artifacts),
        "case_count": len(profile.cases),
        "reference_count": len(profile.references),
        "rubric_version": profile.rubric.rubric_version,
        "rubric_dimensions": list(profile.rubric.dimension_names),
        "actor_adapter": "command",
        "evaluator_adapter": "command",
    }


def run_profile(profile_path: Path, case_id: str, *, checkout: Path) -> dict[str, Any]:
    profile = load_profile(profile_path, checkout=checkout)
    if case_id not in profile.cases:
        raise KeyError(f"unknown case: {case_id}")
    profile.data_home.mkdir(parents=True, exist_ok=True)
    monitor = IntegrityMonitor.from_relative_digests(
        profile.path.parent,
        profile.source_digests,
        extra_files=(
            Path(profile.actor.command[0]),
            Path(profile.evaluator.command[0]),
        ),
    )
    store = RuntimeStore(profile.data_home)
    run_id = uuid.uuid4().hex
    record = store.create_run(
        run_id,
        case_id=case_id,
        policy_digest=profile.policy.digest,
        profile_digest=_profile_digest(profile),
    )
    source = store.put_artifact("source_digests", canonical_json(profile.source_digests))
    store.link_artifact(run_id, "source_digests", source.digest)
    store.transition_run(run_id, RunState.RUNNING)
    try:
        rollout = run_actor(
            profile.cases[case_id],
            profile.policy,
            CommandActorProvider(profile.actor),
            model_id=profile.actor.identity,
        )
        monitor.verify()
    except (ProviderFailure, IntegrityViolation):
        store.transition_run(run_id, RunState.FAILED)
        raise
    artifact = store.put_artifact("rollout", canonical_json(rollout))
    store.link_artifact(run_id, "rollout", artifact.digest)
    record = store.transition_run(run_id, RunState.COMPLETED)
    return {
        "run_id": record.run_id,
        "state": record.state.value,
        "case_id": record.case_id,
        "policy_digest": record.policy_digest,
        "rollout_digest": rollout.digest,
        "model_id": rollout.model_id,
    }


def _rollout(value: str) -> Rollout:
    data = json.loads(value)
    return Rollout(
        case_id=data["case_id"],
        policy_digest=data["policy_digest"],
        model_id=data["model_id"],
        output=data["output"],
        retrieved_policy_ids=tuple(data["retrieved_policy_ids"]),
        actor_payload_digest=data["actor_payload_digest"],
        digest=data["digest"],
    )


def evaluate_profile_run(
    profile_path: Path, run_id: str, *, checkout: Path
) -> dict[str, Any]:
    profile = load_profile(profile_path, checkout=checkout)
    monitor = IntegrityMonitor.from_relative_digests(
        profile.path.parent,
        profile.source_digests,
        extra_files=(
            Path(profile.actor.command[0]),
            Path(profile.evaluator.command[0]),
        ),
    )
    store = RuntimeStore(profile.data_home)
    record = store.get_run(run_id)
    if record.state is not RunState.COMPLETED:
        raise ValueError("run must be completed before evaluation")
    links = store.run_artifacts(run_id)
    recorded_sources = json.loads(store.read_artifact(links["source_digests"]))
    if recorded_sources != dict(profile.source_digests):
        raise ValueError("source drift detected between run and evaluate")
    if record.profile_digest != _profile_digest(profile):
        raise ValueError("profile drift detected between run and evaluate")
    rollout = _rollout(store.read_artifact(links["rollout"]))
    case = profile.cases[record.case_id]
    references = tuple(profile.references[item] for item in case.hidden_reference_ids)
    try:
        evaluation = evaluate_rollout(
            case,
            rollout,
            references,
            CommandEvaluatorProvider(profile.evaluator, profile.rubric),
            evaluator_id=profile.evaluator.identity,
            rubric_version=profile.rubric.rubric_version,
        )
        monitor.verify()
    except (ProviderFailure, IntegrityViolation):
        store.transition_run(run_id, RunState.FAILED)
        raise
    artifact = store.put_artifact("evaluation", canonical_json(evaluation))
    store.link_artifact(run_id, "evaluation", artifact.digest)
    state = store.transition_run(run_id, RunState.EVALUATED).state.value
    return {
        "run_id": run_id,
        "state": state,
        "evaluation_digest": evaluation.digest,
        "dimensions": {item.name: item.score for item in evaluation.dimensions},
        "failure_kinds": [item.kind.value for item in evaluation.failure_signals],
        "aggregate_score": evaluation.aggregate_score,
    }


def run_status(profile_path: Path, run_id: str, *, checkout: Path) -> dict[str, Any]:
    profile = load_profile(profile_path, checkout=checkout)
    store = RuntimeStore(profile.data_home)
    record = store.get_run(run_id)
    return {
        "run_id": record.run_id,
        "case_id": record.case_id,
        "policy_digest": record.policy_digest,
        "state": record.state.value,
        "artifacts": store.run_artifacts(run_id),
    }
