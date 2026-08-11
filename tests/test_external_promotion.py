from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpo.campaign_profile import load_campaign_profile, resolve_candidate_destination
from kpo.cli import main
from kpo.digest import canonical_digest
from kpo.external_promotion import (
    ExternalPromotionInterrupted,
    apply_campaign_promotion,
    load_promotion_bundle,
    propose_manifest_update,
    recover_campaign_promotion,
)
from kpo.models import CandidatePatch, MutationKind
from kpo.profile import load_profile
from test_campaign_profile import _campaign_profile


def _candidate(
    mutation: MutationKind, artifact_id: str, *, content: str = "Reviewed rule.\n"
) -> CandidatePatch:
    return CandidatePatch(
        parent_policy_digest="parent",
        diagnosis_digest="diagnosis",
        mutation=mutation,
        artifact_id=artifact_id,
        content=content,
        provenance=("synthetic-source",),
        digest="candidate",
    )


def test_manifest_add_is_deterministic_and_preserves_existing_bytes(
    tmp_path: Path,
) -> None:
    profile_path = _campaign_profile(tmp_path / "external")
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    candidate = _candidate(MutationKind.ADD, "new-rule")
    destination = resolve_candidate_destination(profile, candidate)
    original = (profile_path.parent / "policy.jsonl").read_bytes()

    preview = propose_manifest_update(profile, candidate, destination)

    assert preview.original == original
    assert preview.reviewed.startswith(original)
    assert preview.reviewed.endswith(b"\n")
    assert json.loads(preview.reviewed.decode().splitlines()[-1]) == {
        "artifact_id": "new-rule",
        "path": "policies/new-rule.md",
        "version": 1,
    }
    assert "new-rule" in preview.diff


def test_manifest_edit_increments_only_version_and_preserves_row_order(
    tmp_path: Path,
) -> None:
    profile_path = _campaign_profile(tmp_path / "external")
    manifest = profile_path.parent / "policy.jsonl"
    second = {"artifact_id": "second", "path": "policy/second.md", "version": 4}
    (profile_path.parent / "policy" / "second.md").write_text("Second.\n", encoding="utf-8")
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(second) + "\n")
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    candidate = _candidate(MutationKind.EDIT, "base")
    destination = resolve_candidate_destination(profile, candidate)

    preview = propose_manifest_update(profile, candidate, destination)
    rows = [json.loads(line) for line in preview.reviewed.decode().splitlines()]

    assert [row["artifact_id"] for row in rows] == ["base", "second"]
    assert rows[0] == {"artifact_id": "base", "path": "policy/base.md", "version": 2}
    assert rows[1] == second
    assert preview.reviewed.endswith(b"\n")


def test_manifest_edit_preserves_blank_lines_and_updates_the_matching_row(
    tmp_path: Path,
) -> None:
    profile_path = _campaign_profile(tmp_path / "external")
    manifest = profile_path.parent / "policy.jsonl"
    original_row = manifest.read_bytes()
    manifest.write_bytes(b"\n" + original_row + b"\n")
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    candidate = _candidate(MutationKind.EDIT, "base")

    preview = propose_manifest_update(
        profile, candidate, resolve_candidate_destination(profile, candidate)
    )

    assert preview.reviewed.startswith(b"\n")
    rows = [json.loads(line) for line in preview.reviewed.splitlines() if line]
    assert rows == [{"artifact_id": "base", "path": "policy/base.md", "version": 2}]


def test_manifest_update_rejects_duplicate_add_and_missing_edit(tmp_path: Path) -> None:
    profile_path = _campaign_profile(tmp_path / "external")
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    duplicate = _candidate(MutationKind.ADD, "base")
    missing = _candidate(MutationKind.EDIT, "missing")

    with pytest.raises(ValueError, match="already exists"):
        propose_manifest_update(
            profile, duplicate, resolve_candidate_destination(profile, duplicate)
        )
    with pytest.raises(ValueError, match="no policy path"):
        resolve_candidate_destination(profile, missing)


def test_bundle_loader_rejects_unknown_fields_and_corrupted_reviewed_bytes(
    tmp_path: Path,
) -> None:
    from kpo.campaign import initialize_campaign_dataset, run_campaign
    from test_campaign import _working_profile

    profile_path = _working_profile(tmp_path / "external")
    preview = initialize_campaign_dataset(
        profile_path, checkout=Path(__file__).parents[1]
    )
    initialize_campaign_dataset(
        profile_path,
        checkout=Path(__file__).parents[1],
        approval_digest=preview["approval_digest"],
    )
    result = run_campaign(profile_path, checkout=Path(__file__).parents[1])
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    bundle_path = profile.base.data_home / result["bundle_path"]
    original_bundle = bundle_path.read_bytes()

    loaded = load_promotion_bundle(profile, result["campaign_id"])
    assert loaded.approval_digest

    raw = json.loads(original_bundle)
    raw["unknown"] = True
    bundle_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown bundle fields"):
        load_promotion_bundle(profile, result["campaign_id"])

    bundle_path.write_bytes(original_bundle)
    reviewed = bundle_path.parent / "reviewed-content.bin"
    reviewed.write_bytes(reviewed.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="reviewed-content"):
        load_promotion_bundle(profile, result["campaign_id"])


def _prepared_campaign(tmp_path: Path, *, edit: bool = False) -> tuple[Path, dict[str, object]]:
    from kpo.campaign import initialize_campaign_dataset, run_campaign
    from test_campaign import _working_profile

    profile_path = _working_profile(tmp_path / "external")
    if edit:
        proposer = profile_path.parent / "bin" / "proposer"
        proposer.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys\n"
            "json.load(sys.stdin)\n"
            "json.dump({'mutation':'edit','artifact_id':'base','content':'Improved rule.',"
            "'expected_benefit':'improve','possible_regressions':[]},sys.stdout)\n",
            encoding="utf-8",
        )
        proposer.chmod(0o755)
    init = initialize_campaign_dataset(profile_path, checkout=Path(__file__).parents[1])
    initialize_campaign_dataset(
        profile_path,
        checkout=Path(__file__).parents[1],
        approval_digest=init["approval_digest"],
    )
    result = run_campaign(profile_path, checkout=Path(__file__).parents[1])
    return profile_path, result


def _prepared_observer_campaign(
    tmp_path: Path,
) -> tuple[Path, dict[str, object]]:
    from kpo.campaign import initialize_campaign_dataset, run_campaign
    from test_campaign import _add_observers, _working_profile

    profile_path = _working_profile(tmp_path / "external")
    _add_observers(profile_path)
    init = initialize_campaign_dataset(
        profile_path, checkout=Path(__file__).parents[1]
    )
    initialize_campaign_dataset(
        profile_path,
        checkout=Path(__file__).parents[1],
        approval_digest=init["approval_digest"],
    )
    result = run_campaign(
        profile_path,
        checkout=Path(__file__).parents[1],
        campaign_id="observer-promotion",
    )
    return profile_path, result


def test_observer_campaign_uses_strict_v2_bundle_bound_to_agreement_bytes(
    tmp_path: Path,
) -> None:
    import hashlib

    profile_path, campaign = _prepared_observer_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    loaded = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    bundle = loaded.bundle
    bundle_report = loaded.directory / "evaluator-agreement.json"
    campaign_report = (
        profile.base.data_home / str(campaign["evaluator_agreement_path"])
    )
    report_digest = hashlib.sha256(bundle_report.read_bytes()).hexdigest()

    assert bundle["schema_version"] == 2
    assert bundle["protocol"] == "kpo.external-promotion.v2"
    assert bundle["evaluator_agreement_digest"] == report_digest
    assert bundle["files"]["evaluator_agreement"] == report_digest
    assert bundle_report.read_bytes() == campaign_report.read_bytes()
    assert report_digest == campaign["evaluator_agreement_digest"]

    bundle_path = loaded.directory / "bundle.json"
    original_bundle = bundle_path.read_bytes()
    missing = json.loads(original_bundle)
    missing.pop("evaluator_agreement_digest")
    bundle_path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="missing bundle fields"):
        load_promotion_bundle(profile, str(campaign["campaign_id"]))
    bundle_path.write_bytes(original_bundle)

    original = bundle_report.read_bytes()
    bundle_report.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="evaluator-agreement"):
        load_promotion_bundle(profile, str(campaign["campaign_id"]))


def test_v2_promote_preview_displays_bound_agreement_summary(
    tmp_path: Path,
) -> None:
    from kpo.external_promotion import preview_campaign_promotion

    profile_path, campaign = _prepared_observer_campaign(tmp_path)

    preview = preview_campaign_promotion(
        profile_path,
        str(campaign["campaign_id"]),
        checkout=Path(__file__).parents[1],
    )

    assert preview["evaluator_agreement_digest"] == campaign[
        "evaluator_agreement_digest"
    ]
    assert preview["evaluator_agreement_summary"] == campaign[
        "evaluator_agreement_summary"
    ]


def test_v2_apply_and_interrupted_recovery_preserve_agreement_evidence(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_observer_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    loaded = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    agreement_before = (loaded.directory / "evaluator-agreement.json").read_bytes()

    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=loaded.approval_digest,
            fault_after_content=True,
        )
    recovered = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert recovered["state"] == "recovered"
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()
    assert (loaded.directory / "evaluator-agreement.json").read_bytes() == agreement_before


def test_v2_installed_bundle_resume_restores_agreement_binding_without_calls(
    tmp_path: Path,
) -> None:
    from kpo.campaign import (
        CampaignInterrupted,
        campaign_status,
        initialize_campaign_dataset,
        run_campaign,
    )
    from test_campaign import _add_observers, _working_profile

    profile_path = _working_profile(tmp_path / "external")
    _add_observers(profile_path)
    init = initialize_campaign_dataset(
        profile_path, checkout=Path(__file__).parents[1]
    )
    initialize_campaign_dataset(
        profile_path,
        checkout=Path(__file__).parents[1],
        approval_digest=init["approval_digest"],
    )
    with pytest.raises(CampaignInterrupted) as interrupted:
        run_campaign(
            profile_path,
            checkout=Path(__file__).parents[1],
            campaign_id="v2-resume",
            fault_after_bundle=True,
        )
    campaign_id = interrupted.value.campaign_id
    data_home = profile_path.parent / "runtime"
    budget = data_home / "campaigns" / campaign_id / "call-budget.json"
    budget_before = budget.read_bytes()
    failed = campaign_status(profile_path, campaign_id, checkout=Path(__file__).parents[1])
    assert "evaluator_agreement_digest" not in failed

    resumed = run_campaign(
        profile_path,
        checkout=Path(__file__).parents[1],
        campaign_id=campaign_id,
        resume=True,
    )

    assert resumed["state"] == "promotion_previewed"
    assert resumed["evaluator_agreement_digest"]
    assert resumed["evaluator_agreement_summary"]["observer_count"] == 2
    assert budget.read_bytes() == budget_before


def test_v2_committed_recovery_keeps_historical_agreement_inspectable(
    tmp_path: Path,
) -> None:
    from kpo.evaluator_agreement import load_agreement_report

    profile_path, campaign = _prepared_observer_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    loaded = load_promotion_bundle(profile, str(campaign["campaign_id"]))

    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=loaded.approval_digest,
            fault_after_commit=True,
        )
    result = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )
    applied_profile = load_campaign_profile(profile_path, checkout=checkout)

    assert result["state"] == "applied"
    assert load_agreement_report(
        applied_profile, str(campaign["campaign_id"])
    )["report_digest"]


def test_promote_cli_preview_is_deterministic_and_non_mutating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    root = profile_path.parent
    manifest_before = (root / "policy.jsonl").read_bytes()
    holdout_before = (root / "runtime" / "dataset" / "holdout-lock.json").read_bytes()
    argv = [
        "promote",
        "--profile",
        str(profile_path),
        "--campaign",
        str(campaign["campaign_id"]),
        "--checkout",
        str(Path(__file__).parents[1]),
    ]

    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["state"] == "preview"
    assert first["approval_digest"]
    assert "Improved rule" in first["content_diff"]
    assert "improved-rule" in first["manifest_diff"]
    assert (root / "policy.jsonl").read_bytes() == manifest_before
    assert (root / "runtime" / "dataset" / "holdout-lock.json").read_bytes() == holdout_before
    assert not (root / "policies" / "improved-rule.md").exists()


def test_promote_cli_applies_the_exact_previewed_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = str(Path(__file__).parents[1])
    base = [
        "promote",
        "--profile",
        str(profile_path),
        "--campaign",
        str(campaign["campaign_id"]),
        "--checkout",
        checkout,
    ]
    assert main(base) == 0
    approval = json.loads(capsys.readouterr().out)["approval_digest"]

    assert main([*base, "--approve", approval]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["state"] == "applied"
    assert (profile_path.parent / "policies" / "improved-rule.md").is_file()


def test_promote_cli_makes_approve_and_recover_mutually_exclusive(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "promote",
                "--profile",
                str(profile_path),
                "--campaign",
                str(campaign["campaign_id"]),
                "--approve",
                "digest",
                "--recover",
            ]
        )


def test_preview_rejects_a_self_consistent_but_false_review_diff(tmp_path: Path) -> None:
    from kpo.external_promotion import preview_campaign_promotion

    profile_path, campaign = _prepared_campaign(tmp_path)
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    bundle_path = profile.base.data_home / str(campaign["bundle_path"])
    raw = json.loads(bundle_path.read_text())
    raw["content"]["diff"] = "misleading diff\n"
    raw["content"]["diff_digest"] = canonical_digest(raw["content"]["diff"])
    bundle_path.write_text(json.dumps(raw), encoding="utf-8")
    state_path = (
        profile.base.data_home
        / "campaigns"
        / str(campaign["campaign_id"])
        / "campaign.json"
    )
    state = json.loads(state_path.read_text())
    state["promotion_diff"] = raw["content"]["diff"]
    state["promotion_diff_digest"] = raw["content"]["diff_digest"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="content diff"):
        preview_campaign_promotion(
            profile_path, str(campaign["campaign_id"]), checkout=Path(__file__).parents[1]
        )


def test_apply_add_requires_exact_approval_and_rejects_double_apply(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    manifest_before = profile.policy_manifest_path.read_bytes()
    holdout_before = (profile.base.data_home / "dataset" / "holdout-lock.json").read_bytes()

    with pytest.raises(ValueError, match="approval digest"):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest="wrong",
        )
    assert profile.policy_manifest_path.read_bytes() == manifest_before
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()

    result = apply_campaign_promotion(
        profile_path,
        str(campaign["campaign_id"]),
        checkout=checkout,
        approval_digest=bundle.approval_digest,
    )

    assert result["state"] == "applied"
    assert (profile_path.parent / "policies" / "improved-rule.md").read_text() == "Improved rule."
    rows = [json.loads(line) for line in profile.policy_manifest_path.read_text().splitlines()]
    assert rows[-1] == {
        "artifact_id": "improved-rule",
        "path": "policies/improved-rule.md",
        "version": 1,
    }
    reloaded = load_profile(profile_path, checkout=checkout)
    assert reloaded.policy.digest == campaign["candidate_policy_digest"]
    assert (profile.base.data_home / "dataset" / "holdout-lock.json").read_bytes() == holdout_before
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()
    with pytest.raises(ValueError, match="already committed"):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
        )


def test_apply_requires_terminal_promotion_previewed_top_level_state(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    state_path = (
        profile.base.data_home
        / "campaigns"
        / str(campaign["campaign_id"])
        / "campaign.json"
    )
    state = json.loads(state_path.read_text())
    state["state"] = "failed"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="has not reached a promotion preview"):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
        )
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()


@pytest.mark.parametrize("stale", ["manifest", "destination"])
def test_apply_rejects_stale_originals_without_creating_lock(
    tmp_path: Path, stale: str
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path / stale)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    approval = load_promotion_bundle(profile, str(campaign["campaign_id"])).approval_digest
    if stale == "manifest":
        profile.policy_manifest_path.write_bytes(
            profile.policy_manifest_path.read_bytes() + b"\n"
        )
    else:
        destination = profile_path.parent / "policies" / "improved-rule.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("unexpected", encoding="utf-8")

    with pytest.raises(ValueError, match="stale promotion bundle"):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=approval,
        )
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()


def test_existing_apply_lock_serializes_promotions(tmp_path: Path) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    lock = profile.base.data_home / "promotions" / "apply.lock"
    lock.write_text(
        json.dumps({"campaign_id": "another", "approval_digest": "another"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="holds apply.lock"):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
        )
    assert lock.exists()
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()


def test_lock_without_journal_recovers_from_immutable_bundle(tmp_path: Path) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    lock = profile.base.data_home / "promotions" / "apply.lock"
    lock.write_text(
        json.dumps(
            {
                "campaign_id": str(campaign["campaign_id"]),
                "approval_digest": bundle.approval_digest,
            }
        ),
        encoding="utf-8",
    )

    result = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert result["state"] == "recovered"
    assert not lock.exists()
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()


def test_recovery_rejects_lock_owned_by_another_campaign(tmp_path: Path) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    lock = profile.base.data_home / "promotions" / "apply.lock"
    lock.write_text(
        json.dumps({"campaign_id": "other", "approval_digest": "other"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="belongs to another campaign"):
        recover_campaign_promotion(
            profile_path, str(campaign["campaign_id"]), checkout=checkout
        )
    assert lock.exists()


def test_recovery_requires_terminal_promotion_previewed_top_level_state(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_content=True,
        )
    state_path = (
        profile.base.data_home
        / "campaigns"
        / str(campaign["campaign_id"])
        / "campaign.json"
    )
    state = json.loads(state_path.read_text())
    state["state"] = "failed"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="has not reached a promotion preview"):
        recover_campaign_promotion(
            profile_path, str(campaign["campaign_id"]), checkout=checkout
        )
    assert (profile.base.data_home / "promotions" / "apply.lock").exists()


def test_apply_edit_replaces_content_and_increments_manifest_version(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path, edit=True)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    approval = load_promotion_bundle(profile, str(campaign["campaign_id"])).approval_digest

    apply_campaign_promotion(
        profile_path,
        str(campaign["campaign_id"]),
        checkout=checkout,
        approval_digest=approval,
    )

    assert (profile_path.parent / "policy" / "base.md").read_text() == "Improved rule."
    assert json.loads(profile.policy_manifest_path.read_text())["version"] == 2
    assert load_profile(profile_path, checkout=checkout).policy.digest == campaign["candidate_policy_digest"]


def test_interrupted_edit_apply_restores_content_and_manifest_exactly(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path, edit=True)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    content_path = profile_path.parent / "policy" / "base.md"
    content_before = content_path.read_bytes()
    manifest_before = profile.policy_manifest_path.read_bytes()
    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_manifest=True,
        )

    recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert content_path.read_bytes() == content_before
    assert profile.policy_manifest_path.read_bytes() == manifest_before


def test_nested_target_apply_uses_profile_relative_manifest_path(tmp_path: Path) -> None:
    from kpo.campaign import initialize_campaign_dataset, run_campaign
    from test_campaign import _working_profile

    profile_path = _working_profile(tmp_path / "external")
    nested = profile_path.parent / "knowledge"
    nested.mkdir()
    profile_path.write_text(
        profile_path.read_text().replace('target_root = "."', 'target_root = "./knowledge"'),
        encoding="utf-8",
    )
    init = initialize_campaign_dataset(profile_path, checkout=Path(__file__).parents[1])
    initialize_campaign_dataset(
        profile_path,
        checkout=Path(__file__).parents[1],
        approval_digest=init["approval_digest"],
    )
    campaign = run_campaign(profile_path, checkout=Path(__file__).parents[1])
    profile = load_campaign_profile(profile_path, checkout=Path(__file__).parents[1])
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    assert bundle.bundle["manifest_relative_path"] == "knowledge/policies/improved-rule.md"

    apply_campaign_promotion(
        profile_path,
        str(campaign["campaign_id"]),
        checkout=Path(__file__).parents[1],
        approval_digest=bundle.approval_digest,
    )

    assert (nested / "policies" / "improved-rule.md").is_file()
    assert json.loads(profile.policy_manifest_path.read_text().splitlines()[-1])["path"] == (
        "knowledge/policies/improved-rule.md"
    )


@pytest.mark.parametrize("fault", ["content", "manifest"])
def test_interrupted_apply_recovers_both_files_exactly(
    tmp_path: Path, fault: str
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path / fault)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    manifest_before = profile.policy_manifest_path.read_bytes()

    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_content=fault == "content",
            fault_after_manifest=fault == "manifest",
        )
    assert (profile.base.data_home / "promotions" / "apply.lock").exists()

    recovered = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert recovered["state"] == "recovered"
    assert profile.policy_manifest_path.read_bytes() == manifest_before
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()


def test_prepared_recovery_is_idempotent_if_interrupted_after_state_write(
    tmp_path: Path,
) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_content=True,
        )

    with pytest.raises(ExternalPromotionInterrupted, match="recovery state"):
        recover_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            fault_after_state=True,
        )
    lock = profile.base.data_home / "promotions" / "apply.lock"
    journal = (
        profile.base.data_home
        / "promotions"
        / "journals"
        / f"{bundle.approval_digest}.json"
    )
    assert lock.exists()
    assert json.loads(journal.read_text())["state"] == "prepared"
    assert not (profile_path.parent / "policies" / "improved-rule.md").exists()

    recovered = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert recovered["state"] == "recovered"
    assert not lock.exists()
    assert json.loads(journal.read_text())["state"] == "prepared"


def test_committed_journal_recovery_finalizes_without_rewriting_policy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kpo.external_promotion as module

    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_commit=True,
        )
    content = profile_path.parent / "policies" / "improved-rule.md"
    content_before = content.read_bytes()
    manifest_before = profile.policy_manifest_path.read_bytes()
    written: list[Path] = []
    real_atomic_write = module._atomic_write

    def record_write(path: Path, value: bytes) -> None:
        written.append(path)
        real_atomic_write(path, value)

    monkeypatch.setattr(module, "_atomic_write", record_write)
    result = recover_campaign_promotion(
        profile_path, str(campaign["campaign_id"]), checkout=checkout
    )

    assert result["state"] == "applied"
    assert content.read_bytes() == content_before
    assert profile.policy_manifest_path.read_bytes() == manifest_before
    assert content not in written
    assert profile.policy_manifest_path not in written
    assert not (profile.base.data_home / "promotions" / "apply.lock").exists()


def test_committed_journal_mismatch_keeps_lock(tmp_path: Path) -> None:
    profile_path, campaign = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    bundle = load_promotion_bundle(profile, str(campaign["campaign_id"]))
    with pytest.raises(ExternalPromotionInterrupted):
        apply_campaign_promotion(
            profile_path,
            str(campaign["campaign_id"]),
            checkout=checkout,
            approval_digest=bundle.approval_digest,
            fault_after_commit=True,
        )
    (profile_path.parent / "policies" / "improved-rule.md").write_text("tampered")

    with pytest.raises(ValueError, match="committed content"):
        recover_campaign_promotion(
            profile_path, str(campaign["campaign_id"]), checkout=checkout
        )
    assert (profile.base.data_home / "promotions" / "apply.lock").exists()


def test_ordinary_next_campaign_loads_applied_policy_as_parent(tmp_path: Path) -> None:
    from kpo.campaign import run_campaign

    profile_path, campaign_a = _prepared_campaign(tmp_path)
    checkout = Path(__file__).parents[1]
    profile = load_campaign_profile(profile_path, checkout=checkout)
    approval = load_promotion_bundle(profile, str(campaign_a["campaign_id"])).approval_digest
    apply_campaign_promotion(
        profile_path,
        str(campaign_a["campaign_id"]),
        checkout=checkout,
        approval_digest=approval,
    )

    campaign_b = run_campaign(profile_path, checkout=checkout)

    assert campaign_b["campaign_id"] != campaign_a["campaign_id"]
    assert campaign_b["parent_policy_digest"] == campaign_a["candidate_policy_digest"]
    assert campaign_b["state"] == "no_patchable_diagnosis"
