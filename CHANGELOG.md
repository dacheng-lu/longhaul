# Changelog

## Unreleased

- secret scan: a value stored next to a sibling `name` that is itself a credential key
  is now reported. That is how n8n stores HTTP header and OAuth body credentials
  (`{"name": "Secret", "value": "..."}`), and the value's own key is only `value`.
  Measured on real n8n workflow exports: the Autotask header pair produced zero hits
  before and two after; a 37 kB workflow went from 2 to 12 reported hits, all of them
  real (`client_secret`, `password`, `access_token`, `Authorization`). `authorization`,
  `secret` and `apiintegrationcode` join the credential key names, and the pointer
  exemption (`${VAR}`, `«redacted»`) still applies.

## 1.2 — 2026-11-01

The protocol now has a measurement layer under `evals/`. The same ten scenarios run
in three conditions (`baseline` without the skill, `v1` at b8b7d8c, `v1.1` at af52cd8),
each in a throwaway profile that holds exactly one condition and is deleted afterwards.
This release moves the skill and the checkpoint helper to 1.2; the measurement layer
and the checkpoint hardening are described below.

- one profile per run, created and deleted around a single subject call, in a
  throwaway sandbox HOME per attempt, with a preflight that rejects a contaminated
  profile before any model call (no hooks, no memory, no session, one key line,
  allowlisted environment)
- ten fixtures: interruption recovery, a file changed after verification, stale
  evidence, a missing host tool, an ambiguous external side effect, a lying worker
  report, a user scope change, a settled question, and two controls
- weighted scoring (30/25/20/15/10) over five dimensions, four hard blockers, and an
  oracle that runs the repository's own `checkpoint.py`; an infrastructure failure is
  reported and excluded, never scored as zero
- blind judging with one pass per condition over a shuffled start order and every
  cyclic rotation, so a slot bias cannot survive: a dimension is only used when all
  passes agree, otherwise it is null and counted, never averaged
- `run_evals.py validate | plan | run | measure | report` with a resumable JSONL
  ledger, pin-drift detection on model, provider, hermes version and the oracle hash,
  and a generated RESULTS.md section between markers
- the deterministic test suite runs the golden overlay and the null-subject baseline
  through the same oracle, so the scores are checked against both extremes
- a write to an absolute path inside the subject's own recorded working directory is
  mapped back onto the result tree, so following the host prompt is not a scope breach
- outcome and protocol are scored apart: only an out-of-scope write or an invented
  observation zeroes the outcome; a stale record or a re-asked question costs protocol
  only, and the report lists gate-fail claims per condition
- `run --probe` checks the sandbox (bwrap, resolved launcher chain, visible paths) and
  `run --pilot` records the baseline pilot; a live matrix refuses to start without a
  fresh clean probe and a pilot whose baseline stays below the 90-point ceiling on six
  cases
- a crash is no longer taken for a rate limit because its stderr happens to contain the
  digits 429 (a random profile name, a path, a port, a pid, a traceback line number): the
  429 must stand alone as a token and not follow `line `; a stderr that has a bare
  `429` token for another reason (a `file:429:7` locator) still reads as `provider_429`,
  which is retried, so the cost is one wasted attempt and not a wrong success
- a killed run is reaped from the ledger and its sandbox is removed;
  cleanup marks a throwaway profile deleted without calling the host hermes
- one owner for the whole subject path: the runner (not the CLI) owns mode check, plan,
  create/list/chat/delete and cleanup; two explicit modes replace the boolean
  `live`/`key_material` combinations, and the blocked live mode is refused before a
  profile, a key or a process even on a direct Python call
- the offline lane accepts only the verified test double. The caller's file is checked
  once (no link followed) and compared with the repository's own canonical bytes; it is
  an identity witness only. Only the repository's canonical bytes are ever copied (exclusive
  create, fsync, re-hash, read-only) and executed, as `[realpath(sys.executable),
  <attempt>/subject.py]`, so a swap of the caller's file after the check cannot reach the
  attempt, and the ledger pin `hermes_version` is parsed from those same bytes and carries
  the token digest (`… [offline:<digest12>]`), so `run` and `measure` never read the
  caller's file after the gate. One key-free state directory is bound, always with bwrap
  and `--unshare-net`;
  the double's own call log is attempt-owned, so the documented `--fake-home <state>`
  form runs the full offline matrix repeatedly; `probe_problems` fails closed on
  missing, empty or malformed child facts and on missing expected chain targets
- `measure` and its judge are a sandboxed subject route too (`run_judge`: neutral HOME,
  `--unshare-net`, scrubbed environment); a non-double `--hermes` is refused with and
  without `--no-judge`, and `run_evals.py` and `judge.py` start no process themselves;
  `measure` exits 2 when the sandbox refuses the judge. Three layers watch for a spawn
  before the gate: a runtime audit hook drives every CLI subcommand, every flag and a second
  value of every non-boolean option with a non-double `--hermes` and requires the refusal
  before any spawn event; a structural test requires every call a handler *evaluates* before
  `offline_gate` (the gate's own arguments, the other operand of `or`/`and`, and the
  decorators, default values and annotations of a nested `def`) to be on a reviewed
  allow-list, in the handler's straight-line body and in the `if`/`while`/`for`/`with`/`try`
  body that holds the gate (a gate in an `else`/`elif`, `except` or `finally` branch, or
  inside a nested function, is not examined); and a source scan (aliases at any import
  depth, `__import__`, `getattr`, `importlib`) is a tripwire. None of them sees `ctypes`,
  `multiprocessing`, `concurrent.futures` or code built inside a string, so this is not a
  guarantee that a future route cannot bypass the gate
- probe canaries (host file, unix socket, TCP) are required, created before the subject
  starts and recorded; a missing, disabled or uncreatable canary makes the probe dirty,
  never clean. The plan masks `/run/dbus` and `/run/systemd`, and the probe proves that a
  connect to the host system bus fails (the `/tmp` socket canary proves only the `/tmp`
  mask). The probe searches `/proc/self/mountinfo` for the real checkout path (fixed string,
  any directory name, whole path components). The key-free state scan reads every regular
  file completely (overlapping blocks, quoted and unquoted `NAME=value` and `NAME: value`)
  for any `*API_KEY`, `*TOKEN` or `*SECRET` name. ASCII-range UTF-16 key text is found in any
  alignment and inside mostly-UTF-8 files; non-ASCII UTF-16 only in an aligned, mostly-UTF-16
  window. Unreadable entries, symlinks, special files and anything beyond the caps (400 files,
  64 directories, 64 levels, 16 MiB) are refused; base64, hex, split, UTF-32 or binary-encoded
  keys are not detected (stated in RESULTS.md).
  `run_one(bwrap=False)` is a test seam that needs `LONGHAUL_EVAL_TEST_SEAM=1`
- live evaluation stays blocked and no key is read anywhere: `run --live`, `run --live
  --pilot` and `measure --live` refuse before any key or subject process, and a later
  live pilot needs a separately authorized, independently audited credential broker.
  The remaining limits (host root bind, `/proc`, abstract sockets, PDEATHSIG, legacy
  ledger rows, redaction not being a containment proof) are written down in RESULTS.md

**Checkpoint (1.2, Linux-only).** `migrate` now follows the written three-way
revision contract: an external observation is trusted only when the record
revision (G), the observation revision (E) and the deliverable revision (A) are
non-empty and equal, where a v1 external deliverable without its own `revision`
uses `A := G` (v1 carries one global revision) and a record with no global
revision is refused. Any conflict empties all bindings, marks the evidence
`unverified: true` with a deterministic `unverified_reason` and keeps the v1
label as `source_revision`; `source_revision` without `unverified: true` is
invalid, and the gate refuses unverified evidence whatever its binding says.
Unknown fields are preserved, `supporting` is exactly boolean, hand-edited
documents are read as strict JSON (a repeated object key, a non-finite number, a
non-zero literal that underflows to 0.0 in binary64, or a lone surrogate is
refused; writes serialize with `allow_nan=False`), and every path is walked
component by component without following a link. The helper and the eval harness
are **Linux-only** in 1.2: they need descriptor-anchored path traversal and
bwrap, and a platform without them fails closed instead of falling back to
`realpath()`. This supersedes the 1.1 note about CI on Linux, macOS and Windows.

Deterministic tests only: a fake `hermes`, no network, no real profiles, and a key
that never reaches the run directory.

The first CI run of this release failed on a clean checkout and found two defects that the
working tree had hidden. (1) The control cases have an empty `golden/overlay` directory, which
git does not store; the validator treated its absence as a damaged suite, and now accepts it for those two
control cases only (every other case still must have its overlay, which a test pins per case). (2) The workflow used the default shallow checkout, but the eval harness
builds its pinned conditions with `git archive <sha>`; the checkout now fetches the full
history. (3) The suite needs pytest's temporary directory outside `/tmp`, which the sandbox masks;
with no `TMPDIR` set, six sandbox tests failed on the runner. The workflow now sets
`PYTEST_DEBUG_TEMPROOT` to `$RUNNER_TEMP`. The suite had been run only in the working tree, with
`TMPDIR` set by the environment, before; it is now also run in a fresh `git clone` with no temp
variables.

## 1.1 — testable floor

The gate now enforces what the prose promised. Seven rules that v1.0 documented and
never checked are rejected with a test each:

- a credential-shaped value anywhere in the state (new `scripts/secret_scan.py`,
  ported from the context-handoff validator; the value is never echoed)
- an unchanged retry (same action and error) and three attempts on one blocker
  without a hypothesis change
- a bundled `next_action` (a non-ASCII bundle marker is a warning, not a block)
- a timestamp in the future, and a timestamp without an offset
- a shortcut with no upgrade trigger (ponytail's ledger)
- a `callable` capability that was never observed working
- an artifact that no evidence binds

Evidence is now bound per artifact: `revision` is the first 12 hex of the file's
sha256, so editing one deliverable invalidates only the criteria that cite evidence
for it. `result: unknown` plus `unknown_reason` records an observation you could not
make, and a recorded `waivers[]` entry is required to complete over it.

New commands: `hash`, `artifact`, `evidence`, `migrate`. `migrate` re-binds v1
evidence only while the content hash still matches, so it cannot launder evidence for
a changed file. v1 files stay readable with a deprecation warning until v1.2.

Removed: the rune diagnostic table (no measured effect, and the learning loop is
carried by the host), and "two clarification rounds per decision" (a second alignment
round-trip on an aligned user is a defect).

Added `tests/` (46 tests, no network) and CI on Linux, macOS and Windows.

## 1.0 — initial protocol

Durable checkpoints, capability discovery, bounded delegation, ABCD decisions with a
recommendation, SECI-inspired learning, and a stdlib checkpoint validator.
