# KPO v0.2 External Profile Execution Specification

- Status: **Approved by delegated independent reviewer on 2026-08-12**
- Project: Knowledge Policy Optimization
- Scope: external profiles, file-backed inputs, and command-based providers
- Depends on: `0001-kpo-v0.1.md`

## 1. Purpose

KPO v0.2 connects the verified v0.1 core to data and model processes that live
outside the public repository. It must be possible to load one external
profile, execute an Actor against an immutable policy snapshot, evaluate the
rollout with hidden references, and inspect the persisted result through the
CLI.

This is the first real-execution slice. It evaluates policy behavior but does
not yet ask a model to author a candidate patch or run an entire optimization
campaign. Those operations remain separate follow-up slices so their contracts
can be tested independently.

## 2. Goals

1. Define and validate a domain-neutral external `profile.toml`.
2. Load policy artifacts, cases, references, and rubrics from external files.
3. Invoke real Actor and Evaluator processes through a stable JSON protocol.
4. Preserve the v0.1 Actor/Evaluator information boundary.
5. Persist enough metadata and artifacts to inspect or resume a run.
6. Prove that validation, execution, and evaluation do not modify source data.
7. Keep provider credentials, real profiles, and real runtime data outside the
   Git repository and outside serialized KPO artifacts.

## 3. Non-goals

KPO v0.2 will not:

- implement a provider-specific SDK adapter;
- optimize prompts, model parameters, or provider selection;
- let the Evaluator modify a policy;
- generate a candidate patch from free-form model output;
- orchestrate regression and promotion from the new profile CLI;
- accept remote profile URLs;
- discover arbitrary files through unrestricted recursive globbing;
- copy external source files into the code checkout;
- log credentials or the values of environment variables.

The v0.1 synthetic end-to-end optimization demo remains available and
unchanged.

## 4. External profile layout

The profile is a TOML file located outside the KPO checkout. Relative paths are
resolved from the directory containing the profile, never from the current
working directory.

KPO v0.2 accepts `schema_version = 1` only. A missing version or any other
value is a validation error; version coercion and forward compatibility are not
implicit.

```toml
schema_version = 1
profile_id = "example-profile"
data_home = "./runtime"

[policy]
manifest = "./policy.jsonl"

[cases]
manifest = "./cases.jsonl"

[references]
manifest = "./references.jsonl"

[rubric]
path = "./rubric.json"

[actor]
adapter = "command"
command = ["./bin/actor-adapter"]
model_id = "configured-actor"
timeout_seconds = 120
pass_env = ["ACTOR_PROVIDER_KEY"]

[evaluator]
adapter = "command"
command = ["./bin/evaluator-adapter"]
evaluator_id = "configured-evaluator"
timeout_seconds = 120
pass_env = ["EVALUATOR_PROVIDER_KEY"]
```

### 4.1 Path rules

- `profile.toml` must be supplied explicitly; there is no implicit default.
- The profile itself and all referenced source files must be outside the code
  checkout.
- `data_home` must be outside both the code checkout and all source roots.
- Every referenced path is normalized and checked after symlink resolution.
- Path existence, symlink resolution, containment, and source/runtime overlap
  are re-checked at every command invocation, not only by `validate-profile`.
- A missing, unreadable, or duplicate source file is a validation error.
- Command paths may be absolute or profile-relative. Bare command names are
  allowed only when resolved through the process `PATH`.

### 4.2 Secret rules

- Secret values cannot appear in the profile schema.
- `pass_env` lists environment-variable names, not values.
- Only explicitly listed variables are copied to a provider subprocess, in
  addition to the minimum variables required to launch it.
- KPO reports a missing variable by name but never records its value.
- Provider request, response, stderr, exceptions, and journals must not include
  the inherited environment mapping.
- Before any stderr excerpt is retained, KPO replaces every non-empty value
  copied through `pass_env` with `[REDACTED]`. It then redacts case-insensitive
  `authorization`, `api-key`, `apikey`, `token`, `secret`, `password`, and
  `credential` assignments whose label is followed by `:` or `=`, plus the
  generic credential forms already recognized by the repository hygiene
  scanner. These rules are defense in depth; provider programs remain
  responsible for not printing secrets.

### 4.3 Command-adapter trust boundary

Command adapters are trusted plugins. KPO passes inline content rather than
source/profile paths, minimizes the inherited environment, and launches each
profile provider with a working directory under external `data_home`. Source
digests are captured before a call and rechecked immediately after exit; drift
is a typed failure. These controls reduce accidents and detect mutation, but
cannot prevent a malicious process from writing to paths allowed by the OS
user. Hard prevention requires an operator-configured container or read-only
mount.

## 5. External file schemas

All JSON and JSONL text is UTF-8. Unknown fields are rejected in v0.2 so a
misspelled contract field cannot silently change an experiment.

### 5.1 Policy manifest

One JSON object per line:

```json
{"artifact_id":"base-rule","path":"policy/base-rule.md","version":1}
```

- `artifact_id` is non-empty and unique.
- `path` is profile-relative and must remain within the profile directory.
- `version` is a positive integer.
- Policy file content must be non-empty.
- Manifest order does not affect the policy snapshot digest.

### 5.2 Case manifest

One JSON object per line:

```json
{
  "case_id": "case-001",
  "prompt": "Analyze the visible evidence and state a conclusion.",
  "visible_context": ["Visible fact A", "Visible fact B"],
  "hidden_reference_ids": ["reference-001"],
  "tags": ["example"],
  "information_cutoff": "2026-01-01T00:00:00Z"
}
```

Case IDs are unique. Hidden reference IDs are identifiers only; their content
is not loaded before the Actor request has been assembled and persisted.

### 5.3 Reference manifest

One JSON object per line:

```json
{
  "artifact_id": "reference-001",
  "kind": "expected_result",
  "path": "references/reference-001.md"
}
```

Reference IDs are unique. Every reference required by the selected case must
exist, and extra references are not sent to the Evaluator.

### 5.4 Rubric

One JSON object:

```json
{
  "rubric_version": "v1",
  "dimensions": [
    {
      "name": "conclusion_alignment",
      "description": "Compare the conclusion with the supplied references.",
      "weight": 1.0
    }
  ]
}
```

Dimension names are non-empty and unique. Weights are finite and non-negative;
at least one weight must be positive. Weights are configuration metadata and
do not force KPO to collapse dimensions into a scalar score.

## 6. Command provider protocol

KPO invokes one subprocess for one provider request. Requests are written as a
single JSON document to standard input. A successful provider writes exactly
one JSON document to standard output and exits with code zero.

The subprocess working directory is a role-specific directory below
`data_home/provider-work/`; it is created only when a provider is invoked.

Every request contains:

```json
{"protocol":"kpo.command/v1","role":"actor-or-evaluator","request":{}}
```

### 6.1 Actor request

The Actor request contains only:

- the public case payload returned by `TaskCase.actor_payload()`;
- policy artifacts with IDs, versions, and content;
- the configured model ID.

It must not contain reference IDs, reference content, rubric content, evaluator
configuration, source paths, or the external profile contents.

Successful Actor response:

```json
{"output":"The Agent result."}
```

Unknown response fields are rejected. An empty output is invalid.

### 6.2 Evaluator request

The Evaluator is invoked only after the rollout has been finalized. Its request
contains:

- selected case ID;
- immutable rollout fields and digest;
- exactly the hidden references declared by that case;
- the rubric and configured evaluator ID.

Successful Evaluator response:

```json
{
  "dimensions": [
    {
      "name": "conclusion_alignment",
      "score": 0.75,
      "explanation": "The conclusion matches but omits one boundary.",
      "citations": ["rollout-digest", "reference-001"]
    }
  ],
  "failure_signals": [
    {
      "kind": "missing_applicability_boundary",
      "message": "A limiting condition is absent.",
      "citations": ["rollout-digest", "reference-001"]
    }
  ],
  "aggregate_score": null
}
```

The response must contain every configured rubric dimension exactly once and
no unconfigured dimensions. Existing v0.1 score, citation, and diagnosis-kind
validation remains authoritative.

Reference content size is not bounded in v0.2. Extremely large references may
cause provider timeouts or memory pressure; configurable request-size limits
are deferred to a later specification.

### 6.3 Process failures

The adapter returns a typed provider failure for:

- command not found;
- timeout;
- non-zero exit code;
- invalid UTF-8;
- invalid or multiple JSON responses;
- schema-invalid response.

Failure records may contain at most the first 4096 Unicode characters of stderr
after the exact-value and generic-pattern redaction defined in section 4.2.
Successful provider stderr is discarded and is not treated as a warning in
v0.2. The run transitions to `failed`; KPO does not retry because provider calls
may have cost or side effects.

## 7. Evaluation protocol extension

The evaluator provider contract changes from returning only score dimensions
to returning an immutable `EvaluatorResult` containing:

- dimensions;
- failure signals;
- optional aggregate score.

`evaluate_rollout()` validates this result and constructs the immutable
`Evaluation`. Existing deterministic providers and tests migrate to this
contract without changing their observed behavior.

## 8. CLI behavior

### 8.1 Validate a profile

```text
kpo validate-profile --profile /external/profile.toml
```

Validation parses every manifest and source artifact, resolves paths, verifies
referential integrity, validates provider commands and environment-variable
names, and prints a JSON summary containing only IDs, counts, digests, and
normalized adapter types. It never prints source content, reference content,
commands' environment values, or secret values.

No runtime directory is created during validation.

### 8.2 Run an Actor

```text
kpo run --profile /external/profile.toml --case case-001
```

The command:

1. validates the profile;
2. loads the selected case and policy snapshot;
3. records source-tree digests for the files it will read;
4. creates `data_home` and its required parents when absent, then creates a run
   with a generated opaque run ID;
5. invokes the Actor once;
6. persists the Actor request digest and rollout;
7. prints a JSON summary with run ID, state, case ID, policy digest, rollout
   digest, and model ID.

The CLI summary does not print the model output by default. A later inspection
command may expose it explicitly from the external runtime store.

### 8.3 Evaluate a run

```text
kpo evaluate --profile /external/profile.toml --run RUN_ID
```

The command:

1. loads the completed rollout from the profile's runtime data home;
2. verifies the case ID, policy digest, and recorded source digests;
3. loads only the case's declared references and configured rubric;
4. invokes the Evaluator once;
5. persists the provider result and immutable evaluation;
6. transitions the run to `evaluated`;
7. prints evaluation digest, dimension scores, failure kinds, and aggregate
   score when supplied.

If a recorded source digest changed after `run`, evaluation fails as stale
rather than silently mixing experiment versions.

### 8.4 Inspect status

```text
kpo status --profile /external/profile.toml --run RUN_ID
```

Status is read-only and returns run metadata plus the digests of artifacts
already produced. Unknown runs return a non-zero exit code.

The required `--profile` option is a deliberate extension of the v0.1 proposed
`status --run` signature: the external profile identifies the runtime data home
in which the run is stored.

## 9. Persistence changes

The runtime store adds explicit run-artifact links instead of relying on an
artifact's presence alone. At minimum it records:

- run ID and state;
- profile ID digest, never the full profile;
- case ID and policy digest;
- source manifest and selected source-file digests;
- Actor request and rollout artifact digests;
- Evaluator response and evaluation artifact digests;
- typed failure kind when the run fails.

Runtime schema migrations are forward-only and transactional. Opening an
unsupported newer schema fails without mutation.

## 10. Read-only and privacy invariants

For `validate-profile`, `run`, `evaluate`, and `status`:

- source directories are byte-for-byte unchanged;
- only the configured external `data_home` may receive writes;
- source file permissions are not changed;
- no hidden reference content appears in Actor requests or Actor request
  artifacts;
- no environment-variable value appears in CLI output, runtime artifacts, or
  failure messages;
- no external source content is written into the public code checkout;
- no command follows a profile path outside its validated containment boundary.

Tests monitor source-tree digests before and after every command.
Run and evaluate additionally recheck source digests immediately after each
provider subprocess exits and transition to `failed` on drift.

## 11. Acceptance criteria

KPO v0.2 is complete when deterministic temporary external profiles prove:

1. Profile-relative paths resolve independently of the working directory.
2. Invalid, unknown, duplicate, escaping, symlink-escaping, and overlapping
   source/runtime paths are rejected.
3. Policy, case, reference, and rubric schemas enforce their contracts.
4. Actor subprocess input contains no hidden reference or rubric data.
5. Evaluator receives exactly the selected case's hidden references.
6. Environment variables are opt-in and their values never appear in retained
   artifacts or CLI output.
7. Timeout, exit, encoding, and JSON failures become typed failed runs without
   automatic retry.
8. A completed run can be evaluated in a separate CLI invocation.
9. Source drift between run and evaluate is detected.
10. Status reports persisted state without invoking a provider.
11. All non-promotion commands leave source files byte-for-byte unchanged.
12. Existing v0.1 tests and synthetic demo remain green.
13. The full suite requires no network and uses no real profile or domain data.

## 12. Planned TDD sequence after approval

1. Profile schema, strict parsing, and path-containment tests.
2. JSONL policy, case, reference, and rubric loader tests.
3. Command adapter request-isolation and environment tests.
4. Command adapter success, timeout, process, and malformed-response tests.
5. `EvaluatorResult` migration and rubric-conformance tests.
6. Runtime schema migration and run-artifact-link tests.
7. `validate-profile`, `run`, `evaluate`, and `status` CLI tests.
8. Source immutability and source-drift tests.
9. External synthetic profile end-to-end test.
10. Full regression, repository hygiene, and package-build verification.

## 13. Decisions requiring approval

1. Use TOML for the external profile and JSON/JSONL for manifests.
2. Use a subprocess JSON protocol as the first real-provider boundary, keeping
   vendor SDKs outside the core for now.
3. Implement execution and evaluation in v0.2; defer model-authored candidate
   generation and full campaign orchestration to v0.3.
4. Treat source drift as a hard error instead of automatically evaluating with
   changed references or policy files.
5. Do not retry failed provider calls automatically in v0.2.
