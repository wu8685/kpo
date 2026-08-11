import json
from pathlib import Path

from kpo.cli import main
from kpo.demo import run_synthetic_demo


def test_synthetic_demo_improves_holdout_without_writing_target(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "knowledge"
    target_root.mkdir()

    result = run_synthetic_demo(
        checkout=Path.cwd(),
        data_home=tmp_path / "runtime",
        target_root=target_root,
    )

    assert result["baseline_score"] == 0.2
    assert result["candidate_score"] == 1.0
    assert result["validation_gain"] == 0.8
    assert result["holdout_gain"] == 0.8
    assert result["regression_passed"] is True
    assert result["source_unchanged"] is True
    assert result["promotion_state"] == "preview_only"
    assert (target_root / "policies" / "conflict-rule.md").exists() is False


def test_demo_cli_prints_machine_readable_result(tmp_path: Path, capsys: object) -> None:
    target_root = tmp_path / "knowledge"
    target_root.mkdir()

    exit_code = main(
        [
            "demo",
            "--data-home",
            str(tmp_path / "runtime"),
            "--target-root",
            str(target_root),
        ]
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result["regression_passed"] is True
    assert "+When evidence conflicts" in result["promotion_diff"]
