from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_campaign_series_profile import _series_profile

import kpo.campaign_series as series_module
from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import (
    SeriesEntry,
    append_series_entry,
    build_initial_series_state,
    initialize_series,
    load_series_state,
    series_mutation,
    stop_series,
)


def _profile(tmp_path: Path):
    return load_campaign_profile(
        _series_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )


def _entry(
    index: int = 0,
    campaign_id: str = "campaign-a",
    *,
    revision: int = 0,
    campaign_state: str = "promotion_previewed",
) -> SeriesEntry:
    suffix = "" if revision == 0 else f".{revision}"
    return SeriesEntry(
        index=index,
        campaign_id=campaign_id,
        revision=revision,
        campaign_state=campaign_state,
        parent_policy_digest="1" * 64,
        candidate_policy_digest="2" * 64,
        dataset_digest="3" * 64,
        profile_snapshot_digest="4" * 64,
        evidence_path=f"campaigns/{campaign_id}/series-evidence{suffix}.json",
        evidence_digest="5" * 64,
        evidence_record_digest="6" * 64,
    )


def test_initial_series_state_is_deterministic_safe_and_persisted(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    expected = build_initial_series_state(profile, "series-a")
    created = initialize_series(profile, "series-a")
    loaded = load_series_state(profile, "series-a")

    assert created == expected == loaded
    assert created["state"] == "active"
    assert created["entries"] == []
    assert created["max_campaigns"] == 6
    assert created["stopping"] == {
        "min_improvement": 0.01,
        "patience": 2,
        "holdout_degradation": 0.05,
    }
    serialized = json.dumps(created, sort_keys=True)
    assert "created_at" not in serialized
    assert str(tmp_path) not in serialized
    assert (profile.base.data_home / "series" / "series-a" / "series.json").is_file()


def test_series_initialization_is_non_overwriting_and_path_safe(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    with pytest.raises(FileExistsError):
        initialize_series(profile, "series-a")
    with pytest.raises(ValueError, match="safe path component"):
        initialize_series(profile, "../escape")


def test_immutable_publish_translates_hard_link_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def collide(source: object, target: object) -> None:
        raise FileExistsError("simulated concurrent publisher")

    monkeypatch.setattr(series_module.os, "link", collide)

    with pytest.raises(FileExistsError, match="immutable artifact already exists"):
        series_module._atomic_new_json(tmp_path / "artifact.json", {"value": 1})


@pytest.mark.parametrize("mutation", ["unknown", "digest", "identity", "contract"])
def test_series_state_strict_reload_rejects_tampering(
    tmp_path: Path, mutation: str
) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")
    path = profile.base.data_home / "series" / "series-a" / "series.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "unknown":
        raw["extra"] = True
    elif mutation == "digest":
        raw["state_digest"] = "0" * 64
    elif mutation == "identity":
        raw["series_id"] = "series-b"
    else:
        raw["contract"]["profile_id"] = "changed"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        load_series_state(profile, "series-a")


def test_series_append_is_ordered_immutable_and_idempotent(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    first = append_series_entry(profile, "series-a", _entry())
    repeated = append_series_entry(profile, "series-a", _entry())

    assert first == repeated
    assert first["entries"] == [
        {
            "index": 0,
            "campaign_id": "campaign-a",
            "revision": 0,
            "campaign_state": "promotion_previewed",
            "parent_policy_digest": "1" * 64,
            "candidate_policy_digest": "2" * 64,
            "dataset_digest": "3" * 64,
            "profile_snapshot_digest": "4" * 64,
            "evidence_path": "campaigns/campaign-a/series-evidence.json",
            "evidence_digest": "5" * 64,
            "evidence_record_digest": "6" * 64,
        }
    ]

    with pytest.raises(ValueError, match="next series index"):
        append_series_entry(profile, "series-a", _entry(index=2, campaign_id="campaign-c"))
    with pytest.raises(ValueError, match="revision.*already attached"):
        append_series_entry(profile, "series-a", _entry(index=1, campaign_id="campaign-a"))
    with pytest.raises(ValueError, match="series index.*already attached"):
        append_series_entry(profile, "series-a", _entry(index=0, campaign_id="campaign-b"))


def test_series_append_preserves_multiple_entry_order(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    append_series_entry(profile, "series-a", _entry())
    state = append_series_entry(
        profile, "series-a", _entry(index=1, campaign_id="campaign-b")
    )

    assert [entry["campaign_id"] for entry in state["entries"]] == [
        "campaign-a",
        "campaign-b",
    ]
    assert [entry["index"] for entry in state["entries"]] == [0, 1]


def test_failed_campaign_can_append_immutable_revision_chain(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    failed = _entry(campaign_state="failed")
    resumed = _entry(revision=1, campaign_state="promotion_previewed")
    append_series_entry(profile, "series-a", failed)
    state = append_series_entry(profile, "series-a", resumed)

    assert [(item["index"], item["revision"], item["campaign_state"]) for item in state["entries"]] == [
        (0, 0, "failed"),
        (0, 1, "promotion_previewed"),
    ]
    assert append_series_entry(profile, "series-a", resumed) == state


def test_revision_chain_is_gap_free_and_only_follows_failure(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    append_series_entry(profile, "series-a", _entry(campaign_state="failed"))
    with pytest.raises(ValueError, match="next campaign revision"):
        append_series_entry(
            profile,
            "series-a",
            _entry(revision=2, campaign_state="promotion_previewed"),
        )

    profile = _profile(tmp_path / "second")
    initialize_series(profile, "series-a")
    append_series_entry(profile, "series-a", _entry())
    with pytest.raises(ValueError, match="revision.*failed"):
        append_series_entry(
            profile,
            "series-a",
            _entry(revision=1, campaign_state="promotion_previewed"),
        )


def test_max_campaigns_counts_unique_campaigns_not_revisions(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")
    append_series_entry(profile, "series-a", _entry(campaign_state="failed"))
    append_series_entry(
        profile,
        "series-a",
        _entry(revision=1, campaign_state="failed"),
    )

    state = append_series_entry(
        profile,
        "series-a",
        _entry(index=1, campaign_id="campaign-b"),
    )

    assert len(state["entries"]) == 3
    assert len({item["campaign_id"] for item in state["entries"]}) == 2


def test_owner_stop_is_permanent_and_idempotent(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")

    stopped = stop_series(profile, "series-a")
    repeated = stop_series(profile, "series-a")

    assert stopped == repeated
    assert stopped["state"] == "stopped_by_owner"
    with pytest.raises(ValueError, match="stopped_by_owner"):
        append_series_entry(profile, "series-a", _entry())


def test_concurrent_series_mutation_fails_without_changing_state(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    initialize_series(profile, "series-a")
    before = load_series_state(profile, "series-a")

    with series_mutation(profile, "series-a", operation="test-holder"):
        with pytest.raises(ValueError, match="series concurrent mutation"):
            append_series_entry(profile, "series-a", _entry())
        with pytest.raises(ValueError, match="series concurrent mutation"):
            stop_series(profile, "series-a")

    assert load_series_state(profile, "series-a") == before


def test_profile_without_series_cannot_initialize_series(tmp_path: Path) -> None:
    from test_campaign_profile import _campaign_profile

    profile = load_campaign_profile(
        _campaign_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )

    with pytest.raises(ValueError, match="no series configuration"):
        initialize_series(profile, "series-a")
