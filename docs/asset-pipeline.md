# Asset pack handoff inside Longhaul

Longhaul coordinates exploratory work across sessions. When a deliverable
includes generated visual assets, treat the handoff between the prompt author,
the builder and the verifier as its own checkpointed sub-task. The asset
pipeline is a specialist workflow, not a replacement for Longhaul's gate.

## Roles

| Role | Responsibility | Evidence |
|---|---|---|
| Prompt author | Write the asset contract and builder prompt; do not build | prompt file with model + effort |
| Builder | Produce raw assets and a manifest | raw stdout + generated files |
| Integrator | Normalize, map to UI, run static/tests | pack directory + integration test |
| Verifier | Parse geometry/palette, render, inspect | validator report + screenshot |

Use a frontier model only for the prompt. Use a cost-effective builder for the
volume. Never let the builder invent the palette, IDs or occlusion rules.

## Checkpoint sequence

1. **Contract** — create `A1`, the prompt/contract file. Record the exact model
   and effort. Acceptance: every asset ID is named.
2. **Build** — create `A2`, the raw builder output. Acceptance: every requested
   asset is present and parseable; fences/prose are not accepted as success.
3. **Normalize** — create `A3`, the pack plus manifest. Acceptance: manifest
   keys equal SVG IDs; one file per ID.
4. **Verify** — create `E1`, the deterministic validator report. Acceptance:
   exit 0, dimensions/palette/geometry match the contract.
5. **Review** — create `E2`, the rendered contact sheet and live UI screenshot.
   Acceptance: named visual defects are fixed or explicitly accepted.
6. **Gate** — write acceptance criteria that cite `E1` and `E2`, then run
   `scripts/checkpoint.py gate`. Publish only after the human gate.

## Resume rules

- Bind evidence to the artifact revision. Re-run the validator after any asset
  or manifest edit; a previous pass does not carry over.
- If the builder regresses an approved part while fixing another, keep the
  approved base and change only the affected region. Record the rejected pass.
- Do not auto-retry a nondeterministic model call merely because the prior
  output was rejected; change the prompt or the contract first.
- A model claiming “done” is not evidence. The validator and screenshot are.

## Boundaries

- Generated assets may need license review; record source and model terms.
- Longhaul's checkpoint gate checks bookkeeping and content hashes, not visual
  quality or semantic correctness.
- The reusable Hermes procedure lives in the `frontier-prompt-asset-pipeline`
  skill; this document is the Longhaul-specific checkpoint shape.
