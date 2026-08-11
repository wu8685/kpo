# KPO v0.6 Evaluator Agreement Audit Specification

- Status: **Approved for TDD implementation**
- Depends on: `0002-external-profile-execution.md`,
  `0003-optimization-campaign.md`,
  `0005-external-promotion-and-chaining.md`
- Scope: independent observer Evaluators, deterministic agreement metrics,
  runtime audit artifacts, and promotion-review binding

## 1. Purpose

KPO can now produce, evaluate, review, apply, recover, and chain knowledge-policy
changes. Its next limiting factor is the reliability of the measurement signal.
A campaign currently trusts one Evaluator. More data or faster orchestration
would amplify that Evaluator's systematic bias without revealing it.

v0.6 characterizes this measurement risk before adding more automation. A
profile may configure independent observer Evaluators. They score the same
rollout against the same hidden references and rubric without seeing one
another's outputs. KPO computes deterministic per-dimension agreement metrics,
persists a strict audit report outside the repository, and binds the report to
any promotion bundle created by that campaign.

This is an audit milestone, not automatic calibration. Without trusted gold
scores, score means and offsets are tendencies, not proof that an Evaluator is
correct. The primary Evaluator remains the only input to diagnosis and gates in
v0.6. Observable disagreement informs the operator's exact promotion decision;
it does not silently change scores or appoint a majority as truth.

## 2. Configuration contract

The existing singular `[evaluator]` section remains required and is the
primary Evaluator. Existing profiles with no observers remain valid and retain
v0.5 behavior.

Observer Evaluators are optional repeated tables:

```toml
[[observer_evaluators]]
name = "observer-a"
adapter = "command"
command = ["./bin/observer-a"]
evaluator_id = "observer-model-a"
timeout_seconds = 30
pass_env = []
```

Each observer uses the existing command protocol and configuration rules.

- `name` is a non-empty, path-safe identifier matching
  `[A-Za-z0-9_-][A-Za-z0-9._-]*` and is unique within the profile.
- `evaluator_id` is non-empty and unique across the primary and all observers.
- An observer's command, timeout, environment allowlist, executable checks,
  working-directory isolation, and stderr redaction match the primary
  Evaluator contract.
- Observer working directories are
  `data_home/provider-work/evaluators/<name>`.
- Unknown fields, duplicate names/identities, unsafe names, and a malformed
  observer table are validation errors.
- `validate-profile` reports observer count and identities but never environment
  values.

The complete ordered observer configuration participates in the campaign
profile snapshot. Execution, normalized observer serialization, and report
output sort observers by `name`; TOML declaration order never changes call or
metric order. The existing byte-level profile source digest remains stricter:
rewriting or reordering profile TOML is still source drift during a campaign.

Observer configurations are serialized in the snapshot under a sorted
`observers` key. Each entry contains `name`, identity, command,
`timeout_seconds`, `pass_env` names (never values), and executable digest. This
binds the report to the exact observer set and executable bytes that produced
it. Observer executable files also enter the campaign integrity monitor.

## 3. Information isolation and authority

For every Evaluation event, each observer receives the same
`EvaluatorRequest` fields as the primary Evaluator:

- the selected case;
- the same rollout;
- the selected case's hidden references;
- the same rubric.

Observers do not receive:

- primary or peer scores, explanations, failure signals, or identities;
- rejected-candidate summaries;
- partition-level gates or pass/fail results;
- promotion paths, bundles, diffs, approval digests, apply state, or recovery
  evidence;
- cases or hidden references not selected for that Evaluation event.

The primary Evaluator alone drives train failure diagnosis, Proposer input,
vector scores, pass/fail gates, and rejection summaries. Observer scores never
replace, average, veto, or adjust primary scores in v0.6. The Proposer request
remains byte-for-byte unchanged and never receives the agreement report or its
digest.

An observer is independent in protocol and invocation, not guaranteed to be
independent in model training or data. Agreement therefore measures observed
behavioral consistency, not truth and not statistical independence.
The identity-uniqueness constraint ensures that metrics compare distinct
Evaluator configurations. If two entries ultimately use the same underlying
model through different commands or configurations, KPO is comparing distinct
invocation paths; it does not claim model-level independence.

## 4. Evaluation events and execution

An Evaluation event is uniquely identified by:

```text
canonical_digest({
  case_digest,
  rollout_digest,
  policy_digest,
  partition,
  rubric_digest
})
```

The partition comes from the campaign dataset entry. Parent-policy evaluation
reused as the ablated baseline has the same event ID and is counted once.
Repeated cache hits for the same event and Evaluator are also counted once.
The event ID identifies the evaluation target independently of which Evaluator
scores it. Deduplication is by the pair of event ID and Evaluator identity; the
underlying cache key additionally binds Evaluator configuration, executable
digest, request, and context. Distinct Evaluators scoring one event therefore
produce distinct cache entries and comparable observations.

Execution order is deterministic:

1. Actor produces a rollout.
2. Primary Evaluator scores it.
3. Observers run in sorted `name` order.
4. Successful scores and sanitized failure kinds enter the agreement
   collector.
5. The campaign continues using only the primary result.

Every attempted observer invocation consumes the existing campaign-wide
provider-call budget unless it is a valid cache hit. The cache key already
binds command configuration, evaluator identity, executable digest, request,
and context; observer identity cannot alias the primary or another observer.

`BudgetExhausted` is a global campaign terminal condition, not a non-fatal
observer provider failure. If it occurs before an observer invocation, the
remaining observer slots for the already-primary-scored event are recorded as
`not_attempted_budget_exhausted`; they do not consume calls and are not
misreported as timeouts or malformed responses. No later event is invented or
counted. A `budget_exhausted` report is explicitly partial and summarizes only
events that reached a successful primary score before exhaustion. If budget is
exhausted before the primary score, that target is not an Evaluation event and
is not counted.

A primary Evaluator failure retains existing campaign failure behavior. An
observer timeout, malformed response, or provider failure is recorded as
unavailable for that event and does not fail the campaign. Integrity violations
remain fatal regardless of which provider call detected them. KPO never drops
an observer failure without reporting its sanitized failure kind.

## 5. Agreement metrics

Agreement is computed only from comparable successful primary/observer score
pairs for the same event and rubric dimension. Failure signals and free-text
explanations are not merged or compared in v0.6.

For each partition, rubric dimension, and observer, KPO reports:

- `comparable_count`;
- `primary_mean`;
- `observer_mean`;
- `signed_offset_mean = mean(observer_score - primary_score)`;
- `mean_absolute_delta = mean(abs(observer_score - primary_score))`;
- `max_absolute_delta`;
- `unavailable_count`, grouped by sanitized failure kind.

For each partition and dimension, KPO also reports panel dispersion over events
with at least two successful Evaluators:

- `panel_event_count`;
- mean of each event's `max(score) - min(score)`;
- maximum event range.

All arithmetic uses deterministic binary floating-point operations over event
IDs sorted lexicographically and observer names sorted lexicographically.
Values must be finite and remain within their mathematical ranges. Empty
comparisons use count `0` and JSON `null` for means/maxima; KPO never emits NaN
or substitutes zero for missing evidence.

These values are called agreement and score-tendency metrics. They are not
called accuracy, calibration error, confidence, or ground truth. A positive
signed offset shows that an observer scores higher than the primary; it does
not establish which one is correct.

## 6. Strict agreement report

Every clean campaign terminal state persists one agreement report when at least
one observer is configured:

```text
data_home/campaigns/<campaign_id>/evaluator-agreement.json
```

The report protocol is `kpo.evaluator-agreement/v1`. It is strict and contains:

- campaign ID, profile ID, and profile snapshot digest;
- primary Evaluator identity;
- sorted observer names and identities;
- rubric version and sorted dimension names;
- unique event count and event counts by partition;
- `collection_state`, exactly `complete` or `budget_exhausted`;
- observer availability totals by sanitized failure kind;
- the metrics in section 5, partitioned by dataset partition;
- `report_digest`, a canonical digest computed from all other report fields.

The file contains no prompts, rollout output, reference content, explanations,
failure messages, stderr, provider environment values, absolute paths, raw
per-event scores, or holdout case IDs. It is written atomically under external
`data_home`.

Clean terminal states are `promotion_previewed`, `budget_exhausted`,
`no_patchable_diagnosis`, and `iteration_exhausted` if that state is introduced
later. Existing v0.6 campaign states remain unchanged; v0.6 does not introduce
`iteration_exhausted`. A failed campaign may retain partial cached Evaluations
but does not claim a complete agreement report. Resume reconstructs the same
collector deterministically from a strict runtime checkpoint, valid cached
calls, and new calls. Provider cache responses alone do not carry sufficient
event and partition coordinates to recover prior rejected-candidate evidence.

When observers are configured, campaign creation atomically initializes:

```text
data_home/campaigns/<campaign_id>/evaluator-agreement.checkpoint.json
```

The strict protocol is `kpo.evaluator-agreement-checkpoint/v1`. It binds the
campaign/profile snapshot, primary and observer identities, rubric, dimensions,
and a sorted map of event digests to partition, primary scores, observer scores,
or sanitized unavailability kinds. It contains no case IDs, prompts, rollout or
reference content, explanations, stderr, paths, or environment values. KPO
atomically rewrites it only after an event has a successful primary score and
all observer slots are either successful or explicitly unavailable.

A resumed observer campaign without its checkpoint, or with a schema, digest,
identity, or observation conflict, transitions to `failed` with failure kind
`evaluator_agreement_checkpoint`; it never silently starts an empty collector.
Checkpoint initialization or update failure uses the same typed failure. After
a clean aggregate report is durably persisted, KPO removes the checkpoint. A
cleanup failure is logged but does not reverse the clean campaign result; the
checkpoint remains external private runtime data and can be removed later.

For `promotion_previewed` and `no_patchable_diagnosis`, `collection_state` is
`complete`. For `budget_exhausted`, it is `budget_exhausted`; the report is a
valid, explicitly partial audit of observations completed before the hard
limit. `not_attempted_budget_exhausted` is an availability kind, not a Provider
failure kind.

The agreement report is written and strictly reloaded before any clean terminal
state is committed. If its atomic write or validation fails, the campaign
transitions to `failed` with typed failure kind
`evaluator_agreement_persistence` and must not reach `promotion_previewed` or
another clean state without a valid report.

Campaign state stores only:

- `evaluator_agreement_path`, relative to `data_home`;
- `evaluator_agreement_digest`, the lowercase hexadecimal SHA-256 digest of
  the exact report file bytes;
- a summary containing observer count, event count, maximum observed panel
  range by dimension aggregated across all partitions, and unavailable totals.

`campaign-status` returns this summary and digest. It does not return raw
holdout case IDs or the complete report.

## 7. CLI audit command

```text
kpo evaluator-agreement \
  --profile PROFILE \
  --campaign CAMPAIGN_ID
```

The command loads the campaign profile and state, validates the report's strict
schema, recomputes the internal canonical `report_digest`, separately computes
the report file's SHA-256 for the state `evaluator_agreement_digest` binding,
checks campaign/profile/snapshot identity, and prints the complete aggregate
report as stable JSON. It never invokes a provider and never writes profile,
policy, dataset, target, bundle, or report files.

The command fails for an unknown campaign, a campaign without configured
observers, a missing, truncated, or schema-invalid report, an unknown field, a
digest mismatch, a path escape, an identity mismatch, or state/report binding
drift. An explicitly `budget_exhausted` report is valid, not schema-partial.

## 8. Promotion-review binding

A promotion candidate generated by a campaign with observers must bind the
completed agreement report before the campaign becomes
`promotion_previewed`. The report is not merely advisory state that can be
changed after the operator reads it.

KPO introduces promotion bundle protocol `kpo.external-promotion.v2`:

- all v1 fields and byte files remain unchanged;
- `bundle.json` additionally contains `evaluator_agreement_digest`, defined as
  the lowercase hexadecimal SHA-256 digest of the exact
  `evaluator-agreement.json` bytes;
- `files` additionally contains key `evaluator_agreement` with the same byte
  digest;
- the exact report bytes are copied into the immutable bundle directory as
  `evaluator-agreement.json` before final bundle installation;
- the canonical promotion approval digest therefore binds the agreement report
  byte digest alongside the candidate, regression report, content, manifest,
  paths, and diffs.

The report's internal `report_digest` is a separate canonical digest over all
other structured report fields. It is validated whenever the report is loaded
but does not appear as another bundle field; the bundle binds it transitively
by binding the complete report bytes.

Bundle v1 remains loadable for v0.5 campaigns without an agreement report.
Bundle parsing selects one exact field schema by protocol version; v1 never
silently accepts v2 fields and v2 never accepts a missing agreement file or
field. New campaigns with observers must use v2. New campaigns without
observers may continue to use v1.

`kpo promote` preview for v2 validates the agreement file and displays its
digest plus the campaign-state summary next to both diffs and the approval
digest. Apply revalidates the exact agreement bytes but does not rerun
Evaluators. Recovery uses the immutable v2 bundle exactly as v1 and never
modifies the agreement report. If a crash occurs after report or bundle
installation but before campaign-state update, resume strictly reloads the
installed report and bundle, restores the report path/digest/summary binding in
campaign state, and performs zero Provider calls.

## 9. Reproducibility and drift boundary

v0.6 measures disagreement within one reproducible campaign snapshot. It does
not claim to detect temporal Evaluator drift by bypassing the provider cache or
replaying old private rollouts. Command executable digests and identities
already participate in profile snapshots and cache keys; changing them creates
a different campaign snapshot rather than silently mixing observations.

Cross-campaign drift detection requires a separately specified anchor set,
fresh-replay contract, and privacy/retention policy. It is a v0.6 non-goal.

## 10. Synthetic falsification experiment

Use only deterministic synthetic providers and synthetic cases. Configure one
primary plus two observers:

- the primary returns existing deterministic rubric scores;
- `observer-offset` adds a deterministic `+0.20` to one dimension, capped at
  `1.0`, and matches the primary elsewhere; synthetic primary scores for the
  offset fixture stay at or below `0.80`, so the expected offset is exactly
  `+0.20` rather than cap-dependent;
- `observer-failure` returns the primary score on selected case IDs and a
  deterministic typed failure on the rest.

Run a campaign that reaches a promotion preview, then inspect campaign status,
the audit CLI, bundle v2, and promote preview.

The experiment passes only if:

1. the offset dimension has the exact analytically expected signed offset,
   mean absolute delta, and maximum absolute delta;
2. untouched dimensions report zero delta;
3. observer failures are counted by sanitized kind without failing the
   campaign;
4. duplicate parent/ablation events are counted once;
5. primary vector gates and candidate outcome are identical to a single-
   Evaluator control run;
6. report output is byte-deterministic across a clean rerun with the same
   inputs and campaign ID normalization;
7. bundle v2 approval changes if one agreement-report byte changes and the
   corrupted bundle is rejected;
8. Proposer request captures contain no observer output, report digest,
   partition agreement, holdout detail, or promotion data;
9. holdout lock, dataset manifest, policy, and target remain unchanged before
   explicit promotion apply.

The hypothesis is falsified if known offsets are not recovered exactly,
observer failure changes the primary campaign decision, duplicate events bias
metrics, report corruption does not invalidate promotion review, or observer
information enters Proposer input.

## 11. TDD sequence

1. Backward-compatible profile tests: zero observers, multiple sorted
   observers, duplicate/unsafe names, duplicate identities, and unknown fields.
2. Pure agreement-math tests for exact offsets, missing pairs, null empty
   aggregates, multiple dimensions/partitions, panel range, ordering
   invariance, and duplicate-event rejection.
3. Provider isolation tests proving identical Evaluator payload shape and no
   peer outputs.
4. Observer failure tests proving sanitized availability reporting and primary
   campaign continuity; primary failure behavior remains unchanged. Separate
   budget tests prove global exhaustion stops execution, records only remaining
   slots on an already-primary-scored event as
   `not_attempted_budget_exhausted`, and emits an explicitly partial report.
5. Campaign integration test proving all observers run for unique Evaluation
   events, primary-only diagnosis/gates, call-budget accounting, and strict
   report persistence before a promotion preview.
6. Report loader and `evaluator-agreement` CLI tests for stable JSON,
   non-mutation, corruption, unknown fields, path escape, identity drift, and
   state/report digest mismatch.
7. Promotion bundle v2 tests for strict v1/v2 parsing, agreement byte-digest
   binding, preview display, wrong/corrupt report rejection, apply, recovery,
   and v1 backward compatibility.
8. Proposer boundary and frozen-holdout regression tests.
9. Full legacy suite, build, hygiene, privacy scan, and the external synthetic
   experiment in section 10.

## 12. Acceptance criteria

1. Existing singular-Evaluator profiles and v1 promotion bundles retain v0.5
   behavior.
2. Except for slots explicitly not attempted because the global call budget was
   exhausted, each configured observer independently scores every unique
   campaign Evaluation event with the same authorized
   case/reference/rubric payload.
3. Primary Evaluator behavior is the only input to diagnosis, Proposer, vector
   gates, and candidate pass/fail.
4. Agreement metrics are deterministic, mathematically correct, partitioned,
   explicit about missing evidence, and never described as truth or calibration.
5. Observer failures are visible and sanitized but do not fail a campaign;
   primary failures and integrity violations retain existing behavior.
6. A strict aggregate agreement report is persisted externally, bound to
   campaign state, and inspectable without provider calls or writes.
7. A v2 promotion approval digest binds the exact agreement report bytes;
   report corruption or substitution invalidates preview and apply.
8. Proposer, Actor, and observers receive no new unauthorized information.
9. Provider-call budgets, cache keys, profile snapshots, source integrity, and
   frozen holdout guarantees include observer execution without weakening.
10. No real profile, rollout, reference, score, agreement report, credential,
    runtime artifact, or local path enters the repository or CI artifacts.

## 13. Non-goals

- No automatic score correction, calibration, weighting, adjudication, or
  majority-truth claim.
- No disagreement gate or automatic promotion veto in v0.6.
- No fresh replay or cross-campaign drift detection.
- No campaign-series orchestration, target-quality stopping rule, or automatic
  real promotion.
- No automatic dataset growth or curriculum generation.
- No change to Actor, Proposer, candidate mutation, or human approval
  authority.
- No storage of raw per-event observer scores in the aggregate report.

## 14. Decisions for reviewer

The delegated reviewer must decide:

1. whether observer-only measurement is valuable enough without changing
   primary gates;
2. whether the metric set is sufficient and correctly avoids calibration
   claims;
3. whether observer failure should be non-fatal while integrity violations
   remain fatal;
4. whether aggregate partition reporting leaks any protected holdout detail;
5. whether bundle v2 is the correct way to bind agreement evidence to exact
   human promotion approval;
6. whether v1 compatibility and v2 strictness are fully specified;
7. whether deduplication by Evaluation event prevents parent/ablation double
   counting without hiding legitimate repeated observations;
8. whether the scope should include any disagreement veto now or defer all
   policy decisions until evidence exists.
