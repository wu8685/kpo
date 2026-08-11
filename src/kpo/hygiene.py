from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
_FORBIDDEN_DIRECTORIES = frozenset(
    {"profiles", "datasets", "runs", "artifacts", "candidates", "evidence"}
)
_ABSOLUTE_USER_PATH = re.compile(
    r"(?:/" + r"Users/|/" + r"home/)[^\s/]+|[A-Za-z]:\\" + r"Users\\"
)
_SECRET_PATTERNS = (
    re.compile(r"\bgh" + r"p_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bs" + r"k-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAK" + r"IA[A-Z0-9]{16}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE " + r"KEY-----"
    ),
)


@dataclass(frozen=True, slots=True)
class HygieneViolation:
    code: str
    path: str
    message: str


def _is_synthetic_fixture(relative: Path) -> bool:
    parts = relative.parts
    return len(parts) >= 2 and parts[:2] == ("testdata", "synthetic")


def _iter_repository_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in _SKIPPED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file():
            files.append(path)
    return tuple(sorted(files))


def scan_repository(
    root: Path, *, external_denylist: Path | None = None
) -> tuple[HygieneViolation, ...]:
    repository = root.expanduser().resolve()
    if not repository.is_dir():
        raise ValueError("repository root must be an existing directory")

    denylist: tuple[str, ...] = ()
    if external_denylist is not None:
        denylist_path = external_denylist.expanduser().resolve()
        denylist = tuple(
            line.strip().casefold()
            for line in denylist_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    violations: list[HygieneViolation] = []
    for path in _iter_repository_files(repository):
        relative = path.relative_to(repository)
        relative_text = relative.as_posix()
        forbidden_parts = set(relative.parts) & _FORBIDDEN_DIRECTORIES
        if forbidden_parts and not _is_synthetic_fixture(relative):
            violations.append(
                HygieneViolation(
                    code="forbidden_path",
                    path=relative_text,
                    message="real profile or runtime asset path is not repository content",
                )
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _ABSOLUTE_USER_PATH.search(text):
            violations.append(
                HygieneViolation(
                    code="absolute_user_path",
                    path=relative_text,
                    message="tracked text contains an absolute user path",
                )
            )
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            violations.append(
                HygieneViolation(
                    code="possible_secret",
                    path=relative_text,
                    message="tracked text contains a possible credential",
                )
            )
        folded = text.casefold()
        if any(term in folded for term in denylist):
            violations.append(
                HygieneViolation(
                    code="external_denylist",
                    path=relative_text,
                    message="tracked text matches an entry from the external denylist",
                )
            )
    return tuple(violations)
