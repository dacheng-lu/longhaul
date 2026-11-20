# Preserve verification and recovery capacity

Apply when work has a real time, call or cost budget. Do not manufacture a budget
or interrupt routine work to request one. Treat these as engineering heuristics,
not a Kelly-optimal policy: agent costs are usually additive and payoffs uncertain.

1. Record the user's limits, measured remaining resources, current source of those
   measurements, and estimated costs for the next bounded attempt, recovery and
   final verification. Keep units separate. Mark unknown estimates explicitly.
2. Prefer a discriminating small experiment before increasing scope. Never use a
   model's self-reported confidence as a calibrated win probability.
3. Before each attempt, retain enough resources for verification and a realistic
   recovery route. For a chosen retry reserve, require remaining >= attempt cost
   times (1 + reserved retries) + recovery + verification, in each resource.
4. If the check fails, reduce the next experiment, use a known affordable route,
   or save a truthful incomplete handoff. Do not silently lower acceptance or
   request more budget when already-authorized independent work remains possible.
5. Refresh costs after execution and on resume. Treat concurrent workers as sharing
   one budget; serialize allocations. Shared providers, prompts and dependencies
   can fail together. More workers do not prove risk diversification.
6. Reconcile uncertain external effects before any retry, even when budget remains.

Use an optional task-local `budget.json` sidecar. Makeextreme's
`python -m makeextreme.budget PLAN` evaluates declared estimates; it neither
measures costs nor grants approval. It is optional, not a Longhaul dependency.
Store measurement provenance and estimate revisions in checkpoint decisions;
no change to checkpoint schema or automatic acceptance is introduced.

## Cases to check before adopting a policy

| Case | Expected decision |
| --- | --- |
| 30 minutes left; attempt 25; verification 10 | Choose a smaller attempt; do not start the 25-minute route. |
| 30 left; attempt 8; one reserved retry; recovery 4; verification 10 | Exactly affordable, subject to estimates and existing permissions. |
| No credible cost estimate | Run an affordable bounded probe or retain an unknown; do not report budget admission. |
| Three workers share one failing provider | Revise the shared dependency or hypothesis before spending on more copies. |
| External write may have succeeded | Reconcile first; budget does not authorize duplicate effects. |
| Conservative reserve prevents all useful work | Re-estimate with evidence and record the tradeoff; reserves are not sacred percentages. |

Compare policies on the same tasks, failure traces, acceptance criteria and total
budgets. Report completion within budget, cost, unverified outputs and actual
recovery cost separately. Retain regressions and unsuccessful cases. The current
Makeextreme paired example is synthetic and does not prove agent effectiveness.

Theory reference: [Kelly (1956), A New Interpretation of Information Rate](https://bpb-us-w2.wpmucdn.com/u.osu.edu/dist/7/36891/files/2017/07/Kelly1956-1uwz47o.pdf).
Kelly optimizes expected log growth under specified gambling assumptions; the
25% fraction applies to the fair +50%/-40% game, not arbitrary engineering work.

Source copy: Kelly (1956), Bell System Technical Journal 35(4), 917–926;
DOI 10.1002/j.1538-7305.1956.tb03809.x. The linked Ohio State-hosted PDF
reproduces the original paper with AT&T permission.
