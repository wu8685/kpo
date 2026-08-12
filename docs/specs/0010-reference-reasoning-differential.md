# KPO v0.10 Reference Reasoning Differential Specification

- Status: implemented and approved by delegated implementation review
- Extends: `0003-optimization-campaign.md`,
  `0006-evaluator-agreement-audit.md`, and
  `0009-untouched-generalization-measurement.md`
- Scope: optional train-only structured reasoning extraction, claim
  differencing, three-axis diagnosis, privacy-safe Proposer enrichment, and
  aggregate evidence

## 1. Purpose

KPO can currently detect score failures and determine whether repeated
knowledge-policy changes transfer beyond a reused holdout. It cannot explain
how an Agent's analytical path differs from a reference analysis. A scalar
score or `missing_policy_knowledge` signal does not distinguish a missing
intermediate claim, a contradictory conclusion, an unsupported leap, or a
different analytical frame.

v0.10 adds an optional read-only differential analyzer for the train case that
already drives Campaign diagnosis. It extracts structured reasoning from the
Agent rollout and authorized train reference, compares the two claim graphs,
and supplies a privacy-reviewed diagnosis summary to the Proposer.

The analyzer describes similarity and difference. It does not declare the
reference correct, copy reference prose into policy, change gates, or use
validation, holdout, or generalization cases.

## 2. Preserved guarantees

1. Profiles without `[differential_analyzer]` preserve v0.9 Provider requests,
   call counts, cache keys, Campaign state, promotion bundles, series evidence,
   agreement/calibration evidence, generalization evidence, and CLI output
   bytes exactly.
2. Differential analysis runs only for the `Partition.TRAIN` evaluation that
   already precedes diagnosis. No validation, holdout, regression,
   calibration-anchor, or generalization case may enter any differential
   Provider request or artifact.
3. Existing Actor, Evaluator, observer, Proposer, promotion, and generalization
   semantics remain unchanged except for one optional, privacy-safe field in a
   configured Proposer request.
4. Every optional Provider call counts against the existing finite Campaign
   Provider-call budget, including failures. v0.10 adds no hidden or unbounded
   budget.
5. Differential artifacts are runtime evidence outside the repository and
   never write to the external knowledge store.
6. A configured analyzer is advisory. Candidate acceptance still depends only
   on the existing validation, holdout, regression, and ablation gates.
7. Differential analysis and v0.9 generalization are orthogonal and may coexist
   in one profile and series. Differential analysis changes configured
   per-Campaign evidence; generalization changes series state and end-of-series
   measurement. Neither feature may read or mutate the other's private data.

## 3. Structured claim graph

Strict protocol `kpo.reasoning-claims/v1` contains:

- `schema_version` equal to `v1`;
- an ordered non-empty `claims` array;
- a canonical record digest.

Each claim contains exactly:

- `claim_id`: unique safe identifier within the graph;
- `claim_type`: closed enum `factual`, `analytical`, `predictive`, `normative`,
  or `conclusive`;
- `text`: non-empty private claim text;
- `evidence_citations`: an array of identifiers present in the Provider's
  authorized input; it may be empty only for an explicitly unsupported claim;
- `prerequisite_claim_ids`: unique earlier or later claim IDs forming a DAG;
- `confidence`: finite number in `[0, 1]`;
- `salience`: finite number in `[0, 1]`;
- `provenance`: `agent_rollout` or `reference_artifact`.

Claim IDs are semantic only within one graph. All prerequisite IDs must exist,
self-edges and duplicate edges are errors, and the graph must be acyclic. The
canonical digest binds every field and claim order.

Claim text, citations, and graph edges are private intermediate evidence. They
never enter Campaign state, series evidence, promotion bundles, CLI output, or
the public repository.

## 4. Claim differential

Strict protocol `kpo.claim-differential/v1` binds the two input graph digests
and contains ordered entries. Each entry contains exactly:

- `kind`: closed enum `matched`, `contradicted`, `missing`, `surplus`, or
  `frame_divergence`;
- nullable reference claim ID and nullable Agent claim ID, with coordinates
  required by the kind;
- `summary`: a non-empty derived explanation;
- `confidence`: finite number in `[0, 1]`.

`missing` means reference-only; `surplus` means Agent-only. `matched` and
`contradicted` require both coordinates; contradiction also requires a shared
sub-question. `frame_divergence` requires both coordinates and means that both
claims express well-defined but materially incompatible analytical frames. If
only one side expresses a frame, the entry is `missing` or `surplus`, not
`frame_divergence`. Every input claim is accounted for by at least one entry. An
entry cannot refer to an unknown claim, and exact duplicate coordinates/kinds
are errors.

The complete differential is private. Only the diagnosis summary defined
below may enter the Proposer request.

## 5. Three independent diagnosis axes

Strict protocol `kpo.differential-diagnosis/v1` contains:

- the Evaluation digest and claim-differential digest;
- `reference_is_standard: false`;
- conclusion validity, reasoning fidelity, and reasoning soundness axes;
- ordered gap summaries;
- mandatory caveats;
- a canonical record digest.

Each axis contains a closed assessment (`supported`, `challenged`, or
`unavailable`), confidence in `[0, 1]`, and a non-empty summary:

- **conclusion validity** asks whether the conclusion is supported by the
  existing Evaluation evidence and authorized outcomes;
- **reasoning fidelity** describes structural resemblance to the reference;
- **reasoning soundness** asks whether the Agent's own claim DAG follows from
  its cited evidence and prerequisites.

These axes are independent. A matching conclusion cannot imply soundness; high
fidelity cannot imply validity; divergence from the reference cannot imply an
error. `conclusion_validity` must cite the existing Evaluation digest and may
not be computed only from claim matching.

Each gap summary contains only differential kind, claim types, an abstract
frame label, confidence, and a derived recommendation category. It contains no
claim text, reference text, citation content, case ID, rollout output, score,
path, or Provider error.

`caveats` must explicitly state that the reference is comparison material, not
truth, and that fidelity is not a substitute for validity or soundness.

## 6. Optional Provider contract

A Campaign profile may add:

```toml
[differential_analyzer.extractor]
adapter = "command"
command = ["./bin/reasoning-extractor"]
model_id = "extractor-v1"
timeout_seconds = 30
pass_env = []

[differential_analyzer.differ]
adapter = "command"
command = ["./bin/claim-differ"]
model_id = "differ-v1"
timeout_seconds = 30
pass_env = []

[differential_analyzer.diagnostician]
adapter = "command"
command = ["./bin/differential-diagnostician"]
model_id = "diagnostician-v1"
timeout_seconds = 30
pass_env = []
```

All three sections are required together; partial configuration and unknown
fields are errors. They use the existing command-adapter rules, minimal
environment, external working directories, executable-content binding,
redacted failures, source-integrity verification, and path-free wire protocol.

Provider roles and requests are:

1. `reasoning_extractor`:
   `{source_kind, sources: [{source_id, content}], claim_schema_version}`. The
   reference-side request receives, in the case contract's order, exactly the
   same complete hidden reference contents supplied to the primary Evaluator
   for that train case; it receives no rollout or policy. The Agent-side request
   contains one source holding the rollout output and no reference or policy.
2. `claim_differ`: `{reference_claims, agent_claims}`.
3. `differential_diagnostician`: `{claim_differential, evaluation_summary}`.
   The summary contains rubric version, dimension names/scores, failure kinds,
   and Evaluation digest, but no rollout output or reference content.

Every response is strictly parsed and digest-bound. Provider failures are
typed and cannot be converted into fabricated empty graphs or zero-valued
diagnoses.

## 7. Campaign integration and budget

For a configured Campaign iteration, after the primary train Evaluation and
before ordinary diagnosis/Proposer execution, KPO performs exactly four
optional calls:

1. extract the Agent rollout claim graph;
2. extract the authorized train reference claim graph;
3. compute the claim differential;
4. compute the differential diagnosis.

Calls use the existing Campaign call executor and cache. Cache keys bind role,
Provider identity/configuration/executable, input digest, rubric digest where
applicable, and protocol version. Cached reference extraction may be reused
only when every bound byte and Provider coordinate is identical.

If the remaining Campaign budget cannot cover the next call, the Campaign
terminates under existing budget-exhaustion semantics. A Provider or protocol
failure produces unavailable differential evidence and continues through the
existing ordinary diagnosis path; it never makes a Campaign falsely pass.
A failed call makes the differential evidence for that iteration unavailable.
Successful earlier calls remain in the ordinary digest-keyed Campaign cache
and may be reused on resume only when every bound cache-key component is
unchanged; partial cached results are never reported as available evidence.

The Proposer request gains nullable `differential_diagnosis`. When available it
contains only:

- `reference_is_standard: false`;
- the three aggregate axes;
- privacy-safe gap summaries and caveats;
- the diagnosis record digest.

It does not duplicate raw reference claims. The existing authorized train
reference supplied to the Proposer by v0.3 remains unchanged; v0.10 neither
adds references nor expands that boundary.

## 8. Mechanical partition isolation

Differential orchestration accepts a `Partition` argument and rejects every
value except `Partition.TRAIN` before a Provider call. Campaign integration has
one call site, immediately after the train Evaluation. Vector regression,
observer evaluation, calibration, series reporting, promotion, and
generalization modules must not import or invoke differential orchestration.

Tests instrument every Provider role and prove that validation, holdout,
regression, anchor, and generalization IDs/content never occur in requests,
cache material, private differentials, aggregate evidence, or Proposer
enrichment.

## 9. Private and aggregate artifacts

Private per-iteration claim graphs, differentials, and full diagnoses are
stored under the Campaign runtime directory in digest-bound files. They may
contain train reference/rollout content and are never printed.

A configured terminal Campaign writes strict aggregate protocol
`kpo.differential-analysis-summary/v1` containing only:

- Campaign/profile identity digests;
- extractor, differ, and diagnostician identity/configuration/executable
  digests;
- attempted, available, and unavailable analysis counts;
- aggregate matched/contradicted/missing/surplus/frame-divergence counts;
- aggregate three-axis assessment counts;
- nullable safe failure-kind counts;
- a canonical record digest.

It contains no case IDs, claim IDs/text/edges, differential summaries, gap
summaries, references, rollout output, scores, citations, policy content,
commands, paths, timestamps, stderr, or environment values.

For configured series Campaigns, per-Campaign series evidence uses the explicit
protocol `kpo.campaign-series-evidence/v2` and adds nullable summary byte/record
digests. This is distinct from the series **state** protocol
`kpo.campaign-series/v2` introduced by v0.9 generalization. A profile may use
both protocols simultaneously: differential configuration selects evidence
v2, while generalization configuration independently selects series-state v2.
Series evidence may report only aggregate count trends loaded from strictly
bound summaries. Unconfigured Campaigns continue to emit the exact evidence
v1 artifacts.

`campaign-status` adds only aggregate availability/counts when configured.

## 10. Stable contracts and promotion binding

Configured Provider semantic/executable digests enter the Campaign profile
snapshot and stable series contract. Drift is rejected before further Provider
work. A configured promotion bundle binds the exact aggregate differential
summary bytes, as agreement and calibration summaries are already bound. It
does not copy private differential artifacts.

Differential evidence cannot change gates, candidate ranking, approval digest
semantics, or promotion authority except that the approval digest binds the
additional reviewed aggregate bytes when configured.

The stable series contract adds a `differential_analyzer` component containing
exactly these safe coordinates for each role:

- extractor identity, normalized configuration digest, and executable digest;
- differ identity, normalized configuration digest, and executable digest;
- diagnostician identity, normalized configuration digest, and executable
  digest.

The normalized configuration binding follows the v0.8 Provider component
rules and includes command arguments, timeout, environment-variable names, and
protocol settings only through its digest. It never exposes commands, working
directories, environment values, or paths.

## 11. TDD sequence

1. claim graph strict schema, canonical digest, references, and DAG validation;
2. claim differential schema and complete-accounting validation;
3. three-axis diagnosis independence, caveats, and privacy denylist;
4. command Provider protocols and malformed/failure responses;
5. optional strict profile parsing, snapshot/contract binding, and unconfigured
   v0.9 byte compatibility;
6. fail-fast train-only partition isolation with instrumented Providers;
7. four-call Campaign integration, cache keys, finite-budget behavior, and
   unavailable fallback;
8. Proposer enrichment privacy and unchanged legacy request tests;
9. private persistence plus aggregate summary strict loading/tamper tests;
10. series-evidence/promotion binding and read-only status trends;
11. external falsification, full legacy suite, build, hygiene/privacy scans,
    and delegated implementation review.

## 12. External falsification experiment

Outside the repository, use synthetic reasoning with a known four-step
reference chain: identify frame, apply pattern, verify boundary, conclude.
Construct Agent rollouts covering at least:

- correct conclusion with a missing boundary step;
- wrong conclusion after a contradictory intermediate claim;
- correct sound reasoning under a different frame;
- unsupported surplus claim.

The experiment passes only if:

1. both claim graphs match the known claims and DAGs;
2. all five differential kinds are correctly classified across the cases;
3. validity, fidelity, and soundness produce the expected independent
   combinations, including high validity with low fidelity;
4. the Proposer receives the expected abstract reasoning-gap categories and no
   raw claim/reference text in the differential field;
5. configured patches improve a separate validation set and an untouched
   generalization measurement without using either set in differential calls;
6. zero validation/holdout/regression/generalization content enters any
   differential Provider request or private train artifact;
7. no reference reasoning appears in aggregate Campaign/series evidence,
   promotion bundles, status, or CLI output;
8. an otherwise identical unconfigured profile preserves v0.9 bytes and call
   counts;
9. malformed or failing differential Providers cannot create a passing
   candidate, bypass the budget, or fabricate available evidence.
10. a profile configuring both differential analysis and generalization uses
    evidence v2 plus series-state v2 without cross-reading either feature's
    private artifacts, and the untouched measurement remains single-use.

Any failed condition falsifies the implementation. Fidelity improvement alone
is not success.

## 13. Non-goals

- no automatic adoption or verbatim copying of reference reasoning;
- no claim that a reference expert is correct;
- no validation, holdout, regression, calibration, or generalization
  differential analysis;
- no change to Evaluator scores, gates, candidate ranking, automatic promotion,
  rollback, or owner stop decisions;
- no cross-case pattern mining, curriculum selection, case sampling, or
  automatic dataset growth;
- no real-profile orchestration or real domain corpus in the repository/CI;
- no built-in LLM SDK or framework-selected extraction prompt;
- no replacement of existing failure diagnosis; differential evidence only
  enriches it when available.
