from __future__ import annotations

from pathlib import Path

import pytest

from kpo.campaign import initialize_campaign_dataset, run_campaign
from kpo.integrity import IntegrityViolation
from kpo.runner import run_profile
from test_campaign import _working_profile
from test_external_cli import _operational_profile


def _init(profile: Path) -> None:
    preview = initialize_campaign_dataset(
        profile, checkout=Path(__file__).parents[1]
    )
    initialize_campaign_dataset(
        profile,
        checkout=Path(__file__).parents[1],
        approval_digest=preview["approval_digest"],
    )


def test_v02_run_detects_provider_source_drift_immediately(tmp_path: Path) -> None:
    profile = _operational_profile(tmp_path / "external")
    policy = profile.parent / "policy" / "base.md"
    actor = profile.parent / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,pathlib,sys\n"
        f"pathlib.Path({str(policy)!r}).write_text('tampered')\n"
        "json.load(sys.stdin); json.dump({'output':'result'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)

    with pytest.raises(IntegrityViolation, match="source_drift"):
        run_profile(profile, "case-1", checkout=Path(__file__).parents[1])


def test_campaign_detects_source_and_target_drift_after_provider_call(
    tmp_path: Path,
) -> None:
    source_profile = _working_profile(tmp_path / "source-drift")
    _init(source_profile)
    policy = source_profile.parent / "policy" / "base.md"
    actor = source_profile.parent / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,pathlib,sys\n"
        f"pathlib.Path({str(policy)!r}).write_text('tampered')\n"
        "json.load(sys.stdin); json.dump({'output':'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    source_result = run_campaign(
        source_profile, checkout=Path(__file__).parents[1]
    )
    assert source_result["state"] == "failed"
    assert source_result["failure_kind"] == "source_drift"

    target_profile = _working_profile(tmp_path / "target-drift")
    _init(target_profile)
    intrusion = target_profile.parent / "policies" / "intrusion.md"
    actor = target_profile.parent / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,pathlib,sys\n"
        f"p=pathlib.Path({str(intrusion)!r}); p.parent.mkdir(); p.write_text('intrusion')\n"
        "json.load(sys.stdin); json.dump({'output':'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    target_result = run_campaign(
        target_profile, checkout=Path(__file__).parents[1]
    )
    assert target_result["state"] == "failed"
    assert target_result["failure_kind"] == "target_drift"


def test_provider_cwd_is_under_data_home_and_not_target(tmp_path: Path) -> None:
    profile = _working_profile(tmp_path / "external")
    _init(profile)
    actor = profile.parent / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,pathlib,sys\n"
        "pathlib.Path('adapter-scratch.txt').write_text('ok')\n"
        "r=json.load(sys.stdin)['request']; policy=' '.join(x['content'] for x in r['policy_artifacts'])\n"
        "json.dump({'output':'improved' if 'Improved rule' in policy else 'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])
    assert result["state"] == "promotion_previewed"
    scratch = profile.parent / "runtime" / "provider-work" / "actor" / "adapter-scratch.txt"
    assert scratch.read_text() == "ok"
    assert result["sandbox"] == "none"


def test_all_campaign_provider_requests_and_env_hide_profile_paths(
    tmp_path: Path,
) -> None:
    profile = _working_profile(tmp_path / "external")
    _init(profile)
    root = str(profile.parent)
    actor = profile.parent / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "e=json.load(sys.stdin); s=json.dumps(e)\n"
        f"assert {root!r} not in s; assert all({root!r} not in v for v in os.environ.values())\n"
        "r=e['request']; policy=' '.join(x['content'] for x in r['policy_artifacts'])\n"
        "json.dump({'output':'improved' if 'Improved rule' in policy else 'baseline'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    evaluator = profile.parent / "bin" / "evaluator"
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "e=json.load(sys.stdin); s=json.dumps(e)\n"
        f"assert {root!r} not in s; assert all({root!r} not in v for v in os.environ.values())\n"
        "r=e['request']; improved=r['rollout']['output']=='improved'; score=1.0 if improved else 0.2\n"
        "rollout=r['rollout']['digest']; ref=r['references'][0]['artifact_id']\n"
        "signals=[] if improved else [{'kind':'missing_policy_knowledge','message':'missing','citations':[rollout,ref]}]\n"
        "json.dump({'dimensions':[{'name':'alignment','score':score,'explanation':'measured','citations':[rollout,ref]}],'failure_signals':signals,'aggregate_score':score},sys.stdout)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)
    proposer = profile.parent / "bin" / "proposer"
    proposer.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "e=json.load(sys.stdin); s=json.dumps(e)\n"
        f"assert {root!r} not in s; assert all({root!r} not in v for v in os.environ.values())\n"
        "json.dump({'mutation':'add','artifact_id':'improved-rule','content':'Improved rule.','expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
        encoding="utf-8",
    )
    proposer.chmod(0o755)

    result = run_campaign(profile, checkout=Path(__file__).parents[1])
    assert result["state"] == "promotion_previewed"
