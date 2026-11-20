# Proposal — Longhaul 2.0: judgement for a cluster of agents

Status: **proposed, not decided, not scheduled.** Design only. The v1.3 live-benefit
measurement is not finished; until it is, 2.0 is a design and at most a minimal prototype,
and if the baseline shows no benefit the direction is withdrawn.

## Why

Longhaul already keeps a long task's goal, constraints and evidence across interruption, and
refuses stale evidence at the gate. What it does not yet do is answer three questions that a
cluster of workers creates: *who was allowed to ask for this*, *which phase is this task in
and why*, and *can this "done" be trusted when the producer is not the judge*.

An orchestrator (LangGraph, the OpenAI Agents SDK, ADK, the Microsoft Agent Framework,
CrewAI) decides who calls whom. This layer decides what a worker may do, where it is confined,
and whether its claim of completion survives an independent check. It is not another
orchestrator and does not compete with them.

North star, shared with the other two projects: the least human attention for the most value.
A notification, a question, a setting or a panel element must buy more than it costs, or it
is removed.

## What 2.0 would add

1. **Mandate.** An immutable record: policy version, scope, strength, the requester identity
   *as reported by the host*, the approver, and the time. A widened scope creates a new
   Mandate; it never edits the old one. A revoked Mandate cannot authorise a later worker's
   completion claim. Longhaul records the identity it was given; it does not authenticate it.
2. **Phase decision table.** The task phase is decided by a rule table applied in order, not
   by the agent's own sentence. Each rule names the evidence it reads and the phase it
   yields; the table is data, checked by the validator, and a missing rule is `unknown`, not
   `done`.
3. **Stateless adjudication.** The same inputs produce the same verdict. No hidden state, no
   wall-clock heuristics that cannot be replayed. A verdict records its inputs, the policy
   version and the rule that matched, so it can be replayed after a policy change — and a
   policy change invalidates old verdicts rather than rewriting them.
4. **Independent verification.** The producer cannot certify its own completion. A verdict
   names its verifier: a deterministic check, or a different identity that did not produce the
   artifact, and which of the two it was. "Same agent, second look" is not independent and is
   labelled as such.
5. **Host-run gate.** The phase gate runs in the host, consuming Makeextreme's evidence
   (artifact hashes, journal entries, approvals). The documentation states exactly which
   integrations still depend on the agent's cooperation and which are enforced.
6. **Cluster evaluation cases.** Add to the evaluation set: two workers racing for one task;
   a worker that forges "done"; a peer write that makes evidence stale; an injected
   instruction inside a tool result; a revoked Mandate arriving after the work started. Each
   case has a negative control and a named rule when it fails.

## Shared contract

A versioned exchange with Makeextreme and FlowLore, at minimum:
`task / stage / mandate / effect / capability / artifact+hash / evidence / approval`.
It is frozen only after one real task runs end to end (the release-digest example); until
then it is `v0` and explicitly not authoritative. Missing fields cannot be silently promoted
to `pass`. Each project stays independently usable without the other two.

## Autonomy ladder (shared)

L0 shadow (record only) → L1 reversible inside the task directory → L2 whitelisted egress with
a credential broker → L3 irreversible with a human approval bound to the content hash.
Promotion is a human decision backed by shadow-mode agreement, never automatic.

## Acceptance

- A revoked Mandate cannot let a later worker's completion claim pass.
- A producer's self-report never yields `done` without a named independent verifier.
- The same inputs and policy version produce byte-identical verdicts on replay; a policy
  change marks old verdicts invalid rather than silently recomputing them.
- A peer write between check and gate makes the evidence stale and the gate fails, naming the
  hash that changed.
- Two workers on one task: one claim wins, the other is refused with the rule named.
- An injected instruction in a tool result cannot change the phase or the policy.
- Attention cost: for each successfully delivered task, count interruptions and why. Report
  it; never optimise it by hiding a problem.

## Non-goals and limits

- No claim that the sandbox or the approval is a security proof; no identity authentication.
- No scheduler, no queue, no model hosting. The host supplies execution, storage and identity.
- No new orchestration features before the v1.3 baseline exists.
- If the v1.3 measurement shows no benefit over the current workflow, this proposal is
  withdrawn and the roadmap says so.

## Order

M0 the existing P0 items (protocol conflict, open audit findings) → M1 contract v0 + the
release-digest end to end → M2 Mandate + phase table + stateless verdicts → M3 independent
verification + cluster cases → M4 host gate + attention metrics → M5 MCP surface and one
framework adapter. Each milestone is a PR with tests, failures and limits; never a direct push
to `main`.
