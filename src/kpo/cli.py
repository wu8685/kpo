from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from kpo.demo import run_synthetic_demo
from kpo.hygiene import scan_repository


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
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
