"""Optional run binding: byte-identical evidence from another run cannot complete a criterion.

The baseline this guards: two cases can produce the same artifact bytes, so the
content revision is equal and a checkpoint that cites the wrong case's evidence is
coherent as far as v2's revision contract goes. The `run` object is bookkeeping, not
authentication: it stops an accidental cross-case swap, it cannot prove who ran what.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "checkpoint.py"
NOW = "2026-11-08T16:00:00Z"
TEXT = "release digest\n"
SHA = hashlib.sha256(TEXT.encode()).hexdigest()
REV = SHA[:12]

RUN_A = {"case_id": "d-valid-3", "model": "lfm3:1.7b", "input_sha256": "a" * 64}
RUN_B = {"case_id": "d-valid-6", "model": "lfm3:1.7b", "input_sha256": "b" * 64}


def record(*, evidence_run=None, criterion_run=None, status="verified", result="pass"):
    evidence = {"id": "E1", "kind": "local", "locator": "report.md", "artifact_ids": ["A1"],
                "artifact_revisions": {"A1": REV}, "observed_at": NOW, "result": result,
                "summary": "compared bytes"}
    criterion = {"id": "C1", "criterion": "Digest matches expected", "status": status,
                 "evidence_ids": ["E1"]}
    if evidence_run is not None:
        evidence["run"] = evidence_run
    if criterion_run is not None:
        criterion["run"] = criterion_run
    return {"schema_version": 2, "task_id": "run-binding", "goal": "Publish the digest",
            "status": "in_progress", "next_action": "Run the gate",
            "provenance": {"generated_at": NOW, "writer": "test"},
            "constraints": [], "milestones": [], "acceptance": [criterion],
            "artifacts": [{"id": "A1", "kind": "local", "locator": "report.md",
                           "revision": REV, "sha256": SHA}],
            "evidence": [evidence], "blockers": [], "attempts": [], "decisions": [],
            "capabilities": [], "lessons": [], "shortcuts": [], "waivers": []}


def call(command, doc):
    with tempfile.TemporaryDirectory(prefix="lh-run-") as d:
        Path(d, "report.md").write_text(TEXT)
        path = Path(d, "state.json")
        path.write_text(json.dumps(doc))
        proc = subprocess.run([sys.executable, str(SCRIPT), command, str(path)],
                              capture_output=True, text=True)
        payload = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
        return proc.returncode, payload, path.read_text()


class RunBinding(unittest.TestCase):
    def test_without_bindings_the_same_record_still_passes(self):
        # Backward compatibility, and the gap this feature closes: the swap is
        # invisible to the content revision alone.
        code, payload, _ = call("gate", record())
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])

    def test_evidence_and_criterion_from_the_same_run_pass(self):
        code, payload, _ = call("gate", record(evidence_run=RUN_A, criterion_run=RUN_A))
        self.assertEqual(code, 0, payload)

    def test_evidence_from_another_case_cannot_complete_the_criterion(self):
        code, payload, _ = call("gate", record(evidence_run=RUN_B, criterion_run=RUN_A))
        self.assertEqual(code, 1)
        self.assertIn("is not from the criterion's run", " ".join(payload["errors"]))

    def test_a_criterion_with_a_run_needs_bound_evidence(self):
        code, payload, _ = call("gate", record(criterion_run=RUN_A))
        self.assertEqual(code, 1)
        self.assertIn("is not from the criterion's run", " ".join(payload["errors"]))

    def test_malformed_run_shapes_are_refused_before_the_gate(self):
        for bad in ({}, {"case_id": "x"}, dict(RUN_A, extra="x"), dict(RUN_A, model="")):
            code, payload, _ = call("validate", record(evidence_run=bad))
            self.assertEqual(code, 1, bad)
            self.assertIn("run must map exactly", " ".join(payload["errors"]), bad)
            code, payload, _ = call("validate", record(criterion_run=bad))
            self.assertEqual(code, 1, bad)
            self.assertIn("run must map exactly", " ".join(payload["errors"]), bad)

    def test_legacy_records_without_a_run_are_untouched(self):
        with tempfile.TemporaryDirectory(prefix="lh-run-") as d:
            Path(d, "report.md").write_text(TEXT)
            path = Path(d, "state.json")
            path.write_text(json.dumps(record()))
            code, payload, before = call("validate", json.loads(path.read_text()))
        self.assertEqual(code, 0, payload)
        self.assertNotIn("run", json.loads(before))

    def test_the_cli_writes_the_run_binding_it_was_given(self):
        with tempfile.TemporaryDirectory(prefix="lh-run-") as d:
            Path(d, "report.md").write_text(TEXT)
            path = Path(d, "state.json")
            path.write_text(json.dumps(record()))
            proc = subprocess.run([sys.executable, str(SCRIPT), "evidence", str(path),
                                   "--id", "E1", "--artifact", "A1", "--result", "pass",
                                   "--run-case", RUN_A["case_id"], "--run-model", RUN_A["model"],
                                   "--run-input", RUN_A["input_sha256"]],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stored = json.loads(path.read_text())["evidence"][0]
            self.assertEqual(stored["run"], RUN_A)
            proc = subprocess.run([sys.executable, str(SCRIPT), "evidence", str(path),
                                   "--id", "E1", "--artifact", "A1", "--result", "pass",
                                   "--run-case", RUN_A["case_id"]],
                                  capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0, "a partial run binding must be refused")


if __name__ == "__main__":
    unittest.main()
