from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign import campaign_status, run_campaign
from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import build_series_contract, initialize_series
from kpo.external_promotion import preview_campaign_promotion
from test_campaign import _add_observers, _initialize, _working_profile


def _add_differential(profile: Path, *, fail_role: str | None = None) -> tuple[Path, Path]:
    root = profile.parent
    capture = root / "runtime" / "captures" / "differential.jsonl"
    proposer_capture = root / "runtime" / "captures" / "proposer.json"
    capture.parent.mkdir(parents=True, exist_ok=True)

    extractor = root / "bin" / "extractor"
    extractor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin); q=r['request']; "
        f"p=Path({str(capture)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        + ("sys.exit(3)\n" if fail_role == "extractor" else "")
        + "prov=q['source_kind']; sid=q['sources'][0]['source_id']; cid='reference-claim' if prov=='reference_artifact' else 'agent-claim'\n"
        "json.dump({'schema_version':'v1','claims':[{'claim_id':cid,'claim_type':'analytical','text':'private '+prov,'evidence_citations':[sid],'prerequisite_claim_ids':[],'confidence':0.8,'salience':0.7,'provenance':prov}]},sys.stdout)\n",
        encoding="utf-8",
    )
    extractor.chmod(0o755)
    differ = root / "bin" / "differ"
    differ.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin); "
        f"p=Path({str(capture)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        + ("sys.exit(3)\n" if fail_role == "differ" else "")
        + "json.dump({'schema_version':'v1','entries':[{'kind':'matched','reference_claim_id':'reference-claim','agent_claim_id':'agent-claim','summary':'same','confidence':0.9}]},sys.stdout)\n",
        encoding="utf-8",
    )
    differ.chmod(0o755)
    diagnostician = root / "bin" / "diagnostician"
    diagnostician.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin); "
        f"p=Path({str(capture)!r}); p.write_text((p.read_text() if p.exists() else '')+json.dumps(r,sort_keys=True)+'\\n')\n"
        + ("sys.exit(3)\n" if fail_role == "diagnostician" else "")
        + "axis=lambda a,s:{'assessment':a,'confidence':0.8,'summary':s}\n"
        "json.dump({'schema_version':'v1','reference_is_standard':False,'conclusion_validity':axis('challenged','evaluation challenges it'),'reasoning_fidelity':axis('supported','structure matches'),'reasoning_soundness':axis('supported','chain is sound'),'gaps':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    diagnostician.chmod(0o755)
    proposer = root / "bin" / "proposer"
    proposer.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\nfrom pathlib import Path\n"
        "r=json.load(sys.stdin); q=r['request']; "
        f"Path({str(proposer_capture)!r}).write_text(json.dumps(q,sort_keys=True))\n"
        "json.dump({'mutation':'add','artifact_id':'improved-rule','content':'Improved rule.','expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    proposer.chmod(0o755)
    with profile.open("a", encoding="utf-8") as stream:
        stream.write(
            '''
[differential_analyzer.extractor]
adapter = "command"
command = ["./bin/extractor"]
model_id = "extractor-v1"
timeout_seconds = 10
pass_env = []

[differential_analyzer.differ]
adapter = "command"
command = ["./bin/differ"]
model_id = "differ-v1"
timeout_seconds = 10
pass_env = []

[differential_analyzer.diagnostician]
adapter = "command"
command = ["./bin/diagnostician"]
model_id = "diagnostician-v1"
timeout_seconds = 10
pass_env = []
'''
        )
    return capture, proposer_capture


def test_configured_campaign_runs_train_only_differential_and_persists_evidence(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    capture, proposer_capture = _add_differential(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="differential-campaign",
    )

    assert result["state"] == "promotion_previewed"
    assert result["differential_analysis_summary"]["attempted_count"] == 1
    assert result["differential_analysis_summary"]["available_count"] == 1
    assert result["differential_analysis_summary"]["kind_counts"]["matched"] == 1
    proposer = json.loads(proposer_capture.read_text())
    assert proposer["differential_diagnosis"]["reference_is_standard"] is False
    requests = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [row["role"] for row in requests] == [
        "reasoning_extractor",
        "reasoning_extractor",
        "claim_differ",
        "differential_diagnostician",
    ]
    serialized = json.dumps(requests)
    assert "Expected 0." in serialized
    assert "Expected 1." not in serialized
    assert "Expected 2." not in serialized
    assert "Expected 3." not in serialized
    root = profile.parent / "runtime" / "campaigns" / "differential-campaign"
    assert (root / "differential" / "iteration-0000.json").is_file()
    summary = json.loads((root / "differential-analysis-summary.json").read_text())
    assert "private agent_rollout" not in json.dumps(summary)
    assert "reference-claim" not in json.dumps(summary)
    bundle_root = profile.parent / "runtime" / "promotions" / "previews" / "differential-campaign"
    bundle = json.loads((bundle_root / "bundle.json").read_text())
    assert bundle["protocol"] == "kpo.external-promotion.v3"
    assert bundle["differential_analysis_digest"] == result["differential_analysis_digest"]
    assert (bundle_root / "differential-analysis-summary.json").read_bytes() == (
        root / "differential-analysis-summary.json"
    ).read_bytes()


def test_failed_differential_provider_is_unavailable_and_proposer_falls_back(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    _, proposer_capture = _add_differential(profile, fail_role="differ")
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="failed-differential",
    )

    assert result["state"] == "promotion_previewed"
    summary = result["differential_analysis_summary"]
    assert summary["attempted_count"] == 1
    assert summary["available_count"] == 0
    assert summary["unavailable_count"] == 1
    assert summary["failure_kind_counts"] == {"non_zero_exit": 1}
    proposer = json.loads(proposer_capture.read_text())
    assert "differential_diagnosis" not in proposer


def test_campaign_status_rejects_tampered_differential_summary(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    _add_differential(profile)
    _initialize(profile)
    run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="tamper-summary",
    )
    summary_path = (
        profile.parent
        / "runtime"
        / "campaigns"
        / "tamper-summary"
        / "differential-analysis-summary.json"
    )
    summary = json.loads(summary_path.read_text())
    summary["available_count"] = 9
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="digest"):
        campaign_status(
            profile,
            "tamper-summary",
            checkout=Path(__file__).parents[1],
        )


def test_differential_calls_obey_existing_campaign_budget(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _add_differential(profile)
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            "max_provider_calls = 100", "max_provider_calls = 5"
        ),
        encoding="utf-8",
    )
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="differential-budget",
    )

    assert result["state"] == "budget_exhausted"
    assert result["call_count"] == 5
    assert result["differential_analysis_summary"]["available_count"] == 0
    assert result["differential_analysis_summary"]["attempted_count"] == 1
    assert result["differential_analysis_summary"]["unavailable_count"] == 1
    assert result["differential_analysis_summary"]["failure_kind_counts"] == {
        "budget_exhausted": 1
    }


def test_configured_series_uses_safe_contract_and_evidence_v2(tmp_path: Path) -> None:
    profile_path = _working_profile(tmp_path / "external")
    _add_differential(profile_path)
    with profile_path.open("a", encoding="utf-8") as stream:
        stream.write("\n[series]\nmax_campaigns = 2\n")
    _initialize(profile_path)
    profile = load_campaign_profile(
        profile_path, checkout=Path(__file__).parents[1]
    )
    contract = build_series_contract(profile)
    serialized_contract = json.dumps(contract.components, sort_keys=True)
    initialize_series(profile, "differential-series")

    result = run_campaign(
        profile_path,
        checkout=Path(__file__).parents[1],
        campaign_id="differential-series-campaign",
        series_id="differential-series",
    )

    assert set(contract.components["differential_analyzer"]) == {
        "extractor",
        "differ",
        "diagnostician",
    }
    assert str(tmp_path) not in serialized_contract
    evidence = json.loads(
        (profile.base.data_home / result["series_evidence_path"]).read_text()
    )
    assert evidence["protocol"] == "kpo.campaign-series-evidence/v2"
    assert evidence["differential_summary_byte_digest"] == result[
        "differential_analysis_digest"
    ]
    assert evidence["differential_summary_record_digest"] == result[
        "differential_analysis_summary"
    ]["record_digest"]
    assert "private" not in json.dumps(evidence).lower()


def test_observer_and_differential_promotion_bindings_coexist(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _add_observers(profile)
    _add_differential(profile)
    _initialize(profile)

    result = run_campaign(
        profile,
        checkout=Path(__file__).parents[1],
        campaign_id="combined-evidence",
    )

    assert result["state"] == "promotion_previewed"
    bundle_root = (
        profile.parent / "runtime" / "promotions" / "previews" / "combined-evidence"
    )
    bundle = json.loads((bundle_root / "bundle.json").read_text())
    assert bundle["protocol"] == "kpo.external-promotion.v4"
    assert (bundle_root / "evaluator-agreement.json").is_file()
    assert (bundle_root / "differential-analysis-summary.json").is_file()
    preview = preview_campaign_promotion(
        profile,
        "combined-evidence",
        checkout=Path(__file__).parents[1],
    )
    assert preview["evaluator_agreement_digest"] == result[
        "evaluator_agreement_digest"
    ]
    assert preview["differential_analysis_digest"] == result[
        "differential_analysis_digest"
    ]
