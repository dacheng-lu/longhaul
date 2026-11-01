# Eval results

Status: harness built; the baseline pilot has not been run and the 60-run matrix is not started.

## What this measures

Ten cases, one per failure mode the protocol claims to handle, each run under three conditions
(`baseline` = no skill, `v1` = 1.0, `v1.1` = 1.1). Two controls (`control-trivial`,
`control-finish`) exist so the eval cannot reward pure hedging: a run that always answers
"incomplete" loses them. Trials default to 2; fewer than 3 trials is an exploratory run and is
labelled as such, never published as a claim.

## Baseline pilot (gate in front of the matrix)

Before the 60-run matrix: baseline only, 10 cases x 1 trial, scored outcome-only. If six or more
cases reach 90 or better, the prompts are still teaching the behaviour they are supposed to
measure and the matrix stops: the prompts are reduced to the facts of the task, the near-miss
mutants in `scenarios/<id>/mutants.json` are sharpened, and the pilot is rerun. Only fewer than
six cases at 90 or better authorises the matrix. Prompts therefore carry the task, never the
protocol; the expected behaviour lives in `expected.json`, the state and the oracle.

## Calibration runs

Every case ships `scenarios/<id>/mutants.json` with four runs and the score band the oracle has
to reproduce: the golden end state (exactly 1.0), the null subject (no work, a completion
claim), at least one near miss (a plausible wrong route, including one out-of-scope write and
one fabricated claim where the case allows it) and one correct alternative route (a different
tool or a different order, no blocker, 0.9 or better). Golden and null are the two extremes and
hide everything in between; the near misses are what the pilot and the release check actually
compare against.

## Pins

Model, provider, judge, hermes version, repo head, skill fingerprints and the per-run raw table
are written by `run_evals.py report` into the generated section below. They are not typed by
hand: a number without its pins is not a result.

## Reproducing

```
python3 evals/scripts/run_evals.py validate
python3 evals/scripts/run_evals.py plan --trials 2
python3 evals/scripts/run_evals.py run --trials 2 --parallel 3    # live: blocked, see below
python3 evals/scripts/run_evals.py measure
python3 evals/scripts/run_evals.py report
```

`validate`, `plan`, `measure --no-judge` and `report` work without a model. A real subject
run and a real judge need `--live`, which is fail-closed (see “Live evaluation is
blocked”). The harness's own double can be exercised offline when `--hermes` points at a
byte-identical copy of `tests/fake_hermes.py` — either directly or as
`python <verified-double>` — and `--offline-state` names a key-free state directory that
is neither the invoking user's home nor the repository and holds no `.hermes/.env`.

The harness is Linux-only: `bwrap` (bubblewrap) is a hard prerequisite, not an optional
dependency. A missing or broken bwrap is a preflight failure; there is no unwrapped
fallback, and the test suite fails loudly instead of skipping.

## Live evaluation is blocked (fail-closed)

The harness has exactly two execution modes, and only one of them may run:
`OFFLINE_VERIFIED_DOUBLE` and `LIVE_BLOCKED`. `LIVE_BLOCKED` is refused inside the
runner (`evals/runners/hermes.py`), first, before a profile, a key or a process, so a
direct Python call is as fail-closed as the CLI: `run --live` (matrix), `run --live
--pilot` and `measure --live` exit 3 with `blocked: …` and start nothing. This is a
deliberate refusal, not an untested path.

No runner API reads, copies, inherits or accepts key material any more: the key
reader, the key writer and the `key_material` switch are gone, not disabled. The
reason is unchanged: the sandbox is a mount/pid/network boundary, not a secret
boundary, and a regex or a redaction pass is **not a containment proof**. A later
live pilot needs a separately authorized, independently audited credential broker or
a provably least-privilege hermetic execution environment; nothing here is one, and
no speculative broker is built.

## Offline path (the verified test double)

The offline lane exists only to exercise the harness with the double that ships in
this repository. It is selected by content, never by a name, a flag or a caller's
promise: two canonical input forms are accepted — the direct double entry and the
historical `python <verified-double>` launcher — each optionally with exactly one
`--fake-home <dir>` pair that must name the bound state.

The gate returns one immutable **`VerifiedDouble` token**, and that token is the only
byte source downstream. Line 1 of the caller's file must be one of the allowed python3
shebangs (`#!/usr/bin/env python3`, `#!/usr/bin/python3[.N]`); the caller's file is read
**once** without following a link, canonicalised and compared with the repository
double's canonical bytes, which are read once too. The token carries *those* repository
bytes, not the caller's. `_attempt_double` writes only `token.repository_bytes` into
`<attempt>/subject.py` (`O_EXCL`, fsync, re-read, re-hash), binds the copy read-only and
runs `[realpath(sys.executable), <attempt>/subject.py, …]`. After the gate no
caller-controlled byte and no caller path is read or executed again, so a swap between
check and exec cannot reach the attempt (the TOCTOU class is removed, not only the
missing comparison). A forged `#!/bin/sh` first line, an edited byte, `python rogue.py`,
a symlinked entry, an extra argv and a `python3`-named ELF in a temp directory are
refused before any process starts. A bundled interpreter must be
`realpath(sys.executable)` or root-owned under `/usr` and not group- or world-writable.
`run`, `probe`, `pilot`, `measure` and the judge all consume the same token/plan.

`run --live --probe` is the one exception to “no supplied path runs”: it executes the
supplied `--hermes` entry with `--version` and an inspection shell **inside** bwrap
(read-only host root, no network, no model, no key). Every subject run and every judge
call uses the verified copy instead.

The double's own call log is **attempt-owned**: the runner sets
`LONGHAUL_FAKE_LOG` to a writable directory inside the attempt and copies the log into
the run directory afterwards. Only the small config and the chat counter stay in
`--fake-home`, so the documented `--fake-home <state>` form can run the full matrix
twice on the same state.

The state scan reads **every regular file completely**, in overlapping blocks, with a
no-follow open and a 16 MiB total budget: a key after the old 64 KiB prefix, or one
straddling a block boundary, is found. It refuses a `.hermes/.env`, any
`*API_KEY`/`*TOKEN`/`*SECRET` assignment (quoted or not, `=` or `:`), a `sk-…` shaped
value, a symlink or special entry, an unreadable file or directory
(`os.walk(onerror=...)`), and any cap (400 files, 64 directories, 64 per level, 64
levels, 16 MiB) — the caps read `too many`/`too deep`/`too large to prove key-free`. A
token limit such as `max_tokens=4096` is not a key (the name must end in
`API_KEY`/`TOKEN`/`SECRET`).

UTF-16 coverage is exact, not general: ASCII-range UTF-16 text (a key made of plain
ASCII characters) is found in any alignment and in any mixture with UTF-8, because each
window is also scanned with the NUL bytes removed. A key whose *characters* are outside
ASCII (accented letters, CJK, …) is only found when the window is aligned to the
encoding and predominantly UTF-16 (a BOM or a high NUL share selects the UTF-16 view).
UTF-32 and other encodings are not decoded at all.

What the scan does **not** detect, and no test pretends it does: base64, hex or
otherwise encoded keys, a key split across files or lines, non-ASCII UTF-16 key material
outside the aligned/predominant case above, UTF-32 and other encodings, and key material
in binary files. Treat the scan as a guard against obvious mistakes, not as a credential
boundary.

The runner owns the whole path: mode check, `SandboxPlan`, `profile create`, `skills
list`, `chat`, `profile delete` and cleanup. `run_evals.py` builds no plan and starts no
subject; it names no spawn API at all. (`validate` does run the shipped checkpoint gate
on repository fixtures — a fixed maintenance call, never a subject.) The offline plan
binds exactly one state directory, requires it to be key-free before any process starts
and adds `--unshare-net`. A missing or broken bwrap is a preflight failure: there is no
unwrapped fallback.

### The judge is a subject route (`measure`)

`measure` judges with the same entry, so it gets the same boundary. A non-double
`--hermes` is refused before any file is read or process starts — also with
`--no-judge`, which used to skip the gate. Every judge call runs through `run_judge`
in `evals/runners/hermes.py`: a fresh neutral attempt directory, the verified copy,
`--unshare-net`, a scrubbed environment and a neutral HOME. The judge never inherits
the harness environment, writes nothing into the harness HOME, and its attempt
directory is removed in `finally`. When the sandbox refuses the judge (setup rc 2),
`measure` prints the reason and exits 2 instead of writing silent `unavailable` rows.
`evals/scripts/run_evals.py` and `evals/scripts/judge.py` contain no spawn site at all.
Three independent layers guard the route:

1. **Runtime audit hook (primary).** It runs the real CLI over every parser subcommand,
   every `store_true` flag *and* a second value for every non-boolean option that has a
   default (`--trials` 2→3, `--parallel` 1→2, `--run-id` `inv-alt`, …), with a non-double
   `--hermes`, and fails if any `subprocess.Popen`, `os.system`, `os.exec*`,
   `os.posix_spawn`, `os.fork`, `os.spawn*` or `pty.spawn` fires before the gate refuses;
   only `[python, scripts/checkpoint.py, gate, …]` may appear otherwise.
2. **Gate-first structure (evaluation order).** For every function that owns an
   `offline_gate(…)` call (discovered in the module, then pinned), every call the handler
   *evaluates* before the gate must be in a reviewed allow-list of pure helpers; the list
   lives in the test with one justification per entry. "Before" is evaluation order, not
   line order: the gate statement's own calls (its arguments, the other operand of
   `or`/`and`, conditional expressions) and the header of a compound statement whose body
   contains the gate are included, while a body that runs after the gate
   (`if not offline_gate(...):`) is not. A nested `def`/`lambda` counts through its
   decorators, default values and annotations (evaluated when the `def` runs, in the handler's frame), not
   through its body (R8d, found by the orchestrator: a line-based scope test had taken the
   `def` line, defaults included, for the body). A spawn written with `import os as _o` or
   `__import__('subprocess')`, or one guarded by an argument value the runtime sweep never
   produces, is a call that is not on the list. The check follows the handler's own body
   only; it does not descend into an allow-listed helper (see `cleanup()` below). It reads
   the handler's statement list and the `body` of the compound statement that holds the
   gate: a gate placed in an `else`/`elif`, `except` or `finally` branch, or inside a nested
   function, is **not** examined (found by the final audit, not fixed in 1.2; today no
   handler is written that way).
3. **AST tripwire (secondary).** It discovers aliases at any import depth (`import os as
   _o` inside a function, `from subprocess import run as r`), `getattr`, `importlib`,
   `__import__('subprocess')`, `mod.subprocess`, keyword `args=`, `*cmd`, f-strings and
   attribute references such as `RUN = subprocess.run`; anything unclassified fails.

None of the three proves the absence of a spawn that they cannot see: `ctypes` (the
documented example is `ctypes.CDLL(None).system(...)`), `multiprocessing`,
`concurrent.futures` and code built inside a string bypass the audit events and the
scanner, and a spawn inside an allow-listed helper (or behind a helper whose name the
reviewer accepted) is only covered if the runtime hook reaches it. The three layers
overlap by design; none of them is a credential boundary.

### The `bwrap=False` test seam

`run_one(..., bwrap=False)` exists for deterministic unit tests of the run
bookkeeping. It is selected only by `LONGHAUL_EVAL_TEST_SEAM=1` in the environment;
without it the runner raises `LauncherError` before anything is written or started,
and no file name, argv shape or flag can select it.

## Probe facts, not metadata

`run --probe` runs through the same runner-owned plan and reads child facts, never a
`wrapped` flag: PID 1 is bwrap, cwd and HOME are the neutral sandbox paths, `/tmp`
and `/run/user` are empty tmpfs mounts, the sandbox root lists only the own sandbox, a
decoy sibling stays invisible, every launcher-chain target is readable, and the host
file, host unix-socket and TCP canaries are all unreachable (the probe plan also uses
`--unshare-net`). The candidate skill is visible only under its installed root. A
missing, empty or malformed fact fails the probe; an expected chain target the
inspection never reported is a failure, not a pass.

The three canaries are created in a private host directory under `/tmp` **before** the
subject starts and are removed afterwards; the probe record stores all three
declarations, `probe_gate` refuses a record that does not declare them, and a
disabled (`canaries=False`), uncreatable or unreported canary is a dirty probe. The
canaries never live in a directory the plan binds — the offline state may be the
harness HOME, so binding it must not expose one.

The plan masks `/run/dbus` and `/run/systemd` as well: `--unshare-net` alone does not
close a host unix socket that is already visible under `/run`. The probe connects to
`/run/dbus/system_bus_socket` from inside; a successful connect is a dirty probe, and
the `/tmp` socket canary now only proves the `/tmp` mask (it is not a host-socket
proof).

What this boundary does **not** prove: `/proc` still describes the mount and pid
namespaces; the sandbox starts from a read-only host root bind, so host bind mounts and
readable host data outside the masked paths remain visible; mountinfo metadata is only
treated as a leak when the child can actually traverse the repository path (a fact the
probe reports); abstract sockets and a future live run's network are outside the
offline evidence; the `--die-with-parent` guarantee races with exec (PDEATHSIG is not a
delivery guarantee); and a legacy ledger with `started` rows but no owner stamp is only
reaped after an explicit refusal, never killed blind. Redaction of copied text stays a
best-effort filter and is **not** a containment proof. Do not describe this as full
isolation, and do not describe `--unshare-net` as "no host socket is reachable".

## Comparator limits (control-trivial, control-finish)

Both controls are only control cases. `control-trivial` wants `VERSION: 3.14.2` and
an answer that names exactly that version; `control-finish` wants `out/report.md` to
contain `total: 60` and three files unchanged. The comparator checks those files and
that regex — nothing more. A subject that reads the fixture can therefore satisfy
them without doing the task, and a subject that writes a correct report in a
different shape can fail them. They exist to punish blanket hedging ("always answer
incomplete"), not to measure capability; the ten failure-mode cases are the
measurement part. Do not read a high control score as evidence of skill quality, and
do not read a control failure as a subject defect without opening the work tree.

## Isolation

One throwaway profile per run (`lheval…`, deleted before the work tree is moved), scrubbed
environment, no hooks, no memory, no sessions, and every Hermes call (profile create, skills
list, chat, delete) runs inside the same runner-owned bwrap plan: read-only host root, tmpfs
over the real home(s), `/tmp`, `/var/tmp`, `/run/user`, `/run/dbus` and `/run/systemd`, only the
launcher chain, the attempt directory and the key-free state directory mounted, a new pid
namespace, `--unshare-net` and `--die-with-parent`. The candidate conditions load the skill under test; the baseline must
prove it carries none of it (preflight P2/P3). A run whose baseline mentions the skill is
flagged `contamination_suspect` and is not valid. The subject stream is subject-reported:
`-s longhaul` on the command line only preloads a skill, so a candidate counts as `confirmed`
only if a `skill_view` call has a successful tool result — and the report withholds every
numerical primary delta unless **all** candidate runs are confirmed **and** their recorded
skill fingerprint matches the pinned tree. A denied or unanswered `skill_view` never counts.

What this boundary does **not** do: the sandbox still starts from `--ro-bind / /`, so
readable host data outside the masked paths (other users' homes, `/etc`, `/usr`, service
state under `/var/lib`) remains visible, and `/proc/self/mountinfo` still describes the
mount namespaces (it is treated as a leak only when the child can traverse the repository
path). The probe checks named facts (`/tmp`, `/run/user`, the sandbox root, the launcher
chain, the host/socket/TCP canaries, a real connect attempt to the host system bus, the
repository-visibility fact, a `longhaul` name scan), not "the host is invisible". Offline
runs additionally use `--unshare-net`; the plan masks `/run/dbus` and `/run/systemd`, and
the probe proves the system-bus connect fails. That says nothing about the network of a
future live run, and abstract sockets are not covered. Do not describe this as full
isolation: it is a mount/pid boundary that keeps the subject's work paths separate, and the
live path is blocked precisely because no credential boundary is solved.

## Known residual risks (not fixed here)

- `oracle.run_checkpoint` runs the host checkpoint gate with the inherited host
  environment on a **subject-written** state file. The subject is finished by then and
  the output is at most a one-bit "missing or hash mismatch" oracle, but the call is not
  hermetic.
- A `measure` invocation killed with SIGKILL leaves its judge attempt directory behind
  (it is in no ledger, so `--cleanup` cannot see it). It contains fake data only.
- Key encodings the scan does not detect are listed above (base64/hex/split/UTF-32/
  binary, and non-ASCII UTF-16 outside the aligned/predominant case); the scan is a
  guard, not a credential boundary.
- The copy of the attempt-owned fake log into the run directory (`is_file` +
  `copyfile`) follows a symlink planted in the attempt directory. Today only the verified
  double runs there, so it is not exploitable now; it must be hardened (no-follow read
  plus an explicit regular-file check) before any live lane.
- `probe_gate` is a consistency check on a `probe.json` that may have been written by
  hand (including the legacy list form of `canaries`); it is not authentication of a
  probe run.
- `cleanup()` is an allow-listed pre-gate helper, and it is not a no-op: it sends SIGKILL
  to process groups and removes directories according to the ledger found in
  `--out-root` (a maintenance path on the run's own out-root, not a spawn). The runtime
  driver only reaches it with an empty ledger, so the filled-ledger path is read here,
  not measured; it deserves a review before any live lane.
- `classify` reads a stderr that contains `429` as its own token as `provider_429`, even when
  the token is a locator such as `file.py:429:7` or `pid 429` and not an HTTP status (the
  R8d fix removed only the substring cases: hex profile names, temp paths, ports, `line 429`).
  The status is retried, so the cost is one wasted attempt; a classification that must be
  exact would need the provider's structured error, not a regular expression over stderr.
- The test suite is not safe to run in two processes at once (pytest-xdist, two shells):
  the tests that exercise the real subject root share the fixed directory
  `/var/tmp/lheval-subject`, and one process can delete the decoy sibling that another
  has just planted (`test_live_probe_cannot_see_a_decoy_sibling` failed once in eight
  doubled runs). CI and the numbers above use one process.
- The test suite needs pytest's temporary directory outside `/tmp` and `/var/tmp`. The sandbox
  masks both, and the fake `hermes` entry of the tests lives under pytest's temporary
  directory, so with the default base (`/tmp/pytest-of-<user>`, what you get when none of
  `TMPDIR`, `TEMP`, `TMP` is set) six tests fail with `baseline: /tmp carries 1 host
  entry/entries`. Set `TMPDIR` or pytest's `PYTEST_DEBUG_TEMPROOT` to a directory elsewhere;
  CI sets the latter to `$RUNNER_TEMP`. One test (`test_launcher_chain_resolves_the_measured_roots`)
  skips on a host without `hermes`, so a runner shows `1 skipped`.
- `write_state` cleanup, two limits found by the final audit and left open in 1.2. (a) In the
  anchored case, when the unlink by leaf fails, the cleanup falls back to `os.unlink` by
  name at the path `mkstemp` returned, with no identity check; if the parent was swapped in
  that same window it can remove a foreign file that bears the temp's name (this needs write
  access to the parent directory). `write_state` may then raise a raw `FileNotFoundError`
  from `os.replace` instead of a `RecordError`; the caller path was not measured. (b) The
  `(st_dev, st_ino)` comparison in the non-anchored cleanup is exercised by a script during
  the audit but has no regression test of its own: a mutant that drops it survives the suite.
- Live evaluation stays blocked; no live run, key or model call is validated by any of
  the above.

<!-- generated:begin -->
Not generated yet. `run_evals.py report` rewrites this section; do not edit it by hand.
<!-- generated:end -->
