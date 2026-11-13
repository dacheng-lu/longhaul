#!/usr/bin/env python3
"""Verify a local-model evaluation run against its frozen contract.

This checks bookkeeping and evidence coherence, not whether a model is good.
Three outcomes are kept separate:

    execution_status     complete | partial | not_started
    evidence_status      valid | incomplete | invalid
    quality_gate_status  pass | fail | not_evaluated

A quality-gate failure is a successful verification of a failing model. An
invalid binding or missing raw trace is not. Exit code 0 means the verification
file was written, not that the model passed.

Usage:
    python3 scripts/eval_verify.py verify RUN_DIR [--check-artifacts] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BINDING_FIELDS = ("matrix", "suites", "scoring", "retrieval", "gates",
                  "failure_policy", "execution")
STATUSES = {"completed", "failed", "aborted", "not_run"}
HEX64 = set("0123456789abcdef")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sha256_file(path_: Path) -> str:
    h = hashlib.sha256()
    with path_.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def load(path_: Path, failures: list) -> dict | None:
    try:
        return json.loads(path_.read_text(encoding="utf-8"))
    except FileNotFoundError:
        failures.append(f"missing {path_.name}")
    except (OSError, ValueError) as exc:
        failures.append(f"cannot read {path_.name}: {exc}")
    return None


def require(cond, failures: list, message: str) -> None:
    if not cond:
        failures.append(message)


def validate_contract(c: dict, failures: list) -> None:
    require(isinstance(c, dict), failures, "contract is not an object")
    if not isinstance(c, dict):
        return
    for key in ("schema_version", "run_id", "created_at", "matrix", "suites",
                "scoring", "retrieval", "gates", "failure_policy", "execution"):
        require(key in c, failures, f"contract missing {key}")
    require(c.get("schema_version") == "1.0", failures, "contract schema_version must be 1.0")

    matrix = c.get("matrix")
    require(isinstance(matrix, list) and matrix, failures, "matrix must be a non-empty list")
    labels = []
    if isinstance(matrix, list):
        for i, m in enumerate(matrix):
            where = f"matrix[{i}]"
            require(isinstance(m, dict), failures, f"{where} is not an object")
            if not isinstance(m, dict):
                continue
            for key in ("label", "repo", "revision", "file", "bytes", "sha256",
                        "arch", "quant", "tier"):
                require(key in m, failures, f"{where} missing {key}")
            labels.append(m.get("label"))
            require(isinstance(m.get("bytes"), int) and m.get("bytes", 0) > 0,
                    failures, f"{where} bytes must be a positive integer")
            require(is_hex64(m.get("sha256")), failures, f"{where} sha256 is not 64 hex")
            require(m.get("tier") in ("full", "variant"), failures, f"{where} tier is invalid")
        require(len(labels) == len(set(labels)), failures, "matrix labels are not unique")

    suites = c.get("suites")
    require(isinstance(suites, list) and suites, failures, "suites must be a non-empty list")
    ids = []
    if isinstance(suites, list):
        for i, s in enumerate(suites):
            where = f"suites[{i}]"
            require(isinstance(s, dict), failures, f"{where} is not an object")
            if not isinstance(s, dict):
                continue
            require(bool(s.get("id")), failures, f"{where} missing id")
            require(isinstance(s.get("required"), bool), failures, f"{where} required must be bool")
            ids.append(s.get("id"))
            if "cases" in s:
                require(isinstance(s["cases"], int) and s["cases"] >= 0,
                        failures, f"{where} cases must be a non-negative integer")
            if "case_ids_sha256" in s:
                require(is_hex64(s["case_ids_sha256"]), failures,
                        f"{where} case_ids_sha256 is not 64 hex")
        require(len(ids) == len(set(ids)), failures, "suite ids are not unique")

    scoring = c.get("scoring")
    if isinstance(scoring, dict):
        require(bool(scoring.get("scorer")), failures, "scoring.scorer missing")
        require(bool(scoring.get("version")), failures, "scoring.version missing")
        require(isinstance(scoring.get("normalization"), list), failures,
                "scoring.normalization must be a list")
        if "scorer_sha256" in scoring:
            require(is_hex64(scoring["scorer_sha256"]), failures,
                    "scoring.scorer_sha256 is not 64 hex")
    else:
        failures.append("scoring must be an object")

    retrieval = c.get("retrieval")
    if isinstance(retrieval, dict):
        require(bool(retrieval.get("embedding_model")), failures,
                "retrieval.embedding_model missing")
        require(isinstance(retrieval.get("dim"), int) and retrieval.get("dim", 0) > 0,
                failures, "retrieval.dim must be a positive integer")
        for key in ("index_sha256", "case_ids_sha256"):
            if key in retrieval:
                require(is_hex64(retrieval[key]), failures, f"retrieval.{key} is not 64 hex")
    else:
        failures.append("retrieval must be an object")

    require(isinstance(c.get("gates"), dict), failures, "gates must be an object")
    require(isinstance(c.get("failure_policy"), str) and c["failure_policy"],
            failures, "failure_policy must be a non-empty string")

    execution = c.get("execution")
    if isinstance(execution, dict):
        require(is_hex64(execution.get("snapshot_sha256")), failures,
                "execution.snapshot_sha256 is not 64 hex")
        require(bool(execution.get("server")), failures, "execution.server missing")
    else:
        failures.append("execution must be an object")


def validate_run(run: dict, contract: dict, contract_sha: str, failures: list) -> str:
    for key in ("schema_version", "run_id", "contract_sha256", "binding_digest", "created_at"):
        require(key in run, failures, f"run.json missing {key}")
    require(run.get("schema_version") == "1.0", failures, "run schema_version must be 1.0")
    require(run.get("contract_sha256") == contract_sha, failures,
            "run.contract_sha256 does not match contract.json bytes")
    expected = digest({k: contract.get(k) for k in BINDING_FIELDS})
    if run.get("binding_digest") != expected:
        failures.append("run.binding_digest does not match the contract binding")
    if run.get("run_id") != contract.get("run_id"):
        failures.append("run.run_id differs from contract.run_id")
    return expected


def resolve_raw(run_dir: Path, rel: str, failures: list, label: str) -> bool:
    if not isinstance(rel, str) or not rel:
        failures.append(f"{label}: raw path is empty")
        return False
    path_ = (run_dir / rel).resolve()
    try:
        path_.relative_to(run_dir.resolve())
    except ValueError:
        failures.append(f"{label}: raw path escapes the run directory: {rel}")
        return False
    if not path_.is_file():
        failures.append(f"{label}: raw file missing: {rel}")
        return False
    if path_.stat().st_size == 0:
        failures.append(f"{label}: raw file is empty: {rel}")
        return False
    return True


def verify(run_dir: Path, check_artifacts: bool, artifacts_dir: Path | None) -> dict:
    failures: list[str] = []
    contract_path = run_dir / "contract.json"
    run_path = run_dir / "run.json"
    metrics_path = run_dir / "metrics.json"

    contract = load(contract_path, failures)
    run = load(run_path, failures)
    metrics = load(metrics_path, failures)

    contract_sha = ""
    binding = ""
    if contract_path.is_file():
        contract_sha = sha256_file(contract_path)
    if isinstance(contract, dict):
        validate_contract(contract, failures)
    if isinstance(contract, dict) and isinstance(run, dict):
        binding = validate_run(run, contract, contract_sha, failures)

    matrix = contract.get("matrix", []) if isinstance(contract, dict) else []
    suites = contract.get("suites", []) if isinstance(contract, dict) else []
    required = {s.get("id"): s.get("cases") for s in suites
                if isinstance(s, dict) and s.get("required")}
    matrix_labels = [m.get("label") for m in matrix if isinstance(m, dict)]

    models: dict[str, dict] = {}
    if isinstance(metrics, dict):
        require(metrics.get("schema_version") == "1.0", failures,
                "metrics schema_version must be 1.0")
        require(metrics.get("run_id") == (contract or {}).get("run_id"), failures,
                "metrics.run_id differs from contract.run_id")
        entries = metrics.get("models")
        require(isinstance(entries, list) and entries, failures,
                "metrics.models must be a non-empty list")
        if isinstance(entries, list):
            for e in entries:
                require(isinstance(e, dict), failures, "metrics entry is not an object")
                if not isinstance(e, dict):
                    continue
                label = e.get("label")
                if not isinstance(label, str) or not label:
                    failures.append("metrics entry has no valid label")
                    continue
                if label not in matrix_labels:
                    failures.append(f"metrics label not in matrix: {label}")
                    continue
                if label in models:
                    failures.append(f"duplicate metrics entry: {label}")
                    continue
                status = e.get("execution_status")
                if status not in STATUSES:
                    failures.append(f"{label}: invalid execution_status {status!r}")
                coverage: dict = e.get("coverage") if isinstance(e.get("coverage"), dict) else {}
                raw_ok = True
                raw = e.get("raw")
                if status == "completed":
                    if not isinstance(raw, list) or not raw:
                        failures.append(f"{label}: completed model has no raw trace list")
                        raw_ok = False
                    else:
                        for rel in raw:
                            raw_ok &= resolve_raw(run_dir, rel, failures, label)
                    if not isinstance(e.get("scores"), dict) or not e["scores"]:
                        failures.append(f"{label}: completed model has empty scores")
                    if not isinstance(e.get("gates"), dict) or not e["gates"]:
                        failures.append(f"{label}: completed model has no gate results")
                    for sid, expected_n in required.items():
                        cov = coverage.get(sid)
                        if not isinstance(cov, dict):
                            failures.append(f"{label}: missing coverage for {sid}")
                            continue
                        # The contract is the frozen requirement. A metrics
                        # record that declares a smaller `required` describes its
                        # own bar, so it can raise what must be shown but never
                        # lower what the contract asked for.
                        req = (expected_n if isinstance(expected_n, int)
                               and not isinstance(expected_n, bool) else None)
                        declared = cov.get("required")
                        if isinstance(declared, int) and not isinstance(declared, bool):
                            req = declared if req is None else max(req, declared)
                        obs = cov.get("observed")
                        if not isinstance(obs, int) or isinstance(obs, bool) or obs < 0:
                            failures.append(f"{label}: invalid observed count for {sid}")
                        elif isinstance(req, int) and obs < req:
                            failures.append(
                                f"{label}: coverage {sid} {obs}/{req} is incomplete")
                elif status in ("failed", "aborted", "not_run"):
                    pass
                models[label] = {"execution_status": status, "coverage": coverage,
                                 "raw_ok": raw_ok if status == "completed" else None,
                                 "gates": e.get("gates", {})}
        for label in matrix_labels:
            if label not in models:
                failures.append(f"matrix model has no record: {label}")

    if check_artifacts and artifacts_dir is not None:
        for m in matrix:
            if not isinstance(m, dict):
                continue
            path_ = artifacts_dir / str(m.get("file", ""))
            if not path_.is_file():
                failures.append(f"artifact missing: {m.get('file')}")
                continue
            if path_.stat().st_size != m.get("bytes"):
                failures.append(f"artifact size mismatch: {m.get('file')}")
                continue
            if sha256_file(path_) != m.get("sha256"):
                failures.append(f"artifact sha256 mismatch: {m.get('file')}")

    completed = [v for v in models.values() if v["execution_status"] == "completed"]
    if not models:
        execution_status = "not_started"
    elif len(completed) == len(models):
        execution_status = "complete"
    elif completed:
        execution_status = "partial"
    else:
        execution_status = "not_started"

    if failures:
        evidence_status = "invalid"
    elif execution_status != "complete":
        evidence_status = "incomplete"
    else:
        evidence_status = "valid"

    if not completed:
        quality = "not_evaluated"
    elif any(not all(bool(v) for v in m["gates"].values()) for m in completed):
        quality = "fail"
    else:
        quality = "pass"

    return {
        "schema_version": "1.0",
        "run_id": (contract or {}).get("run_id"),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "contract_sha256": contract_sha,
        "binding_digest": binding,
        "execution_status": execution_status,
        "evidence_status": evidence_status,
        "quality_gate_status": quality,
        "models": models,
        "failures": failures,
        "artifacts_checked": bool(check_artifacts and artifacts_dir is not None),
    }


def write_atomic(path_: Path, data: dict) -> None:
    path_.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path_.parent, prefix=path_.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path_)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a frozen local-model evaluation run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify RUN_DIR and write verification.json")
    v.add_argument("run_dir", type=Path)
    v.add_argument("--check-artifacts", action="store_true",
                   help="hash model files against the matrix")
    v.add_argument("--artifacts-dir", type=Path, default=None)
    v.add_argument("--json", action="store_true", help="print the full result")
    args = ap.parse_args()

    if not args.run_dir.is_dir():
        print(f"not a directory: {args.run_dir}", file=sys.stderr)
        return 2
    result = verify(args.run_dir, args.check_artifacts, args.artifacts_dir)
    write_atomic(args.run_dir / "verification.json", result)
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print(f"execution={result['execution_status']} "
              f"evidence={result['evidence_status']} "
              f"quality={result['quality_gate_status']}")
        for failure in result["failures"]:
            print(f"  - {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
