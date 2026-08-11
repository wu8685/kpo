from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign import _initialize
from test_campaign_series_campaign import _series_working_profile

import kpo.campaign as campaign_module
from kpo.campaign import CampaignInterrupted, run_campaign
from kpo.campaign_profile import (
    campaign_integrity_monitor,
    campaign_profile_snapshot,
    load_campaign_profile,
)
from kpo.campaign_series import (
    initialize_series,
    load_series_state,
    reconcile_series,
)
from kpo.digest import canonical_digest


def test_reconcile_attaches_terminal_campaign_after_append_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    original_append = campaign_module.append_series_entry

    def interrupt(*args, **kwargs):
        raise CampaignInterrupted("campaign-a")

    monkeypatch.setattr(campaign_module, "append_series_entry", interrupt)
    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=checkout,
            campaign_id="campaign-a",
            series_id="series-a",
        )
    assert load_series_state(profile, "series-a")["entries"] == []

    monkeypatch.setattr(campaign_module, "append_series_entry", original_append)
    recovered = reconcile_series(profile, "series-a")
    repeated = reconcile_series(profile, "series-a")

    assert recovered["attached_count"] == 1
    assert repeated["attached_count"] == 0
    assert repeated["already_present_count"] == 1
    assert load_series_state(profile, "series-a")["entries"][0][
        "campaign_id"
    ] == "campaign-a"


def test_reconcile_ignores_standalone_campaigns(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    run_campaign(path, checkout=checkout, campaign_id="standalone")

    result = reconcile_series(profile, "series-a")

    assert result["attached_count"] == 0
    assert result["ignored_count"] >= 1
    assert load_series_state(profile, "series-a")["entries"] == []


def test_reconcile_rejects_bound_nonterminal_campaign(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    campaign_dir = profile.base.data_home / "campaigns" / "campaign-a"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text(
        '{"campaign_id":"campaign-a","series_id":"series-a",'
        '"series_index":0,"state":"running"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-terminal"):
        reconcile_series(profile, "series-a")


def test_new_series_campaign_reconciles_prior_terminal_gap_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _series_working_profile(tmp_path / "external", max_campaigns=3)
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    original_append = campaign_module.append_series_entry

    monkeypatch.setattr(
        campaign_module,
        "append_series_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CampaignInterrupted("campaign-a")
        ),
    )
    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=checkout,
            campaign_id="campaign-a",
            series_id="series-a",
        )
    monkeypatch.setattr(campaign_module, "append_series_entry", original_append)

    second = run_campaign(
        path,
        checkout=checkout,
        campaign_id="campaign-b",
        series_id="series-a",
    )

    assert second["series_index"] == 1
    assert [
        entry["campaign_id"]
        for entry in load_series_state(profile, "series-a")["entries"]
    ] == ["campaign-a", "campaign-b"]


def _write_dead_allocation_lock(profile, state: dict) -> Path:
    campaign_dir = profile.base.data_home / "campaigns" / state["campaign_id"]
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text(
        json.dumps(state, sort_keys=True), encoding="utf-8"
    )
    lock_path = profile.base.data_home / "series" / ".locks" / "series-a.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "protocol": "kpo.campaign-series-lock/v1",
                "series_id": "series-a",
                "operation": "allocation",
                "campaign_id": state["campaign_id"],
                "series_index": 0,
                "revision": 0,
                "initial_state_digest": canonical_digest(state),
                "pid": 99999999,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return lock_path


def test_reconcile_recovers_provably_interrupted_initial_allocation(
    tmp_path: Path,
) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    series = initialize_series(profile, "series-a")
    state = {
        "campaign_id": "campaign-a",
        "state": "running",
        "iteration": 0,
        "profile_snapshot_digest": campaign_profile_snapshot(profile),
        "parent_policy_digest": profile.base.policy.digest,
        "target_digest": campaign_integrity_monitor(profile).target_digest,
        "rejected_candidates": [],
        "sandbox": profile.sandbox_type,
        "series_id": "series-a",
        "series_index": 0,
        "series_evidence_revision": 0,
        "series_contract_digest": series["series_contract_digest"],
    }
    lock_path = _write_dead_allocation_lock(profile, state)

    result = reconcile_series(profile, "series-a")

    campaign = json.loads(
        (
            profile.base.data_home
            / "campaigns"
            / "campaign-a"
            / "campaign.json"
        ).read_text(encoding="utf-8")
    )
    assert not lock_path.exists()
    assert campaign["state"] == "failed"
    assert campaign["failure_kind"] == "series_initialization_interrupted"
    assert result["attached_count"] == 0
    assert result["already_present_count"] == 1
    assert load_series_state(profile, "series-a")["entries"][0][
        "campaign_state"
    ] == "failed"


def test_reconcile_keeps_ambiguous_dead_allocation_locked(tmp_path: Path) -> None:
    path = _series_working_profile(tmp_path / "external")
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    series = initialize_series(profile, "series-a")
    state = {
        "campaign_id": "campaign-a",
        "state": "running",
        "iteration": 0,
        "profile_snapshot_digest": campaign_profile_snapshot(profile),
        "parent_policy_digest": profile.base.policy.digest,
        "target_digest": campaign_integrity_monitor(profile).target_digest,
        "rejected_candidates": [],
        "sandbox": profile.sandbox_type,
        "series_id": "series-a",
        "series_index": 0,
        "series_evidence_revision": 0,
        "series_contract_digest": series["series_contract_digest"],
    }
    lock_path = _write_dead_allocation_lock(profile, state)
    (lock_path.parents[2] / "campaigns" / "campaign-a" / "call-budget.json").write_text(
        "{}", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="series manual recovery required"):
        reconcile_series(profile, "series-a")

    assert lock_path.exists()


def test_reconcile_attaches_missing_resumed_revision_without_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _series_working_profile(tmp_path / "external", max_campaigns=1)
    checkout = Path(__file__).parents[1]
    _initialize(path)
    profile = load_campaign_profile(path, checkout=checkout)
    initialize_series(profile, "series-a")
    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=checkout,
            campaign_id="campaign-a",
            series_id="series-a",
            fault_after_train=True,
        )
    budget_path = (
        profile.base.data_home
        / "campaigns"
        / "campaign-a"
        / "call-budget.json"
    )
    original_append = campaign_module.append_series_entry
    monkeypatch.setattr(
        campaign_module,
        "append_series_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CampaignInterrupted("campaign-a")
        ),
    )
    with pytest.raises(CampaignInterrupted):
        run_campaign(
            path,
            checkout=checkout,
            campaign_id="campaign-a",
            series_id="series-a",
            resume=True,
        )
    budget_before = budget_path.read_bytes()
    monkeypatch.setattr(campaign_module, "append_series_entry", original_append)

    recovered = reconcile_series(profile, "series-a")

    assert budget_path.read_bytes() == budget_before
    assert recovered["attached_count"] == 1
    assert [
        (entry["revision"], entry["campaign_state"])
        for entry in load_series_state(profile, "series-a")["entries"]
    ] == [(0, "failed"), (1, "promotion_previewed")]
