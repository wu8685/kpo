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


def test_campaign_passes_to_preview_without_writing_target(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _initialize(profile)
    policy_before = (profile.parent / "policy" / "base.md").read_bytes()

    result = run_campaign(profile, checkout=Path(__file__).parents[1])

    assert result["state"] == "promotion_previewed"
    assert result["report_passed"] is True
    assert result["call_count"] > 0
    assert "Improved rule" in result["promotion_diff"]
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
