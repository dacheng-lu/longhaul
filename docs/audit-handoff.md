# Audit handoff v1: bounded completeness check

Use FlowLore to select the recipe, Longhaul to set scope and acceptance, and
Makeextreme to run the fixed audit-record stages. The optional local checker is
not an automatic runtime integration and never writes checkpoint acceptance.

```bash
python scripts/check_audit_handoff.py /path/to/task --head FULL_EXPECTED_COMMIT
```

Run Makeextreme's current record checks first. Pass the expected head from the
actual reviewed checkout, not from the handoff itself. `handoff.json` must have:

| Field | Meaning |
| --- | --- |
| `version` | Integer 1 |
| `head` | Expected full 40-character commit |
| `artifacts` | SHA-256 mapping for `validated.json` and `report.md` |
| `unresolved` | List of unresolved findings; must be empty to pass |
| `open_coverage` | List of open units; must be empty to pass |
| `limitations` | Nonempty list of limits on the evidence |

The checker also inspects the snapshot for hidden unresolved findings and open
coverage. A confirmed finding does not make this completeness check fail: deciding
whether its impact blocks release is a separate acceptance criterion. This check
does not validate the entire producer schema, replay evidence, authenticate a
reviewer, inspect current Git state, or prove report semantics. A caller must run
the producer checks and review the actual findings before recording acceptance.

After the checks and domain review, register exact report/snapshot revisions in
Longhaul's checkpoint helper. Use separate criteria for record consistency,
coverage completeness and acceptable findings. Keep failed or missing criteria
open. Never convert a model's completion message into acceptance automatically.

The offline Makeextreme `examples/audit_ladder/records/demo.py --longhaul PATH`
exercise demonstrates interruption, resume, report drift and source drift using
synthetic data. Cross-session durable storage and real audits remain unverified.

The local bundle must be trusted and quiescent. Hashes detect drift, not forgery;
this helper is not an authorization or race-resistant filesystem boundary.
