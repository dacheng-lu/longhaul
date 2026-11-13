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

## Bind checkpoint evidence to an input

For evidence produced by a particular local-model case, hash the exact request
bytes (including any frozen retrieved context), then record the same run object
on the evidence and its acceptance criterion. The checkpoint gate compares all
three fields, so reusing evidence after changing only the input hash is refused.

Register the input as a supporting artifact first: the gate can only refuse bytes
it can hash, and `run.input_sha256` is a label the record carries, not a file the
gate reads. Without the second `artifact` step below, the record still passes
after `inputs/request.json` is edited.

```sh
python3 scripts/checkpoint.py artifact state.json --id A2 \
  --locator inputs/request.json --supporting
input_sha256=$(sha256sum inputs/request.json | cut -d ' ' -f 1)
python3 scripts/checkpoint.py evidence state.json --id E1 --artifact A1 --artifact A2 \
  --result pass --run-case d-valid-3 --run-model lfm3:1.7b \
  --run-input "$input_sha256"
```

Add this object to the criterion in `state.json` before running `gate`:

```json
{
  "id": "C1",
  "criterion": "The case output matches its expected result",
  "status": "verified",
  "evidence_ids": ["E1"],
  "run": {
    "case_id": "d-valid-3",
    "model": "lfm3:1.7b",
    "input_sha256": "<the SHA-256 printed above>"
  }
}
```

An artifact edit also makes evidence tied to its older revision stale, and
evidence with `result: fail` or `unknown` cannot satisfy a verified criterion.
These checks validate record consistency; they do not authenticate who ran the
model or prove the model's answer is correct. See `tests/test_run_binding.py`
for the same-input match and the changed-input refusal.

## Rules

- Every matrix label has exactly one metrics record; duplicates and unknown
  labels fail.
- A completed model has non-empty scores, non-empty gate results, all required
  suites at `observed >= required`, and at least one non-empty raw file.
- `required` is the contract's `cases` count. A metrics record may declare a higher
  bar (and must then meet it) but cannot lower what the frozen contract asked for.
- Raw paths must stay inside the run directory.
- Zero is a valid score. Empty or missing metrics are not.
- `not_run`, `failed` and `aborted` records are allowed and keep the run
  `incomplete`; they are never silently dropped from coverage.
- Quality gates are evaluated per model; a single false gate makes the overall
  quality status `fail`. Only the record's own gate *values* are read, and only
  for truthiness: the contract's `gates` object is bound into the run digest but
  its numbers are never compared with the metrics. Contract semantics are
  unspecified (the tests use a free-form `{"tg": 5}`), so a contract that says
  `accuracy: 0.9` and a record that says `0.1` still reports `pass`; this is
  characterised by `test_gate_values_are_truthiness_checked_not_compared`. Do
  not read a quality `pass` as a claim about contract thresholds.

## Not checked

- Whether a prompt, case or gate is scientifically right.
- Whether the model's answer is true; a domain oracle is still required.
- Who ran the evaluation. Hashes bind bytes, not identity.
- That a report renders those bytes; that is FlowLore's check.
