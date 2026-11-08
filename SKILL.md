---
name: longhaul
description: "远航 · Longhaul coordinates complex long-horizon research, creation and implementation using durable checkpoints, available MCP/tool discovery, bounded model/subagent delegation, ABCD choices with a recommendation, evidence-based recovery and completion, and SECI-inspired learning. Use when the user requests Longhaul/远航, long-task safeguards, cross-session continuation, repeated-failure recovery, or this combined workflow. Skip routine one-step tasks unless explicitly requested."
---

# 远航 · Longhaul

Version: 1.2

Support sustained progress, recoverable work and honest delivery. Treat this skill as an orchestration protocol, not a background runtime or a guarantee of success. Follow host policies, user authorization and relevant domain skills. Do not replace domain validation with generic reflection.

## Start or recover

1. Read the current request and relevant task state before asking questions. Preserve original objectives while incorporating steering. Separate facts from assumptions.
2. Establish deliverables, constraints, acceptance criteria and scope exclusions. State reasonable reversible assumptions; do not turn a clear request into a compulsory interview.
3. Locate the task's own persistent checkpoint. Bind it to a unique task ID and explicit location; never select another task merely because its state is newer. Compare recorded state with actual artifacts and external results on recovery.
4. Discover relevant skills, callable MCPs/tools and supported delegation/model capabilities. Read [capabilities.md](references/capabilities.md) when selecting integrations or delegating. Resolve available capabilities before researching replacements.
5. Choose one concrete next action and a small milestone. Continue authorized work autonomously. Ask only questions that materially affect the outcome; consult [grill.md](references/grill.md).

For a consequential investment or product decision, optionally use [value-validation.md](references/value-validation.md) to test the weakest value assumption before expanding work. Apply the purpose-specific criteria; do not require monetization for noncommercial tasks.

## Keep durable task state

Maintain one authoritative record per task: goal, constraints, milestones, artifact revision, acceptance criteria, evidence, blockers, attempts, decisions, capabilities and next action. Keep reusable lessons separate from progress. Record observable facts and concise rationales, not private reasoning traces or credentials.

Use host-native durable task storage if provided. Otherwise use a project-backed persistent location or the host's Library/file-persistence workflow. An absolute scratch path is not durable storage. Persist checkpoint changes and retain the returned locator; if persistence is unavailable, disclose the limitation and provide a resumable handoff.

For filesystem workflows, run:
`python3 <skill-dir>/scripts/checkpoint.py init <task-state.json> --task-id <id> --goal <goal>`
Register each output with `artifact` (it records the sha256 and a content-derived revision), bind observations with `evidence`, write acceptance criteria that cite those evidence ids, then run `validate`; run `gate` before delivery. Editing a file gives it a new revision, so only the evidence and criteria that depend on it go stale — re-verify what changed instead of re-running everything. Read [state.md](references/state.md). Store task state outside this installed skill directory. With API-backed storage preserve equivalent fields.

Checkpoint after a verified milestone, consequential decision, failed approach, and before planned handoff or long operation. Do not depend on receiving a pre-crash event. On resumption reconcile actual state and stale evidence before proceeding. Use separate records for independent tasks and one writer for shared state.

Two additions stay optional, so every older record keeps validating. Record why a task stopped in a state a machine can sort: `report PATH --state progress|waiting|ready|blocked|failed|dropped --reason ...`; `blockers` keeps the prose a human reads. Record who authorised the work, under which policy version and with which scope, in `<record-stem>.mandate.json`; before resuming granted work, run `revalidate` and never continue on a grant that no longer holds. Read [report-states.md](references/report-states.md) and [mandate.md](references/mandate.md). Prose is not a mandate, and prompt text never relaxes either rule.

## Execute, inspect, adapt

- Perform the next bounded action with the appropriate skill/tool. Parallelize independent reads; sequence dependencies and shared mutations.
- Compare observations to the criterion. Separate attempted, produced and verified. Link evidence to the artifact revision it supports.
- Trigger focused grilling at consequential forks, contradictory evidence, repeated failure or pre-delivery. Challenge the weakest assumption with a discriminating check. Avoid ceremonial fourteen-step reflection.
- Default to at most two unchanged transient attempts and three meaningfully distinct approaches to one blocker, unless the user sets another budget. Record attempts with `blocker_key`, `hypothesis_before` and `hypothesis_after`: the gate rejects an unchanged retry and three attempts on one blocker without a hypothesis change. Check external effects before retrying an operation that may have succeeded. Change hypothesis, use a safe fallback or report the blocker while continuing independent work.
- When a real resource budget applies, reserve verification and recovery capacity before another attempt. Use measured costs, keep units separate, and refresh shared allocations on resume. Read [recovery-budget.md](references/recovery-budget.md); do not infer a universal 25% allocation or calibrated probabilities from model confidence.
- Never lower acceptance criteria merely to declare completion. Record user-approved scope changes explicitly.
- Give concise progress updates at the host's required cadence: findings, uncertainty and next action.

## Delegate selectively

Use supported subagents for separable research, implementation or independent review when benefit exceeds coordination cost. Default to zero for small work and at most two concurrent workers for complex work, within host limits. Use only enumerated models and actual model-selection parameters. Read [capabilities.md](references/capabilities.md). If delegation is unavailable, work sequentially; never fabricate a worker result.

## Grill with ABCD + recommendation

For consequential user-owned choices, present four genuinely distinct options A–D with tradeoffs, mark one recommended choice and explain why. Permit free-form responses. Use fewer options if four are artificial or the host cannot support them. Never use a clarification widget for approval. Read [grill.md](references/grill.md) for triggers and UI adaptation.

Resolve researchable facts with tools, not preference votes. Do not re-ask settled questions. Continue independent work while awaiting answers. For optional unanswered choices, state a reversible default; missing answers never authorize consequential external actions.

## Learn with SECI

For meaningful reusable findings: observe practice, articulate a candidate rule, integrate with existing knowledge, and test on another case. Preserve source, scope, counterexamples and validation status. Do not turn untested self-reflection into a universal rule or automatically rewrite this skill after one failure.

Read [learning.md](references/learning.md) when extracting lessons or diagnosing a persistent stall. Do not claim weight updates or guaranteed improvement.

## Verify and hand off

1. Compare current output to every acceptance criterion. Verify actual files, tests, primary sources or observed external state. Independently inspect important worker claims.
2. Ensure evidence applies to the current revision. Reuse valid evidence when relevant inputs have not changed; rerun checks when invalidated.
3. Run the checkpoint gate if available. It checks bookkeeping and local hashes, not semantic correctness. An observation you could not make is recorded as `result: unknown` with a reason; completing over it needs a recorded waiver naming who approved it. Complete domain-specific quality checks.
4. Persist state and deliver using host-specific artifact links. Report verified outcomes and material limitations. If incomplete, provide usable partial work, blockers and the exact next action without marking the overall task complete.
5. Never claim installation, external execution, scheduling, recovery or verification without an observed result. Disclose material capability reductions.
