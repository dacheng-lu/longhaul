# Decision depth: roles as methods, weighted matrix

Use this when ABCD alone is too thin: a consequential choice where at least two stakeholders want conflicting things, the route is hard to reverse, or three or more real options exist. Everything else stays with [grill.md](grill.md). Resolve facts with tools first; this file structures judgment, it does not replace evidence.

## Ambition check (once, before roles)

Answer each with yes/no and the consequence. "No" is a valid answer; plain speed is the default.

- **Altitude:** what should happen after this decision — the goal beyond the goal? Yes → restate the decision question. Weak for creative work.
- **Depth:** what would a field-leading professional do here that nobody asked for: rawer material, specific methods, an outside check? Yes → add the method to a role or criterion.
- **Range:** describe the decision with verbs (what it must achieve), then name the missing field (for example a red team). Yes → add that role.

Source: Felix Rieseberg, *How to be more ambitious* (2026-10-13), https://felixrieseberg.com/how-to-be-more-ambitious-a-tutorial-for-humans-with-models/ .

## Procedure

1. **Data inventory.** List what raw data and sources are actually reachable. Every later number maps to a line here or is marked as an assumption.
2. **Roles are methods (3–5, at most 6).** Pick by function: who decides, pays, operates, blocks, uses, sells, is liable. For each role record interest, silent assumption and the check it performs ("finance computes three-year cost from quote X"). Drop a role that has no executable check. Do not role-play voices: ask the model to *do* what the role does, not to *write* like it.
3. **Criteria and weights.** One or two criteria per role, weights summing to 100 %. Type each weight as *data* (from the inventory) or *assumption* (with a reason). Hard limits — law, policy, budget cap — filter options before scoring; they are not weights.
4. **Matrix.** Options (≤ 6) × criteria (≤ 6), scale `++ +2, + +1, 0, − −1, −− −2`, score = Σ weight × value. Mark divergence where roles differ by two steps or more; name it, never average it away.
5. **Sensitivity.** Shift the two heaviest weights by ±10 points (rest proportional). Record "robust" or "flips when …". This is the depth instrument when the data cannot carry statistics or simulation.
6. **Recommend in ABCD form.** The strongest routes become A–D in the [grill.md](grill.md) format with one recommendation. Add reversibility, the next action and a kill criterion (what would overturn the choice). Score is input; reversibility and hard limits weigh in.
7. **Outside judge.** The author does not validate its own recommendation. Use a different model, a pilot, a measurement or a named person. Until that result is observed, record it as `result: unknown` with a reason.

## Record with the checkpoint

Register the matrix as an `artifact`. Bind the sensitivity result and the outside judgment as `evidence` to that revision, and cite them from an acceptance criterion such as "recommendation holds under ±10-point weight shifts". Editing the matrix after new research then makes exactly that evidence stale — a matrix written before the research cannot silently survive it. If research runs in parallel, label the matrix provisional and state what changed in the revised one.

## Status

A proposal, not a measured result: no eval scenario covers this file yet. The SECI-style labels (shared practice → articulation → combination → practice) describe the structure only; simulated role statements are not tacit-to-tacit transfer (see [learning.md](learning.md)).
