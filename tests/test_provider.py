from __future__ import annotations

import json
import sys

import pytest

from kpo.models import (
    DiagnosisKind,
    EvaluatorResult,
    PolicyArtifact,
    PolicySnapshot,
    ReferenceArtifact,
    Rollout,
    TaskCase,
)
from kpo.pipeline import ActorRequest, EvaluatorRequest
from kpo.profile import CommandProviderConfig, Rubric, RubricDimension
from kpo.provider import (
    CommandActorProvider,
    CommandEvaluatorProvider,
    ProviderFailure,
    ProviderFailureKind,
)


def _config(code: str, *, pass_env: tuple[str, ...] = (), timeout: float = 2) -> CommandProviderConfig:
    return CommandProviderConfig(
        command=(sys.executable, "-c", code),
        identity="test-provider",
        timeout_seconds=timeout,
        pass_env=pass_env,
    )


def test_actor_command_protocol_excludes_hidden_reference_and_rubric() -> None:
    code = (
        "import json,sys; r=json.load(sys.stdin); "
        "assert r['protocol']=='kpo.command/v1'; assert r['role']=='actor'; "
        "s=json.dumps(r); assert 'ref-hidden' not in s; assert 'rubric' not in s; "
        "json.dump({'output':'actor result'},sys.stdout)"
    )
    provider = CommandActorProvider(_config(code))
    case = TaskCase("case-1", "Analyze", hidden_reference_ids=("ref-hidden",))
    policy = PolicySnapshot.from_artifacts((PolicyArtifact("base", "Rule"),))

    output = provider.generate(
        ActorRequest(case_payload=case.actor_payload(), policy_artifacts=policy.artifacts)
    )

    assert output == "actor result"


def test_evaluator_command_returns_dimensions_and_failure_signals() -> None:
    response = {
        "dimensions": [
            {
                "name": "alignment",
                "score": 0.5,
                "explanation": "Partial match.",
                "citations": ["rollout", "ref-1"],
            }
        ],
        "failure_signals": [
            {
                "kind": "missing_policy_knowledge",
                "message": "A rule is missing.",
                "citations": ["rollout", "ref-1"],
            }
        ],
        "aggregate_score": 0.5,
    }
    code = f"import json,sys; json.load(sys.stdin); json.dump({response!r},sys.stdout)"
    rubric = Rubric("v1", (RubricDimension("alignment", "Compare", 1.0),))
    provider = CommandEvaluatorProvider(_config(code), rubric)
    rollout = Rollout.create(
        case_id="case-1",
        policy_digest="policy",
        model_id="actor",
        output="result",
        retrieved_policy_ids=("base",),
        actor_payload_digest="payload",
    )

    result = provider.evaluate(
        EvaluatorRequest(
            case_id="case-1",
            rollout=rollout,
            references=(ReferenceArtifact("ref-1", "expected", "expected"),),
            rubric_version="v1",
        )
    )

    assert isinstance(result, EvaluatorResult)
    assert result.dimensions[0].name == "alignment"
    assert result.failure_signals[0].kind is DiagnosisKind.MISSING_POLICY_KNOWLEDGE
    assert result.aggregate_score == 0.5


def test_provider_environment_is_opt_in_and_failure_redacts_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "not-a-real-secret-value-12345"
    monkeypatch.setenv("KPO_TEST_SECRET", secret)
    monkeypatch.setenv("KPO_NOT_PASSED", "must-not-pass")
    code = (
        "import os,sys; assert 'KPO_NOT_PASSED' not in os.environ; "
        "sys.stderr.write('token=' + os.environ['KPO_TEST_SECRET']); sys.exit(3)"
    )
    provider = CommandActorProvider(_config(code, pass_env=("KPO_TEST_SECRET",)))

    with pytest.raises(ProviderFailure) as captured:
        provider.generate(ActorRequest(case_payload={"case_id": "x"}, policy_artifacts=()))

    assert captured.value.kind is ProviderFailureKind.NON_ZERO_EXIT
    assert secret not in str(captured.value)
    assert secret not in captured.value.stderr_excerpt
    assert "[REDACTED]" in captured.value.stderr_excerpt


def test_missing_opt_in_environment_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPO_MISSING_ENV", raising=False)
    provider = CommandActorProvider(_config("pass", pass_env=("KPO_MISSING_ENV",)))
    with pytest.raises(ProviderFailure) as captured:
        provider.generate(ActorRequest(case_payload={"case_id": "x"}, policy_artifacts=()))
    assert captured.value.kind is ProviderFailureKind.CONFIGURATION_ERROR


def test_generic_secret_pattern_is_redacted_from_stderr() -> None:
    fake = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    code = f"import sys; sys.stderr.write({fake!r}); sys.exit(2)"
    with pytest.raises(ProviderFailure) as captured:
        CommandActorProvider(_config(code)).generate(
            ActorRequest(case_payload={"case_id": "x"}, policy_artifacts=())
        )
    assert fake not in captured.value.stderr_excerpt
    assert "[REDACTED]" in captured.value.stderr_excerpt


@pytest.mark.parametrize("names", [("extra",), ()])
def test_evaluator_rejects_dimensions_that_do_not_match_rubric(
    names: tuple[str, ...],
) -> None:
    dimensions = [
        {
            "name": name,
            "score": 0.5,
            "explanation": "result",
            "citations": ["rollout"],
        }
        for name in names
    ]
    response = {
        "dimensions": dimensions,
        "failure_signals": [],
        "aggregate_score": None,
    }
    code = f"import json,sys; json.load(sys.stdin); json.dump({response!r},sys.stdout)"
    provider = CommandEvaluatorProvider(
        _config(code), Rubric("v1", (RubricDimension("alignment", "Compare", 1.0),))
    )
    rollout = Rollout.create(
        case_id="case-1",
        policy_digest="p",
        model_id="m",
        output="o",
        retrieved_policy_ids=(),
        actor_payload_digest="a",
    )
    with pytest.raises(ProviderFailure) as captured:
        provider.evaluate(EvaluatorRequest("case-1", rollout, (), "v1"))
    assert captured.value.kind is ProviderFailureKind.INVALID_RESPONSE


def test_provider_timeout_is_typed_and_not_retried() -> None:
    provider = CommandActorProvider(
        _config("import time; time.sleep(1)", timeout=0.01)
    )

    with pytest.raises(ProviderFailure) as captured:
        provider.generate(ActorRequest(case_payload={"case_id": "x"}, policy_artifacts=()))

    assert captured.value.kind is ProviderFailureKind.TIMEOUT


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (("definitely-not-a-kpo-command",), ProviderFailureKind.COMMAND_NOT_FOUND),
        (
            (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"),
            ProviderFailureKind.INVALID_ENCODING,
        ),
        (
            (sys.executable, "-c", "print('{} {}')"),
            ProviderFailureKind.INVALID_RESPONSE,
        ),
    ],
)
def test_provider_process_and_response_failures_are_typed(
    command: tuple[str, ...], expected: ProviderFailureKind
) -> None:
    config = CommandProviderConfig(command, "test", 1, ())
    with pytest.raises(ProviderFailure) as captured:
        CommandActorProvider(config).generate(
            ActorRequest(case_payload={"case_id": "x"}, policy_artifacts=())
        )
    assert captured.value.kind is expected
