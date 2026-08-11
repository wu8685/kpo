from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any

from kpo.campaign_cache import BudgetExhausted, CampaignCallExecutor
from kpo.campaign_profile import (
    CampaignProfile,
    campaign_integrity_monitor,
    campaign_profile_snapshot,
    load_campaign_profile,
    resolve_candidate_destination,
    validate_campaign_id,
)
from kpo.dataset import DatasetManager
from kpo.diagnosis import diagnose
from kpo.digest import canonical_digest
from kpo.external_promotion import (
    persist_promotion_bundle,
    propose_manifest_update,
    resume_campaign_promotion_preview,
)
from kpo.evaluator_agreement import (
    AgreementCollector,
    AgreementCheckpointError,
    AgreementPersistenceError,
    cleanup_agreement_checkpoint,
    evaluation_event_id,
    load_agreement_checkpoint,
    persist_agreement_checkpoint,
    persist_agreement_report,
)
from kpo.models import (
    Diagnosis,
    Evaluation,
    Partition,
    PolicySnapshot,
    ReferenceArtifact,
    RejectedCandidateSummary,
    Rollout,
    TaskCase,
)
from kpo.integrity import IntegrityMonitor, IntegrityViolation
from kpo.pipeline import evaluate_rollout, run_actor, run_proposer
from kpo.promotion import PromotionManager
from kpo.provider import (
    CommandActorProvider,
    CommandEvaluatorProvider,
    CommandProposerProvider,
    ProviderFailure,
)
from kpo.regression import apply_candidate
from kpo.vector_regression import VectorRegressionReport, run_vector_regression


class CampaignInterrupted(RuntimeError):
    def __init__(self, campaign_id: str) -> None:
        self.campaign_id = campaign_id
        super().__init__(f"campaign interrupted: {campaign_id}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def initialize_campaign_dataset(
    profile_path: Path,
    *,
    checkout: Path,
    approval_digest: str | None = None,
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    manager = DatasetManager(profile.base.data_home)
    preview = manager.preview_initialize(profile.dataset)
    if approval_digest is None:
        return {
            "state": "preview",
            "dataset_digest": preview.dataset_digest,
            "holdout_digest": preview.holdout_digest,
            "approval_digest": preview.approval_digest,
        }
    result = manager.apply_initialize(preview, approval_digest=approval_digest)
    return {
        "state": result.state,
        "holdout_digest": result.holdout_digest,
        "journal_path": str(result.journal_path),
    }


def _references(profile: CampaignProfile, case: TaskCase) -> tuple[ReferenceArtifact, ...]:
    return tuple(profile.base.references[item] for item in case.hidden_reference_ids)


def _call_context(
    profile: CampaignProfile,
    case: TaskCase,
    policy: PolicySnapshot,
    references: tuple[ReferenceArtifact, ...],
    *,
    role: str,
    random_seed: int = 0,
) -> dict[str, Any]:
    context = {
        "case_digest": canonical_digest(case),
        "policy_digest": policy.digest,
        "decoding_configuration_digest": canonical_digest({}),
        "random_seed": random_seed,
    }
    if role in {"evaluator", "proposer"}:
        context.update(
            reference_set_digest=canonical_digest(references),
            rubric_digest=canonical_digest(profile.base.rubric),
        )
    return context


def _evaluate_case(
    profile: CampaignProfile,
    case: TaskCase,
    policy: PolicySnapshot,
    executor: CampaignCallExecutor,
    *,
    partition: Partition,
    collector: AgreementCollector | None = None,
) -> tuple[Rollout, Evaluation]:
    references = _references(profile, case)
    actor_context = _call_context(
        profile, case, policy, references, role="actor"
    )
    evaluator_context = _call_context(
        profile, case, policy, references, role="evaluator"
    )
    rollout = run_actor(
        case,
        policy,
        CommandActorProvider(
            profile.base.actor, executor=executor, context=actor_context
        ),
        model_id=profile.base.actor.identity,
    )
    evaluation = evaluate_rollout(
        case,
        rollout,
        references,
        CommandEvaluatorProvider(
            profile.base.evaluator,
            profile.base.rubric,
            executor=executor,
            context=evaluator_context,
        ),
        evaluator_id=profile.base.evaluator.identity,
        rubric_version=profile.base.rubric.rubric_version,
    )
    if collector is not None:
        event_id = evaluation_event_id(
            case_digest=canonical_digest(case),
            rollout_digest=rollout.digest,
            policy_digest=policy.digest,
            partition=partition.value,
            rubric_digest=canonical_digest(profile.base.rubric),
        )
        collector.record_primary(
            event_id,
            partition.value,
            {item.name: item.score for item in evaluation.dimensions},
        )
        observers = profile.base.observer_evaluators
        for index, observer in enumerate(observers):
            try:
                observed = evaluate_rollout(
                    case,
                    rollout,
                    references,
                    CommandEvaluatorProvider(
                        observer.config,
                        profile.base.rubric,
                        executor=executor,
                        context=evaluator_context,
                    ),
                    evaluator_id=observer.config.identity,
                    rubric_version=profile.base.rubric.rubric_version,
                )
            except BudgetExhausted:
                for remaining in observers[index:]:
                    collector.record_unavailable(
                        event_id,
                        remaining.name,
                        "not_attempted_budget_exhausted",
                    )
                raise
            except ProviderFailure as error:
                collector.record_unavailable(event_id, observer.name, error.kind.value)
                continue
            collector.record_observer(
                event_id,
                observer.name,
                {item.name: item.score for item in observed.dimensions},
            )
    return rollout, evaluation


def _agreement_binding(
    profile: CampaignProfile,
    collector: AgreementCollector | None,
    campaign_id: str,
    snapshot_digest: str,
    *,
    collection_state: str,
) -> dict[str, Any]:
    if collector is None:
        return {}
    report = collector.report(
        campaign_id=campaign_id,
        profile_id=profile.base.profile_id,
        profile_snapshot_digest=snapshot_digest,
        collection_state=collection_state,
    )
    return persist_agreement_report(
        profile.base.data_home,
        campaign_id,
        report,
    )


def _persist_agreement_checkpoint(
    profile: CampaignProfile,
    collector: AgreementCollector | None,
    campaign_id: str,
    snapshot_digest: str,
) -> None:
    if collector is not None:
        persist_agreement_checkpoint(
            profile.base.data_home,
            campaign_id,
            collector,
            profile_snapshot_digest=snapshot_digest,
        )


def _state_path(profile: CampaignProfile, campaign_id: str) -> Path:
    return profile.base.data_home / "campaigns" / campaign_id / "campaign.json"


def _rejections(raw: list[dict[str, Any]]) -> tuple[RejectedCandidateSummary, ...]:
    from kpo.models import MutationKind

    return tuple(
        RejectedCandidateSummary(
            item["candidate_digest"],
            MutationKind(item["mutation"]),
            item["artifact_id"],
            MappingProxyType(dict(item["validation_deltas"])),
            MappingProxyType(dict(item["regression_deltas"])),
            tuple(item["failed_gates"]),
        )
        for item in raw
    )


def _rejection_json(item: RejectedCandidateSummary) -> dict[str, Any]:
    return {
        "candidate_digest": item.candidate_digest,
        "mutation": item.mutation.value,
        "artifact_id": item.artifact_id,
        "validation_deltas": dict(item.validation_deltas),
        "regression_deltas": dict(item.regression_deltas),
        "failed_gates": list(item.failed_gates),
    }


def _failed_non_holdout_gates(
    report: VectorRegressionReport, profile: CampaignProfile
) -> tuple[str, ...]:
    failed: list[str] = []
    for partition, thresholds in (
        (Partition.VALIDATION, profile.gates.validation),
        (Partition.REGRESSION, profile.gates.regression),
    ):
        for dimension, threshold in thresholds.items():
            if report.partition_gains[partition][dimension] < threshold:
                failed.append(f"{partition.value}.{dimension}")
    for dimension, threshold in profile.gates.ablation.items():
        if report.ablation_gains[dimension] < threshold:
            failed.append(f"ablation.{dimension}")
    return tuple(failed)


def run_campaign(
    profile_path: Path,
    *,
    checkout: Path,
    campaign_id: str | None = None,
    resume: bool = False,
    fault_after_train: bool = False,
    fault_after_bundle: bool = False,
    fault_after_rejection: bool = False,
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    dataset_manager = DatasetManager(profile.base.data_home)
    dataset_manager.verify_holdout(profile.dataset)
    if (
        profile.frozen_holdout_digest is not None
        and profile.frozen_holdout_digest != f"sha256:{profile.dataset.holdout_digest}"
    ):
        raise ValueError("profile frozen_holdout_digest does not match dataset")
    campaign_id = validate_campaign_id(campaign_id or uuid.uuid4().hex)
    path = _state_path(profile, campaign_id)
    snapshot_digest = campaign_profile_snapshot(profile)
    monitor = campaign_integrity_monitor(profile)
    target_digest = monitor.target_digest

    existing_campaign = path.exists()
    if existing_campaign:
        state = json.loads(path.read_text(encoding="utf-8"))
        bundle_directory = (
            profile.base.data_home
            / "promotions"
            / "previews"
            / campaign_id
        )
        if (
            resume
            and state["state"] in {"running", "failed"}
            and bundle_directory.exists()
        ):
            state.update(
                resume_campaign_promotion_preview(profile, campaign_id, state)
            )
            _atomic_json(path, state)
            cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
            return state
        if not resume or state["state"] != "failed":
            raise ValueError("only failed campaigns may be resumed")
        if state["profile_snapshot_digest"] != snapshot_digest:
            raise ValueError("campaign profile or source drift detected")
        if state["target_digest"] != target_digest:
            raise ValueError("campaign promotion target drift detected")
    else:
        if resume:
            raise KeyError(f"unknown campaign: {campaign_id}")
        state = {
            "campaign_id": campaign_id,
            "state": "running",
            "iteration": 0,
            "profile_snapshot_digest": snapshot_digest,
            "parent_policy_digest": profile.base.policy.digest,
            "target_digest": target_digest,
            "rejected_candidates": [],
            "sandbox": profile.sandbox_type,
        }
        _atomic_json(path, state)

    executor = CampaignCallExecutor(
        profile.base.data_home,
        campaign_id,
        max_calls=profile.max_provider_calls,
        integrity_check=monitor.verify,
    )
    observer_coordinates = tuple(
        (observer.name, observer.config.identity)
        for observer in profile.base.observer_evaluators
    )
    collector: AgreementCollector | None = None
    if profile.base.observer_evaluators:
        try:
            if existing_campaign:
                collector = load_agreement_checkpoint(
                    profile.base.data_home,
                    campaign_id,
                    primary_evaluator_id=profile.base.evaluator.identity,
                    observers=observer_coordinates,
                    rubric_version=profile.base.rubric.rubric_version,
                    dimensions=profile.base.rubric.dimension_names,
                    profile_snapshot_digest=snapshot_digest,
                )
            else:
                collector = AgreementCollector(
                    primary_evaluator_id=profile.base.evaluator.identity,
                    observers=observer_coordinates,
                    rubric_version=profile.base.rubric.rubric_version,
                    dimensions=profile.base.rubric.dimension_names,
                )
                _persist_agreement_checkpoint(
                    profile, collector, campaign_id, snapshot_digest
                )
        except AgreementCheckpointError:
            state.update(
                state="failed",
                failure_kind="evaluator_agreement_checkpoint",
            )
            _atomic_json(path, state)
            return state
    rejected = list(_rejections(state.get("rejected_candidates", [])))
    seen = {item.candidate_digest for item in rejected}

    try:
        start_iteration = int(state.get("iteration", 0))
        for iteration in range(start_iteration, profile.max_iterations):
            state.update(state="running", iteration=iteration)
            _atomic_json(path, state)
            patchable: list[tuple[TaskCase, Rollout, Evaluation, Diagnosis]] = []
            train_entries = profile.dataset.by_partition[Partition.TRAIN]
            for entry in train_entries:
                case = profile.base.cases[entry.case_id]
                rollout, evaluation = _evaluate_case(
                    profile,
                    case,
                    profile.base.policy,
                    executor,
                    partition=Partition.TRAIN,
                    collector=collector,
                )
                _persist_agreement_checkpoint(
                    profile, collector, campaign_id, snapshot_digest
                )
                if not evaluation.failure_signals:
                    continue
                diagnosis = diagnose(evaluation)
                if diagnosis.patchable:
                    patchable.append((case, rollout, evaluation, diagnosis))
            if fault_after_train:
                state.update(state="failed", call_count=executor.call_count)
                _atomic_json(path, state)
                raise CampaignInterrupted(campaign_id)
            if not patchable:
                binding = _agreement_binding(
                    profile,
                    collector,
                    campaign_id,
                    snapshot_digest,
                    collection_state="complete",
                )
                state.update(
                    state="no_patchable_diagnosis",
                    call_count=executor.call_count,
                    rejected_candidate_count=len(rejected),
                    **binding,
                )
                _atomic_json(path, state)
                cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
                return state

            case, rollout, evaluation, diagnosis = patchable[0]
            proposal_context = _call_context(
                profile,
                case,
                profile.base.policy,
                _references(profile, case),
                role="proposer",
            ) | {"rejected_candidates_digest": canonical_digest(rejected)}
            proposed = run_proposer(
                diagnosis,
                profile.base.policy,
                rollout,
                evaluation,
                _references(profile, case),
                CommandProposerProvider(
                    profile.proposer, executor=executor, context=proposal_context
                ),
                rejected_candidates=tuple(rejected),
            )
            candidate = proposed.candidate
            if candidate.digest in seen:
                binding = _agreement_binding(
                    profile,
                    collector,
                    campaign_id,
                    snapshot_digest,
                    collection_state="complete",
                )
                state.update(
                    state="no_patchable_diagnosis",
                    call_count=executor.call_count,
                    rejected_candidate_count=len(rejected),
                    **binding,
                )
                _atomic_json(path, state)
                cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
                return state
            seen.add(candidate.digest)

            def scorer(case_id: str, policy: PolicySnapshot) -> dict[str, float]:
                partition = next(
                    entry.partition
                    for entry in profile.dataset.entries
                    if entry.case_id == case_id
                )
                _, scored = _evaluate_case(
                    profile,
                    profile.base.cases[case_id],
                    policy,
                    executor,
                    partition=partition,
                    collector=collector,
                )
                _persist_agreement_checkpoint(
                    profile, collector, campaign_id, snapshot_digest
                )
                return {item.name: item.score for item in scored.dimensions}

            report = run_vector_regression(
                profile.base.policy,
                candidate,
                profile.dataset,
                scorer,
                profile.gates,
            )
            if report.passed:
                binding = _agreement_binding(
                    profile,
                    collector,
                    campaign_id,
                    snapshot_digest,
                    collection_state="complete",
                )
                destination = resolve_candidate_destination(profile, candidate)
                monitor.verify()
                preview = PromotionManager(profile.base.data_home / "promotion").preview(
                    candidate,
                    report,
                    target_root=profile.target_root,
                    relative_path=destination.target_relative_path,
                    allowlist=profile.allowlist,
                )
                manifest_preview = propose_manifest_update(
                    profile, candidate, destination
                )
                bundle_path = persist_promotion_bundle(
                    profile,
                    campaign_id=campaign_id,
                    profile_snapshot_digest=snapshot_digest,
                    target_digest=target_digest,
                    candidate=candidate,
                    report=report,
                    destination=destination,
                    content_preview=preview,
                    manifest_preview=manifest_preview,
                    evaluator_agreement_path=(
                        profile.base.data_home
                        / str(binding["evaluator_agreement_path"])
                        if binding
                        else None
                    ),
                    evaluator_agreement_digest=(
                        str(binding["evaluator_agreement_digest"])
                        if binding
                        else None
                    ),
                )
                if fault_after_bundle:
                    state.update(
                        state="failed",
                        call_count=executor.call_count,
                        rejected_candidate_count=len(rejected),
                    )
                    _atomic_json(path, state)
                    raise CampaignInterrupted(campaign_id)
                state.update(
                    state="promotion_previewed",
                    promotion_state="previewed",
                    iteration=iteration + 1,
                    call_count=executor.call_count,
                    candidate_digest=candidate.digest,
                    candidate_policy_digest=report.candidate_policy_digest,
                    report_digest=report.digest,
                    report_passed=True,
                    promotion_diff=preview.diff,
                    promotion_diff_digest=preview.diff_digest,
                    manifest_diff=manifest_preview.diff,
                    manifest_diff_digest=manifest_preview.diff_digest,
                    bundle_path=bundle_path.relative_to(
                        profile.base.data_home
                    ).as_posix(),
                    rejected_candidate_count=len(rejected),
                    **binding,
                )
                _atomic_json(path, state)
                cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
                return state

            rejection = RejectedCandidateSummary(
                candidate.digest,
                candidate.mutation,
                candidate.artifact_id,
                report.partition_gains[Partition.VALIDATION],
                report.partition_gains[Partition.REGRESSION],
                _failed_non_holdout_gates(report, profile),
            )
            rejected.append(rejection)
            state["rejected_candidates"] = [
                _rejection_json(item) for item in rejected
            ]
            state["iteration"] = iteration + 1
            state["call_count"] = executor.call_count
            _atomic_json(path, state)
            if fault_after_rejection:
                state["state"] = "failed"
                _atomic_json(path, state)
                raise CampaignInterrupted(campaign_id)

        state.update(
            state="budget_exhausted",
            call_count=executor.call_count,
            rejected_candidate_count=len(rejected),
            **_agreement_binding(
                profile,
                collector,
                campaign_id,
                snapshot_digest,
                collection_state="budget_exhausted",
            ),
        )
        _atomic_json(path, state)
        cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
        return state
    except BudgetExhausted:
        try:
            binding = _agreement_binding(
                profile,
                collector,
                campaign_id,
                snapshot_digest,
                collection_state="budget_exhausted",
            )
        except AgreementPersistenceError:
            state.update(
                state="failed",
                call_count=executor.call_count,
                failure_kind="evaluator_agreement_persistence",
            )
            _atomic_json(path, state)
            return state
        state.update(
            state="budget_exhausted",
            call_count=executor.call_count,
            rejected_candidate_count=len(rejected),
            **binding,
        )
        _atomic_json(path, state)
        cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
        return state
    except CampaignInterrupted:
        raise
    except IntegrityViolation as error:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind=error.kind,
            changed_paths=list(error.changed_paths),
        )
        _atomic_json(path, state)
        return state
    except ProviderFailure as error:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind=error.kind.value,
        )
        _atomic_json(path, state)
        return state
    except AgreementPersistenceError:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind="evaluator_agreement_persistence",
        )
        _atomic_json(path, state)
        return state
    except AgreementCheckpointError:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind="evaluator_agreement_checkpoint",
        )
        _atomic_json(path, state)
        return state
    except BaseException:
        state.update(state="failed", call_count=executor.call_count)
        _atomic_json(path, state)
        raise


def campaign_status(
    profile_path: Path, campaign_id: str, *, checkout: Path
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    path = _state_path(profile, campaign_id)
    if not path.exists():
        raise KeyError(f"unknown campaign: {campaign_id}")
    return json.loads(path.read_text(encoding="utf-8"))
