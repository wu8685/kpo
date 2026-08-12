from __future__ import annotations

import hashlib
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
from kpo.campaign_series import (
    SeriesEntry,
    append_series_entry,
    effective_series_entries,
    expected_series_parent,
    load_series_state,
    persist_campaign_series_evidence,
    reconcile_series,
    series_mutation,
)
from kpo.dataset import DatasetManager
from kpo.diagnosis import diagnose
from kpo.digest import canonical_digest
from kpo.evaluator_agreement import (
    AgreementCheckpointError,
    AgreementCollector,
    AgreementPersistenceError,
    _validate_report_schema,
    cleanup_agreement_checkpoint,
    evaluation_event_id,
    load_agreement_checkpoint,
    persist_agreement_checkpoint,
    persist_agreement_report,
)
from kpo.evaluator_audit_runtime import (
    EvaluatorAuditBudgetError,
    EvaluatorCalibrationCheckpointError,
    cleanup_calibration_checkpoint,
    run_evaluator_calibration,
)
from kpo.evaluator_calibration_report import EvaluatorCalibrationPersistenceError
from kpo.external_promotion import (
    persist_promotion_bundle,
    propose_manifest_update,
    resume_campaign_promotion_preview,
)
from kpo.integrity import IntegrityViolation
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
from kpo.pipeline import evaluate_rollout, run_actor, run_proposer
from kpo.promotion import PromotionManager
from kpo.provider import (
    CommandActorProvider,
    CommandEvaluatorProvider,
    CommandProposerProvider,
    ProviderFailure,
)
from kpo.reasoning_differential import (
    DifferentialDiagnosis,
    load_differential_summary,
    persist_differential_iteration,
    persist_differential_summary,
    run_reasoning_differential,
)
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


def _persist_series_terminal(
    profile: CampaignProfile,
    state: dict[str, Any],
    path: Path,
    *,
    report: VectorRegressionReport | None,
) -> dict[str, Any]:
    if profile.differential_analyzer is not None:
        analyzer = profile.differential_analyzer
        state.update(
            **persist_differential_summary(
                profile.base.data_home,
                state["campaign_id"],
                profile_snapshot_digest=state["profile_snapshot_digest"],
                extractor=analyzer.extractor,
                differ=analyzer.differ,
                diagnostician=analyzer.diagnostician,
            )
        )
    series_id = state.get("series_id")
    if series_id is None:
        _atomic_json(path, state)
        return state
    report_bindings = dict(state)
    if (
        state.get("evaluator_agreement_digest") is not None
        and state.get("evaluator_agreement_report_digest") is None
    ):
        agreement_path = (
            profile.base.data_home
            / "campaigns"
            / state["campaign_id"]
            / "evaluator-agreement.json"
        )
        agreement_payload = agreement_path.read_bytes()
        agreement_report = _validate_report_schema(
            json.loads(agreement_payload.decode("utf-8"))
        )
        if (
            hashlib.sha256(agreement_payload).hexdigest()
            != state["evaluator_agreement_digest"]
            or agreement_report["campaign_id"] != state["campaign_id"]
            or agreement_report["profile_snapshot_digest"]
            != state["profile_snapshot_digest"]
        ):
            raise ValueError("series agreement report binding mismatch")
        report_bindings["evaluator_agreement_report_digest"] = (
            agreement_report["report_digest"]
        )
    binding = persist_campaign_series_evidence(
        profile,
        series_id=series_id,
        series_index=state["series_index"],
        campaign_id=state["campaign_id"],
        revision=state["series_evidence_revision"],
        terminal_state=state["state"],
        profile_snapshot_digest=state["profile_snapshot_digest"],
        iteration_count=state.get("iteration", 0),
        call_count=state.get("call_count", 0),
        rejected_candidate_count=state.get("rejected_candidate_count", 0),
        report=report,
        report_bindings=report_bindings,
    )
    state.update(
        series_evidence_path=binding["path"],
        series_evidence_digest=binding["byte_digest"],
        series_evidence_record_digest=binding["record_digest"],
    )
    _atomic_json(path, state)
    append_series_entry(
        profile,
        series_id,
        SeriesEntry(
            index=state["series_index"],
            campaign_id=state["campaign_id"],
            revision=state["series_evidence_revision"],
            campaign_state=state["state"],
            parent_policy_digest=state["parent_policy_digest"],
            candidate_policy_digest=state.get("candidate_policy_digest"),
            dataset_digest=profile.dataset.digest,
            profile_snapshot_digest=state["profile_snapshot_digest"],
            evidence_path=binding["path"],
            evidence_digest=binding["byte_digest"],
            evidence_record_digest=binding["record_digest"],
        ),
    )
    return state


def _series_report_record(report: VectorRegressionReport) -> dict[str, Any]:
    return {
        "candidate_digest": report.candidate_digest,
        "parent_policy_digest": report.parent_policy_digest,
        "candidate_policy_digest": report.candidate_policy_digest,
        "partition_gains": {
            partition.value: dict(report.partition_gains[partition])
            for partition in (
                Partition.VALIDATION,
                Partition.HOLDOUT,
                Partition.REGRESSION,
            )
        },
        "ablation_gains": dict(report.ablation_gains),
        "passed": report.passed,
        "digest": report.digest,
    }


def _series_report_from_record(record: Any) -> VectorRegressionReport:
    if not isinstance(record, dict):
        raise ValueError("series resume report is missing")
    return VectorRegressionReport(
        candidate_digest=record["candidate_digest"],
        parent_policy_digest=record["parent_policy_digest"],
        candidate_policy_digest=record["candidate_policy_digest"],
        measurements=(),
        partition_gains=MappingProxyType(
            {
                partition: MappingProxyType(
                    dict(record["partition_gains"][partition.value])
                )
                for partition in (
                    Partition.VALIDATION,
                    Partition.HOLDOUT,
                    Partition.REGRESSION,
                )
            }
        ),
        ablation_gains=MappingProxyType(dict(record["ablation_gains"])),
        passed=record["passed"],
        digest=record["digest"],
    )


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
    series_id: str | None = None,
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

    series_index: int | None = None
    series_state: dict[str, Any] | None = None
    if series_id is not None:
        validate_campaign_id(series_id)
        if profile.series is None:
            raise ValueError("campaign profile has no series configuration")
        if resume:
            series_state = load_series_state(profile, series_id)
            if series_state["state"] != "active":
                raise ValueError(f"campaign series is {series_state['state']}")
            expected_parent = expected_series_parent(profile, series_state)
            if profile.base.policy.digest != expected_parent:
                raise ValueError("series policy lineage mismatch")

    existing_campaign = path.exists()
    if existing_campaign:
        state = json.loads(path.read_text(encoding="utf-8"))
        if series_id is not None:
            assert series_state is not None
            if (
                state.get("series_id") != series_id
                or state.get("series_contract_digest")
                != series_state["series_contract_digest"]
            ):
                raise ValueError("campaign series binding mismatch")
            attached = [
                entry
                for entry in series_state["entries"]
                if entry["campaign_id"] == campaign_id
            ]
            if attached:
                latest = attached[-1]
                if latest["campaign_state"] != "failed":
                    raise ValueError("campaign revision is not in failed state")
                if (
                    latest["index"] != state.get("series_index")
                    or latest["revision"]
                    != state.get("series_evidence_revision")
                ):
                    raise ValueError("campaign series revision binding mismatch")
                state["series_evidence_revision"] = latest["revision"] + 1
            elif state.get("series_evidence_revision") != 0:
                raise ValueError("campaign series revision binding mismatch")
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
            if series_id is None:
                _atomic_json(path, state)
            else:
                report = _series_report_from_record(
                    state.pop("series_pending_report", None)
                )
                if (
                    report.digest != state.get("report_digest")
                    or report.candidate_digest != state.get("candidate_digest")
                    or report.candidate_policy_digest
                    != state.get("candidate_policy_digest")
                    or report.parent_policy_digest
                    != state.get("parent_policy_digest")
                    or report.passed is not True
                ):
                    raise ValueError("series resume report binding mismatch")
                _persist_series_terminal(profile, state, path, report=report)
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
        if series_id is not None:
            with series_mutation(
                profile,
                series_id,
                operation="allocation",
                campaign_id=campaign_id,
            ) as update_lock:
                reconcile_series(profile, series_id, _locked=True)
                series_state = load_series_state(profile, series_id)
                if series_state["state"] != "active":
                    raise ValueError(f"campaign series is {series_state['state']}")
                expected_parent = expected_series_parent(profile, series_state)
                if profile.base.policy.digest != expected_parent:
                    raise ValueError("series policy lineage mismatch")
                campaign_count = len(effective_series_entries(series_state))
                if campaign_count >= series_state["max_campaigns"]:
                    raise ValueError("campaign series max_campaigns is exhausted")
                series_index = campaign_count
                state.update(
                    series_id=series_id,
                    series_index=series_index,
                    series_evidence_revision=0,
                    series_contract_digest=series_state[
                        "series_contract_digest"
                    ],
                )
                update_lock(
                    series_index=series_index,
                    initial_state_digest=canonical_digest(state),
                )
                _atomic_json(path, state)
        else:
            _atomic_json(path, state)

    if profile.evaluator_audit is not None:
        try:
            calibration_binding = run_evaluator_calibration(
                profile,
                campaign_id,
                profile_snapshot_digest=snapshot_digest,
                integrity_monitor=monitor,
            )
        except FileNotFoundError:
            state.update(
                state="failed",
                failure_kind="evaluator_calibration_approval",
            )
            _persist_series_terminal(profile, state, path, report=None)
            return state
        except (EvaluatorCalibrationCheckpointError, EvaluatorAuditBudgetError):
            state.update(
                state="failed",
                failure_kind="evaluator_calibration_checkpoint",
            )
            _persist_series_terminal(profile, state, path, report=None)
            return state
        except EvaluatorCalibrationPersistenceError:
            state.update(
                state="failed",
                failure_kind="evaluator_calibration_persistence",
            )
            _persist_series_terminal(profile, state, path, report=None)
            return state
        state.update(**calibration_binding)
        _atomic_json(path, state)
        cleanup_calibration_checkpoint(profile.base.data_home, campaign_id)

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
            _persist_series_terminal(profile, state, path, report=None)
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
                _persist_series_terminal(profile, state, path, report=None)
                cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
                return state

            case, rollout, evaluation, diagnosis = patchable[0]
            differential_diagnosis: DifferentialDiagnosis | None = None
            if profile.differential_analyzer is not None:
                references = _references(profile, case)
                analyzer = profile.differential_analyzer
                differential_context = _call_context(
                    profile,
                    case,
                    profile.base.policy,
                    references,
                    role="evaluator",
                )
                try:
                    differential_analysis = run_reasoning_differential(
                        partition=Partition.TRAIN,
                        rollout=rollout,
                        references=references,
                        evaluation=evaluation,
                        extractor=analyzer.extractor,
                        differ=analyzer.differ,
                        diagnostician=analyzer.diagnostician,
                        executor=executor,
                        context=differential_context,
                    )
                except BudgetExhausted:
                    persist_differential_iteration(
                        profile.base.data_home,
                        campaign_id,
                        iteration,
                        analysis=None,
                        failure_kind="budget_exhausted",
                    )
                    raise
                except ProviderFailure as error:
                    persist_differential_iteration(
                        profile.base.data_home,
                        campaign_id,
                        iteration,
                        analysis=None,
                        failure_kind=error.kind.value,
                    )
                else:
                    persist_differential_iteration(
                        profile.base.data_home,
                        campaign_id,
                        iteration,
                        analysis=differential_analysis,
                    )
                    differential_diagnosis = differential_analysis.diagnosis
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
                differential_diagnosis=differential_diagnosis,
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
                _persist_series_terminal(profile, state, path, report=None)
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
                if state.get("series_id") is not None:
                    state["series_pending_report"] = _series_report_record(report)
                    _atomic_json(path, state)
                binding = _agreement_binding(
                    profile,
                    collector,
                    campaign_id,
                    snapshot_digest,
                    collection_state="complete",
                )
                differential_binding: dict[str, Any] = {}
                if profile.differential_analyzer is not None:
                    analyzer = profile.differential_analyzer
                    differential_binding = persist_differential_summary(
                        profile.base.data_home,
                        campaign_id,
                        profile_snapshot_digest=snapshot_digest,
                        extractor=analyzer.extractor,
                        differ=analyzer.differ,
                        diagnostician=analyzer.diagnostician,
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
                    differential_analysis_path=(
                        profile.base.data_home
                        / str(differential_binding["differential_analysis_path"])
                        if differential_binding
                        else None
                    ),
                    differential_analysis_digest=(
                        str(differential_binding["differential_analysis_digest"])
                        if differential_binding
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
                    **differential_binding,
                )
                state.pop("series_pending_report", None)
                _persist_series_terminal(profile, state, path, report=report)
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
        _persist_series_terminal(profile, state, path, report=None)
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
            _persist_series_terminal(profile, state, path, report=None)
            return state
        state.update(
            state="budget_exhausted",
            call_count=executor.call_count,
            rejected_candidate_count=len(rejected),
            **binding,
        )
        _persist_series_terminal(profile, state, path, report=None)
        cleanup_agreement_checkpoint(profile.base.data_home, campaign_id)
        return state
    except CampaignInterrupted:
        revision = state.get("series_evidence_revision", 0)
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError("campaign series revision is invalid")
        suffix = "" if revision == 0 else f".{revision}"
        expected_evidence_path = (
            f"campaigns/{campaign_id}/series-evidence{suffix}.json"
        )
        if (
            state.get("series_id") is not None
            and state.get("series_evidence_path") != expected_evidence_path
        ):
            state.setdefault("call_count", executor.call_count)
            _persist_series_terminal(profile, state, path, report=None)
        raise
    except IntegrityViolation as error:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind=error.kind,
            changed_paths=list(error.changed_paths),
        )
        _persist_series_terminal(profile, state, path, report=None)
        return state
    except ProviderFailure as error:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind=error.kind.value,
        )
        _persist_series_terminal(profile, state, path, report=None)
        return state
    except AgreementPersistenceError:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind="evaluator_agreement_persistence",
        )
        _persist_series_terminal(profile, state, path, report=None)
        return state
    except AgreementCheckpointError:
        state.update(
            state="failed",
            call_count=executor.call_count,
            failure_kind="evaluator_agreement_checkpoint",
        )
        _persist_series_terminal(profile, state, path, report=None)
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
    state = json.loads(path.read_text(encoding="utf-8"))
    if profile.differential_analyzer is not None:
        summary = load_differential_summary(
            profile.base.data_home,
            campaign_id,
            profile_snapshot_digest=state["profile_snapshot_digest"],
            expected_byte_digest=state.get("differential_analysis_digest"),
        )
        if summary != state.get("differential_analysis_summary"):
            raise ValueError("campaign differential summary binding mismatch")
    return state
