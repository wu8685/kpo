from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kpo.campaign_cache import BudgetExhausted, CampaignCallExecutor
from kpo.profile import CommandProviderConfig
from kpo.provider import ProviderFailure


def _context(seed: int) -> dict[str, object]:
    return {
        "case_digest": "case",
        "policy_digest": "policy",
        "decoding_configuration_digest": "decoding",
        "random_seed": seed,
    }


def _counting_config(counter: Path, *, fail: bool = False) -> CommandProviderConfig:
    code = (
        "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        "request=json.load(sys.stdin); "
        + (
            "sys.stderr.write('failed'); sys.exit(2)"
            if fail
            else "json.dump({'output':request['request']['value']},sys.stdout)"
        )
    )
    return CommandProviderConfig(
        (sys.executable, "-c", code, str(counter)), "provider", 2, ()
    )


def test_provider_call_cache_is_persistent_and_complete_input_keyed(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter"
    config = _counting_config(counter)
    executor = CampaignCallExecutor(tmp_path / "runtime", "campaign-1", max_calls=3)
    context = {
        "case_digest": "case-a",
        "policy_digest": "policy-a",
        "reference_set_digest": "refs",
        "rubric_digest": "rubric",
        "decoding_configuration_digest": "decoding",
        "random_seed": 1,
    }

    first = executor.invoke(config, "actor", {"value": "result"}, context=context)
    second = executor.invoke(config, "actor", {"value": "result"}, context=context)
    resumed = CampaignCallExecutor(tmp_path / "runtime", "campaign-1", max_calls=3)
    third = resumed.invoke(config, "actor", {"value": "result"}, context=context)

    assert first == second == third == {"output": "result"}
    assert counter.read_text() == "1"
    assert resumed.call_count == 1
    changed = dict(context, random_seed=2)
    resumed.invoke(config, "actor", {"value": "result"}, context=changed)
    assert counter.read_text() == "2"


def test_failed_calls_are_counted_cached_and_not_retried(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    executor = CampaignCallExecutor(tmp_path / "runtime", "campaign-1", max_calls=2)
    config = _counting_config(counter, fail=True)
    context = _context(1)

    for _ in range(2):
        with pytest.raises(ProviderFailure):
            executor.invoke(config, "actor", {"value": "x"}, context=context)

    assert counter.read_text() == "1"
    assert executor.call_count == 1


def test_provider_call_budget_is_hard_limit(tmp_path: Path) -> None:
    executor = CampaignCallExecutor(tmp_path / "runtime", "campaign-1", max_calls=1)
    config = _counting_config(tmp_path / "counter")
    executor.invoke(config, "actor", {"value": "a"}, context=_context(1))

    with pytest.raises(BudgetExhausted):
        executor.invoke(config, "actor", {"value": "b"}, context=_context(2))

    state = json.loads(executor.state_path.read_text(encoding="utf-8"))
    assert state["call_count"] == 1
