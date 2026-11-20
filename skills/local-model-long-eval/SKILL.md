---
name: local-model-long-eval
description: "Run or resume long local-model evaluations with evidence. Use for reproducible local model matrices, checkpoint recovery, and raw-trace scoring."
---

# Local Model Long Evaluation

## When to use

Use when running, resuming, or auditing a long local-model evaluation
whose conclusions must be traceable to exact artifacts and raw outputs.
Support partial runs without presenting them as completed evaluations.

Longhaul owns criteria, evidence validation, and run binding.
An execution adapter may delegate scheduling to Makeextreme.
A reporting adapter may delegate rendering to FlowLore.
If an adapter is unavailable, preserve the same file contracts and state
transitions; do not assume an undocumented CLI exists.

## Inputs

Require these versioned inputs before scoring:

- Model matrix: label, repository, immutable revision, filename,
  exact bytes, expected SHA-256 and its source, architecture,
  quantization where applicable, and tier (`full` or `variant`).
- Evaluation specification: suites, case IDs, required coverage,
  prompts, scoring rules, normalization rules, gates, and timeout policy.
- Frozen retrieval inputs: corpus, queries, retrieved contexts and order,
  plus hashes; embedding model identity, dimension, and index provenance.
- Execution identity: evaluator and server revisions or binary hashes,
  dependency versions, adapter version, and immutable script snapshot.
- External machine profile: paths, ports, resource limits, concurrency
  exclusions, launch settings, health deadlines, and retry limits.

Resolve the launch plan before running scored cases. Record the resolved
configuration and hash it. A changed scored configuration creates a new
run binding; do not silently combine incompatible attempts.

## Invariants

1. One writer per canonical model artifact across all cooperating queues.
   Hold the artifact lock throughout download, resume, verification,
   atomic promotion, and completion-marker creation. Use storage with
   reliable locking, or an equivalent single-writer coordinator.

2. A model is ready only after exact size and SHA-256 verification.
   Atomically write a structured `.ok` marker containing artifact
   identity, bytes, digest, and verification time. Revalidate on reuse
   unless the verified artifact is protected by immutable storage.

3. Admit only one model evaluation at a time on the assigned resource
   pool. Coordinate downloads and embedding workloads through the same
   resource admission policy. A queue lock alone does not protect against
   unrelated download writers.

4. File existence is not completion. An extra-results JSON file must have
   nonempty, schema-valid metrics, expected case coverage, valid raw-trace
   references, and a matching run binding before that stage is reusable.
   A complete run also requires every required suite and verification step.
   Zero is a valid metric value; missing or empty metrics are not.

5. Bind every attempt to model bytes, evaluation inputs, frozen retrieval,
   execution snapshot, and resolved configuration. Preserve failed attempts;
   never overwrite their traces with a retry.

6. Run from an immutable snapshot. Never replace executing scripts through
   scp, deployment, or synchronization. Stage updates separately and activate
   them for a subsequent attempt.

7. Save raw requests, responses, errors, timing data, and resource samples.
   Derive scores from raw evidence with a pinned scorer. Never trust a
   model's self-reported score.

8. Separate execution completion, evidence validity, and quality-gate pass.
   A model may finish correctly and fail a quality gate. An aborted case
   must remain visible in coverage and denominator accounting.

9. Scan publishable artifacts for secrets before release. Keep sensitive
   originals access-controlled; create traceable sanitized derivatives
   rather than silently rewriting sealed evidence.

## Step-by-step

1. **Freeze the contract.**
   Assign a run ID. Snapshot inputs and execution code. Pre-register the
   ordered case-ID list and its hash before observing scored outputs.
   Define required suites per tier, denominators, failure treatment,
   normalization, quality gates, and retry budgets.

2. **Acquire artifacts.**
   Lock each canonical destination. Download to a partial file.
   Use `curl -C -` only when the remote object and range behavior permit
   reliable resume. Check expected bytes and SHA-256, atomically promote
   the verified file, then write `.ok`.
   Quarantine mismatches and retry within the declared budget.

3. **Admit or resume work.**
   Acquire the queue/resource lock. Validate checkpoints and their binding.
   Reuse only verified completed stages. Wait for valid model markers with
   a bounded deadline. Record blocked, failed, and aborted states explicitly.

4. **Preflight resources and dependencies.**
   Inspect available RAM, usable GPU memory, swap/zram usage and compression,
   disk capacity, and competing workloads.
   Pause downloads when the profile requires it.
   Confirm the embedding service has the required model loaded by making
   a real embedding request and checking dimension and usable output.
   Repeat dependency checks after service restarts.

   Historical profile values were: at least 3 GB MemAvailable for admission,
   paused downloads for models larger than 6 GB, and no overlapping model
   inference and embedding workload. These are profile values, not portable
   constants. Use declared units and actual headroom, including download
   cache, context/KV allocation, and GPU carveouts.

5. **Start the model service.**
   Launch a tracked process group from the execution snapshot.
   Record command arguments, environment overrides, and binary identity.
   Poll health with a deadline, then make a representative inference probe.

   The source profile used llama.cpp, context 8192, four threads, Jinja,
   reasoning budget zero, temperature zero, and seed 42.
   Resolve GPU offload from the hardware profile and preflight measurements;
   file-size heuristics are only an initial estimate.
   Record effective request settings and template identity. A fixed seed
   does not guarantee identical output across backends or hardware.

6. **Run the declared extra suites.**
   The source suite consists of:
   - 4K speed: prompt processing, token generation, and peak RSS.
   - Instruction following: eight Chinese, English, and German cases.
   - Strict JSON: ten documents.
   - 4K NIAH: five needles.
   - Tool-schema compliance: ten cases.

   Define tokenization, context construction, warmup, repetitions, and
   measurement units in the evaluation specification.
   For JSON, parse strictly first. Apply only predeclared field-level
   numeric-string normalization for semantic scoring. Report strict schema
   compliance separately when the schema requires numeric types.
   Store every raw output and timing record before committing a checkpoint.

7. **Run frozen wiki evaluation.**
   Use the pre-registered stratified 64-case ID file with
   `p0_run --ids-file <case-file>` when that evaluator is available.
   Pin the source embedding configuration: Ollama nomic-embed-text, 768
   dimensions, plus its actual model identity and index provenance.
   Replay the frozen retrieval results; if live embedding is still required,
   validate it before admitting the suite.

   For the source Gemma/wiki setup, explicitly send:
   `chat_template_kwargs: {"enable_thinking": false}`.
   Do not assume a server-level reasoning budget disables template thinking.
   For other backends, use a supported equivalent and verify its behavior.
   If the required mode cannot be enforced, block the affected comparison
   or register a separate variant before scoring.

8. **Monitor and stop.**
   Enforce stage deadlines and the profile's low-memory watchdog.
   The historical emergency threshold was MemAvailable below 800 MB.
   On termination, mark the attempt aborted, stop its process group,
   escalate after a bounded grace period, and confirm resource release.
   Shut down the model service before admitting the next model.

9. **Recompute and verify.**
   Recompute metrics from raw traces using the pinned scorer.
   Check coverage, denominators, bindings, artifact hashes, and gates.
   Keep full and variant results distinguishable.
   Produce a machine-readable verification result with explicit failures.

10. **Render and package.**
    Render REPORT.md, summary.json, and CSV from verified results.
    Check report claims against evidence and hash the final output bytes.
    Package code revisions, model hashes, resolved commands, raw traces,
    supported scope, and known failures. Run the secret scan on the final
    release candidate and seal its manifest.
    Publish only within the user's existing authorization.

## Failure handling

- **Concurrent append or oversized file:** stop competing writers,
  quarantine the artifact, invalidate its marker, and reacquire under
  the artifact lock. Do not repair corruption by arbitrary truncation.
- **Empty metrics:** reject the checkpoint and rerun the incomplete stage
  in a new attempt directory.
- **Embedding 404 after restart:** open a dependency circuit breaker after
  the first confirmed dependency failure. Restore the pinned model and
  pass a probe before retrying; do not send all 64 requests into failure.
- **Memory thrash:** pause competing workloads and abort if limits are
  exceeded. Reduced context or changed offload requires a new recorded
  configuration; do not label an 8K failure as a successful 4K run.
- **Poor zram compression:** use observed effective headroom, not nominal
  zram size. A 1.22:1 ratio does not provide another RAM-sized capacity pool.
- **Unexpected EOF in executing code:** retain the attempt as failed,
  inspect snapshot integrity, and restart from verified immutable code.
- **Thinking consumes the budget:** preserve the failed trace, verify the
  effective template/request behavior, and retry under a new binding if
  settings change.
- **Network or driver wedge:** reduce load and apply a bounded reconnect
  policy. Do not loop rfkill toggles. Module unloading and reboot belong
  to an external machine-specific runbook and require applicable
  authorization; record the interruption before recovery.
- **Exhausted retry budget:** stop the affected stage, retain evidence,
  and report the blocking reason. Never retry indefinitely.

## Artifacts

Use a run directory with these logical artifacts:

- `contract.json`: evaluation criteria, suite coverage, gates, and tiers.
- `inputs/`: matrix, case IDs, frozen retrieval, and resolved configuration.
- `run.json`: run ID, schema version, binding hashes, and execution identity.
- `journal.jsonl`: stage transitions, attempt IDs, reasons, and timestamps.
- `attempts/`: immutable raw requests, responses, errors, timings, and logs.
- `checkpoints/`: atomic completion records with evidence references.
- `metrics.json`: raw-derived results with denominators and scorer identity.
- `verification.json`: evidence validity, coverage, and quality-gate status.
- `REPORT.md`, `summary.json`, `summary.csv`: rendered deliverables.
- `release/manifest.json`: exact released file paths, sizes, and hashes.
- `release/secret-scan.json`: scan result bound to the release candidate.

Keep model binaries in a separately verified artifact cache.
Exclude a manifest's own hash from its file inventory; store the manifest
digest in a separate seal. Final verification and scan records must identify
the exact input manifest digest they evaluated.

## Checklist

- [ ] Matrix identities, exact bytes, digests, and tiers are frozen.
- [ ] Criteria, case IDs, retrieval, and scorer are pre-registered.
- [ ] Execution code and resolved configuration are immutable and bound.
- [ ] Artifact writers and resource consumers share effective locks.
- [ ] Readiness markers and reusable checkpoints are validated.
- [ ] Resource and dependency probes pass before scored requests.
- [ ] Effective thinking/template behavior is verified.
- [ ] Raw traces, timings, failures, and denominators are preserved.
- [ ] Services and child processes are stopped after each attempt.
- [ ] Metrics are recomputed; evidence validity and quality gates are separate.
- [ ] Reports match evidence and final-byte hashes.
- [ ] Release scope, known failures, and secret-scan results are recorded.
