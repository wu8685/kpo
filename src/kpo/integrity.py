from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping


class IntegrityViolation(RuntimeError):
    def __init__(self, kind: str, changed_paths: tuple[str, ...]) -> None:
        self.kind = kind
        self.changed_paths = changed_paths
        super().__init__(f"{kind}: monitored files changed")


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class IntegrityMonitor:
    def __init__(
        self,
        source_files: Mapping[Path, str],
        *,
        source_root: Path,
        target_root: Path | None = None,
        excluded_roots: tuple[Path, ...] = (),
    ) -> None:
        self.source_root = source_root.resolve()
        self.source_files = {path.resolve(): digest for path, digest in source_files.items()}
        self.target_root = target_root.resolve() if target_root is not None else None
        self.excluded_roots = tuple(path.resolve() for path in excluded_roots)
        self.target_snapshot = self._target_snapshot()

    @classmethod
    def from_relative_digests(
        cls,
        root: Path,
        digests: Mapping[str, str],
        *,
        extra_files: tuple[Path, ...] = (),
        target_root: Path | None = None,
        excluded_roots: tuple[Path, ...] = (),
    ) -> "IntegrityMonitor":
        resolved_root = root.resolve()
        sources = {resolved_root / relative: digest for relative, digest in digests.items()}
        for path in extra_files:
            sources[path.resolve()] = _digest(path.resolve()) or "missing"
        return cls(
            sources,
            source_root=resolved_root,
            target_root=target_root,
            excluded_roots=excluded_roots,
        )

    def _excluded(self, path: Path) -> bool:
        return any(_inside(path, root) for root in self.excluded_roots)

    def _target_snapshot(self) -> dict[str, str]:
        if self.target_root is None or not self.target_root.exists():
            return {}
        result: dict[str, str] = {}
        for path in sorted(item for item in self.target_root.rglob("*") if item.is_file()):
            resolved = path.resolve()
            if self._excluded(resolved):
                continue
            result[path.relative_to(self.target_root).as_posix()] = _digest(path) or "missing"
        return result

    @property
    def target_digest(self) -> str:
        digest = hashlib.sha256()
        for relative, value in sorted(self.target_snapshot.items()):
            digest.update(relative.encode("utf-8"))
            digest.update(value.encode("ascii"))
        return digest.hexdigest()

    def verify(self) -> None:
        changed_sources = tuple(
            sorted(
                path.relative_to(self.source_root).as_posix()
                if _inside(path, self.source_root)
                else path.name
                for path, expected in self.source_files.items()
                if _digest(path) != expected
            )
        )
        if changed_sources:
            raise IntegrityViolation("source_drift", changed_sources)
        current_target = self._target_snapshot()
        if current_target != self.target_snapshot:
            changed = tuple(
                sorted(
                    relative
                    for relative in set(current_target) | set(self.target_snapshot)
                    if current_target.get(relative) != self.target_snapshot.get(relative)
                )
            )
            raise IntegrityViolation("target_drift", changed)
