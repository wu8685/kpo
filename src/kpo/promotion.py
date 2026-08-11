from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from kpo.digest import canonical_digest
from kpo.models import CandidatePatch, MutationKind


class PromotionReport(Protocol):
    """The stable report boundary required to create a promotion preview."""

    candidate_digest: str
    passed: bool
    digest: str


class PromotionInterrupted(RuntimeError):
    """Raised by fault injection after the destination has been replaced."""


@dataclass(frozen=True, slots=True)
class PromotionPreview:
    candidate_digest: str
    regression_report_digest: str
    target_root: Path
    relative_path: str
    allowlist: tuple[str, ...]
    original_digest: str | None
    new_digest: str
    new_content: str
    diff: str
    diff_digest: str
    preview_digest: str


@dataclass(frozen=True, slots=True)
class PromotionResult:
    state: str
    preview_digest: str
    journal_path: Path


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: str
    preview_digest: str
    journal_path: Path


def _bytes_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    _atomic_write(path, (payload + "\n").encode("utf-8"))


def _normalize_relative(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} violates promotion allowlist")
    return path


def _resolve_allowlisted_target(
    target_root: Path, relative_path: str, allowlist: tuple[str, ...]
) -> tuple[Path, Path]:
    root = target_root.expanduser().resolve()
    relative = _normalize_relative(relative_path, label="destination path")
    prefixes = tuple(
        _normalize_relative(prefix, label="allowlist entry") for prefix in allowlist
    )
    if not prefixes:
        raise ValueError("promotion allowlist is required")
    if not any(relative == prefix or prefix in relative.parents for prefix in prefixes):
        raise ValueError("destination path is outside promotion allowlist")
    destination = (root / relative).resolve()
    if destination != root and root not in destination.parents:
        raise ValueError("destination path is outside promotion allowlist")
    return relative, destination


class PromotionManager:
    def __init__(self, runtime_home: Path) -> None:
        self.runtime_home = runtime_home.expanduser().resolve()
        self.journals_dir = self.runtime_home / "journals"
        self.snapshots_dir = self.runtime_home / "snapshots"
        self.journals_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def preview(
        self,
        candidate: CandidatePatch,
        report: PromotionReport,
        *,
        target_root: Path,
        relative_path: str,
        allowlist: tuple[str, ...],
    ) -> PromotionPreview:
        if not report.passed:
            raise ValueError("regression report did not pass promotion gates")
        if report.candidate_digest != candidate.digest:
            raise ValueError("regression report does not match candidate digest")
        relative, destination = _resolve_allowlisted_target(
            target_root, relative_path, allowlist
        )
        exists = destination.exists()
        if candidate.mutation is MutationKind.ADD and exists:
            raise ValueError("add promotion target already exists")
        if candidate.mutation is MutationKind.EDIT and not exists:
            raise ValueError("edit promotion target does not exist")
        if candidate.mutation not in {MutationKind.ADD, MutationKind.EDIT}:
            raise NotImplementedError(
                f"promotion for {candidate.mutation.value} is not implemented in v0.1"
            )

        original = destination.read_bytes() if exists else b""
        updated = candidate.content.encode("utf-8")
        original_text = original.decode("utf-8") if exists else ""
        diff = "".join(
            difflib.unified_diff(
                original_text.splitlines(keepends=True),
                candidate.content.splitlines(keepends=True),
                fromfile=f"a/{relative}" if exists else "/dev/null",
                tofile=f"b/{relative}",
            )
        )
        diff_digest = canonical_digest(diff)
        fields = {
            "candidate_digest": candidate.digest,
            "regression_report_digest": report.digest,
            "target_root": str(target_root.expanduser().resolve()),
            "relative_path": str(relative),
            "allowlist": allowlist,
            "original_digest": _bytes_digest(original) if exists else None,
            "new_digest": _bytes_digest(updated),
            "diff_digest": diff_digest,
        }
        return PromotionPreview(
            candidate_digest=candidate.digest,
            regression_report_digest=report.digest,
            target_root=target_root.expanduser().resolve(),
            relative_path=str(relative),
            allowlist=allowlist,
            original_digest=fields["original_digest"],
            new_digest=fields["new_digest"],
            new_content=candidate.content,
            diff=diff,
            diff_digest=diff_digest,
            preview_digest=canonical_digest(fields),
        )

    def apply(
        self,
        preview: PromotionPreview,
        *,
        approval_digest: str,
        fault_after_replace: bool = False,
    ) -> PromotionResult:
        if approval_digest != preview.diff_digest:
            raise ValueError("approval digest does not match preview diff")
        relative, destination = _resolve_allowlisted_target(
            preview.target_root, preview.relative_path, preview.allowlist
        )
        exists = destination.exists()
        current = destination.read_bytes() if exists else b""
        current_digest = _bytes_digest(current) if exists else None
        if current_digest != preview.original_digest:
            raise ValueError("stale promotion preview: destination has changed")

        snapshot_dir = self.snapshots_dir / preview.preview_digest
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / "original.bin"
        if exists:
            _atomic_write(snapshot_path, current)

        journal_path = self.journals_dir / f"{preview.preview_digest}.json"
        journal = {
            "preview_digest": preview.preview_digest,
            "state": "prepared",
            "target_root": str(preview.target_root),
            "relative_path": str(relative),
            "original_exists": exists,
            "original_digest": preview.original_digest,
            "new_digest": preview.new_digest,
            "snapshot_path": str(snapshot_path) if exists else None,
        }
        _atomic_json(journal_path, journal)

        # Store the exact reviewed content beside the journal. Reconstructing
        # content from a unified diff would lose unchanged context and precise
        # newline information.
        reviewed_path = snapshot_dir / "reviewed.txt"
        reviewed_bytes = preview.new_content.encode("utf-8")
        if _bytes_digest(reviewed_bytes) != preview.new_digest:
            raise ValueError("preview content digest does not match reviewed content")
        _atomic_write(reviewed_path, reviewed_bytes)
        _atomic_write(destination, reviewed_bytes)

        if fault_after_replace:
            raise PromotionInterrupted("fault injected after destination replacement")

        journal["state"] = "committed"
        _atomic_json(journal_path, journal)
        return PromotionResult(
            state="committed",
            preview_digest=preview.preview_digest,
            journal_path=journal_path,
        )

    def recover(self, preview_digest: str) -> RecoveryResult:
        journal_path = self.journals_dir / f"{preview_digest}.json"
        if not journal_path.exists():
            raise KeyError(f"unknown promotion journal: {preview_digest}")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal["state"] != "prepared":
            raise ValueError(f"promotion journal is not recoverable: {journal['state']}")
        target_root = Path(journal["target_root"])
        _, destination = _resolve_allowlisted_target(
            target_root,
            journal["relative_path"],
            (str(Path(journal["relative_path"]).parent),),
        )
        if journal["original_exists"]:
            snapshot_path = Path(journal["snapshot_path"])
            _atomic_write(destination, snapshot_path.read_bytes())
        else:
            destination.unlink(missing_ok=True)
        journal["state"] = "rolled_back"
        _atomic_json(journal_path, journal)
        return RecoveryResult(
            state="rolled_back",
            preview_digest=preview_digest,
            journal_path=journal_path,
        )
