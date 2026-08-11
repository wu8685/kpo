from __future__ import annotations

import math

import pytest

from kpo.calibration_metrics import CalibrationCollector


ANCHORS = (
    ("b" * 64, {"alignment": 0.6, "validity": 0.7}),
    ("a" * 64, {"alignment": 0.2, "validity": 0.5}),
)
SLOTS = ("primary", "observer:a")


def _collector() -> CalibrationCollector:
    return CalibrationCollector(
        anchors=ANCHORS,
        slots=SLOTS,
        dimensions=("alignment", "validity"),
    )


def test_calibration_metrics_recover_signed_absolute_rms_stddev_and_max() -> None:
    collector = _collector()
    collector.record_scores(
        "a" * 64, "primary", {"alignment": 0.4, "validity": 0.5}
    )
    collector.record_scores(
        "b" * 64, "primary", {"alignment": 0.5, "validity": 0.9}
    )
    collector.record_unavailable("a" * 64, "observer:a", "timeout")
    collector.record_scores(
        "b" * 64, "observer:a", {"alignment": 0.8, "validity": 0.7}
    )

    metrics = collector.metrics()
    primary = metrics["primary"]["alignment"]
    observer = metrics["observer:a"]["alignment"]

    assert primary == {
        "comparable_count": 2,
        "expected_mean": pytest.approx(0.4),
        "evaluator_mean": pytest.approx(0.45),
        "signed_error_mean": pytest.approx(0.05),
        "mean_absolute_error": pytest.approx(0.15),
        "root_mean_square_error": pytest.approx(math.sqrt(0.025)),
        "error_population_stddev": pytest.approx(0.15),
        "max_absolute_error": pytest.approx(0.2),
        "unavailable_counts": {},
    }
    assert observer["comparable_count"] == 1
    assert observer["expected_mean"] == pytest.approx(0.6)
    assert observer["evaluator_mean"] == pytest.approx(0.8)
    assert observer["signed_error_mean"] == pytest.approx(0.2)
    assert observer["error_population_stddev"] == pytest.approx(0.0)
    assert observer["unavailable_counts"] == {"timeout": 1}


def test_empty_calibration_comparisons_use_null_not_zero_or_nan() -> None:
    collector = _collector()
    for anchor_digest, _ in ANCHORS:
        collector.record_unavailable(
            anchor_digest, "primary", "not_attempted_budget_exhausted"
        )
        collector.record_unavailable(anchor_digest, "observer:a", "timeout")

    metric = collector.metrics()["primary"]["validity"]

    assert metric == {
        "comparable_count": 0,
        "expected_mean": None,
        "evaluator_mean": None,
        "signed_error_mean": None,
        "mean_absolute_error": None,
        "root_mean_square_error": None,
        "error_population_stddev": None,
        "max_absolute_error": None,
        "unavailable_counts": {"not_attempted_budget_exhausted": 2},
    }


def test_calibration_order_is_deterministic_and_duplicate_replay_is_idempotent() -> None:
    forward = _collector()
    reverse = _collector()
    rows = (
        ("a" * 64, "primary", {"alignment": 0.3, "validity": 0.4}),
        ("a" * 64, "observer:a", {"alignment": 0.2, "validity": 0.5}),
        ("b" * 64, "primary", {"alignment": 0.7, "validity": 0.8}),
        ("b" * 64, "observer:a", {"alignment": 0.6, "validity": 0.7}),
    )
    for collector, ordered in ((forward, rows), (reverse, reversed(rows))):
        for anchor_digest, slot, scores in ordered:
            collector.record_scores(anchor_digest, slot, scores)
            collector.record_scores(anchor_digest, slot, scores)

    assert forward.metrics() == reverse.metrics()
    assert forward.observations() == reverse.observations()
    assert [row["anchor_digest"] for row in forward.observations()] == [
        "a" * 64,
        "b" * 64,
    ]
    assert list(forward.metrics()) == ["primary", "observer:a"]


def test_calibration_rejects_unknown_conflicting_or_invalid_observations() -> None:
    collector = _collector()
    collector.record_scores(
        "a" * 64, "primary", {"alignment": 0.3, "validity": 0.4}
    )

    with pytest.raises(ValueError, match="conflicting"):
        collector.record_scores(
            "a" * 64, "primary", {"alignment": 0.4, "validity": 0.4}
        )
    with pytest.raises(ValueError, match="conflicting"):
        collector.record_unavailable("a" * 64, "primary", "timeout")
    with pytest.raises(ValueError, match="unknown anchor"):
        collector.record_unavailable("c" * 64, "primary", "timeout")
    with pytest.raises(ValueError, match="unknown evaluator slot"):
        collector.record_unavailable("b" * 64, "observer:missing", "timeout")
    with pytest.raises(ValueError, match="dimensions"):
        collector.record_scores("b" * 64, "primary", {"alignment": 0.4})
    with pytest.raises(ValueError, match="finite"):
        collector.record_scores(
            "b" * 64,
            "primary",
            {"alignment": math.nan, "validity": 0.4},
        )


def test_calibration_constructor_rejects_duplicates_and_bad_expected_scores() -> None:
    with pytest.raises(ValueError, match="duplicate anchor"):
        CalibrationCollector(
            anchors=(ANCHORS[0], ANCHORS[0]),
            slots=SLOTS,
            dimensions=("alignment", "validity"),
        )
    with pytest.raises(ValueError, match="duplicate evaluator slot"):
        CalibrationCollector(
            anchors=ANCHORS,
            slots=("primary", "primary"),
            dimensions=("alignment", "validity"),
        )
    with pytest.raises(ValueError, match="expected score"):
        CalibrationCollector(
            anchors=(("a" * 64, {"alignment": 2.0, "validity": 0.5}),),
            slots=SLOTS,
            dimensions=("alignment", "validity"),
        )
