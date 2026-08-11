from __future__ import annotations

import math
import re
from collections import Counter
from types import MappingProxyType
from typing import Mapping, Sequence


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CalibrationCollector:
    def __init__(
        self,
        *,
        anchors: Sequence[tuple[str, Mapping[str, float]]],
        slots: Sequence[str],
        dimensions: Sequence[str],
    ) -> None:
        dimension_names = tuple(sorted(dimensions))
        if not dimension_names or len(set(dimension_names)) != len(dimension_names):
            raise ValueError("calibration dimensions must be non-empty and unique")
        slot_names = tuple(slots)
        if not slot_names or slot_names[0] != "primary":
            raise ValueError("first evaluator slot must be primary")
        if len(set(slot_names)) != len(slot_names):
            raise ValueError("duplicate evaluator slot")
        if tuple(sorted(slot_names[1:])) != slot_names[1:]:
            raise ValueError("observer evaluator slots must be sorted")

        expected: dict[str, Mapping[str, float]] = {}
        for anchor_digest, raw_scores in anchors:
            if not isinstance(anchor_digest, str) or not _DIGEST.fullmatch(
                anchor_digest
            ):
                raise ValueError("anchor digest must be lowercase SHA-256")
            if anchor_digest in expected:
                raise ValueError(f"duplicate anchor digest: {anchor_digest}")
            expected[anchor_digest] = self._normalize_scores(
                raw_scores,
                dimension_names,
                label="expected score",
            )
        if not expected:
            raise ValueError("calibration anchors must not be empty")

        self.dimensions = dimension_names
        self.slots = slot_names
        self.expected = MappingProxyType(dict(sorted(expected.items())))
        self._observations: dict[str, dict[str, dict[str, object]]] = {
            anchor_digest: {} for anchor_digest in self.expected
        }

    @staticmethod
    def _normalize_scores(
        raw: Mapping[str, float],
        dimensions: tuple[str, ...],
        *,
        label: str,
    ) -> Mapping[str, float]:
        if not isinstance(raw, Mapping) or set(raw) != set(dimensions):
            raise ValueError(f"{label} dimensions must match rubric")
        normalized: dict[str, float] = {}
        for dimension in dimensions:
            score = raw[dimension]
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise ValueError(f"{label}.{dimension} must be finite")
            if not 0 <= score <= 1:
                raise ValueError(f"{label}.{dimension} must be in [0, 1]")
            normalized[dimension] = float(score)
        return MappingProxyType(normalized)

    def _coordinate(self, anchor_digest: str, slot: str) -> dict[str, object]:
        if anchor_digest not in self.expected:
            raise ValueError(f"unknown anchor digest: {anchor_digest}")
        if slot not in self.slots:
            raise ValueError(f"unknown evaluator slot: {slot}")
        return self._observations[anchor_digest]

    def _record(
        self, anchor_digest: str, slot: str, observation: dict[str, object]
    ) -> None:
        coordinates = self._coordinate(anchor_digest, slot)
        existing = coordinates.get(slot)
        if existing is None:
            coordinates[slot] = observation
        elif existing.get("state") == "pending" and observation.get("state") != "pending":
            coordinates[slot] = observation
        elif existing != observation:
            raise ValueError("conflicting calibration observation")

    def mark_pending(self, anchor_digest: str, slot: str) -> None:
        self._record(anchor_digest, slot, {"state": "pending"})

    def record_scores(
        self,
        anchor_digest: str,
        slot: str,
        scores: Mapping[str, float],
    ) -> None:
        normalized = self._normalize_scores(
            scores, self.dimensions, label="evaluator score"
        )
        self._record(
            anchor_digest,
            slot,
            {"state": "completed", "scores": dict(normalized)},
        )

    def record_unavailable(
        self,
        anchor_digest: str,
        slot: str,
        failure_kind: str,
    ) -> None:
        if not isinstance(failure_kind, str) or not failure_kind.strip():
            raise ValueError("calibration failure kind is required")
        self._record(
            anchor_digest,
            slot,
            {"state": "unavailable", "failure_kind": failure_kind},
        )

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return math.fsum(values) / len(values) if values else None

    def _metric(self, slot: str, dimension: str) -> dict[str, object]:
        expected_values: list[float] = []
        evaluator_values: list[float] = []
        unavailable: Counter[str] = Counter()
        for anchor_digest in self.expected:
            observation = self._observations[anchor_digest].get(slot)
            if observation is None:
                continue
            if observation["state"] == "completed":
                expected_values.append(self.expected[anchor_digest][dimension])
                raw_scores = observation["scores"]
                assert isinstance(raw_scores, dict)
                evaluator_values.append(float(raw_scores[dimension]))
            else:
                unavailable[str(observation["failure_kind"])] += 1

        errors = [
            evaluator - expected
            for expected, evaluator in zip(
                expected_values, evaluator_values, strict=True
            )
        ]
        absolute = [abs(error) for error in errors]
        signed_mean = self._mean(errors)
        population_stddev = (
            math.sqrt(
                math.fsum((error - signed_mean) ** 2 for error in errors)
                / len(errors)
            )
            if errors and signed_mean is not None
            else None
        )
        return {
            "comparable_count": len(errors),
            "expected_mean": self._mean(expected_values),
            "evaluator_mean": self._mean(evaluator_values),
            "signed_error_mean": signed_mean,
            "mean_absolute_error": self._mean(absolute),
            "root_mean_square_error": (
                math.sqrt(math.fsum(error * error for error in errors) / len(errors))
                if errors
                else None
            ),
            "error_population_stddev": population_stddev,
            "max_absolute_error": max(absolute) if absolute else None,
            "unavailable_counts": dict(sorted(unavailable.items())),
        }

    def metrics(self) -> dict[str, dict[str, dict[str, object]]]:
        return {
            slot: {
                dimension: self._metric(slot, dimension)
                for dimension in self.dimensions
            }
            for slot in self.slots
        }

    def observations(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for anchor_digest in self.expected:
            values = self._observations[anchor_digest]
            result.append(
                {
                    "anchor_digest": anchor_digest,
                    "expected_scores": dict(self.expected[anchor_digest]),
                    "slots": {
                        slot: values[slot]
                        for slot in self.slots
                        if slot in values
                    },
                }
            )
        return result

    def observation(self, anchor_digest: str, slot: str) -> dict[str, object] | None:
        coordinates = self._coordinate(anchor_digest, slot)
        value = coordinates.get(slot)
        return None if value is None else dict(value)
