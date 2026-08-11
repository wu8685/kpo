from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign import _initialize
from test_campaign_series_campaign import _series_working_profile
from test_generalization_manifest import _append_generalization_case, _manifest

from kpo.campaign import run_campaign
from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import (
    GeneralizationInterrupted,
    generalize_series,
    initialize_series,
    load_series_state,
    series_evidence_report,
    series_status,
    stop_series,
)
from kpo.external_promotion import (
    apply_campaign_promotion,
    preview_campaign_promotion,
)
from kpo.cli import main


def _measurement_profile(
    root: Path, *, max_provider_calls: int = 40, capture: bool = False
) -> Path:
    path = _series_working_profile(root, max_campaigns=2)
    _append_generalization_case(root)
    _manifest(root)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            f'''

[series.generalization]
manifest = "./generalization.jsonl"
max_provider_calls = {max_provider_calls}
'''
        )
    if capture:
        for name in ("actor", "evaluator"):
            script = root / "bin" / name
            text = script.read_text(encoding="utf-8")
            marker = "r=json.load(sys.stdin)['request']"
            capture_path = root / "runtime" / f"{name}-requests.jsonl"
            replacement = (
                marker
                + "\nfrom pathlib import Path\n"
                + f"p=Path({str(capture_path)!r}); p.parent.mkdir(parents=True,exist_ok=True); "
                + "p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')"
            )
            script.write_text(text.replace(marker, replacement), encoding="utf-8")
    return path


def _add_second_generalization_case(root: Path) -> None:
    cases_path = root / "cases.jsonl"
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
    ]
    cases.append(
        {
            "case_id": "future-2",
            "prompt": "Analyze a second untouched future scenario.",
            "visible_context": ["A second future-only visible fact."],
            "hidden_reference_ids": ["future-ref-2"],
            "tags": ["future"],
            "information_cutoff": None,
        }
    )
    cases_path.write_text(
        "".join(json.dumps(row) + "\n" for row in cases), encoding="utf-8"
    )
    (root / "references" / "future-ref-2.md").write_text(
        "Expected second future reasoning.\n", encoding="utf-8"
    )
    with (root / "references.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "artifact_id": "future-ref-2",
                    "kind": "expected",
                    "path": "references/future-ref-2.md",
                }
            )
            + "\n"
        )
    _manifest(
        root,
        [
            {
                "case_id": "future-1",
                "weight": 1.0,
                "provenance": ["synthetic:v1"],
            },
            {
                "case_id": "future-2",
                "weight": 2.0,
                "provenance": ["synthetic:v1"],
            },
        ],
    )


def _applied_stopped(path: Path, *, series_id: str = "series-a"):
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, series_id)
    campaign = run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-a",
        series_id=series_id,
    )
    preview = preview_campaign_promotion(
        path, "campaign-a", checkout=checkout
    )
    apply_campaign_promotion(
        path,
        "campaign-a",
        checkout=checkout,
        approval_digest=preview["approval_digest"],
    )
    current = load_campaign_profile(path, checkout=checkout)
    stop_series(current, series_id)
    return current, campaign


def test_generalization_preflight_rejects_without_consuming_or_calling(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external", capture=True)
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    root = profile.base.data_home / "series" / "series-a"

    with pytest.raises(ValueError, match="series_not_stopped"):
        generalize_series(profile, "series-a")

    assert not (root / "generalization-consumption.json").exists()
    assert not (root / "generalization-calls.json").exists()


def test_generalization_budget_is_checked_before_consumption(tmp_path: Path) -> None:
    path = _measurement_profile(
        tmp_path / "external", max_provider_calls=3
    )
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    stop_series(profile, "series-a")

    with pytest.raises(ValueError, match="generalization_budget_insufficient"):
        generalize_series(profile, "series-a")

    root = profile.base.data_home / "series" / "series-a"
    assert not (root / "generalization-consumption.json").exists()


def test_generalization_requires_configuration_before_consumption(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")
    stop_series(profile, "series-a")

    with pytest.raises(ValueError, match="generalization_not_configured"):
        generalize_series(profile, "series-a")


def test_generalization_rejects_previewed_promotion_without_consuming(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-a",
        series_id="series-a",
    )
    stop_series(profile, "series-a")

    with pytest.raises(ValueError, match="series_promotion_not_final"):
        generalize_series(profile, "series-a")

    assert not (
        profile.base.data_home
        / "series"
        / "series-a"
        / "generalization-consumption.json"
    ).exists()


def test_generalization_measurement_is_matched_private_and_single_use(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external", capture=True)
    profile, campaign = _applied_stopped(path)
    root = profile.base.data_home / "series" / "series-a"

    result = generalize_series(profile, "series-a")

    assert result["protocol"] == "kpo.series-generalization/v1"
    assert result["state"] == "measured"
    assert result["case_count"] == 1
    assert result["matched_case_count"] == 1
    assert result["unavailable_case_count"] == 0
    assert result["provider_call_count"] == 4
    assert result["dimensions"] == {
        "alignment": {
            "initial_score": 0.2,
            "final_score": 1.0,
            "generalization_gain": 0.8,
            "holdout_gain": 0.8,
            "divergence": 0.0,
        }
    }
    assert campaign["candidate_policy_digest"] == result["final_policy_digest"]
    serialized = json.dumps(result, sort_keys=True)
    for prohibited in (
        "future-1",
        "future-only",
        "future-ref-1",
        "Expected future",
        "artifact_id",
        str(tmp_path),
    ):
        assert prohibited not in serialized

    consumption = json.loads(
        (root / "generalization-consumption.json").read_text(encoding="utf-8")
    )
    assert consumption["state"] == "measured"
    assert consumption["artifact_path"] == "series/series-a/generalization.json"
    ledger_before = (root / "generalization-calls.json").read_bytes()

    with pytest.raises(ValueError, match="generalization_consumed"):
        generalize_series(profile, "series-a")

    assert (root / "generalization-calls.json").read_bytes() == ledger_before


def test_campaign_paths_do_not_touch_generalization_cases(tmp_path: Path) -> None:
    path = _measurement_profile(tmp_path / "external", capture=True)
    profile, _ = _applied_stopped(path)

    private_marker = "future-1"
    for capture_name in ("actor-requests.jsonl", "evaluator-requests.jsonl"):
        capture = profile.base.data_home / capture_name
        assert capture.is_file()
        assert private_marker not in capture.read_text(encoding="utf-8")
    series_root = profile.base.data_home / "series" / "series-a"
    for artifact in series_root.glob("*.json"):
        assert private_marker not in artifact.read_text(encoding="utf-8")


def test_campaign_detects_generalization_source_drift_after_provider_call(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external")
    manifest = path.parent / "generalization.jsonl"
    actor = path.parent / "bin" / "actor"
    text = actor.read_text(encoding="utf-8")
    marker = "r=json.load(sys.stdin)['request']"
    mutation = (
        marker
        + "\nfrom pathlib import Path\n"
        + f"p=Path({str(manifest)!r}); p.write_text(p.read_text()+'\\n')"
    )
    actor.write_text(text.replace(marker, mutation), encoding="utf-8")
    _initialize(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    initialize_series(profile, "series-a")

    result = run_campaign(
        path,
        checkout=Path(__file__).parents[1],
        campaign_id="campaign-a",
        series_id="series-a",
    )

    assert result["state"] == "failed"
    assert result["failure_kind"] == "source_drift"
    assert result["call_count"] == 1


def test_generalization_interruption_consumes_without_retry(tmp_path: Path) -> None:
    path = _measurement_profile(tmp_path / "external")
    profile, _ = _applied_stopped(path)

    with pytest.raises(GeneralizationInterrupted):
        generalize_series(
            profile, "series-a", fault_after_consumption=True
        )

    state = load_series_state(profile, "series-a")
    assert state["generalization_state"] == "measuring"
    assert series_status(profile, "series-a")["generalization_state"] == (
        "measuring_interrupted"
    )
    with pytest.raises(ValueError, match="generalization_consumed"):
        generalize_series(profile, "series-a")
    assert not (
        profile.base.data_home
        / "series"
        / "series-a"
        / "generalization-calls.json"
    ).exists()


def test_generalization_provider_failure_is_terminal_safe_evidence(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external")
    evaluator = path.parent / "bin" / "evaluator"
    text = evaluator.read_text(encoding="utf-8")
    marker = "r=json.load(sys.stdin)['request'];"
    evaluator.write_text(
        text.replace(
            marker,
            marker + " sys.exit(3) if r['case_id']=='future-1' else None;",
        ),
        encoding="utf-8",
    )
    profile, _ = _applied_stopped(path)

    result = generalize_series(profile, "series-a")

    assert result["state"] == "failed"
    assert result["matched_case_count"] == 0
    assert result["unavailable_case_count"] == 1
    assert result["provider_call_count"] == 2
    assert result["failure_kind"] == "generalization_provider_failure"
    assert result["dimensions"]["alignment"] == {
        "initial_score": None,
        "final_score": None,
        "generalization_gain": None,
        "holdout_gain": 0.8,
        "divergence": None,
    }


def test_generalization_provider_failure_after_one_pair_is_partial(
    tmp_path: Path,
) -> None:
    path = _measurement_profile(tmp_path / "external")
    _add_second_generalization_case(path.parent)
    evaluator = path.parent / "bin" / "evaluator"
    text = evaluator.read_text(encoding="utf-8")
    marker = "r=json.load(sys.stdin)['request'];"
    evaluator.write_text(
        text.replace(
            marker,
            marker + " sys.exit(3) if r['case_id']=='future-2' else None;",
        ),
        encoding="utf-8",
    )
    profile, _ = _applied_stopped(path)

    result = generalize_series(profile, "series-a")

    assert result["state"] == "partial"
    assert result["case_count"] == 2
    assert result["matched_case_count"] == 1
    assert result["unavailable_case_count"] == 1
    assert result["provider_call_count"] == 6
    assert result["dimensions"]["alignment"]["generalization_gain"] == 0.8


def test_generalization_cli_runs_explicit_one_way_measurement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _measurement_profile(tmp_path / "external")
    _applied_stopped(path)

    assert (
        main(
            [
                "series",
                "generalize",
                "--profile",
                str(path),
                "--series",
                "series-a",
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["state"] == "measured"
    assert result["provider_call_count"] == 4


def test_generalization_status_and_evidence_are_read_only(tmp_path: Path) -> None:
    path = _measurement_profile(tmp_path / "external")
    profile, _ = _applied_stopped(path)
    before = series_evidence_report(profile, "series-a")
    assert before["generalization_measurement"] is None
    assert series_status(profile, "series-a") == {
        **{
            key: value
            for key, value in series_status(profile, "series-a").items()
            if key
            not in {
                "generalization_state",
                "generalization_case_count",
                "generalization_consumed",
            }
        },
        "generalization_state": "available",
        "generalization_case_count": 1,
        "generalization_consumed": False,
    }

    measured = generalize_series(profile, "series-a")
    ledger = (
        profile.base.data_home
        / "series"
        / "series-a"
        / "generalization-calls.json"
    )
    ledger_before = ledger.read_bytes()

    status = series_status(profile, "series-a")
    evidence = series_evidence_report(profile, "series-a")

    assert status["generalization_state"] == "measured"
    assert status["generalization_consumed"] is True
    assert "dimensions" not in status
    assert evidence["generalization_measurement"]["record_digest"] == measured[
        "record_digest"
    ]
    assert evidence["generalization_measurement"]["dimensions"] == measured[
        "dimensions"
    ]
    assert ledger.read_bytes() == ledger_before
