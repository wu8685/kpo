# 0013 — Dual-loop feedback and component optimization

## Status

Approved by delegated governor on 2026-08-13. Review context:
`kpo-spec-0013-dual-loop-review`; final task:
`019ff6ef-f4f4-7dfe-8e33-3b6b9c55254e`.

## Problem

KPO currently diagnoses a failed evaluation and may propose a knowledge-policy
patch. That is necessary but incomplete. Repeated evidence can instead show
that the knowledge was retrieved incorrectly, the Actor failed to apply it,
the Evaluator is unstable, the Proposer generated the wrong mutation, the
dataset cannot identify the failure, or the role orchestration is defective.

Routing every failure to the knowledge policy corrupts the policy with fixes
for failures caused elsewhere. Refusing to update anything until the Evaluator
is perfect prevents the optimization process from learning at all.

KPO therefore needs two coupled but separately governed loops:

1. an **object loop** that improves the active knowledge policy;
2. a **meta loop** that improves the components and roles that run, evaluate,
   and govern the object loop.

Both loops consume evidence. Neither may approve its own mutation.

## Goals

1. Preserve every actionable observation as versioned feedback.
2. Attribute feedback to the most plausible optimization target without
   assuming that every failure is missing knowledge.
3. Produce either a policy candidate, a component candidate, or a bounded
   request for more evidence.
4. Allow a verified winner to become the active policy or active component for
   the next iteration.
5. Keep validation, holdout, provenance, rollback, privacy, and independent
   approval guarantees intact.

## Non-goals

- changing model weights;
- treating fidelity to a reference as factual truth;
- allowing an LLM component to approve its own replacement;
- tuning several components on the same evidence without an explicitly
  registered factorial experiment;
- publishing external profiles, prompts, role personas, real feedback, or
  domain knowledge in this repository.

## Optimization targets

The closed target registry is:

| Target | Responsibility | Example repair |
| --- | --- | --- |
| `knowledge_policy` | knowledge, procedures, examples, priorities, limits | add a missing rule or applicability boundary |
| `retrieval` | selecting policy artifacts for a case | change query, ranking, filtering, or context budget |
| `actor` | applying retrieved policy to produce an answer | change the external Actor instruction or role contract |
| `evaluator` | extracting and judging evidence | change evaluator decomposition, verifier panel, calibration, or response contract |
| `rubric` | defining dimensions, weights, gates, and judgment wording | split a conflated dimension or repair an invalid gate |
| `proposer` | converting diagnosed gaps into minimal mutations | change proposal constraints or mutation strategy |
| `dataset` | cases, partitions, labels, references, and coverage | add a train case, repair an inconsistent reference, or create a new untouched set |
| `orchestration` | role topology, sequencing, budgets, retries, and arbitration | split extractor/verifier roles or change escalation order |
| `runtime` | provider execution and integrity infrastructure | repair timeout, schema, persistence, or identity handling |

The registry is domain-neutral. External profiles may name component instances,
but must map them to one of these targets.

## Feedback record

Every feedback item is immutable and content-addressed. Protocol
`kpo.feedback/v1` contains exactly:

- `feedback_id`;
- source Campaign, iteration, case, partition, rollout, evaluation, and policy
  digests where available;
- one or more evidence references, never unbound prose;
- an observed symptom from a closed registry;
- zero or more attribution hypotheses;
- whether additional evidence is required;
- record digest.

Operational logs may attach a creation time as non-canonical envelope metadata.
It is excluded from the protocol bytes and record digest, so identical evidence
produces an identical feedback record.

The closed symptom registry is:

- `missing_knowledge`;
- `wrong_or_overbroad_knowledge`;
- `retrieval_miss`;
- `application_failure`;
- `unsupported_reasoning`;
- `reference_problem`;
- `evaluator_disagreement`;
- `evaluator_calibration_drift`;
- `rubric_defect`;
- `proposal_failure`;
- `coverage_gap`;
- `role_or_sequence_failure`;
- `runtime_failure`.

An attribution hypothesis contains:

- one optimization target;
- a confidence in `[0, 1]`;
- non-empty supporting evidence digests;
- non-empty disconfirming or missing evidence;
- a proposed experiment class, not mutation content.

Confidence orders investigation. It is not a reward and cannot by itself
authorize a mutation.

## Feedback router

The router consumes only persisted evidence and returns
`kpo.feedback-routing/v1`:

- the feedback digest;
- ordered attribution hypotheses;
- one disposition: `policy_candidate`, `component_candidate`,
  `collect_evidence`, `reference_review`, or `runtime_repair`;
- the chosen target when the disposition creates a candidate;
- the evidence and rule that selected the disposition;
- a record digest.

### Deterministic safety rules

1. Runtime/provider failure routes to `runtime_repair`; it cannot mutate policy.
2. Reference inconsistency routes to `reference_review`; it cannot mutate
   policy or score the Actor as wrong.
3. Evaluator uncertainty, disagreement, or calibration drift routes to
   `collect_evidence` or an `evaluator` candidate; it cannot mutate policy from
   the disputed judgment.
4. Retrieval failure cannot mutate policy until a replay proves the required
   artifact is absent rather than merely not retrieved.
5. Policy-application failure routes to an `actor` or `retrieval` experiment
   unless independent evidence shows the policy instruction itself is
   ambiguous or contradictory.
6. Missing or invalid policy knowledge may create a policy candidate only from
   train evidence. Validation, holdout, regression, and untouched
   generalization evidence may gate or reject a candidate but never create or
   modify one.
7. Competing top hypotheses whose confidence difference is below a configured,
   pre-registered margin route to `collect_evidence`; the router must not pick
   one arbitrarily.
8. A feedback item may create multiple hypotheses but at most one mutation
   candidate in one iteration.

Rules 1–6 are framework invariants. The ambiguity margin is external profile
configuration and is sealed before a Campaign begins.

### Composition with the existing diagnosis path

Dual-loop routing is a strict opt-in superset of the existing deterministic
`diagnose()` path, not a second policy diagnosis implementation. With the
feature disabled, `diagnose()` and `propose_patch()` remain byte-for-byte and
call-for-call unchanged. With it enabled, each existing `DiagnosisKind` is
first mapped to a feedback symptom:

| Existing diagnosis | Feedback symptom |
| --- | --- |
| `missing_policy_knowledge` | `missing_knowledge` |
| `retrieval_failure` | `retrieval_miss` |
| `policy_application_failure` | `application_failure` |
| `invalid_or_misapplied_rule` | `wrong_or_overbroad_knowledge` |
| `missing_applicability_boundary` | `wrong_or_overbroad_knowledge` |
| `insufficient_task_evidence` | `coverage_gap` |
| `reference_inconsistency` | `reference_problem` |
| `evaluator_uncertainty` | `evaluator_disagreement` |
| `runtime_or_provider_failure` | `runtime_failure` |

The existing diagnosis digest is an evidence reference in the feedback record.
Its deterministic priority and patchability remain authoritative. The router
may further restrict mutation, require an experiment, or route a non-policy
candidate; it cannot make an existing non-patchable diagnosis directly
patchable. New symptoms arise only from separately persisted evidence produced
by configured components.

## Optional profile contract

The feature is enabled only by this exact top-level TOML section:

```toml
[feedback_optimization]
ambiguity_margin = 0.15
max_component_candidates = 4
max_consecutive_rejections = 2
enabled_targets = [
  "knowledge_policy", "evaluator"
]

[feedback_optimization.gates.knowledge_policy]
hard = ["validation_non_degradation", "holdout_non_degradation"]
advisory = ["confirmed_gap_delta"]

[feedback_optimization.gates.evaluator]
hard = ["independent_calibration", "repeatability_non_degradation"]
advisory = ["agreement_delta", "provider_call_delta"]
```

- `ambiguity_margin` is finite and in `[0, 1]`.
- `max_component_candidates` and `max_consecutive_rejections` are positive
  integers and enter Campaign budgets.
- `enabled_targets` is a unique non-empty subset of the closed target registry.
  `knowledge_policy` must be present when legacy policy proposals are routed.
- Each enabled target requires one `gates.<target>` table. `hard` is a unique
  non-empty array from that target's closed metric registry; `advisory` is a
  unique array from the same registry and cannot overlap `hard`.
- The example is illustrative; profiles select only metrics implemented for
  their enabled targets. Unknown target or metric names are rejected.
- The complete section, gates, budgets, component versions, and ambiguity
  margin enter the profile snapshot and are sealed before the Campaign begins.
  They cannot change within a registered experiment.

Profiles without the section do not parse, validate, snapshot, instantiate, or
call any dual-loop component.

The closed gate-metric registry is:

| Target | Allowed metrics |
| --- | --- |
| `knowledge_policy` | `confirmed_gap_delta`, `validation_non_degradation`, `holdout_non_degradation`, `regression_non_degradation`, `unsupported_claim_non_degradation`, `boundary_violation_non_degradation` |
| `retrieval` | `retrieval_recall_delta`, `retrieval_precision_non_degradation`, `actor_outcome_non_degradation`, `context_budget_non_increase` |
| `actor` | `semantic_outcome_delta`, `unsupported_claim_non_degradation`, `instruction_compliance_delta` |
| `evaluator` | `independent_calibration`, `repeatability_non_degradation`, `agreement_delta`, `discrimination_delta`, `provider_call_delta` |
| `rubric` | `dimension_isolation`, `independent_outcome_validity`, `agreement_non_degradation`, `discrimination_delta` |
| `proposer` | `accepted_patch_delta`, `validation_outcome_non_degradation`, `patch_size_non_increase`, `regression_non_degradation` |
| `dataset` | `coverage_delta`, `label_review_pass`, `reference_review_pass`, `contamination_absent` |
| `orchestration` | `semantic_outcome_non_degradation`, `provider_call_delta`, `cost_delta`, `latency_delta`, `failure_rate_non_degradation` |
| `runtime` | `integrity_pass`, `recovery_pass`, `failure_rate_non_degradation`, `semantic_bytes_unchanged` |

Metric semantics and direction are fixed by this specification. Every `_delta`
is candidate minus parent:

- higher-or-equal is passing for `retrieval_recall_delta`, `semantic_outcome_delta`,
  `instruction_compliance_delta`, `discrimination_delta`,
  `accepted_patch_delta`, and `coverage_delta`;
- lower-or-equal is passing for `confirmed_gap_delta`, `provider_call_delta`,
  `cost_delta`, and `latency_delta`;
- `agreement_delta` is advisory-only and has no passing direction. It reports
  changed agreement for diagnosis, because maximizing agreement alone can
  reward correlated error.

All `_non_degradation` and `_non_increase` metrics require candidate no worse
than parent. `_pass`, `_absent`, `independent_*`, and
`dimension_isolation` are Boolean hard evidence. A profile cannot place
`agreement_delta` in `hard` or redefine any arithmetic or direction.

## Object loop: knowledge-policy optimization

The existing policy candidate and promotion contracts remain authoritative.
This specification adds the following lifecycle:

```text
active policy N
  -> train rollout and feedback
  -> policy candidate N+1
  -> matched validation, regression, ablation, and holdout measurement
  -> independent approval
  -> active policy N+1 for the next iteration
```

A passing policy candidate is not merely retained as a report: it becomes the
next active external policy snapshot only after the existing human-owner,
digest-bound `promote --approve DIFF_DIGEST` transaction succeeds. Enabling
`[feedback_optimization]`, passing gates, or receiving an Evaluator verdict is
not promotion authorization.

The active-policy pointer and complete lineage are persisted outside the
checkout. Promotion never writes raw sources. A failed or rejected candidate
remains evidence and cannot become an active parent.

## Meta loop: component and role optimization

Protocol `kpo.component-candidate/v1` contains exactly:

- target and external component instance ID;
- parent component version and digest;
- candidate version and immutable configuration digest;
- feedback and evidence digests that motivated the change;
- one declared mutation hypothesis;
- expected benefit and possible regressions;
- frozen replay-plan digest;
- rollback descriptor;
- candidate digest.

Private component bytes remain external. KPO records their digests and
versioned evidence, not their sensitive contents.

Each component experiment changes exactly one registered target. Interaction
changes involving two or more targets require a separate, pre-approved
factorial experiment specification. A component candidate is evaluated against
its parent on the same frozen replay plan and the same policy snapshot.

### Target-specific evaluation

- `retrieval`: retrieval recall/precision against authorized labels, Actor
  outcome, context budget, and unchanged holdout isolation;
- `actor`: matched semantic outcome and unsupported-claim regressions under the
  same retrieved policy;
- `evaluator`: calibration, repeatability, agreement, and discrimination on
  sealed anchors or independently labeled historical outputs; the candidate
  Evaluator cannot judge itself;
- `rubric`: discrimination, inter-rater agreement, dimension isolation, and
  outcome validity on independently labeled sealed anchors; the rubric author
  and candidate Evaluator cannot be the sole measurement or approver;
- `proposer`: accepted minimal patches, downstream validation outcome, patch
  size, and regression rate; the candidate Proposer cannot approve its own
  patches;
- `dataset`: coverage and label/reference review; frozen holdout members never
  move into train, and new cases do not retroactively repair a completed run;
- `orchestration`: semantic outcome plus provider calls, cost, latency,
  failure rate, and role-attribution evidence;
- `runtime`: deterministic integrity and failure-recovery tests before any
  semantic rerun.

## Multi-dimensional reward evidence

KPO does not collapse feedback into one scalar reward. Protocol
`kpo.optimization-evidence/v1` reports a vector:

- confirmed reasoning-gap deltas;
- rubric-dimension deltas where the Evaluator is authorized;
- validation, holdout, and regression deltas;
- unsupported-claim and boundary-violation deltas;
- Evaluator calibration/repeatability/agreement deltas;
- provider calls, estimated cost, latency, and failure-rate deltas;
- data coverage and contamination status.

The external profile declares target-specific hard gates and advisory metrics
before the experiment. Hard gates are conjunctive. An advisory improvement
cannot compensate for a failed semantic, safety, integrity, or privacy gate.

## Component promotion

Protocol `kpo.component-promotion/v1` follows the same safety shape as policy
promotion:

1. preview the exact parent/candidate version and configuration digests;
2. bind the complete replay evidence and independent review digest;
3. bind an independent recommendation from a reviewer distinct from the
   candidate owner, then require explicit digest approval from the human owner
   or a valid campaign-scoped delegation;
4. snapshot the active component pointer and configuration;
5. journal the transaction;
6. atomically advance the active component pointer;
7. make rollback recoverable and auditable.

The winning component becomes active only for later iterations. It cannot
rewrite evidence or results from the iteration that selected it.

Human-owner digest approval remains the default authority for applying content
or advancing an active pointer. An independent reviewer may recommend accept
or reject, but that recommendation alone is not `--approve`.

A delegated reviewer may authorize only an external **experimental active
pointer** when a separate owner-created delegation artifact was sealed before
the Campaign. Protocol `kpo.approval-delegation/v1` binds the owner identity,
delegate identity, Campaign and profile digests, allowed targets, maximum
promotion count, iteration range, and whether rollback is mandatory on the
first confirmed regression. It never authorizes writes to a source vault,
public repository, or production knowledge store. Delegation cannot be created
by TOML opt-in, by the candidate generator, or by the delegate itself. Applying
content outside the experimental runtime still requires the human owner's
exact diff-digest approval.

## Independence and anti-Goodhart constraints

1. Actor, Evaluator, Proposer, router, and approver identities are recorded.
2. A component cannot generate its mutation, supply its only supporting
   evidence, and approve itself.
3. Evaluator changes require an unchanged independent measurement path.
4. Proposer changes are judged by downstream evidence, not proposal fluency.
5. Router changes replay a sealed attribution set with independent expected
   dispositions before becoming active.
6. Holdout and untouched generalization case identifiers, details, and evidence
   never enter feedback records, diagnosis, routing, attribution hypotheses,
   mutation generation, component candidates, or component prompts.
7. The same failed case may motivate a candidate but may not be the sole
   evidence for its promotion.
8. Thresholds, partitions, and reward gates are immutable during one registered
   experiment.

## Campaign iteration order

One iteration executes in this order:

1. seal active policy, component versions, partitions, budgets, and gates;
2. run the train case and persist rollout/evaluation/differential evidence;
3. create feedback and route it;
4. create at most one policy or component candidate;
5. run its registered validation/replay plan;
6. independently recommend accept or reject, then obtain the exact digest
   approval from the human owner or a valid campaign-scoped delegation;
7. if that digest approval is valid, advance the corresponding active pointer
   for the next iteration;
8. append immutable iteration evidence and continue until budget or stop rule.

Policy and component candidates may both be proposed from one batch of
feedback, but they are tested in separate iterations. Their evidence and
lineage cannot be merged post hoc.

Feedback records that can reach routing or candidate generation are created
only from train cases. Validation, regression, holdout, and untouched
generalization observations are persisted as gate evidence in their existing
protocols, not converted into mutation-driving feedback.

## Stop rules

A Campaign stops or pauses mutation when:

- provider-call, cost, iteration, or wall-clock budget is exhausted;
- integrity or contamination fails;
- no attribution hypothesis clears the ambiguity margin;
- the required independent measurement path is unavailable;
- validation or holdout degrades beyond its registered gate;
- the same target produces the configured number of consecutive rejected
  candidates;
- the owner requests stop.

`collect_evidence`, `reference_review`, and `runtime_repair` are valid learning
outcomes. They may change a later experiment, but they do not silently count as
policy improvement.

## Persistence and status

Campaign state additionally records:

- active policy digest per iteration;
- active component-version digests per iteration;
- feedback, routing, candidate, replay, review, and promotion digests;
- target-specific accept/reject counts;
- aggregate feedback disposition and target trends;
- rollback ancestry.

### Active pointers and journal authority

Active pointers live under the configured external runtime home, never inside
the checkout or public profile. The append-only promotion journal is the single
source of truth. `active-policy.json` and `active-components.json` are atomic,
derived indexes over the latest committed journal transaction; they are not an
independent source of lineage.

Every pointer transaction binds the parent and candidate digests, validation
and review digests, previous pointer digest, rollback ancestor, and committed
state. The implementation first snapshots the previous index, writes a
`prepared` journal, atomically replaces the derived index, then marks the
journal `committed`. Recovery of an interrupted `prepared` transaction restores
the snapshotted index. Rollback appends a new committed journal entry pointing
to an existing ancestor; it never deletes or rewrites the failed promotion.

On load, KPO derives the expected pointers from committed journals and rejects
or recovers any mismatching index before another iteration. Existing policy
content written by `PromotionManager` remains content storage; the new journal
records which stored digest is active for an opt-in Campaign.

Public status exposes only aggregate counts and digests. It never exposes
rollouts, reference content, policy text, private component configuration,
holdout details, or feedback evidence prose.

## Backward compatibility

Profiles without `[feedback_optimization]` preserve all existing behavior and
serialized artifacts. Existing diagnosis-to-policy proposal behavior remains
unchanged. Dual-loop routing and component candidates are strictly opt-in.

## Acceptance tests

1. Missing policy knowledge routes to a policy candidate with bound evidence.
2. Runtime and reference failures cannot create policy candidates.
3. Evaluator disagreement cannot mutate policy from the disputed judgment.
4. Retrieval miss requests a replay before policy mutation.
5. Ambiguous attribution routes to `collect_evidence`.
6. Exactly one target may change in a component candidate and replay plan.
7. A component cannot approve itself.
8. Evaluator candidates require an independent measurement path.
9. Accepted policy and component candidates advance only their respective
   active pointer for the next iteration and preserve rollback ancestry.
10. Rejected candidates never become active parents.
11. Holdout evidence cannot enter routing or candidate generation.
12. Status output contains aggregate counts/digests but no private content.
13. Profiles without the opt-in section preserve previous bytes and call
   counts.
14. Repository hygiene rejects private profiles, component bytes, domain data,
   machine paths, and runtime artifacts.
15. Untouched generalization identifiers and evidence cannot enter feedback,
   routing, attribution, or any candidate surface.
16. Creation times are excluded from canonical feedback bytes and digests.
17. A rubric defect routes only to a `rubric` candidate or more evidence.

## Planned TDD sequence

Implementation follows these red-green-refactor slices:

1. profile parsing, closed target/metric validation, and byte-exact opt-out;
2. immutable feedback records, canonical digests, and train-only source checks;
3. legacy `DiagnosisKind` mapping and deterministic safety routing;
4. ambiguity, retrieval replay, reference, runtime, and evaluator guards;
5. single-target component candidates and identity/self-approval checks;
6. target-specific evidence vectors and conjunctive gates;
7. journal-authoritative pointer advance, interrupted recovery, and rollback;
8. holdout/generalization isolation and private-status hygiene;
9. end-to-end synthetic object-loop and meta-loop Campaign tests.

No production implementation for a slice is written until its tests fail for
the expected reason.

## External falsification experiment

Outside the repository, create a synthetic profile and sealed fixtures whose
correct dispositions are independently labeled before execution:

1. a missing rule supported by two train cases;
2. the same rule present but intentionally omitted by retrieval;
3. a clear retrieved rule intentionally ignored by the Actor;
4. an injected reference contradiction;
5. two Evaluators forced to disagree on the decisive claim;
6. two competing attribution hypotheses inside the ambiguity margin;
7. a candidate component attempting self-approval;
8. an accepted candidate followed by an injected next-iteration regression;
9. one holdout and one untouched-generalization canary identifier.

The experiment passes only if routing matches every sealed label, cases 2–6
produce no policy mutation, case 7 is rejected, case 8 advances then rolls back
through journaled ancestry, and neither canary appears in feedback, routing,
candidate, prompt, or status bytes. Re-running from the same sealed inputs must
produce byte-identical canonical artifacts and the same provider-call count.
