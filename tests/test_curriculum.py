from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.curriculum import (
    ACTIONABLE_KINDS,
    CurriculumCandidate,
    CurriculumSelection,
    load_curriculum_inbox,
    select_curriculum,
    validate_curriculum_eligibility,
)
from kpo.dataset import load_dataset
from kpo.generalization import load_generalization_manifest
from kpo.models import TaskCase


def _write_inbox(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def _row(
    case_id: str,
    *,
    gap_kinds: list[str] | None = None,
    facets: list[str] | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "gap_kinds": gap_kinds or ["missing"],
        "facets": facets or ["boundary"],
        "provenance": [f"source:{case_id}"],
    }


def test_curriculum_inbox_is_strict_normalized_and_order_independent(
    tmp_path: Path,
) -> None:
    rows = [
        _row(
            "candidate-b",
            gap_kinds=["missing", "frame_divergence"],
            facets=["boundary", "causal-order"],
        ),
        _row("candidate-a", gap_kinds=["surplus"]),
    ]
    first = load_curriculum_inbox(
        _write_inbox(tmp_path / "first.jsonl", rows),
        allowed_facets=("boundary", "causal-order"),
    )
    second = load_curriculum_inbox(
        _write_inbox(tmp_path / "second.jsonl", list(reversed(rows))),
        allowed_facets=("boundary", "causal-order"),
    )

    assert tuple(candidate.case_id for candidate in first.candidates) == (
        "candidate-a",
        "candidate-b",
    )
    assert first.record_digest == second.record_digest
    assert first.byte_digest != second.byte_digest
    assert first.candidates[1].gap_kinds == ("missing", "frame_divergence")
    assert set(ACTIONABLE_KINDS) == {
        "missing",
        "surplus",
        "contradicted",
        "frame_divergence",
    }


@pytest.mark.parametrize(
    ("rows", "allowed_facets", "message"),
    [
        ([], ("boundary",), "must not be empty"),
        ([_row("candidate-a") | {"unknown": True}], ("boundary",), "unknown"),
        ([_row("candidate-a", gap_kinds=["matched"])], ("boundary",), "gap kind"),
        (
            [_row("candidate-a", facets=["undeclared"])],
            ("boundary",),
            "undeclared facet",
        ),
        (
            [_row("candidate-a", gap_kinds=["missing", "MISSING"])],
            ("boundary",),
            "normalized",
        ),
        ([_row("candidate-a"), _row("CANDIDATE-A")], ("boundary",), "case_id"),
    ],
)
def test_curriculum_inbox_rejects_invalid_records(
    tmp_path: Path,
    rows: list[dict[str, object]],
    allowed_facets: tuple[str, ...],
    message: str,
) -> None:
    path = _write_inbox(tmp_path / "inbox.jsonl", rows)
    with pytest.raises(ValueError, match=message):
        load_curriculum_inbox(path, allowed_facets=allowed_facets)


def test_curriculum_inbox_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "inbox.jsonl"
    path.write_text(
        '{"case_id":"a","case_id":"b","gap_kinds":["missing"],'
        '"facets":["boundary"],"provenance":["source"]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_curriculum_inbox(path, allowed_facets=("boundary",))


def test_curriculum_inbox_preserves_case_sensitive_provenance(tmp_path: Path) -> None:
    row = _row("candidate-a")
    row["provenance"] = ["Source:Case-Sensitive"]
    inbox = load_curriculum_inbox(
        _write_inbox(tmp_path / "inbox.jsonl", [row]),
        allowed_facets=("boundary",),
    )

    assert inbox.candidates[0].provenance == ("Source:Case-Sensitive",)


def test_selection_is_gap_weighted_greedy_and_deterministic() -> None:
    candidates = (
        CurriculumCandidate(
            "candidate-a",
            ("missing",),
            ("boundary",),
            ("source:a",),
        ),
        CurriculumCandidate(
            "candidate-b",
            ("missing",),
            ("boundary",),
            ("source:b",),
        ),
        CurriculumCandidate(
            "candidate-c",
            ("contradicted",),
            ("counterfactual",),
            ("source:c",),
        ),
    )

    selection = select_curriculum(
        tuple(reversed(candidates)),
        kind_counts={
            "missing": 3,
            "surplus": 0,
            "contradicted": 1,
            "frame_divergence": 0,
        },
        max_selection=2,
        diversity_weight=0.5,
    )

    assert isinstance(selection, CurriculumSelection)
    assert selection.selected_case_ids == ("candidate-a", "candidate-c")
    assert selection.gap_weights == {
        "missing": 0.75,
        "surplus": 0.0,
        "contradicted": 0.25,
        "frame_divergence": 0.0,
    }
    assert selection.steps[0].score == 0.875
    assert selection.steps[1].diversity == 1.0
    assert selection.steps[1].score == 0.625


@pytest.mark.parametrize(
    ("kind_counts", "message"),
    [
        (
            {
                "missing": 0,
                "surplus": 0,
                "contradicted": 0,
                "frame_divergence": 0,
            },
            "signal unavailable",
        ),
        (
            {
                "missing": 1,
                "surplus": 0,
                "contradicted": 0,
                "frame_divergence": 0,
                "matched": 2,
            },
            "kind_counts",
        ),
    ],
)
def test_selection_requires_exact_actionable_signal(
    kind_counts: dict[str, int], message: str
) -> None:
    candidates = (
        CurriculumCandidate(
            "candidate-a", ("missing",), ("boundary",), ("source:a",)
        ),
    )
    with pytest.raises(ValueError, match=message):
        select_curriculum(
            candidates,
            kind_counts=kind_counts,
            max_selection=1,
            diversity_weight=0.3,
        )


def _eligibility_fixture(tmp_path: Path) -> tuple[dict[str, TaskCase], object, object]:
    cases = {
        f"case-{index}": TaskCase(
            case_id=f"case-{index}",
            prompt=f"Analyze eligibility scenario {index}.",
            visible_context=(f"Eligibility evidence {index}.",),
            hidden_reference_ids=("ref",),
        )
        for index in range(7)
    }
    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        "".join(
            json.dumps(
                {
                    "case_id": f"case-{index}",
                    "partition": partition,
                    "weight": 1.0,
                    "provenance": [f"source:{index}"],
                }
            )
            + "\n"
            for index, partition in enumerate(
                ("train", "validation", "holdout", "regression")
            )
        ),
        encoding="utf-8",
    )
    dataset = load_dataset(dataset_path, cases)
    generalization_path = tmp_path / "generalization.jsonl"
    generalization_path.write_text(
        json.dumps(
            {"case_id": "case-4", "weight": 1.0, "provenance": ["source:4"]}
        )
        + "\n",
        encoding="utf-8",
    )
    generalization = load_generalization_manifest(
        generalization_path, cases, dataset
    )
    return cases, dataset, generalization


def test_curriculum_eligibility_accepts_only_isolated_cases(tmp_path: Path) -> None:
    cases, dataset, generalization = _eligibility_fixture(tmp_path)
    inbox = load_curriculum_inbox(
        _write_inbox(tmp_path / "inbox.jsonl", [_row("case-5")]),
        allowed_facets=("boundary",),
    )

    result = validate_curriculum_eligibility(
        inbox,
        cases=cases,
        dataset=dataset,
        generalization=generalization,
    )

    assert result == inbox.candidates


@pytest.mark.parametrize(
    ("candidate_id", "message"),
    [
        ("case-0", "dataset"),
        ("case-4", "generalization"),
        ("missing-case", "case manifest"),
    ],
)
def test_curriculum_eligibility_rejects_existing_or_unknown_ids(
    tmp_path: Path, candidate_id: str, message: str
) -> None:
    cases, dataset, generalization = _eligibility_fixture(tmp_path)
    inbox = load_curriculum_inbox(
        _write_inbox(tmp_path / "inbox.jsonl", [_row(candidate_id)]),
        allowed_facets=("boundary",),
    )
    with pytest.raises(ValueError, match=message):
        validate_curriculum_eligibility(
            inbox,
            cases=cases,
            dataset=dataset,
            generalization=generalization,
        )


@pytest.mark.parametrize("collision", ["prompt", "context", "complete"])
def test_curriculum_eligibility_rejects_content_contamination(
    tmp_path: Path, collision: str
) -> None:
    cases, dataset, generalization = _eligibility_fixture(tmp_path)
    original = cases["case-5"]
    if collision == "prompt":
        cases["case-5"] = TaskCase(
            "case-5",
            cases["case-0"].prompt.upper(),
            original.visible_context,
            original.hidden_reference_ids,
        )
    elif collision == "context":
        cases["case-5"] = TaskCase(
            "case-5",
            original.prompt,
            (cases["case-4"].visible_context[0].upper(),),
            original.hidden_reference_ids,
        )
    else:
        cases["case-5"] = TaskCase(
            "case-5",
            cases["case-0"].prompt,
            cases["case-0"].visible_context,
            original.hidden_reference_ids,
        )
    inbox = load_curriculum_inbox(
        _write_inbox(tmp_path / "inbox.jsonl", [_row("case-5")]),
        allowed_facets=("boundary",),
    )
    with pytest.raises(ValueError, match="contamination"):
        validate_curriculum_eligibility(
            inbox,
            cases=cases,
            dataset=dataset,
            generalization=generalization,
        )


def test_curriculum_eligibility_rejects_inbox_batch_duplicates(
    tmp_path: Path,
) -> None:
    cases, dataset, generalization = _eligibility_fixture(tmp_path)
    cases["case-6"] = TaskCase(
        "case-6",
        cases["case-5"].prompt.lower(),
        ("different evidence",),
        ("ref",),
    )
    inbox = load_curriculum_inbox(
        _write_inbox(
            tmp_path / "inbox.jsonl", [_row("case-5"), _row("case-6")]
        ),
        allowed_facets=("boundary",),
    )
    with pytest.raises(ValueError, match="duplicate candidate"):
        validate_curriculum_eligibility(
            inbox,
            cases=cases,
            dataset=dataset,
            generalization=generalization,
        )
