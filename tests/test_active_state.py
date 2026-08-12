from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.active_state import ActiveStateInterrupted, ActiveStateManager


def test_active_pointer_requires_exact_approval_and_advances_next_version(
    tmp_path: Path,
) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_policy("policy-v1")
    preview = manager.preview_policy_advance(
        parent_digest="policy-v1",
        candidate_digest="policy-v2",
        evidence_digest="evidence-v2",
        review_digest="review-v2",
        candidate_owner_identity="proposer-1",
        reviewer_identity="reviewer-1",
    )

    with pytest.raises(ValueError, match="approval"):
        manager.apply_policy_advance(
            preview, approval_digest="wrong", approver_identity="owner-1"
        )

    manager.apply_policy_advance(
        preview,
        approval_digest=preview.approval_digest,
        approver_identity="owner-1",
    )

    assert manager.active_policy_digest() == "policy-v2"
    journal = [
        json.loads(line)
        for line in (tmp_path / "promotion-journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [entry["state"] for entry in journal] == [
        "committed",
        "prepared",
        "committed",
    ]
    assert journal[-1]["previous_digest"] == "policy-v1"


def test_confirmed_regression_rolls_back_without_rewriting_history(
    tmp_path: Path,
) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_component("actor", "actor-v1")
    preview = manager.preview_component_advance(
        target="actor",
        parent_digest="actor-v1",
        candidate_digest="actor-v2",
        evidence_digest="evidence-v2",
        review_digest="review-v2",
        candidate_owner_identity="proposer-1",
        reviewer_identity="reviewer-1",
    )
    manager.apply_component_advance(
        preview,
        approval_digest=preview.approval_digest,
        approver_identity="owner-1",
    )

    manager.rollback_component(
        target="actor",
        failed_digest="actor-v2",
        ancestor_digest="actor-v1",
        regression_evidence_digest="regression-1",
    )

    assert manager.active_component_digest("actor") == "actor-v1"
    entries = (tmp_path / "promotion-journal.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(entries) == 4
    assert json.loads(entries[-1])["action"] == "rollback"


def test_prepared_pointer_interruption_recovers_snapshot(tmp_path: Path) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_policy("policy-v1")
    preview = manager.preview_policy_advance(
        parent_digest="policy-v1",
        candidate_digest="policy-v2",
        evidence_digest="evidence-v2",
        review_digest="review-v2",
        candidate_owner_identity="proposer-1",
        reviewer_identity="reviewer-1",
    )

    with pytest.raises(ActiveStateInterrupted):
        manager.apply_policy_advance(
            preview,
            approval_digest=preview.approval_digest,
            approver_identity="owner-1",
            fault_after_pointer=True,
        )

    assert manager.active_policy_digest() == "policy-v2"
    assert manager.recover_pending() == "rolled_back"
    assert manager.active_policy_digest() == "policy-v1"
    assert json.loads(
        (tmp_path / "promotion-journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )["state"] == "rolled_back"


def test_recovery_after_committed_before_pending_cleanup_keeps_candidate(
    tmp_path: Path,
) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_policy("policy-v1")
    preview = manager.preview_policy_advance(
        parent_digest="policy-v1",
        candidate_digest="policy-v2",
        evidence_digest="evidence-v2",
        review_digest="review-v2",
        candidate_owner_identity="proposer-1",
        reviewer_identity="reviewer-1",
    )
    with pytest.raises(ActiveStateInterrupted):
        manager.apply_policy_advance(
            preview,
            approval_digest=preview.approval_digest,
            approver_identity="owner-1",
            fault_after_commit=True,
        )

    assert manager.recover_pending() == "committed"
    assert manager.active_policy_digest() == "policy-v2"


def test_load_rejects_index_that_disagrees_with_committed_journal(
    tmp_path: Path,
) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_policy("policy-v1")
    (tmp_path / "active-policy.json").write_text(
        '{"digest":"tampered"}\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="journal"):
        ActiveStateManager(tmp_path)


def test_policy_rollback_and_approval_identities(tmp_path: Path) -> None:
    manager = ActiveStateManager(tmp_path)
    manager.initialize_policy("policy-v1")
    with pytest.raises(ValueError, match="reviewer"):
        manager.preview_policy_advance(
            parent_digest="policy-v1",
            candidate_digest="policy-v2",
            evidence_digest="evidence-v2",
            review_digest="review-v2",
            candidate_owner_identity="same-identity",
            reviewer_identity="same-identity",
        )
    preview = manager.preview_policy_advance(
        parent_digest="policy-v1",
        candidate_digest="policy-v2",
        evidence_digest="evidence-v2",
        review_digest="review-v2",
        candidate_owner_identity="proposer-1",
        reviewer_identity="reviewer-1",
    )
    with pytest.raises(ValueError, match="approve itself"):
        manager.apply_policy_advance(
            preview,
            approval_digest=preview.approval_digest,
            approver_identity="proposer-1",
        )
    manager.apply_policy_advance(
        preview,
        approval_digest=preview.approval_digest,
        approver_identity="owner-1",
    )

    manager.rollback_policy(
        failed_digest="policy-v2",
        ancestor_digest="policy-v1",
        regression_evidence_digest="regression-1",
    )

    assert manager.active_policy_digest() == "policy-v1"
