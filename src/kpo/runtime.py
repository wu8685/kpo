from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from kpo.digest import canonical_digest


_RUNTIME_SCHEMA_VERSION = 2


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
    profile_digest: str | None = None


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
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_meta'"
            ).fetchone()
            if has_meta:
                version_row = connection.execute(
                    "SELECT schema_version FROM runtime_meta"
                ).fetchone()
                if version_row and version_row[0] > _RUNTIME_SCHEMA_VERSION:
                    raise ValueError("newer runtime schema is not supported")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    schema_version INTEGER NOT NULL
                )
                """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS artifacts (
                    digest TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    profile_digest TEXT
                )
                """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS run_artifacts (
                    run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,
                    PRIMARY KEY (run_id, role),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id),
                    FOREIGN KEY (artifact_digest) REFERENCES artifacts(digest)
                )
                """
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(runs)")
                }
                if "profile_digest" not in columns:
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN profile_digest TEXT"
                    )
                if not connection.execute("SELECT 1 FROM runtime_meta").fetchone():
                    connection.execute(
                        "INSERT INTO runtime_meta (schema_version) VALUES (?)",
                        (_RUNTIME_SCHEMA_VERSION,),
                    )
                else:
                    connection.execute(
                        "UPDATE runtime_meta SET schema_version = ?",
                        (_RUNTIME_SCHEMA_VERSION,),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

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
        self,
        run_id: str,
        *,
        case_id: str,
        policy_digest: str,
        profile_digest: str | None = None,
    ) -> RunRecord:
        if not run_id.strip():
            raise ValueError("run_id is required")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, case_id, policy_digest, state, created_at, updated_at,
                     profile_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    case_id,
                    policy_digest,
                    RunState.PENDING.value,
                    now,
                    now,
                    profile_digest,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, case_id, policy_digest, state, profile_digest
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
            profile_digest=row["profile_digest"],
        )

    def link_artifact(self, run_id: str, role: str, digest: str) -> None:
        self.get_run(run_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_artifacts (run_id, role, artifact_digest)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id, role) DO UPDATE SET artifact_digest=excluded.artifact_digest
                """,
                (run_id, role, digest),
            )

    def run_artifacts(self, run_id: str) -> dict[str, str]:
        self.get_run(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role, artifact_digest FROM run_artifacts WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return {row["role"]: row["artifact_digest"] for row in rows}

    def read_artifact(self, digest: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relative_path FROM artifacts WHERE digest = ?", (digest,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact: {digest}")
        return (self.artifacts_dir / row["relative_path"]).read_text(encoding="utf-8")

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
