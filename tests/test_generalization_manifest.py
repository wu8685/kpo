from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign_profile import _campaign_profile

from kpo.campaign_profile import load_campaign_profile
from kpo.cli import main
from kpo.generalization import load_generalization_manifest


def _append_generalization_case(root: Path, *, case_id: str = "future-1") -> None:
    case_path = root / "cases.jsonl"
    rows = [
        json.loads(line)
        for line in case_path.read_text(encoding="utf-8").splitlines()
    ]
    rows.append(
        {
            "case_id": case_id,
            "prompt": "Analyze an untouched future scenario.",
            "visible_context": ["A future-only visible fact."],
            "hidden_reference_ids": ["future-ref-1"],
            "tags": ["future"],
            "information_cutoff": None,
        }
    )
    case_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    reference_path = root / "references" / "future-ref-1.md"
    reference_path.write_text("Expected future reasoning.\n", encoding="utf-8")
    reference_manifest = root / "references.jsonl"
    with reference_manifest.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "artifact_id": "future-ref-1",
                    "kind": "expected",
                    "path": "references/future-ref-1.md",
                }
            )
            + "\n"
        )


def _manifest(root: Path, rows: list[dict] | None = None) -> Path:
    path = root / "generalization.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                rows
                if rows is not None
                else [
                    {
                        "case_id": "future-1",
                        "weight": 1.0,
                        "provenance": ["synthetic:v1"],
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    return path


def _base(tmp_path: Path):
    path = _campaign_profile(tmp_path / "external")
    _append_generalization_case(path.parent)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    return profile, path


def test_generalization_manifest_is_strict_deterministic_and_ordered(
    tmp_path: Path,
) -> None:
    profile, path = _base(tmp_path)
    manifest_path = _manifest(path.parent)

    first = load_generalization_manifest(
        manifest_path, profile.base.cases, profile.dataset
    )
    second = load_generalization_manifest(
        manifest_path, profile.base.cases, profile.dataset
    )

    assert first == second
    assert first.digest
    assert first.entries[0].case_id == "future-1"
    assert first.entries[0].weight == 1.0
    assert first.entries[0].provenance == ("synthetic:v1",)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "must not be empty"),
        (
            [
                {
                    "case_id": "future-1",
                    "weight": 1.0,
                    "provenance": ["synthetic:v1"],
                    "extra": True,
                }
            ],
            "unknown generalization entry fields",
        ),
        (
            [
                {
                    "case_id": "missing",
                    "weight": 1.0,
                    "provenance": ["synthetic:v1"],
                }
            ],
            "unknown generalization case_id",
        ),
        (
            [
                {
                    "case_id": "future-1",
                    "weight": 0,
                    "provenance": ["synthetic:v1"],
                }
            ],
            "weight",
        ),
        (
            [
                {
                    "case_id": "future-1",
                    "weight": 1.0,
                    "provenance": [],
                }
            ],
            "provenance",
        ),
        (
            [
                {
                    "case_id": "future-1",
                    "weight": 1.0,
                    "provenance": ["one"],
                },
                {
                    "case_id": " FUTURE-1 ",
                    "weight": 1.0,
                    "provenance": ["two"],
                },
            ],
            "duplicate generalization case_id",
        ),
        (
            [
                {
                    "case_id": "case-1",
                    "weight": 1.0,
                    "provenance": ["synthetic:v1"],
                }
            ],
            "overlaps campaign dataset",
        ),
    ],
)
def test_generalization_manifest_rejects_invalid_records(
    tmp_path: Path, rows: list[dict], message: str
) -> None:
    profile, path = _base(tmp_path)
    manifest_path = _manifest(path.parent, rows)

    with pytest.raises(ValueError, match=message):
        load_generalization_manifest(
            manifest_path, profile.base.cases, profile.dataset
        )


@pytest.mark.parametrize("collision", ["prompt", "context", "content"])
def test_generalization_manifest_rejects_cross_partition_contamination(
    tmp_path: Path, collision: str
) -> None:
    profile, path = _base(tmp_path)
    cases_path = path.parent / "cases.jsonl"
    rows = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
    ]
    campaign = rows[0]
    future = rows[-1]
    if collision in {"prompt", "content"}:
        future["prompt"] = campaign["prompt"].swapcase()
    if collision in {"context", "content"}:
        future["visible_context"] = [campaign["visible_context"][0].upper()]
    cases_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    reloaded = load_campaign_profile(path, checkout=Path(__file__).parents[1])

    with pytest.raises(ValueError, match="generalization contamination"):
        load_generalization_manifest(
            _manifest(path.parent), reloaded.base.cases, reloaded.dataset
        )


def test_dataset_growth_cannot_move_generalization_case_into_campaign(
    tmp_path: Path,
) -> None:
    from test_campaign import _initialize
    from test_generalization_profile import _generalization_profile

    path = _generalization_profile(tmp_path / "external")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    proposed = path.parent / "proposed-dataset.jsonl"
    rows = [
        json.loads(line)
        for line in profile.dataset_path.read_text(encoding="utf-8").splitlines()
    ]
    rows.append(
        {
            "case_id": "future-1",
            "partition": "train",
            "weight": 1.0,
            "provenance": ["synthetic:growth"],
        }
    )
    proposed.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="overlaps campaign dataset"):
        main(
            [
                "dataset",
                "grow",
                "--profile",
                str(path),
                "--inbox",
                str(proposed),
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )
