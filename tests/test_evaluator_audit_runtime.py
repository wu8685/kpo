from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kpo.calibration_metrics import CalibrationCollector
from kpo.evaluator_audit_runtime import (
    EvaluatorCalibrationCheckpointError,
    EvaluatorAuditBudgetError,
    EvaluatorAuditBudgetExhausted,
    EvaluatorAuditCallExecutor,
    cleanup_calibration_checkpoint,
    load_calibration_checkpoint,
    persist_calibration_checkpoint,
)
from kpo.profile import CommandProviderConfig
from kpo.provider import ProviderFailure


def _counting_config(counter: Path, *, fail: bool = False) -> CommandProviderConfig:
    code = (
        "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        "request=json.load(sys.stdin); "
        + (
            "sys.stderr.write('failed'); sys.exit(2)"
            if fail
            else "json.dump({'echo':request['request']['value']},sys.stdout)"
        )
    )
    return CommandProviderConfig(
        (sys.executable, "-c", code, str(counter)),
        "synthetic-evaluator",
        2,
        (),
    )


def test_audit_executor_is_fresh_per_call_and_per_campaign_without_cache(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    counter = tmp_path / "counter"
    config = _counting_config(counter)
    first = EvaluatorAuditCallExecutor(
        data_home, "campaign-a", max_calls=3
    )

    assert first.invoke(config, {"value": "same"}) == {"echo": "same"}
    assert first.invoke(config, {"value": "same"}) == {"echo": "same"}
    resumed = EvaluatorAuditCallExecutor(
        data_home, "campaign-a", max_calls=3
    )
    assert resumed.call_count == 2
    second_campaign = EvaluatorAuditCallExecutor(
        data_home, "campaign-b", max_calls=3
    )
    assert second_campaign.invoke(config, {"value": "same"}) == {"echo": "same"}

    assert counter.read_text() == "3"
    assert not (data_home / "campaigns" / "campaign-a" / "provider-cache").exists()
    assert first.state_path.name == "evaluator-audit-call-budget.json"
    assert not (first.root / "call-budget.json").exists()


def test_audit_budget_is_hard_persistent_and_failed_calls_are_counted(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    counter = tmp_path / "counter"
    executor = EvaluatorAuditCallExecutor(
        data_home, "campaign-a", max_calls=1
    )
    with pytest.raises(ProviderFailure):
        executor.invoke(_counting_config(counter, fail=True), {"value": "x"})

    resumed = EvaluatorAuditCallExecutor(
        data_home, "campaign-a", max_calls=1
    )
    assert resumed.call_count == 1
    with pytest.raises(EvaluatorAuditBudgetExhausted):
        resumed.invoke(_counting_config(counter), {"value": "x"})
    assert counter.read_text() == "1"

    state = json.loads(resumed.state_path.read_text())
    assert state["protocol"] == "kpo.evaluator-audit-budget/v1"
    assert state["campaign_id"] == "campaign-a"
    assert state["max_calls"] == 1
    assert state["call_count"] == 1
    assert state["budget_digest"]


def test_audit_budget_rejects_configuration_change_corruption_and_unknown_fields(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "runtime"
    executor = EvaluatorAuditCallExecutor(
        data_home, "campaign-a", max_calls=2
    )

    with pytest.raises(EvaluatorAuditBudgetError, match="max_calls"):
        EvaluatorAuditCallExecutor(data_home, "campaign-a", max_calls=3)

    raw = json.loads(executor.state_path.read_text())
    executor.state_path.write_text(json.dumps(raw | {"unknown": True}))
    with pytest.raises(EvaluatorAuditBudgetError, match="fields"):
        EvaluatorAuditCallExecutor(data_home, "campaign-a", max_calls=2)

    executor.state_path.write_text(json.dumps(raw | {"call_count": 1}))
    with pytest.raises(EvaluatorAuditBudgetError, match="digest"):
        EvaluatorAuditCallExecutor(data_home, "campaign-a", max_calls=2)


def test_audit_executor_checks_integrity_before_and_after_provider_call(
    tmp_path: Path,
) -> None:
    checks: list[int] = []
    executor = EvaluatorAuditCallExecutor(
        tmp_path / "runtime",
        "campaign-a",
        max_calls=1,
        integrity_check=lambda: checks.append(len(checks)),
    )

    executor.invoke(_counting_config(tmp_path / "counter"), {"value": "x"})

    assert checks == [0, 1]


_ANCHORS = (
    ("a" * 64, {"alignment": 0.2}),
    ("b" * 64, {"alignment": 0.6}),
)
_SLOT_RECORDS = (
    {
        "slot": "primary",
        "identity": "evaluator-primary",
        "configuration_digest": "1" * 64,
    },
    {
        "slot": "observer:a",
        "identity": "evaluator-observer",
        "configuration_digest": "2" * 64,
    },
)


def _collector() -> CalibrationCollector:
    return CalibrationCollector(
        anchors=_ANCHORS,
        slots=("primary", "observer:a"),
        dimensions=("alignment",),
    )


def _persist_checkpoint(
    data_home: Path, collector: CalibrationCollector
) -> Path:
    return persist_calibration_checkpoint(
        data_home,
        "campaign-a",
        collector,
        profile_snapshot_digest="3" * 64,
        anchor_set_digest="4" * 64,
        rubric_version="rubric-v1",
        slot_records=_SLOT_RECORDS,
        max_provider_calls=4,
    )


def _load_checkpoint(data_home: Path) -> CalibrationCollector:
    return load_calibration_checkpoint(
        data_home,
        "campaign-a",
        anchors=_ANCHORS,
        dimensions=("alignment",),
        profile_snapshot_digest="3" * 64,
        anchor_set_digest="4" * 64,
        rubric_version="rubric-v1",
        slot_records=_SLOT_RECORDS,
        max_provider_calls=4,
    )


def test_calibration_checkpoint_round_trip_is_strict_and_source_bound(
    tmp_path: Path,
) -> None:
    collector = _collector()
    collector.record_scores("a" * 64, "primary", {"alignment": 0.3})
    collector.record_unavailable("a" * 64, "observer:a", "timeout")
    checkpoint = _persist_checkpoint(tmp_path, collector)

    restored = _load_checkpoint(tmp_path)

    assert restored.observations() == collector.observations()
    raw = json.loads(checkpoint.read_text())
    assert raw["protocol"] == "kpo.evaluator-calibration-checkpoint/v1"
    assert raw["anchor_set_digest"] == "4" * 64
    assert raw["slots"] == list(_SLOT_RECORDS)
    assert raw["observations"][0]["slots"]["primary"][
        "configuration_digest"
    ] == "1" * 64
    assert "anchor_id" not in checkpoint.read_text()


def test_pending_checkpoint_becomes_interrupted_unknown_without_replay(
    tmp_path: Path,
) -> None:
    collector = _collector()
    collector.mark_pending("a" * 64, "primary")
    _persist_checkpoint(tmp_path, collector)

    restored = _load_checkpoint(tmp_path)

    slot = restored.observations()[0]["slots"]["primary"]
    assert slot == {
        "state": "unavailable",
        "failure_kind": "interrupted_unknown",
    }
    persisted = json.loads(
        (
            tmp_path
            / "campaigns"
            / "campaign-a"
            / "evaluator-calibration.checkpoint.json"
        ).read_text()
    )
    assert persisted["observations"][0]["slots"]["primary"]["state"] == "unavailable"
    assert "pending" not in json.dumps(persisted)


def test_calibration_checkpoint_rejects_corruption_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    checkpoint = _persist_checkpoint(tmp_path, _collector())
    raw = json.loads(checkpoint.read_text())
    checkpoint.write_text(json.dumps(raw | {"unknown": True}))
    with pytest.raises(EvaluatorCalibrationCheckpointError, match="fields"):
        _load_checkpoint(tmp_path)

    _persist_checkpoint(tmp_path, _collector())
    with pytest.raises(EvaluatorCalibrationCheckpointError, match="identity"):
        load_calibration_checkpoint(
            tmp_path,
            "campaign-a",
            anchors=_ANCHORS,
            dimensions=("alignment",),
            profile_snapshot_digest="3" * 64,
            anchor_set_digest="9" * 64,
            rubric_version="rubric-v1",
            slot_records=_SLOT_RECORDS,
            max_provider_calls=4,
        )

    raw = json.loads(checkpoint.read_text())
    raw["observations"] = []
    checkpoint.write_text(json.dumps(raw))
    with pytest.raises(EvaluatorCalibrationCheckpointError, match="digest"):
        _load_checkpoint(tmp_path)


def test_calibration_checkpoint_cleanup_warns_but_does_not_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint = _persist_checkpoint(tmp_path, _collector())
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == checkpoint:
            raise OSError("injected")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    cleanup_calibration_checkpoint(tmp_path, "campaign-a")

    assert checkpoint.exists()
    assert "could not remove completed calibration checkpoint" in caplog.text
