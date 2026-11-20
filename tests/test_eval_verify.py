"""Behaviour tests for eval_verify: evidence validity and quality are separate.

Each test mutates one rule and demands the matching status. A zero score is a
valid result; an empty file or a short coverage count is not.
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "eval_verify.py"
SPEC = importlib.util.spec_from_file_location("eval_verify", SCRIPT)
EV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EV)
NOW = "2026-11-09T09:00:00Z"


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_run(tmp_path: Path, *, gates=None, scores=None, coverage=None,
             status="completed", raw=True, contract_mut=None,
             sha256="b" * 64) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    contract = {
        "schema_version": "1.0",
        "run_id": "r-1",
        "created_at": NOW,
        "matrix": [{
            "label": "m1", "repo": "org/repo", "revision": "abc", "file": "m1.gguf",
            "bytes": 4, "sha256": sha256, "arch": "qwen", "quant": "Q4_K_M",
            "tier": "full",
        }],
        "suites": [
            {"id": "extra", "required": True, "cases": 3},
            {"id": "wiki", "required": True, "cases": 2},
        ],
        "scoring": {"scorer": "eval", "version": "1", "normalization": []},
        "retrieval": {"embedding_model": "nomic-embed-text", "dim": 768},
        "gates": {"tg": 5},
        "failure_policy": "not_run_scores_zero_kept_in_denominator",
        "execution": {"snapshot_sha256": "c" * 64, "server": "llama.cpp"},
    }
    if contract_mut:
        contract_mut(contract)
    cpath = write(run / "contract.json", contract)
    csha = hashlib.sha256(cpath.read_bytes()).hexdigest()
    binding = EV.digest({k: contract.get(k) for k in EV.BINDING_FIELDS})
    write(run / "run.json", {
        "schema_version": "1.0", "run_id": "r-1", "contract_sha256": csha,
        "binding_digest": binding, "created_at": NOW,
    })
    if raw:
        (run / "attempts" / "m1").mkdir(parents=True)
        (run / "attempts" / "m1" / "raw.json").write_text('{"ok": true}\n')
    write(run / "metrics.json", {
        "schema_version": "1.0", "run_id": "r-1",
        "models": [{
            "label": "m1", "attempt_id": "a1", "execution_status": status,
            "coverage": coverage if coverage is not None else {
                "extra": {"required": 3, "observed": 3},
                "wiki": {"required": 2, "observed": 2},
            },
            "scores": {"total": 0} if scores is None else scores,
            "gates": {"tg": True} if gates is None else gates,
            "raw": ["attempts/m1/raw.json"] if raw else [],
        }],
    })
    return run


def verify(run: Path, *args):
    proc = subprocess.run([sys.executable, str(SCRIPT), "verify", str(run), *args],
                          capture_output=True, text=True)
    data = json.loads((run / "verification.json").read_text())
    return proc, data


def test_valid_run_passes_with_zero_score(tmp_path):
    proc, data = verify(make_run(tmp_path, scores={"total": 0}))
    assert proc.returncode == 0
    assert data["execution_status"] == "complete"
    assert data["evidence_status"] == "valid"
    assert data["quality_gate_status"] == "pass"
    assert data["failures"] == []


def test_failed_gate_is_quality_failure_not_evidence_failure(tmp_path):
    proc, data = verify(make_run(tmp_path, gates={"tg": False}))
    assert proc.returncode == 0
    assert data["evidence_status"] == "valid"
    assert data["quality_gate_status"] == "fail"


def test_empty_metrics_is_invalid(tmp_path):
    run = make_run(tmp_path)
    write(run / "metrics.json", {"schema_version": "1.0", "run_id": "r-1", "models": []})
    proc, data = verify(run)
    assert proc.returncode == 0
    assert data["evidence_status"] == "invalid"
    assert any("non-empty" in f for f in data["failures"])


def test_short_coverage_is_invalid(tmp_path):
    _, data = verify(make_run(tmp_path, coverage={
        "extra": {"required": 3, "observed": 2},
        "wiki": {"required": 2, "observed": 2},
    }))
    assert data["evidence_status"] == "invalid"
    assert any("coverage extra 2/3" in f for f in data["failures"])


def test_missing_raw_is_invalid(tmp_path):
    _, data = verify(make_run(tmp_path, raw=False))
    assert data["evidence_status"] == "invalid"
    assert any("raw trace" in f for f in data["failures"])


def test_contract_hash_mismatch_is_invalid(tmp_path):
    run = make_run(tmp_path)
    runj = json.loads((run / "run.json").read_text())
    runj["contract_sha256"] = "d" * 64
    write(run / "run.json", runj)
    _, data = verify(run)
    assert data["evidence_status"] == "invalid"
    assert any("contract_sha256" in f for f in data["failures"])


def test_binding_mismatch_is_invalid(tmp_path):
    run = make_run(tmp_path)
    runj = json.loads((run / "run.json").read_text())
    runj["binding_digest"] = "e" * 64
    write(run / "run.json", runj)
    _, data = verify(run)
    assert any("binding_digest" in f for f in data["failures"])


def test_incomplete_models_are_reported_not_scored(tmp_path):
    _, data = verify(make_run(tmp_path, status="not_run"))
    assert data["execution_status"] == "not_started"
    assert data["evidence_status"] == "incomplete"
    assert data["quality_gate_status"] == "not_evaluated"


def test_artifact_hash_check(tmp_path):
    run = make_run(tmp_path, sha256=hashlib.sha256(b"abcd").hexdigest())
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "m1.gguf").write_bytes(b"abcd")
    _, good = verify(run, "--check-artifacts", "--artifacts-dir", str(art))
    assert good["evidence_status"] == "valid"
    (art / "m1.gguf").write_bytes(b"abce")
    _, bad = verify(run, "--check-artifacts", "--artifacts-dir", str(art))
    assert bad["evidence_status"] == "invalid"
    assert any("sha256 mismatch" in f for f in bad["failures"])


def test_duplicate_metric_label_is_invalid(tmp_path):
    run = make_run(tmp_path)
    m = json.loads((run / "metrics.json").read_text())
    m["models"].append(dict(m["models"][0]))
    write(run / "metrics.json", m)
    _, data = verify(run)
    assert any("duplicate metrics entry" in f for f in data["failures"])
