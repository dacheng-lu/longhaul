# Working with Makeextreme: demonstrated handoff and candidate contract

Status: **manual local demonstration; candidate contract, not an implemented
adapter or a security boundary.** Both projects remain usable independently.

[Run the crash and evidence example](https://github.com/dacheng-lu/makeextreme/tree/main/examples/longhaul_recovery).
On a Linux host it interrupts a local three-stage pipeline, resumes it, verifies
the resulting report content, records a Longhaul observation, changes the report
and confirms that the old evidence is stale. The example uses temporary files;
it does not demonstrate durable cross-session storage or an external action.

The CI example pins Longhaul commit
`22e9a19c99e9b9daf05a88a0efae05b565d6469a`. Check current compatibility
before relying on another version. Makeextreme is not a Longhaul dependency.

## What is actually exchanged in the example

1. Makeextreme writes its own stage state, journal and report. The example reads
   the journal and report directly, and checks the expected stage start counts
   and exact report content.
2. Only after that check, the example registers the report with the Longhaul
   helper and records a `pass` observation with an acceptance criterion.
3. After changing and re-registering the report, it confirms the gate rejects
   the stale observation; it checks the new content and records it again.

There is no automatic conversion of Makeextreme state to Longhaul evidence,
no signed receipt, and no proof that the operator is independent of the producer.
A green gate checks bookkeeping and local hashes, not semantic truth.

## Candidate exchange requirements

The following fields describe requirements to evaluate, not a JSON schema or
existing API. An adapter should be specified only after negative cases and a
real integration justify it.

| Field | Purpose |
| --- | --- |
| Contract version and task/run/stage IDs | Identify the format and the exact execution being discussed. |
| Producer and component revisions | Record which runner, stage definition and checker supplied the observation. |
| Artifact locator, kind and revision | Identify the checked artifact and its content revision; define hashing and path semantics explicitly. |
| Check and observed outcome | Identify what was actually checked, when, and whether it passed, failed or was unknown. |
| Evidence locator and limitations | Point to the underlying record and state what it cannot establish. |
| External-effect and approval state | Preserve uncertainty; an approval label alone does not prove identity or that an external action occurred. |

A consumer must check identity, version and freshness before relying on an
observation. A missing artifact, stale revision, unknown result or incompatible
contract must not become a `pass`. A stage result alone must not complete a
whole-task acceptance criterion. When an external action may already have
happened, reconcile its actual state before retrying.

Task and stage records are inputs, not trusted instructions. Hashes bind data to
a version; they do not prove that a check happened, that the producer told the
truth, or that a result is correct. Authentication, sandboxing, durable storage
and authoritative external observations remain host responsibilities.

## Next validation

Before treating this as an adapter, exercise failure, interruption, changed or
missing output, forged or incomplete records, and uncertain external effects;
record actual versions and costs. Validate the hash/path rules on both sides and
keep the demonstration repeatable without requiring either project as a runtime
dependency of the other.
