# Checkpoint contract (schema v2)

The helper executes no stored commands and makes no network requests. It checks
bookkeeping and local file hashes. It cannot decide whether the work is correct.

| Command | Behaviour |
|---|---|
| `init PATH --task-id ID --goal TEXT` | create a v2 template, exclusively; refuse overwrite |
| `artifact PATH --id A1 --locator PATH [--supporting]` | register or refresh an output; records sha256 and `revision = sha256[:12]` |
| `artifact PATH --id A1 --kind external --locator URL --revision LABEL` | register an external artifact you can point at |
| `evidence PATH --id E1 --artifact A1 [--artifact A2] --result pass\|fail\|unknown` | append an observation bound to the artifacts' current revisions |
| `hash PATH` | print sha256 and the 12-hex revision |
| `validate PATH` | structure only |
| `gate PATH` | pre-delivery: every criterion verified or waived, no unresolved blocker |
| `migrate PATH [--write]` | v1 → v2 under the three-way revision contract (below) |

## Revision means content

`revision` is derived from the file, not typed by a human. Registering an artifact
again after editing it produces a new revision, and every piece of evidence bound
to the old one is reported **stale** — but only the criteria that cite that evidence
are affected. That is the point of v2: one changed deliverable no longer invalidates
the whole record, and nobody is tempted to bump a label instead of re-running a check.

External artifacts have no content hash, so their `revision` is a label you supply
(commit SHA, release tag, observation date). Its authority is exactly the credibility
of that label.

## Required entries

- Top level: `schema_version: 2`, `task_id`, `goal`, `next_action` (one step),
  `status` (in_progress / blocked / complete), `provenance{generated_at, writer}`.
- `artifacts[]`: id, kind (local/external), locator, revision; local also needs
  sha256 with `revision == sha256[:12]`. `supporting` must be a boolean
  (`true`/`false`); `true` marks an input that is not a deliverable and exempts it
  from coverage.
- `evidence[]`: id, kind, locator, `artifact_ids[]`, `artifact_revisions{id: rev}`,
  `observed_at` (ISO-8601 with offset, not in the future), `result`
  (pass/fail/unknown). Optional `run{case_id, model, input_sha256}` records which
  run produced the observation; it is bookkeeping, not authentication. `unknown`
  needs `unknown_reason`; external evidence needs `summary`. Optional:
  `unverified: true` with `unverified_reason`, and
  `source_revision` (only together with `unverified: true`).
- `acceptance[]`: id, criterion, status (pending/verified/failed/unobservable),
  `evidence_ids[]`. Optional `run{case_id, model, input_sha256}`: a criterion that
  carries it is satisfied only by evidence carrying the identical object.
- `waivers[]`: criterion_id, reason, approved_by, approved_at. Required to complete
  over an `unobservable` criterion; silence is not consent.
- `attempts[]`: `blocker_key`, `action`, `error`, `hypothesis_before`,
  `hypothesis_after`, `at`.
- `shortcuts[]`: what, ceiling, upgrade_trigger.
- `capabilities[]`: name, class (callable / installed_unavailable / discovered /
  unavailable), purpose, permission, source, observed_at, last_success, fallback.
- Free lists, shape not enforced: `constraints`, `milestones`, `decisions`,
  `lessons`, `blockers`.

## What the gate rejects

A criterion that is not verified; a verified criterion without evidence; a failed or
unknown observation standing behind a verified criterion; an evidence entry marked
`unverified` or bound to an older artifact revision; a criterion whose `run` binding
does not match the cited evidence's `run`; a `revision: ""`; a non-boolean
`supporting`; a `source_revision` without `unverified: true`; a missing file or hash
mismatch; an artifact no evidence binds;
an unchanged retry (same action and error) or three attempts on one blocker without a
hypothesis change; a bundled `next_action`; a future or timezone-less timestamp; a
shortcut with no upgrade trigger; a `callable` capability that was never observed
working; a credential-shaped value anywhere in the document (or in the mandate file);
a `report` whose state is not one of the six or whose reason is empty; `status: complete`
standing over `blocked` or `failed`; a mandate file that is unreadable, incomplete, or
missing its `policy_version`; unresolved blockers; a
`blocked` status; and an empty record on a delivery gate.

It never echoes a matched secret value — it names the location so you can delete it.

## What it cannot do

A passed gate is bookkeeping. It does not prove the artifact is correct, that a remote
system exists, or that a worker told the truth. Those remain the reviewer's job.

## Safe edits and migration

For whole-file updates write a sibling temporary file and replace atomically; the CLI
does this for `artifact`, `evidence` and `migrate --write`. The temporary file keeps its
own descriptor, so the original mode and owner are applied with `fchmod`/`fchown` on
that descriptor (never by name), and its inode is confirmed through the directory
descriptor the CLI walked. The write is refused if the target changed up to a second
byte and `(st_dev, st_ino)` comparison made immediately before the atomic replace. That
is the exact promise and its limit: there is no serialization against a writer that acts
after that second check, no cross-process lock and no CAS protocol.

Keep task state outside the installed skill directory and persist it through the host's
durable storage workflow.

### The three-way revision contract (v1 → v2)

`migrate` reads v1, maps `deliverables` to `artifacts[].A1..An`, and binds each piece of
evidence to the artifact with the same locator. Three revisions decide whether an
observation is trusted, and none of them has precedence:

    G = state.artifact_revision      the record's global revision
    E = evidence.artifact_revision   the revision the observation names
    A = deliverable.revision         an external deliverable's own label

- **External:** trusted exactly when G, E and A are non-empty and `G == E == A`. A v1
  external deliverable may omit `revision`; v1 carries one global revision, so
  `A := G`. An explicit `revision` stays authoritative and `revision: ""` is an error.
  A deliverable with neither its own revision nor a non-empty G is refused, and the
  source file stays byte for byte unchanged.
- **Local:** trusted exactly when G and E are non-empty, equal, and the content read
  through the same no-follow descriptor still hashes to the v1 `sha256`; the new
  revision is that hash's 12-hex prefix.
- **Any conflict:** every binding of that observation becomes `""` (even one that
  matched), `unverified: true` and a deterministic `unverified_reason` are written, the
  v1 label is kept as `source_revision`, and a warning is printed. The gate refuses
  unverified evidence whatever its binding says, and a conflict is never migrated
  silently.

`unverified` is optional; its absence means the observation is verified. When present it
must be exactly `true` and requires `unverified_reason`; `false` is invalid.
`source_revision` is only valid together with `unverified: true`.

Unknown top-level, deliverable and evidence fields are preserved with their exact key,
type and value, including falsey values and a boolean `supporting`. Only these mappings
change: v1-local `sha256 → sha256` plus the computed `revision`; v1-external
`revision → revision` (or `A := G` when absent); v1-evidence
`artifact_revision → source_revision`; binding fields are re-derived. A non-boolean or
otherwise colliding value is refused before any write.

### Strict input and paths

Hand-edited documents are read as strict JSON: a repeated object key, `NaN`,
`Infinity`/`-Infinity`, a number that does not fit a finite float, a non-zero literal
that underflows to `0.0` in binary64 (for example `1e-400`), and a string containing a
lone surrogate (which could never be written back as UTF-8) are refused, and every write
serializes with `allow_nan=False`. A refusal leaves the source bytes untouched.

Every other number is stored as the nearest IEEE-754 binary64 value: `1e5` becomes
`100000.0`, and digits beyond binary64 precision are lost (a 40-digit integer keeps its
exact value because integers are not binary64). That is the one place where the "same
type and value" field contract normalises instead of copying, and it is why a literal
that would collapse to `0.0` is refused rather than silently changed.

## Optional additions (outside the v2 schema)

`report` answers *why* the unit stopped in a word a machine can sort
(`progress | waiting | ready | blocked | failed | dropped`); `blockers` keeps the prose a
human reads. A record without it validates exactly as before, and the gate refuses
`status: complete` over `blocked` or `failed`. See [report-states.md](report-states.md).

`<record-stem>.mandate.json` answers *why the work may exist and how far it may go*: who
asked, on which channel, under which policy version, with which scope and strength. Its own
file because a mandate never changes while the record is rewritten on every step. Optional
for every existing task; when present it must be complete, credential-free and revalidatable.
See [mandate.md](mandate.md).

Every path the CLI reads or writes is walked one component at a time through a directory
descriptor with `O_NOFOLLOW`; the file is opened `O_NONBLOCK` and must be a regular file
(`fstat`/`S_ISREG`). A symlink component, a symlinked final file, a FIFO or any other
special file is refused as JSON within the same call. A platform without
descriptor-anchored traversal refuses every path operation — there is no `realpath()`
fallback — which is why v1.2 supports Linux only.

A v1 file stays readable: `validate` and `gate` upgrade it in memory and print a warning
telling you to run `migrate`. That compatibility path is removed in v1.2.

On recovery inspect actual artifacts, not only records. Mark complete only after the gate
and the domain-specific checks pass; otherwise provide an in-progress or blocked handoff.
