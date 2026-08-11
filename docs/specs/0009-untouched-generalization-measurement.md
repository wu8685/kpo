# KPO v0.9 Untouched Generalization Measurement Specification

- Status: approved for TDD by delegated review
- Extends: `0003-optimization-campaign.md`,
  `0005-external-promotion-and-chaining.md`, and
  `0008-campaign-series-orchestration.md`
- Scope: optional frozen generalization cases, end-of-series matched measurement,
  one-way consumption, aggregate divergence evidence, and strict campaign isolation

## 1. Purpose

KPO v0.8 can show that an applied knowledge-policy sequence improves a frozen
campaign holdout. Reusing that holdout across an adaptive Campaign series can,
however, select policy changes that fit the holdout without transferring to
new cases. A rising holdout curve is therefore necessary evidence, but it is
not sufficient evidence of generalization.

v0.9 adds an optional untouched generalization measurement. Generalization
cases are bound when a series begins, excluded from every ordinary Campaign
path, and measured only after the owner stops the series. KPO compares the
initial and final applied policy on those cases and reports whether the
generalization gain agrees with the cumulative applied holdout gain.

This feature measures adaptive overfitting; it does not prevent it. The result
is advisory evidence for an operator and never an automatic gate, rollback,
promotion, or truth claim.

## 2. Preserved guarantees

1. A profile without `[series.generalization]` preserves the v0.8 profile
   snapshot, dataset contract, series v1 state, Provider requests, call counts,
   Campaign artifacts, series evidence, and command output bytes exactly.
2. Generalization cases never enter an ordinary Campaign Actor, primary
   Evaluator, observer Evaluator, calibration, Proposer, diagnosis, candidate,
   promotion, Campaign status, Campaign evidence, series evidence, stopping
   indicator, or dataset-growth path.
3. Generalization measurement uses the configured Actor and primary Evaluator
   only. It does not invoke observers, calibration Evaluators, or the Proposer.
4. Generalization Provider calls use a separate finite budget and cannot
   consume or extend any Campaign budget.
5. The external knowledge store remains read-only. Measurement cannot create a
   candidate or mutate, promote, or roll back policy content.
6. Reference fidelity and factual validity remain separate rubric dimensions.
   A reference is evaluation material, not an automatically accepted truth.

## 3. Optional profile contract

Generalization is opt-in under the existing series section:

```toml
[series]
max_campaigns = 10

[series.generalization]
manifest = "./generalization.jsonl"
max_provider_calls = 80
```

`manifest` is a safe path relative to the profile directory.
`max_provider_calls` is a positive integer. Unknown fields are errors. A
generalization section requires `[series]`; `[series]` remains valid without
the section.

The strict JSONL manifest contains one object per line:

```json
{"case_id":"future-001","weight":1.0,"provenance":["synthetic:v1"]}
```

Each object contains exactly `case_id`, `weight`, and `provenance`:

- `case_id` names a case in the external profile's case manifest;
- `weight` is finite and greater than zero;
- `provenance` is a non-empty array of non-empty strings;
- IDs are unique under the same normalization used by the Campaign dataset;
- the manifest is non-empty;
- no generalization ID may occur in the Campaign dataset;
- exact normalized prompt/context duplicates and cross-partition normalized
  prompt/context reuse between generalization and any Campaign partition are
  contamination errors.

The canonical `generalization_digest` binds each ordered case's prompt,
visible context, hidden-reference IDs, tags, information cutoff, weight, and
provenance. The digest does not reveal those values.

The Campaign `Partition` enum and Campaign dataset schema do not gain a new
partition. Generalization is deliberately a separate manifest and loader so a
generic "iterate every Campaign partition" path cannot include it by accident.

## 4. Series initialization and stable contract

A generalization-enabled series uses strict protocol
`kpo.campaign-series/v2`; a series without the feature continues to use v1.
The v2 state adds only:

- `generalization_digest`;
- `generalization_case_count`;
- `generalization_max_provider_calls`;
- `initial_policy_snapshot_digest`;
- `generalization_state`, initially `available`.

The stable series contract adds a `generalization` component containing the
digest, count, budget, and a digest of the normalized relative manifest
coordinate. It never contains a case ID, path, prompt, context, reference,
provenance value, or policy content. Any content or configuration change after
initialization is a typed `series_contract_mismatch` before an ordinary
Campaign Provider call or a generalization Provider call.

At initialization KPO writes an immutable initial-policy snapshot under the
series runtime directory. The snapshot contains the exact policy artifacts
already authorized for Actor use, their versions, and their canonical digest.
It is runtime evidence outside the checkout and is never included in series
state, status, evidence, or command output. Its byte digest is stored in v2
state. Creation is atomic; an incomplete snapshot prevents series creation.

Loading v2 state verifies the state digest and the snapshot byte digest, but
ordinary series reporting does not deserialize or expose snapshot content.

## 5. Eligibility and explicit command

Measurement is requested only by:

```text
kpo series generalize --profile PROFILE --series SERIES_ID
```

The command is eligible only when all of the following hold:

1. the series uses v2 and has a generalization configuration;
2. the series state is `stopped_by_owner`;
3. read-only validation proves that every bound Campaign is terminal, every
   Campaign/evidence binding is valid, and policy lineage passes the existing
   `expected_series_parent` rules;
4. no Campaign is non-terminal and no promotion is `previewed`, `applying`, or
   awaiting recovery;
5. the current loaded policy digest equals the expected final applied-policy
   digest derived from the series lineage;
6. the current generalization contract equals the initialized contract;
7. the initial-policy snapshot and current generalization source bytes pass
   integrity verification;
8. `generalization_state` is `available` and no consumption record exists;
9. `max_provider_calls` is at least `4 * generalization_case_count`.

All preflight checks occur before consumption and before any Provider call. A
preflight error leaves the measurement available.

Preflight validation is read-only. It must not invoke the mutating
`reconcile_series` operation, append an orphaned Campaign, repair evidence, or
change series state. If a write would be required, the command rejects before
consumption and tells the operator to run the explicit `series reconcile`
command first. That command remains the only path for attaching orphaned
Campaign evidence.

`series generalize` acquires the existing series mutation lock with operation
name `generalize` before preflight. After successful preflight, while holding
that lock, KPO atomically creates:

```text
data_home/series/<series_id>/generalization-consumption.json
```

The strict protocol `kpo.generalization-consumption/v1` initially contains:

- protocol, series ID, series-contract digest, and generalization digest;
- state `measuring`;
- null artifact relative path, byte digest, and artifact record digest;
- a canonical consumption-record digest.

It contains no timestamp. Its identity fields are immutable. The record's mere
existence is the one-way gate; it is never deleted or reset. After creating and
strictly reloading it, KPO changes `generalization_state` to `measuring` in the
series state and durably reloads that state before releasing the mutation lock
and making the first Provider call. A second invocation is rejected even if
the first process crashed or a Provider failed.

After measurement, KPO reacquires the same mutation lock and may make exactly
one monotonic record transition from `measuring` to the terminal measurement
state while binding the aggregate artifact's relative path, byte digest, and
record digest. Atomic replacement is allowed for that transition; identity
fields never change. Crash recovery and reconciliation may report or finish
binding an already-written terminal artifact, but must never make another
generalization Provider call.

## 6. Matched measurement

For every generalization case, in manifest order, KPO executes exactly four
calls unless the separate budget or a Provider failure ends measurement:

1. Actor with the immutable initial policy;
2. primary Evaluator for that initial rollout;
3. Actor with the verified final applied policy;
4. primary Evaluator for that final rollout.

Actor requests contain only the existing Actor-authorized case payload and one
of the two policy snapshots. Evaluator requests use the existing hidden-
reference boundary. Neither request identifies the series, reports holdout
results, or contains Campaign history. Each Actor/Evaluator request is bound to
its executable and normalized Provider configuration exactly as in the series
contract.

KPO maintains a durable call ledger under the private series runtime directory.
The ledger records only call sequence, role, request digest, response digest,
and success/failure state. It may retain private Provider responses in a
content-addressed runtime cache for audit, but none enter the aggregate result
or public repository. The configured budget is a hard upper bound including
failed calls.

For each rubric dimension, KPO computes weighted means from successful matched
case pairs:

- `initial_score`;
- `final_score`;
- `generalization_gain = final_score - initial_score`;
- `holdout_gain`, the final cumulative applied holdout gain from v0.8 series
  evidence;
- `divergence = generalization_gain - holdout_gain`.

All arithmetic uses decimal conversion from canonical score strings and is
quantized to `1e-12` before serialization. A case contributes only when both
policies have a valid primary-Evaluator result for every rubric dimension.
There is no imputation. `matched_case_count`, `unavailable_case_count`, and
`provider_call_count` make incomplete measurement explicit.

`holdout_gain` is obtained by running the existing read-only series-evidence
report pipeline, or an equivalent lineage-verifying subroutine, on the current
series state and extracting the final per-dimension
`cumulative_applied_holdout_gain` from the
`kpo.campaign-series-report/v1` result. This computation makes zero Provider
calls and does not consume the generalization budget.

## 7. Terminal artifact and state

The immutable aggregate artifact is stored at:

```text
data_home/series/<series_id>/generalization.json
```

Strict protocol `kpo.series-generalization/v1` contains only:

- protocol, series ID, profile ID, and series-contract digest;
- initial- and final-policy digests;
- generalization digest and case count;
- matched and unavailable case counts;
- Provider-call count and configured budget;
- terminal state: `measured`, `partial`, or `failed`;
- for each ordered rubric dimension, nullable aggregate `initial_score`,
  `final_score`, `generalization_gain`, `holdout_gain`, and `divergence`;
- nullable safe failure kind from a closed enum;
- canonical record digest.

It never contains case IDs, prompts, context, tags, information cutoffs,
provenance, reference IDs or content, rollout output, Evaluator explanations or
citations, per-case scores, policy content, artifact IDs, commands, paths,
timestamps, environment values, stderr, exceptions, or raw Provider payloads.

`measured` requires every case to contribute a complete matched pair. `partial`
means at least one but not every case contributed. `failed` means no case
contributed. Provider, protocol, budget, persistence, and integrity failures
are evidence states after consumption, not authorization to retry.

After atomically writing and strictly reloading the artifact, KPO reacquires
the series mutation lock, records its relative path, byte digest, and record
digest through the consumption record's sole monotonic transition, and sets v2
`generalization_state` to the same terminal state. If the process stops after
consumption but before a valid terminal artifact is bound, status reports
`measuring_interrupted`; the partition remains consumed. Stale-lock recovery
may reconcile the consumption record and series state, but never delete the
record or repeat a Provider call.

The command prints the strict aggregate artifact. Repeated invocation always
returns a typed `generalization_consumed` error; it does not replay or print the
existing result. Read-only access is through `series status` summary fields and
`series evidence` as specified below.

## 8. Read-only reporting

For a v2 series, `series status` adds:

- `generalization_state`;
- `generalization_case_count`;
- `generalization_consumed`.

It adds no score before or after measurement.

For a v2 series, `series evidence` adds a nullable
`generalization_measurement`. Before consumption it is null. After a valid
terminal artifact is bound it contains the artifact's aggregate counts,
terminal state, dimensions, and record digest. It never causes Provider calls.
An interrupted or corrupt binding is an explicit integrity error, not an
invented null result.

Generalization results do not alter v0.8 learning curves, indicators, dataset
segments, Campaign counts, promotion counts, or stopping semantics.

## 9. Isolation invariants

The implementation must make the following mechanically testable:

1. Ordinary Campaign code accepts a `CampaignDataset`, never a union with the
   generalization manifest.
2. Generalization loading is called only by profile validation, series
   initialization/contract verification, status verification, and the explicit
   generalization command.
3. Before the one-way command, no serialized runtime artifact outside the
   immutable private initial-policy snapshot and generalization contract may
   contain generalization source content or identifiers.
4. Ordinary Campaign Provider-request digests and call counts are byte-equal
   between an otherwise identical v1 profile and a v2 profile.
5. Generalization aggregates cannot be imported by diagnosis, Proposer,
   candidate selection, promotion approval, stopping indicators, or dataset
   growth.
6. Repository hygiene rejects committed runtime generalization artifacts and
   real generalization manifests under the existing repository boundary.

## 10. Error taxonomy

Operator-visible errors use safe kinds without paths or private content:

- `generalization_not_configured`;
- `series_not_stopped`;
- `series_contract_mismatch`;
- `series_lineage_mismatch`;
- `generalization_budget_insufficient`;
- `generalization_consumed`;
- `generalization_source_integrity`;
- `generalization_provider_failure`;
- `generalization_protocol_failure`;
- `generalization_persistence_failure`.

Unexpected exception text, Provider stderr, paths, IDs, or payload fragments
must not be copied into the terminal aggregate artifact.

## 11. TDD sequence

Implementation proceeds in this order, with each group failing before its
minimum implementation is written:

1. strict generalization-manifest parsing, digesting, deduplication, and
   cross-partition contamination tests;
2. optional profile parsing and byte-exact v0.8 compatibility tests;
3. v2 stable-contract, initialization, immutable initial-policy snapshot, and
   tamper-detection tests;
4. broad isolation spies proving zero generalization access and zero extra
   calls across Actor, Evaluator, observers, calibration, Proposer, Campaign,
   promotion, dataset growth, status, evidence, and reconciliation;
5. eligibility and preflight tests proving failures do not consume;
6. atomic one-way consumption and concurrent/repeated invocation tests;
7. finite-budget matched execution tests with exact request boundaries;
8. aggregate arithmetic, incomplete evidence, strict artifact, privacy, and
   deterministic-byte tests;
9. interrupted-measurement and corrupt-binding tests proving no retry;
10. CLI and read-only reporting tests;
11. full legacy suite, repository hygiene scan, private-path/domain scan, and
    external falsification experiment.

## 12. External falsification experiment

Outside the repository, create a synthetic profile with two dimensions, four
passing/applied Campaigns, and at least two untouched generalization cases. A
synthetic confounder appears only in holdout cases. The Proposer's patches and
primary Evaluator reward that confounder on holdout, while the untouched cases
do not reward it.

The experiment passes only when all conditions hold:

1. cumulative applied holdout gain rises monotonically through four Campaigns;
2. measured generalization gain is flat or negative in every dimension;
3. each reported divergence has the correct negative sign and exact value;
4. before the explicit command, no generalization case identifier, content,
   reference, score, or Provider request occurs in any Campaign artifact,
   series evidence, agreement report, calibration report, promotion bundle, or
   Provider log;
5. the command makes exactly four calls per case under its independent budget;
6. a second command invocation makes zero calls and is rejected as consumed;
7. changing any generalization byte after initialization is rejected before a
   Provider call;
8. an otherwise identical profile without generalization produces byte-equal
   v0.8 Campaign and series artifacts;
9. forced interruption after consumption cannot cause re-measurement.

Failure of any condition falsifies the v0.9 implementation; a favorable score
alone cannot pass the experiment.

## 13. Non-goals

- no automatic stop, rollback, promotion rejection, or policy mutation based
  on generalization evidence;
- no repeated, incremental, interactive, or per-Campaign generalization check;
- no post-initialization growth, resampling, rebalancing, or editing of the
  generalization manifest;
- no per-case result display and no generalization examples in Proposer input;
- no observer agreement or calibration run on generalization cases;
- no cross-series comparison or pooled benchmark;
- no claim that a finite partition represents future real-world performance;
- no reference-expert differential analyzer, curriculum selection, automatic
  data acquisition, or real-profile orchestration in v0.9;
- no real profile, corpus, rollout, score, or domain-specific identifier in the
  public repository.
