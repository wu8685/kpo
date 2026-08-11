from pathlib import Path

import pytest

from kpo.runtime import RunState, RuntimeStore, resolve_data_home


def test_runtime_data_home_must_be_explicit_and_outside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(ValueError, match="required"):
        resolve_data_home(checkout, None)

    with pytest.raises(ValueError, match="outside"):
        resolve_data_home(checkout, checkout / ".kpo")

    external = tmp_path / "runtime"
    assert resolve_data_home(checkout, external) == external.resolve()


def test_artifacts_are_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime")

    first = store.put_artifact("rollout", "same content")
    second = store.put_artifact("rollout", "same content")

    assert first.digest == second.digest
    assert first.path == second.path
    assert first.path.read_text(encoding="utf-8") == "same content"


def test_run_state_machine_rejects_invalid_transition(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime")
    run = store.create_run("run-1", case_id="case-1", policy_digest="abc")

    assert run.state is RunState.PENDING
    assert store.transition_run("run-1", RunState.RUNNING).state is RunState.RUNNING
    assert store.transition_run("run-1", RunState.COMPLETED).state is RunState.COMPLETED

    with pytest.raises(ValueError, match="invalid run transition"):
        store.transition_run("run-1", RunState.RUNNING)
