from dataclasses import FrozenInstanceError

import pytest

from kpo.models import PolicyArtifact, PolicySnapshot, TaskCase


def test_task_case_is_immutable_and_hides_references() -> None:
    case = TaskCase(
        case_id="case-1",
        prompt="Choose a route.",
        visible_context=("map",),
        hidden_reference_ids=("reference-1",),
        tags=("synthetic",),
    )

    with pytest.raises(FrozenInstanceError):
        case.prompt = "changed"  # type: ignore[misc]

    assert "reference-1" not in case.actor_payload()["visible_context"]
    assert "hidden_reference_ids" not in case.actor_payload()


def test_task_case_rejects_reference_leakage() -> None:
    with pytest.raises(ValueError, match="hidden reference"):
        TaskCase(
            case_id="case-1",
            prompt="Choose a route.",
            visible_context=("reference-1",),
            hidden_reference_ids=("reference-1",),
        )


def test_policy_snapshot_digest_is_order_independent() -> None:
    first = PolicyArtifact("rule-a", "Prefer verified paths.")
    second = PolicyArtifact("rule-b", "State uncertainty.")

    forward = PolicySnapshot.from_artifacts((first, second))
    reverse = PolicySnapshot.from_artifacts((second, first))

    assert forward.digest == reverse.digest
    assert forward.artifact_ids == ("rule-a", "rule-b")


def test_policy_snapshot_changes_when_content_changes() -> None:
    original = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-a", "Prefer verified paths."),)
    )
    changed = PolicySnapshot.from_artifacts(
        (PolicyArtifact("rule-a", "Prefer the shortest path."),)
    )

    assert original.digest != changed.digest
