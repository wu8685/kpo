from pathlib import Path

from kpo.hygiene import scan_repository


def test_hygiene_allows_only_synthetic_fixture_data(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    synthetic = repository / "testdata" / "synthetic"
    synthetic.mkdir(parents=True)
    (synthetic / "case.json").write_text(
        '{"case_id":"synthetic-1"}\n', encoding="utf-8"
    )

    assert scan_repository(repository) == ()

    profiles = repository / "profiles"
    profiles.mkdir()
    (profiles / "real.yaml").write_text("id: real\n", encoding="utf-8")

    violations = scan_repository(repository)
    assert any(violation.code == "forbidden_path" for violation in violations)


def test_hygiene_detects_absolute_user_paths_and_secrets(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    absolute_path = "/" + "Users" + "/example/private"
    fake_secret = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    (repository / "bad.txt").write_text(
        f"path: {absolute_path}\ntoken: {fake_secret}\n",
        encoding="utf-8",
    )

    violations = scan_repository(repository)
    codes = {violation.code for violation in violations}

    assert "absolute_user_path" in codes
    assert "possible_secret" in codes


def test_external_denylist_match_does_not_echo_private_term(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    private_term = "private-subject-name"
    (repository / "note.md").write_text(private_term, encoding="utf-8")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(private_term + "\n", encoding="utf-8")

    violations = scan_repository(repository, external_denylist=denylist)

    assert any(violation.code == "external_denylist" for violation in violations)
    assert all(private_term not in violation.message for violation in violations)
