from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping
from collections.abc import Callable

from kpo.digest import canonical_digest
from kpo.profile import CommandProviderConfig
from kpo.provider import ProviderFailure, ProviderFailureKind, _invoke


class BudgetExhausted(RuntimeError):
    """Raised before a provider call would exceed the configured budget."""


_COMMON_CONTEXT = frozenset(
    {"case_digest", "policy_digest", "decoding_configuration_digest", "random_seed"}
)
_ROLE_CONTEXT = {
    "actor": _COMMON_CONTEXT,
    "evaluator": _COMMON_CONTEXT | {"reference_set_digest", "rubric_digest"},
    "proposer": _COMMON_CONTEXT
    | {"reference_set_digest", "rubric_digest", "rejected_candidates_digest"},
    "reasoning_extractor": _COMMON_CONTEXT
    | {"reference_set_digest", "rubric_digest"},
    "claim_differ": _COMMON_CONTEXT | {"reference_set_digest", "rubric_digest"},
    "differential_diagnostician": _COMMON_CONTEXT
    | {"reference_set_digest", "rubric_digest"},
}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CampaignCallExecutor:
    def __init__(
        self,
        data_home: Path,
        campaign_id: str,
        *,
        max_calls: int,
        integrity_check: Callable[[], None] | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.root = data_home.expanduser().resolve() / "campaigns" / campaign_id
        self.cache_dir = self.root / "provider-cache"
        self.state_path = self.root / "call-budget.json"
        self.max_calls = max_calls
        self.integrity_check = integrity_check
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state["max_calls"] != max_calls:
                raise ValueError("campaign call budget changed during resume")
            self.call_count = int(state["call_count"])
        else:
            self.call_count = 0
            _atomic_json(
                self.state_path,
                {"max_calls": self.max_calls, "call_count": self.call_count},
            )

    def _key(
        self,
        config: CommandProviderConfig,
        role: str,
        request: dict[str, Any],
        context: Mapping[str, Any],
    ) -> str:
        executable = Path(config.command[0]).resolve(strict=True)
        environment_digests = {
            name: canonical_digest(os.environ[name]) if name in os.environ else None
            for name in config.pass_env
        }
        return canonical_digest(
            {
                "protocol": "kpo.command/v1",
                "role": role,
                "request": request,
                "context": dict(context),
                "adapter": {
                    "command": config.command,
                    "identity": config.identity,
                    "timeout_seconds": config.timeout_seconds,
                    "pass_env_names": config.pass_env,
                    "pass_env_value_digests": environment_digests,
                    "executable_digest": _file_digest(executable),
                    "working_directory": config.working_directory,
                },
            }
        )

    def invoke(
        self,
        config: CommandProviderConfig,
        role: str,
        request: dict[str, Any],
        *,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = _ROLE_CONTEXT.get(role)
        if required is None:
            raise ValueError(f"unknown provider role for campaign cache: {role}")
        missing_context = required - set(context)
        if missing_context:
            raise ValueError(
                f"provider cache context is incomplete: {sorted(missing_context)}"
            )
        key = self._key(config, role, request, context)
        cache_path = self.cache_dir / key[:2] / f"{key}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached["protocol"] != "kpo.command/v1" or cached["role"] != role:
                raise ValueError("cached provider response contract mismatch")
            if cached["state"] == "failed":
                raise ProviderFailure(
                    ProviderFailureKind(cached["failure_kind"]),
                    cached["detail"],
                    stderr_excerpt=cached.get("stderr_excerpt", ""),
                )
            response = cached["response"]
            if not isinstance(response, dict):
                raise ValueError("cached provider response schema is invalid")
            return response
        if self.call_count >= self.max_calls:
            raise BudgetExhausted("provider call budget exhausted")
        self.call_count += 1
        _atomic_json(
            self.state_path,
            {"max_calls": self.max_calls, "call_count": self.call_count},
        )
        try:
            if self.integrity_check is not None:
                self.integrity_check()
            try:
                response = _invoke(config, role, request)
            finally:
                if self.integrity_check is not None:
                    self.integrity_check()
        except ProviderFailure as error:
            _atomic_json(
                cache_path,
                {
                    "protocol": "kpo.command/v1",
                    "role": role,
                    "state": "failed",
                    "failure_kind": error.kind.value,
                    "detail": error.detail,
                    "stderr_excerpt": error.stderr_excerpt,
                },
            )
            raise
        _atomic_json(
            cache_path,
            {
                "protocol": "kpo.command/v1",
                "role": role,
                "state": "completed",
                "response": response,
            },
        )
        return response
