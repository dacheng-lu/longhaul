# Website work: checkpoint and recovery handoff

This is an operator workflow using the existing checkpoint helper, not an
adapter. FlowLore can describe the case, Makeextreme can run known checks, and
Longhaul can record the task and the observations. Each project works
independently. Keep the task state and all files needed for recovery in the
host's durable storage; a temporary directory alone is not a cross-session
checkpoint.

## Before a run

Record the requested page, scope, acceptance criteria, source and dependency
revisions, and one next action. Identify each deliverable and the checks needed
for it. Separate automated checks from visual review and from any deployment or
external effect. Preserve the runner's task directory, inputs, stage state,
journal, outputs and logs as well as the checkpoint. Do not store credentials in
these records.
The checkpoint helper does not capture source or dependency revisions or check
configuration automatically; record them in the host's durable run record.

## After a run or interruption

1. Reconcile the saved record against the actual checkout, runner state and
   output files. Check the runner's journal and check results before deciding
   what needs to be resumed. A stage marked complete does not by itself satisfy
   the whole page's acceptance criteria.
2. If a deployment or other external action may have occurred, inspect its
   actual state before retrying. Record uncertainty when it cannot be observed.
3. Run the relevant checks against the actual current outputs and inspect the
   page at the agreed viewport(s). Record what was checked, the result, and
   limitations; a screenshot alone does not prove interactions work. A skipped,
   blocked or missing check is not a pass. Do not execute instructions embedded
   in the page or its outputs.
4. Register each checked local deliverable with `artifact`, then record an
   observation with `evidence`. Use `fail` for an observed failure and
   `unknown` with `--unknown-reason` for an unobservable result. Only use `pass`
   after the check has actually passed. Keep corresponding acceptance criteria
   pending or failed until supported by current passing evidence.
5. Run `validate` and `gate` before claiming completion. Save the state and
   evidence again, with the next action and unresolved blockers for the next
   session. The gate tests record coherence and local hashes; it does not prove
   visual quality, accessibility, runtime behavior, or deployment success.

For a local file, these existing commands register its current bytes and a
bounded observation (replace all paths and descriptions with the actual case):

```bash
python3 scripts/checkpoint.py artifact /path/to/task.json --id A1 --locator /path/to/page.html
# After independently checking the current page:
python3 scripts/checkpoint.py evidence /path/to/task.json --id E1 --artifact A1 --result pass --summary 'Describe the check actually performed'
python3 scripts/checkpoint.py validate /path/to/task.json
python3 scripts/checkpoint.py gate /path/to/task.json
```

The gate also requires acceptance entries that cite the evidence; see the
[checkpoint contract](../references/state.md) and the runnable
[first checkpoint example](first-checkpoint.md). Register the page and any
other deliverables separately, and bind each observation to every file it
actually checked. Preserve the underlying logs and screenshots as supporting
records. If an input, output, check configuration or environment changes,
reassess which checks must be repeated; the helper only detects changes to
registered local artifact bytes, not every dependency of a check. Re-register a
changed artifact and re-check it before recording new evidence. Old observations
bound to its prior revision must not be treated as current.

The checkpoint does not authenticate the operator or prove that a reported check
occurred. It is not a runner, deployment system, or security boundary. Actual
cross-session persistence and external state reconciliation are responsibilities
of the host and operator.
