from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign import run_campaign
from kpo.campaign_profile import (
    campaign_integrity_monitor,
    campaign_profile_snapshot,
    load_campaign_profile,
)
from kpo.evaluator_audit_runtime import run_evaluator_calibration
from kpo.evaluator_calibration import (
    apply_anchor_approval,
    preview_anchor_approval,
)
from test_campaign import _initialize, _working_profile
from test_evaluator_calibration import _add_evaluator_audit


def _capture_providers(profile: Path) -> dict[str, Path]:
    root = profile.parent
    captures = {
        role: root / "runtime" / "captures" / f"{role}.jsonl"
        for role in ("actor", "evaluator", "proposer")
    }
    for path in captures.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    actor = root / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin)['request']\n"
        f"p=Path({str(captures['actor'])!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        "policy=' '.join(x['content'] for x in r['policy_artifacts'])\n"
        "json.dump({'output':'improved' if 'Improved rule' in policy else 'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    evaluator = root / "bin" / "evaluator"
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin)['request']\n"
        f"p=Path({str(captures['evaluator'])!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        "anchor='random_seed' in r; improved=r['rollout']['output']=='improved'\n"
        "score=0.8 if anchor else (1.0 if improved else 0.2)\n"
        "rollout=r['rollout']['digest']; ref=r['references'][0]['artifact_id']\n"
        "signals=[] if (anchor or improved) else [{'kind':'missing_policy_knowledge','message':'missing rule','citations':[rollout,ref]}]\n"
        "json.dump({'dimensions':[{'name':'alignment','score':score,'explanation':'measured','citations':[rollout,ref]}],'failure_signals':signals,'aggregate_score':score},sys.stdout)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)
    proposer = root / "bin" / "proposer"
    proposer.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin)['request']\n"
        f"p=Path({str(captures['proposer'])!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        "json.dump({'mutation':'add','artifact_id':'improved-rule','content':'Improved rule.','expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    proposer.chmod(0o755)
    return captures


def _approve(profile: Path) -> None:
    checkout = Path(__file__).parents[1]
    preview = preview_anchor_approval(profile, checkout=checkout)
    apply_anchor_approval(
        profile, checkout=checkout, approval_digest=preview["approval_digest"]
    )


def _requests(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_campaign_requires_anchor_approval_before_any_provider_call(
    tmp_path: Path,
) -> None:
    profile = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="missing-anchor-approval",
    )

    assert result["state"] == "failed"
    assert result["failure_kind"] == "evaluator_calibration_approval"
    assert all(not path.exists() for path in captures.values())


def test_campaign_runs_fresh_anchor_audit_and_binds_safe_summary(
    tmp_path: Path,
) -> None:
    profile = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile)
    _approve(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="calibrated-campaign",
    )

    assert result["state"] == "promotion_previewed"
    assert result["evaluator_calibration_digest"]
    assert result["evaluator_calibration_summary"]["anchor_count"] == 2
    assert result["evaluator_calibration_summary"]["collection_state"] == "complete"
    data_home = profile.parent / "runtime"
    budget = json.loads(
        (
            data_home
            / "campaigns"
            / "calibrated-campaign"
            / "evaluator-audit-call-budget.json"
        ).read_text()
    )
    assert budget["call_count"] == 2
    evaluator_requests = _requests(captures["evaluator"])
    anchor_requests = [row for row in evaluator_requests if "random_seed" in row]
    optimization_requests = [
        row for row in evaluator_requests if "random_seed" not in row
    ]
    assert len(anchor_requests) == 2
    assert optimization_requests
    assert all(row["random_seed"] == 0 for row in anchor_requests)
    assert "expected_scores" not in json.dumps(anchor_requests)
    assert "provenance" not in json.dumps(anchor_requests)
    assert not (
        data_home
        / "campaigns"
        / "calibrated-campaign"
        / "evaluator-calibration.checkpoint.json"
    ).exists()
    assert "expected_scores" not in json.dumps(result, sort_keys=True)


def test_audit_budget_exhaustion_is_visible_but_does_not_change_optimization(
    tmp_path: Path,
) -> None:
    control = _working_profile(tmp_path / "control")
    control_captures = _capture_providers(control)
    _initialize(control)
    control_result = run_campaign(
        control,
        checkout=Path(__file__).parents[1],
        campaign_id="control-campaign",
    )

    audited = _add_evaluator_audit(_working_profile(tmp_path / "audited"))
    audited.write_text(
        audited.read_text().replace(
            "[evaluator_audit]\nanchor_manifest = \"./anchors.jsonl\"\nmax_provider_calls = 5",
            "[evaluator_audit]\nanchor_manifest = \"./anchors.jsonl\"\nmax_provider_calls = 1",
        ),
        encoding="utf-8",
    )
    audited_captures = _capture_providers(audited)
    _approve(audited)
    _initialize(audited)
    audited_result = run_campaign(
        audited,
        checkout=Path(__file__).parents[1],
        campaign_id="audited-campaign",
    )

    assert audited_result["state"] == control_result["state"] == "promotion_previewed"
    assert audited_result["candidate_digest"] == control_result["candidate_digest"]
    assert audited_result["report_passed"] == control_result["report_passed"]
    assert audited_result["call_count"] == control_result["call_count"]
    assert audited_result["evaluator_calibration_summary"][
        "collection_state"
    ] == "budget_exhausted"
    assert _requests(audited_captures["actor"]) == _requests(control_captures["actor"])
    assert _requests(audited_captures["proposer"]) == _requests(
        control_captures["proposer"]
    )
    assert [
        row for row in _requests(audited_captures["evaluator"]) if "random_seed" not in row
    ] == _requests(control_captures["evaluator"])


def test_calibration_resume_converts_uncertain_pending_call_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile_path)
    _approve(profile_path)
    profile = load_campaign_profile(
        profile_path, checkout=Path(__file__).parents[1]
    )
    snapshot = campaign_profile_snapshot(profile)
    monitor = campaign_integrity_monitor(profile)

    def interrupt(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("injected uncertain interruption")

    monkeypatch.setattr(
        "kpo.evaluator_audit_runtime.EvaluatorAuditCallExecutor.invoke", interrupt
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        run_evaluator_calibration(
            profile,
            "resume-calibration",
            profile_snapshot_digest=snapshot,
            integrity_monitor=monitor,
        )

    monkeypatch.undo()
    binding = run_evaluator_calibration(
        profile,
        "resume-calibration",
        profile_snapshot_digest=snapshot,
        integrity_monitor=monitor,
    )
    report = json.loads(
        (
            profile.base.data_home
            / str(binding["evaluator_calibration_path"])
        ).read_text()
    )
    observations = json.loads(
        (
            profile.base.data_home
            / str(binding["evaluator_calibration_observations_path"])
        ).read_text()
    )

    assert report["coverage"]["primary"] == {"completed": 1, "unavailable": 1}
    assert report["unavailable_totals"] == {
        "primary": {"interrupted_unknown": 1}
    }
    assert len(_requests(captures["evaluator"])) == 1
    slot_states = [
        row["slots"]["primary"] for row in observations["observations"]
    ]
    assert sum(state["failure_kind"] == "interrupted_unknown" for state in slot_states if state["state"] == "unavailable") == 1


def test_calibration_provider_failure_is_measurement_unavailability(
    tmp_path: Path,
) -> None:
    profile = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile)
    evaluator = profile.parent / "bin" / "evaluator"
    evaluator.write_text(
        evaluator.read_text().replace(
            "anchor='random_seed' in r; improved=r['rollout']['output']=='improved'",
            "anchor='random_seed' in r; sys.exit(3) if anchor else None; improved=r['rollout']['output']=='improved'",
        ),
        encoding="utf-8",
    )
    _approve(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="failed-measurement",
    )

    assert result["state"] == "promotion_previewed"
    assert result["evaluator_calibration_summary"]["unavailable_totals"] == {
        "primary": {"non_zero_exit": 2}
    }
    assert _requests(captures["actor"])
    assert _requests(captures["proposer"])


def test_calibration_persistence_failure_prevents_optimization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpo.evaluator_calibration_report import EvaluatorCalibrationPersistenceError

    profile = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile)
    _approve(profile)
    _initialize(profile)

    def fail_persistence(*args: object, **kwargs: object) -> dict[str, object]:
        raise EvaluatorCalibrationPersistenceError("injected")

    monkeypatch.setattr(
        "kpo.evaluator_audit_runtime.persist_calibration_artifacts",
        fail_persistence,
    )
    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="calibration-persistence-failure",
    )

    assert result["state"] == "failed"
    assert result["failure_kind"] == "evaluator_calibration_persistence"
    assert not captures["actor"].exists()
    assert not captures["proposer"].exists()
    assert len(_requests(captures["evaluator"])) == 2


def test_partial_calibration_artifacts_recover_from_checkpoint_without_calls(
    tmp_path: Path,
) -> None:
    profile_path = _add_evaluator_audit(_working_profile(tmp_path / "external"))
    captures = _capture_providers(profile_path)
    _approve(profile_path)
    profile = load_campaign_profile(
        profile_path, checkout=Path(__file__).parents[1]
    )
    snapshot = campaign_profile_snapshot(profile)
    monitor = campaign_integrity_monitor(profile)
    first = run_evaluator_calibration(
        profile,
        "partial-artifacts",
        profile_snapshot_digest=snapshot,
        integrity_monitor=monitor,
    )
    report_path = profile.base.data_home / str(first["evaluator_calibration_path"])
    observation_path = profile.base.data_home / str(
        first["evaluator_calibration_observations_path"]
    )
    calls_before = captures["evaluator"].read_bytes()
    report_path.unlink()
    assert observation_path.exists() and not report_path.exists()

    recovered = run_evaluator_calibration(
        profile,
        "partial-artifacts",
        profile_snapshot_digest=snapshot,
        integrity_monitor=monitor,
    )

    assert report_path.exists()
    assert observation_path.exists()
    assert captures["evaluator"].read_bytes() == calls_before
    assert recovered["evaluator_calibration_summary"] == first[
        "evaluator_calibration_summary"
    ]
