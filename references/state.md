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
| `migrate PATH [--write]` | v1 → v2; re-binds evidence only while the content hash still matches |

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
  sha256 with `revision == sha256[:12]`. `supporting: true` marks an input that is
  not a deliverable and exempts it from coverage.
- `evidence[]`: id, kind, locator, `artifact_ids[]`, `artifact_revisions{id: rev}`,
  `observed_at` (ISO-8601 with offset, not in the future), `result`
  (pass/fail/unknown). `unknown` needs `unknown_reason`; external evidence needs
  `summary`.
- `acceptance[]`: id, criterion, status (pending/verified/failed/unobservable),
  `evidence_ids[]`.
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
unknown observation standing behind a verified criterion; evidence older than its
artifact's revision; a missing file or hash mismatch; an artifact no evidence binds;
an unchanged retry (same action and error) or three attempts on one blocker without a
hypothesis change; a bundled `next_action`; a future or timezone-less timestamp; a
shortcut with no upgrade trigger; a `callable` capability that was never observed
working; a credential-shaped value anywhere in the document; unresolved blockers; a
`blocked` status; and an empty record on a delivery gate.

It never echoes a matched secret value — it names the location so you can delete it.

## What it cannot do

A passed gate is bookkeeping. It does not prove the artifact is correct, that a remote
system exists, or that a worker told the truth. Those remain the reviewer's job.

## Safe edits and migration

For whole-file updates write a sibling temporary file and replace atomically; the CLI
does this for `artifact`, `evidence` and `migrate --write`. Keep task state outside the
installed skill directory and persist it through the host's durable storage workflow.

`migrate` reads v1, maps `deliverables` to `artifacts[].A1..An`, and binds each piece
of evidence to the artifact with the same locator. Evidence is re-bound to the new
content-addressed revision **only if** the file still hashes to the sha256 v1 recorded;
otherwise it keeps the old label, is reported stale, and must be re-verified. Migration
therefore cannot launder evidence for a file that changed.

A v1 file stays readable: `validate` and `gate` upgrade it in memory and print a warning
telling you to run `migrate`. That compatibility path is removed in v1.2.

On recovery inspect actual artifacts, not only records. Mark complete only after the gate
and the domain-specific checks pass; otherwise provide an in-progress or blocked handoff.
