# KPO v0.3 Optimization Campaign Specification

- Status: **Approved by delegated independent reviewer on 2026-08-12**
- Depends on: `0001-kpo-v0.1.md`, `0002-external-profile-execution.md`
- Scope: proposer execution, partitioned datasets, matched replay, and preview-only campaigns

## 1. Purpose

v0.3 turns external execution into a repeatable knowledge-policy optimization
campaign. Given an external profile and partitioned cases, KPO evaluates a
baseline, diagnoses policy-attributable failures, asks an isolated Proposer for
one minimal candidate, replays parent/candidate/ablation policies on matched
cases, and produces a promotion preview only when configured gates pass.

The model weights remain fixed. KPO does not claim that reference fidelity is
factual truth, and no model-generated patch can approve or apply itself.

## 2. Profile extensions

```toml
[dataset]
manifest = "./dataset.jsonl"
frozen_holdout_digest = "sha256:..." # optional before initialization

[proposer]
adapter = "command"
command = ["./bin/proposer-adapter"]
model_id = "configured-proposer"
timeout_seconds = 120
pass_env = ["PROPOSER_PROVIDER_KEY"]

[campaign]
max_iterations = 5
max_provider_calls = 200

[campaign.gates.validation]
conclusion_alignment = 0.05
[campaign.gates.holdout]
conclusion_alignment = 0.0
[campaign.gates.regression]
conclusion_alignment = 0.0
[campaign.gates.ablation]
conclusion_alignment = 0.01

[promotion]
target_root = "./knowledge"
allowlist = ["policies"]
add_directory = "policies"

[sandbox]
type = "none"
```

- All paths and secrets follow v0.2 rules.
- Budgets are mandatory positive integers. One CLI invocation cannot exceed
  either budget and never silently continues in another process.
- A provider call is any Actor, Evaluator, or Proposer subprocess invocation.
  Successful and typed-failure calls consume one call; cache hits do not. v0.3
  never retries automatically.
- `target_root` is read-only during a campaign. Only a later digest-approved
  `promote` invocation may write it.
- An `edit` candidate keeps the existing path recorded by the policy manifest.
  An `add` candidate maps to `<add_directory>/<artifact_id>.md`.
  `add_directory` must be inside the promotion allowlist, and artifact IDs used
  as filenames are restricted to ASCII letters, digits, `.`, `_`, and `-` with
  no separators or `..` segment. The resolved destination is rechecked by the
  v0.1 promotion allowlist logic before preview.
- Target-quality thresholds are profile inputs set by the human owner. The
  reviewer may reject weak or inconsistent thresholds but cannot invent the
  final real-world target.
- Command adapters are trusted plugins. KPO uses a `data_home` working
  directory, minimal environment, path-free request schemas, and post-call
  source/target digest checks. It detects but cannot prevent writes permitted
  by the OS user.
- Sandbox types are `none`, `container`, and `readonly_mount`. v0.3 executes
  only `none`; selecting either hard-sandbox type fails validation as
  unavailable until its launch contract is specified. Campaign metadata always
  reports the effective type.

## 3. Dataset contract

`dataset.jsonl` contains one strict object per case:

```json
{"case_id":"case-001","partition":"train","weight":1.0,"provenance":["source-id"]}
```

Partitions are `train`, `validation`, `holdout`, and `regression`.

- Every case ID must exist in the case manifest and occur exactly once.
- Weight must be finite and positive; provenance must be non-empty.
- Validation and holdout must both be non-empty.
- Exact duplicate content and duplicate normalized case IDs are rejected.
- Cross-partition duplicate prompts or visible contexts are contamination
  errors unless an explicit, reviewed exception records why they are distinct.
- The holdout case-ID/content digest is frozen when first initialized. Dataset
  growth cannot add, remove, edit, reweight, or repartition holdout cases.
- New data enters an external inbox and is validated before manifest mutation.
  KPO never crawls, downloads, or publishes domain data by itself.
- Growth reports counts, provenance coverage, duplicates, contamination, and
  partition balance. More rows alone never count as improvement.

Before the first campaign, `dataset init` computes a holdout digest and returns
a preview. `dataset init --approve DIGEST` records it as an immutable lock in
external `data_home`; it does not edit the profile or source manifests. If the
profile supplies `frozen_holdout_digest`, it must match the lock. Campaign and
growth commands reject an absent or mismatched lock.

## 4. Proposer isolation and protocol

The command envelope remains `kpo.command/v1` with role `proposer`. It receives:

- parent policy artifacts and digest;
- immutable rollout, evaluation, and diagnosis;
- cited reference excerpts used by that evaluation;
- allowed mutation kinds (`add`, `edit` in v0.3);
- prior rejected-candidate summaries from the same campaign, containing only
  candidate digest, mutation, artifact ID, validation/regression per-dimension
  deltas, and failed non-holdout gate names;
- destination artifact IDs, but no promotion approval or credentials.

It does not receive holdout cases, holdout scores, holdout pass/fail, promotion
diff digests, or evaluation results from candidates that have not already been
rejected. Rejection summaries are ordered by iteration and become part of the
complete Proposer request and provider-cache key.

Only train reference excerpts are supplied. Semantic similarity between train
and holdout references remains an overfitting risk; validation reports
source-domain overlap and profiles should keep those sources disjoint when
practical.

Response:

```json
{
  "mutation":"add",
  "artifact_id":"boundary-rule",
  "content":"A minimal policy rule.",
  "expected_benefit":"Correct the diagnosed omission.",
  "possible_regressions":["May abstain too often."]
}
```

KPO rejects unknown fields, unsupported mutations, stale parent digests,
existing IDs for `add`, missing IDs for `edit`, empty content, and patches that
do not cite the diagnosis/evaluation provenance. Command adapters are trusted
under the explicit detection and sandbox boundary in section 2.

The other v0.1 mutation kinds are deferred until their apply, regression, and
rollback semantics are specified. Emitting one is a typed
unsupported-mutation failure, never a silent rejection.

## 5. Campaign algorithm

`kpo campaign --profile PROFILE` performs finite iterations:

1. Snapshot profile, dataset, policy, rubric, and target digests. Before and
   immediately after every non-cached provider subprocess invocation, recheck
   captured source files and the target tree. Drift records typed
   `source_drift` or `target_drift` and transitions to `failed`.
2. Run and evaluate the parent policy on all train cases.
3. Diagnose failures. Non-policy diagnoses are retained but never proposed.
4. Select one policy-attributable diagnosis by ascending case ID, then the
   existing deterministic diagnosis priority from v0.1.
5. Invoke Proposer once and validate one minimal candidate.
6. For every validation, holdout, and regression case, run matched parent and
   candidate trials with identical visible inputs and provider configuration.
   Train cases are used only for diagnosis and never for gates.
7. Compute ablation by replacing only the candidate policy with the parent
   snapshot while preserving the same case, references, rubric, provider
   identities, decoding configuration, information cutoff, and random seed.
8. Apply configured gates independently by partition and dimension.
9. If gates fail, retain evidence and continue within budgets using the
   unchanged parent. A rejected candidate never becomes the next parent. The
   next Proposer request includes the permitted rejection summary; generating
   the same candidate digest twice terminates as `no_patchable_diagnosis`
   instead of consuming another iteration.
10. If gates pass, create a stable promotion preview and stop the campaign in
    `promotion_previewed` state even when budget remains. Never apply it
    automatically or search for a better unapproved candidate.

Provider/runtime failures consume budget, create typed evidence, and stop the
current iteration without retry. A campaign resumes only by explicit command
and must not repeat completed digest-keyed calls.

## 6. Measurement rules

- Parent, candidate, and ablation use the same case snapshot, rubric, Actor,
  Evaluator, decoding configuration, and information cutoff.
- Scores remain vectors. Every rubric dimension must have explicit validation,
  holdout, regression, and ablation delta thresholds. Unknown or missing
  dimensions are errors, and every dimension must pass. Optional aggregate
  scores are reported but never used for gates.
- Reports include per-case/per-dimension scores, partition aggregates,
  candidate-parent deltas, ablation deltas, uncertainty, call counts, and cost
  metadata when providers expose it.
- A passing validation gain cannot compensate for a failing holdout or
  regression gate.
- Holdout details are withheld from Actor and Proposer and exposed only as
  aggregate pass/fail until campaign termination.

## 7. CLI

```text
kpo validate-profile --profile PROFILE
kpo dataset validate --profile PROFILE
kpo dataset init --profile PROFILE
kpo dataset init --profile PROFILE --approve DIGEST
kpo dataset grow --profile PROFILE --inbox EXTERNAL_JSONL --approve DIGEST
kpo campaign --profile PROFILE
kpo campaign --profile PROFILE --resume CAMPAIGN_ID
kpo campaign-status --profile PROFILE --campaign CAMPAIGN_ID
kpo promote --profile PROFILE --candidate CANDIDATE_ID
kpo promote --profile PROFILE --candidate CANDIDATE_ID --approve DIFF_DIGEST
```

`dataset grow` first emits a preview digest and makes no changes. Supplying that
exact digest applies only the reviewed non-holdout manifest additions, with an
external snapshot and journal. `promote` retains the v0.1 preview/apply split.

## 8. Persistence and reproducibility

Campaign, iteration, provider-call, measurement, candidate, dataset-growth,
and preview records are linked by immutable digests. SQLite migrations remain
transactional and forward-only. Runtime output stays in external `data_home`.

The campaign records random seeds and selection order. Default selection is
deterministic. Concurrent mutation of profile, dataset, policy, rubric, or
promotion target causes a stale-input failure.

Every provider cache key digests the complete serialized command request plus
case, policy, reference-set, rubric, model/provider identity, decoding
configuration, random seed, adapter configuration, adapter executable content,
and protocol-version digests. Cached response schemas are revalidated; partial
keys are forbidden.

## 9. Safety and stopping

A campaign stops when any condition holds:

- a candidate passes all gates and a promotion preview exists;
- iteration or provider-call budget is exhausted;
- no patchable diagnosis remains;
- source drift, provider failure, or invariant violation occurs;
- the owner requests stop.

The system may automatically start another finite campaign only when an
external orchestrator has reviewed the prior evidence and budgets. Credentials,
meaningful external cost, private-data egress, promotion apply, publication,
destructive action, core-objective changes, and final real-world target quality
remain human decisions.

Terminal states are exactly `promotion_previewed`, `budget_exhausted`,
`no_patchable_diagnosis`, `failed`, and `stopped`. Only `failed` campaigns may
resume completed digest-keyed work; all other terminal states start a new
campaign.

## 10. Acceptance criteria

1. Strict dataset validation catches duplicate IDs/content, contamination,
   missing partitions, bad provenance/weights, and holdout mutation.
2. Dataset growth is preview-first, digest-approved, journaled, recoverable,
   and cannot modify frozen holdout.
3. Proposer receives no holdout data or approval material and emits one valid,
   provenance-complete `add` or `edit` candidate.
4. Non-policy diagnoses never invoke Proposer.
5. Matched vector scoring runs parent/candidate/ablation across all required
   partitions and enforces gates independently.
6. Rejected candidates do not alter the active parent.
7. Passing candidates create a stable preview without writing target content.
8. Budgets and stop conditions are deterministic and persisted.
9. Resume does not repeat completed digest-keyed provider calls.
10. KPO detects source or target drift immediately after every provider call,
    records a typed violation, and aborts. Hard prevention is available only
    through an operator-configured sandbox whose effective type is reported.
11. Repository tests are network-free and use progressively larger synthetic
    datasets: 4, 16, 64, then 256 cases.
12. Existing v0.1 and v0.2 tests remain green and repository hygiene passes.

## 11. Planned TDD sequence after approval

1. Dataset schema, contamination, frozen-holdout, and growth-preview tests.
2. Proposer isolation, response, provenance, and failure tests.
3. Vector measurement and independent gate tests.
4. Campaign budget, state, cache, rejection, and resume tests.
5. Preview-only promotion integration tests.
6. Synthetic datasets at 4/16/64/256 cases with deterministic expected gains.
7. Full regression, hygiene, build, and delegated reviewer acceptance.

## 12. Decisions delegated for review

1. Keep each campaign finite and budgeted even when an external orchestrator
   continuously starts reviewed campaigns.
2. Freeze holdout permanently after initialization; grow only train,
   validation, and regression partitions.
3. Allow only `add` and `edit` proposer mutations in v0.3.
4. Stop on the first passing promotion preview rather than chaining unapproved
   candidates into a new parent.
5. Require human decisions for real target thresholds and consequential
   external effects while delegating intermediate engineering gates.
