from __future__ import annotations

import math
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from kpo.digest import canonical_digest
from kpo.models import Evaluation, Partition, ReferenceArtifact, Rollout
from kpo.profile import CommandProviderConfig


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_HEX = frozenset("0123456789abcdef")


def _unit(value: float, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{label} must be finite and between zero and one")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in _HEX for item in value
    ):
        raise ValueError(f"{label} must be a sha256 digest")


class ClaimType(str, Enum):
    FACTUAL = "factual"
    ANALYTICAL = "analytical"
    PREDICTIVE = "predictive"
    NORMATIVE = "normative"
    CONCLUSIVE = "conclusive"


@dataclass(frozen=True, slots=True)
class ReasoningClaim:
    claim_id: str
    claim_type: ClaimType
    text: str
    evidence_citations: tuple[str, ...]
    prerequisite_claim_ids: tuple[str, ...]
    confidence: float
    salience: float
    provenance: str

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not _SAFE_ID.fullmatch(
            self.claim_id
        ):
            raise ValueError("claim_id must be a safe identifier")
        if not isinstance(self.claim_type, ClaimType):
            raise ValueError("claim_type is invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("claim text is required")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_citations
        ):
            raise ValueError("evidence citations must be non-empty strings")
        if (
            len(set(self.prerequisite_claim_ids))
            != len(self.prerequisite_claim_ids)
            or any(
                not isinstance(item, str) or not _SAFE_ID.fullmatch(item)
                for item in self.prerequisite_claim_ids
            )
        ):
            raise ValueError("prerequisite claim IDs are invalid")
        _unit(self.confidence, "confidence")
        _unit(self.salience, "salience")
        if self.provenance not in {"agent_rollout", "reference_artifact"}:
            raise ValueError("claim provenance is invalid")


@dataclass(frozen=True, slots=True)
class ClaimGraph:
    protocol: str
    claims: tuple[ReasoningClaim, ...]
    digest: str

    @classmethod
    def create(cls, claims: tuple[ReasoningClaim, ...]) -> "ClaimGraph":
        if not claims:
            raise ValueError("claim graph must not be empty")
        by_id = {claim.claim_id: claim for claim in claims}
        if len(by_id) != len(claims):
            raise ValueError("duplicate claim_id")
        provenances = {claim.provenance for claim in claims}
        if len(provenances) != 1:
            raise ValueError("claim graph provenance must be uniform")
        for claim in claims:
            for prerequisite in claim.prerequisite_claim_ids:
                if prerequisite not in by_id:
                    raise ValueError("unknown prerequisite claim_id")
                if prerequisite == claim.claim_id:
                    raise ValueError("claim graph must be acyclic")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(claim_id: str) -> None:
            if claim_id in visiting:
                raise ValueError("claim graph must be acyclic")
            if claim_id in visited:
                return
            visiting.add(claim_id)
            for prerequisite in by_id[claim_id].prerequisite_claim_ids:
                visit(prerequisite)
            visiting.remove(claim_id)
            visited.add(claim_id)

        for claim in claims:
            visit(claim.claim_id)
        fields = {"protocol": "kpo.reasoning-claims/v1", "claims": claims}
        return cls(**fields, digest=canonical_digest(fields))


class DifferentialKind(str, Enum):
    MATCHED = "matched"
    CONTRADICTED = "contradicted"
    MISSING = "missing"
    SURPLUS = "surplus"
    FRAME_DIVERGENCE = "frame_divergence"


@dataclass(frozen=True, slots=True)
class DifferentialEntry:
    kind: DifferentialKind
    reference_claim_id: str | None
    agent_claim_id: str | None
    summary: str
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DifferentialKind):
            raise ValueError("differential kind is invalid")
        for value in (self.reference_claim_id, self.agent_claim_id):
            if value is not None and not _SAFE_ID.fullmatch(value):
                raise ValueError("differential claim coordinate is invalid")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("differential summary is required")
        _unit(self.confidence, "differential confidence")


@dataclass(frozen=True, slots=True)
class ClaimDifferential:
    protocol: str
    reference_claims_digest: str
    agent_claims_digest: str
    entries: tuple[DifferentialEntry, ...]
    digest: str

    @classmethod
    def create(
        cls,
        reference: ClaimGraph,
        agent: ClaimGraph,
        entries: tuple[DifferentialEntry, ...],
    ) -> "ClaimDifferential":
        reference_ids = {claim.claim_id for claim in reference.claims}
        agent_ids = {claim.claim_id for claim in agent.claims}
        seen: set[tuple[DifferentialKind, str | None, str | None]] = set()
        used_reference: set[str] = set()
        used_agent: set[str] = set()
        for entry in entries:
            both = entry.reference_claim_id is not None and entry.agent_claim_id is not None
            reference_only = entry.reference_claim_id is not None and entry.agent_claim_id is None
            agent_only = entry.reference_claim_id is None and entry.agent_claim_id is not None
            valid = (
                entry.kind
                in {
                    DifferentialKind.MATCHED,
                    DifferentialKind.CONTRADICTED,
                    DifferentialKind.FRAME_DIVERGENCE,
                }
                and both
            ) or (entry.kind is DifferentialKind.MISSING and reference_only) or (
                entry.kind is DifferentialKind.SURPLUS and agent_only
            )
            if not valid:
                raise ValueError("differential coordinates do not match kind")
            if (
                entry.reference_claim_id is not None
                and entry.reference_claim_id not in reference_ids
            ) or (
                entry.agent_claim_id is not None
                and entry.agent_claim_id not in agent_ids
            ):
                raise ValueError("differential coordinates reference unknown claims")
            coordinate = (
                entry.kind,
                entry.reference_claim_id,
                entry.agent_claim_id,
            )
            if coordinate in seen:
                raise ValueError("duplicate differential coordinates")
            seen.add(coordinate)
            if entry.reference_claim_id is not None:
                used_reference.add(entry.reference_claim_id)
            if entry.agent_claim_id is not None:
                used_agent.add(entry.agent_claim_id)
        if used_reference != reference_ids or used_agent != agent_ids:
            raise ValueError("differential entries must account for every claim")
        fields = {
            "protocol": "kpo.claim-differential/v1",
            "reference_claims_digest": reference.digest,
            "agent_claims_digest": agent.digest,
            "entries": entries,
        }
        return cls(**fields, digest=canonical_digest(fields))


class AxisAssessment(str, Enum):
    SUPPORTED = "supported"
    CHALLENGED = "challenged"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DifferentialAxis:
    assessment: AxisAssessment
    confidence: float
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, AxisAssessment):
            raise ValueError("axis assessment is invalid")
        _unit(self.confidence, "axis confidence")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("axis summary is required")


@dataclass(frozen=True, slots=True)
class GapSummary:
    kind: DifferentialKind
    reference_claim_type: ClaimType | None
    agent_claim_type: ClaimType | None
    frame_label: str
    confidence: float
    recommendation_category: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DifferentialKind):
            raise ValueError("gap kind is invalid")
        for value in (self.reference_claim_type, self.agent_claim_type):
            if value is not None and not isinstance(value, ClaimType):
                raise ValueError("gap claim type is invalid")
        if not isinstance(self.frame_label, str) or not self.frame_label.strip():
            raise ValueError("gap frame label is required")
        if (
            not isinstance(self.recommendation_category, str)
            or not _SAFE_ID.fullmatch(self.recommendation_category)
        ):
            raise ValueError("gap recommendation category is invalid")
        _unit(self.confidence, "gap confidence")


_CAVEATS = (
    "The reference is comparison material, not truth; reference is comparison material.",
    "Reasoning fidelity is not a substitute for conclusion validity or reasoning soundness.",
)


@dataclass(frozen=True, slots=True)
class DifferentialDiagnosis:
    protocol: str
    evaluation_digest: str
    claim_differential_digest: str
    reference_is_standard: bool
    conclusion_validity: DifferentialAxis
    reasoning_fidelity: DifferentialAxis
    reasoning_soundness: DifferentialAxis
    gaps: tuple[GapSummary, ...]
    caveats: tuple[str, ...]
    digest: str

    @classmethod
    def create(
        cls,
        *,
        evaluation_digest: str,
        claim_differential_digest: str,
        conclusion_validity: DifferentialAxis,
        reasoning_fidelity: DifferentialAxis,
        reasoning_soundness: DifferentialAxis,
        gaps: tuple[GapSummary, ...],
    ) -> "DifferentialDiagnosis":
        _digest(evaluation_digest, "evaluation_digest")
        _digest(claim_differential_digest, "claim_differential_digest")
        fields = {
            "protocol": "kpo.differential-diagnosis/v1",
            "evaluation_digest": evaluation_digest,
            "claim_differential_digest": claim_differential_digest,
            "reference_is_standard": False,
            "conclusion_validity": conclusion_validity,
            "reasoning_fidelity": reasoning_fidelity,
            "reasoning_soundness": reasoning_soundness,
            "gaps": gaps,
            "caveats": _CAVEATS,
        }
        return cls(**fields, digest=canonical_digest(fields))

    def to_public_value(self) -> dict[str, Any]:
        def axis(value: DifferentialAxis) -> dict[str, Any]:
            return {
                "assessment": value.assessment.value,
                "confidence": value.confidence,
                "summary": value.summary,
            }

        return {
            "protocol": self.protocol,
            "evaluation_digest": self.evaluation_digest,
            "claim_differential_digest": self.claim_differential_digest,
            "reference_is_standard": self.reference_is_standard,
            "conclusion_validity": axis(self.conclusion_validity),
            "reasoning_fidelity": axis(self.reasoning_fidelity),
            "reasoning_soundness": axis(self.reasoning_soundness),
            "gaps": [
                {
                    "kind": gap.kind.value,
                    "reference_claim_type": (
                        None
                        if gap.reference_claim_type is None
                        else gap.reference_claim_type.value
                    ),
                    "agent_claim_type": (
                        None
                        if gap.agent_claim_type is None
                        else gap.agent_claim_type.value
                    ),
                    "frame_label": gap.frame_label,
                    "confidence": gap.confidence,
                    "recommendation_category": gap.recommendation_category,
                }
                for gap in self.gaps
            ],
            "caveats": list(self.caveats),
            "digest": self.digest,
        }


def _invalid_response(message: str, error: Exception | None = None) -> None:
    from kpo.provider import ProviderFailure, ProviderFailureKind

    failure = ProviderFailure(ProviderFailureKind.INVALID_RESPONSE, message)
    if error is None:
        raise failure
    raise failure from error


def _strict(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _invalid_response(f"{label} response fields do not match the protocol")
    return value


def _claim_value(claim: ReasoningClaim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim_type": claim.claim_type.value,
        "text": claim.text,
        "evidence_citations": list(claim.evidence_citations),
        "prerequisite_claim_ids": list(claim.prerequisite_claim_ids),
        "confidence": claim.confidence,
        "salience": claim.salience,
        "provenance": claim.provenance,
    }


def _graph_value(graph: ClaimGraph) -> dict[str, Any]:
    return {
        "protocol": graph.protocol,
        "claims": [_claim_value(claim) for claim in graph.claims],
        "digest": graph.digest,
    }


def _differential_value(differential: ClaimDifferential) -> dict[str, Any]:
    return {
        "protocol": differential.protocol,
        "reference_claims_digest": differential.reference_claims_digest,
        "agent_claims_digest": differential.agent_claims_digest,
        "entries": [
            {
                "kind": entry.kind.value,
                "reference_claim_id": entry.reference_claim_id,
                "agent_claim_id": entry.agent_claim_id,
                "summary": entry.summary,
                "confidence": entry.confidence,
            }
            for entry in differential.entries
        ],
        "digest": differential.digest,
    }


class CommandReasoningExtractorProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        *,
        executor: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.context = context or {}

    def extract(
        self, *, source_kind: str, sources: tuple[tuple[str, str], ...]
    ) -> ClaimGraph:
        if source_kind not in {"agent_rollout", "reference_artifact"}:
            raise ValueError("source_kind is invalid")
        if not sources or len({source_id for source_id, _ in sources}) != len(sources):
            raise ValueError("extractor sources must be non-empty and unique")
        from kpo.provider import _dispatch

        value = _dispatch(
            self.config,
            "reasoning_extractor",
            {
                "source_kind": source_kind,
                "sources": [
                    {"source_id": source_id, "content": content}
                    for source_id, content in sources
                ],
                "claim_schema_version": "v1",
            },
            self.executor,
            self.context,
        )
        data = _strict(value, {"schema_version", "claims"}, "extractor")
        if data["schema_version"] != "v1":
            _invalid_response("extractor schema_version is invalid")
        if not isinstance(data["claims"], list):
            _invalid_response("extractor claims must be an array")
        authorized = {source_id for source_id, _ in sources}
        try:
            claims = []
            for raw in data["claims"]:
                item = _strict(
                    raw,
                    {
                        "claim_id",
                        "claim_type",
                        "text",
                        "evidence_citations",
                        "prerequisite_claim_ids",
                        "confidence",
                        "salience",
                        "provenance",
                    },
                    "claim",
                )
                if not isinstance(item["evidence_citations"], list) or not isinstance(
                    item["prerequisite_claim_ids"], list
                ):
                    raise ValueError("claim arrays are invalid")
                citations = tuple(item["evidence_citations"])
                if not set(citations).issubset(authorized):
                    raise ValueError("claim citation is not authorized")
                if item["provenance"] != source_kind:
                    raise ValueError("claim provenance does not match source_kind")
                claims.append(
                    ReasoningClaim(
                        claim_id=item["claim_id"],
                        claim_type=ClaimType(item["claim_type"]),
                        text=item["text"],
                        evidence_citations=citations,
                        prerequisite_claim_ids=tuple(item["prerequisite_claim_ids"]),
                        confidence=item["confidence"],
                        salience=item["salience"],
                        provenance=item["provenance"],
                    )
                )
            return ClaimGraph.create(tuple(claims))
        except (KeyError, TypeError, ValueError) as error:
            _invalid_response(f"extractor response violates the claim schema: {error}", error)


class CommandClaimDifferProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        *,
        executor: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.context = context or {}

    def compare(self, reference: ClaimGraph, agent: ClaimGraph) -> ClaimDifferential:
        from kpo.provider import _dispatch

        value = _dispatch(
            self.config,
            "claim_differ",
            {
                "reference_claims": _graph_value(reference),
                "agent_claims": _graph_value(agent),
            },
            self.executor,
            self.context,
        )
        data = _strict(value, {"schema_version", "entries"}, "claim differ")
        if data["schema_version"] != "v1" or not isinstance(data["entries"], list):
            _invalid_response("claim differ schema is invalid")
        try:
            entries = []
            for raw in data["entries"]:
                item = _strict(
                    raw,
                    {
                        "kind",
                        "reference_claim_id",
                        "agent_claim_id",
                        "summary",
                        "confidence",
                    },
                    "differential entry",
                )
                entries.append(
                    DifferentialEntry(
                        kind=DifferentialKind(item["kind"]),
                        reference_claim_id=item["reference_claim_id"],
                        agent_claim_id=item["agent_claim_id"],
                        summary=item["summary"],
                        confidence=item["confidence"],
                    )
                )
            return ClaimDifferential.create(reference, agent, tuple(entries))
        except (KeyError, TypeError, ValueError) as error:
            _invalid_response(f"claim differ response violates the schema: {error}", error)


class CommandDifferentialDiagnosticianProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        *,
        executor: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.context = context or {}

    def diagnose(
        self, differential: ClaimDifferential, evaluation: Evaluation
    ) -> DifferentialDiagnosis:
        from kpo.provider import _dispatch

        value = _dispatch(
            self.config,
            "differential_diagnostician",
            {
                "claim_differential": _differential_value(differential),
                "evaluation_summary": {
                    "rubric_version": evaluation.rubric_version,
                    "dimensions": [
                        {"name": item.name, "score": item.score}
                        for item in evaluation.dimensions
                    ],
                    "failure_kinds": [
                        item.kind.value for item in evaluation.failure_signals
                    ],
                    "evaluation_digest": evaluation.digest,
                },
            },
            self.executor,
            self.context,
        )
        data = _strict(
            value,
            {
                "schema_version",
                "reference_is_standard",
                "conclusion_validity",
                "reasoning_fidelity",
                "reasoning_soundness",
                "gaps",
            },
            "differential diagnosis",
        )
        if data["schema_version"] != "v1" or data["reference_is_standard"] is not False:
            _invalid_response("differential diagnosis schema or reference standard is invalid")
        try:
            def parse_axis(raw: Any) -> DifferentialAxis:
                item = _strict(
                    raw, {"assessment", "confidence", "summary"}, "diagnosis axis"
                )
                return DifferentialAxis(
                    AxisAssessment(item["assessment"]),
                    item["confidence"],
                    item["summary"],
                )

            if not isinstance(data["gaps"], list):
                raise ValueError("diagnosis gaps must be an array")
            gaps = []
            for raw in data["gaps"]:
                item = _strict(
                    raw,
                    {
                        "kind",
                        "reference_claim_type",
                        "agent_claim_type",
                        "frame_label",
                        "confidence",
                        "recommendation_category",
                    },
                    "diagnosis gap",
                )
                gaps.append(
                    GapSummary(
                        kind=DifferentialKind(item["kind"]),
                        reference_claim_type=(
                            None
                            if item["reference_claim_type"] is None
                            else ClaimType(item["reference_claim_type"])
                        ),
                        agent_claim_type=(
                            None
                            if item["agent_claim_type"] is None
                            else ClaimType(item["agent_claim_type"])
                        ),
                        frame_label=item["frame_label"],
                        confidence=item["confidence"],
                        recommendation_category=item["recommendation_category"],
                    )
                )
            return DifferentialDiagnosis.create(
                evaluation_digest=evaluation.digest,
                claim_differential_digest=differential.digest,
                conclusion_validity=parse_axis(data["conclusion_validity"]),
                reasoning_fidelity=parse_axis(data["reasoning_fidelity"]),
                reasoning_soundness=parse_axis(data["reasoning_soundness"]),
                gaps=tuple(gaps),
            )
        except (KeyError, TypeError, ValueError) as error:
            _invalid_response(
                f"differential diagnosis response violates the schema: {error}", error
            )


@dataclass(frozen=True, slots=True)
class DifferentialAnalysis:
    agent_claims: ClaimGraph
    reference_claims: ClaimGraph
    differential: ClaimDifferential
    diagnosis: DifferentialDiagnosis


def run_reasoning_differential(
    *,
    partition: Partition,
    rollout: Rollout,
    references: tuple[ReferenceArtifact, ...],
    evaluation: Evaluation,
    extractor: CommandProviderConfig,
    differ: CommandProviderConfig,
    diagnostician: CommandProviderConfig,
    executor: Any,
    context: Mapping[str, Any],
) -> DifferentialAnalysis:
    if partition is not Partition.TRAIN:
        raise ValueError("reasoning differential analysis is train-only")
    if not references:
        raise ValueError("reasoning differential references are required")
    if evaluation.rollout_digest != rollout.digest:
        raise ValueError("differential evaluation does not match rollout")

    extractor_provider = CommandReasoningExtractorProvider(
        extractor, executor=executor, context=context
    )
    agent_claims = extractor_provider.extract(
        source_kind="agent_rollout",
        sources=(("agent-rollout", rollout.output),),
    )
    reference_claims = extractor_provider.extract(
        source_kind="reference_artifact",
        sources=tuple(
            (reference.artifact_id, reference.content) for reference in references
        ),
    )
    differential = CommandClaimDifferProvider(
        differ, executor=executor, context=context
    ).compare(reference_claims, agent_claims)
    diagnosis = CommandDifferentialDiagnosticianProvider(
        diagnostician, executor=executor, context=context
    ).diagnose(differential, evaluation)
    return DifferentialAnalysis(
        agent_claims=agent_claims,
        reference_claims=reference_claims,
        differential=differential,
        diagnosis=diagnosis,
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)
    return payload


def persist_differential_iteration(
    data_home: Path,
    campaign_id: str,
    iteration: int,
    *,
    analysis: DifferentialAnalysis | None,
    failure_kind: str | None = None,
) -> Path:
    if (analysis is None) == (failure_kind is None):
        raise ValueError("exactly one differential result is required")
    fields: dict[str, Any] = {
        "protocol": "kpo.differential-analysis-private/v1",
        "iteration": iteration,
        "state": "available" if analysis is not None else "unavailable",
        "failure_kind": failure_kind,
    }
    if analysis is not None:
        fields["agent_claims"] = _graph_value(analysis.agent_claims)
        fields["reference_claims"] = _graph_value(analysis.reference_claims)
        fields["claim_differential"] = _differential_value(analysis.differential)
        fields["diagnosis"] = analysis.diagnosis.to_public_value()
    fields["record_digest"] = canonical_digest(fields)
    path = (
        data_home.expanduser().resolve()
        / "campaigns"
        / campaign_id
        / "differential"
        / f"iteration-{iteration:04d}.json"
    )
    _atomic_json(path, fields)
    return path


def persist_differential_summary(
    data_home: Path,
    campaign_id: str,
    *,
    profile_snapshot_digest: str,
    extractor: CommandProviderConfig,
    differ: CommandProviderConfig,
    diagnostician: CommandProviderConfig,
) -> dict[str, Any]:
    root = data_home.expanduser().resolve() / "campaigns" / campaign_id
    records = []
    for path in sorted((root / "differential").glob("iteration-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        base_fields = {
            "protocol",
            "iteration",
            "state",
            "failure_kind",
            "record_digest",
        }
        if not isinstance(record, dict) or record.get("state") not in {
            "available",
            "unavailable",
        }:
            raise ValueError("private differential record state is invalid")
        expected_fields = (
            base_fields
            | {
                "agent_claims",
                "reference_claims",
                "claim_differential",
                "diagnosis",
            }
            if record["state"] == "available"
            else base_fields
        )
        if set(record) != expected_fields:
            raise ValueError("private differential record fields are invalid")
        if (
            record["protocol"] != "kpo.differential-analysis-private/v1"
            or not isinstance(record["iteration"], int)
            or isinstance(record["iteration"], bool)
            or record["iteration"] < 0
            or (
                record["state"] == "available"
                and record["failure_kind"] is not None
            )
            or (
                record["state"] == "unavailable"
                and (
                    not isinstance(record["failure_kind"], str)
                    or not _SAFE_ID.fullmatch(record["failure_kind"])
                )
            )
        ):
            raise ValueError("private differential record contract is invalid")
        digest = record.pop("record_digest", None)
        if digest != canonical_digest(record):
            raise ValueError("private differential record digest mismatch")
        record["record_digest"] = digest
        records.append(record)

    kinds = {kind.value: 0 for kind in DifferentialKind}
    assessments = {
        axis: {assessment.value: 0 for assessment in AxisAssessment}
        for axis in (
            "conclusion_validity",
            "reasoning_fidelity",
            "reasoning_soundness",
        )
    }
    failures: dict[str, int] = {}
    for record in records:
        if record["state"] == "unavailable":
            kind = record["failure_kind"]
            failures[kind] = failures.get(kind, 0) + 1
            continue
        differential = record["claim_differential"]
        for entry in differential["entries"]:
            kinds[entry["kind"]] += 1
        diagnosis = record["diagnosis"]
        for axis in assessments:
            assessments[axis][diagnosis[axis]["assessment"]] += 1

    def provider_binding(config: CommandProviderConfig) -> dict[str, Any]:
        executable = Path(config.command[0])
        return {
            "identity": config.identity,
            "configuration_digest": canonical_digest(
                {
                    "command": ("<executable>", *config.command[1:]),
                    "identity": config.identity,
                    "timeout_seconds": config.timeout_seconds,
                    "pass_env": config.pass_env,
                }
            ),
            "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
        }

    available = sum(record["state"] == "available" for record in records)
    fields = {
        "protocol": "kpo.differential-analysis-summary/v1",
        "campaign_identity_digest": canonical_digest(campaign_id),
        "profile_snapshot_digest": profile_snapshot_digest,
        "providers": {
            "extractor": provider_binding(extractor),
            "differ": provider_binding(differ),
            "diagnostician": provider_binding(diagnostician),
        },
        "attempted_count": len(records),
        "available_count": available,
        "unavailable_count": len(records) - available,
        "kind_counts": kinds,
        "axis_assessment_counts": assessments,
        "failure_kind_counts": dict(sorted(failures.items())),
    }
    fields["record_digest"] = canonical_digest(fields)
    path = root / "differential-analysis-summary.json"
    payload = _atomic_json(path, fields)
    return {
        "differential_analysis_path": path.relative_to(data_home).as_posix(),
        "differential_analysis_digest": hashlib.sha256(payload).hexdigest(),
        "differential_analysis_summary": fields,
    }


def load_differential_summary(
    data_home: Path,
    campaign_id: str,
    *,
    profile_snapshot_digest: str,
    expected_byte_digest: str | None = None,
) -> dict[str, Any]:
    path = (
        data_home.expanduser().resolve()
        / "campaigns"
        / campaign_id
        / "differential-analysis-summary.json"
    )
    payload = path.read_bytes()
    if (
        expected_byte_digest is not None
        and hashlib.sha256(payload).hexdigest() != expected_byte_digest
    ):
        raise ValueError("differential summary byte digest mismatch")
    value = json.loads(payload.decode("utf-8"))
    fields = {
        "protocol",
        "campaign_identity_digest",
        "profile_snapshot_digest",
        "providers",
        "attempted_count",
        "available_count",
        "unavailable_count",
        "kind_counts",
        "axis_assessment_counts",
        "failure_kind_counts",
        "record_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("differential summary fields do not match the protocol")
    digest = value["record_digest"]
    content = {key: item for key, item in value.items() if key != "record_digest"}
    if digest != canonical_digest(content):
        raise ValueError("differential summary record digest mismatch")
    if (
        value["protocol"] != "kpo.differential-analysis-summary/v1"
        or value["campaign_identity_digest"] != canonical_digest(campaign_id)
        or value["profile_snapshot_digest"] != profile_snapshot_digest
    ):
        raise ValueError("differential summary identity mismatch")
    for name in ("attempted_count", "available_count", "unavailable_count"):
        count = value[name]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("differential summary counts are invalid")
    if value["available_count"] + value["unavailable_count"] != value["attempted_count"]:
        raise ValueError("differential summary counts are inconsistent")
    if set(value["kind_counts"]) != {kind.value for kind in DifferentialKind}:
        raise ValueError("differential summary kind counts are invalid")
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in value["kind_counts"].values()
    ):
        raise ValueError("differential summary kind counts are invalid")
    expected_assessments = {assessment.value for assessment in AxisAssessment}
    if set(value["axis_assessment_counts"]) != {
        "conclusion_validity",
        "reasoning_fidelity",
        "reasoning_soundness",
    } or any(
        set(counts) != expected_assessments
        for counts in value["axis_assessment_counts"].values()
    ):
        raise ValueError("differential summary axis counts are invalid")
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for counts in value["axis_assessment_counts"].values()
        for count in counts.values()
    ):
        raise ValueError("differential summary axis counts are invalid")
    if set(value["providers"]) != {"extractor", "differ", "diagnostician"} or any(
        set(provider) != {"identity", "configuration_digest", "executable_digest"}
        for provider in value["providers"].values()
    ):
        raise ValueError("differential summary provider bindings are invalid")
    if not isinstance(value["failure_kind_counts"], dict) or any(
        not isinstance(kind, str)
        or not _SAFE_ID.fullmatch(kind)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for kind, count in value["failure_kind_counts"].items()
    ):
        raise ValueError("differential summary failure counts are invalid")
    for provider in value["providers"].values():
        if (
            not isinstance(provider["identity"], str)
            or not provider["identity"].strip()
        ):
            raise ValueError("differential summary provider identity is invalid")
        _digest(provider["configuration_digest"], "provider configuration digest")
        _digest(provider["executable_digest"], "provider executable digest")
    return value
