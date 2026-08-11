# KPO v0.8 Campaign Series Evidence Specification

- Status: approved for TDD by delegated review
- Extends: `0003-optimization-campaign.md`,
  `0005-external-promotion-and-chaining.md`,
  `0006-evaluator-agreement-audit.md`, and
  `0007-evaluator-calibration-and-drift.md`
- Scope: opt-in campaign-series identity, aggregate campaign evidence,
  deterministic stopping indicators, lineage verification, and crash-safe
  reconciliation

## 1. Purpose

KPO v0.7 produces reproducible evidence for one finite campaign and can compare
Evaluator calibration across campaigns, but independent campaigns have no
framework-level relationship. Operators cannot yet answer, from a strict
artifact, whether repeated knowledge-policy work is still producing usable
improvements, whether applied changes are cumulatively degrading the frozen
holdout, or whether a configured series budget has been consumed.

v0.8 adds an optional series layer. A series binds an ordered set of existing
finite campaigns, captures only privacy-safe aggregates that the campaign has
already computed, verifies policy lineage, and derives deterministic stopping
indicators. It makes zero additional Provider calls.

The series is an evidence and budget layer, not an autonomous optimizer. It
does not start another campaign, apply a promotion, grow a dataset, or stop on
quality indicators without an explicit operator action. `max_campaigns` is the
one enforced series safety limit.

## 2. Preserved guarantees

1. `[series]` is optional. A profile without it and a campaign invocation
   without `--series` preserve the v0.7 profile snapshot, campaign state,
   Provider requests, call counts, artifacts, and outcomes exactly.
2. Every campaign remains finite under its existing iteration and Provider-call
   budgets.
3. Promotion remains preview-first and digest-approved. A series never invokes
   or weakens `kpo promote`.
4. Dataset growth remains preview-first, digest-approved, and unable to change
   the frozen holdout.
5. Series execution adds no Actor, Evaluator, observer, calibration, or
   Proposer calls and does not share or reduce any campaign budget.
6. Holdout details remain unavailable to Proposer. Series state, status, and
   evidence contain no case identifiers or per-case measurements.
7. Agreement and calibration remain observational. Series evidence does not
   correct, rank, select, or replace Evaluators.
8. A campaign belongs to at most one series and cannot be rebound.

## 3. Optional profile contract

A campaign profile may add:

```toml
[series]
max_campaigns = 10

[series.stopping]
min_improvement = 0.01
patience = 3
holdout_degradation = 0.05
```

`max_campaigns` is required when `[series]` exists and is a positive integer.
It is a hard safety limit on attached campaigns, including failed campaigns.

`[series.stopping]` is optional. Its fields are strict:

- `min_improvement` and `holdout_degradation` are finite numbers in `[0, 1]`;
- `patience` is a positive integer;
- `min_improvement` and `patience` must either both be present or both absent;
- `holdout_degradation` may be configured independently;
- unknown fields at either level are errors.

The scalar thresholds apply independently to every rubric dimension. Absence
of `[series.stopping]` means that only `max_campaigns` and owner stop are
available.

Profiles without `[series]` cannot initialize or join a series. The profile
file remains an integrity-monitored campaign source, so adding `[series]` may
change that profile's ordinary campaign snapshot. Exact v0.7 snapshot
compatibility applies to profiles that omit the new section. When a profile has
`[series]` but a campaign omits `--series`, KPO performs no series write or
additional Provider call; the series configuration is also bound separately by
the stable contract whenever the series feature is used.

## 4. Stable series contract

The complete campaign profile snapshot is intentionally not stable across a
series: an approved promotion changes policy files and an approved dataset
growth changes cases and the dataset manifest. A series therefore stores a
privacy-safe `contract` object and its `series_contract_digest`.

The contract contains only stable semantic component digests and safe scalar
metadata:

- profile ID;
- rubric version, ordered dimension names, and canonical rubric digest;
- separate canonical digests for the validation, holdout, regression, and
  ablation threshold mappings from `[campaign.gates]`; these bind only the
  configured scalar thresholds and do not include dataset content or a result
  of validating a dataset against those thresholds;
- for Actor, primary Evaluator, and Proposer: identity, a digest of the complete
  normalized command configuration, and executable-content digest;
- for every observer, in configured order: observer name, identity, normalized
  configuration digest, and executable-content digest;
- frozen holdout digest;
- a digest of safe promotion coordinates: target path relative to the profile
  root, allowlist, and add directory;
- evaluator-audit contract digest or null. When present, it binds the complete
  anchor set and request-set digests, budget, drift thresholds, and configured
  Evaluator slots without exposing anchor content or paths;
- canonical series-configuration digest.

The normalized command configuration digest binds all command arguments,
identity, timeout, environment-variable names, and protocol-relevant settings.
Only its digest enters the artifact: command paths, working directories, and
environment values never do. Executable bytes are bound separately.

The contract deliberately excludes policy content, policy-manifest content,
case/reference source bytes outside the frozen holdout contract, and the full
dataset digest. These may change through approved promotion or dataset growth.

At series initialization KPO stores the contract object and digest. Before any
series-bound campaign Provider call, KPO recomputes both. A mismatch is a typed
`series_contract_mismatch` error and reports only changed component names and
old/new component digests. It never reports paths, commands, environment
values, or content.

The current frozen holdout digest must always equal the value stored by the
series. A mismatch is fatal even if another component digest was forged to
match.

The base profile loader must accept `series` as an optional strict top-level
section so the same profile remains usable by standalone commands. It does not
interpret the section outside campaign-profile loading.

## 5. Campaign evidence artifact

Only a series-bound campaign writes immutable evidence for each terminal
revision:

```text
data_home/campaigns/<campaign_id>/series-evidence.json
data_home/campaigns/<campaign_id>/series-evidence.<revision>.json
```

Revision zero uses the unsuffixed path for compatibility. Revision one and
later use the numbered path. Each `(campaign_id, revision)` evidence file is
immutable and idempotent only when the complete content is identical.

The strict protocol `kpo.campaign-series-evidence/v1` contains:

- campaign, series, profile, zero-based series-index, and non-negative
  revision identity;
- full campaign-profile snapshot digest and stable series-contract digest;
- dataset digest and frozen holdout digest effective for the campaign;
- terminal campaign state;
- parent-policy digest and nullable candidate-policy digest;
- iteration count, optimization call count, and rejected-candidate count;
- nullable passing-candidate aggregate scores;
- nullable vector-regression report digest;
- nullable agreement report byte digest and report digest;
- nullable calibration aggregate/observation byte digests and report digest;
- a canonical record digest over every preceding field.

For a `promotion_previewed` campaign, `scores` contains exactly the weighted
matched gains already present in the in-memory `VectorRegressionReport`:

```json
{
  "validation_gain": {"dimension": 0.05},
  "holdout_gain": {"dimension": 0.04},
  "regression_gain": {"dimension": 0.03},
  "ablation_gain": {"dimension": 0.06}
}
```

These values are candidate-minus-parent gains, not absolute scores. For
`no_patchable_diagnosis`, `budget_exhausted`, and `failed`, `scores`, candidate
policy, and regression-report digest are null. Rejected-candidate summaries
remain in the campaign state under the existing information boundary and are
not copied into series evidence.

The artifact never contains case IDs, policy content, artifact IDs, mutation
content, rollout output, reference content, raw/per-event/per-anchor scores,
anchor digests, expected anchor scores, provenance, diffs, stderr, commands,
paths, timestamps, or environment values.

KPO writes the evidence atomically, strictly reloads it, and records its
profile-relative path, byte digest, and record digest in the terminal campaign
state before attempting the series append. A write or reload failure changes a
series-bound campaign to `failed` with failure kind
`campaign_series_evidence_persistence`. A standalone campaign has no such write
and retains v0.7 behavior.

That failure kind is resumable. Resume must not take the existing
promotion-bundle fast path while a required series evidence file or binding is
missing. It deterministically reconstructs the terminal aggregate from the
campaign's already completed digest-keyed cache, retries evidence persistence,
then appends it. It makes zero new Provider calls. Missing, corrupt, or
incomplete cache material is an integrity error; KPO never fabricates a null or
zero aggregate to make reconciliation succeed.

A series-bound failed campaign may be explicitly resumed with the same
campaign ID. The resumed terminal result receives the next revision while
keeping the same series index. Older evidence and entries are never modified
or deleted. Only a latest `failed` revision can be resumed; a non-failed latest
revision is final.

The passing aggregate is captured before the in-memory report disappears. The
series layer never loads or reconstructs `VectorRegressionReport.measurements`.

## 6. Series state artifact

Series state is stored at:

```text
data_home/series/<series_id>/series.json
```

Series IDs follow campaign-ID path-component rules. The strict protocol
`kpo.campaign-series/v1` contains:

- protocol, series ID, profile ID, state, contract, and contract digest;
- initial policy digest and frozen holdout digest;
- configured `max_campaigns` and stopping thresholds;
- an ordered array of entries;
- a canonical state digest.

Each entry contains only:

- zero-based series index, campaign ID, and non-negative revision;
- campaign terminal state;
- parent- and nullable candidate-policy digests;
- dataset and campaign-profile snapshot digests;
- evidence relative path, byte digest, and record digest.

The series state contains no timestamps. Given the same explicit series ID,
configuration, and ordered campaign evidence, its bytes are deterministic.
Promotion state is not copied into an entry because it may change after the
campaign is attached; it is strictly reloaded from the bound campaign state.

Series states are exactly `active` and `stopped_by_owner`. Only
`stopped_by_owner` is terminal. Diminishing-return, holdout-degradation, and
max-campaign results are derived indicators, not persisted state transitions.

Series state writes are atomic and strictly reloaded. Each entry is identified
by `(campaign_id, revision)` and is immutable. Revision zero is the first
terminal result. Revision `N+1` is accepted only when revision `N` is `failed`;
revisions are gap-free and consecutive for that campaign. An identical append
is idempotent. A changed append at an existing coordinate is rejected.

All revisions of one campaign use the same campaign ID and series index and
appear consecutively. A series index cannot be rebound to another campaign.
The effective terminal result is the highest revision for each campaign.
`max_campaigns` counts unique campaign IDs, never revisions.

## 7. Binding, ordering, and reconciliation

Before Provider execution, a series-bound campaign state adds immutable
`series_id`, `series_index`, and `series_contract_digest` fields. Standalone
campaigns omit all three.

Before allocating an index, KPO reconciles already terminal campaigns bound to
the series and rejects a new start while any bound campaign is non-terminal.
The next index is exactly the number of unique campaigns with contiguous
indexes. A
short-lived exclusive mutation lock serializes series initialization, campaign
index allocation, append, reconcile, and owner stop. A second concurrent
mutation fails without Provider calls. A stale lock is never silently removed;
`series reconcile` may remove it only when its recorded campaign is either
absent before any campaign state was created, is durably terminal and safely
attached, or is provably interrupted during initial index allocation before a
Provider subsystem could be constructed. The last case requires all of the
following: the lock records allocation phase and the same campaign ID/index;
the campaign state is still the exact initial `running` state; and the campaign
directory has no optimization or audit call-budget file, Provider cache,
agreement/calibration checkpoint, report, promotion bundle binding, or other
post-initialization artifact. Reconcile first atomically changes that campaign
to `failed` with `failure_kind = series_initialization_interrupted`, then
removes the lock. The failed campaign may be resumed normally. Any ambiguous
state remains locked and produces a typed manual-recovery error; absence of a
`call_count` field alone is never sufficient evidence that no call occurred.

After a series-bound campaign writes its terminal campaign state, KPO appends
its evidence inline. A process crash between those writes may leave a terminal
campaign absent from the series.

`series reconcile` scans campaign-state files under `data_home`, selects only
campaigns whose immutable `series_id` equals the requested series, validates
their contract, index, revision, terminal state, evidence binding, and state
digest, then appends missing entries in index and revision order. It performs
no Provider call and is idempotent. It fails on gaps, non-sequential revisions,
changed entries, missing or invalid evidence, a foreign series binding, or a
non-terminal bound campaign. A revision may follow only a failed revision.

A standalone campaign cannot later be attached. A campaign bound to one series
cannot be resumed or reconciled under another.
The reconcile scan therefore ignores every pre-v0.8 or standalone campaign
whose state has no `series_id`, even if its campaign ID happens to resemble a
series-bound ID.

`series status` and `series evidence` are strictly read-only. They do not
reconcile, rewrite indicators, remove locks, invoke Providers, or mutate any
campaign, policy, dataset, target, or promotion artifact.

## 8. Dynamic policy lineage

Series initialization records the current policy digest as
`initial_policy_digest`. To determine the expected parent for a new campaign,
KPO selects the latest revision for each campaign and replays effective
campaigns in series-index order starting from that digest:

1. every entry's parent-policy digest must equal the current replay digest;
2. KPO strictly reloads the entry's campaign state;
3. if its promotion state is `applied`, its candidate-policy digest becomes the
   next replay digest;
4. if promotion is absent, `previewed`, or `recovered`, the replay digest is
   unchanged.

`applying` is not treated as either parent or candidate state. It means the
recoverable promotion transaction may have partially modified policy or
manifest bytes. Every series operation raises the typed error
`series_promotion_recovery_required` until the operator runs the existing
`kpo promote --recover` flow and the campaign becomes `recovered`, or the
transaction is known to have finished as `applied`. No series read returns
potentially mixed lineage evidence while a promotion is `applying`.

The currently loaded profile policy must equal the final replay digest before
any new Provider call. Otherwise KPO raises `series_policy_lineage_mismatch`.

This dynamic replay handles promotion after campaign attachment without a
stale series snapshot. Applying an older, non-latest preview after a later
campaign has already used the old parent makes the historical replay invalid;
status, evidence, reconcile, and new campaign start all reject that divergence.

## 9. Derived evidence and stopping indicators

`series evidence` strictly loads every entry, campaign state, and campaign
evidence record. It verifies all byte, record, state, report, contract, and
lineage bindings before producing output. Learning curves and indicators use
only the latest revision for each campaign. Older revisions remain available
through full audit output and never affect derived indicators.

For each campaign and dimension, the learning curve reports:

- nullable proposed validation, holdout, regression, and ablation matched gain;
- whether the candidate is currently applied;
- dataset segment index;
- cumulative applied holdout gain from the initial series policy.

A dataset segment is a maximal contiguous run of entries with the same full
dataset digest. Validation and regression gains from different segments are
displayed but are not claimed comparable. Diminishing-return patience resets
to zero at every dataset-segment boundary.

Within one dataset segment, a non-failed campaign is a no-improvement campaign
when either it has no passing aggregate scores or every dimension's proposed
validation gain is strictly less than `min_improvement`. Failed campaigns are
displayed but neither increment nor reset patience. The
`diminishing_returns` indicator becomes true when the current segment ends with
at least `patience` consecutive no-improvement campaigns.

The frozen holdout is stable across all segments. For every applied promotion,
KPO adds its matched holdout gain to a per-dimension cumulative total. This sum
is a sum of separately measured candidate-minus-parent gains, not an absolute
holdout score; it is interpretable only along the verified sequential policy
lineage and must be read alongside Evaluator calibration/drift evidence. The
`holdout_degradation` indicator becomes true when any cumulative total is
strictly less than `-holdout_degradation`. Unapplied previews never affect the
cumulative total.

The `max_campaigns` indicator is true when unique attached campaign count is greater
than or equal to the configured maximum. Unlike the two quality indicators,
it is enforced: another campaign in that series is rejected before Provider
calls. Quality indicators remain advisory and do not block campaign execution.

The evidence output also contains:

- campaign counts by terminal state and applied-promotion count;
- safe dataset segment boundaries represented by indexes and dataset digests;
- existing privacy-reviewed agreement aggregates by observer/dimension when
  present;
- existing privacy-reviewed calibration aggregates by Evaluator/dimension when
  present;
- typed unavailability rather than fabricated zeroes for absent reports.

Agreement and calibration reports are loaded through their strict existing
validators. Only aggregate metrics enter series output. Report identities and
byte digests must match campaign evidence. Per-event observations and
per-anchor calibration observations are never loaded into the output. For the
private calibration-observation file, the series loader reads bytes only to
verify the aggregate report's bound byte digest; it never JSON-decodes or
interprets the per-anchor records.

The output protocol is `kpo.campaign-series-report/v1`. It contains no case
IDs, policy/artifact content, artifact IDs, reference content, per-case or
per-anchor scores, anchor digests, approval digests, provenance, commands,
paths, timestamps, stderr, or environment values. For the same validated local
artifacts its canonical JSON bytes are deterministic.

## 10. CLI

```text
kpo series init --profile PROFILE [--series SERIES_ID]
kpo series status --profile PROFILE --series SERIES_ID
kpo series evidence --profile PROFILE --series SERIES_ID [--full]
kpo series reconcile --profile PROFILE --series SERIES_ID
kpo series stop --profile PROFILE --series SERIES_ID

kpo campaign --profile PROFILE --series SERIES_ID
kpo campaign --profile PROFILE --series SERIES_ID --resume CAMPAIGN_ID
```

`series init` creates exactly one immutable series identity and prints safe
configuration, contract digests, and initial state. An omitted ID is generated
with the same mechanism as campaign IDs. Existing series content is never
overwritten.

`series status` prints safe identity, state, counts, latest campaign ID,
dataset-segment count, and current derived indicators. `series evidence`
prints the complete safe aggregate report using only effective latest
revisions. `--full` includes every immutable revision in an audit section while
derived metrics remain latest-revision-only. Both are read-only. Reports expose
both unique `campaign_count` and total `revision_count`.

`series reconcile` is the explicit crash-recovery write described in section
7. `series stop` atomically changes `active` to `stopped_by_owner`; repeated
stop is idempotent. A stopped series cannot accept or resume a campaign and
cannot be reactivated.

A campaign `--resume` under a series must match the immutable series ID and
index already stored in its failed campaign state. Its latest attached revision
must also be `failed`; the new terminal result is appended as revision `N+1`.
Existing standalone resume semantics remain unchanged.

CLI errors for contract mismatch, policy lineage mismatch, owner stop,
max-campaign exhaustion, concurrent mutation, and reconciliation corruption
are typed, stable, and occur before Provider calls whenever the relevant state
already exists.

## 11. Synthetic falsification experiment

Use an external synthetic profile with two rubric dimensions, fixed Provider
executables, a frozen holdout, and:

```toml
[series]
max_campaigns = 6

[series.stopping]
min_improvement = 0.01
patience = 2
holdout_degradation = 0.05
```

Produce five ordered campaigns:

1. passing preview with validation gains `0.20/0.20`, holdout gains
   `0.10/0.10`, then apply it;
2. passing preview with validation gains `0.10/0.10`, holdout gains
   `0.02/0.02`, then apply it;
3. grow only non-holdout data through approved dataset growth, then produce a
   passing preview with validation gains `0.005/0.005`; do not apply it;
4. finish `no_patchable_diagnosis` on the same grown dataset;
5. produce a passing preview with validation gains `0.007/0.007`, holdout gains
   `-0.18/0.00`, then apply it under gates that explicitly permit this drop.

The experiment is falsified unless all conditions hold:

1. campaigns 1-2 form dataset segment 0 and campaigns 3-5 segment 1;
2. patience resets at campaign 3, then campaigns 3-4 set
   `diminishing_returns`; campaign 5 keeps it true;
3. applying campaigns 1, 2, and 5 yields exact cumulative holdout gains
   `-0.06/0.12`; with threshold `0.05`, only the first dimension triggers
   `holdout_degradation` under the strict less-than rule;
4. campaign 6 is allowed despite quality indicators and makes
   `max_campaigns` true;
5. campaign 7 is rejected before any Provider call;
6. the exact learning curve, applied flags, segments, and indicators survive a
   fresh process and are byte-deterministic;
7. killing the process after a terminal campaign state but before append leaves
   recoverable evidence; `series reconcile` attaches it exactly once with zero
   Provider calls;
8. applying an out-of-order older preview causes strict lineage rejection;
9. a changed rubric, gate, Provider configuration/executable, observer set,
   frozen holdout, audit contract, or series threshold causes a contract error,
   while approved policy promotion and non-holdout dataset growth do not;
10. scanning every series artifact and CLI result finds no prohibited private
    field or absolute path;
11. a control profile without `[series]` produces byte-identical v0.7 campaign
    state, Provider requests, budgets, promotion bundle, and outcome;
12. all legacy tests, repository hygiene, compilation, and package build pass.

## 12. Planned TDD sequence after approval

1. Optional series-profile parsing and exact standalone-v0.7 compatibility.
2. Safe contract-component and digest tests, including allowed and forbidden
   changes.
3. Strict series state creation, immutable revision-chain append, stop, and
   atomic-write tests.
4. Strict per-revision campaign series-evidence artifact tests, including
   denylisted fields, revision-aware paths, idempotency, and byte/record binding.
5. Pure dataset segmentation, diminishing-return, cumulative-holdout, strict
   threshold, and max-budget tests.
6. Dynamic applied-promotion lineage tests over effective latest revisions,
   including out-of-order mutation and invalid revision order.
7. Series binding, single membership, unique-campaign index allocation,
   concurrent mutation, and idempotent reconcile tests.
8. Series resume tests for failed-to-success revision append, rejection after a
   non-failed latest revision, and unique-ID max-campaign counting.
9. Read-only status/evidence aggregate and determinism tests, including full
   revision audit, report corruption, and unavailable metrics.
10. CLI tests for all `series` subcommands and `campaign --series`/resume.
11. Crash injection between evidence, terminal campaign state, and revision
    append, including reconcile of a missing resumed revision.
12. External five/six-campaign falsification experiment with dataset growth and
    exact cumulative gains.
13. Full legacy suite, privacy/hygiene scan, compile, build, delegated code
    review, and release gate.

## 13. Acceptance criteria

1. A series binds ordered campaigns, stable semantic configuration, frozen
   holdout identity, and policy lineage without binding approved mutable policy
   or non-holdout dataset content.
2. Series-bound campaign evidence persists only aggregate matched gains and
   strict report bindings; it contains no per-case or per-anchor data.
3. Every series read detects state, evidence, report, contract, or lineage
   corruption before returning a result.
4. Dataset growth creates an explicit evidence segment and resets validation
   patience; frozen-holdout cumulative gains remain comparable across segments.
5. Diminishing-return and holdout-degradation indicators use the exact rules in
   section 9 and remain advisory.
6. `max_campaigns` is enforced before Provider calls. Owner stop is explicit,
   permanent, and idempotent.
7. Series revision append and reconcile are deterministic, idempotent,
   single-membership, preserve every failed/resumed attempt, and recover the
   terminal-state/append crash window without Provider calls.
8. Promotion timing is handled by dynamic campaign-state reload and strict
   sequential lineage verification.
9. Status and evidence are read-only and privacy-safe. Series operations never
   mutate policy, dataset, target, promotion, or Provider cache artifacts.
10. Standalone profiles and campaigns preserve v0.7 behavior exactly.

## 14. Non-goals

- No automatic campaign loop, promotion approval/apply, dataset growth,
  curriculum, sampling, anchor generation, or rollback.
- No new Provider call, new quality metric, score correction, Evaluator
  selection, or calibration-based gate.
- No claim that validation gains remain comparable across dataset segments.
- No replacement for an untouched external final evaluation set. Repeated use
  of the frozen campaign holdout remains an adaptive-evaluation risk and is a
  candidate for a later generalization-gate specification.
- No automatic terminal transition from quality indicators.
- No series reactivation, campaign rebinding, merging, deletion, or reordering.
- No binding of series evidence into promotion bundles in v0.8.
- No real-domain data or profile in this repository.

## 15. Human or delegated decisions

- Approving promotions, dataset growth, anchors, thresholds, and any external
  cost or publication remains outside the series runtime.
- An operator or explicitly delegated reviewer decides whether advisory quality
  indicators justify `series stop`, further data work, or a new series.
- Final target quality and transfer to a real domain require separately approved
  evidence and are not inferred from this synthetic series.
