from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kpo.digest import canonical_digest


class ActiveStateInterrupted(RuntimeError):
    """Raised by fault injection after an active pointer was replaced."""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class ActiveAdvancePreview:
    kind: str
    target: str | None
    parent_digest: str
    candidate_digest: str
    evidence_digest: str
    review_digest: str
    candidate_owner_identity: str
    reviewer_identity: str
    approval_digest: str


class ActiveStateManager:
    def __init__(self, runtime_home: Path) -> None:
        self.runtime_home = runtime_home.expanduser().resolve()
        self.runtime_home.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.runtime_home / "promotion-journal.jsonl"
        self.pending_path = self.runtime_home / "promotion-pending.json"
        self.policy_path = self.runtime_home / "active-policy.json"
        self.components_path = self.runtime_home / "active-components.json"
        if self.pending_path.exists():
            self.recover_pending()
        if self.journal_path.exists():
            self.reconcile()

    def _append_journal(self, entry: dict[str, Any]) -> None:
        entry = {**entry, "record_digest": canonical_digest(entry)}
        with self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _journal_entries(self) -> tuple[dict[str, Any], ...]:
        if not self.journal_path.exists():
            return ()
        entries: list[dict[str, Any]] = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            digest = entry.pop("record_digest", None)
            if digest != canonical_digest(entry):
                raise ValueError("active-state journal record digest mismatch")
            entries.append(entry)
        return tuple(entries)

    def _derived_state(self) -> tuple[str | None, dict[str, str]]:
        policy: str | None = None
        components: dict[str, str] = {}
        for entry in self._journal_entries():
            if entry.get("state") not in {"committed", "rolled_back"}:
                continue
            kind = entry.get("kind")
            active = entry.get("active_digest")
            if not isinstance(active, str) or not active:
                raise ValueError("active-state journal has invalid digest")
            if kind == "policy":
                policy = active
            elif kind == "component":
                target = entry.get("target")
                if not isinstance(target, str) or not target:
                    raise ValueError("active-state journal has invalid target")
                components[target] = active
            else:
                raise ValueError("active-state journal has invalid kind")
        return policy, components

    def reconcile(self) -> None:
        expected_policy, expected_components = self._derived_state()
        actual_policy = (
            self._read_digest(self.policy_path) if self.policy_path.exists() else None
        )
        actual_components = self._components()
        if actual_policy != expected_policy or actual_components != expected_components:
            raise ValueError("active pointer index disagrees with committed journal")

    def initialize_policy(self, digest: str) -> None:
        if self.policy_path.exists():
            raise ValueError("active policy is already initialized")
        _atomic_json(self.policy_path, {"digest": digest})
        self._append_journal(
            {
                "protocol": "kpo.active-state/v1",
                "action": "initialize",
                "kind": "policy",
                "target": None,
                "previous_digest": None,
                "active_digest": digest,
                "state": "committed",
            }
        )

    def initialize_component(self, target: str, digest: str) -> None:
        components = self._components()
        if target in components:
            raise ValueError("active component is already initialized")
        components[target] = digest
        _atomic_json(self.components_path, components)
        self._append_journal(
            {
                "protocol": "kpo.active-state/v1",
                "action": "initialize",
                "kind": "component",
                "target": target,
                "previous_digest": None,
                "active_digest": digest,
                "state": "committed",
            }
        )

    def active_policy_digest(self) -> str:
        return self._read_digest(self.policy_path)

    def active_component_digest(self, target: str) -> str:
        try:
            return self._components()[target]
        except KeyError as error:
            raise KeyError(f"unknown active component: {target}") from error

    def _read_digest(self, path: Path) -> str:
        if not path.exists():
            raise KeyError("active pointer is not initialized")
        value = json.loads(path.read_text(encoding="utf-8"))
        return str(value["digest"])

    def _components(self) -> dict[str, str]:
        if not self.components_path.exists():
            return {}
        value = json.loads(self.components_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("invalid active component index")
        return value

    def _preview(
        self,
        *,
        kind: str,
        target: str | None,
        parent_digest: str,
        candidate_digest: str,
        evidence_digest: str,
        review_digest: str,
        candidate_owner_identity: str,
        reviewer_identity: str,
    ) -> ActiveAdvancePreview:
        if not candidate_owner_identity.strip() or not reviewer_identity.strip():
            raise ValueError("candidate owner and reviewer identities are required")
        if candidate_owner_identity == reviewer_identity:
            raise ValueError("reviewer must be distinct from candidate owner")
        fields = {
            "kind": kind,
            "target": target,
            "parent_digest": parent_digest,
            "candidate_digest": candidate_digest,
            "evidence_digest": evidence_digest,
            "review_digest": review_digest,
            "candidate_owner_identity": candidate_owner_identity,
            "reviewer_identity": reviewer_identity,
        }
        return ActiveAdvancePreview(**fields, approval_digest=canonical_digest(fields))

    def preview_policy_advance(self, **values: str) -> ActiveAdvancePreview:
        if self.active_policy_digest() != values["parent_digest"]:
            raise ValueError("policy parent is not active")
        return self._preview(kind="policy", target=None, **values)

    def preview_component_advance(
        self, *, target: str, **values: str
    ) -> ActiveAdvancePreview:
        if self.active_component_digest(target) != values["parent_digest"]:
            raise ValueError("component parent is not active")
        return self._preview(kind="component", target=target, **values)

    def apply_policy_advance(
        self,
        preview: ActiveAdvancePreview,
        *,
        approval_digest: str,
        approver_identity: str,
        fault_after_pointer: bool = False,
        fault_after_commit: bool = False,
    ) -> None:
        self._apply(
            preview,
            approval_digest=approval_digest,
            approver_identity=approver_identity,
            fault_after_pointer=fault_after_pointer,
            fault_after_commit=fault_after_commit,
        )

    def apply_component_advance(
        self,
        preview: ActiveAdvancePreview,
        *,
        approval_digest: str,
        approver_identity: str,
        fault_after_pointer: bool = False,
        fault_after_commit: bool = False,
    ) -> None:
        self._apply(
            preview,
            approval_digest=approval_digest,
            approver_identity=approver_identity,
            fault_after_pointer=fault_after_pointer,
            fault_after_commit=fault_after_commit,
        )

    def _apply(
        self,
        preview: ActiveAdvancePreview,
        *,
        approval_digest: str,
        approver_identity: str,
        fault_after_pointer: bool,
        fault_after_commit: bool,
    ) -> None:
        if approval_digest != preview.approval_digest:
            raise ValueError("approval digest does not match preview")
        if not approver_identity.strip():
            raise ValueError("approver identity is required")
        if approver_identity == preview.candidate_owner_identity:
            raise ValueError("candidate owner cannot approve itself")
        current = (
            self.active_policy_digest()
            if preview.kind == "policy"
            else self.active_component_digest(str(preview.target))
        )
        if current != preview.parent_digest:
            raise ValueError("stale active-state preview")

        pending = {
            "protocol": "kpo.active-state-pending/v1",
            "state": "prepared",
            "preview_digest": canonical_digest(preview),
            "kind": preview.kind,
            "target": preview.target,
            "previous_digest": preview.parent_digest,
            "active_digest": preview.candidate_digest,
            "candidate_owner_identity": preview.candidate_owner_identity,
            "reviewer_identity": preview.reviewer_identity,
        }
        _atomic_json(self.pending_path, pending)
        self._append_journal(pending)
        if preview.kind == "policy":
            _atomic_json(self.policy_path, {"digest": preview.candidate_digest})
        else:
            components = self._components()
            components[str(preview.target)] = preview.candidate_digest
            _atomic_json(self.components_path, components)
        if fault_after_pointer:
            raise ActiveStateInterrupted("fault injected after pointer replacement")
        self._append_journal(
            {
                **pending,
                "protocol": "kpo.active-state/v1",
                "action": "advance",
                "state": "committed",
                "evidence_digest": preview.evidence_digest,
                "review_digest": preview.review_digest,
                "approval_digest": approval_digest,
                "approver_identity": approver_identity,
            }
        )
        if fault_after_commit:
            raise ActiveStateInterrupted("fault injected after committed journal")
        self.pending_path.unlink(missing_ok=True)

    def recover_pending(self) -> str:
        if not self.pending_path.exists():
            raise KeyError("no prepared active-state transaction")
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        if pending.get("state") != "prepared":
            raise ValueError("active-state transaction is not recoverable")
        preview_digest = pending.get("preview_digest")
        committed = any(
            entry.get("preview_digest") == preview_digest
            and entry.get("state") == "committed"
            and entry.get("action") == "advance"
            for entry in self._journal_entries()
        )
        if committed:
            self.pending_path.unlink()
            return "committed"
        kind = str(pending["kind"])
        target = pending.get("target")
        previous = str(pending["previous_digest"])
        candidate = str(pending["active_digest"])
        current = (
            self.active_policy_digest()
            if kind == "policy"
            else self.active_component_digest(str(target))
        )
        if current != candidate:
            raise ValueError("prepared transaction pointer does not match candidate")
        if kind == "policy":
            _atomic_json(self.policy_path, {"digest": previous})
        elif kind == "component":
            components = self._components()
            components[str(target)] = previous
            _atomic_json(self.components_path, components)
        else:
            raise ValueError("prepared transaction has unknown kind")
        self._append_journal(
            {
                **pending,
                "protocol": "kpo.active-state/v1",
                "action": "recover",
                "state": "rolled_back",
                "failed_digest": candidate,
                "active_digest": previous,
            }
        )
        self.pending_path.unlink()
        return "rolled_back"

    def rollback_component(
        self,
        *,
        target: str,
        failed_digest: str,
        ancestor_digest: str,
        regression_evidence_digest: str,
    ) -> None:
        self._rollback(
            kind="component",
            target=target,
            failed_digest=failed_digest,
            ancestor_digest=ancestor_digest,
            regression_evidence_digest=regression_evidence_digest,
        )

    def rollback_policy(
        self,
        *,
        failed_digest: str,
        ancestor_digest: str,
        regression_evidence_digest: str,
    ) -> None:
        self._rollback(
            kind="policy",
            target=None,
            failed_digest=failed_digest,
            ancestor_digest=ancestor_digest,
            regression_evidence_digest=regression_evidence_digest,
        )

    def _rollback(
        self,
        *,
        kind: str,
        target: str | None,
        failed_digest: str,
        ancestor_digest: str,
        regression_evidence_digest: str,
    ) -> None:
        current = (
            self.active_policy_digest()
            if kind == "policy"
            else self.active_component_digest(str(target))
        )
        if current != failed_digest:
            raise ValueError(f"failed {kind} is not active")
        known = self._known_digests(kind=kind, target=target)
        if ancestor_digest not in known:
            raise ValueError("rollback ancestor is unknown")
        if kind == "policy":
            _atomic_json(self.policy_path, {"digest": ancestor_digest})
        else:
            components = self._components()
            components[str(target)] = ancestor_digest
            _atomic_json(self.components_path, components)
        self._append_journal(
            {
                "protocol": "kpo.active-state/v1",
                "action": "rollback",
                "kind": kind,
                "target": target,
                "previous_digest": failed_digest,
                "active_digest": ancestor_digest,
                "regression_evidence_digest": regression_evidence_digest,
                "state": "committed",
            }
        )

    def _known_digests(self, *, kind: str, target: str | None) -> set[str]:
        known: set[str] = set()
        for entry in self._journal_entries():
            if entry.get("kind") == kind and entry.get("target") == target:
                known.add(str(entry["active_digest"]))
        return known
