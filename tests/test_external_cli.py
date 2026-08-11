from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.cli import main
from kpo.profile import load_profile
from kpo.runner import evaluate_profile_run, run_profile
from test_profile import _write_profile


def _operational_profile(root: Path) -> Path:
    profile = _write_profile(root)
    actor = root / "bin" / "actor"
    actor.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "json.dump({'output':'agent output'},sys.stdout)\n",
        encoding="utf-8",
    )
    actor.chmod(0o755)
    evaluator = root / "bin" / "evaluator"
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "request=json.load(sys.stdin)['request']\n"
        "rollout=request['rollout']['digest']\n"
        "reference=request['references'][0]['artifact_id']\n"
        "json.dump({'dimensions':[{'name':'alignment','score':0.8,"
        "'explanation':'aligned','citations':[rollout,reference]}],"
        "'failure_signals':[],'aggregate_score':0.8},sys.stdout)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)
    return profile


def test_validate_profile_does_not_create_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = _operational_profile(tmp_path / "external")

    assert main(["validate-profile", "--profile", str(profile_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["profile_id"] == "example"
    assert result["case_count"] == 1
    assert not (profile_path.parent / "runtime").exists()


def test_run_evaluate_and_status_work_across_invocations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = _operational_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    before = dict(load_profile(profile_path, checkout=checkout).source_digests)

    assert main(["run", "--profile", str(profile_path), "--case", "case-1"]) == 0
    run_result = json.loads(capsys.readouterr().out)
    run_id = run_result["run_id"]
    assert run_result["state"] == "completed"
    assert "output" not in run_result

    assert main(["evaluate", "--profile", str(profile_path), "--run", run_id]) == 0
    evaluation = json.loads(capsys.readouterr().out)
    assert evaluation["state"] == "evaluated"
    assert evaluation["dimensions"] == {"alignment": 0.8}

    assert main(["status", "--profile", str(profile_path), "--run", run_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "evaluated"
    assert set(status["artifacts"]) >= {"rollout", "evaluation", "source_digests"}
    assert dict(load_profile(profile_path, checkout=checkout).source_digests) == before


def test_evaluate_rejects_source_drift(tmp_path: Path) -> None:
    profile_path = _operational_profile(tmp_path / "external")
    run = run_profile(profile_path, "case-1", checkout=Path(__file__).parents[1])
    (profile_path.parent / "policy" / "base.md").write_text(
        "Changed policy.\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="source drift"):
        evaluate_profile_run(
            profile_path, run["run_id"], checkout=Path(__file__).parents[1]
        )
