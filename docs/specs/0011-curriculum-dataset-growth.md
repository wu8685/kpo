# KPO v0.11 Differential-Guided Curriculum Specification

- Status: implemented, externally falsified, and approved for release
- Extends: `0003-optimization-campaign.md`,
  `0008-campaign-series-orchestration.md`,
  `0009-untouched-generalization-measurement.md`, and
  `0010-reference-reasoning-differential.md`
- Scope: deterministic metadata-only candidate selection, train-only dataset
  growth preview, digest-bound approval, recovery, and privacy-safe evidence

## 1. Purpose

KPO can diagnose how an Agent's reasoning differs from an authorized reference
and can safely append an externally prepared dataset manifest. It cannot yet
decide which available train cases would best address the observed reasoning
gaps. Dataset growth is therefore safe but manual and unguided.

v0.11 adds a curriculum layer between Campaigns. It reads aggregate actionable
differential kinds from a completed Campaign, ranks externally prepared
candidate cases using metadata only, and produces a train-only dataset-growth
preview. The operator still reviews and approves the exact selection before
the dataset manifest changes.

Curriculum selection characterizes training needs; it does not evaluate the
candidate cases. Selection performs zero Actor, Evaluator, Proposer,
differential, or other Provider calls. Its value is judged only by later
Campaign validation, holdout, regression, ablation, and untouched
generalization evidence.

## 2. Preserved guarantees

1. Profiles without `[curriculum]` preserve v0.10 profile snapshots, Campaign
   requests and call counts, dataset-growth behavior, series contracts and
   state, evidence protocols, promotion bundles, CLI output, and approval
   semantics exactly.
2. Curriculum can add cases only to `Partition.TRAIN`. It cannot add, remove,
   edit, reweight, or repartition validation, holdout, regression, or existing
   train entries.
3. Candidate selection never invokes a Provider and never uses candidate
   rollout, Evaluation, score, hidden-reference content, or observed outcome.
4. Candidate prompts and visible contexts may be read only for mechanical
   contamination and duplicate checks. They do not influence ranking.
5. Generalization remains structurally separate and single-use. No
   generalization identifier, prompt, context, reference, score, or artifact
   may become a curriculum signal or selected dataset entry.
6. Selection does not write the external dataset manifest. Only an exact
   digest-approved apply operation may reuse the existing journaled
   `DatasetManager` growth transaction.
7. Curriculum evidence is advisory. A later policy candidate must still pass
   every ordinary promotion gate; curriculum selection cannot alter gates,
   scoring, candidate ranking, or promotion authority.
8. Candidate inboxes, selection plans, case identifiers, and feature metadata
   are external/private runtime data and never enter this repository.

## 3. Profile configuration

An optional strict section enables curriculum selection:

```toml
[curriculum]
inbox = "./candidate-inbox.jsonl"
max_selection = 8
diversity_weight = 0.30
allowed_facets = ["boundary", "causal-order", "counterfactual"]
```

The section contains exactly:

- `inbox`: safe path inside the external profile directory;
- `max_selection`: positive integer and hard upper bound on selected cases per
  plan;
- `diversity_weight`: finite number in `[0, 1]`.
- `allowed_facets`: unique non-empty array forming the external profile's
  closed facet registry. Each value is case-folded and whitespace-collapsed
  and must already equal that normalized representation.

`[curriculum]` requires `[series]` and `[differential_analyzer]`. Silent
fallback to unguided or diversity-only selection is forbidden: without a
strictly bound, available differential signal, KPO returns
`curriculum_signal_unavailable` and makes no plan.

The stable series contract adds a `curriculum` component containing only:

- algorithm protocol `kpo.curriculum-selection/v1`;
- `max_selection`;
- `diversity_weight`;
- canonical facet-registry digest.

The mutable inbox path, bytes, case identifiers, and metadata do not enter the
stable series contract. Each plan separately binds the exact inbox bytes. A
configured Campaign profile snapshot binds the curriculum algorithm,
selection limits, diversity weight, and facet-registry digest, but not mutable
inbox bytes. An unconfigured profile preserves the exact v0.10 snapshot,
contract shape, and digest.

## 4. Candidate inbox

The inbox is a strict non-empty JSONL file. Each record contains exactly:

```json
{
  "case_id": "candidate-001",
  "gap_kinds": ["missing", "frame_divergence"],
  "facets": ["boundary", "causal-order"],
  "provenance": ["source-bundle:v1"]
}
```

Fields are:

- `case_id`: unique non-empty case identifier present in the external
  profile's case manifest;
- `gap_kinds`: unique non-empty array drawn from `missing`, `surplus`,
  `contradicted`, and `frame_divergence`; `matched` is not actionable and is
  forbidden;
- `facets`: unique non-empty array used only for diversity; every value must
  occur in the profile's closed `allowed_facets` registry;
- `provenance`: unique non-empty source strings using the existing dataset
  provenance rules.

Records are canonicalized in case-ID order for digest and selection purposes;
file order cannot change the result. Duplicate case IDs, fields, gap kinds,
facets, or provenance values are errors. Unknown fields and invalid UTF-8 are
errors.

Gap-kind and facet labels are normalized by case folding and whitespace
collapse before uniqueness, scoring, and digesting. The closed gap-kind values
must already equal their normalized representation.

Every candidate case and all of its hidden reference IDs must already be valid
under the external profile. The inbox carries no prompt, context, reference
content, partition, weight, or score.

## 5. Eligibility and isolation

Eligibility is checked before scoring and before any preview artifact is
written. Any violation rejects the whole plan rather than silently dropping a
candidate.

Candidate prompts and visible-context items are retrieved from the external
profile's case manifest through the inbox `case_id`. They are never supplied
by the inbox or a Provider. Hidden-reference contents are neither retrieved
nor used by eligibility or scoring; only the already validated reference IDs
in the case contract are relevant.

1. A candidate ID cannot occur in the current Campaign dataset in any
   partition.
2. A candidate ID cannot occur in the generalization manifest.
3. Normalize prompts and visible-context strings by Unicode-aware case folding
   and whitespace collapse, using the same deterministic normalization as
   dataset contamination checks.
4. A candidate prompt cannot equal any current dataset or generalization
   prompt after normalization.
5. A candidate visible-context item cannot equal any current dataset or
   generalization context item after normalization.
6. Candidate inbox records cannot duplicate each other's normalized prompts,
   contexts, or complete prompt/context content digest.
7. Symlinks or path traversal cannot move the inbox, generated proposal, or
   runtime artifacts outside their authorized roots.
8. Selected dataset rows are constructed internally with partition `train`,
   weight `1.0`, and the inbox provenance. The inbox cannot control these
   fields.

After construction, KPO must load the complete proposed dataset through the
existing strict `load_dataset` and, when configured, re-run
`load_generalization_manifest` against it. The frozen holdout digest must equal
the current lock before a growth preview is returned.

## 6. Differential signal selection

Curriculum selection takes a required series ID. KPO strictly loads the series
state, verifies the current stable contract, and searches effective series
entries in reverse order.

Signal precedence is:

1. the newest `promotion_previewed` Campaign with a strictly loaded
   differential summary containing at least one available analysis and at
   least one actionable differential entry;
2. otherwise, the newest terminal Campaign satisfying the same conditions;
3. otherwise, `curriculum_signal_unavailable`.

The Campaign state, summary byte digest, summary record digest, profile
snapshot digest, and series entry evidence binding must all agree. Missing,
tampered, mismatched, or private iteration-only evidence cannot be used as a
signal.

The differential summary must have `available_count >= 1` and:

```text
kind_counts["missing"]
+ kind_counts["surplus"]
+ kind_counts["contradicted"]
+ kind_counts["frame_divergence"] > 0
```

A summary with no available analysis or an all-zero actionable vector is not
a curriculum signal.

The actionable count vector contains exactly:

- `missing`;
- `surplus`;
- `contradicted`;
- `frame_divergence`.

`matched` is excluded. Counts are normalized into weights:

```text
gap_weight[kind] = count[kind] / sum(actionable counts)
```

The private selection plan binds the source Campaign through:

```text
canonical_digest({
  "campaign_id": campaign_id,
  "profile_id": profile_id,
  "series_id": series_id,
  "series_index": series_index,
  "revision": revision
})
```

This deterministic identity digest does not replace the existing evidence
bindings and is not published in aggregate Campaign or series evidence.

## 7. Deterministic selection

For candidate `c`:

```text
relevance(c) = min(1, sum(gap_weight[k] for k in c.gap_kinds))
feature_set(c) = {"gap:" + k} union {"facet:" + f}
diversity(c, selected) = 1                                  if selected is empty
                       = 1 - max(Jaccard(c, s) for s selected) otherwise
score(c, selected) = (1 - diversity_weight) * relevance(c)
                   + diversity_weight * diversity(c, selected)
```

Selection is greedy. At each step KPO recomputes diversity for all remaining
candidates and selects the highest tuple:

1. information score;
2. relevance;
3. lexicographically smaller case ID.

Scores use deterministic finite arithmetic rounded to twelve decimal places.
Selection stops at `min(max_selection, eligible_count)`. It must select at
least one case.

The algorithm is deliberately metadata-only. Feature quality is an external
data responsibility, and later evaluation may falsify the selection. KPO does
not claim causal information gain or monotonic improvement.

## 8. Private plan and public preview

The strict private plan protocol `kpo.curriculum-selection/v1` contains:

- profile identity and current profile-snapshot digests, plus the series
  identity digest;
- current dataset and frozen holdout digests;
- inbox byte and canonical-record digests;
- curriculum configuration digest;
- source differential summary byte and record digests;
- source Campaign identity digest;
- actionable count vector and normalized weights;
- ordered selected case IDs with relevance, diversity, and information score;
- aggregate selected gap coverage;
- generated proposed-dataset byte and record digests;
- nested `DatasetGrowthPreview.approval_digest`;
- curriculum approval digest.

It is stored under:

```text
data_home/curriculum/previews/<curriculum_approval_digest>/selection.json
data_home/curriculum/previews/<curriculum_approval_digest>/proposed-dataset.jsonl
```

The curriculum approval digest binds every field above, the exact current
dataset bytes, and the nested growth approval. Preview directories are
immutable: an existing path with different bytes is an error; identical bytes
are reusable.

CLI preview output may show selected IDs, scores, gap coverage, and the
approval digest because it is an explicit local review surface. It never shows
candidate prompts, contexts, references, source Campaign IDs, policy content,
rollouts, Evaluations, or private claim graphs.

No curriculum plan, selected ID, feature, or score enters Campaign status,
series evidence, generalization artifacts, or promotion bundles. Later series
evidence naturally records only the changed dataset digest and a new dataset
segment.

## 9. Approval, apply, and recovery

Commands are:

```bash
kpo curriculum select --profile PROFILE --series SERIES_ID
kpo curriculum select --profile PROFILE --series SERIES_ID --approve DIGEST
kpo curriculum recover --profile PROFILE --approval DIGEST
```

Preview performs zero writes to the external dataset and zero Provider calls.
It persists only the immutable private review plan and generated proposal under
`data_home`.

Apply must:

1. recompute and strictly reload the plan from current source bytes, then read
   `proposed-dataset.jsonl` from that exact immutable preview directory as the
   canonical proposed manifest bytes and verify its byte and record digests
   against the plan;
2. require the supplied digest to equal the curriculum approval digest;
3. reject stale current-dataset, inbox, differential-summary, profile,
   contract, holdout, generalization, or generated-proposal bytes;
4. reconstruct the exact `DatasetGrowthPreview`;
5. invoke `DatasetManager.apply_growth` using the nested growth approval;
6. return the committed journal binding without starting a Campaign.

The curriculum approval therefore authorizes exactly one existing
`DatasetManager` transaction; KPO does not infer or substitute the inner
approval.

Recovery strictly loads the immutable plan, maps its curriculum approval to
the nested growth approval, and invokes `DatasetManager.recover_growth`.
Recovery is valid only for a prepared transaction and restores the exact
pre-growth manifest snapshot. Repeated apply or recovery cannot create a
second growth event.

## 10. Series behavior

Curriculum selection is permitted only while the series state is `active` and
no series mutation lock exists. Preview uses operation
`curriculum_preview`; apply uses `curriculum_apply`. These exclusive locks
prevent allocation, evidence append, reconciliation, stop, generalization
consumption, and curriculum work from racing.

Apply re-verifies that the source differential Campaign is still the selected
effective entry and that the series has not advanced since preview. The plan
binds the series state digest for this purpose.

Stale-lock behavior is operation-specific:

1. `curriculum_preview` performs no external dataset write. Existing series
   stale-lock recovery may remove it only after loading the series and proving
   its state digest still equals the lock's initial digest.
2. `curriculum_apply` binds the curriculum approval and nested growth approval
   in the lock. It must never be removed as an ordinary harmless stale lock.
3. `curriculum recover --approval DIGEST` may take over a matching stale
   `curriculum_apply` lock. It strictly loads the immutable plan and growth
   journal: a `prepared` journal is rolled back through
   `DatasetManager.recover_growth`; a `committed` journal is verified against
   the proposed bytes and reported as already committed; absence of a journal
   is safe only when the external manifest still equals the plan's original
   bytes. Only then is the stale lock removed.
4. A mismatched approval, series digest, journal, or manifest leaves the lock
   intact and reports manual recovery required.

Recovery excludes concurrent mutation and remains permitted after the series
stops because restoring a prepared dataset transaction is a safety operation
rather than new curriculum authority.

Dataset growth does not append a Campaign entry or consume `max_campaigns`.
The next Campaign uses the new dataset digest. Existing series learning-curve
segmentation detects that digest change without mutating historical evidence.

Generalization-enabled series remain protocol `kpo.campaign-series/v2`;
curriculum does not change generalization state or protocol. Differential
Campaign evidence remains `kpo.campaign-series-evidence/v2`; curriculum does
not introduce a new Campaign evidence protocol.

## 11. Failure model

Expected failures are typed and make no external dataset change:

- `curriculum_signal_unavailable`;
- malformed or empty inbox;
- unknown, duplicate, already-dataset, or generalization candidate;
- prompt/context contamination;
- invalid or tampered summary, series, plan, or proposal;
- stale preview;
- wrong approval digest;
- series not active or mutation locked;
- frozen holdout mismatch;
- prepared transaction requiring recovery.

Because selection performs zero Provider calls, Provider failures and Provider
budgets are not curriculum states. `max_selection` is the finite selection
budget.

## 12. TDD sequence

1. strict inbox schema, canonical digest, closed gap kinds, and validation;
2. candidate eligibility, generalization exclusion, and normalized duplicate
   checks;
3. strict signal discovery and digest binding across series/Campaign summary;
4. gap-weight normalization, relevance, Jaccard diversity, greedy ordering,
   rounding, and tie breaking;
5. curriculum profile parsing, stable-contract binding, and unconfigured v0.10
   byte compatibility;
6. proposed train-only dataset construction and double isolation validation;
7. private plan schema, immutable persistence, privacy denylist, and tamper
   loading;
8. preview/apply approval binding, stale checks, and zero Provider calls;
9. interruption recovery and at-most-once behavior;
10. operation-specific stale-lock recovery, series mutation exclusion,
    dataset segmentation, and generalization coexistence;
11. CLI preview/apply/recover and path-free output tests;
12. external falsification, full legacy suite, build, hygiene/privacy scans,
    and delegated implementation review.

## 13. External falsification experiment

Outside the repository, build a synthetic active series whose latest bound
differential signal is:

```text
missing=6, frame_divergence=3, surplus=1, contradicted=0
```

Prepare at least forty candidate cases across the four actionable kinds and
multiple facets declared in the profile's closed `allowed_facets` registry.
An otherwise valid candidate using an undeclared facet must be rejected. The
experiment passes only if:

1. repeated previews over identical bytes are byte-identical and select the
   same ordered IDs and scores;
2. selected cases overrepresent `missing` and `frame_divergence` relative to a
   uniform inbox while the diversity term selects more than one facet;
3. selection makes zero Provider calls and reads no candidate reference
   content for scoring;
4. case-folded prompt/context collisions with train, validation, holdout,
   regression, generalization, or another inbox candidate reject the entire
   plan before a preview;
5. a wrong approval, modified inbox, advanced series, changed differential
   summary, or modified proposed bytes cannot change the dataset;
6. an injected interruption after manifest replacement is recoverable through
   a matching stale `curriculum_apply` lock to the exact original bytes, a
   stale `curriculum_preview` lock is safely removable, and the same approval
   cannot be applied twice;
7. the next Campaign sees the expanded train partition and a new dataset
   segment without changing historical evidence or consuming an extra series
   slot;
8. validation, holdout, regression, and ablation gates remain authoritative;
9. an untouched generalization measurement remains single-use and contains no
   curriculum case, feature, plan, or score;
10. at least one approved selected batch produces non-negative holdout gain
    and reduces the targeted actionable differential count in a later
    Campaign; otherwise the selection hypothesis is falsified;
11. no candidate content or private differential evidence appears in public
    Campaign/series/promotion/generalization artifacts, CLI output beyond the
    explicit local preview, or the public repository;
12. an unconfigured control preserves v0.10 bytes, protocols, and call counts.

Any failed condition falsifies v0.11. Metadata relevance or diversity alone is
not evidence of learning.

## 14. Non-goals

- no automatic case generation or reference creation;
- no candidate rollout, scoring, uncertainty sampling, or active learning;
- no LLM or Provider-selected curriculum;
- no validation, holdout, regression, calibration, or generalization growth;
- no silent diversity-only fallback when differential evidence is absent;
- no feature-quality guarantee or claim of causal information gain;
- no automatic Campaign start, policy promotion, series stop, or
  generalization consumption;
- no real profile, corpus, inbox, selection plan, or runtime evidence in the
  repository or CI.
