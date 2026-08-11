from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kpo.digest import canonical_digest
from kpo.evaluator_agreement import (
    AgreementCollector,
    cleanup_agreement_checkpoint,
    evaluation_event_id,
    load_agreement_checkpoint,
    persist_agreement_checkpoint,
)


OBSERVERS = (("observer-a", "evaluator-a"), ("observer-b", "evaluator-b"))


def _collector() -> AgreementCollector:
    return AgreementCollector(
        primary_evaluator_id="primary",
        observers=OBSERVERS,
        rubric_version="rubric-v1",
        dimensions=("alignment", "specificity"),
    )


def _report(collector: AgreementCollector, state: str = "complete") -> dict:
    return collector.report(
        campaign_id="campaign-1",
        profile_id="profile-1",
        profile_snapshot_digest="snapshot-1",
        collection_state=state,
    )


def test_agreement_metrics_recover_exact_offsets_missing_pairs_and_panel_range() -> None:
    collector = _collector()
    collector.record_primary(
        "event-1", "validation", {"alignment": 0.2, "specificity": 0.5}
    )
    collector.record_observer(
        "event-1", "observer-a", {"alignment": 0.4, "specificity": 0.5}
    )
    collector.record_unavailable("event-1", "observer-b", "timeout")
    collector.record_primary(
        "event-2", "validation", {"alignment": 0.6, "specificity": 0.7}
    )
    collector.record_observer(
        "event-2", "observer-a", {"alignment": 0.8, "specificity": 0.7}
    )
    collector.record_observer(
        "event-2", "observer-b", {"alignment": 0.5, "specificity": 0.6}
    )

    report = _report(collector)
    alignment = report["metrics"]["validation"]["alignment"]
    observer_a = alignment["observers"]["observer-a"]
    observer_b = alignment["observers"]["observer-b"]

    assert report["event_count"] == 2
    assert report["event_counts"] == {"validation": 2}
    assert observer_a == {
        "comparable_count": 2,
        "primary_mean": pytest.approx(0.4),
        "observer_mean": pytest.approx(0.6),
        "signed_offset_mean": pytest.approx(0.2),
        "mean_absolute_delta": pytest.approx(0.2),
        "max_absolute_delta": pytest.approx(0.2),
        "unavailable_count": {},
    }
    assert observer_b["comparable_count"] == 1
    assert observer_b["primary_mean"] == pytest.approx(0.6)
    assert observer_b["observer_mean"] == pytest.approx(0.5)
    assert observer_b["signed_offset_mean"] == pytest.approx(-0.1)
    assert observer_b["unavailable_count"] == {"timeout": 1}
    assert alignment["panel"] == {
        "panel_event_count": 2,
        "mean_event_range": pytest.approx(0.25),
        "max_event_range": pytest.approx(0.3),
    }
    assert report["availability"] == {"observer-b": {"timeout": 1}}


def test_empty_comparisons_are_null_and_budget_exhaustion_is_explicit() -> None:
    collector = _collector()
    collector.record_primary(
        "event-1", "holdout", {"alignment": 0.2, "specificity": 0.4}
    )
    collector.record_unavailable(
        "event-1", "observer-a", "not_attempted_budget_exhausted"
    )
    collector.record_unavailable(
        "event-1", "observer-b", "not_attempted_budget_exhausted"
    )

    report = _report(collector, "budget_exhausted")
    metric = report["metrics"]["holdout"]["alignment"]["observers"][
        "observer-a"
    ]

    assert report["collection_state"] == "budget_exhausted"
    assert metric == {
        "comparable_count": 0,
        "primary_mean": None,
        "observer_mean": None,
        "signed_offset_mean": None,
        "mean_absolute_delta": None,
        "max_absolute_delta": None,
        "unavailable_count": {"not_attempted_budget_exhausted": 1},
    }
    assert report["metrics"]["holdout"]["alignment"]["panel"] == {
        "panel_event_count": 0,
        "mean_event_range": None,
        "max_event_range": None,
    }


def test_event_insertion_order_and_duplicate_cache_replay_are_deterministic() -> None:
    forward = _collector()
    reverse = _collector()
    observations = (
        ("event-z", "regression", 0.3, 0.5),
        ("event-a", "regression", 0.4, 0.6),
    )
    for collector, rows in ((forward, observations), (reverse, reversed(observations))):
        for event_id, partition, primary, observer in rows:
            scores = {"alignment": primary, "specificity": primary}
            observer_scores = {"alignment": observer, "specificity": observer}
            collector.record_primary(event_id, partition, scores)
            collector.record_observer(event_id, "observer-a", observer_scores)
            collector.record_unavailable(event_id, "observer-b", "timeout")
            # Valid cache replay is idempotent and must not bias counts.
            collector.record_primary(event_id, partition, scores)
            collector.record_observer(event_id, "observer-a", observer_scores)
            collector.record_unavailable(event_id, "observer-b", "timeout")

    assert _report(forward) == _report(reverse)
    assert _report(forward)["event_count"] == 2


def test_conflicting_duplicate_or_invalid_score_is_rejected() -> None:
    collector = _collector()
    collector.record_primary(
        "event-1", "train", {"alignment": 0.2, "specificity": 0.4}
    )

    with pytest.raises(ValueError, match="conflicting"):
        collector.record_primary(
            "event-1", "train", {"alignment": 0.3, "specificity": 0.4}
        )
    with pytest.raises(ValueError, match="dimension"):
        collector.record_observer("event-1", "observer-a", {"alignment": 0.3})
    with pytest.raises(ValueError, match="finite"):
        collector.record_observer(
            "event-1",
            "observer-a",
            {"alignment": math.nan, "specificity": 0.4},
        )


def test_event_id_binds_partition_and_target_not_evaluator_identity() -> None:
    fields = {
        "case_digest": "case",
        "rollout_digest": "rollout",
        "policy_digest": "policy",
        "partition": "validation",
        "rubric_digest": "rubric",
    }

    event_id = evaluation_event_id(**fields)

    assert event_id == canonical_digest(fields)
    assert event_id != evaluation_event_id(**(fields | {"partition": "holdout"}))


def test_checkpoint_round_trip_preserves_collector_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    collector = _collector()
    collector.record_primary(
        "a" * 64, "validation", {"alignment": 0.2, "specificity": 0.4}
    )
    collector.record_observer(
        "a" * 64, "observer-a", {"alignment": 0.4, "specificity": 0.4}
    )
    collector.record_unavailable("a" * 64, "observer-b", "timeout")

    checkpoint = persist_agreement_checkpoint(
        tmp_path,
        "campaign-1",
        collector,
        profile_snapshot_digest="snapshot-1",
    )
    restored = load_agreement_checkpoint(
        tmp_path,
        "campaign-1",
        primary_evaluator_id="primary",
        observers=OBSERVERS,
        rubric_version="rubric-v1",
        dimensions=("alignment", "specificity"),
        profile_snapshot_digest="snapshot-1",
    )

    assert checkpoint.is_file()
    assert _report(restored) == _report(collector)
    assert "case_id" not in checkpoint.read_text(encoding="utf-8")

    raw = json.loads(checkpoint.read_text(encoding="utf-8"))
    checkpoint.write_text(
        json.dumps(raw | {"unexpected": True}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown agreement checkpoint fields"):
        load_agreement_checkpoint(
            tmp_path,
            "campaign-1",
            primary_evaluator_id="primary",
            observers=OBSERVERS,
            rubric_version="rubric-v1",
            dimensions=("alignment", "specificity"),
            profile_snapshot_digest="snapshot-1",
        )


def test_checkpoint_cleanup_failure_warns_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    collector = _collector()
    checkpoint = persist_agreement_checkpoint(
        tmp_path,
        "campaign-1",
        collector,
        profile_snapshot_digest="snapshot-1",
    )
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == checkpoint:
            raise OSError("injected")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    cleanup_agreement_checkpoint(tmp_path, "campaign-1")

    assert checkpoint.exists()
    assert "could not remove completed agreement checkpoint" in caplog.text
