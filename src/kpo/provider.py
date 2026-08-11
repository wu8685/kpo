from __future__ import annotations

import json
import os
import re
import subprocess
from enum import Enum
from typing import Any
from collections.abc import Mapping
from typing import Protocol

from kpo.models import (
    DiagnosisKind,
    EvaluatorResult,
    FailureSignal,
    MutationKind,
    ProposalDraft,
    ScoreDimension,
)
from kpo.pipeline import ActorRequest, EvaluatorRequest, ProposerRequest
from kpo.profile import CommandProviderConfig, Rubric


class InvocationExecutor(Protocol):
    def invoke(
        self,
        config: CommandProviderConfig,
        role: str,
        request: dict[str, Any],
        *,
        context: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def _dispatch(
    config: CommandProviderConfig,
    role: str,
    request: dict[str, Any],
    executor: InvocationExecutor | None,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if executor is None:
        return _invoke(config, role, request)
    return executor.invoke(config, role, request, context=context)


class ProviderFailureKind(str, Enum):
    CONFIGURATION_ERROR = "configuration_error"
    COMMAND_NOT_FOUND = "command_not_found"
    TIMEOUT = "timeout"
    NON_ZERO_EXIT = "non_zero_exit"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_RESPONSE = "invalid_response"


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        kind: ProviderFailureKind,
        message: str,
        *,
        stderr_excerpt: str = "",
    ) -> None:
        self.kind = kind
        self.detail = message
        self.stderr_excerpt = stderr_excerpt
        suffix = f": {stderr_excerpt}" if stderr_excerpt else ""
        super().__init__(f"provider failure ({kind.value}): {message}{suffix}")


_LABELLED_SECRET = re.compile(
    r"(?i)\b(authorization|api[-_]?key|token|secret|password|credential)\s*[:=]\s*\S+"
)
_GENERIC_SECRETS = (
    re.compile(r"\bgh" + r"p_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bs" + r"k-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAK" + r"IA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE " + r"KEY-----"),
)


def _redact(stderr: str, values: tuple[str, ...]) -> str:
    result = stderr
    for value in values:
        if value:
            result = result.replace(value, "[REDACTED]")
    result = _LABELLED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
    for pattern in _GENERIC_SECRETS:
        result = pattern.sub("[REDACTED]", result)
    return result[:4096]


def _environment(config: CommandProviderConfig) -> tuple[dict[str, str], tuple[str, ...]]:
    environment: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    secret_values: list[str] = []
    for name in config.pass_env:
        if name not in os.environ:
            raise ProviderFailure(
                ProviderFailureKind.CONFIGURATION_ERROR,
                f"required environment variable is missing: {name}",
            )
        value = os.environ[name]
        environment[name] = value
        secret_values.append(value)
    return environment, tuple(secret_values)


def _invoke(config: CommandProviderConfig, role: str, request: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {"protocol": "kpo.command/v1", "role": role, "request": request},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    environment, secret_values = _environment(config)
    try:
        working_directory = config.working_directory
        if working_directory is not None:
            working_directory.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            config.command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=config.timeout_seconds,
            check=False,
            cwd=working_directory,
        )
    except FileNotFoundError as error:
        raise ProviderFailure(
            ProviderFailureKind.COMMAND_NOT_FOUND, "command was not found"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ProviderFailure(ProviderFailureKind.TIMEOUT, "command timed out") from error
    try:
        stderr = completed.stderr.decode("utf-8", errors="strict")
        stdout = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ProviderFailure(
            ProviderFailureKind.INVALID_ENCODING, "provider output is not UTF-8"
        ) from error
    if completed.returncode != 0:
        raise ProviderFailure(
            ProviderFailureKind.NON_ZERO_EXIT,
            f"command exited with code {completed.returncode}",
            stderr_excerpt=_redact(stderr, secret_values),
        )
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ProviderFailure(
            ProviderFailureKind.INVALID_RESPONSE, "provider returned invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise ProviderFailure(
            ProviderFailureKind.INVALID_RESPONSE, "provider response must be an object"
        )
    return value


def _strict_response(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise ProviderFailure(
            ProviderFailureKind.INVALID_RESPONSE,
            f"{label} response fields do not match the protocol",
        )


class CommandActorProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        *,
        executor: InvocationExecutor | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.context = context or {}

    def generate(self, request: ActorRequest) -> str:
        request_value = {
                "case": request.case_payload,
                "policy_artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "version": artifact.version,
                        "content": artifact.content,
                    }
                    for artifact in request.policy_artifacts
                ],
                "model_id": self.config.identity,
            }
        value = _dispatch(
            self.config,
            "actor",
            request_value,
            self.executor,
            self.context,
        )
        _strict_response(value, {"output"}, "actor")
        output = value["output"]
        if not isinstance(output, str) or not output.strip():
            raise ProviderFailure(
                ProviderFailureKind.INVALID_RESPONSE, "actor output is required"
            )
        return output


class CommandEvaluatorProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        rubric: Rubric,
        *,
        executor: InvocationExecutor | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.rubric = rubric
        self.executor = executor
        self.context = context or {}

    def evaluate(self, request: EvaluatorRequest) -> EvaluatorResult:
        value = _dispatch(
            self.config,
            "evaluator",
            {
                "case_id": request.case_id,
                "rollout": {
                    "case_id": request.rollout.case_id,
                    "policy_digest": request.rollout.policy_digest,
                    "model_id": request.rollout.model_id,
                    "output": request.rollout.output,
                    "retrieved_policy_ids": request.rollout.retrieved_policy_ids,
                    "actor_payload_digest": request.rollout.actor_payload_digest,
                    "digest": request.rollout.digest,
                },
                "references": [
                    {
                        "artifact_id": reference.artifact_id,
                        "kind": reference.kind,
                        "content": reference.content,
                    }
                    for reference in request.references
                ],
                "rubric": {
                    "rubric_version": self.rubric.rubric_version,
                    "dimensions": [
                        {
                            "name": dimension.name,
                            "description": dimension.description,
                            "weight": dimension.weight,
                        }
                        for dimension in self.rubric.dimensions
                    ],
                },
                "evaluator_id": self.config.identity,
            },
            self.executor,
            self.context,
        )
        _strict_response(
            value, {"dimensions", "failure_signals", "aggregate_score"}, "evaluator"
        )
        raw_dimensions = value["dimensions"]
        raw_signals = value["failure_signals"]
        if not isinstance(raw_dimensions, list) or not isinstance(raw_signals, list):
            raise ProviderFailure(
                ProviderFailureKind.INVALID_RESPONSE,
                "evaluator dimensions and failure_signals must be arrays",
            )
        dimensions: list[ScoreDimension] = []
        try:
            for item in raw_dimensions:
                _strict_response(item, {"name", "score", "explanation", "citations"}, "dimension")
                dimensions.append(
                    ScoreDimension(
                        name=item["name"],
                        score=item["score"],
                        explanation=item["explanation"],
                        citations=tuple(item["citations"]),
                    )
                )
            names = tuple(dimension.name for dimension in dimensions)
            if len(set(names)) != len(names) or set(names) != set(self.rubric.dimension_names):
                raise ValueError("evaluator dimensions do not match rubric")
            signals: list[FailureSignal] = []
            for item in raw_signals:
                _strict_response(item, {"kind", "message", "citations"}, "failure signal")
                signals.append(
                    FailureSignal(
                        kind=DiagnosisKind(item["kind"]),
                        message=item["message"],
                        citations=tuple(item["citations"]),
                    )
                )
            return EvaluatorResult(
                dimensions=tuple(dimensions),
                failure_signals=tuple(signals),
                aggregate_score=value["aggregate_score"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ProviderFailure):
                raise
            raise ProviderFailure(
                ProviderFailureKind.INVALID_RESPONSE,
                "evaluator response violates the schema",
            ) from error


class CommandProposerProvider:
    def __init__(
        self,
        config: CommandProviderConfig,
        *,
        executor: InvocationExecutor | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.proposer_id = config.identity
        self.executor = executor
        self.context = context or {}

    def propose(self, request: ProposerRequest) -> ProposalDraft:
        value = _dispatch(
            self.config,
            "proposer",
            {
                "parent_policy": {
                    "digest": request.parent_policy.digest,
                    "artifacts": [
                        {
                            "artifact_id": artifact.artifact_id,
                            "version": artifact.version,
                            "content": artifact.content,
                        }
                        for artifact in request.parent_policy.artifacts
                    ],
                },
                "rollout": {
                    "case_id": request.rollout.case_id,
                    "output": request.rollout.output,
                    "digest": request.rollout.digest,
                },
                "evaluation": {
                    "digest": request.evaluation.digest,
                    "dimensions": [
                        {
                            "name": item.name,
                            "score": item.score,
                            "explanation": item.explanation,
                            "citations": item.citations,
                        }
                        for item in request.evaluation.dimensions
                    ],
                    "failure_signals": [
                        {
                            "kind": item.kind.value,
                            "message": item.message,
                            "citations": item.citations,
                        }
                        for item in request.evaluation.failure_signals
                    ],
                },
                "diagnosis": {
                    "digest": request.diagnosis.digest,
                    "kind": request.diagnosis.kind.value,
                    "message": request.diagnosis.message,
                    "citations": request.diagnosis.citations,
                },
                "references": [
                    {
                        "artifact_id": item.artifact_id,
                        "kind": item.kind,
                        "content": item.content,
                    }
                    for item in request.references
                ],
                "allowed_mutations": request.allowed_mutations,
                "rejected_candidates": [
                    {
                        "candidate_digest": item.candidate_digest,
                        "mutation": item.mutation.value,
                        "artifact_id": item.artifact_id,
                        "validation_deltas": dict(item.validation_deltas),
                        "regression_deltas": dict(item.regression_deltas),
                        "failed_gates": item.failed_gates,
                    }
                    for item in request.rejected_candidates
                ],
                "model_id": self.config.identity,
            },
            self.executor,
            self.context,
        )
        _strict_response(
            value,
            {
                "mutation",
                "artifact_id",
                "content",
                "expected_benefit",
                "possible_regressions",
            },
            "proposer",
        )
        try:
            mutation = MutationKind(value["mutation"])
            if mutation not in {MutationKind.ADD, MutationKind.EDIT}:
                raise ValueError(f"unsupported proposer mutation: {mutation.value}")
            regressions = value["possible_regressions"]
            if not isinstance(regressions, list) or any(
                not isinstance(item, str) or not item.strip() for item in regressions
            ):
                raise ValueError("possible_regressions must be a string array")
            for field in ("artifact_id", "content", "expected_benefit"):
                if not isinstance(value[field], str) or not value[field].strip():
                    raise ValueError(f"{field} is required")
            return ProposalDraft(
                mutation,
                value["artifact_id"],
                value["content"],
                value["expected_benefit"],
                tuple(regressions),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderFailure(
                ProviderFailureKind.INVALID_RESPONSE,
                f"unsupported or invalid proposer response: {error}",
            ) from error
