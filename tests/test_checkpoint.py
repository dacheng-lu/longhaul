"""Tests for the Longhaul checkpoint gate.

Every check here is mutation-sensitive: delete the corresponding check from
scripts/checkpoint.py and one of these tests goes red. The gate is itself a
self-certifying checker, so it is tested from its predicted failure surface,
not by replaying its own happy path.

Run: python3 -m pytest tests -q
"""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = os.environ.get('CHECKPOINT_UNDER_TEST') or str(REPO / 'scripts' / 'checkpoint.py')
sys.path.insert(0, str(REPO / 'scripts'))

import secret_scan  # noqa: E402


def now(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(
        timespec='seconds').replace('+00:00', 'Z')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def call(*args):
    proc = subprocess.run([sys.executable, SCRIPT] + [str(a) for a in args],
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {'_stdout': proc.stdout, '_stderr': proc.stderr}
    return proc.returncode, payload


def build(tmp_path, name='report.md', content='verified deliverable\n'):
    art = tmp_path / name
    art.write_text(content)
    state = {
        'schema_version': 2, 'task_id': 'task-1', 'goal': 'prove the gate',
        'status': 'in_progress', 'next_action': 'write the summary',
        'provenance': {'generated_at': now(), 'writer': 'tests', 'derived_from': None},
        'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
        'decisions': [], 'capabilities': [], 'lessons': [], 'shortcuts': [],
        'waivers': [],
        'artifacts': [{'id': 'A1', 'kind': 'local', 'locator': str(art),
                       'sha256': sha(art), 'revision': sha(art)[:12]}],
        'evidence': [{'id': 'E1', 'kind': 'local', 'locator': str(art),
                      'artifact_ids': ['A1'], 'artifact_revisions': {'A1': sha(art)[:12]},
                      'observed_at': now(), 'result': 'pass'}],
        'acceptance': [{'id': 'C1', 'criterion': 'report matches the spec',
                        'status': 'verified', 'evidence_ids': ['E1']}],
    }
    return state, art


def check(tmp_path, state, gate=True):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return call('gate' if gate else 'validate', path)


class TestSecrets:
    def test_clean_text_has_no_hits(self):
        assert secret_scan.scan_text('the report covers section 3 of the spec') == []

    def test_assignment_is_detected(self):
        assert 'credential_assignment' in secret_scan.scan_text(
            'api_key = sk-live-ABCDEF0123456789abcd')

    def test_jwt_is_detected(self):
        token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop'
        assert 'jwt' in secret_scan.scan_text(token)

    def test_sha256_field_is_exempt(self):
        digest = 'a' * 64
        state = {'artifacts': [{'id': 'A1', 'sha256': digest}]}
        assert secret_scan.scan_state(state) == []

    def test_long_hex_outside_exempt_field_is_flagged(self):
        state = {'lessons': ['the build id was ' + 'b' * 40]}
        assert secret_scan.scan_state(state)

    def test_luhn_accepts_valid_and_rejects_invalid(self):
        assert secret_scan.luhn('4111111111111111') is True
        assert secret_scan.luhn('4111111111111112') is False

    def test_card_number_is_detected(self):
        assert 'card_number' in secret_scan.scan_text('card 4111 1111 1111 1111 on file')

    def test_iban_is_detected(self):
        assert 'iban' in secret_scan.scan_text('pay to DE89 3704 0044 0532 0130 00 please')

    def test_gate_fails_and_never_echoes_the_secret(self, tmp_path):
        state, _ = build(tmp_path)
        state['lessons'] = ['api_key = sk-live-ABCDEF0123456789abcd']
        code, out = check(tmp_path, state)
        assert code == 1
        assert any('secret-shaped' in e for e in out['errors'])
        assert 'ABCDEF0123456789abcd' not in json.dumps(out)


class TestControl:
    def test_fresh_template_validates(self, tmp_path):
        path = tmp_path / 'state.json'
        code, _ = call('init', path, '--task-id', 't1', '--goal', 'g1')
        assert code == 0
        assert call('validate', path)[0] == 0

    def test_fresh_template_fails_gate(self, tmp_path):
        path = tmp_path / 'state.json'
        call('init', path, '--task-id', 't1', '--goal', 'g1')
        code, out = call('gate', path)
        assert code == 1
        assert any('nonempty before delivery' in e for e in out['errors'])

    def test_init_refuses_to_overwrite(self, tmp_path):
        path = tmp_path / 'state.json'
        call('init', path, '--task-id', 't1', '--goal', 'g1')
        code, out = call('init', path, '--task-id', 't1', '--goal', 'g1')
        assert code == 1 and 'File exists' in out['error']

    def test_complete_state_passes_gate(self, tmp_path):
        state, _ = build(tmp_path)
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_missing_file_is_a_clean_failure(self, tmp_path):
        code, out = call('gate', tmp_path / 'nope.json')
        assert code == 1 and out['ok'] is False

    def test_malformed_json_is_a_clean_failure(self, tmp_path):
        path = tmp_path / 'bad.json'
        path.write_text('{bad')
        code, out = call('validate', path)
        assert code == 1 and out['ok'] is False


class TestEnforced:
    def test_stale_evidence_after_artifact_change(self, tmp_path):
        state, art = build(tmp_path)
        art.write_text('changed after the checkpoint\n')
        state['artifacts'][0].update(sha256=sha(art), revision=sha(art)[:12])
        code, out = check(tmp_path, state)
        assert code == 1
        assert any('stale' in e for e in out['errors'])

    def test_verified_without_evidence(self, tmp_path):
        state, _ = build(tmp_path)
        state['acceptance'][0]['evidence_ids'] = []
        code, out = check(tmp_path, state)
        assert code == 1 and any('without evidence' in e for e in out['errors'])

    def test_failed_evidence_cannot_support_a_verified_criterion(self, tmp_path):
        state, _ = build(tmp_path)
        state['evidence'][0]['result'] = 'fail'
        code, out = check(tmp_path, state)
        assert code == 1 and any('failed or unknown evidence' in e for e in out['errors'])

    def test_hash_mismatch_on_disk(self, tmp_path):
        state, art = build(tmp_path)
        art.write_text('different content now\n')
        code, out = check(tmp_path, state)
        assert code == 1 and any('hash mismatch' in e for e in out['errors'])

    def test_open_blocker_blocks_the_gate(self, tmp_path):
        state, _ = build(tmp_path)
        state['blockers'] = [{'text': 'waiting for credentials'}]
        code, out = check(tmp_path, state)
        assert code == 1 and any('blockers' in e for e in out['errors'])

    def test_dangling_evidence_reference(self, tmp_path):
        state, _ = build(tmp_path)
        state['acceptance'][0]['evidence_ids'] = ['E9']
        code, out = check(tmp_path, state)
        assert code == 1 and any('unknown evidence E9' in e for e in out['errors'])

    def test_naive_timestamp_is_rejected(self, tmp_path):
        state, _ = build(tmp_path)
        state['evidence'][0]['observed_at'] = '2026-10-31T10:00:00'
        code, out = check(tmp_path, state)
        assert code == 1 and any('timezone required' in e for e in out['errors'])

    def test_duplicate_acceptance_id(self, tmp_path):
        state, _ = build(tmp_path)
        state['acceptance'].append(dict(state['acceptance'][0]))
        code, out = check(tmp_path, state)
        assert code == 1 and any('duplicate id' in e for e in out['errors'])


class TestGateHardening:
    """The seven rules v1.0 promised in prose and never checked."""

    def test_unchanged_retry_is_rejected(self, tmp_path):
        state, _ = build(tmp_path)
        state['attempts'] = [
            {'blocker_key': 'B1', 'action': 'curl api', 'error': 'timeout',
             'hypothesis_before': 'network is fine', 'hypothesis_after': 'network is fine',
             'at': now()},
            {'blocker_key': 'B1', 'action': 'curl api', 'error': 'timeout',
             'hypothesis_before': 'network is fine', 'hypothesis_after': 'network is fine',
             'at': now()},
        ]
        code, out = check(tmp_path, state)
        assert code == 1 and any('unchanged retry' in e for e in out['errors'])

    def test_three_attempts_without_hypothesis_change(self, tmp_path):
        state, _ = build(tmp_path)
        state['attempts'] = [
            {'blocker_key': 'B1', 'action': 'run %d' % i, 'error': 'exit 2',
             'hypothesis_before': 'same', 'hypothesis_after': 'same', 'at': now()}
            for i in range(3)
        ]
        code, out = check(tmp_path, state)
        assert code == 1 and any('without a hypothesis change' in e for e in out['errors'])

    def test_hypothesis_change_resets_the_budget(self, tmp_path):
        state, _ = build(tmp_path)
        state['attempts'] = [
            {'blocker_key': 'B1', 'action': 'run 0', 'error': 'exit 2',
             'hypothesis_before': 'a', 'hypothesis_after': 'b', 'at': now()},
            {'blocker_key': 'B1', 'action': 'run 1', 'error': 'exit 3',
             'hypothesis_before': 'b', 'hypothesis_after': 'b', 'at': now()},
        ]
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_bundled_next_action_is_rejected(self, tmp_path):
        state, _ = build(tmp_path)
        state['next_action'] = 'fix the parser then rerun the suite'
        code, out = check(tmp_path, state)
        assert code == 1 and any('one step' in e for e in out['errors'])

    def test_non_ascii_bundle_is_only_a_warning(self, tmp_path):
        state, _ = build(tmp_path)
        state['next_action'] = '先修解析器，然后再跑测试'
        code, out = check(tmp_path, state)
        assert code == 0
        assert any('may bundle' in w for w in out['warnings'])

    def test_future_timestamp_is_rejected(self, tmp_path):
        state, _ = build(tmp_path)
        state['evidence'][0]['observed_at'] = '2099-01-01T00:00:00Z'
        code, out = check(tmp_path, state)
        assert code == 1 and any('in the future' in e for e in out['errors'])

    def test_shortcut_without_upgrade_trigger(self, tmp_path):
        state, _ = build(tmp_path)
        state['shortcuts'] = [{'what': 'skip the mac probe', 'ceiling': 'linux only',
                               'at': now()}]
        code, out = check(tmp_path, state)
        assert code == 1 and any('no-trigger' in e for e in out['errors'])

    def test_shortcut_with_trigger_passes(self, tmp_path):
        state, _ = build(tmp_path)
        state['shortcuts'] = [{'what': 'skip the mac probe', 'ceiling': 'linux only',
                               'upgrade_trigger': 'first mac user files a bug', 'at': now()}]
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_callable_capability_needs_proof(self, tmp_path):
        state, _ = build(tmp_path)
        state['capabilities'] = [{'name': 'playwright', 'class': 'callable'}]
        code, out = check(tmp_path, state)
        assert code == 1 and any('last_success' in e for e in out['errors'])

    def test_callable_capability_may_declare_itself_untested(self, tmp_path):
        state, _ = build(tmp_path)
        state['capabilities'] = [{'name': 'playwright', 'class': 'callable', 'untested': True}]
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_orphan_artifact_blocks_the_gate(self, tmp_path):
        state, _ = build(tmp_path)
        extra = tmp_path / 'notes.md'
        extra.write_text('input\n')
        state['artifacts'].append({'id': 'A2', 'kind': 'local', 'locator': str(extra),
                                   'sha256': sha(extra), 'revision': sha(extra)[:12]})
        code, out = check(tmp_path, state)
        assert code == 1 and any('no evidence binds it' in e for e in out['errors'])

    def test_supporting_artifact_is_exempt_from_coverage(self, tmp_path):
        state, _ = build(tmp_path)
        extra = tmp_path / 'notes.md'
        extra.write_text('input\n')
        state['artifacts'].append({'id': 'A2', 'kind': 'local', 'locator': str(extra),
                                   'sha256': sha(extra), 'revision': sha(extra)[:12],
                                   'supporting': True})
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_unknown_result_needs_a_reason(self, tmp_path):
        state, _ = build(tmp_path)
        state['evidence'][0].update(result='unknown')
        code, out = check(tmp_path, state, gate=False)
        assert code == 1 and any('unknown_reason' in e for e in out['errors'])

    def test_unobservable_criterion_needs_a_waiver(self, tmp_path):
        state, _ = build(tmp_path)
        state['acceptance'][0]['status'] = 'unobservable'
        code, out = check(tmp_path, state)
        assert code == 1 and any('waiver' in e for e in out['errors'])

    def test_waiver_unblocks_an_unobservable_criterion(self, tmp_path):
        state, _ = build(tmp_path)
        state['acceptance'][0]['status'] = 'unobservable'
        state['waivers'] = [{'criterion_id': 'C1', 'reason': 'third-party API behind a WAF',
                             'approved_by': 'dacheng-lu', 'approved_at': now()}]
        code, out = check(tmp_path, state)
        assert code == 0, out

    def test_waiver_requires_an_approver(self, tmp_path):
        state, _ = build(tmp_path)
        state['waivers'] = [{'criterion_id': 'C1', 'reason': 'blocked', 'approved_at': now()}]
        code, out = check(tmp_path, state, gate=False)
        assert code == 1 and any('silence is not consent' in e for e in out['errors'])


class TestCli:
    def test_hash_matches_revision(self, tmp_path):
        art = tmp_path / 'x.txt'
        art.write_text('content\n')
        code, out = call('hash', art)
        assert code == 0 and out['revision'] == out['sha256'][:12]

    def test_artifact_accepts_a_cwd_relative_path(self, tmp_path, monkeypatch):
        state = tmp_path / 'state' / 'task.json'
        call('init', state, '--task-id', 't1', '--goal', 'g1')
        art = tmp_path / 'out.md'
        art.write_text('x\n')
        monkeypatch.chdir(tmp_path)
        code, out = call('artifact', state, '--id', 'A1', '--locator', 'out.md')
        assert code == 0, out
        assert out['artifact']['locator'] == str(art.resolve())

    def test_artifact_names_the_path_it_tried(self, tmp_path):
        state = tmp_path / 'state' / 'task.json'
        call('init', state, '--task-id', 't1', '--goal', 'g1')
        code, out = call('artifact', state, '--id', 'A1', '--locator', 'missing.md')
        assert code == 1 and 'no such file' in out['error']

    def test_artifact_and_evidence_build_a_passing_gate(self, tmp_path):
        path = tmp_path / 'state.json'
        art = tmp_path / 'out.md'
        art.write_text('done\n')
        assert call('init', path, '--task-id', 't1', '--goal', 'g1')[0] == 0
        assert call('artifact', path, '--id', 'A1', '--locator', 'out.md')[0] == 0
        assert call('evidence', path, '--id', 'E1', '--artifact', 'A1', '--result', 'pass')[0] == 0
        state = json.loads(path.read_text())
        state['acceptance'] = [{'id': 'C1', 'criterion': 'file exists', 'status': 'verified',
                                'evidence_ids': ['E1']}]
        path.write_text(json.dumps(state))
        code, out = call('gate', path)
        assert code == 0, out

    def test_evidence_revision_is_refreshed_by_artifact(self, tmp_path):
        path = tmp_path / 'state.json'
        art = tmp_path / 'out.md'
        art.write_text('one\n')
        call('init', path, '--task-id', 't1', '--goal', 'g1')
        call('artifact', path, '--id', 'A1', '--locator', 'out.md')
        call('evidence', path, '--id', 'E1', '--artifact', 'A1', '--result', 'pass')
        art.write_text('two\n')
        call('artifact', path, '--id', 'A1', '--locator', 'out.md')
        state = json.loads(path.read_text())
        assert state['evidence'][0]['artifact_revisions']['A1'] != state['artifacts'][0]['revision']


class TestV1Compat:
    def v1(self, tmp_path, mutate=None):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        state = {
            'schema_version': 1, 'task_id': 't1', 'goal': 'g1', 'status': 'in_progress',
            'artifact_revision': 'r1', 'next_action': 'n',
            'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
            'decisions': [], 'capabilities': [], 'lessons': [],
            'acceptance': [{'id': 'C1', 'criterion': 'report exists', 'status': 'verified',
                            'evidence_ids': ['E1']}],
            'deliverables': [{'kind': 'local', 'locator': str(art), 'sha256': sha(art)}],
            'evidence': [{'id': 'E1', 'kind': 'local', 'locator': str(art),
                          'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}],
        }
        if mutate:
            mutate(state, art)
        return state, art

    def test_v1_is_read_in_memory_with_a_warning(self, tmp_path):
        state, _ = self.v1(tmp_path)
        code, out = check(tmp_path, state)
        assert code == 0, out
        assert any('run migrate' in w for w in out['warnings'])

    def test_migrate_persists_v2(self, tmp_path):
        state, _ = self.v1(tmp_path)
        path = tmp_path / 'state.json'
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['schema_version'] == 2
        assert migrated['artifacts'][0]['id'] == 'A1'
        assert call('gate', path)[0] == 0

    def test_migrate_does_not_launder_changed_content(self, tmp_path):
        def change(state, art):
            art.write_text('edited after the v1 checkpoint\n')
        state, _ = self.v1(tmp_path, change)
        path = tmp_path / 'state.json'
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0
        assert any('re-verify' in w for w in out['warnings'])
        code, out = call('gate', path)
        assert code == 1 and any('stale' in e for e in out['errors'])

    def test_unbound_evidence_blocks_the_gate(self, tmp_path):
        def unbound(state, art):
            state['evidence'][0]['locator'] = 'https://example.invalid/api'
            state['evidence'][0]['kind'] = 'external'
            state['evidence'][0]['summary'] = 'endpoint returned 200'
        state, _ = self.v1(tmp_path, unbound)
        code, out = check(tmp_path, state)
        assert code == 1
        assert any('artifact_ids' in e for e in out['errors'])
