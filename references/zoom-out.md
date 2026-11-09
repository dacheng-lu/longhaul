# Bounded zoom-out: from request to a testable opportunity

Use when the user asks to zoom out, find the bigger goal, think bigger, find a
non-obvious route, 整点新鲜的, 跳出局限, or when two attempts merely polish the same
framing. Skip routine fixes, mechanical edits and already-settled decisions.
If the user explicitly wants ideation for creative work, use it; do not impose it
on ordinary taste work. This is an optional decision protocol, not a new runtime.

## 1. Preserve the request; raise the outcome

Keep the original deliverable and authorization visible. Write three concrete levels:

- **Task:** the requested artifact or action.
- **Outcome:** whose situation improves, how, and relative to what alternative.
- **Standing system:** what could sustain that outcome, with an owner and feedback.

Name the affected people, decision owner, main tradeoff and one question whose
answer would change the direction. Treat the higher levels as hypotheses until
supported. A larger goal does not authorize more access, spending, data collection,
deployment, continuous monitoring or a change to the user's success criteria.
Honor authorization already present; do not create a second approval round.

## 2. Inventory the means, not just the ambition

Inspect available artifacts and record `have / partial / missing` plus evidence for:
signals, signal-to-source mapping, safe reproduction, independent verification,
and permission. Add the owner of each material missing item. Keep a documented
capability distinct from a callable, tested one (see [capabilities.md](capabilities.md)).
An inaccessible repository is unknown; do not infer its API from its name.

## 3. Change the frame once

Generate before selecting. Pick **one** move that fits the obstacle:

| Obstacle | Move | Required observable output |
| --- | --- | --- |
| Optimizing the requested feature misses the outcome | Reframe | Change beneficiary, verb, unit of value or time horizon |
| More features leave the bottleneck intact | Eliminate / invert | Name what disappears, and what must replace its function |
| Two tools dominate the imagination | Family / limit | Name their shared function; examine an endpoint with only one or neither |
| Domain vocabulary hides alternatives | Structural analogy | Map functional roles and relations to a named mechanism elsewhere; state where the mapping breaks |
| Every idea violates a constraint | Constraint play | Relax one constraint temporarily; restore it before recommending action |

Produce up to three materially different frames (one decisive frame can suffice).
For each, state the changed assumption and causal mechanism. Ask what it stops us
building. Merely adding another sensor, model or dashboard is not a new frame.
Do not cycle through all five moves or generate a long idea list. If the move
fails, allow one re-diagnosis, then return to the authorized task with the uncertainty.
Default budget: one short decision brief, one generation pass and one selection pass.

## 4. Select an opportunity, not an adjective

Compare against the current alternative, including doing less or stopping. Assess:
user value, bottleneck removed, unique available assets, setup/support burden,
second-order effects, evidence strength, reversibility and the cost of being wrong.
Distinguish a technical demonstration, field reliability and buyer demand.

Treat “10x” as a proposed measurable outcome, never a promised multiplier. If a
multiplier is useful, name its metric, baseline, denominator and quality constraints.
Do not turn model confidence, novelty ratings or tool count into calibrated success
probabilities. Report unknowns; label any subjective estimate and its assumptions.
Novelty is useful only when its mechanism serves the outcome.

Use [grill.md](grill.md) for consequential user-owned choices and
[value-validation.md](value-validation.md) for evidence about value. Choose a
reversible default inside existing authorization instead of blocking on preferences.

## 5. Zoom back in: one bounded experiment

Record one selected hypothesis with a baseline, smallest discriminating test,
inputs/version pins, required capabilities, resource/time cap, measurements,
predeclared continue/adjust/stop conditions and a rollback or exit. Name the next
concrete action. An experiment can succeed as learning while rejecting its hypothesis.

If necessary inputs are absent, specify the experiment as **blocked / not run** and
do the independent preparation that is authorized. A fixture may verify plumbing;
it cannot establish real-world accuracy, demand or hardware compatibility. Keep
unknown, skipped, failed and passed observations separate.

Define reference labels and expected failure behavior before implementing the
candidate. A few assertions invented alongside a toy function are only a rule
smoke test; they do not reproduce the named physical failure or validate the
hypothesis. Report exactly what was exercised (including unused inputs), and
separate logical deductions from observations. Cite raw traces for execution claims.

Save the brief using [zoom-out-template.md](zoom-out-template.md) in task-owned
storage, or render it inline for a short advisory request. Register a saved brief
with the existing checkpoint artifact/evidence mechanism; no checkpoint schema
change is required. Store conclusions and evidence, not private reasoning traces.

## Ownership and handoff

- **Longhaul:** owns the frame, decision, scope and experiment acceptance criteria.
- **Makeextreme:** runs explicitly configured stages of the selected experiment;
  checks dependencies, outputs and recovery. It does not invent a wider goal,
  choose a market or convert a passed command into a validated hypothesis.
- **Flowlore:** retains sourced recipes, scope, counterexamples and measured results.
  A drafted recipe stays draft until its own evidence requirements are met.

These are responsibilities, not a claim of automatic cross-project integration.
Inspect the actual adapter before claiming a handoff ran. A strategic reviewer is
optional for multiple workstreams; give it bounded, read-only review and inspect
its evidence. A single agent can switch modes. Do not require extra agents, tmux,
manual copy/paste or a permanent no-implementation role to use this protocol.

At a milestone, reopen the frame only if new evidence changes the selected route,
the outcome drifts, or the bottleneck moves. Do not perpetually re-plan.

See [source lineage and adaptations](zoom-out-sources.md). This protocol is an
original synthesis; the external skills are references, not installed dependencies.
