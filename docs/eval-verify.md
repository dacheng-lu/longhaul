# Local-model evaluation verification

`scripts/eval_verify.py` checks a frozen local-model evaluation run. It is about
bookkeeping and evidence coherence, not about whether a model is good.

It answers three separate questions:

- `execution_status`: did every matrix entry finish? `complete | partial | not_started`
- `evidence_status`: is the record coherent? `valid | incomplete | invalid`
- `quality_gate_status`: did the finished models pass the declared gates?
  `pass | fail | not_evaluated`

A model that finished and failed a gate is a **valid** run with a quality
failure. A missing raw trace, a short coverage count or a changed contract is an
**invalid** run. Exit code 0 means `verification.json` was written; it does not
mean the model passed.

## Run directory

```text
run/
  contract.json      frozen matrix, suites, scorer, retrieval, gates, execution
  run.json           contract sha256 and binding digest
  metrics.json       one record per matrix label, including not_run
  attempts/          raw requests, responses, timings, logs
  verification.json  written by this tool
```

`contract.schema.json` is in `schemas/`. The binding digest is the SHA-256 of the
canonical JSON of `matrix, suites, scoring, retrieval, gates, failure_policy,
execution`; changing any of those invalidates `run.json`.

## Usage

```sh
python3 scripts/eval_verify.py verify path/to/run
python3 scripts/eval_verify.py verify path/to/run --check-artifacts --artifacts-dir /models
python3 scripts/eval_verify.py verify path/to/run --json
```

`--check-artifacts` hashes each model file and compares exact bytes and SHA-256
with the matrix. It is opt-in because a model set can be tens of gigabytes.

## Rules

- Every matrix label has exactly one metrics record; duplicates and unknown
  labels fail.
- A completed model has non-empty scores, non-empty gate results, all required
  suites at `observed >= required`, and at least one non-empty raw file.
- Raw paths must stay inside the run directory.
- Zero is a valid score. Empty or missing metrics are not.
- `not_run`, `failed` and `aborted` records are allowed and keep the run
  `incomplete`; they are never silently dropped from coverage.
- Quality gates are evaluated per model; a single false gate makes the overall
  quality status `fail`.

## Not checked

- Whether a prompt, case or gate is scientifically right.
- Whether the model's answer is true; a domain oracle is still required.
- Who ran the evaluation. Hashes bind bytes, not identity.
- That a report renders those bytes; that is FlowLore's check.
