from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kpo.cli import main
from kpo.profile import load_profile
from kpo.provider import ProviderFailure
from kpo.runner import evaluate_profile_run, run_profile
from kpo.runtime import RunState, RuntimeStore
from test_profile import _append_observer, _write_profile


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
    _write_valid_evaluator(root / "bin" / "evaluator")
    return profile


def _write_valid_evaluator(evaluator: Path) -> None:
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


def _write_failing_evaluator(evaluator: Path) -> None:
    evaluator.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('temporary failure')\nsys.exit(7)\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)


def test_validate_profile_does_not_create_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = _operational_profile(tmp_path / "external")

    assert main(["validate-profile", "--profile", str(profile_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["profile_id"] == "example"
    assert result["case_count"] == 1
    assert not (profile_path.parent / "runtime").exists()


def test_validate_profile_reports_observer_identities_without_runtime_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = _operational_profile(tmp_path / "external-observers")
    _append_observer(profile_path, "observer-a", "observer-model")

    assert main(["validate-profile", "--profile", str(profile_path)]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["observer_evaluator_count"] == 1
    assert result["observer_evaluators"] == [
        {"name": "observer-a", "evaluator_id": "observer-model"}
    ]
    assert "pass_env" not in json.dumps(result)
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


def test_evaluator_provider_failure_can_be_explicitly_retried_without_actor(
    tmp_path: Path,
) -> None:
    profile_path = _operational_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    run = run_profile(profile_path, "case-1", checkout=checkout)
    profile = load_profile(profile_path, checkout=checkout)
    store = RuntimeStore(profile.data_home)
    before_links = store.run_artifacts(run["run_id"])
    _write_failing_evaluator(profile_path.parent / "bin" / "evaluator")

    with pytest.raises(ProviderFailure):
        evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)

    assert store.get_run(run["run_id"]).state is RunState.EVALUATION_FAILED
    assert store.run_artifacts(run["run_id"])["rollout"] == before_links["rollout"]
    assert "evaluation" not in store.run_artifacts(run["run_id"])

    (profile_path.parent / "bin" / "actor").write_text(
        "#!/usr/bin/env python3\nraise SystemExit(99)\n", encoding="utf-8"
    )
    _write_valid_evaluator(profile_path.parent / "bin" / "evaluator")
    result = evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)
    assert result["state"] == "evaluated"


def test_repeated_evaluator_failure_remains_retryable(tmp_path: Path) -> None:
    profile_path = _operational_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    run = run_profile(profile_path, "case-1", checkout=checkout)
    _write_failing_evaluator(profile_path.parent / "bin" / "evaluator")

    for _ in range(2):
        with pytest.raises(ProviderFailure):
            evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)
        profile = load_profile(profile_path, checkout=checkout)
        assert RuntimeStore(profile.data_home).get_run(run["run_id"]).state is RunState.EVALUATION_FAILED


def test_legacy_failed_run_remains_terminal_even_with_rollout(tmp_path: Path) -> None:
    profile_path = _operational_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    run = run_profile(profile_path, "case-1", checkout=checkout)
    profile = load_profile(profile_path, checkout=checkout)
    store = RuntimeStore(profile.data_home)
    store.transition_run(run["run_id"], RunState.FAILED)

    with pytest.raises(ValueError, match="completed rollout"):
        evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)


@pytest.mark.parametrize("field", ["policy_digest", "case_id", "rollout_digest"])
def test_evaluation_retry_rejects_identity_drift_before_provider(
    tmp_path: Path, field: str
) -> None:
    profile_path = _operational_profile(tmp_path / field)
    checkout = Path(__file__).parents[1]
    run = run_profile(profile_path, "case-1", checkout=checkout)
    profile = load_profile(profile_path, checkout=checkout)
    store = RuntimeStore(profile.data_home)
    _write_failing_evaluator(profile_path.parent / "bin" / "evaluator")
    with pytest.raises(ProviderFailure):
        evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)
    assert store.get_run(run["run_id"]).state is RunState.EVALUATION_FAILED
    marker = tmp_path / f"{field}-provider-called"
    evaluator = profile_path.parent / "bin" / "evaluator"
    evaluator.write_text(
        "#!/usr/bin/env python3\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('called')\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o755)

    if field in {"policy_digest", "case_id"}:
        value = "0" * 64 if field == "policy_digest" else "different-case"
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                f"UPDATE runs SET {field} = ? WHERE run_id = ?",
                (value, run["run_id"]),
            )
    else:
        rollout_artifact = store.run_artifacts(run["run_id"])["rollout"]
        with sqlite3.connect(store.database_path) as connection:
            relative = connection.execute(
                "SELECT relative_path FROM artifacts WHERE digest = ?",
                (rollout_artifact,),
            ).fetchone()[0]
        rollout_path = store.artifacts_dir / relative
        value = json.loads(rollout_path.read_text(encoding="utf-8"))
        value["digest"] = "0" * 64
        rollout_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="mismatch"):
        evaluate_profile_run(profile_path, run["run_id"], checkout=checkout)
    assert not marker.exists()
