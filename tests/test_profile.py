from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kpo.profile import load_profile


def _write_profile(root: Path, *, schema_version: int = 1) -> Path:
    root.mkdir(parents=True)
    (root / "policy").mkdir()
    (root / "references").mkdir()
    (root / "bin").mkdir()
    for name in ("actor", "evaluator"):
        command = root / "bin" / name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    (root / "policy" / "base.md").write_text("Use evidence.\n", encoding="utf-8")
    (root / "references" / "expected.md").write_text(
        "State uncertainty.\n", encoding="utf-8"
    )
    (root / "policy.jsonl").write_text(
        json.dumps(
            {"artifact_id": "base", "path": "policy/base.md", "version": 1}
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "cases.jsonl").write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "prompt": "Analyze.",
                "visible_context": ["Visible fact."],
                "hidden_reference_ids": ["ref-1"],
                "tags": ["synthetic"],
                "information_cutoff": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "references.jsonl").write_text(
        json.dumps(
            {
                "artifact_id": "ref-1",
                "kind": "expected_result",
                "path": "references/expected.md",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "rubric.json").write_text(
        json.dumps(
            {
                "rubric_version": "v1",
                "dimensions": [
                    {
                        "name": "alignment",
                        "description": "Compare the result.",
                        "weight": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profile = root / "profile.toml"
    profile.write_text(
        f'''schema_version = {schema_version}
profile_id = "example"
data_home = "./runtime"

[policy]
manifest = "./policy.jsonl"
[cases]
manifest = "./cases.jsonl"
[references]
manifest = "./references.jsonl"
[rubric]
path = "./rubric.json"
[actor]
adapter = "command"
command = ["./bin/actor"]
model_id = "actor-model"
timeout_seconds = 10
pass_env = []
[evaluator]
adapter = "command"
command = ["./bin/evaluator"]
evaluator_id = "evaluator-model"
timeout_seconds = 10
pass_env = []
''',
        encoding="utf-8",
    )
    return profile


def test_profile_loads_relative_to_profile_not_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "external")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    loaded = load_profile(profile_path, checkout=Path(__file__).parents[1])

    assert loaded.profile_id == "example"
    assert loaded.policy.artifact_ids == ("base",)
    assert loaded.cases["case-1"].hidden_reference_ids == ("ref-1",)
    assert loaded.references["ref-1"].content == "State uncertainty.\n"
    assert loaded.rubric.dimension_names == ("alignment",)
    assert not loaded.data_home.exists()


@pytest.mark.parametrize("schema_version", [0, 2])
def test_profile_rejects_unsupported_schema(
    tmp_path: Path, schema_version: int
) -> None:
    profile_path = _write_profile(
        tmp_path / f"external-{schema_version}", schema_version=schema_version
    )

    with pytest.raises(ValueError, match="schema_version"):
        load_profile(profile_path, checkout=Path(__file__).parents[1])


def test_profile_rejects_unknown_and_duplicate_manifest_fields(tmp_path: Path) -> None:
    unknown_root = tmp_path / "unknown"
    unknown_profile = _write_profile(unknown_root)
    with unknown_profile.open("a", encoding="utf-8") as stream:
        stream.write("unexpected = true\n")
    with pytest.raises(ValueError, match="unknown"):
        load_profile(unknown_profile, checkout=Path(__file__).parents[1])

    duplicate_root = tmp_path / "duplicate"
    duplicate_profile = _write_profile(duplicate_root)
    line = (duplicate_root / "policy.jsonl").read_text(encoding="utf-8")
    (duplicate_root / "policy.jsonl").write_text(line + line, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_profile(duplicate_profile, checkout=Path(__file__).parents[1])


def test_profile_rejects_source_escape_and_symlink_escape(tmp_path: Path) -> None:
    escape_root = tmp_path / "escape"
    escape_profile = _write_profile(escape_root)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (escape_root / "policy.jsonl").write_text(
        json.dumps({"artifact_id": "base", "path": "../outside.md", "version": 1})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside profile"):
        load_profile(escape_profile, checkout=Path(__file__).parents[1])

    symlink_root = tmp_path / "symlink"
    symlink_profile = _write_profile(symlink_root)
    symlink = symlink_root / "policy" / "linked.md"
    try:
        os.symlink(outside, symlink)
    except OSError:
        pytest.skip("symlinks are unavailable")
    (symlink_root / "policy.jsonl").write_text(
        json.dumps(
            {"artifact_id": "base", "path": "policy/linked.md", "version": 1}
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside profile"):
        load_profile(symlink_profile, checkout=Path(__file__).parents[1])


def test_profile_rejects_runtime_overlap_and_checkout_sources(tmp_path: Path) -> None:
    root = tmp_path / "overlap"
    profile_path = _write_profile(root)
    text = profile_path.read_text(encoding="utf-8").replace(
        'data_home = "./runtime"', 'data_home = "./policy/base.md/runtime"'
    )
    profile_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="data_home"):
        load_profile(profile_path, checkout=Path(__file__).parents[1])

    checkout_profile = Path(__file__).parents[1] / "temporary-profile.toml"
    checkout_profile.write_text('schema_version = 1\n', encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="outside the code checkout"):
            load_profile(checkout_profile, checkout=Path(__file__).parents[1])
    finally:
        checkout_profile.unlink()


def test_profile_rejects_duplicate_pass_env(tmp_path: Path) -> None:
    root = tmp_path / "duplicate-env"
    profile_path = _write_profile(root)
    text = profile_path.read_text(encoding="utf-8").replace(
        "pass_env = []", 'pass_env = ["TOKEN", "TOKEN"]', 1
    )
    profile_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_profile(profile_path, checkout=Path(__file__).parents[1])


def _append_observer(profile_path: Path, name: str, identity: str) -> None:
    executable = profile_path.parent / "bin" / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    with profile_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f'''\n[[observer_evaluators]]
name = "{name}"
adapter = "command"
command = ["./bin/{name}"]
evaluator_id = "{identity}"
timeout_seconds = 10
pass_env = []
'''
        )


def test_profile_loads_observers_in_semantic_name_order(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path / "observers")
    _append_observer(profile_path, "observer-z", "evaluator-z")
    _append_observer(profile_path, "observer-a", "evaluator-a")

    loaded = load_profile(profile_path, checkout=Path(__file__).parents[1])

    assert tuple(item.name for item in loaded.observer_evaluators) == (
        "observer-a",
        "observer-z",
    )
    assert tuple(item.config.identity for item in loaded.observer_evaluators) == (
        "evaluator-a",
        "evaluator-z",
    )
    assert loaded.observer_evaluators[0].config.working_directory == (
        loaded.data_home / "provider-work" / "evaluators" / "observer-a"
    )


@pytest.mark.parametrize(
    ("second_name", "second_identity", "match"),
    [
        ("observer-a", "evaluator-b", "duplicate.*name"),
        ("observer-b", "evaluator-a", "duplicate.*identity"),
        ("observer-b", "evaluator-model", "duplicate.*identity"),
    ],
)
def test_profile_rejects_duplicate_observer_coordinates(
    tmp_path: Path, second_name: str, second_identity: str, match: str
) -> None:
    profile_path = _write_profile(tmp_path / "duplicate-observers")
    _append_observer(profile_path, "observer-a", "evaluator-a")
    _append_observer(profile_path, second_name, second_identity)

    with pytest.raises(ValueError, match=match):
        load_profile(profile_path, checkout=Path(__file__).parents[1])


def test_profile_rejects_unsafe_or_unknown_observer_fields(tmp_path: Path) -> None:
    unsafe = _write_profile(tmp_path / "unsafe-observer")
    _append_observer(unsafe, "observer.a", "evaluator-a")
    unsafe.write_text(
        unsafe.read_text(encoding="utf-8").replace(
            'name = "observer.a"', 'name = "../observer"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name"):
        load_profile(unsafe, checkout=Path(__file__).parents[1])

    unknown = _write_profile(tmp_path / "unknown-observer")
    _append_observer(unknown, "observer-a", "evaluator-a")
    with unknown.open("a", encoding="utf-8") as stream:
        stream.write('unexpected = true\n')
    with pytest.raises(ValueError, match="unknown observer"):
        load_profile(unknown, checkout=Path(__file__).parents[1])
