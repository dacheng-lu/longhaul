# Checkpoint contract

The Python standard-library helper executes no stored commands and makes no network requests.
- init PATH --task-id ID --goal TEXT: create exclusively; refuse overwrite.
- validate PATH: check fields and references.
- gate PATH: additionally require all acceptance criteria verified for current revision, deliverables present and local hashes intact. Does not assess semantic truth.

Fill acceptance, deliverables and artifact_revision before substantive milestones. Keep task_id stable. Change artifact_revision whenever deliverables or criteria change and invalidate affected evidence. This conservative v1 gate requires evidence to match the overall revision.

Acceptance entries: id, criterion, status (pending/verified/failed), evidence_ids.
Evidence entries: id, kind (local/external), locator, artifact_revision, observed_at (ISO-8601 with timezone), result (pass/fail). Add sha256 for local files; add summary for external evidence.
Deliverables: kind (local/external), locator, sha256 for local files.

Resolve relative local paths against the state file directory. Use final file hashes, not directories. Observe external evidence/results with actual tools: the helper cannot validate remote existence or truth. Never invent records to satisfy the gate.

Keep milestones, attempts, decisions, capabilities and lessons as concise task-specific arrays. The helper does not enforce their inner schema. Record failed attempts with error and changed hypothesis. Keep blockers explicit and next_action concrete.

Use safe edits. For whole-file updates write a sibling temporary file, validate it, then atomically replace the owned state. Persist via the host's durable storage workflow and retain its returned locator. Keep all task state outside the skill folder; redact secrets.

On recovery inspect actual artifacts, not only records. A passed gate is bookkeeping, not semantic verification. Mark complete only after the gate and domain-specific checks pass; otherwise provide an in-progress/blocked handoff.

