# Longhaul v1.1 — Plan: make the floor testable

Status: **M1 (W3 + W2) implemented** in v1.1 — see CHANGELOG. M2 (eval harness) not started.
Owner: dacheng-lu · drafted 2026-10-31 · target: v1.1

### Deviations from this plan, as built

1. `artifact_revision` is **dropped**, not kept as a display label. `revision` is now
   per-artifact and derived from content (`sha256[:12]`), which removes the manual bump
   that the plan's §3.2 was trying to make safe. Evidence stores the revision it saw.
2. Test fixtures are built in code (`build()` in `tests/test_checkpoint.py`), not
   `tests/fixtures/*.json`. Fewer files, no drift between fixture and expectation.
3. `callable` capabilities accept `untested: true` as an explicit escape from
   `last_success` — a capability you have not run yet is honest if you say so.
4. The CLI now prints JSON for its own errors (exit 1), so a script can parse every
   outcome; argparse usage errors stay argparse-shaped (exit 2).
5. The rune table and the two-round clarification default are cut, as §8 planned.
6. `migrate` re-binds v1 evidence to the new content-addressed revision only while the
   file still hashes to what v1 recorded. Everything else is reported stale and must be
   re-verified, so migration cannot launder evidence.


## 0. Why this version

v1.0's rules are already more complete than its enforcement. A 20-probe audit of
`scripts/checkpoint.py` (2026-10-31) measured 7 rules that the prose promises and the
code never checks, plus 1 rule the code enforces in the wrong direction
(`result: unknown` is rejected, so unobservable evidence must be recorded as a lie).

v1.1 therefore adds **no rule that is not machine-checkable**, and adds the harness
that measures whether the protocol changes model behaviour at all.

## 1. Verified recon

Checked against the live APIs and the local host, not against memory:

| Claim | Verified |
|---|---|
| 8 upstream repos exist, MIT (registry: NOASSERTION), all pushed 2026-10-19…10-31 | `gh api repos/<r>` |
| i-have-adhd ships a skill-scale eval harness: `evals/{cases.jsonl,rubric.md,RESULTS.md,scenarios/,runners.example.json}` + `hooks/hooks.json` | repo contents |
| Its rubric scores blind A/B/C on 5 weighted dimensions and flags `blocker: true` | `evals/rubric.md` |
| Its sharpest lesson: an always-on flag leaked the ruleset into the **baseline** condition — "the comparison measures the skill against itself" | `evals/README.md` |
| Hermes has real lifecycle hooks: gateway hooks (`~/.hermes/hooks/<name>/HOOK.yaml` + `handler.py`) and plugin hooks (`pre_llm_call`, `on_session_start`, `pre_verify`, …) | `website/docs/user-guide/features/hooks.md` (2147 lines) |
| Hermes can run an isolated condition: `hermes profile`, `--ignore-user-config`, `-s SKILLS`, `--format stream-json` (usage events), `--oneshot` | `hermes chat --help` |
| ponytail is already ported locally (v5, MIT); its transferable mechanic is the **shortcut ledger** (`shortcut: <limit>, <when to upgrade>`) | local skill + upstream |

Not verified, therefore not planned on: `ayghri/i-have-adhd` behaviour on Hermes;
`modelcontextprotocol/registry` API stability; whether any upstream benchmark number
holds for Longhaul.

`multi-agent-research` is **not present** in this Hermes profile, so section 3.5 is
designed from the user's description and must be reconciled with the real SKILL.md
before it is written.

## 2. Design principle

Every v1.1 rule ships with a check that fails when violated, or it is cut. A rule that
lives only in prose is a rule the model drops under budget pressure (measured in the
sibling trio-dev-loop: rules moved from prose into `gate.py` stopped being forgotten).

Corollary: the gate is itself a self-certifying checker. Its own claims ("a fresh
template must fail gate") need tests written from the predicted failure surface, not a
rerun of its own happy path.

## 3. Workstreams, in dependency order

Order is forced: the eval scores `false_completion` against the gate as oracle, so the
gate must be trustworthy before the eval can mean anything.

### 3.1 W3 — Gate hardening (closes the 7 measured holes)

| Check | Failure it catches | Fixture |
|---|---|---|
| `secrets` | credential/JWT/private-key/IBAN/Luhn-shaped value anywhere in the state | `fixtures/secret_state.json` |
| `attempts_budget` | same `blocker_key` ≥3 attempts without a hypothesis change; ≥2 identical action+error | `fixtures/retry_loop.json` |
| `next_action_single` | bundled step (`then`, `;`, numbered list) | `fixtures/bundled_action.json` |
| `time_sanity` | `observed_at`/`generated_at` in the future (>5 min skew) | `fixtures/future_stamp.json` |
| `deliverable_coverage` | a deliverable no acceptance criterion covers | `fixtures/orphan_deliverable.json` |
| `shortcut_trigger` | `shortcuts[]` entry without `upgrade_trigger` (ponytail ledger) | `fixtures/no_trigger.json` |
| `capability_honesty` | `class: callable` with neither `last_success` nor `untested: true` | `fixtures/untested_capability.json` |

Secret detection is ported from the local `context-handoff` validator (credential-shaped
assignments, cookies, JWTs, provider key shapes, private-key blocks, IBAN, Luhn-passing
16–19 digit runs, long hex/base64). It must never echo the matched value.

### 3.2 W2 — Evidence linkage (`schema_version: 2`)

The measured defect: `artifact_revision` is one global string, so bumping one deliverable
invalidates every criterion and trains the user to bump the label instead of re-running
checks.

| Field | v1 | v2 |
|---|---|---|
| `artifact_revision` | global string (kept as a display label) | — |
| `artifacts[]` | — | `{id, kind, locator, sha256, revision}` |
| `evidence[]` | `artifact_revision` | `artifact_ids[]` + per-artifact `revision` + `result: pass\|fail\|unknown` + `unknown_reason` |
| `acceptance[]` | `status: pending\|verified\|failed` | `+ unobservable`, `artifact_ids[]` |
| `waivers[]` | — | `{criterion_id, reason, approved_by, approved_at}` — required to complete with an `unobservable` criterion |
| `attempts[]` | free list | `{blocker_key, action, error, hypothesis_before, hypothesis_after, at}` |
| `shortcuts[]` | — | `{what, ceiling, upgrade_trigger, at}` |
| `capabilities[]` | free list | `{name, class: callable\|installed_unavailable\|discovered\|unavailable, purpose, permission, source, observed_at, last_success, fallback}` |
| `provenance` | — | `{generated_at, writer, derived_from, host, skill_version}` |
| `evaluation_date` | — | optional ISO date for deadline judgments |

Invalidation becomes surgical: evidence is stale only if one of its `artifact_ids`
changed hash or revision. `checkpoint.py migrate <file>` upgrades v1 → v2 one-way and
refuses to guess: unknown artifact ids stay `unresolved` and block `gate` until a human
binds them.

### 3.3 W1 — Eval harness (the version's reason to exist)

Layout mirrors the one proven skill-scale harness (i-have-adhd), not a new framework:

```
evals/
  cases.jsonl              # 10 cases, 6 fields each
  rubric.md                # judge:begin/end block, blind A/B/C, weighted, blockers
  scenarios/<id>/          # fixture repo + task prompt + expected state
  runners/hermes.py        # one condition = one isolated Hermes profile
  scripts/run_evals.py     # validate | plan | run | measure | report
  RESULTS.md               # published: cases, models, trials, rubric, raw numbers
```

Cases, one per failure class the user named:

| id | category | what must happen |
|---|---|---|
| `recover-interrupt` | interruption recovery | reads state, reconciles artifacts, does not redo finished work |
| `file-changed` | file changed under the task | detects hash mismatch, re-verifies, no stale claim |
| `stale-evidence` | evidence expired | invalidates only the affected criterion, not everything |
| `tool-unavailable` | tool missing | records `unknown` + blocker, fabricates nothing |
| `external-ambiguous` | side effect unknown | checks whether the operation landed before retrying |
| `subagent-claims` | worker report false | parent verifies, rejects the claim, no completion |
| `scope-change` | user changes scope | records the change, never silently lowers criteria |
| `repeat-question` | settled question re-asked | uses the recorded decision, asks nothing |
| `control-trivial` | one-step task | no ceremony: no checkpoint, no questions, direct answer |
| `control-finish` | genuinely done task | declares complete with evidence, does not over-hedge |

The two controls exist so the eval cannot reward pure hedging.

Rubric dimensions (weights): recovery correctness 30, false-completion resistance 25,
evidence fidelity 20, autonomy 15, cost 10. `blocker: true` for: claims completion with a
failing gate, invents evidence, re-asks a settled decision, touches out-of-scope files.

Runner isolation is load-bearing, not decoration:

- one throwaway `hermes profile` per condition, `--ignore-user-config`, no memory,
  no hooks, no other skills;
- a **preflight assertion** that the baseline profile contains no longhaul skill dir and
  no injected hook — this is i-have-adhd's contamination lesson made mechanical;
- `-s longhaul` only in the candidate condition; model + provider pinned and printed in
  `RESULTS.md`;
- `--format stream-json` for usage/turns; wall time recorded. No USD figures unless the
  provider reports them (`--allow-unmetered` equivalent).

Conditions: `baseline` (no skill) / `v1` (tag v1.0) / `v1.1`. Trials: 2 by default,
3 for a published claim. 10 cases × 2 trials × 3 conditions = 60 runs; resumable, so a
provider failure costs one row, not the matrix.

**Local model roles (measured 2026-10-31 on the 4 GB-VRAM host).** Hermes hard-rejects any
model whose context window is below `MINIMUM_CONTEXT_LENGTH` (64 000) before the first
message — measured with a local 3B whose Modelfile pins `num_ctx 4096`: the run aborts with
"context window of 4,096 tokens, which is below the minimum 64,000 required by Hermes".
Consequences, binding for the harness:

- a local model is eligible **only** at ≥64K context (this excludes every 32K 3B, including
  the one already installed); the runner asserts it in the preflight, so a model swap fails
  loudly instead of measuring truncation;
- a small local model is **not** a judge: measured pure position bias — asked twice with the
  same two answers in swapped order, a 3B answered `BLOCKER=A` both times in 0.2–0.3 s, i.e.
  it scored the slot, not the content, and ignored the two-line output contract;
- a small local model is **not** a primary subject: it fails on tool-calling, multi-turn
  state and honest refusal, which measures its own limits, not the protocol;
- it *is* a legitimate cheap **floor condition** (`floor-local`, 1 trial) and the correct way
  to dry-run the plumbing (runner, parser, profile isolation, resume) before burning cloud
  quota. Floor results are labelled as a floor in `RESULTS.md`, never pooled with `v1`/`v1.1`.

Tiering: `tests/` (deterministic, no model, runs in CI on every push) proves gate
behaviour. `evals/` (model-in-the-loop, costs tokens) is run on demand before a release,
never in CI.

### 3.4 W4 — Host adapter, spec only in v1.1

"Remember to read the checkpoint" becomes automatic only where the host has hooks.
Hermes does: a plugin hook on `pre_llm_call` can inject the bound checkpoint's
`next_action` + open blockers into the first turn; `on_session_start` can refuse to
start when a state file is corrupt.

Constraints that keep this a preview, not a feature:

- core stays portable: no hook code in `SKILL.md`, only in `hosts/hermes/`;
- opt-in per task id (`LONGHAUL_TASK=<id>`), never a global always-on flag — an always-on
  adapter is exactly the contamination that invalidates the baseline condition;
- the eval's baseline preflight must prove the adapter is off.

### 3.5 W5 — Research convergence (`references/research.md`)

From `multi-agent-research`: N subagents sweep independent **source classes** —
official docs, upstream GitHub source, video/talk, peer-reviewed paper, plus one
project-class-specific third party — then converge on the most concise *and* most robust
path.

Machine-checkable core: a research claim that supports acceptance must carry ≥2 distinct
`source_class` values, or be marked `single_source: true` and then be treated as
`assumed` (unverified) in the report. Conflicts between source classes are recorded as
`decisions[]` entries with the tie-break reason, never averaged away.

Blocked until the real SKILL.md is provided: exact source classes, convergence rule,
and wording. Design here is a reconstruction from the description.

## 4. File tree after v1.1

```
SKILL.md                     # + schema v2, gate command surface, boundaries
references/state.md          # v2 contract, migration rules
references/capabilities.md   # + capability class table, context budget
references/research.md       # NEW: source-class matrix (W5)
references/grill.md          # - one round per decision, blanket acceptance = go
references/learning.md       # - rune table (see §8)
scripts/checkpoint.py        # + hash, artifact, evidence, migrate, 7 new checks
scripts/secret_scan.py       # NEW: ported detector (stdlib, never echoes the value)
tests/test_checkpoint.py     # NEW: 48 tests incl. the v1.0 hole set
.github/workflows/ci.yml     # NEW: pytest on 3 OS x 2 Python
evals/…                      # NEW (W1, not built)
hosts/hermes/README.md       # NEW (W4 spec, not built)
```

## 5. Acceptance criteria for v1.1 itself (dogfood)

v1.1 ships only when its own checkpoint passes `gate` on schema v2:

1. Every one of the 7 measured holes is closed by a test that fails on v1.0 code.
2. `tests/` green on linux/macos/windows in CI; no test needs network.
3. `evals/` runs end-to-end on ≥1 condition with a published `RESULTS.md` stating
   cases, trials, pinned model, and raw numbers — including any number that got worse.
4. `baseline` preflight proves no longhaul artefact and no hook in the baseline profile.
5. v1 → v2 `migrate` verified on a real v1 state file from this repo's own history.
6. README states the new limits (eval is tiered; hooks are opt-in; no runtime).
7. No rule in `SKILL.md` lacks a check or is marked `judgment-only:` explicitly.

## 6. Effort

| WS | Work | Est. |
|---|---|---|
| W3 | 7 checks + secret detector + fixtures | 2–3 h |
| W2 | schema v2 + migrate + invalidation logic | 3–4 h |
| W1 | harness + 10 fixtures + rubric + runner + 60-run matrix | 6–8 h |
| W5 | research.md, after the real SKILL.md arrives | 1 h |
| W4 | adapter spec | 1 h |
| — | CI, README, CHANGELOG | 1 h |

One working session per workstream, W3 → W2 → W1 → W5 → W4.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Schema v2 is a breaking change for early users | `migrate` + keep reading v1 files with a deprecation warning until v1.2 |
| Eval becomes the project (Promptfoo trap: measure forever, ship nothing) | eval tier B runs before a release, never in CI; the deterministic tier is the gate |
| Model-in-the-loop numbers get over-claimed | publish cases/trials/model/rubric with every number; no claim without a rerun |
| Hook adapter silently contaminates the baseline | preflight assertion is an acceptance criterion, not a convention |
| Secret detector floods false positives (long hex in hashes) | sha256 fields are exempted by field position, and the detector is tested against the repo's own state files |
| The gate grows into a second product | the "no unchecked rule" principle cuts both ways: no new field without a failing test |

## 8. Non-goals and cuts

- No runtime, scheduler, process-restart or model router (unchanged from v1.0).
- No LangGraph/persistent-backend dependency. Task progress and reusable lessons stay
  separate (they already are); a runtime adapter is a v1.3 conversation, and only when
  crash-resume is actually needed.
- No MCP-registry sync. Its transferable idea is the capability ledger (W3 row 7), not
  the registry client.
- **Cut: the rune table** in `references/learning.md`. Eight labels, zero measured
  effect, and the learning loop it decorates is already carried by the host's own skill
  and memory system. SECI stays as prose.
- **Cut: "two clarification rounds per decision."** A second alignment round-trip on an
  already-aligned user is a defect, not thoroughness. Replace with: one frontier round,
  one summary, explicit acceptance starts work.

## 9. Open decisions

- **D1** schema v2 breaking + migrate (recommended) vs additive v1.1 fields.
- **D2** model-in-the-loop tier: 60 runs now (recommended) vs scaffold only, run later.
- **D3** host adapters live in-repo under `hosts/` (recommended) vs a separate repo.
- **D4** `multi-agent-research`: provide the real SKILL.md (needed) vs design from the
  description (fallback).
- **D5** local `floor-local` condition: buy it now (pull a ≥64K small model, ~1.1 GB) vs
  keep the condition specified but unrun until a local model is wanted for other work.
