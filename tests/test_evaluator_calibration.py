from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from kpo.campaign_profile import campaign_profile_snapshot, load_campaign_profile
from kpo.cli import main
from kpo.digest import canonical_digest
from kpo.evaluator_calibration import (
    apply_anchor_approval,
    build_anchor_evaluator_request,
    load_anchor_approval,
    preview_anchor_approval,
)
from kpo.provider import CommandEvaluatorProvider
from test_campaign_profile import _campaign_profile


def _add_evaluator_audit(
    profile_path: Path,
    rows: list[dict[str, object]] | None = None,
) -> Path:
    root = profile_path.parent
    anchors = root / "anchors"
    anchors.mkdir(exist_ok=True)
    if rows is None:
        rows = [
            {
                "protocol": "kpo.evaluator-anchor/v1",
                "anchor_id": "anchor-b",
                "rollout_path": "./anchors/anchor-b.txt",
                "reference_ids": ["ref-2"],
                "expected_scores": {"alignment": 0.25},
                "provenance": ["synthetic-review-b"],
            },
            {
                "protocol": "kpo.evaluator-anchor/v1",
                "anchor_id": "anchor-a",
                "rollout_path": "./anchors/anchor-a.txt",
                "reference_ids": ["ref-1"],
                "expected_scores": {"alignment": 0.75},
                "provenance": ["synthetic-review-a"],
            },
        ]
    for index, row in enumerate(rows):
        rollout = root / str(row["rollout_path"])
        if ".." not in rollout.parts:
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rollout.write_text(f"Fixed synthetic output {index}.\n", encoding="utf-8")
    (root / "anchors.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    with profile_path.open("a", encoding="utf-8") as stream:
        stream.write(
            '''

[evaluator_audit]
anchor_manifest = "./anchors.jsonl"
max_provider_calls = 5

[evaluator_audit.drift_thresholds]
alignment = 0.10
'''
        )
    return profile_path


def test_profile_without_evaluator_audit_preserves_v06_shape(tmp_path: Path) -> None:
    profile = load_campaign_profile(
        _campaign_profile(tmp_path / "external"),
        checkout=Path(__file__).parents[1],
    )

    assert profile.evaluator_audit is None
    commands = (profile.base.actor, profile.base.evaluator, profile.proposer)
    v06_snapshot = canonical_digest(
        {
            "profile_sources": profile.base.source_digests,
            "dataset_digest": profile.dataset.digest,
            "commands": [
                {
                    "config": command,
                    "executable_digest": hashlib.sha256(
                        Path(command.command[0]).read_bytes()
                    ).hexdigest(),
                }
                for command in commands
            ],
            "observers": [],
            "gates": profile.gates,
            "promotion": {
                "target_root": profile.target_root,
                "allowlist": profile.allowlist,
                "add_directory": profile.add_directory,
            },
        }
    )
    assert campaign_profile_snapshot(profile) == v06_snapshot


def test_evaluator_audit_loads_sorted_strict_anchors_and_two_digests(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))

    audit = load_campaign_profile(
        path, checkout=Path(__file__).parents[1]
    ).evaluator_audit

    assert audit is not None
    assert audit.manifest_path == path.parent / "anchors.jsonl"
    assert audit.max_provider_calls == 5
    assert audit.drift_thresholds == {"alignment": 0.10}
    assert tuple(anchor.anchor_id for anchor in audit.anchors) == (
        "anchor-a",
        "anchor-b",
    )
    assert audit.anchor_set_digest != audit.anchor_request_set_digest
    assert audit.manifest_digest == hashlib.sha256(
        audit.manifest_path.read_bytes()
    ).hexdigest()
    assert all(anchor.blinded_digest for anchor in audit.anchors)
    assert all(anchor.request_digest for anchor in audit.anchors)
    for anchor in audit.anchors:
        relative = str(anchor.rollout_path.relative_to(path.parent))
        assert audit.source_digests[relative] == anchor.rollout_digest


def test_expected_scores_and_provenance_never_influence_request_digests(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    checkout = Path(__file__).parents[1]
    original = load_campaign_profile(path, checkout=checkout).evaluator_audit
    assert original is not None

    manifest = path.parent / "anchors.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    rows[0]["expected_scores"]["alignment"] = 0.90
    rows[0]["provenance"] = ["different-private-review"]
    manifest.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    protected_change = load_campaign_profile(path, checkout=checkout).evaluator_audit
    assert protected_change is not None

    assert protected_change.anchor_set_digest != original.anchor_set_digest
    assert (
        protected_change.anchor_request_set_digest
        == original.anchor_request_set_digest
    )
    assert tuple(anchor.blinded_digest for anchor in protected_change.anchors) == tuple(
        anchor.blinded_digest for anchor in original.anchors
    )
    assert tuple(anchor.request_digest for anchor in protected_change.anchors) == tuple(
        anchor.request_digest for anchor in original.anchors
    )

    (path.parent / "anchors" / "anchor-b.txt").write_text(
        "Changed request-visible output.\n", encoding="utf-8"
    )
    request_change = load_campaign_profile(path, checkout=checkout).evaluator_audit
    assert request_change is not None
    assert request_change.anchor_set_digest != protected_change.anchor_set_digest
    assert (
        request_change.anchor_request_set_digest
        != protected_change.anchor_request_set_digest
    )
    assert tuple(anchor.blinded_digest for anchor in request_change.anchors) != tuple(
        anchor.blinded_digest for anchor in protected_change.anchors
    )


def test_complete_digest_binds_manifest_path_but_request_digest_binds_bytes(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    checkout = Path(__file__).parents[1]
    original = load_campaign_profile(path, checkout=checkout).evaluator_audit
    assert original is not None

    source = path.parent / "anchors" / "anchor-a.txt"
    relocated = path.parent / "anchors" / "relocated-a.txt"
    relocated.write_bytes(source.read_bytes())
    manifest = path.parent / "anchors.jsonl"
    manifest.write_text(
        manifest.read_text().replace(
            "./anchors/anchor-a.txt", "./anchors/relocated-a.txt"
        ),
        encoding="utf-8",
    )
    changed = load_campaign_profile(path, checkout=checkout).evaluator_audit
    assert changed is not None

    assert changed.anchor_set_digest != original.anchor_set_digest
    assert changed.anchor_request_set_digest == original.anchor_request_set_digest
    assert tuple(anchor.blinded_digest for anchor in changed.anchors) == tuple(
        anchor.blinded_digest for anchor in original.anchors
    )


def test_audit_sources_are_read_once_and_bound_to_loaded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    rollout = (path.parent / "anchors" / "anchor-a.txt").resolve()
    original_read_bytes = Path.read_bytes
    reads = 0

    def controlled_read_bytes(candidate: Path) -> bytes:
        nonlocal reads
        if candidate.resolve() == rollout:
            reads += 1
            return b"Captured exactly once.\n"
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", controlled_read_bytes)
    audit = load_campaign_profile(
        path, checkout=Path(__file__).parents[1]
    ).evaluator_audit
    assert audit is not None
    anchor = next(item for item in audit.anchors if item.anchor_id == "anchor-a")

    assert reads == 1
    assert anchor.rollout_content == "Captured exactly once.\n"
    assert anchor.rollout_digest == hashlib.sha256(b"Captured exactly once.\n").hexdigest()
    assert audit.source_digests["anchors/anchor-a.txt"] == anchor.rollout_digest


def test_anchor_approval_preview_is_safe_and_apply_is_exact_and_immutable(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    checkout = Path(__file__).parents[1]

    preview = preview_anchor_approval(path, checkout=checkout)
    serialized = json.dumps(preview, sort_keys=True)

    assert preview["protocol"] == "kpo.evaluator-anchor-approval-preview/v1"
    assert preview["anchor_count"] == 2
    assert preview["anchor_set_digest"]
    assert preview["approval_digest"]
    assert "Fixed output" not in serialized
    assert "Expected 0" not in serialized
    assert "synthetic-review" not in serialized
    assert "expected_scores" not in serialized
    assert "provenance" not in serialized

    profile = load_campaign_profile(path, checkout=checkout)
    assert profile.evaluator_audit is not None
    record_path = (
        profile.base.data_home
        / "anchors"
        / "approved"
        / f"{profile.evaluator_audit.anchor_set_digest}.json"
    )
    with pytest.raises(ValueError, match="approval digest"):
        apply_anchor_approval(path, checkout=checkout, approval_digest="wrong")
    assert not record_path.exists()

    applied = apply_anchor_approval(
        path, checkout=checkout, approval_digest=preview["approval_digest"]
    )
    before = record_path.read_bytes()
    loaded, record_digest = load_anchor_approval(
        load_campaign_profile(path, checkout=checkout)
    )

    assert applied["state"] == "approved"
    assert applied["record_path"] == str(record_path.relative_to(profile.base.data_home))
    assert loaded["approval_digest"] == preview["approval_digest"]
    assert applied["record_digest"] == record_digest
    with pytest.raises(FileExistsError):
        apply_anchor_approval(
            path, checkout=checkout, approval_digest=preview["approval_digest"]
        )
    assert record_path.read_bytes() == before


def test_anchor_approval_reloads_sources_and_rejects_drift_or_corruption(
    tmp_path: Path,
) -> None:
    checkout = Path(__file__).parents[1]
    drifted = _add_evaluator_audit(_campaign_profile(tmp_path / "drifted"))
    preview = preview_anchor_approval(drifted, checkout=checkout)
    (drifted.parent / "anchors" / "anchor-a.txt").write_text(
        "Changed after preview.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="approval digest"):
        apply_anchor_approval(
            drifted, checkout=checkout, approval_digest=preview["approval_digest"]
        )

    corrupted = _add_evaluator_audit(_campaign_profile(tmp_path / "corrupted"))
    approved = preview_anchor_approval(corrupted, checkout=checkout)
    apply_anchor_approval(
        corrupted, checkout=checkout, approval_digest=approved["approval_digest"]
    )
    profile = load_campaign_profile(corrupted, checkout=checkout)
    assert profile.evaluator_audit is not None
    record_path = (
        profile.base.data_home
        / "anchors"
        / "approved"
        / f"{profile.evaluator_audit.anchor_set_digest}.json"
    )
    record = json.loads(record_path.read_text())
    record["unknown"] = True
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="approval record fields"):
        load_anchor_approval(profile)


def test_anchor_approval_publish_failure_leaves_no_partial_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    checkout = Path(__file__).parents[1]
    preview = preview_anchor_approval(path, checkout=checkout)
    profile = load_campaign_profile(path, checkout=checkout)
    assert profile.evaluator_audit is not None
    record_path = (
        profile.base.data_home
        / "anchors"
        / "approved"
        / f"{profile.evaluator_audit.anchor_set_digest}.json"
    )

    def fail_link(source: Path, destination: Path) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr("kpo.evaluator_calibration.os.link", fail_link)
    with pytest.raises(OSError, match="publish failure"):
        apply_anchor_approval(
            path,
            checkout=checkout,
            approval_digest=preview["approval_digest"],
        )

    assert not record_path.exists()
    assert list(record_path.parent.iterdir()) == []


def test_anchor_approval_binds_raw_reference_source_and_provider_content(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    reference = path.parent / "references" / "ref-1.md"
    reference.write_bytes(b"Expected 0.\r\n")
    checkout = Path(__file__).parents[1]
    preview = preview_anchor_approval(path, checkout=checkout)
    apply_anchor_approval(
        path, checkout=checkout, approval_digest=preview["approval_digest"]
    )
    profile = load_campaign_profile(path, checkout=checkout)
    record, _ = load_anchor_approval(profile)
    bound = record["reference_digests"]["ref-1"]

    assert bound["source_digest"] == hashlib.sha256(b"Expected 0.\r\n").hexdigest()
    assert bound["content_digest"] == hashlib.sha256(b"Expected 0.\n").hexdigest()


def test_anchor_approval_cli_previews_then_applies_exact_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    checkout = Path(__file__).parents[1]

    assert main(
        [
            "anchors",
            "approve",
            "--profile",
            str(path),
            "--checkout",
            str(checkout),
        ]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["protocol"] == "kpo.evaluator-anchor-approval-preview/v1"

    assert main(
        [
            "anchors",
            "approve",
            "--profile",
            str(path),
            "--checkout",
            str(checkout),
            "--approve",
            preview["approval_digest"],
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["state"] == "approved"


class _CaptureEvaluatorExecutor:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def invoke(
        self,
        config: object,
        role: str,
        request: dict[str, object],
        *,
        context: object,
    ) -> dict[str, object]:
        assert role == "evaluator"
        self.requests.append(request)
        return {
            "dimensions": [
                {
                    "name": "alignment",
                    "score": 0.5,
                    "explanation": "synthetic",
                    "citations": [request["rollout"]["digest"]],  # type: ignore[index]
                }
            ],
            "failure_signals": [],
            "aggregate_score": 0.5,
        }


def _captured_anchor_request(path: Path) -> dict[str, object]:
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    assert profile.evaluator_audit is not None
    anchor = profile.evaluator_audit.anchors[0]
    request = build_anchor_evaluator_request(profile, anchor)
    capture = _CaptureEvaluatorExecutor()
    CommandEvaluatorProvider(
        profile.base.evaluator,
        profile.base.rubric,
        executor=capture,
    ).evaluate(request)
    assert len(capture.requests) == 1
    return capture.requests[0]


def test_fixed_anchor_request_is_blinded_deterministic_and_private(
    tmp_path: Path,
) -> None:
    path = _add_evaluator_audit(_campaign_profile(tmp_path / "external"))
    before = _captured_anchor_request(path)
    profile = load_campaign_profile(path, checkout=Path(__file__).parents[1])
    assert profile.evaluator_audit is not None
    anchor = profile.evaluator_audit.anchors[0]

    assert before["case_id"] == anchor.blinded_digest
    assert before["random_seed"] == 0
    rollout = before["rollout"]
    assert rollout["case_id"] == anchor.blinded_digest  # type: ignore[index]
    assert rollout["policy_digest"] == anchor.request_digest  # type: ignore[index]
    assert rollout["model_id"] == "kpo-anchor-fixture/v1"  # type: ignore[index]
    assert tuple(rollout["retrieved_policy_ids"]) == ()  # type: ignore[index]

    manifest = path.parent / "anchors.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    rows[1]["expected_scores"]["alignment"] = 0.10
    rows[1]["provenance"] = ["private-review-changed"]
    manifest.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    after = _captured_anchor_request(path)

    assert after == before
    serialized = json.dumps(after, sort_keys=True)
    assert "anchor-a" not in serialized
    assert "private-review" not in serialized
    assert "expected_scores" not in serialized
    assert "provenance" not in serialized


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows.append(dict(rows[0])), "duplicate anchor_id"),
        (
            lambda rows: rows[0].update(reference_ids=["missing-reference"]),
            "missing anchor references",
        ),
        (
            lambda rows: rows[0].update(reference_ids=["ref-1", "ref-1"]),
            "duplicate reference_ids",
        ),
        (
            lambda rows: rows[0].update(expected_scores={"unknown": 0.5}),
            "expected score dimensions",
        ),
        (
            lambda rows: rows[0].update(expected_scores={"alignment": 1.1}),
            "expected score",
        ),
        (
            lambda rows: rows[0].update(provenance=["same", "same"]),
            "duplicate provenance",
        ),
    ],
)
def test_evaluator_anchor_manifest_rejects_malformed_rows(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    path = _campaign_profile(tmp_path / "external")
    rows: list[dict[str, object]] = [
        {
            "protocol": "kpo.evaluator-anchor/v1",
            "anchor_id": "anchor-a",
            "rollout_path": "./anchors/anchor-a.txt",
            "reference_ids": ["ref-1"],
            "expected_scores": {"alignment": 0.5},
            "provenance": ["synthetic-review"],
        }
    ]
    mutate(rows)  # type: ignore[operator]
    _add_evaluator_audit(path, rows)

    with pytest.raises(ValueError, match=message):
        load_campaign_profile(path, checkout=Path(__file__).parents[1])


def test_evaluator_audit_rejects_unknown_fields_unsafe_paths_and_bad_budget(
    tmp_path: Path,
) -> None:
    checkout = Path(__file__).parents[1]

    unknown = _add_evaluator_audit(_campaign_profile(tmp_path / "unknown"))
    unknown.write_text(
        unknown.read_text().replace(
            "max_provider_calls = 5", 'max_provider_calls = 5\nextra = "no"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown evaluator_audit fields"):
        load_campaign_profile(unknown, checkout=checkout)

    bad_budget = _add_evaluator_audit(_campaign_profile(tmp_path / "budget"))
    bad_budget.write_text(
        bad_budget.read_text().replace("max_provider_calls = 5", "max_provider_calls = 0"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_provider_calls"):
        load_campaign_profile(bad_budget, checkout=checkout)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    unsafe_path = _campaign_profile(tmp_path / "unsafe")
    row = {
        "protocol": "kpo.evaluator-anchor/v1",
        "anchor_id": "anchor-a",
        "rollout_path": str(outside),
        "reference_ids": ["ref-1"],
        "expected_scores": {"alignment": 0.5},
        "provenance": ["synthetic-review"],
    }
    _add_evaluator_audit(unsafe_path, [row])
    with pytest.raises(ValueError, match="rollout_path.*outside profile"):
        load_campaign_profile(unsafe_path, checkout=checkout)
