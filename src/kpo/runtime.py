from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from kpo.digest import canonical_digest


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EVALUATED = "evaluated"
    DIAGNOSED = "diagnosed"
    CANDIDATE = "candidate"
    REGRESSED = "regressed"
    PROMOTION_PREVIEWED = "promotion_previewed"
    PROMOTED = "promoted"


_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.RUNNING: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.COMPLETED: frozenset({RunState.EVALUATED}),
    RunState.EVALUATED: frozenset({RunState.DIAGNOSED}),
    RunState.DIAGNOSED: frozenset({RunState.CANDIDATE}),
    RunState.CANDIDATE: frozenset({RunState.REGRESSED}),
    RunState.REGRESSED: frozenset({RunState.PROMOTION_PREVIEWED}),
    RunState.PROMOTION_PREVIEWED: frozenset({RunState.PROMOTED}),
    RunState.FAILED: frozenset(),
    RunState.PROMOTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    digest: str
    kind: str
    path: Path


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    case_id: str
    policy_digest: str
    state: RunState


def resolve_data_home(checkout: Path, requested: Path | None) -> Path:
    if requested is None:
        raise ValueError("runtime data home is required")
    checkout_root = checkout.expanduser().resolve()
    runtime_root = requested.expanduser().resolve()
    if runtime_root == checkout_root or checkout_root in runtime_root.parents:
        raise ValueError("runtime data home must be outside the code checkout")
    return runtime_root


class RuntimeStore:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().resolve()
        self.artifacts_dir = self.data_home / "artifacts" / "sha256"
        self.database_path = self.data_home / "kpo.sqlite3"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    digest TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def put_artifact(self, kind: str, content: str) -> ArtifactRecord:
        if not kind.strip():
            raise ValueError("artifact kind is required")
        digest = canonical_digest({"kind": kind, "content": content})
        relative_path = Path(digest[:2]) / f"{digest}.txt"
        target = self.artifacts_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{digest}.",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts
                    (digest, kind, relative_path, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (digest, kind, str(relative_path), now),
            )
        return ArtifactRecord(digest=digest, kind=kind, path=target)

    def create_run(
        self, run_id: str, *, case_id: str, policy_digest: str
    ) -> RunRecord:
        if not run_id.strip():
            raise ValueError("run_id is required")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, case_id, policy_digest, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    case_id,
                    policy_digest,
                    RunState.PENDING.value,
                    now,
                    now,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, case_id, policy_digest, state
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return RunRecord(
            run_id=row["run_id"],
            case_id=row["case_id"],
            policy_digest=row["policy_digest"],
            state=RunState(row["state"]),
        )

    def transition_run(self, run_id: str, target: RunState) -> RunRecord:
        current = self.get_run(run_id)
        if target not in _ALLOWED_TRANSITIONS[current.state]:
            raise ValueError(
                f"invalid run transition: {current.state.value} -> {target.value}"
            )
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (target.value, now, run_id),
            )
        return self.get_run(run_id)
