# KPO v0.4 Exact Integrity Deduplication Specification

- Status: **Approved by delegated independent reviewer on 2026-08-12**
- Depends on: `0002-external-profile-execution.md`,
  `0003-optimization-campaign.md`
- Scope: byte-exact integrity verification efficiency only

## 1. Purpose

v0.3 verifies captured source files and the complete promotion target tree
before and after every non-cached provider call. This remains the default and
is not weakened in v0.4.

An external synthetic experiment completed campaigns at 4, 16, 64, and 256
cases with all safety checks passing. At 256 cases, 897 provider calls took
85.951 seconds; a diagnostic-only control with integrity verification disabled
took 14.551 seconds. The unsafe control is evidence about cost, not a product
mode.

The first optimization is deliberately narrow: eliminate duplicate byte
hashing when the same physical file is represented more than once in a single
verification pass. No metadata shortcut, reduced cadence, or hard sandbox is
introduced.

## 2. Preserved guarantees

Every `IntegrityMonitor.verify()` invocation still:

1. enumerates the current target-tree membership;
2. reads and SHA-256 hashes the bytes of every distinct monitored file that
   currently exists;
3. detects additions, removals, and content changes;
4. raises `source_drift` before `target_drift` when one physical change belongs
   to both classifications;
5. for campaigns, runs immediately before and after every non-cached Actor,
   Evaluator, or Proposer subprocess call, preserving the v0.3 campaign
   contract. The v0.2 single-case `run` and `evaluate` paths retain their
   existing immediate post-call checks.

Changing bytes to different bytes of the same length and restoring the original
mtime must still be detected. `st_mtime`, `st_ctime`, inode, and size may be
recorded for diagnostics but may not suppress byte hashing in the default mode.
The v0.4 implementation exposes no non-default mode. A metadata-assisted mode
requires a separate specification as described in section 7.

## 3. Unified verification algorithm

At construction, the monitor retains:

- the existing resolved source-path to expected-digest mapping;
- the existing target snapshot as target-relative path to expected digest;
- the source root, target root, and excluded roots.

For each `verify()` call:

1. Enumerate current target files exactly once as
   `target-relative path -> resolved physical path`, applying the existing
   exclusion rules.
2. Form the union of resolved source paths and current resolved target paths.
3. Compute `_digest(path)` exactly once for every distinct path in that union.
   `_digest` must use SHA-256 byte hashing as defined in v0.2 and must not
   suppress hashing based on `st_mtime`, `st_size`, `st_ino`, or other
   filesystem metadata. A path that is missing returns the existing missing
   value and is not read.
4. Reconstruct current source results from the shared digest map and compare
   them with the captured source digests.
5. If source drift exists, raise `IntegrityViolation("source_drift", paths)`
   with the same path naming and ordering as v0.3, and do not check target
   drift. This preserves the v0.3 short-circuit behavior where only the first
   detected violation is raised.
6. Reconstruct the current target-relative snapshot from the shared digest map.
   For each target-relative path enumerated in step 1, look up the digest of its
   resolved physical path. Convert `None` to the string `"missing"` to match the
   v0.3 target sentinel, then compare the result with the captured target
   snapshot.
7. If target drift exists, raise
   `IntegrityViolation("target_drift", paths)` with the same relative names and
   ordering as v0.3.

Multiple target-relative paths may resolve to the same physical file through
symlinks or aliases. Each relative entry remains present in the target snapshot,
but its physical bytes are hashed only once per verification pass. A source
path that is also inside the target tree is likewise hashed once and reused for
both comparisons.

`target_digest` remains byte-for-byte compatible with v0.3 for the same target
snapshot. No persisted campaign schema, cache key, profile field, CLI command,
or status field changes.

## 4. Failure and compatibility rules

- Source-drift precedence is mandatory even when a modified source is also
  represented in the target tree.
- Existing excluded-root behavior remains unchanged.
- Target file additions and deletions must still be detected from membership,
  independently of content hashing.
- Missing captured source files remain `source_drift`.
- Provider cache hits still do not invoke subprocesses and do not add new
  integrity checks.
- Existing campaign resume and target-digest comparisons remain compatible.

## 5. TDD sequence

1. Add a failing unit test that instruments `_digest` and proves a source file
   also present in the target tree is hashed once, not twice, per `verify()`.
2. Add a failing test for two target-relative aliases resolving to one physical
   file; both target entries remain represented while bytes are hashed once.
3. Add a failing test where a monitored source file is also present inside the
   target tree: confirm it is hashed once per `verify()`, and its modification
   produces `source_drift` with source-relative naming.
4. Add a same-size content-swap test that writes different bytes of identical
   length to a monitored source, restores the original `st_mtime` with
   `os.utime()`, and asserts `verify()` raises `source_drift`. This proves that
   neither length nor mtime suppresses byte hashing.
5. Preserve existing source/target drift, provider isolation, campaign, cache,
   and promotion tests.
6. Implement the smallest unified-capture refactor that passes these tests.
7. Run the full suite, build, hygiene, compile, and diff checks.
8. Re-run the external 4/16/64/256 command-provider scaling experiment and
   record wall time and calls per second. Performance is evidence, not a release
   gate for this exact-safety slice.

## 6. Acceptance criteria

1. Every distinct resolved monitored path is byte-hashed at most once in one
   `verify()` invocation.
2. Every currently existing distinct monitored path is still byte-hashed in
   every `verify()` invocation.
3. Source and target drift outcomes and changed-path ordering match v0.3.
4. Same-size, restored-mtime content changes are detected.
5. Target additions, removals, aliases, and excluded roots retain their v0.3
   semantics.
6. The full existing and new test suite passes.
7. All four scaling sizes retain the 11 previously approved experiment checks.
8. The measured 256-case result is reported without claiming that exact
   deduplication solves the asymptotic cost.
9. When a source file is also present in the target tree, modifying it raises
   `source_drift`, not `target_drift`, and the changed path uses the source
   naming convention.

## 7. Non-goals and deferred work

- No metadata-assisted `trusted_fast` mode.
- No reduction from per-call to per-stage integrity checks.
- No container or read-only-mount implementation.
- No provider batching or concurrency.
- No quality-gate, dataset, Proposer, promotion, or knowledge-policy behavior
  change.

A future metadata-assisted mode requires a separate specification. It must be
non-default, report its residual risk and platform behavior, and never be
described as byte-exact.

## 8. Decisions for reviewer

The delegated reviewer must decide:

1. whether resolved physical paths are the correct deduplication identity;
2. whether source-drift precedence fully preserves v0.3 observable behavior;
3. whether symlink aliases and target membership remain sufficiently explicit;
4. whether performance should remain measurement-only for this slice;
5. whether any compatibility case is missing before TDD begins.
