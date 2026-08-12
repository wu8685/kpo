from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.cli import main
from kpo.dataset import load_dataset
from test_curriculum_plan import _ready_profile


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, object]:
    assert main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_curriculum_cli_previews_and_applies_without_private_source_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _ready_profile(tmp_path)
    common = (
        "--profile",
        str(profile.base.path),
        "--series",
        "series-a",
        "--checkout",
        str(Path(__file__).parents[1]),
    )

    preview = _run(capsys, "curriculum", "select", *common)

    assert preview["state"] == "preview"
    assert preview["selected_case_ids"] == ["candidate-1"]
    assert preview["selected_gap_coverage"] == {"missing": 1}
    assert preview["approval_digest"]
    serialized = json.dumps(preview, sort_keys=True)
    assert "campaign-a" not in serialized
    assert "Analyze a novel" not in serialized

    applied = _run(
        capsys,
        "curriculum",
        "select",
        *common,
        "--approve",
        str(preview["approval_digest"]),
    )
    assert applied["state"] == "committed"
    updated = load_dataset(profile.dataset_path, profile.base.cases)
    assert "candidate-1" in {entry.case_id for entry in updated.entries}
