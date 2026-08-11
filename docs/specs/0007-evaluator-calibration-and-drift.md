# KPO v0.7 Evaluator Calibration and Drift Audit Specification

- Status: **Approved for TDD implementation**
- Depends on: `0002-external-profile-execution.md`,
  `0003-optimization-campaign.md`,
  `0006-evaluator-agreement-audit.md`
- Scope: approved synthetic anchor sets, fixed-rollout fresh replay,
  per-Evaluator calibration evidence, and cross-campaign drift comparison

## 1. Purpose

v0.6 reveals whether Evaluators disagree inside one campaign. It does not show
which Evaluator better matches a trusted scoring contract, whether an
Evaluator has systematic score bias, or whether the measurement instrument
changes between campaigns.

v0.7 characterizes those risks before KPO automates campaign series or dataset
curricula. A profile may register an externally stored synthetic anchor set.
Each anchor supplies a fixed rollout, selected hidden references, and
human-approved expected rubric scores. KPO freshly evaluates these fixed
requests once per campaign, computes calibration metrics, and can compare two
campaigns for matched-anchor score drift.

This is still measurement-only. Anchor evidence never corrects Evaluator
scores, changes diagnosis, enters Proposer input, modifies gates, or approves
or vetoes promotion. "Calibration" means agreement with the approved synthetic
score contract. It is not proof of factual truth or transfer to real-domain
accuracy.

## 2. Optional profile contract

Profiles may add:

```toml
[evaluator_audit]
anchor_manifest = "./anchors.jsonl"
max_provider_calls = 100

[evaluator_audit.drift_thresholds]
alignment = 0.10
validity = 0.05
```

- The complete section is optional. Profiles without it retain v0.6 behavior.
- `max_provider_calls` is a positive audit-only budget. It is separate from
  `campaign.max_provider_calls` and cannot reduce the optimization budget.
- `drift_thresholds` must contain exactly the rubric dimensions. Each value is
  finite and in `[0, 1]`.
- The anchor manifest and every referenced rollout file enter the profile
  snapshot and integrity monitor.
- Unknown fields, unsafe paths, duplicate anchor IDs, dimension mismatch, and
  malformed values are validation errors.

The JSONL anchor manifest protocol is `kpo.evaluator-anchor/v1`. Every row has
exactly:

```json
{
  "protocol": "kpo.evaluator-anchor/v1",
  "anchor_id": "anchor-001",
  "rollout_path": "./anchors/anchor-001.txt",
  "reference_ids": ["reference-001"],
  "expected_scores": {"alignment": 0.8, "validity": 1.0},
  "provenance": ["synthetic-fixture-review-001"]
}
```

- `anchor_id` is a unique safe component matching the campaign-ID rules.
- `rollout_path` resolves to a UTF-8 file inside the external profile root.
  Content must be non-empty.
- `reference_ids` is a non-empty, duplicate-free list of existing profile
  references. Only these references enter this anchor's Evaluator request.
- `expected_scores` contains exactly the rubric dimensions. Values are finite
  numbers in `[0, 1]`.
- `provenance` is a non-empty, duplicate-free list of non-empty strings. It
  explains the human review basis without entering Provider requests.
- Anchors are sorted by `anchor_id`; manifest row order is not execution order.
- Anchor rollouts are measurement fixtures, never policy, train, validation,
  holdout, or regression cases.

## 3. Explicit anchor approval

KPO validates anchor structure but cannot declare expected scores trustworthy.
Every distinct anchor-set digest requires explicit approval:

```text
kpo anchors approve --profile PROFILE
kpo anchors approve --profile PROFILE --approve APPROVAL_DIGEST
```

Preview returns the anchor count, rubric version, source digests, anchor-set
digest, and a canonical approval digest. It returns no rollout text, reference
content, expected per-anchor scores, or provenance strings.

Apply requires the exact preview digest and atomically creates an immutable
record:

```text
data_home/anchors/approved/<anchor_set_digest>.json
```

The record binds profile ID, rubric version/dimensions, manifest digest,
rollout/reference source digests, expected-score digest, anchor count, and
approval digest. Existing records are never overwritten. A campaign with an
`[evaluator_audit]` section refuses to start its audit unless the exact current
anchor set has an approved record.

Apply re-reads and re-hashes every bound source immediately before record
creation; source drift between preview and apply invalidates the approval.

Approval is human-only for real external profiles. A delegated reviewer may
review schemas and synthetic test fixtures but cannot approve the owner's real
anchor content or expected scores.
The human operator verifies anchor content through external means—direct file
review or independent digest computation—before supplying the approval digest.
KPO validates structural and byte identity; the human validates the semantic
correctness of expected scores and provenance.

## 4. Fixed Evaluator requests

Anchor replay isolates the Evaluator from Actor and policy changes. KPO does not
invoke the Actor for anchors. It constructs one deterministic `Rollout` per
anchor:

- `case_id` is a blinded anchor digest, never the human-readable `anchor_id`;
- `model_id = "kpo-anchor-fixture/v1"`;
- `output` is the exact rollout file content;
- `policy_digest` is an anchor-request digest, not the campaign policy;
- `retrieved_policy_ids` is empty;
- `actor_payload_digest` binds the anchor protocol and anchor-request digest.

KPO computes two set digests. The approved `anchor_set_digest` binds complete
rows, including expected scores and provenance, and never enters a Provider
request. A separate `anchor_request_set_digest` binds only sorted anchor IDs,
rollout byte digests, selected reference IDs, kinds, and byte digests, the
rubric digest, and fixed decoding seed. The blinded anchor digest binds
`anchor_id` and
`anchor_request_set_digest`. The per-anchor request digest binds only that
blinded digest and the same request-visible material. No Provider-visible digest
is derived from expected scores, provenance, or the complete
`anchor_set_digest`.
Every later reference to "anchor digest" means this blinded anchor digest.

The resulting rollout digest is stable while the anchor row and rollout bytes
are stable. Each Evaluator receives the fixed rollout, only the anchor's
selected references, and the profile rubric. It does not receive:

- expected scores or provenance;
- another Evaluator's result or identity;
- campaign cases, dataset partitions, diagnosis, candidate, gates, rejected
  candidates, agreement metrics, promotion data, or drift thresholds.

Evaluator execution order is primary first, followed by observers sorted by
name. Calibration code identifies slots as `primary` and `observer:<name>`;
identity and executable digest are recorded separately so a changed
configuration remains visible.

Configuration digest follows the v0.6 observer serialization contract: slot
name, identity, command, `timeout_seconds`, `pass_env` names (never values),
`data_home`-relative working-directory coordinate, and executable digest. The
primary uses slot name `primary` with the same fields.

## 5. Fresh replay and separate budget

Every new campaign with approved anchors performs one fresh anchor audit before
ordinary train evaluation. "Fresh" means no provider-response cache is read or
written across campaign IDs. Each attempted anchor Evaluator invocation calls
the configured command and consumes one call from the
`evaluator_audit.max_provider_calls` budget.

The audit budget is stored separately:

```text
data_home/campaigns/<campaign_id>/evaluator-audit-call-budget.json
```

It never increments or reduces the optimization campaign's
`call-budget.json`. Provider command validation, minimal environment, stderr
redaction, working-directory isolation, and integrity checks remain identical
to ordinary Evaluator invocation.

Provider failure, timeout, invalid response, or audit-budget exhaustion is
measurement unavailability and does not change the optimization campaign's
decision path. Integrity violations and audit persistence corruption remain
fatal infrastructure failures.

Fresh replay means once per new campaign, not once per resume attempt. Resume
loads the same audit report or continues its checkpoint; it never replays a
completed anchor slot merely because the campaign process restarted.

Anchor Evaluator adapters must implement deterministic decoding for identical
requests and fixed `random_seed = 0` (for example, temperature zero where the
underlying runtime exposes temperature). KPO binds the seed and complete command
configuration but cannot prove a third-party command's internal determinism;
an adapter that ignores this contract produces unsupported calibration
evidence.

## 6. Audit checkpoint and crash recovery

Before its first anchor call, a new campaign atomically initializes:

```text
data_home/campaigns/<campaign_id>/evaluator-calibration.checkpoint.json
```

The strict protocol `kpo.evaluator-calibration-checkpoint/v1` binds campaign
ID, profile snapshot digest, the complete `anchor_set_digest`, rubric, sorted
Evaluator slots and identities, audit budget, and completed anchor-slot
observations.

Each observation contains only an anchor digest, expected dimension scores,
Evaluator dimension scores or sanitized unavailability kind, and configuration
digest. It contains no anchor ID, rollout/reference content, provenance,
explanations, stderr, environment values, or absolute paths.

Before invoking a slot, KPO persists that slot as `pending` and consumes its
audit-budget unit. After return, it atomically replaces `pending` with successful
scores or a sanitized unavailable kind. If a process resumes with a `pending`
slot, KPO records `interrupted_unknown` and does not invoke it again: an external
call may already have occurred, so replay would violate once-per-campaign
semantics and could select a different nondeterministic result. On budget
exhaustion, all remaining configured slots are recorded as
`not_attempted_budget_exhausted` without invoking Providers.

A resumed audit with a missing, corrupt, mismatched, or conflicting checkpoint
transitions the campaign to `failed` with failure kind
`evaluator_calibration_checkpoint`. Checkpoint write failure uses the same
typed failure. After both final audit artifacts and campaign-state bindings are
durable, KPO removes the checkpoint. Cleanup failure is logged without
reversing a clean result.

## 7. Calibration metrics

For each Evaluator slot and rubric dimension, KPO compares successful scores to
the approved expected scores for the same anchor:

- `comparable_count`;
- `expected_mean`;
- `evaluator_mean`;
- `signed_error_mean = mean(evaluator_score - expected_score)`;
- `mean_absolute_error`;
- `root_mean_square_error`;
- `error_population_stddev`;
- `max_absolute_error`;
- unavailable counts grouped by sanitized kind.

Arithmetic iterates anchor digests lexicographically and Evaluator slots in
execution order: primary, then observers sorted by name.
Empty comparisons use count `0` and JSON `null` for numeric aggregates. Values
must be finite and remain within their mathematical ranges; KPO never emits
NaN or substitutes zero for missing evidence.

These are calibration metrics against synthetic expected labels. A positive
signed error means the Evaluator scores higher than the approved anchor label.
It does not establish real-world optimism, factual error, or model quality.
The approved anchor set is the complete finite measurement population;
`error_population_stddev` divides by `N`, not the `N-1` sample correction.

## 8. Strict audit artifacts

Every configured audit produces two external artifacts:

```text
data_home/campaigns/<campaign_id>/evaluator-calibration.json
data_home/campaigns/<campaign_id>/evaluator-calibration-observations.json
```

The aggregate report protocol is `kpo.evaluator-calibration/v1`. It contains:

- campaign/profile/profile-snapshot identity;
- complete `anchor_set_digest`, request-only `anchor_request_set_digest`, and
  approved-record file digest;
- rubric version and sorted dimensions;
- the exact per-dimension drift thresholds effective for this campaign;
- Evaluator slots in execution order, with identities and configuration digests;
- `collection_state`, exactly `complete` or `budget_exhausted`;
- configured anchor count and slot coverage counts;
- the metrics in section 7;
- exact observation-file byte digest;
- canonical `report_digest` over all other report fields.

The approved-record file digest is the SHA-256 digest of the immutable approval
record at
`data_home/anchors/approved/<anchor_set_digest>.json`. It transitively binds
the approval digest and every other record field.

The private observation protocol is
`kpo.evaluator-calibration-observations/v1`. It contains sorted anchor digests,
expected scores, and per-slot successful scores or sanitized unavailability
kinds. It contains none of the content fields forbidden in section 6. Its byte
digest is bound by the aggregate report.

Both files are written atomically and strictly reloaded before campaign state
is updated. Persistence or validation failure produces campaign failure kind
`evaluator_calibration_persistence`. No timestamp appears in either artifact;
the files are byte-deterministic for normalized campaign identity.

Campaign state stores only relative paths, exact file digests, aggregate report
digest, and a safe summary: anchor count, coverage per Evaluator slot,
maximum absolute calibration error by dimension, unavailable totals, and
collection state. `campaign-status` never returns per-anchor observations.

## 9. Cross-campaign drift CLI

```text
kpo evaluator-drift \
  --profile PROFILE \
  --baseline CAMPAIGN_A \
  --target CAMPAIGN_B
```

The command is read-only and makes zero Provider calls. It strictly loads both
campaigns' report and observation artifacts. Comparisons require equal profile
ID, anchor-set digest, rubric version, and dimension set. Profile snapshots and
Evaluator configurations may differ and are reported explicitly.

Evaluator slots are matched by `primary` or observer name, not by identity.
Added and removed slots are listed and excluded from numeric drift metrics. For
each matched slot and dimension over anchors with successful scores in both
campaigns, report:

- `matched_anchor_count`;
- `baseline_identity` and `target_identity`;
- `configuration_changed`;
- mean signed score delta `mean(target_score - baseline_score)`;
- mean and maximum absolute score delta;
- baseline and target unavailable totals.

Because comparisons require identical expected scores, the mean signed score
delta is mathematically identical to the change in mean signed calibration
error. KPO reports the former once and documents the identity rather than
emitting two names for the same value.

`drift_detected` is true when the absolute change in signed calibration error
for any matched slot/dimension—equivalently, the absolute mean signed score
delta—is strictly greater than that dimension's threshold stored in the target
campaign report. The output includes baseline/target thresholds and
`threshold_changed`; the current profile's possibly changed threshold never
rewrites historical semantics. Configuration change alone is reported but does
not invent behavioral drift when no comparable scores exist. Empty comparisons
use null metrics and cannot independently set `drift_detected`.

The CLI output contains aggregate values only—no anchor IDs/digests, raw scores,
rollout/reference content, or provenance. It does not write a drift report or
modify either campaign.

## 10. Campaign non-interference

Anchor audit outputs never enter:

- train failure selection or diagnosis;
- Proposer requests, rejected-candidate summaries, or candidate content;
- validation, holdout, regression, or ablation scores;
- vector gates or candidate pass/fail;
- agreement metrics;
- promotion bundles or approval digests in v0.7.

Except for fatal integrity/persistence failures, a campaign with anchors must
make the same optimization Provider requests and reach the same optimization
outcome as a no-anchor control with equivalent policy/dataset/providers.
Audit calls and their separate budget are additional measurement work only.

v0.7 deliberately does not bind calibration evidence into promotion bundle
v2. Operators inspect `campaign-status`, `evaluator-agreement`, and
`evaluator-drift` separately. Any future automation that relies on calibration
or drift requires another specification defining evidence freshness and stop
behavior.

## 11. Synthetic falsification experiment

Use only deterministic synthetic anchors and Providers:

1. approve an anchor set with fixed expected scores below `0.80`;
2. run a baseline campaign whose primary matches expected scores and whose
   observer has a fixed `+0.10` offset on one dimension;
3. change one Evaluator executable/configuration for a new campaign so the same
   dimension gains another deterministic `+0.15` offset;
4. keep other dimensions unchanged;
5. compare the two campaigns with `evaluator-drift`.

The experiment passes only if:

1. baseline and target calibration errors recover the known offsets exactly;
2. drift reports the changed dimension and exact `+0.15` calibration-error
   delta while unchanged dimensions report zero;
3. the configured threshold changes `drift_detected` only at its exact strict
   boundary;
4. a second campaign makes fresh anchor Provider calls despite prior campaign
   artifacts, while same-campaign resume makes none for completed slots;
5. anchor Provider failures and budget exhaustion are explicit without changing
   optimization gates or outcome;
6. Actor and Proposer captures contain no anchor identifiers, content,
   expected scores, calibration metrics, or drift thresholds;
7. calibration artifacts are byte-deterministic after campaign-ID and profile-
   snapshot normalization;
8. corruption, substitution, path escape, unknown fields, and observation-
   digest mismatch are rejected;
9. policy, dataset manifest, holdout lock, target, and promotion bundle remain
   byte-identical to a no-anchor control before explicit promotion.

The hypothesis is falsified if known offsets or drift are not recovered,
unchanged dimensions drift, anchor replay uses the cross-campaign Provider
cache, audit budget changes optimization calls, anchor information reaches the
Proposer, or audit evidence changes candidate outcome.

## 12. TDD sequence

1. Anchor profile and manifest validation tests: paths, exact dimensions,
   duplicates, expected-score ranges, provenance, and backward compatibility.
2. Preview/exact-approval tests for immutable approved anchor-set records.
3. Fixed-rollout construction and Evaluator information-isolation tests.
4. Pure calibration-math tests: signed/absolute/RMS/stddev/max errors, missing
   pairs, null aggregates, ordering invariance, and duplicate rejection.
5. Fresh executor and separate-budget tests, including same-campaign resume.
6. Checkpoint strictness, `pending` to `interrupted_unknown` crash recovery,
   corruption, and cleanup tests.
7. Aggregate/observation artifact schema, digest, path, identity, and state-
   binding tests.
8. Drift math and CLI tests: matched slots, changed configurations, thresholds,
   missing evidence, incompatible anchor sets, stable JSON, and non-mutation.
9. Campaign integration tests proving optimization call bytes, gates, candidate,
   report, and promotion bundle equal a no-anchor control.
10. Full legacy suite, build, hygiene/privacy scan, and section 11 experiment.

## 13. Acceptance criteria

1. Profiles without evaluator anchors retain exact v0.6 behavior.
2. Every anchor set is external, strict, source-bound, and explicitly approved
   before use.
3. Anchor requests use fixed rollouts and selected references; expected scores
   and provenance never enter any Provider request.
4. Each new campaign freshly invokes every configured Evaluator slot for each
   anchor under a separate finite budget; resume is idempotent.
5. Calibration metrics are deterministic, mathematically correct, explicit
   about missing evidence, and limited to synthetic expected-label agreement.
6. Strict aggregate and private observation artifacts are external, digest-
   bound, crash-recoverable, and safe in `campaign-status`.
7. Drift comparison uses matched anchors, detects known changes at exact human-
   configured thresholds, and exposes configuration changes separately.
8. Anchor evidence never changes optimization calls, diagnosis, Proposer,
   gates, candidate outcome, promotion bundle, policy, dataset, holdout, or
   target.
9. Audit persistence/integrity failures are typed and fatal; Provider failures
   and audit budget exhaustion remain visible measurement unavailability.
10. No real anchor, rollout, reference, expected score, observation, credential,
    runtime artifact, or local path enters the repository or CI artifacts.

## 14. Non-goals

- No automatic score correction, calibration adjustment, weighting,
  adjudication, evaluator ranking, or selection.
- No calibration/drift-based promotion gate, pause, rollback, or alert action.
- No automatic campaign series, quality stopping, or dataset curriculum.
- No automatic anchor generation, expected-score assignment, or approval.
- No real-domain transfer or factual-truth claim from synthetic calibration.
- No Actor execution for anchors and no use of anchors as optimization cases.
- No promotion-bundle protocol change in v0.7.
- No cross-anchor-set numeric drift comparison.

## 15. Human-only decisions

- Approving real anchor content, provenance, and expected scores.
- Choosing per-dimension drift thresholds and accepting fresh-replay cost.
- Deciding whether synthetic calibration transfers to the intended domain.
- Acting on calibration or drift evidence, including replacing an Evaluator,
  pausing optimization, changing data, or accepting observed drift.
- Deciding final target quality and authorizing any real promotion.
