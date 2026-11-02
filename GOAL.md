# Goal: dependable long work, understandable from the first visit

Status: working goal, not a release claim or schedule. See [ROADMAP.md](ROADMAP.md)
for planned work and [evals/RESULTS.md](evals/RESULTS.md) for measured results.

## Outcome

Help a person carry a long task through interruption and change: preserve its goal
and constraints, reconcile the actual artifacts, identify the next action, and
support completion claims with evidence tied to the current deliverable. Longhaul
must remain useful on its own; the host provides execution and persistent storage.

## Acceptance targets

| Dimension | Target | Evidence needed |
| --- | --- | --- |
| Quality | Recovery retains the right goal, constraints and current evidence; stale evidence cannot pass the gate. | Repeatable interruption and changed-artifact cases, including negative controls; report failures. |
| Content | A new visitor understands the purpose, boundaries, setup and expected result. | Working links and commands; consistent English, Chinese and German entry points and limitations. |
| Function | Add capabilities only for a demonstrated problem without weakening independent use. | Reproduction, acceptance case, regression checks, compatibility and maintenance impact. |
| Browsing | The project is legible and navigable on desktop and mobile. | Inspect real rendered pages at both widths; check image framing, overflow and links. |

Time targets to test with first-time users, not measured claims: 30 seconds to
understand which project fits, 3 minutes to assess a case and limitations, and
10 minutes to run a minimal example after prerequisites are met. Record sample,
observations, failures and environment; do not claim success from maintainer trials.

## First milestone

1. Offer a reproducible standalone checkpoint example showing a passing gate,
   changed artifact, stale evidence and re-verification. Record actual commands
   and limitations; an example is not a substitute for interruption testing.
2. Compare Longhaul's state and completion semantics with Makeextreme's stage
   execution evidence. Specify a small versioned exchange only after testing a
   real task; integration is a candidate, not an implemented feature.
3. Exercise success, interruption, stale evidence and an uncertain external action;
   keep host isolation and identity guarantees explicit.
4. Test onboarding and browsing with first-time users; log unresolved gaps.

## Improvement loop

Record a problem and a minimal reproduction, add a regression case, make the
smallest change, compare results and costs with the previous version, then review
and publish the evidence. Keep candidate lessons separate from tested rules;
record scope, counterexamples and rollback conditions. Do not promote task output
into privileged instructions or infer model-weight updates from stored lessons.

Stop a milestone when its cases pass and remaining gaps are documented; do not
add capabilities merely to increase scope. Human approval and host permissions
remain authoritative for consequential actions.
