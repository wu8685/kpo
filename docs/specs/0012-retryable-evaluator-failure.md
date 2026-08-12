# 0012 — Retryable evaluator failure

## Status

Approved by the delegated read-only governor for tests-first implementation.

## Problem

An Actor rollout can be complete and durable even when its later Evaluator
provider call fails. Treating both failures as the terminal `failed` state
destroys the distinction between generation failure and evaluation failure and
prevents an operator from retrying evaluation without regenerating the Actor
answer.

## Required behavior

1. `evaluation_failed` is a persisted run state distinct from terminal
   generation or integrity failure.
2. An Evaluator `ProviderFailure` for a run with a durable rollout changes a
   `completed` run to `evaluation_failed`. The rollout and source-digest
   artifacts remain unchanged.
3. KPO does not retry automatically. A later explicit `evaluate` invocation may
   evaluate either a `completed` or `evaluation_failed` run with the same
   profile, references, rubric, and Evaluator configuration.
4. If another Evaluator provider attempt fails while the run is already
   `evaluation_failed`, the state remains `evaluation_failed` and the provider
   error is returned.
5. A successful retry changes `evaluation_failed` to `evaluated` and persists
   exactly one current evaluation artifact through the existing role link.
6. An `IntegrityViolation` during evaluation remains terminal and changes
   either `completed` or `evaluation_failed` to `failed`.
7. Evaluation and retry revalidate source digests, profile digest, policy digest, case ID,
   rollout digest, and configured command executable integrity before invoking
   the Evaluator. Drift or mismatch fails without a provider call.
8. Legacy `failed` runs remain terminal even when a rollout exists. Older
   runtime data did not distinguish Evaluator provider failures from integrity
   failures, so automatic recovery would be unsafe.
9. `status` reports `evaluation_failed` without exposing rollout content or
   provider configuration secrets.

## State transitions

- `completed -> evaluated`
- `completed -> evaluation_failed` on Evaluator provider failure
- `evaluation_failed -> evaluated` on a successful explicit retry
- `evaluation_failed -> evaluation_failed` on another provider failure
- `completed | evaluation_failed -> failed` on integrity failure

The `failed` state remains terminal.

## Acceptance tests

1. A synthetic Evaluator fails once; the run becomes `evaluation_failed`, the
   rollout digest is unchanged, and no evaluation artifact exists.
2. Replacing that synthetic provider response with a valid response and
   explicitly invoking `evaluate` succeeds without invoking the Actor again.
3. A second Evaluator failure leaves the state `evaluation_failed`.
4. A legacy `failed` run remains terminal even if it has a rollout artifact.
5. Source/profile/policy/case/rollout drift is rejected before provider
   invocation.
6. The full existing suite and repository hygiene checks remain green.
