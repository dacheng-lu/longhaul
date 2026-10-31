# Changelog

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
