# KPO Project Instructions

## Project identity

KPO stands for **Knowledge Policy Optimization**. It is a domain-neutral
framework for improving the external knowledge policy used by an Agent without
changing the Agent model's weights.

The repository must remain reusable across domains. Do not hard-code a real
person, organization, investment thesis, proprietary workflow, or other
domain-specific subject into package names, types, commands, prompts, tests, or
default configuration.

## Repository boundary

This GitHub repository may contain:

- generic framework code;
- architecture and behavior specifications;
- schemas and provider interfaces;
- synthetic fixtures and deterministic tests;
- generic documentation and examples.

It must not contain:

- real policy profiles or their identifiers;
- private or copyrighted source corpora;
- copied or derived domain notes;
- real rollout transcripts, reference answers, scores, evidence logs, or
  candidate patches;
- absolute paths from a user's machine;
- credentials, tokens, cookies, or provider secrets.

Real profiles and runtime artifacts live outside the repository. The framework
accepts their locations through explicit configuration. Tests use synthetic
profiles only.

Before staging or committing, inspect tracked content for domain leakage,
private paths, runtime artifacts, and secrets. A local external denylist may be
used by a pre-commit check, but its entries must not be committed to this
repository.

## Knowledge-store safety

- Treat every external knowledge store as read-only by default.
- Ordinary run, evaluate, diagnose, propose, and regress operations must never
  write to a source knowledge store.
- A promotion operation must begin as a dry-run and produce a reviewable diff.
- Applying a promotion requires an explicit approval bound to the candidate
  digest.
- Promotion writes are restricted to profile-defined allowlisted paths.
- Before an applied promotion, create a recoverable snapshot and record a
  journal entry.
- Raw source material is never a promotion target.
- The component proposing a patch cannot approve its promotion.

## Development workflow

Use SDD followed by TDD:

1. Write or update a behavior specification in `docs/specs/`.
2. Obtain user approval for behavior-changing specifications.
3. Write a failing test that demonstrates the approved behavior.
4. Implement the minimum code needed to pass.
5. Refactor while keeping the suite green.

Do not write implementation code for an unapproved new component. Python is the
initial implementation language, but public contracts must remain language- and
provider-neutral.

## Git and publication

The remote is `https://github.com/wu8685/kpo`.

- Do not push routine intermediate work automatically.
- Key, reviewed milestones may be pushed to `main` after relevant tests and
  repository hygiene checks pass.
- For non-bootstrap feature development, use an Issue and a reviewable branch
  or pull request unless the user explicitly requests a direct `main` update.
- Never publish real domain assets as part of a commit, branch, pull request,
  release, or CI artifact.

## Documentation style

Explain KPO in terms of knowledge policies, feedback, evidence, evaluation, and
controlled promotion. Keep examples synthetic. Distinguish imitation fidelity
from factual validity and do not imply that matching a reference guarantees
truth.
