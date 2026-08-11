from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign import (
    CampaignInterrupted,
    campaign_status,
    initialize_campaign_dataset,
    run_campaign,
)
from kpo.cli import main
from test_campaign_profile import _campaign_profile


def _working_profile(root: Path, *, candidate_score: float = 1.0) -> Path:
    profile = _campaign_profile(root)
    actor = root / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "r=json.load(sys.stdin)['request']\n"
        "policy=' '.join(x['content'] for x in r['policy_artifacts'])\n"
        "json.dump({'output':'improved' if 'Improved rule' in policy else 'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    evaluator = root / "bin" / "evaluator"
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "r=json.load(sys.stdin)['request']; improved=r['rollout']['output']=='improved'\n"
        f"score={candidate_score!r} if improved else 0.2\n"
        "rollout=r['rollout']['digest']; ref=r['references'][0]['artifact_id']\n"
        "signals=[] if improved else [{'kind':'missing_policy_knowledge','message':'missing rule','citations':[rollout,ref]}]\n"
        "json.dump({'dimensions':[{'name':'alignment','score':score,'explanation':'measured','citations':[rollout,ref]}],'failure_signals':signals,'aggregate_score':score},sys.stdout)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)
    proposer = root / "bin" / "proposer"
    proposer.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "r=json.load(sys.stdin); assert 'holdout' not in json.dumps(r).lower()\n"
        "json.dump({'mutation':'add','artifact_id':'improved-rule','content':'Improved rule.','expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    proposer.chmod(0o755)
    return profile


def _initialize(profile: Path) -> None:
    preview = initialize_campaign_dataset(
        profile, checkout=Path(__file__).parents[1]
    )
    initialize_campaign_dataset(
        profile,
        checkout=Path(__file__).parents[1],
        approval_digest=preview["approval_digest"],
    )


def _add_observers(profile: Path) -> tuple[Path, Path, Path]:
    root = profile.parent
    capture_root = root / "runtime" / "captures"
    capture_root.mkdir(parents=True)
    offset_capture = capture_root / "offset-requests.jsonl"
    failure_capture = capture_root / "failure-requests.jsonl"
    proposer_capture = capture_root / "proposer-request.json"
    offset = root / "bin" / "observer-offset"
    offset.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "from pathlib import Path\n"
        "r=json.load(sys.stdin)['request']\n"
        f"p=Path({str(offset_capture)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        "improved=r['rollout']['output']=='improved'; score=1.0 if improved else 0.4\n"
        "rollout=r['rollout']['digest']; ref=r['references'][0]['artifact_id']\n"
        "json.dump({'dimensions':[{'name':'alignment','score':score,'explanation':'observer','citations':[rollout,ref]}],'failure_signals':[],'aggregate_score':score},sys.stdout)\n",
        encoding="utf-8",
    )
    offset.chmod(0o755)
    failure = root / "bin" / "observer-failure"
    failure.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "from pathlib import Path\n"
        "r=json.load(sys.stdin)['request']\n"
        f"p=Path({str(failure_capture)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        "sys.exit(3) if r['case_id']=='case-2' else None\n"
        "improved=r['rollout']['output']=='improved'; score=1.0 if improved else 0.2\n"
        "rollout=r['rollout']['digest']; ref=r['references'][0]['artifact_id']\n"
        "json.dump({'dimensions':[{'name':'alignment','score':score,'explanation':'observer','citations':[rollout,ref]}],'failure_signals':[],'aggregate_score':score},sys.stdout)\n",
        encoding="utf-8",
    )
    failure.chmod(0o755)
    proposer = root / "bin" / "proposer"
    proposer.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "from pathlib import Path\n"
        "r=json.load(sys.stdin)\n"
        f"Path({str(proposer_capture)!r}).write_text(json.dumps(r,sort_keys=True))\n"
        "assert 'holdout' not in json.dumps(r).lower()\n"
        "json.dump({'mutation':'add','artifact_id':'improved-rule','content':'Improved rule.','expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    proposer.chmod(0o755)
    with profile.open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[[observer_evaluators]]
name = "observer-offset"
adapter = "command"
command = ["./bin/observer-offset"]
evaluator_id = "offset-model"
timeout_seconds = 10
pass_env = []

[[observer_evaluators]]
name = "observer-failure"
adapter = "command"
command = ["./bin/observer-failure"]
evaluator_id = "failure-model"
timeout_seconds = 10
pass_env = []
'''
        )
    return offset_capture, failure_capture, proposer_capture


def test_campaign_passes_to_preview_without_writing_target(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)
    policy_before = (profile.parent / "policy" / "base.md").read_bytes()

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "promotion_previewed"
    assert result["report_passed"] is True
    assert result["call_count"] > 0
    assert "Improved rule" in result["promotion_diff"]
    assert "new-rule" not in result["promotion_diff"]
    assert "improved-rule" in result["manifest_diff"]
    assert result["promotion_state"] == "previewed"
    assert "approval_digest" not in result
    assert result["candidate_policy_digest"]
    bundle_path = profile.parent / "runtime" / result["bundle_path"]
    assert bundle_path.name == "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["schema_version"] == 1
    assert bundle["campaign_id"] == result["campaign_id"]
    assert bundle["candidate"]["artifact_id"] == "improved-rule"
    assert bundle["candidate_digest"] == result["candidate_digest"]
    assert bundle["candidate_policy_digest"] == result["candidate_policy_digest"]
    assert bundle["target_relative_path"] == "policies/improved-rule.md"
    assert bundle["manifest_relative_path"] == "policies/improved-rule.md"
    assert "approval_digest" not in bundle
    bundle_dir = bundle_path.parent
    assert not (bundle_dir / "original-content.bin").exists()
    assert (bundle_dir / "reviewed-content.bin").read_bytes() == b"Improved rule."
    assert (bundle_dir / "original-manifest.jsonl").read_bytes() == (
        profile.parent / "policy.jsonl"
    ).read_bytes()
    assert b"improved-rule" in (bundle_dir / "reviewed-manifest.jsonl").read_bytes()
    assert not (profile.parent / "policies" / "improved-rule.md").exists()
    assert (profile.parent / "policy" / "base.md").read_bytes() == policy_before
    status = campaign_status(
        profile,
        result["campaign_id"],
        checkout=Path(__file__).parents[1],
    )
    assert status["state"] == "promotion_previewed"


def test_repeated_rejected_candidate_stops_without_changing_parent(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external", candidate_score=0.25)
    _initialize(profile)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "no_patchable_diagnosis"
    assert result["rejected_candidate_count"] == 1
    assert not (profile.parent / "policies" / "improved-rule.md").exists()


def test_provider_call_budget_stops_campaign(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    text = profile.read_text(encoding="utf-8").replace(
        "max_provider_calls = 100", "max_provider_calls = 1"
    )
    profile.write_text(text, encoding="utf-8")
    _initialize(profile)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])
    assert result["state"] == "budget_exhausted"
    assert result["call_count"] == 1


def test_observers_produce_isolated_agreement_report_without_changing_gates(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    offset_capture, failure_capture, proposer_capture = _add_observers(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="observer-campaign",
    )

    assert result["state"] == "promotion_previewed"
    assert result["evaluator_agreement_digest"]
    assert result["evaluator_agreement_summary"]["observer_count"] == 2
    report_path = profile.parent / "runtime" / result["evaluator_agreement_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["protocol"] == "kpo.evaluator-agreement/v1"
    assert report["collection_state"] == "complete"
    assert report["event_count"] == 7
    assert report["event_counts"] == {
        "holdout": 2,
        "regression": 2,
        "train": 1,
        "validation": 2,
    }
    assert report["availability"] == {
        "observer-failure": {"non_zero_exit": 2}
    }
    assert "case-" not in json.dumps(report)
    for capture in (offset_capture, failure_capture):
        requests = [json.loads(line) for line in capture.read_text().splitlines()]
        assert requests
        assert all("dimensions" not in request for request in requests)
        assert all("promotion" not in json.dumps(request) for request in requests)
        assert all("observer-offset" not in json.dumps(request) for request in requests)
        assert all("observer-failure" not in json.dumps(request) for request in requests)
    proposer_request = proposer_capture.read_text(encoding="utf-8")
    assert "observer-offset" not in proposer_request
    assert "observer-failure" not in proposer_request
    assert "evaluator_agreement" not in proposer_request


def test_observer_budget_exhaustion_records_unattempted_slots(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _add_observers(profile)
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            "max_provider_calls = 100", "max_provider_calls = 2"
        ),
        encoding="utf-8",
    )
    _initialize(profile)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "budget_exhausted"
    report_path = profile.parent / "runtime" / result["evaluator_agreement_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["collection_state"] == "budget_exhausted"
    assert report["event_count"] == 1
    assert report["availability"] == {
        "observer-failure": {"not_attempted_budget_exhausted": 1},
        "observer-offset": {"not_attempted_budget_exhausted": 1},
    }


def test_repeated_observer_campaign_candidate_persists_complete_report(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external", candidate_score=0.25)
    _add_observers(profile)
    _initialize(profile)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "no_patchable_diagnosis"
    report = json.loads(
        (profile.parent / "runtime" / result["evaluator_agreement_path"]).read_text()
    )
    assert report["collection_state"] == "complete"


def test_evaluator_agreement_cli_is_read_only_and_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _working_profile(tmp_path / "external")
    _add_observers(profile)
    _initialize(profile)
    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="audit-cli",
    )
    data_home = profile.parent / "runtime"
    report_path = data_home / result["evaluator_agreement_path"]
    state_path = data_home / "campaigns" / "audit-cli" / "campaign.json"
    protected = {
        path: path.read_bytes()
        for path in (
            report_path,
            state_path,
            profile.parent / "policy.jsonl",
            profile.parent / "dataset.jsonl",
            profile.parent / "policy" / "base.md",
        )
    }

    assert (
        main(
            [
                "evaluator-agreement",
                "--profile",
                str(profile),
                "--campaign",
                "audit-cli",
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["report_digest"]
    assert all(path.read_bytes() == content for path, content in protected.items())

    original_report = json.loads(report_path.read_text(encoding="utf-8"))
    report_path.write_text(
        json.dumps(original_report | {"unexpected": True}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown agreement report fields"):
        main(
            [
                "evaluator-agreement",
                "--profile",
                str(profile),
                "--campaign",
                "audit-cli",
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )
    report_path.write_bytes(protected[report_path])

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_path.write_text(
        json.dumps(state | {"evaluator_agreement_path": "../escape.json"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="path"):
        main(
            [
                "evaluator-agreement",
                "--profile",
                str(profile),
                "--campaign",
                "audit-cli",
                "--checkout",
                str(Path(__file__).parents[1]),
            ]
        )
    state_path.write_bytes(protected[state_path])


@pytest.mark.parametrize("max_calls", [100, 2])
def test_agreement_persistence_failure_prevents_clean_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_calls: int,
) -> None:
    from kpo.evaluator_agreement import AgreementPersistenceError

    profile = _working_profile(tmp_path / f"external-{max_calls}")
    _add_observers(profile)
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            "max_provider_calls = 100", f"max_provider_calls = {max_calls}"
        ),
        encoding="utf-8",
    )
    _initialize(profile)

    def fail_persistence(*args: object, **kwargs: object) -> dict[str, object]:
        raise AgreementPersistenceError("injected")

    monkeypatch.setattr("kpo.campaign.persist_agreement_report", fail_persistence)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "failed"
    assert result["failure_kind"] == "evaluator_agreement_persistence"


def test_multi_iteration_resume_restores_prior_observer_events_from_checkpoint(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external", candidate_score=0.25)
    _add_observers(profile)
    _initialize(profile)
    with pytest.raises(CampaignInterrupted) as interrupted:
        run_campaign(
            profile,
            checkout=Path(__file__).parents[1],
            campaign_id="checkpoint-resume",
            fault_after_rejection=True,
        )
    campaign_id = interrupted.value.campaign_id
    checkpoint = (
        profile.parent
        / "runtime"
        / "campaigns"
        / campaign_id
        / "evaluator-agreement.checkpoint.json"
    )
    assert checkpoint.is_file()

    resumed = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id=campaign_id,
        resume=True,
    )

    assert resumed["state"] == "no_patchable_diagnosis"
    report = json.loads(
        (profile.parent / "runtime" / resumed["evaluator_agreement_path"]).read_text()
    )
    assert report["event_count"] == 7
    assert report["event_counts"] == {
        "holdout": 2,
        "regression": 2,
        "train": 1,
        "validation": 2,
    }
    assert not checkpoint.exists()


def test_missing_observer_checkpoint_makes_resume_a_typed_failure(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external", candidate_score=0.25)
    _add_observers(profile)
    _initialize(profile)
    with pytest.raises(CampaignInterrupted) as interrupted:
        run_campaign(
            profile,
            checkout=Path(__file__).parents[1],
            campaign_id="missing-checkpoint",
            fault_after_rejection=True,
        )
    campaign_id = interrupted.value.campaign_id
    checkpoint = (
        profile.parent
        / "runtime"
        / "campaigns"
        / campaign_id
        / "evaluator-agreement.checkpoint.json"
    )
    checkpoint.unlink()

    resumed = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id=campaign_id,
        resume=True,
    )

    assert resumed["state"] == "failed"
    assert resumed["failure_kind"] == "evaluator_agreement_checkpoint"


def test_checkpoint_initialization_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpo.evaluator_agreement import AgreementCheckpointError

    profile = _working_profile(tmp_path / "external")
    _add_observers(profile)
    _initialize(profile)

    def fail_checkpoint(*args: object, **kwargs: object) -> Path:
        raise AgreementCheckpointError("injected")

    monkeypatch.setattr("kpo.campaign.persist_agreement_checkpoint", fail_checkpoint)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "failed"
    assert result["failure_kind"] == "evaluator_agreement_checkpoint"


def test_campaign_rejects_unsafe_explicit_campaign_id(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)

    with pytest.raises(ValueError, match="campaign ID"):
        run_campaign(
            profile,
            checkout=Path(__file__).parents[1],
            campaign_id="../escape",
        )


def test_campaign_rejects_profile_holdout_digest_mismatch(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)
    text = profile.read_text(encoding="utf-8").replace(
        'manifest = "./dataset.jsonl"',
        'manifest = "./dataset.jsonl"\n'
        f'frozen_holdout_digest = "sha256:{"0" * 64}"',
    )
    profile.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="frozen_holdout_digest"):
        run_campaign(profile, checkout=Path(__file__).parents[1])


def test_failed_campaign_resume_reuses_completed_calls(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)
    with pytest.raises(CampaignInterrupted) as captured:
        run_campaign(
            profile,
            checkout=Path(__file__).parents[1],
            fault_after_train=True,
        )
    campaign_id = captured.value.campaign_id
    failed = campaign_status(
        profile, campaign_id, checkout=Path(__file__).parents[1]
    )
    assert failed["state"] == "failed"
    calls_after_failure = failed["call_count"]

    resumed = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id=campaign_id,
        resume=True,
    )
    assert resumed["state"] == "promotion_previewed"
    # Train calls are cache hits on resume; only later stages add calls.
    assert resumed["call_count"] > calls_after_failure
    assert resumed["call_count"] < calls_after_failure * 2 + 20


def test_campaign_resume_finalizes_installed_bundle_without_provider_calls(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)
    with pytest.raises(CampaignInterrupted) as captured:
        run_campaign(
            profile,
            checkout=Path(__file__).parents[1],
            fault_after_bundle=True,
        )
    campaign_id = captured.value.campaign_id
    data_home = profile.parent / "runtime"
    budget_path = data_home / "campaigns" / campaign_id / "call-budget.json"
    budget_before = budget_path.read_bytes()
    interrupted = campaign_status(
        profile, campaign_id, checkout=Path(__file__).parents[1]
    )
    assert interrupted["state"] == "failed"
    assert (data_home / "promotions" / "previews" / campaign_id / "bundle.json").is_file()

    resumed = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id=campaign_id,
        resume=True,
    )

    assert budget_path.read_bytes() == budget_before
    assert resumed["state"] == "promotion_previewed"
    assert resumed["promotion_state"] == "previewed"
    assert resumed["call_count"] == json.loads(budget_before)["call_count"]
    assert resumed["promotion_diff"]
    assert resumed["manifest_diff"]
    assert resumed["bundle_path"]
    assert "approval_digest" not in resumed


def test_campaign_cli_initializes_runs_and_reports_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _working_profile(tmp_path / "external")
    checkout = str(Path(__file__).parents[1])
    assert main(
        ["validate-profile", "--profile", str(profile), "--checkout", checkout]
    ) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["dataset_case_count"] == 4
    assert validated["sandbox"] == "none"
    assert main(
        ["dataset", "init", "--profile", str(profile), "--checkout", checkout]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["state"] == "preview"
    assert main(
        [
            "dataset",
            "init",
            "--profile",
            str(profile),
            "--approve",
            preview["approval_digest"],
            "--checkout",
            checkout,
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        ["campaign", "--profile", str(profile), "--checkout", checkout]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "promotion_previewed"
    assert main(
        [
            "campaign-status",
            "--profile",
            str(profile),
            "--campaign",
            result["campaign_id"],
            "--checkout",
            checkout,
        ]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "promotion_previewed"
    assert status["promotion_state"] == "previewed"
    assert status["promotion_diff"]
    assert status["manifest_diff"]
    assert status["bundle_path"]
    assert "approval_digest" not in status


def test_campaign_evaluates_all_train_cases_before_selecting_diagnosis(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    root = profile.parent
    case = {
        "case_id": "case-5",
        "prompt": "Analyze additional train scenario.",
        "visible_context": ["Additional visible fact."],
        "hidden_reference_ids": ["ref-5"],
        "tags": ["train"],
        "information_cutoff": None,
    }
    with (root / "cases.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(case) + "\n")
    (root / "references" / "ref-5.md").write_text("Expected 5.\n", encoding="utf-8")
    with (root / "references.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "artifact_id": "ref-5",
                    "kind": "expected",
                    "path": "references/ref-5.md",
                }
            )
            + "\n"
        )
    with (root / "dataset.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "case_id": "case-5",
                    "partition": "train",
                    "weight": 1.0,
                    "provenance": ["source-train-2"],
                }
            )
            + "\n"
        )
    _initialize(profile)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])
    assert result["state"] == "promotion_previewed"
    assert result["call_count"] == 17
