from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from kpo.demo import run_synthetic_demo
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
    if args.command == "validate-profile":
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
