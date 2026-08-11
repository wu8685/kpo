from pathlib import Path
from typing import get_type_hints

import pytest

from kpo.models import (
    CandidatePatch,
    MutationKind,
    Partition,
    PolicyArtifact,
    PolicySnapshot,
    RegressionCase,
)
from kpo.promotion import PromotionInterrupted, PromotionManager, PromotionReport
from kpo.regression import run_regression


def test_preview_uses_the_minimal_promotion_report_protocol() -> None:
    assert get_type_hints(PromotionManager.preview)["report"] is PromotionReport


def approved_candidate() -> tuple[CandidatePatch, object]:
    parent = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "Use available evidence."),)
    )
    candidate = CandidatePatch(
        parent_policy_digest=parent.digest,
        diagnosis_digest="diagnosis-1",
        mutation=MutationKind.ADD,
        artifact_id="rule-2",
        content="When evidence conflicts, state the conflict and abstain.\n",
        provenance=("evaluation-1", "reference-1"),
        digest="candidate-1",
    )
    cases = (
        RegressionCase("validation-1", Partition.VALIDATION),
        RegressionCase("holdout-1", Partition.HOLDOUT),
        RegressionCase("regression-1", Partition.REGRESSION),
    )

    def scorer(case: RegressionCase, policy: PolicySnapshot) -> float:
        del case
        contents = "\n".join(artifact.content for artifact in policy.artifacts)
        return 1.0 if "abstain" in contents else 0.2

    report = run_regression(parent, candidate, cases, scorer)
    assert report.passed
    return candidate, report


def test_preview_is_non_mutating_and_requires_bound_approval(tmp_path: Path) -> None:
    candidate, report = approved_candidate()
    target_root = tmp_path / "knowledge"
    target_root.mkdir()
    runtime_home = tmp_path / "runtime"
    manager = PromotionManager(runtime_home)

    preview = manager.preview(
        candidate,
        report,
        target_root=target_root,
        relative_path="policies/rule-2.md",
        allowlist=("policies",),
    )

    destination = target_root / "policies" / "rule-2.md"
    assert destination.exists() is False
    assert "+When evidence conflicts" in preview.diff

    with pytest.raises(ValueError, match="approval digest"):
        manager.apply(preview, approval_digest="wrong")
    assert destination.exists() is False

    result = manager.apply(preview, approval_digest=preview.diff_digest)

    assert result.state == "committed"
    assert destination.read_text(encoding="utf-8") == candidate.content
    assert result.journal_path.exists()


def test_preview_rejects_path_outside_allowlist(tmp_path: Path) -> None:
    candidate, report = approved_candidate()
    target_root = tmp_path / "knowledge"
    target_root.mkdir()
    manager = PromotionManager(tmp_path / "runtime")

    with pytest.raises(ValueError, match="allowlist"):
        manager.preview(
            candidate,
            report,
            target_root=target_root,
            relative_path="../outside.md",
            allowlist=("policies",),
        )


def test_stale_preview_is_rejected(tmp_path: Path) -> None:
    candidate, report = approved_candidate()
    target_root = tmp_path / "knowledge"
    target_root.mkdir()
    manager = PromotionManager(tmp_path / "runtime")
    preview = manager.preview(
        candidate,
        report,
        target_root=target_root,
        relative_path="policies/rule-2.md",
        allowlist=("policies",),
    )
    destination = target_root / "policies" / "rule-2.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("concurrent change\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stale promotion preview"):
        manager.apply(preview, approval_digest=preview.diff_digest)


def test_interrupted_promotion_can_restore_original_state(tmp_path: Path) -> None:
    candidate, report = approved_candidate()
    target_root = tmp_path / "knowledge"
    target_root.mkdir()
    manager = PromotionManager(tmp_path / "runtime")
    preview = manager.preview(
        candidate,
        report,
        target_root=target_root,
        relative_path="policies/rule-2.md",
        allowlist=("policies",),
    )
    destination = target_root / "policies" / "rule-2.md"

    with pytest.raises(PromotionInterrupted):
        manager.apply(
            preview,
            approval_digest=preview.diff_digest,
            fault_after_replace=True,
        )

    assert destination.exists()
    recovery = manager.recover(preview.preview_digest)

    assert recovery.state == "rolled_back"
    assert destination.exists() is False


def test_edit_promotion_preserves_context_and_exact_newlines(tmp_path: Path) -> None:
    parent = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-1", "First line.\nSecond line.\n"),)
    )
    candidate = CandidatePatch(
        parent_policy_digest=parent.digest,
        diagnosis_digest="diagnosis-2",
        mutation=MutationKind.EDIT,
        artifact_id="rule-1",
        content="First line.\nRevised second line.\nThird line.\n",
        provenance=("evaluation-2", "reference-2"),
        digest="candidate-2",
    )
    cases = (
        RegressionCase("validation-1", Partition.VALIDATION),
        RegressionCase("holdout-1", Partition.HOLDOUT),
    )

    def scorer(case: RegressionCase, policy: PolicySnapshot) -> float:
        del case
        contents = "\n".join(artifact.content for artifact in policy.artifacts)
        return 1.0 if "Revised second line" in contents else 0.2

    report = run_regression(parent, candidate, cases, scorer)
    target_root = tmp_path / "knowledge"
    destination = target_root / "policies" / "rule-1.md"
    destination.parent.mkdir(parents=True)
    destination.write_text(parent.artifacts[0].content, encoding="utf-8")
    manager = PromotionManager(tmp_path / "runtime")
    preview = manager.preview(
        candidate,
        report,
        target_root=target_root,
        relative_path="policies/rule-1.md",
        allowlist=("policies",),
    )

    manager.apply(preview, approval_digest=preview.diff_digest)

    assert destination.read_text(encoding="utf-8") == candidate.content
