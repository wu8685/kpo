from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign import _initialize
from test_campaign_series_campaign import _series_working_profile

from kpo.cli import main


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    assert main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_series_cli_init_campaign_status_evidence_and_stop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = str(Path(__file__).parents[1])
    _initialize(path)

    initialized = _run(
        capsys,
        "series",
        "init",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )
    campaign = _run(
        capsys,
        "campaign",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )
    budget_path = (
        path.parent
        / "runtime"
        / "campaigns"
        / campaign["campaign_id"]
        / "call-budget.json"
    )
    budget_before = budget_path.read_bytes()
    status = _run(
        capsys,
        "series",
        "status",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )
    evidence = _run(
        capsys,
        "series",
        "evidence",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )
    full_evidence = _run(
        capsys,
        "series",
        "evidence",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--full",
        "--checkout",
        checkout,
    )

    assert initialized["series_id"] == "series-a"
    assert campaign["series_id"] == "series-a"
    assert status["campaign_count"] == 1
    assert status["revision_count"] == 1
    assert status["latest_campaign_id"] == campaign["campaign_id"]
    assert evidence["protocol"] == "kpo.campaign-series-report/v1"
    assert evidence["campaign_count"] == 1
    assert evidence["revision_count"] == 1
    assert "revision_history" not in evidence
    assert full_evidence["revision_history"][0]["revision"] == 0
    assert evidence["learning_curve"]["alignment"][0]["validation_gain"] == 0.8
    assert budget_path.read_bytes() == budget_before
    serialized = json.dumps(evidence, sort_keys=True)
    for prohibited in ("case_id", "artifact_id", "reference", str(tmp_path)):
        assert prohibited not in serialized

    stopped = _run(
        capsys,
        "series",
        "stop",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )
    assert stopped["state"] == "stopped_by_owner"


def test_series_cli_reconcile_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = str(Path(__file__).parents[1])
    _initialize(path)
    _run(
        capsys,
        "series",
        "init",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )

    result = _run(
        capsys,
        "series",
        "reconcile",
        "--profile",
        str(path),
        "--series",
        "series-a",
        "--checkout",
        checkout,
    )

    assert result["attached_count"] == 0
    assert result["campaign_count"] == 0
