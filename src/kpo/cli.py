from __future__ import annotations

import argparse
import json
import tomllib
import uuid
from collections.abc import Sequence
from pathlib import Path

from kpo.campaign import campaign_status, initialize_campaign_dataset, run_campaign
from kpo.campaign_profile import load_campaign_profile
from kpo.campaign_series import (
    initialize_series,
    reconcile_series,
    series_evidence_report,
    series_status,
    stop_series,
)
from kpo.dataset import DatasetManager, load_dataset
from kpo.demo import run_synthetic_demo
from kpo.evaluator_agreement import load_agreement_report
from kpo.evaluator_calibration import (
    apply_anchor_approval,
    preview_anchor_approval,
)
from kpo.evaluator_drift import compare_profile_evaluator_drift
from kpo.external_promotion import (
    apply_campaign_promotion,
    preview_campaign_promotion,
    recover_campaign_promotion,
)
from kpo.hygiene import scan_repository
from kpo.profile import load_profile
from kpo.runner import evaluate_profile_run, profile_summary, run_profile, run_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kpo", description="Knowledge Policy Optimization"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    demo = subcommands.add_parser("demo", help="run the deterministic synthetic MVP")
    demo.add_argument("--data-home", type=Path, required=True)
    demo.add_argument("--target-root", type=Path, required=True)
    demo.add_argument("--checkout", type=Path, default=Path.cwd())

    hygiene = subcommands.add_parser(
        "hygiene", help="scan repository content for publication risks"
    )
    hygiene.add_argument("--repo", type=Path, default=Path.cwd())
    hygiene.add_argument("--denylist", type=Path)

    validate = subcommands.add_parser("validate-profile", help="validate an external profile")
    validate.add_argument("--profile", type=Path, required=True)
    validate.add_argument("--checkout", type=Path, default=Path.cwd())

    run = subcommands.add_parser("run", help="run an external profile case")
    run.add_argument("--profile", type=Path, required=True)
    run.add_argument("--case", required=True)
    run.add_argument("--checkout", type=Path, default=Path.cwd())

    evaluate = subcommands.add_parser("evaluate", help="evaluate a completed run")
    evaluate.add_argument("--profile", type=Path, required=True)
    evaluate.add_argument("--run", required=True)
    evaluate.add_argument("--checkout", type=Path, default=Path.cwd())

    status = subcommands.add_parser("status", help="inspect a persisted run")
    status.add_argument("--profile", type=Path, required=True)
    status.add_argument("--run", required=True)
    status.add_argument("--checkout", type=Path, default=Path.cwd())

    dataset = subcommands.add_parser("dataset", help="manage an external dataset")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_validate = dataset_commands.add_parser("validate")
    dataset_validate.add_argument("--profile", type=Path, required=True)
    dataset_validate.add_argument("--checkout", type=Path, default=Path.cwd())
    dataset_init = dataset_commands.add_parser("init")
    dataset_init.add_argument("--profile", type=Path, required=True)
    dataset_init.add_argument("--approve")
    dataset_init.add_argument("--checkout", type=Path, default=Path.cwd())
    dataset_grow = dataset_commands.add_parser("grow")
    dataset_grow.add_argument("--profile", type=Path, required=True)
    dataset_grow.add_argument("--inbox", type=Path, required=True)
    dataset_grow.add_argument("--approve")
    dataset_grow.add_argument("--checkout", type=Path, default=Path.cwd())

    anchors = subcommands.add_parser("anchors", help="manage evaluator anchor sets")
    anchor_commands = anchors.add_subparsers(dest="anchor_command", required=True)
    anchor_approve = anchor_commands.add_parser("approve")
    anchor_approve.add_argument("--profile", type=Path, required=True)
    anchor_approve.add_argument("--approve")
    anchor_approve.add_argument("--checkout", type=Path, default=Path.cwd())

    campaign = subcommands.add_parser("campaign", help="run an optimization campaign")
    campaign.add_argument("--profile", type=Path, required=True)
    campaign.add_argument("--resume")
    campaign.add_argument("--series")
    campaign.add_argument("--checkout", type=Path, default=Path.cwd())

    series = subcommands.add_parser("series", help="manage a campaign series")
    series_commands = series.add_subparsers(dest="series_command", required=True)
    for name in ("init", "status", "evidence", "reconcile", "stop"):
        command = series_commands.add_parser(name)
        command.add_argument("--profile", type=Path, required=True)
        command.add_argument("--series", required=name != "init")
        command.add_argument("--checkout", type=Path, default=Path.cwd())
        if name == "evidence":
            command.add_argument("--full", action="store_true")
    campaign_status_parser = subcommands.add_parser(
        "campaign-status", help="inspect a campaign"
    )
    campaign_status_parser.add_argument("--profile", type=Path, required=True)
    campaign_status_parser.add_argument("--campaign", required=True)
    campaign_status_parser.add_argument("--checkout", type=Path, default=Path.cwd())
    agreement = subcommands.add_parser(
        "evaluator-agreement", help="inspect a campaign evaluator agreement report"
    )
    agreement.add_argument("--profile", type=Path, required=True)
    agreement.add_argument("--campaign", required=True)
    agreement.add_argument("--checkout", type=Path, default=Path.cwd())
    drift = subcommands.add_parser(
        "evaluator-drift", help="compare evaluator calibration across campaigns"
    )
    drift.add_argument("--profile", type=Path, required=True)
    drift.add_argument("--baseline", required=True)
    drift.add_argument("--target", required=True)
    drift.add_argument("--checkout", type=Path, default=Path.cwd())
    promote = subcommands.add_parser(
        "promote", help="preview, apply, or recover a campaign promotion"
    )
    promote.add_argument("--profile", type=Path, required=True)
    promote.add_argument("--campaign", required=True)
    promotion_action = promote.add_mutually_exclusive_group()
    promotion_action.add_argument("--approve")
    promotion_action.add_argument("--recover", action="store_true")
    promote.add_argument("--checkout", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "demo":
        result = run_synthetic_demo(
            checkout=args.checkout,
            data_home=args.data_home,
            target_root=args.target_root,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "hygiene":
        violations = scan_repository(
            args.repo, external_denylist=args.denylist
        )
        print(
            json.dumps(
                [
                    {
                        "code": violation.code,
                        "path": violation.path,
                        "message": violation.message,
                    }
                    for violation in violations
                ],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 1 if violations else 0
    if args.command == "anchors":
        if args.anchor_command != "approve":
            raise AssertionError(f"unhandled anchor command: {args.anchor_command}")
        if args.approve is None:
            result = preview_anchor_approval(
                args.profile, checkout=args.checkout
            )
        else:
            result = apply_anchor_approval(
                args.profile,
                checkout=args.checkout,
                approval_digest=args.approve,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "series":
        profile = load_campaign_profile(args.profile, checkout=args.checkout)
        series_id = args.series or uuid.uuid4().hex
        if args.series_command == "init":
            result = initialize_series(profile, series_id)
        elif args.series_command == "status":
            result = series_status(profile, series_id)
        elif args.series_command == "evidence":
            result = series_evidence_report(profile, series_id, full=args.full)
        elif args.series_command == "reconcile":
            result = reconcile_series(profile, series_id)
        elif args.series_command == "stop":
            result = stop_series(profile, series_id)
        else:
            raise AssertionError(f"unhandled series command: {args.series_command}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "dataset":
        if args.dataset_command == "init":
            result = initialize_campaign_dataset(
                args.profile,
                checkout=args.checkout,
                approval_digest=args.approve,
            )
        else:
            profile = load_campaign_profile(args.profile, checkout=args.checkout)
            manager = DatasetManager(profile.base.data_home)
            if args.dataset_command == "validate":
                result = {
                    "state": "valid",
                    "dataset_digest": profile.dataset.digest,
                    "holdout_digest": profile.dataset.holdout_digest,
                    "counts": {
                        partition.value: len(entries)
                        for partition, entries in profile.dataset.by_partition.items()
                    },
                    "holdout_initialized": manager.lock_path.exists(),
                }
            elif args.dataset_command == "grow":
                proposed = load_dataset(args.inbox, profile.base.cases)
                preview = manager.preview_growth(
                    profile.dataset,
                    proposed,
                    manifest_path=profile.dataset_path,
                    proposed_manifest_path=args.inbox,
                )
                if args.approve is None:
                    result = {
                        "state": "preview",
                        "approval_digest": preview.approval_digest,
                        "added_case_ids": preview.added_case_ids,
                        "current_dataset_digest": preview.current_dataset_digest,
                        "proposed_dataset_digest": preview.proposed_dataset_digest,
                    }
                else:
                    applied = manager.apply_growth(
                        preview, approval_digest=args.approve
                    )
                    result = {
                        "state": applied.state,
                        "approval_digest": applied.approval_digest,
                        "journal_path": str(applied.journal_path),
                    }
            else:
                raise AssertionError(f"unhandled dataset command: {args.dataset_command}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "campaign":
        result = run_campaign(
            args.profile,
            checkout=args.checkout,
            campaign_id=args.resume,
            series_id=args.series,
            resume=args.resume is not None,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "campaign-status":
        result = campaign_status(
            args.profile, args.campaign, checkout=args.checkout
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "evaluator-agreement":
        profile = load_campaign_profile(args.profile, checkout=args.checkout)
        result = load_agreement_report(profile, args.campaign)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "evaluator-drift":
        profile = load_campaign_profile(args.profile, checkout=args.checkout)
        result = compare_profile_evaluator_drift(
            profile,
            args.baseline,
            args.target,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "promote":
        if args.recover:
            result = recover_campaign_promotion(
                args.profile, args.campaign, checkout=args.checkout
            )
        elif args.approve is not None:
            result = apply_campaign_promotion(
                args.profile,
                args.campaign,
                checkout=args.checkout,
                approval_digest=args.approve,
            )
        else:
            result = preview_campaign_promotion(
                args.profile, args.campaign, checkout=args.checkout
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "validate-profile":
        raw_profile = tomllib.loads(args.profile.read_text(encoding="utf-8"))
        if any(
            section in raw_profile
            for section in ("dataset", "proposer", "campaign", "promotion")
        ):
            campaign_profile = load_campaign_profile(
                args.profile, checkout=args.checkout
            )
            result = profile_summary(campaign_profile.base) | {
                "dataset_case_count": len(campaign_profile.dataset.entries),
                "dataset_digest": campaign_profile.dataset.digest,
                "max_iterations": campaign_profile.max_iterations,
                "max_provider_calls": campaign_profile.max_provider_calls,
                "sandbox": campaign_profile.sandbox_type,
            }
        else:
            result = profile_summary(load_profile(args.profile, checkout=args.checkout))
    elif args.command == "run":
        result = run_profile(args.profile, args.case, checkout=args.checkout)
    elif args.command == "evaluate":
        result = evaluate_profile_run(args.profile, args.run, checkout=args.checkout)
    elif args.command == "status":
        result = run_status(args.profile, args.run, checkout=args.checkout)
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
