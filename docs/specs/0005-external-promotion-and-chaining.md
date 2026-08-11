# KPO v0.5 External Promotion and Chaining Specification

- Status: **Approved by delegated independent reviewer on 2026-08-12**
- Depends on: `0001-kpo-v0.1.md`, `0002-external-profile-execution.md`,
  `0003-optimization-campaign.md`
- Scope: persisted campaign promotion bundles, explicit apply/recover, policy
  manifest transactions, and a separately started next campaign

## 1. Purpose

v0.3 and v0.4 can diagnose, propose, reject, retry, and produce a passing
promotion preview, but they stop before that preview can become the next policy
snapshot. External experiments verified multi-iteration feedback and frozen-
holdout dataset growth; they also confirmed that the current CLI has no
`promote` command and the campaign state does not persist enough candidate and
manifest material to reconstruct an apply transaction independently.

v0.5 closes that boundary without introducing automatic self-improvement. A
passing campaign persists one immutable promotion transaction bundle outside
the repository. An operator reviews both the policy-content diff and policy-
manifest diff, then supplies the exact transaction approval digest to apply it.
After apply, an ordinary new campaign reloads the profile and naturally uses
the promoted policy as its parent. No campaign chains an unapproved candidate.

## 2. Path coordinate contract

The profile directory is `profile_root`. Promotion `target_root` may equal it
or be a nested directory, but must resolve inside `profile_root` in v0.5. An
outside or symlink-escaping target is rejected during campaign-profile
validation because its content cannot be represented by the existing
profile-relative policy manifest.

Three path forms are distinct:

- `target_relative_path`: relative to `target_root`, checked by the promotion
  allowlist and used for the content-file write;
- `manifest_relative_path`: relative to `profile_root`, stored in the policy
  manifest and loadable by `load_profile`;
- resolved destination: `target_root / target_relative_path`, which must equal
  `profile_root / manifest_relative_path` after resolution.

For `add`, `target_relative_path` is
`<add_directory>/<artifact_id>.md`; `manifest_relative_path` is the resolved
destination relative to `profile_root`. For `edit`, the existing manifest path
is resolved below `profile_root`, must also be below `target_root`, and is then
converted to the corresponding target-relative path. This replaces the v0.3
assumption that one relative string could be interpreted against both roots.

The exact formulas, after resolving and containment checks, are:

```text
target_prefix = target_root.relative_to(profile_root)

add.target_relative_path = add_directory / (artifact_id + ".md")
add.manifest_relative_path = target_prefix / add.target_relative_path

edit.manifest_relative_path = existing manifest path
edit.target_relative_path =
    (profile_root / edit.manifest_relative_path).relative_to(target_root)
```

An empty `target_prefix` is omitted. Every formula is checked again by resolving
both sides and requiring
`target_root / target_relative_path == profile_root / manifest_relative_path`.

Only policy artifacts and the declared policy manifest participate in a
promotion. Reference/raw artifacts are never targets.

## 3. Persisted promotion bundle

When a campaign candidate passes all gates, KPO creates the existing content
preview and a proposed policy-manifest update, then persists a versioned bundle
under external `data_home` before transitioning to `promotion_previewed`:

```text
data_home/promotions/previews/<campaign_id>/
  bundle.json
  original-content.bin       # absent only when add destination did not exist
  reviewed-content.bin
  original-manifest.jsonl
  reviewed-manifest.jsonl
```

`bundle.json` is strict and contains at least:

- bundle schema/protocol version, campaign ID, profile ID, and profile snapshot
  digest;
- complete candidate fields and candidate digest;
- report digest, parent policy digest, and candidate policy digest;
- mutation, artifact ID, target-relative and manifest-relative paths;
- content original/new/diff digests and the reviewable content diff;
- manifest original/new/diff digests and the reviewable manifest diff;
- allowlist snapshot and promotion target digest captured by the campaign;
- the byte digests of all persisted original/reviewed files.

`approval_digest` is not a field inside `bundle.json`. It is
`canonical_digest(the strict parsed bundle object)`, avoiding a self-referential
digest. It binds the complete two-file transaction, not merely the content
diff. The operator-supplied value is recomputed from the validated stored
bundle during apply. Unknown/missing fields, a digest mismatch, an absent
reviewed file, or inconsistent candidate/report/policy digests make the bundle
invalid.

Exactly one bundle may exist for a campaign because a campaign stops on its
first passing candidate and campaign IDs are unique. Existing bundle content
for that ID is never overwritten.

If the process stops after the final bundle directory is installed but before
the campaign state reaches `promotion_previewed`, an explicit resume may
finalize that persisted preview without repeating Provider calls. This is
allowed only when the top-level state is still `running` or `failed`, the
bundle is strict and complete, and its profile snapshot, target digest,
candidate, report, paths, bytes, and diffs still validate against the unchanged
original profile. An absent or invalid bundle follows the ordinary resume
rules; KPO never overwrites or guesses around a partial bundle.

The bundle and byte artifacts are written atomically to runtime storage. They
never write `profile_root` or `target_root`. The campaign state records the
bundle path, candidate digest, candidate policy digest, report digest, and both
diffs. It does not record or display the approval digest. A passing campaign is not considered
`promotion_previewed` until bundle persistence succeeds.

## 4. Policy manifest update

The existing strict policy manifest remains JSONL with
`artifact_id`, `path`, and positive integer `version`.

- `add`: the artifact ID must not exist; append one deterministic JSON row with
  the computed `manifest_relative_path` and version `1`.
- `edit`: exactly one artifact ID must exist; keep its path unchanged and
  increment its version by exactly one.
- Other mutations remain unsupported.

Existing row order is preserved. The changed/new row uses deterministic JSON
serialization and a trailing newline. The exact resulting manifest bytes and
unified diff are persisted in the promotion bundle and covered by the approval
digest.

For edit, the candidate content replaces the artifact at its existing path.
For add, the destination must not exist. Reloading the profile from the applied
content and manifest must produce the bundle's `candidate_policy_digest`.

## 5. CLI and consequential boundary

```text
kpo promote --profile PROFILE --campaign CAMPAIGN_ID
kpo promote --profile PROFILE --campaign CAMPAIGN_ID --approve APPROVAL_DIGEST
kpo promote --profile PROFILE --campaign CAMPAIGN_ID --recover
```

`--approve` and `--recover` are mutually exclusive.

Preview mode loads and validates the stored bundle, rechecks that the profile,
manifest, and destination still match the bundle's original snapshot, and
prints stable JSON containing the campaign ID, candidate digest, candidate
policy digest, approval digest, relative paths, content diff, and manifest
diff. It does not regenerate the candidate and writes neither source nor
target files.

Apply mode requires the exact approval digest supplied out of band by the
operator. KPO, the Proposer, Evaluator, and delegated reviewer never supply or
infer this value for a real policy. Applying real/private policy remains a
human consequential action; synthetic tests may exercise the mechanism.

Recovery mode normally restores an interrupted prepared transaction. A
committed transaction cannot be rolled back through this command; reversal is
a later, separately reviewed promotion. If the journal is already committed
but the process crashed before removing `apply.lock`, recovery acts only as a
commit finalizer: it verifies that content, manifest, and reloaded policy still
match the committed bundle, marks campaign promotion state `applied` if needed,
and removes the stale lock without rewriting or rolling back either file.

## 6. Recoverable two-file transaction

Promotion apply is serialized per `data_home` by atomically creating
`data_home/promotions/apply.lock` with `os.open` flags `O_CREAT | O_EXCL`. Its
strict JSON content is campaign ID and the recomputed approval digest. A second
apply while the file exists fails without writing. The lock never expires based
on PID or time and is not automatically removed after process death; an
interrupted transaction requires explicit recovery. This conservative,
portable lock prevents another apply from entering a possibly half-written
profile. It is removed only after clean commit, a clean pre-write validation
failure, or completed recovery.

After acquiring the lock, apply revalidates:

1. bundle protocol and every persisted byte digest;
2. exact approval digest;
3. current profile snapshot and parent policy digest;
4. current manifest digest and destination original digest/existence;
5. resolved path equality, target containment, and allowlist;
6. candidate/report relationship and proposed manifest semantics.

It then writes a prepared journal under
`data_home/promotions/journals/<approval_digest>.json`, atomically replaces the
content destination first, atomically replaces the policy manifest second,
reloads the profile, and requires the resulting policy digest to equal the
candidate policy digest. Only then is the journal marked `committed`, campaign
promotion state marked `applied`, and the lock removed.

The transaction is recoverable rather than falsely described as cross-file
atomic. A crash may expose the new content before the new manifest. The
persisted preview originals and prepared journal let `--recover` restore both
the destination (or remove it for `add`) and the original manifest byte-for-
byte. Recovery also handles a lock acquired before the prepared journal was
fully written. It reads campaign ID and approval digest from the lock, requires
the CLI campaign ID to match, loads the immutable bundle directly from
`promotions/previews/<campaign_id>`, recomputes its approval digest, and restores
the bundle originals even if no journal exists. A mismatch anywhere is rejected
without guessing or expiring the lock.

A recovered prepared transaction does not introduce a third journal
transaction state. Recovery creates a `prepared` journal from the lock and
bundle if the crash happened before journal persistence, restores and verifies
the originals, writes campaign promotion state `recovered`, and removes the
lock last. The journal remains `prepared`, meaning the approved transaction was
never committed; the separate campaign promotion state records that its writes
were restored. If recovery itself stops after any restore or state-write
boundary but before lock removal, rerunning recovery repeats the original-byte
restoration idempotently and can finish clearing the lock.

If the journal is already `committed`, recovery never restores originals. It
requires the current destination and manifest to match the reviewed bundle,
requires the reloaded policy digest to equal `candidate_policy_digest`, repairs
only a missing campaign `applied` state, and removes the leftover lock. Any
mismatch leaves the lock in place for operator investigation. This covers a
crash after the commit marker but before normal lock removal without creating a
permanent lock or permitting rollback of committed data.

Typed fault injection covers interruption after the content replacement and
after the manifest replacement, plus recovery interruption after the recovered
campaign state is durable but before lock removal.

## 7. Chaining semantics

Chaining is explicit and occurs across separate commands:

1. campaign A stops at a persisted promotion preview;
2. the operator reviews both diffs;
3. an explicit approved apply updates content and manifest;
4. a separately invoked campaign B reloads the same profile.

Campaign B needs no parent-digest override or `--chain` flag. Its normal
profile load must contain the promoted artifact/version and its parent policy
digest must equal campaign A's stored candidate policy digest. The frozen
holdout lock remains unchanged.

An unapproved, interrupted, recovered, or stale promotion never becomes a new
parent. Applying a bundle twice is rejected idempotently as already committed.

## 8. Campaign and promotion state

The top-level campaign state remains the terminal `promotion_previewed` before,
during, and after an external promotion transaction. v0.5 does not add campaign
transitions or make that campaign resumable.

Promotion state is a separate `promotion_state` field in the campaign state
file and promotion journal:

| Promotion state | Meaning |
| --- | --- |
| `previewed` | Immutable bundle persisted; no profile/target write occurred. |
| `applying` | Exclusive lock acquired and apply may be between write boundaries. |
| `applied` | Both files verified, journal committed, lock removed. |
| `recovered` | Interrupted writes restored byte-for-byte, lock removed. |

Campaign creation writes `previewed`; apply writes `applying` before the first
content write and `applied` only after reload verification and journal commit;
recovery writes `recovered` after restoration. The journal remains the authority
for `prepared` versus `committed` transaction status. If a crash occurs before
campaign state is updated, the persistent lock/journal still blocks further
apply until recovery.

`campaign-status` reports the top-level campaign state, promotion state,
candidate/bundle presence, and the two reviewed diffs. It does not return the
approval digest. Only the explicit `promote` preview command displays the
approval digest next to both diffs.

## 9. Information boundaries

The Proposer request remains unchanged: it never receives approval digests,
promotion bundle paths, manifest diffs, apply state, or recovery evidence.

All promotion bundles, snapshots, locks, and journals stay under external
`data_home` and remain excluded from repository publication.

## 10. TDD sequence

1. Path-mapping tests for root and nested target roots, edit/add, containment,
   symlink escape, and v0.3 coordinate-regression cases.
2. Manifest proposal tests for deterministic add and version-increment edit.
3. Failing campaign test proving a passing result persists a complete,
   digest-valid bundle before `promotion_previewed`.
   Also interrupt after bundle installation and prove resume finalizes the
   preview without another Provider call.
4. CLI preview tests proving stable reconstruction and zero source/target
   writes.
5. Wrong approval, stale manifest, stale destination, invalid bundle, and
   double-apply rejection tests.
6. Add and edit apply tests proving both files update and reloaded policy digest
   equals `candidate_policy_digest`.
7. Fault-after-content and fault-after-manifest recovery tests proving exact
   restoration, including add-destination removal.
8. Concurrent-lock and lock-without-journal recovery tests.
   Also cover committed-journal lock finalization without file rewrites and a
   committed-byte mismatch that leaves the lock in place. Interrupt prepared
   recovery after state persistence and prove an idempotent retry clears the
   lock.
9. Synthetic chain test: campaign A preview -> approved apply -> ordinary
   campaign B starts from the promoted policy without an override.
10. Full legacy suite, build, hygiene, privacy, and external synthetic chaining
    experiment.

## 11. Acceptance criteria

1. A passing campaign persists a strict, self-verifying promotion bundle and
   reviewed bytes before reporting `promotion_previewed`; interruption between
   those writes can be explicitly resumed without new Provider calls.
2. Preview CLI is deterministic and non-mutating.
3. Approval binds candidate, report, policy digests, both paths, both original
   and new file digests, and both diffs.
4. Wrong, stale, corrupted, unsupported, escaped, or concurrent transactions
   write nothing and return a typed failure.
5. Add and edit update content plus policy manifest through a journaled,
   recoverable transaction.
6. Reloaded profile policy digest equals the stored candidate policy digest.
7. Recovery restores both files byte-for-byte and removes an added destination
   when necessary; committed-journal finalization only verifies committed bytes
   and clears the leftover lock. Recovery itself is retryable at every boundary
   before lock removal.
8. An ordinary next campaign uses the promoted policy; no override or automatic
   chaining exists.
9. Holdout lock and dataset are unchanged by preview, apply, and recovery.
10. Proposer/Actor information boundaries and source immutability outside the
    explicit apply/recover command remain unchanged.
11. Real promotion still requires a human-provided approval digest; delegated
    review approval alone cannot trigger it.
12. No real profile, policy content, runtime bundle, credential, or local path
    enters the repository or CI artifacts.

## 12. Non-goals

- No automatic promotion or unattended continuous optimization.
- No remote Git commit, push, publication, or deployment of a promoted policy.
- No metadata-assisted `trusted_fast` integrity mode.
- No new quality thresholds or human target-quality decision.
- No rollback of a committed promotion through recovery.
- No mutations beyond `add` and `edit`.

## 13. Decisions for reviewer

The delegated reviewer must decide:

1. whether campaign ID is the correct external lookup key instead of candidate
   ID;
2. whether one approval digest correctly binds the content and manifest
   transaction;
3. whether the profile-root/target-root mapping is complete and safe;
4. whether content-first then manifest is the least unsafe recoverable order;
5. whether explicit new-campaign reload is sufficient chaining;
6. whether lock/crash/recovery semantics cover every write boundary;
7. whether any persisted field could leak holdout or provider secrets.
