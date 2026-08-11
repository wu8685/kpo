from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from kpo.campaign_profile import (
    CampaignProfile,
    PromotionDestination,
    campaign_integrity_monitor,
    campaign_profile_snapshot,
    load_campaign_profile,
    resolve_candidate_destination,
    validate_campaign_id,
)
from kpo.digest import canonical_digest
from kpo.models import CandidatePatch, MutationKind
from kpo.promotion import PromotionPreview, _atomic_write
from kpo.regression import apply_candidate


@dataclass(frozen=True, slots=True)
class ManifestPreview:
    original: bytes
    reviewed: bytes
    original_digest: str
    reviewed_digest: str
    diff: str
    diff_digest: str


class BundleReport(Protocol):
    candidate_digest: str
    parent_policy_digest: str
    candidate_policy_digest: str
    passed: bool
    digest: str


@dataclass(frozen=True, slots=True)
class LoadedPromotionBundle:
    directory: Path
    bundle: dict[str, Any]
    approval_digest: str


class ExternalPromotionInterrupted(RuntimeError):
    """Raised by typed transaction fault injection after writes may have begun."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _row_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def propose_manifest_update(
    profile: CampaignProfile,
    candidate: CandidatePatch,
    destination: PromotionDestination,
) -> ManifestPreview:
    manifest_path = profile.policy_manifest_path
    original = manifest_path.read_bytes()
    lines = original.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    record_line_indexes: list[int] = []
    for line_index, line in enumerate(lines):
        if line.strip():
            records.append(json.loads(line))
            record_line_indexes.append(line_index)
    matches = [
        index
        for index, record in enumerate(records)
        if record["artifact_id"] == candidate.artifact_id
    ]

    if candidate.mutation is MutationKind.ADD:
        if matches:
            raise ValueError("add candidate artifact_id already exists")
        reviewed = original
        if reviewed and not reviewed.endswith(b"\n"):
            reviewed += b"\n"
        reviewed += _row_bytes(
            {
                "artifact_id": candidate.artifact_id,
                "path": destination.manifest_relative_path,
                "version": 1,
            }
        )
    elif candidate.mutation is MutationKind.EDIT:
        if len(matches) != 1:
            raise ValueError("edit candidate artifact_id must exist exactly once")
        index = matches[0]
        current = records[index]
        if current["path"] != destination.manifest_relative_path:
            raise ValueError("edit manifest path does not match destination")
        updated = {
            "artifact_id": current["artifact_id"],
            "path": current["path"],
            "version": current["version"] + 1,
        }
        reviewed_lines = list(lines)
        reviewed_lines[record_line_indexes[index]] = _row_bytes(updated)
        reviewed = b"".join(reviewed_lines)
        if not reviewed.endswith(b"\n"):
            reviewed += b"\n"
    else:
        raise ValueError(f"unsupported candidate mutation: {candidate.mutation.value}")

    original_text = original.decode("utf-8")
    reviewed_text = reviewed.decode("utf-8")
    diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            reviewed_text.splitlines(keepends=True),
            fromfile="a/policy-manifest.jsonl",
            tofile="b/policy-manifest.jsonl",
        )
    )
    return ManifestPreview(
        original=original,
        reviewed=reviewed,
        original_digest=_digest(original),
        reviewed_digest=_digest(reviewed),
        diff=diff,
        diff_digest=canonical_digest(diff),
    )


_BUNDLE_FIELDS = {
    "schema_version",
    "protocol",
    "campaign_id",
    "profile_id",
    "profile_snapshot_digest",
    "candidate",
    "candidate_digest",
    "report_digest",
    "report_passed",
    "parent_policy_digest",
    "candidate_policy_digest",
    "mutation",
    "artifact_id",
    "target_relative_path",
    "manifest_relative_path",
    "content",
    "manifest",
    "allowlist",
    "target_digest",
    "files",
}
_CANDIDATE_FIELDS = {
    "parent_policy_digest",
    "diagnosis_digest",
    "mutation",
    "artifact_id",
    "content",
    "provenance",
    "digest",
}
_CHANGE_FIELDS = {
    "original_digest",
    "reviewed_digest",
    "diff",
    "diff_digest",
}
_FILE_FIELDS = {
    "original_content",
    "reviewed_content",
    "original_manifest",
    "reviewed_manifest",
}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write(path, content)


def persist_promotion_bundle(
    profile: CampaignProfile,
    *,
    campaign_id: str,
    profile_snapshot_digest: str,
    target_digest: str,
    candidate: CandidatePatch,
    report: BundleReport,
    destination: PromotionDestination,
    content_preview: PromotionPreview,
    manifest_preview: ManifestPreview,
) -> Path:
    validate_campaign_id(campaign_id)
    if report.candidate_digest != candidate.digest or not report.passed:
        raise ValueError("passing report must match promotion candidate")
    if report.parent_policy_digest != candidate.parent_policy_digest:
        raise ValueError("candidate parent policy does not match report")
    if content_preview.candidate_digest != candidate.digest:
        raise ValueError("content preview does not match candidate")
    if content_preview.relative_path != destination.target_relative_path:
        raise ValueError("content preview path does not match destination")

    destination_path = (
        profile.target_root / destination.target_relative_path
    ).resolve()
    original_exists = destination_path.exists()
    original_content = destination_path.read_bytes() if original_exists else None
    if (original_content is None) != (content_preview.original_digest is None):
        raise ValueError("content preview original existence changed")
    if (
        original_content is not None
        and _digest(original_content) != content_preview.original_digest
    ):
        raise ValueError("content preview original bytes changed")
    reviewed_content = candidate.content.encode("utf-8")
    if _digest(reviewed_content) != content_preview.new_digest:
        raise ValueError("content preview reviewed bytes mismatch")

    files = {
        "original_content": _digest(original_content) if original_content is not None else None,
        "reviewed_content": _digest(reviewed_content),
        "original_manifest": _digest(manifest_preview.original),
        "reviewed_manifest": _digest(manifest_preview.reviewed),
    }
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "kpo.external-promotion.v1",
        "campaign_id": campaign_id,
        "profile_id": profile.base.profile_id,
        "profile_snapshot_digest": profile_snapshot_digest,
        "candidate": {
            "parent_policy_digest": candidate.parent_policy_digest,
            "diagnosis_digest": candidate.diagnosis_digest,
            "mutation": candidate.mutation.value,
            "artifact_id": candidate.artifact_id,
            "content": candidate.content,
            "provenance": list(candidate.provenance),
            "digest": candidate.digest,
        },
        "candidate_digest": candidate.digest,
        "report_digest": report.digest,
        "report_passed": report.passed,
        "parent_policy_digest": report.parent_policy_digest,
        "candidate_policy_digest": report.candidate_policy_digest,
        "mutation": candidate.mutation.value,
        "artifact_id": candidate.artifact_id,
        "target_relative_path": destination.target_relative_path,
        "manifest_relative_path": destination.manifest_relative_path,
        "content": {
            "original_digest": content_preview.original_digest,
            "reviewed_digest": content_preview.new_digest,
            "diff": content_preview.diff,
            "diff_digest": content_preview.diff_digest,
        },
        "manifest": {
            "original_digest": manifest_preview.original_digest,
            "reviewed_digest": manifest_preview.reviewed_digest,
            "diff": manifest_preview.diff,
            "diff_digest": manifest_preview.diff_digest,
        },
        "allowlist": list(profile.allowlist),
        "target_digest": target_digest,
        "files": files,
    }

    previews = profile.base.data_home / "promotions" / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    final = previews / campaign_id
    if final.exists():
        raise FileExistsError(f"promotion bundle already exists: {campaign_id}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{campaign_id}.", dir=previews))
    try:
        if original_content is not None:
            _atomic_write(temporary / "original-content.bin", original_content)
        _atomic_write(temporary / "reviewed-content.bin", reviewed_content)
        _atomic_write(temporary / "original-manifest.jsonl", manifest_preview.original)
        _atomic_write(temporary / "reviewed-manifest.jsonl", manifest_preview.reviewed)
        _write_json(temporary / "bundle.json", bundle)
        os.replace(temporary, final)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return final / "bundle.json"


def _strict_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing {label} fields: {sorted(missing)}")
    return value


def load_promotion_bundle(
    profile: CampaignProfile, campaign_id: str
) -> LoadedPromotionBundle:
    validate_campaign_id(campaign_id)
    directory = profile.base.data_home / "promotions" / "previews" / campaign_id
    bundle_path = directory / "bundle.json"
    if not bundle_path.is_file():
        raise KeyError(f"unknown promotion bundle: {campaign_id}")
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid promotion bundle JSON") from error
    bundle = _strict_keys(raw, _BUNDLE_FIELDS, label="bundle")
    candidate = _strict_keys(bundle["candidate"], _CANDIDATE_FIELDS, label="candidate")
    content = _strict_keys(bundle["content"], _CHANGE_FIELDS, label="content")
    manifest = _strict_keys(bundle["manifest"], _CHANGE_FIELDS, label="manifest")
    files = _strict_keys(bundle["files"], _FILE_FIELDS, label="files")
    if bundle["schema_version"] != 1 or bundle["protocol"] != "kpo.external-promotion.v1":
        raise ValueError("unsupported promotion bundle protocol")
    if bundle["campaign_id"] != campaign_id or bundle["profile_id"] != profile.base.profile_id:
        raise ValueError("promotion bundle identity mismatch")
    if (
        candidate["digest"] != bundle["candidate_digest"]
        or candidate["parent_policy_digest"] != bundle["parent_policy_digest"]
        or candidate["mutation"] != bundle["mutation"]
        or candidate["artifact_id"] != bundle["artifact_id"]
        or bundle["report_passed"] is not True
    ):
        raise ValueError("promotion bundle candidate/report relationship mismatch")
    if not isinstance(candidate["provenance"], list) or not all(
        isinstance(item, str) for item in candidate["provenance"]
    ):
        raise ValueError("promotion candidate provenance is invalid")
    candidate_fields = {
        "parent_policy_digest": candidate["parent_policy_digest"],
        "diagnosis_digest": candidate["diagnosis_digest"],
        "mutation": candidate["mutation"],
        "artifact_id": candidate["artifact_id"],
        "content": candidate["content"],
        "provenance": tuple(candidate["provenance"]),
    }
    if canonical_digest(candidate_fields) != candidate["digest"]:
        raise ValueError("promotion bundle candidate digest mismatch")
    if not isinstance(bundle["allowlist"], list) or not all(
        isinstance(item, str) for item in bundle["allowlist"]
    ):
        raise ValueError("promotion bundle allowlist is invalid")

    expected_files = {
        "bundle.json",
        "reviewed-content.bin",
        "original-manifest.jsonl",
        "reviewed-manifest.jsonl",
    }
    if files["original_content"] is not None:
        expected_files.add("original-content.bin")
    actual_files = {path.name for path in directory.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise ValueError("promotion bundle file set is invalid")
    for field, filename in (
        ("reviewed_content", "reviewed-content.bin"),
        ("original_manifest", "original-manifest.jsonl"),
        ("reviewed_manifest", "reviewed-manifest.jsonl"),
    ):
        if _digest((directory / filename).read_bytes()) != files[field]:
            raise ValueError(f"promotion bundle {filename} digest mismatch")
    if files["original_content"] is not None and _digest(
        (directory / "original-content.bin").read_bytes()
    ) != files["original_content"]:
        raise ValueError("promotion bundle original-content.bin digest mismatch")
    if content["original_digest"] != files["original_content"]:
        raise ValueError("promotion bundle original content digest mismatch")
    if content["reviewed_digest"] != files["reviewed_content"]:
        raise ValueError("promotion bundle reviewed content digest mismatch")
    if manifest["original_digest"] != files["original_manifest"]:
        raise ValueError("promotion bundle original manifest digest mismatch")
    if manifest["reviewed_digest"] != files["reviewed_manifest"]:
        raise ValueError("promotion bundle reviewed manifest digest mismatch")
    if canonical_digest(content["diff"]) != content["diff_digest"]:
        raise ValueError("promotion bundle content diff digest mismatch")
    if canonical_digest(manifest["diff"]) != manifest["diff_digest"]:
        raise ValueError("promotion bundle manifest diff digest mismatch")
    return LoadedPromotionBundle(
        directory=directory,
        bundle=bundle,
        approval_digest=canonical_digest(bundle),
    )


def _campaign_state_path(profile: CampaignProfile, campaign_id: str) -> Path:
    validate_campaign_id(campaign_id)
    return profile.base.data_home / "campaigns" / campaign_id / "campaign.json"


def _read_campaign_state(
    profile: CampaignProfile, campaign_id: str
) -> tuple[Path, dict[str, Any]]:
    path = _campaign_state_path(profile, campaign_id)
    if not path.is_file():
        raise KeyError(f"unknown campaign: {campaign_id}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("campaign_id") != campaign_id:
        raise ValueError("campaign state identity mismatch")
    return path, value


def _candidate_from_bundle(bundle: dict[str, Any]) -> CandidatePatch:
    raw = bundle["candidate"]
    return CandidatePatch(
        parent_policy_digest=raw["parent_policy_digest"],
        diagnosis_digest=raw["diagnosis_digest"],
        mutation=MutationKind(raw["mutation"]),
        artifact_id=raw["artifact_id"],
        content=raw["content"],
        provenance=tuple(raw["provenance"]),
        digest=raw["digest"],
    )


def _validate_campaign_binding(
    profile: CampaignProfile,
    state: dict[str, Any],
    loaded: LoadedPromotionBundle,
) -> None:
    bundle = loaded.bundle
    expected_bundle_path = (loaded.directory / "bundle.json").relative_to(
        profile.base.data_home
    ).as_posix()
    expected = {
        "bundle_path": expected_bundle_path,
        "candidate_digest": bundle["candidate_digest"],
        "candidate_policy_digest": bundle["candidate_policy_digest"],
        "parent_policy_digest": bundle["parent_policy_digest"],
        "profile_snapshot_digest": bundle["profile_snapshot_digest"],
        "report_digest": bundle["report_digest"],
        "report_passed": True,
        "promotion_diff": bundle["content"]["diff"],
        "promotion_diff_digest": bundle["content"]["diff_digest"],
        "manifest_diff": bundle["manifest"]["diff"],
        "manifest_diff_digest": bundle["manifest"]["diff_digest"],
        "target_digest": bundle["target_digest"],
    }
    if any(state.get(field) != value for field, value in expected.items()):
        raise ValueError("campaign state does not match promotion bundle")


def resume_campaign_promotion_preview(
    profile: CampaignProfile,
    campaign_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("state") not in {"running", "failed"}:
        raise ValueError("campaign state cannot finalize an installed bundle")
    loaded = load_promotion_bundle(profile, campaign_id)
    bundle = loaded.bundle
    if (
        state.get("profile_snapshot_digest") != bundle["profile_snapshot_digest"]
        or state.get("target_digest") != bundle["target_digest"]
        or state.get("parent_policy_digest") != bundle["parent_policy_digest"]
    ):
        raise ValueError("campaign state does not match installed promotion bundle")
    _validate_original_transaction(profile, loaded)
    budget_path = (
        profile.base.data_home / "campaigns" / campaign_id / "call-budget.json"
    )
    if not budget_path.is_file():
        raise ValueError("campaign call budget is missing for installed bundle")
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    call_count = budget.get("call_count")
    if (
        budget.get("max_calls") != profile.max_provider_calls
        or not isinstance(call_count, int)
        or isinstance(call_count, bool)
        or call_count < 0
    ):
        raise ValueError("campaign call budget does not match profile")
    return {
        "state": "promotion_previewed",
        "promotion_state": "previewed",
        "iteration": int(state.get("iteration", 0)) + 1,
        "call_count": budget["call_count"],
        "candidate_digest": bundle["candidate_digest"],
        "candidate_policy_digest": bundle["candidate_policy_digest"],
        "report_digest": bundle["report_digest"],
        "report_passed": True,
        "promotion_diff": bundle["content"]["diff"],
        "promotion_diff_digest": bundle["content"]["diff_digest"],
        "manifest_diff": bundle["manifest"]["diff"],
        "manifest_diff_digest": bundle["manifest"]["diff_digest"],
        "bundle_path": (loaded.directory / "bundle.json").relative_to(
            profile.base.data_home
        ).as_posix(),
        "rejected_candidate_count": len(state.get("rejected_candidates", [])),
    }


def _bundle_paths(
    profile: CampaignProfile, loaded: LoadedPromotionBundle
) -> tuple[Path, Path]:
    bundle = loaded.bundle
    target_relative = Path(bundle["target_relative_path"])
    manifest_relative = Path(bundle["manifest_relative_path"])
    for value, label in (
        (target_relative, "target-relative path"),
        (manifest_relative, "manifest-relative path"),
    ):
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError(f"promotion bundle {label} is unsafe")
    destination = (profile.target_root / target_relative).resolve()
    manifest_destination = (
        profile.base.path.parent / manifest_relative
    ).resolve()
    if destination != manifest_destination:
        raise ValueError("promotion bundle path coordinates do not resolve equally")
    if destination != profile.target_root and profile.target_root not in destination.parents:
        raise ValueError("promotion bundle destination escapes target_root")
    return destination, profile.policy_manifest_path


def _validate_original_transaction(
    profile: CampaignProfile, loaded: LoadedPromotionBundle
) -> tuple[CandidatePatch, Path, Path]:
    bundle = loaded.bundle
    if campaign_profile_snapshot(profile) != bundle["profile_snapshot_digest"]:
        raise ValueError("stale promotion bundle: profile snapshot changed")
    if profile.base.policy.digest != bundle["parent_policy_digest"]:
        raise ValueError("stale promotion bundle: parent policy changed")
    if tuple(bundle["allowlist"]) != profile.allowlist:
        raise ValueError("stale promotion bundle: allowlist changed")
    monitor = campaign_integrity_monitor(profile)
    if monitor.target_digest != bundle["target_digest"]:
        raise ValueError("stale promotion bundle: promotion target changed")
    candidate = _candidate_from_bundle(bundle)
    if candidate.digest != bundle["candidate_digest"]:
        raise ValueError("promotion candidate digest mismatch")
    destination_model = resolve_candidate_destination(profile, candidate)
    if (
        destination_model.target_relative_path != bundle["target_relative_path"]
        or destination_model.manifest_relative_path != bundle["manifest_relative_path"]
    ):
        raise ValueError("promotion destination no longer matches profile")
    destination, manifest_path = _bundle_paths(profile, loaded)
    content = bundle["content"]
    current_exists = destination.exists()
    current_content = destination.read_bytes() if current_exists else None
    current_digest = _digest(current_content) if current_content is not None else None
    if current_digest != content["original_digest"]:
        raise ValueError("stale promotion bundle: destination changed")
    if _digest(manifest_path.read_bytes()) != bundle["manifest"]["original_digest"]:
        raise ValueError("stale promotion bundle: manifest changed")
    reviewed_content = (loaded.directory / "reviewed-content.bin").read_bytes()
    if reviewed_content != candidate.content.encode("utf-8"):
        raise ValueError("reviewed content does not match candidate")
    original_text = current_content.decode("utf-8") if current_content is not None else ""
    expected_content_diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            candidate.content.splitlines(keepends=True),
            fromfile=(
                f"a/{bundle['target_relative_path']}"
                if current_content is not None
                else "/dev/null"
            ),
            tofile=f"b/{bundle['target_relative_path']}",
        )
    )
    if (
        bundle["content"]["diff"] != expected_content_diff
        or bundle["content"]["diff_digest"]
        != canonical_digest(expected_content_diff)
    ):
        raise ValueError("promotion bundle content diff does not match reviewed bytes")
    proposed_manifest = propose_manifest_update(
        profile, candidate, destination_model
    )
    if proposed_manifest.reviewed != (
        loaded.directory / "reviewed-manifest.jsonl"
    ).read_bytes():
        raise ValueError("reviewed manifest does not match candidate semantics")
    if (
        bundle["manifest"]["diff"] != proposed_manifest.diff
        or bundle["manifest"]["diff_digest"] != proposed_manifest.diff_digest
    ):
        raise ValueError("promotion bundle manifest diff does not match reviewed bytes")
    candidate_policy = apply_candidate(profile.base.policy, candidate)
    if candidate_policy.digest != bundle["candidate_policy_digest"]:
        raise ValueError("candidate policy digest does not match reviewed transaction")
    return candidate, destination, manifest_path


def preview_campaign_promotion(
    profile_path: Path, campaign_id: str, *, checkout: Path
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    _, state = _read_campaign_state(profile, campaign_id)
    if state.get("state") != "promotion_previewed":
        raise ValueError("campaign has no promotion preview")
    loaded = load_promotion_bundle(profile, campaign_id)
    _validate_campaign_binding(profile, state, loaded)
    _validate_original_transaction(profile, loaded)
    bundle = loaded.bundle
    return {
        "state": "preview",
        "campaign_id": campaign_id,
        "candidate_digest": bundle["candidate_digest"],
        "candidate_policy_digest": bundle["candidate_policy_digest"],
        "approval_digest": loaded.approval_digest,
        "target_relative_path": bundle["target_relative_path"],
        "manifest_relative_path": bundle["manifest_relative_path"],
        "content_diff": bundle["content"]["diff"],
        "manifest_diff": bundle["manifest"]["diff"],
    }


def _lock_path(profile: CampaignProfile) -> Path:
    return profile.base.data_home / "promotions" / "apply.lock"


def _acquire_lock(path: Path, campaign_id: str, approval_digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {
                "approval_digest": approval_digest,
                "campaign_id": campaign_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ValueError("another promotion transaction holds apply.lock") from error
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _journal_path(profile: CampaignProfile, approval_digest: str) -> Path:
    return profile.base.data_home / "promotions" / "journals" / f"{approval_digest}.json"


def apply_campaign_promotion(
    profile_path: Path,
    campaign_id: str,
    *,
    checkout: Path,
    approval_digest: str,
    fault_after_content: bool = False,
    fault_after_manifest: bool = False,
    fault_after_commit: bool = False,
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    state_path, state = _read_campaign_state(profile, campaign_id)
    if state.get("state") != "promotion_previewed":
        raise ValueError("campaign has not reached a promotion preview")
    loaded = load_promotion_bundle(profile, campaign_id)
    _validate_campaign_binding(profile, state, loaded)
    journal_path = _journal_path(profile, loaded.approval_digest)
    if state.get("promotion_state") == "applied" or (
        journal_path.is_file()
        and json.loads(journal_path.read_text(encoding="utf-8")).get("state")
        == "committed"
        and not _lock_path(profile).exists()
    ):
        raise ValueError("promotion transaction is already committed")
    if approval_digest != loaded.approval_digest:
        raise ValueError("approval digest does not match promotion bundle")
    _, destination, manifest_path = _validate_original_transaction(profile, loaded)
    lock_path = _lock_path(profile)
    _acquire_lock(lock_path, campaign_id, approval_digest)
    prepared = False
    try:
        # Close the validation/acquisition race before recording a prepared
        # transaction. A clean failure here may safely release the lock.
        profile = load_campaign_profile(profile_path, checkout=checkout)
        loaded = load_promotion_bundle(profile, campaign_id)
        _, current_state = _read_campaign_state(profile, campaign_id)
        if current_state.get("state") != "promotion_previewed":
            raise ValueError("campaign has not reached a promotion preview")
        _validate_campaign_binding(profile, current_state, loaded)
        _, destination, manifest_path = _validate_original_transaction(profile, loaded)
        journal = {
            "approval_digest": approval_digest,
            "bundle_path": loaded.directory.relative_to(
                profile.base.data_home
            ).as_posix(),
            "campaign_id": campaign_id,
            "state": "prepared",
        }
        _write_json(journal_path, journal)
        state["promotion_state"] = "applying"
        _write_json(state_path, state)
        prepared = True

        _atomic_write(
            destination, (loaded.directory / "reviewed-content.bin").read_bytes()
        )
        if fault_after_content:
            raise ExternalPromotionInterrupted("fault injected after content replacement")
        _atomic_write(
            manifest_path,
            (loaded.directory / "reviewed-manifest.jsonl").read_bytes(),
        )
        if fault_after_manifest:
            raise ExternalPromotionInterrupted("fault injected after manifest replacement")
        reloaded = load_campaign_profile(profile_path, checkout=checkout)
        if reloaded.base.policy.digest != loaded.bundle["candidate_policy_digest"]:
            raise ExternalPromotionInterrupted("applied policy digest verification failed")
        journal["state"] = "committed"
        _write_json(journal_path, journal)
        if fault_after_commit:
            raise ExternalPromotionInterrupted("fault injected after commit marker")
        state["promotion_state"] = "applied"
        _write_json(state_path, state)
        lock_path.unlink()
        return {
            "state": "applied",
            "campaign_id": campaign_id,
            "candidate_policy_digest": loaded.bundle["candidate_policy_digest"],
            "journal_path": journal_path.relative_to(profile.base.data_home).as_posix(),
        }
    except BaseException:
        if not prepared:
            lock_path.unlink(missing_ok=True)
        raise


def _load_lock(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("no interrupted promotion apply.lock exists")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("promotion apply.lock is invalid") from error
    value = _strict_keys(
        raw, {"campaign_id", "approval_digest"}, label="apply.lock"
    )
    if not all(isinstance(value[field], str) for field in value):
        raise ValueError("promotion apply.lock fields must be strings")
    return value


def recover_campaign_promotion(
    profile_path: Path,
    campaign_id: str,
    *,
    checkout: Path,
    fault_after_state: bool = False,
) -> dict[str, Any]:
    profile = load_campaign_profile(profile_path, checkout=checkout)
    state_path, state = _read_campaign_state(profile, campaign_id)
    if state.get("state") != "promotion_previewed":
        raise ValueError("campaign has not reached a promotion preview")
    lock_path = _lock_path(profile)
    lock = _load_lock(lock_path)
    if lock["campaign_id"] != campaign_id:
        raise ValueError("apply.lock belongs to another campaign")
    loaded = load_promotion_bundle(profile, campaign_id)
    _validate_campaign_binding(profile, state, loaded)
    if lock["approval_digest"] != loaded.approval_digest:
        raise ValueError("apply.lock approval digest does not match bundle")
    journal_path = _journal_path(profile, loaded.approval_digest)
    journal = (
        json.loads(journal_path.read_text(encoding="utf-8"))
        if journal_path.is_file()
        else None
    )
    destination, manifest_path = _bundle_paths(profile, loaded)

    if journal is not None and journal.get("state") == "committed":
        reviewed_content = (loaded.directory / "reviewed-content.bin").read_bytes()
        if not destination.is_file() or destination.read_bytes() != reviewed_content:
            raise ValueError("committed content does not match promotion bundle")
        reviewed_manifest = (
            loaded.directory / "reviewed-manifest.jsonl"
        ).read_bytes()
        if manifest_path.read_bytes() != reviewed_manifest:
            raise ValueError("committed manifest does not match promotion bundle")
        reloaded = load_campaign_profile(profile_path, checkout=checkout)
        if reloaded.base.policy.digest != loaded.bundle["candidate_policy_digest"]:
            raise ValueError("committed policy digest does not match promotion bundle")
        state["promotion_state"] = "applied"
        _write_json(state_path, state)
        lock_path.unlink()
        return {
            "state": "applied",
            "campaign_id": campaign_id,
            "finalized": True,
        }

    if journal is not None and journal.get("state") != "prepared":
        raise ValueError("promotion journal is not recoverable")
    if journal is None:
        journal = {
            "approval_digest": loaded.approval_digest,
            "bundle_path": loaded.directory.relative_to(
                profile.base.data_home
            ).as_posix(),
            "campaign_id": campaign_id,
            "state": "prepared",
        }
        _write_json(journal_path, journal)
    original_manifest = (loaded.directory / "original-manifest.jsonl").read_bytes()
    _atomic_write(manifest_path, original_manifest)
    original_content_path = loaded.directory / "original-content.bin"
    if original_content_path.is_file():
        _atomic_write(destination, original_content_path.read_bytes())
    else:
        destination.unlink(missing_ok=True)
    reloaded = load_campaign_profile(profile_path, checkout=checkout)
    if reloaded.base.policy.digest != loaded.bundle["parent_policy_digest"]:
        raise ValueError("recovered parent policy digest does not match bundle")
    state["promotion_state"] = "recovered"
    _write_json(state_path, state)
    if fault_after_state:
        raise ExternalPromotionInterrupted(
            "fault injected after recovery state persistence"
        )
    lock_path.unlink()
    return {"state": "recovered", "campaign_id": campaign_id}
