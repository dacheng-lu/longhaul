"""Adversarial tests for the v1.2 revision of the Longhaul checkpoint.

Scope: the *measured* failure surface of v1.1 (`scripts/checkpoint.py`, schema v2).
Every test drives the production CLI or the production scanner; nothing is mocked,
no network, no real credentials (synthetic shapes only), no host profile, no writes
outside the pytest temporary directory.

Each test names the behaviour the repository promises (CHANGELOG 1.1,
``references/state.md``, ``docs/PLAN-v1.1.md``) so a regression is identifiable.
The five tests that were strict-xfail defect records in the first v1.2 round now
run as normal tests because the defects are fixed; the rest are controls that
must stay green so a "fix" cannot trade a real check for a quiet suite.

Run: python -m pytest -q tests/test_v12_checkpoint.py   (any Python 3.10+ with pytest)
"""
import hashlib
import json
import os
import random
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = os.environ.get('CHECKPOINT_UNDER_TEST') or str(REPO / 'scripts' / 'checkpoint.py')
sys.path.insert(0, str(REPO / 'scripts'))

import secret_scan  # noqa: E402

# Synthetic values that match one documented shape and no other. Never a real token.
PROVIDER_TOKEN = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
PASSWORD = 'Winter2026Staging!'
BEARER_TOKEN = 'abcdefghijklmnopqrstuvwxyz0123456789'


def now(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(
        timespec='seconds').replace('+00:00', 'Z')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(*args):
    return subprocess.run([sys.executable, SCRIPT] + [str(a) for a in args],
                          capture_output=True, text=True)


def call(*args):
    """Run the production CLI the way a recovery script would: parse stdout."""
    proc = run(*args)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {'_stdout': proc.stdout,
                   '_stderr_tail': proc.stderr.strip().splitlines()[-1:]}
    return proc.returncode, payload


def assert_clean_failure(proc, payload, needle):
    """A refusal must be parseable, must not hide behind a traceback, and must say why."""
    assert payload.get('ok') is False, payload
    assert 'Traceback' not in proc.stderr, proc.stderr
    assert needle in json.dumps(payload), payload


def local_artifact(art_id, path):
    digest = sha(path)
    return {'id': art_id, 'kind': 'local', 'locator': str(path),
            'sha256': digest, 'revision': digest[:12]}


def local_evidence(ev_id, artifacts, result='pass', **extra):
    """Bind one observation to local artifacts at the revisions they carried."""
    entry = {'id': ev_id, 'kind': 'local', 'locator': artifacts[0]['locator'],
             'artifact_ids': [a['id'] for a in artifacts],
             'artifact_revisions': {a['id']: a['revision'] for a in artifacts},
             'observed_at': now(), 'result': result}
    entry.update(extra)
    return entry


def attempt(action, error, before, after, key='B1'):
    return {'blocker_key': key, 'action': action, 'error': error,
            'hypothesis_before': before, 'hypothesis_after': after, 'at': now()}


def record(artifacts, evidence, acceptance, **extra):
    state = {
        'schema_version': 2, 'task_id': 'v12-task', 'goal': 'prove the recovery claims',
        'status': 'in_progress', 'next_action': 're-run the check that changed',
        'provenance': {'generated_at': now(), 'writer': 'tests/test_v12_checkpoint.py',
                       'derived_from': None},
        'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [], 'decisions': [],
        'capabilities': [], 'lessons': [], 'shortcuts': [], 'waivers': [],
        'artifacts': artifacts, 'evidence': evidence, 'acceptance': acceptance,
    }
    state.update(extra)
    return state


def single_deliverable(tmp_path, content='deliverable\n'):
    art = tmp_path / 'report.md'
    art.write_text(content)
    artifact = local_artifact('A1', art)
    return record(
        artifacts=[artifact],
        evidence=[local_evidence('E1', [artifact])],
        acceptance=[{'id': 'C1', 'criterion': 'report matches the spec',
                     'status': 'verified', 'evidence_ids': ['E1']}]), art


def write(tmp_path, state):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return path


def gate(tmp_path, state):
    return call('gate', write(tmp_path, state))


class TestDocumentBoundary:
    """Recovery starts by reading the record; a crash there is a lost task."""

    @pytest.mark.parametrize('document', ['[]', 'null', '42', '"a string"'])
    def test_non_object_document_is_a_clean_parseable_failure(self, tmp_path, document):
        """PLAN-v1.1 deviation 4: every CLI error is JSON on stdout with exit 1."""
        path = tmp_path / 'state.json'
        path.write_text(document)
        for command in ('validate', 'gate'):
            proc = run(command, path)
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, (command, payload)
            assert_clean_failure(proc, payload, 'state must be an object')

    def test_empty_object_is_a_clean_parseable_failure(self, tmp_path):
        """Control: an object document reaches the schema check instead of the boundary."""
        path = tmp_path / 'state.json'
        path.write_text('{}')
        for command in ('validate', 'gate'):
            proc = run(command, path)
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, payload
            assert payload['errors'] == ['unsupported schema_version'], payload
            assert 'Traceback' not in proc.stderr, proc.stderr

    @pytest.mark.parametrize('tokens', [
        ('artifact', '--id', 'A1', '--locator', 'out.md'),
        ('evidence', '--id', 'E1', '--artifact', 'A1', '--result', 'pass'),
        ('migrate', '--write'),
    ])
    def test_mutating_commands_refuse_a_non_object_document(self, tmp_path, tokens):
        """A command that would rewrite the record must refuse, not raise KeyError/TypeError."""
        path = tmp_path / 'state.json'
        path.write_text('[]')
        before = path.read_bytes()
        proc = run(tokens[0], path, *tokens[1:])
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert_clean_failure(proc, payload, 'object')
        assert path.read_bytes() == before, 'a refused command must not rewrite the record'

    def test_migrate_refuses_an_unknown_schema(self, tmp_path):
        """A future schema is not v1; treating it as v1 would silently mangle the record."""
        path = tmp_path / 'state.json'
        path.write_text(json.dumps({'schema_version': 3, 'task_id': 't', 'goal': 'g'}))
        before = path.read_bytes()
        proc = run('migrate', path, '--write')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert_clean_failure(proc, payload, 'unsupported schema_version')
        assert path.read_bytes() == before

    def test_artifact_and_evidence_refuse_a_v1_record(self, tmp_path):
        """Writing v2 fields into a v1 document would corrupt it; send the caller to migrate."""
        path = tmp_path / 'state.json'
        art = tmp_path / 'out.md'
        art.write_text('x\n')
        path.write_text(json.dumps({'schema_version': 1, 'task_id': 't1', 'goal': 'g',
                                    'artifact_revision': 'r1', 'next_action': 'n',
                                    'status': 'in_progress', 'deliverables': [],
                                    'evidence': [], 'acceptance': []}))
        before = path.read_bytes()
        for tokens in (('artifact', '--id', 'A1', '--locator', 'out.md'),
                       ('evidence', '--id', 'E1', '--artifact', 'A1', '--result', 'pass')):
            proc = run(tokens[0], path, *tokens[1:])
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, payload
            assert_clean_failure(proc, payload, 'run migrate')
        assert path.read_bytes() == before

    def test_mutating_commands_refuse_an_incomplete_v2_record(self, tmp_path):
        """A hand-edited v2 object without its lists must be reported, not crash with KeyError."""
        path = tmp_path / 'state.json'
        path.write_text(json.dumps({'schema_version': 2, 'task_id': 't1', 'goal': 'g',
                                    'next_action': 'n', 'status': 'in_progress',
                                    'provenance': {'generated_at': now(), 'writer': 'w'}}))
        (tmp_path / 'out.md').write_text('x\n')
        before = path.read_bytes()
        for tokens in (('artifact', '--id', 'A1', '--locator', 'out.md'),
                       ('evidence', '--id', 'E1', '--artifact', 'A1', '--result', 'pass')):
            proc = run(tokens[0], path, *tokens[1:])
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, payload
            assert_clean_failure(proc, payload, 'run validate')
        assert path.read_bytes() == before


class TestSecrets:
    """CHANGELOG 1.1: a credential-shaped value anywhere in the state is rejected."""

    def test_credential_under_a_credential_named_key_is_reported(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['capabilities'] = [{'name': 'staging-db', 'class': 'discovered',
                                  'purpose': 'read-only inspection', 'password': PASSWORD}]
        hits = secret_scan.scan_state(state)
        assert hits == [('$.capabilities[0].password', 'credential_key')], hits
        code, out = gate(tmp_path, state)
        assert code == 1, out
        secret_errors = [e for e in out['errors'] if 'secret-shaped' in e]
        assert len(secret_errors) == 1, out['errors']
        assert '$.capabilities[0].password' in secret_errors[0], secret_errors
        assert PASSWORD not in json.dumps(out), 'the gate echoed the secret'

    def test_credential_under_a_nested_credential_named_key_is_reported(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['decisions'] = [{'id': 'D1', 'what': 'rotate the token',
                               'details': {'client_secret': 'opaquevalue123'}}]
        assert secret_scan.scan_state(state) == [
            ('$.decisions[0].details.client_secret', 'credential_key')]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('$.decisions[0].details.client_secret' in e for e in out['errors']), out
        assert 'opaquevalue123' not in json.dumps(out)

    def test_credential_in_a_list_under_a_credential_named_key_is_reported(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['lessons'] = [{'what': 'old tokens', 'token': ['abc123def456']}]
        assert secret_scan.scan_state(state) == [('$.lessons[0].token[0]', 'credential_key')]
        assert gate(tmp_path, state)[0] == 1

    @pytest.mark.parametrize('value', [
        '${DB_PASSWORD}', 'env:STAGING_DB_PASSWORD', '«redacted»', '<set in .env>',
        'n/a', 'none', 'required', 'see op://prod/staging-db/password',
        'op://prod/staging-db/password',
    ])
    def test_references_and_placeholders_are_not_reported(self, value):
        """The contract says record the location, not the value; a pointer is not a secret."""
        assert secret_scan.scan_state({'capabilities': [{'password': value}]}) == []

    @pytest.mark.parametrize('entry', [
        {'token_budget': '8192'},
        {'password_policy': 'min 12 chars, rotate quarterly'},
        {'credentials_source': 'env'},
        {'password': 12345},
        {'password': None},
    ])
    def test_harmless_metadata_is_not_reported(self, entry):
        assert secret_scan.scan_state(entry) == []

    def test_credential_named_in_a_sibling_field_is_reported(self, tmp_path):
        """A wrapper that names the credential next to its value, as n8n stores headers.

        Measured on real n8n workflow exports: ``{"name": "Secret", "value": "..."}``
        is invisible to a rule that only looks at the key (``value``) and to the value
        shapes (the secret has no prefix), so it was reported as zero hits.
        """
        state, _ = single_deliverable(tmp_path)
        state['capabilities'] = [{'name': 'Autotask REST', 'class': 'discovered',
                                  'purpose': 'ticket creation',
                                  'headers': [{'name': 'Secret', 'value': 'opaquevalue123'},
                                              {'name': 'Content-Type', 'value': 'application/json'}]}]
        assert secret_scan.scan_state(state) == [
            ('$.capabilities[0].headers[0].value', 'credential_key')]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('$.capabilities[0].headers[0].value' in e for e in out['errors']), out['errors']
        assert 'opaquevalue123' not in json.dumps(out), 'the gate echoed the secret'

    @pytest.mark.parametrize('name', ['Content-Type', 'Accept', 'X-Request-Id', 'Purpose'])
    def test_a_harmless_sibling_name_does_not_mark_the_value(self, name):
        assert secret_scan.scan_state({'headers': [{'name': name, 'value': 'application/json'}]}) == []

    def test_a_pointer_in_a_credential_named_sibling_stays_silent(self):
        """The pointer exemption applies to the sibling rule too."""
        assert secret_scan.scan_state(
            {'headers': [{'name': 'Secret', 'value': '${AT_SECRET}'}]}) == []
        assert secret_scan.scan_state(
            {'headers': [{'name': 'Authorization', 'value': '«redacted»'}]}) == []

    def test_credential_in_a_value_position_is_reported(self, tmp_path):
        """Control: the same password is caught when it sits in a value."""
        state, _ = single_deliverable(tmp_path)
        state['lessons'] = ['staging db: password: ' + PASSWORD]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('secret-shaped' in e for e in out['errors']), out
        assert PASSWORD not in json.dumps(out), 'the gate echoed the secret'

    def test_credential_inside_an_artifact_locator_is_reported(self, tmp_path):
        """The CLI records what the caller gave it; the gate then rejects it, without echo."""
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        url = 'https://robot:%s@github.com/acme/private.git' % PROVIDER_TOKEN
        code, artifact_out = call('artifact', path, '--id', 'A2', '--kind', 'external',
                                  '--locator', url, '--revision', 'v1.0.0')
        assert code == 0, artifact_out
        code, evidence_out = call('evidence', path, '--id', 'E2', '--artifact', 'A2',
                                  '--result', 'pass', '--summary', 'clone listed the remote refs')
        assert code == 0, evidence_out
        assert PROVIDER_TOKEN not in json.dumps([artifact_out, evidence_out]), 'CLI echoed it'
        assert url in path.read_text(), 'the recorded document is not silently rewritten'
        state = json.loads(path.read_text())
        state['acceptance'].append({'id': 'C2',
                                    'criterion': 'the pinned external revision is reachable',
                                    'status': 'verified', 'evidence_ids': ['E2']})
        path.write_text(json.dumps(state))
        assert ('$.artifacts[1].locator', 'provider_key') in secret_scan.scan_state(state)
        code, out = call('gate', path)
        assert code == 1, out
        assert any('$.artifacts[1].locator' in e for e in out['errors']), out['errors']
        assert PROVIDER_TOKEN not in json.dumps(out), 'the gate echoed the secret'

    def test_credential_used_as_an_external_revision_label_is_reported(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        assert call('artifact', path, '--id', 'A2', '--kind', 'external',
                    '--locator', 'https://example.invalid/api', '--revision',
                    PROVIDER_TOKEN)[0] == 0
        assert call('evidence', path, '--id', 'E2', '--artifact', 'A2', '--result', 'pass',
                    '--summary', 'release tag observed')[0] == 0
        state = json.loads(path.read_text())
        assert ('$.artifacts[1].revision', 'provider_key') in secret_scan.scan_state(state)
        code, out = call('gate', path)
        assert code == 1, out
        assert any('.revision' in e for e in out['errors']), out['errors']
        assert PROVIDER_TOKEN not in json.dumps(out)

    def test_credential_in_evidence_summary_is_not_echoed_by_the_cli(self, tmp_path):
        """`evidence` prints the entry it stored; a detected value must be redacted there."""
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        code, out = call('evidence', path, '--id', 'E2', '--artifact', 'A1', '--result', 'pass',
                         '--summary', 'observed via Authorization: Bearer ' + BEARER_TOKEN)
        assert code == 0, out
        assert BEARER_TOKEN not in json.dumps(out), 'CLI echoed the secret'
        assert BEARER_TOKEN in path.read_text(), 'the record keeps what the caller stored'
        assert call('gate', path)[0] == 1

    def test_migrate_refuses_a_secret_bearing_record_without_printing_it(self, tmp_path):
        """A record validate would reject is refused, not converted or echoed."""
        v1 = {'schema_version': 1, 'task_id': 't1', 'goal': 'deploy with ' + PROVIDER_TOKEN,
              'status': 'in_progress', 'artifact_revision': 'r1', 'next_action': 'n',
              'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
              'decisions': [], 'capabilities': [], 'lessons': [],
              'acceptance': [], 'deliverables': [], 'evidence': []}
        path = tmp_path / 'state.json'
        path.write_text(json.dumps(v1))
        before = path.read_bytes()
        for extra in ((), ('--write',)):
            code, out = call('migrate', path, *extra)
            assert code == 1, out
            assert any('$.goal' in e for e in out['errors']), out
            assert PROVIDER_TOKEN not in json.dumps(out)
            assert path.read_bytes() == before, 'a refusal must not write'
        assert 'migrated' not in out

    def test_paths_and_error_messages_never_echo_a_detected_value(self, tmp_path):
        """`hash` and the artifact error path print the caller's path; both must redact it."""
        secret_dir = tmp_path / PROVIDER_TOKEN
        secret_dir.mkdir()
        present = secret_dir / 'present.md'
        present.write_text('x\n')
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        code, out = call('hash', present)
        assert code == 0, out
        assert PROVIDER_TOKEN not in json.dumps(out)
        code, out = call('artifact', path, '--id', 'A9',
                         '--locator', str(secret_dir / 'absent.md'))
        assert code == 1, out
        assert 'no such file' in out['error'], out
        assert PROVIDER_TOKEN not in json.dumps(out)

    def test_normal_locators_hashes_and_revisions_are_not_reported(self, tmp_path):
        """The hash/label exemption keeps its purpose: real paths, URLs and 64-hex stay silent."""
        hexdir = tmp_path / ('a' * 40)
        hexdir.mkdir()
        art = hexdir / 'out.md'
        art.write_text('done\n')
        external = {'id': 'A2', 'kind': 'external', 'locator': 'https://example.invalid/api/v2',
                    'revision': 'v1.0.0'}
        ssh = {'id': 'A3', 'kind': 'external', 'locator': 'ssh://git@github.com/acme/repo.git',
               'revision': 'main@abc1234'}
        artifact = local_artifact('A1', art)
        state = record(
            artifacts=[artifact, external, ssh],
            evidence=[local_evidence('E1', [artifact]),
                      {'id': 'E2', 'kind': 'external', 'locator': external['locator'],
                       'artifact_ids': ['A2'], 'artifact_revisions': {'A2': 'v1.0.0'},
                       'observed_at': now(), 'result': 'pass', 'summary': 'api answered'},
                      {'id': 'E3', 'kind': 'external', 'locator': ssh['locator'],
                       'artifact_ids': ['A3'], 'artifact_revisions': {'A3': 'main@abc1234'},
                       'observed_at': now(), 'result': 'pass', 'summary': 'ref listed'}],
            acceptance=[{'id': 'C%d' % i, 'criterion': 'c', 'status': 'verified',
                         'evidence_ids': ['E%d' % i]} for i in (1, 2, 3)])
        assert secret_scan.scan_state(state) == []
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_shape_rules_are_specific(self):
        """New shapes must not fire on prose or on counts."""
        assert secret_scan.scan_text('Authorization: Bearer ' + BEARER_TOKEN) == ['bearer_token']
        assert secret_scan.scan_text('Bearer tokens are long lived') == []
        assert secret_scan.scan_text('https://user:' + PASSWORD + '@example.invalid/x') == [
            'url_credentials']
        assert secret_scan.scan_text('https://example.invalid:8443/x') == []
        assert secret_scan.scan_text('ssh://git@github.com/acme/repo.git') == []
        assert secret_scan.scan_text('token = abcdef123456') == ['credential_assignment']
        assert secret_scan.scan_text('token_count = 123456') == []
        assert secret_scan.scan_text('tokens: 900') == []


class TestRetryBudget:
    def test_three_unchanged_attempts_are_rejected(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [attempt('run a', 'e', 'h', 'h'),
                             attempt('run b', 'e', 'h', 'h'),
                             attempt('run c', 'e', 'h', 'h')]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert sum('without a hypothesis change' in e for e in out['errors']) == 1, out['errors']

    def test_budget_applies_again_after_the_hypothesis_change(self, tmp_path):
        """Every consecutive window of three counts, not only the first three attempts."""
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [attempt('run a', 'e', 'h0', 'h1'),
                             attempt('run b', 'e', 'h1', 'h1'),
                             attempt('run c', 'e', 'h1', 'h1'),
                             attempt('run d', 'e', 'h1', 'h1')]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('without a hypothesis change' in e for e in out['errors']), out

    def test_two_unchanged_attempts_after_a_change_are_allowed(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [attempt('run a', 'e', 'h0', 'h1'),
                             attempt('run b', 'e', 'h1', 'h1'),
                             attempt('run c', 'e', 'h1', 'h1')]
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_change_every_third_attempt_is_allowed(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [attempt('run a', 'e', 'h0', 'h1'),
                             attempt('run b', 'e', 'h1', 'h1'),
                             attempt('run c', 'e', 'h1', 'h1'),
                             attempt('run d', 'e', 'h1', 'h2'),
                             attempt('run e', 'e', 'h2', 'h2'),
                             attempt('run f', 'e', 'h2', 'h2')]
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_attempts_without_hypothesis_fields_count_as_unchanged(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [{'blocker_key': 'B1', 'action': 'run %d' % i, 'error': 'e',
                              'at': now()} for i in range(3)]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('without a hypothesis change' in e for e in out['errors']), out

    def test_independent_blockers_are_judged_separately(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [
            attempt('a', 'e', 'h0', 'h1', 'B1'),
            attempt('b', 'e', 'h1', 'h2', 'B1'),
            attempt('c', 'e', 'h2', 'h3', 'B1'),
            attempt('x', 'e', 'k', 'k', 'B2'),
            attempt('y', 'e', 'k', 'k', 'B2'),
            attempt('z', 'e', 'k', 'k', 'B2'),
        ]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        budget = [e for e in out['errors'] if 'without a hypothesis change' in e]
        assert len(budget) == 1, out['errors']
        assert 'B2' in budget[0] and 'B1' not in budget[0], budget

    def test_unchanged_retry_guard_survives_the_window_rule(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [attempt('curl api', 'timeout', 'h', 'h'),
                             attempt('curl api', 'timeout', 'h', 'h')]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('unchanged retry' in e for e in out['errors']), out
        assert not any('without a hypothesis change' in e for e in out['errors']), out


class TestMigration:
    def v1(self, tmp_path, deliverables=None, **extra):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        state = {
            'schema_version': 1, 'task_id': 't1', 'goal': 'g', 'status': 'in_progress',
            'artifact_revision': 'r1', 'next_action': 'n',
            'constraints': ['no network'], 'milestones': ['m1'], 'blockers': [],
            'attempts': [], 'decisions': [{'id': 'D1', 'what': 'w', 'why': 'y'}],
            'capabilities': ['pwsh'], 'lessons': ['l1'],
            'acceptance': [{'id': 'C1', 'criterion': 'report exists', 'status': 'verified',
                            'evidence_ids': ['E1']}],
            'deliverables': deliverables if deliverables is not None else [
                {'kind': 'local', 'locator': str(art), 'sha256': sha(art)}],
            'evidence': [{'id': 'E1', 'kind': 'local', 'locator': str(art),
                          'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}],
        }
        state.update(extra)
        path = tmp_path / 'state.json'
        path.write_text(json.dumps(state, indent=2))
        return path, art

    def test_migrated_record_passes_structural_validation(self, tmp_path):
        """Two v1 deliverables may share a locator; evidence must bind every one of them."""
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _ = self.v1(tmp_path, deliverables=[{'kind': 'local', 'locator': str(art),
                                                  'sha256': sha(art)}] * 2)
        digest = sha(art)
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        code, out = call('validate', path)
        assert code == 0, out
        state = json.loads(path.read_text())
        assert state['evidence'][0]['artifact_revisions'] == {'A1': digest[:12],
                                                              'A2': digest[:12]}
        code, out = call('gate', path)
        assert code == 0, out

    def test_shared_locator_with_a_mismatched_hash_is_refused(self, tmp_path):
        """Two deliverables for one path that disagree on its hash are ambiguous.

        Behaviour change (audit finding 6): the old code silently bound both to one
        revision; migration now refuses without writing, because it cannot know which
        content the v1 observation saw.
        """
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _ = self.v1(tmp_path, deliverables=[
            {'kind': 'local', 'locator': str(art), 'sha256': sha(art)},
            {'kind': 'local', 'locator': str(art), 'sha256': 'b' * 64}])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('ambiguous' in e or 'conflict' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_migrate_preserves_v1_content_and_is_idempotent(self, tmp_path):
        """No data loss: unrelated v1 fields survive, evidence is re-bound only on a match."""
        path, art = self.v1(tmp_path, evaluation_date='2026-12-31')
        digest = sha(art)
        assert call('migrate', path, '--write')[0] == 0
        migrated = json.loads(path.read_text())
        assert migrated['schema_version'] == 2
        assert [a['id'] for a in migrated['artifacts']] == ['A1']
        assert migrated['artifacts'][0]['revision'] == digest[:12]
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': digest[:12]}
        assert migrated['evaluation_date'] == '2026-12-31'
        assert migrated['constraints'] == ['no network']
        assert migrated['milestones'] == ['m1']
        assert migrated['decisions'] == [{'id': 'D1', 'what': 'w', 'why': 'y'}]
        assert migrated['capabilities'] == ['pwsh']
        assert migrated['lessons'] == ['l1']
        assert call('gate', path)[0] == 0
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 0 and 'already schema v2' in out['note'], out
        assert path.read_bytes() == before


class TestRevisionAndRecovery:
    def two_deliverables(self, tmp_path):
        one = tmp_path / 'one.md'
        two = tmp_path / 'two.md'
        one.write_text('one v1\n')
        two.write_text('two v1\n')
        a1, a2 = local_artifact('A1', one), local_artifact('A2', two)
        return record(
            artifacts=[a1, a2],
            evidence=[local_evidence('E1', [a1]), local_evidence('E2', [a2])],
            acceptance=[{'id': 'C1', 'criterion': 'one is final', 'status': 'verified',
                         'evidence_ids': ['E1']},
                        {'id': 'C2', 'criterion': 'two is final', 'status': 'verified',
                         'evidence_ids': ['E2']}]), one, two

    def test_editing_one_deliverable_stales_only_its_own_evidence(self, tmp_path):
        """Only the criteria that depend on the changed deliverable are affected."""
        state, one, _ = self.two_deliverables(tmp_path)
        path = write(tmp_path, state)
        assert call('gate', path)[0] == 0
        one.write_text('one v2 edited\n')
        assert call('artifact', path, '--id', 'A1', '--locator', 'one.md')[0] == 0
        code, out = call('gate', path)
        assert code == 1
        assert len(out['errors']) == 1, out['errors']
        assert 'E1' in out['errors'][0] and 'A1' in out['errors'][0], out['errors']
        assert 'E2' not in json.dumps(out) and 'A2' not in json.dumps(out), out['errors']

    def test_reverifying_the_changed_artifact_clears_the_gate(self, tmp_path):
        """The documented loop closes: refresh, re-observe, deliver."""
        state, one, _ = self.two_deliverables(tmp_path)
        path = write(tmp_path, state)
        one.write_text('one v2 edited\n')
        assert call('artifact', path, '--id', 'A1', '--locator', 'one.md')[0] == 0
        assert call('gate', path)[0] == 1
        assert call('evidence', path, '--id', 'E1', '--artifact', 'A1',
                    '--result', 'pass')[0] == 0
        code, out = call('gate', path)
        assert code == 0, out
        state = json.loads(path.read_text())
        revisions = {e['id']: e['artifact_revisions'] for e in state['evidence']}
        assert revisions['E1'] == {'A1': state['artifacts'][0]['revision']}
        assert revisions['E2'] == {'A2': state['artifacts'][1]['revision']}

    def test_unchanged_reregistration_does_not_invalidate_evidence(self, tmp_path):
        """Content-addressed revision: same bytes, same revision, no spurious staleness."""
        path = tmp_path / 'state.json'
        art = tmp_path / 'out.md'
        art.write_text('stable\n')
        assert call('init', path, '--task-id', 't1', '--goal', 'g')[0] == 0
        assert call('artifact', path, '--id', 'A1', '--locator', 'out.md')[0] == 0
        assert call('evidence', path, '--id', 'E1', '--artifact', 'A1', '--result', 'pass')[0] == 0
        before = json.loads(path.read_text())['artifacts'][0]['revision']
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'out.md')
        assert code == 0 and out['artifact']['revision'] == before
        state = json.loads(path.read_text())
        state['acceptance'] = [{'id': 'C1', 'criterion': 'file is stable', 'status': 'verified',
                                'evidence_ids': ['E1']}]
        path.write_text(json.dumps(state))
        code, out = call('gate', path)
        assert code == 0, out

    def test_validate_does_not_stand_in_for_the_gate_after_a_file_disappears(self, tmp_path):
        """state.md: validate is structure only; recovery must run gate."""
        state, art = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        art.unlink()
        code, out = call('validate', path)
        assert code == 0, out
        code, out = call('gate', path)
        assert code == 1, out
        assert any('artifact A1' in e and 'hash mismatch' in e for e in out['errors']), out

    def test_waiver_for_another_criterion_does_not_unblock(self, tmp_path):
        """A recorded waiver is per criterion, not a global unlock (state.md)."""
        state, _ = single_deliverable(tmp_path)
        state['acceptance'][0]['status'] = 'unobservable'
        state['waivers'] = [{'criterion_id': 'C9', 'reason': 'third-party API behind a WAF',
                             'approved_by': 'reviewer', 'approved_at': now()}]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert out['errors'] == ['criterion C1: unobservable needs a recorded waiver'], out


# --------------------------------------------------------------------------- #
# second-round findings (fresh-context audit 2026-10-31, all seven)
# --------------------------------------------------------------------------- #

def v1_record(tmp_path, deliverables=None, evidence=None, **extra):
    """Minimal v1 record whose one local deliverable matches the file on disk."""
    art = tmp_path / 'report.md'
    art.write_text('v1 deliverable\n')
    state = {
        'schema_version': 1, 'task_id': 't1', 'goal': 'g', 'status': 'in_progress',
        'artifact_revision': 'r1', 'next_action': 'n',
        'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
        'decisions': [], 'capabilities': [], 'lessons': [],
        'acceptance': [{'id': 'C1', 'criterion': 'report exists', 'status': 'verified',
                        'evidence_ids': ['E1']}],
        'deliverables': [{'kind': 'local', 'locator': str(art), 'sha256': sha(art)}]
                        if deliverables is None else deliverables,
        'evidence': [{'id': 'E1', 'kind': 'local', 'locator': str(art),
                      'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}]
                    if evidence is None else evidence,
    }
    state.update(extra)
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state, indent=2))
    return path, art, state


class TestMigrationSafety:
    """migrate writes user data: malformed input must be refused before any byte changes."""

    def test_malformed_deliverable_is_refused_without_writing(self, tmp_path):
        path, _, _ = v1_record(tmp_path, deliverables=[None])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('deliverables[0]' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_malformed_evidence_is_refused_without_writing(self, tmp_path):
        path, _, _ = v1_record(tmp_path, evidence=[None])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('evidence[0]' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_invalid_conversion_is_refused_before_replacement(self, tmp_path):
        """The converted record must satisfy validate before it may replace the source."""
        path, art, _ = v1_record(tmp_path, evidence=[
            {'id': 'E1', 'kind': 'local', 'locator': str(tmp_path / 'report.md'),
             'artifact_revision': 'r1', 'observed_at': now(), 'result': 'maybe'}])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('invalid result' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_dry_run_refusal_offers_no_migrated_document(self, tmp_path):
        path, _, _ = v1_record(tmp_path, deliverables=[None])
        before = path.read_bytes()
        code, out = call('migrate', path)
        assert code == 1, out
        assert 'migrated' not in out, out
        assert path.read_bytes() == before

    def test_unmapped_metadata_is_carried_losslessly(self, tmp_path):
        """Unknown fields and per-deliverable extras survive; provenance records the origin."""
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, _ = v1_record(
            tmp_path,
            deliverables=[{'kind': 'local', 'locator': str(art), 'sha256': sha(art),
                           'note': 'retain me'}],
            custom_recovery_note='retain this', evaluation_date='2026-12-31')
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['custom_recovery_note'] == 'retain this'
        assert migrated['artifacts'][0]['note'] == 'retain me'
        assert migrated['evaluation_date'] == '2026-12-31'
        assert migrated['provenance']['derived_from'] == {'schema_version': 1,
                                                         'artifact_revision': 'r1'}
        assert call('validate', path)[0] == 0
        assert call('gate', path)[0] == 0
        assert not (tmp_path / 'state.json.tmp').exists(), 'temporary file left behind'

    def test_malformed_v1_array_is_refused_without_writing(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['lessons'] = 'not an array'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('lessons must be an array' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_deliverable_field_that_v2_reserves_is_refused(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['revision'] = 'mine'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any("'revision' is reserved" in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_v2_only_field_in_a_v1_record_is_refused(self, tmp_path):
        """A v1 record that already carries a v2 field cannot be migrated without a collision."""
        path, _, _ = v1_record(tmp_path, provenance={'generated_at': now(), 'writer': 'x'})
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('provenance' in e for e in out['errors']), out
        assert path.read_bytes() == before


class TestMigrationEvidence:
    """Migration never turns an absent or unverified source revision into trusted evidence."""

    def test_absent_source_revision_never_binds_current_revision(self, tmp_path):
        path, art, state = v1_record(tmp_path)
        state.pop('artifact_revision')
        state['evidence'][0].pop('artifact_revision')
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        entry = migrated['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert 'source_revision' not in entry, entry
        assert any('re-verify' in w for w in out['warnings']), out
        assert call('validate', path)[0] == 0
        code, out = call('gate', path)
        assert code == 1, out
        assert any('stale' in e for e in out['errors']), out['errors']

    def test_mixed_kinds_without_a_revision_bind_nothing_as_verified(self, tmp_path):
        """R6 contract (a): an external deliverable must carry its own revision.

        The external entry is given an explicit label so that the record stays
        migratable; the point of the test is the local observation that names no
        revision, which must bind nothing.
        """
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': sha(art)},
            {'kind': 'external', 'locator': 'report.md', 'revision': 'ext-r1'}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': 'report.md',
                       'observed_at': now(), 'result': 'pass'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert set(migrated['evidence'][0]['artifact_revisions'].values()) == {''}, migrated
        assert migrated['evidence'][0]['unverified'] is True
        code, out = call('gate', path)
        assert code == 1, out

    def test_mixed_kinds_with_matching_old_revision_still_fails_closed(self, tmp_path):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': sha(art)},
            {'kind': 'external', 'locator': 'report.md', 'revision': 'ext-r1'}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': 'report.md',
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        revisions = migrated['evidence'][0]['artifact_revisions']
        assert revisions == {'A1': sha(art)[:12]}, revisions
        assert 'A2' not in migrated['evidence'][0]['artifact_ids'], migrated['evidence']
        code, out = call('gate', path)
        assert code == 1, out
        assert any('artifact A2' in e for e in out['errors']), out['errors']

    def test_external_evidence_binds_a_deliverable_owned_revision_label(self, tmp_path):
        """Contract (a): A comes from the deliverable, and only G==E==A is trusted."""
        url = 'https://example.invalid/api'
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'external', 'locator': url, 'revision': 'r1'}],
            evidence=[{'id': 'E1', 'kind': 'external', 'locator': url,
                       'artifact_revision': 'r1', 'observed_at': now(),
                       'result': 'pass', 'summary': 'api answered'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['artifacts'][0]['revision'] == 'r1'
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': 'r1'}
        code, out = call('gate', path)
        assert code == 0, out

    def test_external_evidence_without_a_deliverable_revision_gets_the_global_revision(self, tmp_path):
        """R-P1: v1 has one global revision, so a missing A is G, trusted when E == G.

        Clause: plan §3.1 bullet 1 (orchestrator ruling R-P1) plus the v1 schema in
        references/state.md, which only names kind, locator and (for local files)
        sha256 for a deliverable.
        """
        url = 'https://example.invalid/api'
        path, _, _ = v1_record(tmp_path, deliverables=[{'kind': 'external', 'locator': url}],
                               evidence=[{'id': 'E1', 'kind': 'external', 'locator': url,
                                          'artifact_revision': 'r1', 'observed_at': now(),
                                          'result': 'pass', 'summary': 'api answered'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['artifacts'][0]['revision'] == 'r1', migrated['artifacts']
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': 'r1'}, migrated
        assert 'unverified' not in migrated['evidence'][0], migrated['evidence']
        assert out['warnings'] == [], out['warnings']
        code, out = call('gate', path)
        assert code == 0, out

    def test_mismatched_hash_keeps_the_observed_label_without_claiming_current(self, tmp_path):
        """The recorded v1 label is kept; the current revision is not asserted."""
        path, art, state = v1_record(tmp_path)
        state['deliverables'][0]['sha256'] = 'b' * 64
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        entry = migrated['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert entry['source_revision'] == 'r1', entry
        assert migrated['artifacts'][0]['revision'] != 'r1', migrated['artifacts']
        code, out = call('gate', path)
        assert code == 1, out
        assert any('stale' in e for e in out['errors']), out['errors']


class TestEquivalentLocators:
    """Equivalent local paths are collected; ambiguity and distinct URIs are respected."""

    def test_equivalent_local_locators_are_all_bound(self, tmp_path):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        digest = sha(art)
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': digest},
            {'kind': 'local', 'locator': './report.md', 'sha256': digest}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': str(art),
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['evidence'][0]['artifact_ids'] == ['A1', 'A2'], migrated
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': digest[:12],
                                                                 'A2': digest[:12]}
        code, out = call('gate', path)
        assert code == 0, out

    def test_conflicting_hashes_on_equivalent_locators_are_refused(self, tmp_path):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': sha(art)},
            {'kind': 'local', 'locator': './report.md', 'sha256': 'b' * 64}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': str(art),
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('ambiguous' in e or 'conflict' in e for e in out['errors']), out
        assert path.read_bytes() == before

    def test_distinct_external_uris_are_not_merged(self, tmp_path):
        first = 'https://example.invalid/a'
        second = 'https://example.invalid/a/'
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'external', 'locator': first, 'revision': 'r1'},
            {'kind': 'external', 'locator': second, 'revision': 'r1'}],
            evidence=[{'id': 'E1', 'kind': 'external', 'locator': first,
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass',
                       'summary': 'first answered'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['evidence'][0]['artifact_ids'] == ['A1'], migrated
        code, out = call('gate', path)
        assert code == 1, out
        assert any('artifact A2' in e and 'no evidence binds it' in e for e in out['errors'])

    @pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs privileges on Windows')
    def test_symlink_alias_is_refused(self, tmp_path):
        """A link is not a plain path: migration refuses rather than guessing identity."""
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        digest = sha(art)
        (tmp_path / 'alias.md').symlink_to(art)
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': digest},
            {'kind': 'local', 'locator': 'alias.md', 'sha256': digest}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': 'report.md',
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('plain path' in e or 'symlink' in e for e in out['errors']), out
        assert path.read_bytes() == before


class TestSecretOutputBoundary:
    """Detected values and keys never reach stdout/stderr, on success and on every error path."""

    def test_unknown_artifact_id_is_not_echoed(self, tmp_path):
        path = tmp_path / 'state.json'
        assert call('init', path, '--task-id', 't1', '--goal', 'g')[0] == 0
        proc = run('evidence', path, '--id', 'E1', '--artifact', PROVIDER_TOKEN,
                   '--result', 'pass')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert 'unknown artifact' in json.dumps(payload), payload
        assert PROVIDER_TOKEN not in proc.stdout + proc.stderr

    def test_nested_map_key_is_not_echoed(self, tmp_path):
        path = tmp_path / 'state.json'
        assert call('init', path, '--task-id', 't1', '--goal', 'g')[0] == 0
        code, artifact_out = call('artifact', path, '--id', PROVIDER_TOKEN, '--kind',
                                  'external', '--locator', 'https://example.invalid',
                                  '--revision', 'r1')
        assert code == 0, artifact_out
        code, evidence_out = call('evidence', path, '--id', 'E1', '--artifact',
                                  PROVIDER_TOKEN, '--result', 'pass')
        assert code == 0, evidence_out
        assert PROVIDER_TOKEN not in json.dumps([artifact_out, evidence_out])
        entry = evidence_out['evidence']
        assert entry['artifact_ids'] == [secret_scan.REDACTED], entry
        assert list(entry['artifact_revisions']) == [secret_scan.REDACTED], entry
        assert PROVIDER_TOKEN in path.read_text(), 'the record keeps what the caller stored'

    def test_error_path_is_not_echoed(self, tmp_path):
        secret_path = tmp_path / (PROVIDER_TOKEN + '.json')
        proc = run('validate', secret_path)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False
        assert PROVIDER_TOKEN not in proc.stdout + proc.stderr

    def test_validation_message_does_not_echo_a_value(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['capabilities'] = [{'name': PROVIDER_TOKEN, 'class': 'not-a-class'}]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('invalid class' in e for e in out['errors']), out['errors']
        assert PROVIDER_TOKEN not in json.dumps(out)

    def test_secret_shaped_dict_key_is_redacted_from_a_path(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['lessons'] = [{PROVIDER_TOKEN: 'password = abcdefgh'}]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('«redacted»' in e for e in out['errors']), out['errors']
        assert PROVIDER_TOKEN not in json.dumps(out)

    def test_redacted_keys_do_not_collide_or_drop_entries(self):
        other = 'ghp_' + 'Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3h2'
        cleaned = secret_scan.redact({PROVIDER_TOKEN: 'a', other: 'b', '«redacted»': 'c'})
        assert len(cleaned) == 3, cleaned
        assert list(cleaned.values()).count('c') == 1
        assert PROVIDER_TOKEN not in json.dumps(cleaned)
        assert other not in json.dumps(cleaned)

    def test_internal_fault_is_not_converted_into_a_clean_refusal(self, tmp_path, monkeypatch):
        """Only input errors are caught; a programmer fault must surface, not look clean."""
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)

        def boom(*args, **kwargs):
            raise TypeError('programmer fault')

        monkeypatch.setattr(checkpoint_module, 'inspect', boom)
        monkeypatch.setattr(sys, 'argv', ['checkpoint.py', 'gate', str(path)])
        with pytest.raises(TypeError):
            checkpoint_module.main()

    def test_harmless_output_is_stable_and_unchanged(self, tmp_path):
        state, art = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        code, out = call('gate', path)
        assert code == 0, out
        assert set(out) == {'ok', 'errors', 'warnings', 'scope'}, out
        assert out['errors'] == [] and out['warnings'] == []
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        assert code == 0, out
        assert out['artifact']['locator'].endswith('report.md'), out
        assert secret_scan.REDACTED not in json.dumps(out), 'a harmless path was redacted'

    def test_redaction_does_not_rewrite_the_state(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        code, out = call('evidence', path, '--id', 'E2', '--artifact', 'A1', '--result',
                         'pass', '--summary', 'observed via Authorization: Bearer '
                         + BEARER_TOKEN)
        assert code == 0, out
        assert BEARER_TOKEN not in json.dumps(out)
        assert BEARER_TOKEN in path.read_text(), 'storage keeps the user value unchanged'


class TestMalformedNestedInput:
    """Malformed nested shapes are reported as JSON, never a traceback, and never rewritten."""

    @pytest.mark.parametrize('field', ['artifacts', 'evidence', 'acceptance', 'waivers',
                                       'attempts', 'capabilities', 'shortcuts'])
    def test_null_entry_is_reported_not_fatal(self, tmp_path, field):
        state, _ = single_deliverable(tmp_path)
        state[field].append(None)
        path = write(tmp_path, state)
        before = path.read_bytes()
        for command in ('validate', 'gate'):
            proc = run(command, path)
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, (command, payload)
            assert payload['ok'] is False
            assert 'Traceback' not in proc.stderr, proc.stderr
        assert path.read_bytes() == before

    def test_unhashable_ids_are_reported_not_fatal(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['artifacts'][0]['id'] = []
        state['waivers'] = [{'criterion_id': [], 'reason': 'r', 'approved_by': 'a',
                             'approved_at': now()}]
        state['evidence'][0]['artifact_ids'] = [['A1']]
        path = write(tmp_path, state)
        proc = run('gate', path)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False
        assert 'Traceback' not in proc.stderr, proc.stderr

    def test_mutating_commands_refuse_corrupt_lists(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['evidence'].append(None)
        path = write(tmp_path, state)
        before = path.read_bytes()
        proc = run('evidence', path, '--id', 'E2', '--artifact', 'A1', '--result', 'pass')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert 'Traceback' not in proc.stderr, proc.stderr
        assert path.read_bytes() == before

        state, _ = single_deliverable(tmp_path)
        state['artifacts'].append(None)
        path = write(tmp_path, state)
        before = path.read_bytes()
        proc = run('artifact', path, '--id', 'A2', '--locator', 'report.md')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert 'Traceback' not in proc.stderr, proc.stderr
        assert path.read_bytes() == before


class TestRetryBudgetStrict:
    """A hypothesis change needs a nonempty value on both sides of the attempt."""

    def test_missing_before_is_not_a_hypothesis_change(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [{'blocker_key': 'B1', 'action': 'run %d' % i, 'error': 'e',
                              'hypothesis_after': 'same', 'at': now()} for i in range(3)]
        path = write(tmp_path, state)
        code, out = call('validate', path)
        assert code == 1, out
        assert any('without a hypothesis change' in e for e in out['errors']), out

    def test_both_sides_required_and_a_real_change_still_counts(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [{'blocker_key': 'B1', 'action': 'a', 'error': 'e',
                              'hypothesis_after': 'x', 'at': now()},
                             {'blocker_key': 'B1', 'action': 'b', 'error': 'e',
                              'hypothesis_after': 'x', 'at': now()},
                             {'blocker_key': 'B1', 'action': 'c', 'error': 'e',
                              'hypothesis_before': 'a', 'hypothesis_after': 'b',
                              'at': now()}]
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_missing_before_flags_only_its_blocker(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['attempts'] = [
            attempt('a', 'e', 'h0', 'h1', 'B1'),
            attempt('b', 'e', 'h1', 'h2', 'B1'),
            attempt('c', 'e', 'h2', 'h3', 'B1'),
        ] + [{'blocker_key': 'B2', 'action': 'run %d' % i, 'error': 'e',
              'hypothesis_after': 'same', 'at': now()} for i in range(3)]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        budget = [e for e in out['errors'] if 'without a hypothesis change' in e]
        assert len(budget) == 1 and 'B2' in budget[0], budget


class TestCredentialKeyAliases:
    """Common key spellings are normalised; only syntactic references are exempt."""

    @pytest.mark.parametrize('key', ['clientSecret', 'client_secret', 'CLIENT-SECRET',
                                     'accessToken', 'access_token', 'refreshToken',
                                     'privateKey', 'apiKey', 'API_KEY', 'clientsecret'])
    def test_common_key_variants_are_detected(self, key):
        assert secret_scan.scan_state({key: 'Synthet1cValue!'}) == [
            ('$.' + key, 'credential_key')]

    @pytest.mark.parametrize('value', ['see hiddenSyntheticPassword!',
                                       'see the vault entry prod/db',
                                       'see README for the password'])
    def test_example_style_reference_is_not_trusted(self, value):
        """A phrase starting with 'see' is not a verifiable pointer."""
        assert secret_scan.scan_state({'password': value}) != []

    @pytest.mark.parametrize('value', ['${DB_PASSWORD}', 'env:DB_PASSWORD',
                                       'op://prod/db/password',
                                       'see op://prod/db/password', 'vault://prod/db',
                                       '«redacted»', '<set in .env>', 'none', 'n/a',
                                       'required'])
    def test_syntactic_references_are_exempt(self, value):
        assert secret_scan.scan_state({'password': value}) == []

    @pytest.mark.parametrize('value', ['${DB_PASSWORD} and more',
                                       'env:DB_PASSWORD extra'])
    def test_reference_must_be_the_whole_value(self, value):
        assert secret_scan.scan_state({'password': value}) != []


# --------------------------------------------------------------------------- #
# third-round invariants (audit R3: trust, preservation, identity, output, writes)
# --------------------------------------------------------------------------- #

LONG_HEX = 'ab' * 21
LONG_B64 = 'Zx7Q' * 16
CARD = '4111' + '1111' * 3
IBAN = 'DE89 3704 0044 0532 0130 00'
JWT = ('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.'
       'c2lnbmF0dXJlLXN5bnRoZXRpYw')


class TestMigrationTrust:
    """An unverified observation must stay machine-rejectable, whatever its label says."""

    def test_label_equal_to_the_actual_digest_cannot_pass_gate(self, tmp_path):
        """A v1 evidence label that happens to equal the v2 digest prefix is not a proof."""
        path, art, state = v1_record(tmp_path)
        digest = sha(art)
        state['evidence'][0]['artifact_revision'] = digest[:12]
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        entry = migrated['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert entry['source_revision'] == digest[:12], entry
        code, out = call('gate', path)
        assert code == 1, out
        assert any('unverified' in e for e in out['errors']), out['errors']

    @pytest.mark.parametrize('label', ['deadbeefcafe', 'other-label', '0' * 12])
    def test_other_unverified_labels_cannot_pass_gate(self, tmp_path, label):
        path, _, state = v1_record(tmp_path)
        state['evidence'][0]['artifact_revision'] = label   # != global 'r1'
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert json.loads(path.read_text())['evidence'][0]['artifact_revisions'] == {'A1': ''}
        code, out = call('gate', path)
        assert code == 1, out

    def test_digest_like_label_without_a_global_revision_cannot_pass_gate(self, tmp_path):
        path, art, state = v1_record(tmp_path)
        state.pop('artifact_revision')
        state['evidence'][0]['artifact_revision'] = sha(art)[:12]
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert json.loads(path.read_text())['evidence'][0]['unverified'] is True
        assert call('gate', path)[0] == 1

    def test_genuine_source_label_and_hash_is_the_control(self, tmp_path):
        path, art, _ = v1_record(tmp_path)
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': sha(art)[:12]}
        assert 'unverified' not in entry
        code, out = call('gate', path)
        assert code == 0, out

    def test_global_label_equal_to_the_digest_is_genuine(self, tmp_path):
        """When the record itself names the content digest, the match is real."""
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        digest = sha(art)
        path, _, state = v1_record(tmp_path, artifact_revision=digest[:12])
        state['evidence'][0]['artifact_revision'] = digest[:12]
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert 'unverified' not in json.loads(path.read_text())['evidence'][0]
        assert call('gate', path)[0] == 0


class TestMigrationPreservation:
    """Reserved nested fields are refused; present optional fields survive by presence."""

    def test_reserved_evidence_fields_are_refused(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['evidence'][0]['artifact_ids'] = ['DO-NOT-DROP']
        state['evidence'][0]['artifact_revisions'] = {'DO-NOT-DROP': 'original'}
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('artifact_ids' in e for e in out['errors']), out
        assert path.read_bytes() == before

    @pytest.mark.parametrize('value', ['', None])
    def test_falsey_evaluation_date_is_preserved(self, tmp_path, value):
        path, _, state = v1_record(tmp_path)
        state['evaluation_date'] = value
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert 'evaluation_date' in migrated, migrated
        assert migrated['evaluation_date'] == value
        assert call('validate', path)[0] == 0

    def test_falsey_unknown_metadata_is_preserved(self, tmp_path):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, state = v1_record(
            tmp_path,
            deliverables=[{'kind': 'local', 'locator': str(art), 'sha256': sha(art),
                           'note': 0}],
            zero=0, empty='', nothing=None, false=False)
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['zero'] == 0 and 'zero' in migrated
        assert migrated['empty'] == '' and 'empty' in migrated
        assert migrated['nothing'] is None and 'nothing' in migrated
        assert migrated['false'] is False and 'false' in migrated
        assert migrated['artifacts'][0]['note'] == 0

    def test_boolean_supporting_is_preserved_not_refused(self, tmp_path):
        """Contract (a): `supporting` is data, not a reserved name, when it is boolean."""
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['supporting'] = True
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert json.loads(path.read_text())['artifacts'][0]['supporting'] is True

    def test_non_boolean_supporting_is_refused(self, tmp_path):
        """A semantically colliding value is refused before any write, byte for byte."""
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['supporting'] = 'yes'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('supporting' in e and 'boolean' in e for e in out['errors']), out['errors']
        assert path.read_bytes() == before


class TestMigrationIdentity:
    """Identity is the file the kernel reaches without following a link, not a string."""

    def test_symlink_parent_traversal_is_refused(self, tmp_path):
        (tmp_path / 'outside' / 'inner').mkdir(parents=True)
        (tmp_path / 'outside' / 'file').write_text('different synthetic content')
        (tmp_path / 'alias').symlink_to(tmp_path / 'outside' / 'inner')
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, state = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': 'report.md', 'sha256': sha(art)}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': 'alias/../report.md',
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('plain path' in e or 'symlink' in e for e in out['errors']), out
        assert path.read_bytes() == before
        assert call('gate', path)[0] == 1

    def test_missing_local_file_is_written_unverified(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['locator'] = 'gone.md'
        state['evidence'][0]['locator'] = 'gone.md'
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}
        assert entry['unverified'] is True
        assert call('gate', path)[0] == 1

    def test_state_file_symlink_is_refused(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        victim = tmp_path / 'victim.json'
        victim.write_text(json.dumps(state))
        link = tmp_path / 'link.json'
        link.symlink_to(victim)
        proc = run('validate', link)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False
        assert victim.read_text() == json.dumps(state)


class TestOutputShapeBoundary:
    """Every shape the scanner detects is masked on output; hash fields survive."""

    @pytest.mark.parametrize('value', [LONG_HEX, LONG_B64, CARD, IBAN, PROVIDER_TOKEN, JWT])
    def test_detected_shapes_are_masked_in_evidence_output(self, tmp_path, value):
        assert secret_scan.scan_text(value), 'control: the value is detected'
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        code, out = call('evidence', path, '--id', 'E2', '--artifact', 'A1',
                         '--result', 'pass', '--summary', value)
        assert code == 0, out
        assert value not in json.dumps(out), 'detected value echoed'
        assert value in path.read_text(), 'storage must keep the caller value'

    def test_legitimate_hash_fields_survive_output(self, tmp_path):
        state, art = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        digest = sha(art)
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        assert code == 0, out
        assert out['artifact']['sha256'] == digest, out
        assert out['artifact']['revision'] == digest[:12], out
        code, out = call('hash', art)
        assert code == 0 and out['sha256'] == digest and out['revision'] == digest[:12]

    def test_detected_shape_in_a_dict_key_is_renamed(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['lessons'] = [{LONG_HEX: 'password = abcdefgh'}]
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert LONG_HEX not in json.dumps(out)
        assert 'redacted' in json.dumps(out)

    @pytest.mark.parametrize('value', [PROVIDER_TOKEN, LONG_HEX])
    def test_argparse_error_does_not_echo_an_invalid_choice(self, tmp_path, value):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        proc = run('evidence', path, '--id', 'E1', '--artifact', 'A1', '--result', value)
        assert proc.returncode == 2, proc
        assert value not in proc.stdout + proc.stderr
        assert 'usage' in proc.stderr.lower(), proc.stderr

    def test_valid_cli_output_is_unchanged(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        proc = run('gate', path)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0 and payload['ok'] is True


class TestAtomicWrite:
    """Writes never touch a pre-existing path, never follow a link, keep the mode."""

    def test_preexisting_state_tmp_regular_is_untouched(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        canary = tmp_path / 'state.json.tmp'
        canary.write_text('synthetic unrelated recovery content')
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        assert code == 0, out
        assert canary.read_text() == 'synthetic unrelated recovery content'

    def test_preexisting_state_tmp_symlink_does_not_touch_its_victim(self, tmp_path):
        path, art, _ = v1_record(tmp_path)
        victim = tmp_path / 'victim.bin'
        victim.write_text('synthetic keep')
        canary = tmp_path / 'state.json.tmp'
        canary.symlink_to(victim)
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert victim.read_text() == 'synthetic keep'
        assert canary.is_symlink()

    @pytest.mark.skipif(os.name == 'nt', reason='umask semantics differ on Windows')
    def test_existing_mode_0600_survives_a_permissive_umask(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        os.chmod(path, 0o600)
        previous = os.umask(0)
        try:
            code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        finally:
            os.umask(previous)
        assert code == 0, out
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)

    def test_concurrent_modification_is_refused(self, tmp_path):
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        raw = path.read_bytes()
        newer = json.loads(raw)
        newer['goal'] = 'changed by another writer'
        path.write_text(json.dumps(newer))
        with pytest.raises(checkpoint_module.RecordError):
            checkpoint_module.write_state(path, state, raw)
        assert json.loads(path.read_text())['goal'] == 'changed by another writer'

    def test_failed_replace_leaves_original_and_no_temp(self, tmp_path, monkeypatch):
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        before = path.read_bytes()
        names = {item.name for item in tmp_path.iterdir()}

        def boom(*args, **kwargs):
            raise OSError('synthetic replace failure')

        monkeypatch.setattr(checkpoint_module.os, 'replace', boom)
        with pytest.raises(OSError):
            checkpoint_module.write_state(path, state, before)
        assert path.read_bytes() == before
        assert {item.name for item in tmp_path.iterdir()} == names

    def test_directory_target_is_refused(self, tmp_path):
        target = tmp_path / 'state.json'
        target.mkdir()
        proc = run('validate', target)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False

    def test_init_creates_a_private_file(self, tmp_path):
        path = tmp_path / 'state.json'
        assert call('init', path, '--task-id', 't1', '--goal', 'g')[0] == 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)

    def test_success_leaves_no_extra_files(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        names = {item.name for item in tmp_path.iterdir()}
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        assert code == 0, out
        assert {item.name for item in tmp_path.iterdir()} == names


# --------------------------------------------------------------------------- #
# R6: the written three-way source-revision contract (plan 3a)
# --------------------------------------------------------------------------- #

EXTERNAL_URL = 'https://example.invalid/item'


def run_bounded(*args, timeout=1.5):
    """A path that blocks must be reported as a timeout, not as a hung suite."""
    return subprocess.run([sys.executable, SCRIPT] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=timeout)


def external_v1(tmp_path, record_revision='g1', observation='g1', deliverable='g1',
                supporting=None, unknown=None):
    """A v1 record whose only deliverable and only observation are external.

    G = state.artifact_revision, E = evidence.artifact_revision,
    A = deliverable.revision. Each argument may be None to omit it.
    """
    deliverable_entry = {'kind': 'external', 'locator': EXTERNAL_URL}
    if deliverable is not None:
        deliverable_entry['revision'] = deliverable
    if supporting is not None:
        deliverable_entry['supporting'] = supporting
    if unknown:
        deliverable_entry.update(unknown)
    evidence_entry = {'id': 'E1', 'kind': 'external', 'locator': EXTERNAL_URL,
                      'observed_at': now(), 'result': 'pass', 'summary': 'endpoint answered'}
    if observation is not None:
        evidence_entry['artifact_revision'] = observation
    state = {
        'schema_version': 1, 'task_id': 't1', 'goal': 'g', 'status': 'in_progress',
        'next_action': 'n', 'constraints': [], 'milestones': [], 'blockers': [],
        'attempts': [], 'decisions': [], 'capabilities': [], 'lessons': [],
        'acceptance': [{'id': 'C1', 'criterion': 'the item is reachable',
                        'status': 'verified', 'evidence_ids': ['E1']}],
        'deliverables': [deliverable_entry], 'evidence': [evidence_entry],
    }
    if record_revision is not None:
        state['artifact_revision'] = record_revision
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state, indent=2))
    return path


class TestSourceRevisionContract:
    """External provenance is trusted only when G, E and A are non-empty and equal.

    A difference is a CONFLICT: every binding is emptied, the observation is
    marked unverified with a deterministic reason, the v1 label is kept as
    ``source_revision`` and a warning is shown. There is no precedence rule.
    """

    @pytest.mark.parametrize('record_revision,observation,deliverable', [
        ('global-good', 'stale-rev', 'stale-rev'),   # E == A, G differs: the R4 finding
        ('r1', 'other', 'r1'),                       # G == A, E differs
        ('r1', 'r1', 'other'),                       # G == E, A differs
        (None, 'r1', 'r1'),                          # G absent: nothing to compare against
    ])
    def test_a_disagreeing_or_incomplete_triple_is_unverified(
            self, tmp_path, record_revision, observation, deliverable):
        path = external_v1(tmp_path, record_revision, observation, deliverable)
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        assert path.read_bytes() != before, 'the record is migratable, only untrusted'
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_ids'] == ['A1'], entry
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert isinstance(entry['unverified_reason'], str) and entry['unverified_reason'].strip()
        assert entry['source_revision'] == observation, entry
        assert any('re-verify' in warning for warning in out['warnings']), out['warnings']
        code, out = call('gate', path)
        assert code == 1, out
        assert any('unverified' in error for error in out['errors']), out['errors']

    def test_a_trusted_triple_binds_the_deliverable_revision(self, tmp_path):
        """Clean control: the same shape with G == E == A stays verified."""
        path = external_v1(tmp_path, 'r1', 'r1', 'r1')
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': 'r1'}, entry
        assert 'unverified' not in entry, entry
        assert out['warnings'] == [], out['warnings']
        code, out = call('gate', path)
        assert code == 0, out

    def test_a_missing_observation_revision_is_unverified(self, tmp_path):
        path = external_v1(tmp_path, 'r1', None, 'r1')
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert 'source_revision' not in entry, entry
        assert call('gate', path)[0] == 1

    def test_a_missing_deliverable_revision_falls_back_to_the_record_revision(self, tmp_path):
        """R-P1 clean control: A := G, and G == E keeps the observation verified.

        Clause: plan §3.1 bullet 1 (orchestrator ruling R-P1); the v1 schema in
        references/state.md gives an external deliverable only kind and locator,
        so one global revision is the documented meaning of A.
        """
        path = external_v1(tmp_path, 'g1', 'g1', None)
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['artifacts'][0]['revision'] == 'g1', migrated['artifacts']
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': 'g1'}, migrated
        assert 'unverified' not in migrated['evidence'][0], migrated['evidence']
        assert out['warnings'] == [], out['warnings']
        code, out = call('gate', path)
        assert code == 0, out

    def test_the_fallback_conflicts_when_the_observation_names_another_revision(self, tmp_path):
        """R-P1 dirty case: A := G but E != G is still a CONFLICT, never trusted."""
        path = external_v1(tmp_path, 'g1', 'other', None)
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        migrated = json.loads(path.read_text())
        assert migrated['artifacts'][0]['revision'] == 'g1', migrated['artifacts']
        entry = migrated['evidence'][0]
        assert entry['artifact_revisions'] == {'A1': ''}, entry
        assert entry['unverified'] is True, entry
        assert entry['source_revision'] == 'other', entry
        assert entry['unverified_reason'].strip(), entry
        assert any('re-verify' in warning for warning in out['warnings']), out['warnings']
        code, out = call('gate', path)
        assert code == 1, out

    def test_the_fallback_is_refused_without_a_global_revision(self, tmp_path):
        """R-P1 dirty case: no A and no G means there is nothing to fall back to."""
        path = external_v1(tmp_path, None, 'g1', None)
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('global revision' in error for error in out['errors']), out['errors']
        assert 'migrated' not in out, out
        assert path.read_bytes() == before, 'a refusal must not write'

    def test_an_empty_deliverable_revision_is_refused(self, tmp_path):
        path = external_v1(tmp_path, 'g1', 'g1', '')
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert path.read_bytes() == before

    def test_the_record_revision_is_kept_in_provenance_for_an_untrusted_triple(self, tmp_path):
        """The v1 global label is still recorded as provenance, not as a binding."""
        path = external_v1(tmp_path, 'global-good', 'stale-rev', 'stale-rev')
        assert call('migrate', path, '--write')[0] == 0
        migrated = json.loads(path.read_text())
        assert migrated['provenance']['derived_from'] == {'schema_version': 1,
                                                          'artifact_revision': 'global-good'}
        assert migrated['evidence'][0]['artifact_revisions'] == {'A1': ''}

    def test_an_unverified_observation_has_no_partial_bindings(self, tmp_path):
        """C6: if one binding of an observation cannot be proven, none of them is kept.

        Two external deliverables share a locator; only A1 matches the observation,
        so the old code wrote {'A1': 'g1', 'A2': ''} for an unverified entry.
        """
        url = 'https://example.invalid/item'
        path, _, _ = v1_record(
            tmp_path, artifact_revision='g1',
            deliverables=[{'kind': 'external', 'locator': url, 'revision': 'g1'},
                          {'kind': 'external', 'locator': url, 'revision': 'other'}],
            evidence=[{'id': 'E1', 'kind': 'external', 'locator': url,
                       'artifact_revision': 'g1', 'observed_at': now(), 'result': 'pass',
                       'summary': 'seen'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['artifact_ids'] == ['A1', 'A2'], entry
        assert entry['artifact_revisions'] == {'A1': '', 'A2': ''}, entry
        assert entry['unverified'] is True, entry
        code, out = call('gate', path)
        assert code == 1, out


class TestFieldPreservation:
    """Only the documented mappings may change; everything else survives verbatim."""

    def test_boolean_supporting_survives_for_local_and_external(self, tmp_path):
        art = tmp_path / 'report.md'
        art.write_text('v1 deliverable\n')
        path, _, _ = v1_record(tmp_path, deliverables=[
            {'kind': 'local', 'locator': str(art), 'sha256': sha(art), 'supporting': True},
            {'kind': 'external', 'locator': EXTERNAL_URL, 'revision': 'r1',
             'supporting': False, 'custom': {'keep': [1, None, False]}}],
            evidence=[{'id': 'E1', 'kind': 'local', 'locator': str(art),
                       'artifact_revision': 'r1', 'observed_at': now(), 'result': 'pass'}])
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        local, external = json.loads(path.read_text())['artifacts']
        assert local['supporting'] is True, local
        assert external['supporting'] is False, external
        assert type(external['supporting']) is bool, external
        assert external['custom'] == {'keep': [1, None, False]}, external

    def test_non_boolean_supporting_is_refused(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['supporting'] = 'yes'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        code, out = call('migrate', path, '--write')
        assert code == 1, out
        assert any('supporting' in error and 'boolean' in error
                   for error in out['errors']), out['errors']
        assert path.read_bytes() == before

    def test_unknown_evidence_fields_survive(self, tmp_path):
        path, art, state = v1_record(tmp_path)
        state['evidence'][0]['custom_note'] = {'nested': [0, '', None, False]}
        state['evidence'][0]['empty_string'] = ''
        path.write_text(json.dumps(state))
        code, out = call('migrate', path, '--write')
        assert code == 0, out
        entry = json.loads(path.read_text())['evidence'][0]
        assert entry['custom_note'] == {'nested': [0, '', None, False]}, entry
        assert entry['empty_string'] == '', entry


class TestFieldPreservationProperty:
    """Round-trip property over a generated corpus of v1 records.

    The corpus mixes falsey values (0, '', None, False, [], {}), nested values,
    unicode, unknown keys, boolean ``supporting`` and both deliverable kinds.
    Only documented mappings may change; every other key must keep its exact
    key, type and value.
    """

    WORDS = ('alpha', 'beta', 'gamma', 'delta', 'ünïcode', '日本語', 'naïve café',
             'two words', '--flag=value', 'a/b', 'x:y')

    # Documented mappings of a v1 record (checkpoint.py V1_FIELDS).
    MAPPED_TOP = frozenset({'schema_version', 'task_id', 'goal', 'status', 'next_action',
                            'artifact_revision', 'deliverables', 'evidence', 'constraints',
                            'milestones', 'acceptance', 'blockers', 'attempts', 'decisions',
                            'capabilities', 'lessons', 'evaluation_date'})
    MAPPED_EVIDENCE = frozenset({'artifact_revision'})

    def value(self, rng, depth=0):
        choice = rng.randrange(7 if depth < 2 else 5)
        if choice == 0:
            return None
        if choice == 1:
            return rng.choice([True, False])
        if choice == 2:
            return rng.choice([0, 1, -7, 42, 2 ** 40])
        if choice == 3:
            return rng.choice([0.0, 1.5, -0.25])
        if choice == 4:
            return rng.choice(['', 'plain', ' '.join(rng.sample(self.WORDS, 2))])
        if choice == 5:
            return [self.value(rng, depth + 1) for _ in range(2)]
        return {'nested': self.value(rng, depth + 1), 'k': rng.choice(self.WORDS)}

    def field(self, rng, taken):
        while True:
            name = rng.choice(['note_%d', 'ä_meta_%d', 'x %d', 'extra_%d', 'Field%d'])
            name = name % rng.randrange(10000)
            if name not in taken:
                taken.add(name)
                return name

    def build(self, rng, tmp_path, index):
        art = tmp_path / ('report-%d.md' % index)
        art.write_text('generated deliverable %d\n' % index)
        digest = sha(art)
        top_taken = set()
        top = {self.field(rng, top_taken): self.value(rng) for _ in range(3)}
        top.update(zero=0, empty='', nothing=None, false=False,
                   nested={'deep': [1, {'x': None}, '日本']}, unicode='naïve — ünïcode')
        extra_taken = set()
        local = {'kind': 'local', 'locator': str(art), 'sha256': digest,
                 'supporting': rng.choice([True, False])}
        for _ in range(2):
            local[self.field(rng, extra_taken)] = self.value(rng)
        external = {'kind': 'external', 'locator': 'https://example.invalid/%d' % index,
                    'supporting': rng.choice([True, False])}
        if index % 2:
            # R-P1: half the corpus carries an explicit A, half falls back to G.
            external['revision'] = 'ext-%d' % index
        for _ in range(2):
            external[self.field(rng, extra_taken)] = self.value(rng)
        evidence_taken = set()
        record_revision = rng.choice(['g%d' % index, 'g-shared'])
        local_observation = rng.choice([record_revision, 'other-%d' % index, None])
        external_observation = rng.choice([record_revision, 'ext-%d' % index, None])
        local_ev = {'id': 'E1', 'kind': 'local', 'locator': str(art),
                    'observed_at': now(), 'result': 'pass'}
        if local_observation is not None:
            local_ev['artifact_revision'] = local_observation
        for _ in range(2):
            local_ev[self.field(rng, evidence_taken)] = self.value(rng)
        external_ev = {'id': 'E2', 'kind': 'external', 'locator': external['locator'],
                       'observed_at': now(), 'result': 'pass',
                       'summary': 'generated observation'}
        if external_observation is not None:
            external_ev['artifact_revision'] = external_observation
        for _ in range(2):
            external_ev[self.field(rng, evidence_taken)] = self.value(rng)
        state = {
            'schema_version': 1, 'task_id': 't1', 'goal': 'g', 'status': 'in_progress',
            'next_action': 'n', 'artifact_revision': record_revision,
            'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
            'decisions': [], 'capabilities': [], 'lessons': [],
            'acceptance': [{'id': 'C1', 'criterion': 'generated criterion',
                            'status': 'verified', 'evidence_ids': ['E1', 'E2']}],
            'deliverables': [local, external],
            'evidence': [local_ev, external_ev],
        }
        state.update(top)
        return state

    def test_unknown_fields_survive_with_identical_key_type_and_value(self, tmp_path):
        rng = random.Random(20261031)
        corpus = 40
        checked = 0
        for index in range(corpus):
            state = self.build(rng, tmp_path, index)
            path = tmp_path / ('state-%d.json' % index)
            path.write_text(json.dumps(state, ensure_ascii=False))
            code, out = call('migrate', path, '--write')
            assert code == 0, (index, out)
            migrated = json.loads(path.read_text())
            for key, value in state.items():
                if key in self.MAPPED_TOP:
                    continue
                assert key in migrated, (index, key, 'dropped')
                assert type(migrated[key]) is type(value), (index, key, 'type changed')
                assert migrated[key] == value, (index, key, migrated[key], value)
                checked += 1
            for source, target in zip(state['deliverables'], migrated['artifacts']):
                for key, value in source.items():
                    assert key in target, (index, key, 'deliverable field dropped', target)
                    assert type(target[key]) is type(value), (index, key, 'type changed')
                    assert target[key] == value, (index, key, target[key], value)
                    checked += 1
            for source, target in zip(state['evidence'], migrated['evidence']):
                for key, value in source.items():
                    if key in self.MAPPED_EVIDENCE:
                        continue
                    assert key in target, (index, key, 'evidence field dropped', target)
                    assert type(target[key]) is type(value), (index, key, 'type changed')
                    assert target[key] == value, (index, key, target[key], value)
                    checked += 1
                if 'artifact_revision' in source and target.get('unverified') is True:
                    assert target['source_revision'] == source['artifact_revision'], target
        assert checked > corpus * 20, checked


class TestUnverifiedRepresentation:
    """`unverified` is optional; when present it is exactly true and needs a reason."""

    OMIT = object()

    def v2_with_marker(self, tmp_path, marker, reason=True):
        state, _ = single_deliverable(tmp_path)
        entry = state['evidence'][0]
        if marker is not self.OMIT:
            entry['unverified'] = marker
        if reason:
            entry['unverified_reason'] = 'synthetic observation was not re-observed'
        return state

    def test_unverified_false_is_invalid(self, tmp_path):
        code, out = gate(tmp_path, self.v2_with_marker(tmp_path, False))
        assert code == 1, out
        assert any('exactly true' in error for error in out['errors']), out['errors']

    def test_unverified_null_is_invalid(self, tmp_path):
        code, out = gate(tmp_path, self.v2_with_marker(tmp_path, None))
        assert code == 1, out
        assert any('exactly true' in error for error in out['errors']), out['errors']

    def test_unverified_true_without_a_reason_is_invalid(self, tmp_path):
        path = write(tmp_path, self.v2_with_marker(tmp_path, True, reason=False))
        code, out = call('validate', path)
        assert code == 1, out
        assert any('unverified_reason' in error for error in out['errors']), out['errors']

    def test_gate_refuses_unverified_whatever_the_binding_says(self, tmp_path):
        """The marker wins over a binding that happens to equal the revision."""
        state = self.v2_with_marker(tmp_path, True)
        assert state['evidence'][0]['artifact_revisions'] == {
            'A1': state['artifacts'][0]['revision']}
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('unverified' in error for error in out['errors']), out['errors']

    def test_absent_marker_is_a_verified_observation(self, tmp_path):
        """Clean control for the representation: omission means verified."""
        state = self.v2_with_marker(tmp_path, self.OMIT, reason=False)
        assert 'unverified' not in state['evidence'][0]
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_source_revision_without_the_marker_is_invalid(self, tmp_path):
        """Audit R6 observation 7: the v1 label only describes an unverified observation.

        Deleting `unverified` while keeping `source_revision` (and forging the
        binding to the current revision) was the measured gate-0 route.
        """
        state = self.v2_with_marker(tmp_path, self.OMIT, reason=False)
        state['evidence'][0]['source_revision'] = 'old-label'
        path = write(tmp_path, state)
        code, out = call('validate', path)
        assert code == 1, out
        assert any('source_revision' in error for error in out['errors']), out['errors']
        code, out = call('gate', path)
        assert code == 1, out


class TestDescriptorAnchoredPaths:
    """One descriptor-anchored walker: every component O_NOFOLLOW, final O_NONBLOCK."""

    def test_symlinked_state_parent_is_refused(self, tmp_path):
        real = tmp_path / 'real'
        real.mkdir()
        state, _ = single_deliverable(real)
        path = real / 'state.json'
        path.write_text(json.dumps(state))
        alias = tmp_path / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        before = path.read_bytes()
        for command in ('validate', 'gate'):
            proc = run_bounded(command, alias / 'state.json')
            payload = json.loads(proc.stdout)
            assert proc.returncode == 1, (command, payload)
            assert payload['ok'] is False, payload
            assert 'Traceback' not in proc.stderr, proc.stderr
            assert 'symlink' in json.dumps(payload).lower(), payload
        assert path.read_bytes() == before, 'a refused read must not rewrite the state'

    def test_symlinked_state_parent_does_not_migrate(self, tmp_path):
        real = tmp_path / 'real'
        real.mkdir()
        path, _, _ = v1_record(real)
        alias = tmp_path / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        before = path.read_bytes()
        proc = run_bounded('migrate', alias / 'state.json', '--write')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False, payload
        assert 'symlink' in json.dumps(payload).lower(), payload
        assert path.read_bytes() == before
        assert json.loads(path.read_text())['schema_version'] == 1

    def test_state_fifo_is_refused_within_the_bound(self, tmp_path):
        fifo = tmp_path / 'fifo'
        os.mkfifo(fifo)
        try:
            proc = run_bounded('validate', fifo)
        except subprocess.TimeoutExpired:
            pytest.fail('validate blocked on a FIFO instead of refusing it')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False, payload
        assert 'regular file' in json.dumps(payload), payload

    def test_migration_refuses_a_fifo_locator_within_the_bound(self, tmp_path):
        path, _, state = v1_record(tmp_path)
        state['deliverables'][0]['locator'] = 'fifo'
        path.write_text(json.dumps(state))
        os.mkfifo(tmp_path / 'fifo')
        before = path.read_bytes()
        try:
            proc = run_bounded('migrate', path, '--write')
        except subprocess.TimeoutExpired:
            pytest.fail('migration blocked on a FIFO instead of refusing it')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert 'regular file' in json.dumps(payload), payload
        assert path.read_bytes() == before

    def test_gate_refuses_a_fifo_artifact_with_the_file_type_reason(self, tmp_path):
        os.mkfifo(tmp_path / 'pipe')
        artifact = {'id': 'A1', 'kind': 'local', 'locator': 'pipe',
                    'sha256': 'a' * 64, 'revision': 'a' * 12}
        state = record(artifacts=[artifact], evidence=[local_evidence('E1', [artifact])],
                       acceptance=[{'id': 'C1', 'criterion': 'c', 'status': 'verified',
                                    'evidence_ids': ['E1']}])
        proc = run_bounded('gate', write(tmp_path, state))
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert any('regular file' in error for error in payload['errors']), payload['errors']

    @pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs privileges on Windows')
    def test_gate_refuses_a_symlinked_local_artifact(self, tmp_path):
        target = tmp_path / 'target.md'
        target.write_text('synthetic content\n')
        (tmp_path / 'link.md').symlink_to(target)
        artifact = {'id': 'A1', 'kind': 'local', 'locator': 'link.md',
                    'sha256': sha(target), 'revision': sha(target)[:12]}
        state = record(artifacts=[artifact], evidence=[local_evidence('E1', [artifact])],
                       acceptance=[{'id': 'C1', 'criterion': 'c', 'status': 'verified',
                                    'evidence_ids': ['E1']}])
        code, out = call('gate', write(tmp_path, state))
        assert code == 1, out
        assert any('symlink' in error for error in out['errors']), out['errors']
        assert sha(target) not in json.dumps(out), 'the refusal must not hash the target'

    @pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs privileges on Windows')
    def test_hash_refuses_a_symlinked_file(self, tmp_path):
        target = tmp_path / 'target.md'
        target.write_text('synthetic content\n')
        link = tmp_path / 'link.md'
        link.symlink_to(target)
        proc = run_bounded('hash', link)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert payload['ok'] is False, payload
        assert 'symlink' in json.dumps(payload).lower(), payload

    def test_hash_refuses_a_fifo_within_the_bound(self, tmp_path):
        fifo = tmp_path / 'fifo'
        os.mkfifo(fifo)
        try:
            proc = run_bounded('hash', fifo)
        except subprocess.TimeoutExpired:
            pytest.fail('hash blocked on a FIFO')
        payload = json.loads(proc.stdout)
        assert proc.returncode == 1, payload
        assert 'regular file' in json.dumps(payload), payload

    @pytest.mark.skipif(os.name == 'nt', reason='symlink creation needs privileges on Windows')
    def test_artifact_refuses_a_symlinked_local_file(self, tmp_path):
        path = tmp_path / 'state.json'
        assert call('init', path, '--task-id', 't1', '--goal', 'g')[0] == 0
        target = tmp_path / 'target.md'
        target.write_text('synthetic content\n')
        (tmp_path / 'link.md').symlink_to(target)
        before = path.read_bytes()
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'link.md')
        assert code == 1, out
        assert 'symlink' in json.dumps(out).lower(), out
        assert path.read_bytes() == before

    def test_missing_traversal_capability_fails_closed(self, tmp_path, monkeypatch):
        """No realpath() fallback: a platform that cannot anchor must refuse."""
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        raw = path.read_bytes()
        monkeypatch.setattr(checkpoint_module, '_CAN_WALK', False, raising=False)
        with pytest.raises(checkpoint_module.RecordError) as reading:
            checkpoint_module.load(path)
        assert 'descriptor' in str(reading.value), str(reading.value)
        with pytest.raises(checkpoint_module.RecordError) as writing:
            checkpoint_module.write_state(path, state, raw)
        assert 'descriptor' in str(writing.value), str(writing.value)
        assert path.read_bytes() == raw


class TestAtomicWriteSecondCompare:
    """Atomic whole-file write; refusal if the file changed up to the second check.

    Exactly this promise, no more: no serialization against a writer after the
    second comparison and no cross-process lock.
    """

    def test_a_writer_between_the_checks_is_not_overwritten(self, tmp_path, monkeypatch):
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        before = path.read_bytes()
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def interleave(*args, **kwargs):
            path.write_bytes(b'synthetic concurrent write')
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', interleave)
        with pytest.raises(checkpoint_module.RecordError):
            checkpoint_module.write_state(path, state, before)
        assert path.read_bytes() == b'synthetic concurrent write'
        assert not list(tmp_path.glob('.state.json.*.tmp')), 'temporary file left behind'

    def test_a_replaced_inode_with_identical_bytes_is_not_overwritten(self, tmp_path, monkeypatch):
        """Same bytes, different inode: the second comparison is bytes *and* identity."""
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        before = path.read_bytes()
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def swap(*args, **kwargs):
            other = tmp_path / 'other.json'
            other.write_bytes(before)
            os.replace(other, path)
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', swap)
        with pytest.raises(checkpoint_module.RecordError):
            checkpoint_module.write_state(path, state, before)
        assert path.read_bytes() == before
        assert not list(tmp_path.glob('.state.json.*.tmp')), 'temporary file left behind'


class TestWriteDirectoryAnchor:
    """The temp file must be the one held, in the directory the walk verified."""

    def swapped_parent(self, tmp_path, monkeypatch):
        """Rename the real directory and point the old name at another directory."""
        import checkpoint as checkpoint_module
        real = tmp_path / 'real'
        real.mkdir()
        state, _ = single_deliverable(real)
        path = real / 'state.json'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def swap(*args, **kwargs):
            real.rename(tmp_path / 'moved')
            real.symlink_to(elsewhere, target_is_directory=True)
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', swap)
        return checkpoint_module, state, path, before, elsewhere

    def test_parent_swapped_to_symlink_between_walk_and_mkstemp_is_refused_and_leaves_no_temp(
            self, tmp_path, monkeypatch):
        """Audit R6 C4b: the temp must not be usable after the parent was redirected."""
        checkpoint_module, state, path, before, elsewhere = self.swapped_parent(tmp_path,
                                                                              monkeypatch)
        with pytest.raises(checkpoint_module.RecordError) as refused:
            checkpoint_module.write_state(path, state, before)
        assert 'verified directory' in str(refused.value), str(refused.value)
        assert list(elsewhere.iterdir()) == [], 'temporary file left in the redirected directory'
        assert (tmp_path / 'moved' / 'state.json').read_bytes() == before

    def test_no_record_bytes_are_written_outside_the_verified_directory(
            self, tmp_path, monkeypatch):
        """A6: without the seen/held check the record bytes reach the redirected directory.

        The redirected directory is made read-only after mkstemp so the stray file
        survives cleanup; the refusal must happen before any byte is written.
        """
        import checkpoint as checkpoint_module
        real = tmp_path / 'real'
        real.mkdir()
        state, _ = single_deliverable(real)
        path = real / 'state.json'
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def swap(*args, **kwargs):
            real.rename(tmp_path / 'moved')
            real.symlink_to(elsewhere, target_is_directory=True)
            created = real_mkstemp(*args, **kwargs)
            os.chmod(elsewhere, 0o500)
            return created

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', swap)
        try:
            with pytest.raises(checkpoint_module.RecordError):
                checkpoint_module.write_state(path, state, before)
            strays = list(elsewhere.iterdir())
            assert len(strays) == 1, strays
            assert strays[0].read_bytes() == b'', 'record bytes were written outside the walk'
            assert (tmp_path / 'moved' / 'state.json').read_bytes() == before
        finally:
            os.chmod(elsewhere, 0o700)
            for stray in elsewhere.iterdir():
                stray.unlink()

    def test_the_cleanup_removes_the_anchored_temp_and_leaves_a_same_named_decoy(
            self, tmp_path, monkeypatch, capsys):
        """R8 final audit MINOR: cleanup by name would delete a decoy, not the temp.

        The parent is redirected after mkstemp and the target is replaced by a new
        inode that carries the identical bytes, so the second comparison refuses.
        The temp file still lives in the directory the walk verified, while its
        lexical name now resolves through the symlink into the other directory,
        where a decoy of exactly that name waits.
        """
        import checkpoint as checkpoint_module
        real = tmp_path / 'real'
        real.mkdir()
        state, artifact = single_deliverable(real)
        path = real / 'state.json'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        before = path.read_bytes()
        moved = tmp_path / 'moved'
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        seen = {}
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def swap(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            seen['name'] = name
            # The target becomes a new inode with the same bytes: the second
            # comparison must refuse while the bytes stay exactly as they were.
            fresh = real / '.state.json.next'
            fresh.write_bytes(before)
            os.replace(fresh, path)
            # Redirect the parent: the lexical temp path now names another directory.
            real.rename(moved)
            real.symlink_to(elsewhere, target_is_directory=True)
            decoy = elsewhere / os.path.basename(name)
            decoy.write_bytes(b'decoy\n')
            seen['decoy'] = decoy
            return fd, name

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', swap)
        monkeypatch.setattr(sys, 'argv', ['checkpoint.py', 'artifact', str(path),
                                          '--id', 'A1', '--locator', str(artifact)])
        code = checkpoint_module.main()
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert code == 1, payload
        assert payload.get('ok') is False, payload
        assert 'Traceback' not in captured.err, captured.err
        assert 'changed on disk while it was being written' in json.dumps(payload), payload
        assert 'name' in seen, 'the write never reached mkstemp'
        assert list(moved.glob('.state.json.*.tmp')) == [], \
            'the temporary file was left in the directory the walk verified'
        assert seen['decoy'].read_bytes() == b'decoy\n', \
            'a same-named file at the redirected location was deleted'
        assert (moved / 'state.json').read_bytes() == before

    def test_a_decoy_in_the_anchored_directory_is_never_deleted_as_the_temp(
            self, tmp_path, monkeypatch):
        """R8c MINOR: the temp lands elsewhere and a decoy takes its leaf.

        The parent is redirected BEFORE mkstemp, so the real temp file is created in
        the redirected directory, and a decoy with exactly the temp's leaf name waits
        in the directory the walk anchored. The inode comparison must refuse; the
        cleanup must then remove only the file this call created -- never the decoy,
        never by leaf through dir_fd -- and must close the mkstemp descriptor.
        """
        import checkpoint as checkpoint_module
        real = tmp_path / 'real'
        real.mkdir()
        state, _ = single_deliverable(real)
        path = real / 'state.json'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        before = path.read_bytes()
        moved = tmp_path / 'moved'
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        seen = {}
        real_mkstemp = checkpoint_module.tempfile.mkstemp

        def swap(*args, **kwargs):
            real.rename(moved)
            real.symlink_to(elsewhere, target_is_directory=True)
            fd, name = real_mkstemp(*args, **kwargs)      # lands in `elsewhere`
            decoy = moved / os.path.basename(name)
            decoy.write_bytes(b'DECOY\n')
            seen['fd'] = fd
            seen['decoy'] = decoy
            return fd, name

        monkeypatch.setattr(checkpoint_module.tempfile, 'mkstemp', swap)
        with pytest.raises(checkpoint_module.RecordError) as refused:
            checkpoint_module.write_state(path, state, before)
        assert 'verified directory' in str(refused.value), str(refused.value)
        assert (moved / 'state.json').read_bytes() == before
        assert 'fd' in seen, 'the write never reached mkstemp'
        try:
            os.fstat(seen['fd'])
            fd_open = True
        except OSError:
            fd_open = False
        decoy_gone = not seen['decoy'].exists()
        leftover = sorted(entry.name for entry in elsewhere.iterdir())
        assert (decoy_gone, fd_open, leftover) == (False, False, []), (
            'decoy_deleted=%r mkstemp_fd_still_open=%r leftover_elsewhere=%r'
            % (decoy_gone, fd_open, leftover))
        assert seen['decoy'].read_bytes() == b'DECOY\n'

    def test_the_temp_mode_is_applied_to_the_held_descriptor_not_to_a_name(
            self, tmp_path, monkeypatch):
        """fchmod on the held fd: a swapped temp name must not chmod an unrelated file."""
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        os.chmod(path, 0o600)
        before = path.read_bytes()
        victim = tmp_path / 'victim.md'
        victim.write_text('synthetic victim\n')
        os.chmod(victim, 0o644)
        real_fdopen = checkpoint_module.os.fdopen

        def swap(*args, **kwargs):
            for name in os.listdir(tmp_path):
                if name.startswith('.state.json.') and name.endswith('.tmp'):
                    pending = tmp_path / name
                    pending.unlink()
                    pending.symlink_to(victim)
            return real_fdopen(*args, **kwargs)

        def stop(*args, **kwargs):
            raise OSError('synthetic stop after the mode was applied')

        monkeypatch.setattr(checkpoint_module.os, 'fdopen', swap)
        monkeypatch.setattr(checkpoint_module.os, 'replace', stop)
        with pytest.raises(OSError):
            checkpoint_module.write_state(path, state, before)
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644, 'chmod followed the swapped name'
        assert path.read_bytes() == before

    def test_the_mode_and_owner_are_applied_to_the_held_descriptor_not_by_name(
            self, tmp_path, monkeypatch):
        """CK7: name-based chmod/chown must not be used at all; the held fd is.

        The earlier test only proved the ordering around fdopen, so a name-based
        chmod before fdopen survived. Here every name-based call raises, and the
        write must still succeed with the original mode.
        """
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        os.chmod(path, 0o640)
        before = path.read_bytes()
        named = []

        def refuse(*args, **kwargs):
            named.append((args, kwargs))
            raise AssertionError('name-based chmod/chown must not touch the temp file')

        monkeypatch.setattr(checkpoint_module.os, 'chmod', refuse)
        monkeypatch.setattr(checkpoint_module.os, 'chown', refuse)
        checkpoint_module.write_state(path, state, before)
        assert named == [], named
        assert stat.S_IMODE(path.stat().st_mode) == 0o640, oct(path.stat().st_mode)


class TestV2SupportingType:
    """`supporting` is exactly boolean on v2 artifacts, before the kind branch."""

    def test_a_non_boolean_supporting_on_an_external_artifact_is_refused(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['artifacts'].append({'id': 'A2', 'kind': 'external',
                                   'locator': 'https://example.invalid/x', 'revision': 'v1',
                                   'supporting': 'no'})
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('supporting must be a boolean' in error for error in out['errors']), out

    def test_a_non_boolean_supporting_on_a_local_artifact_is_refused(self, tmp_path):
        state, _ = single_deliverable(tmp_path)
        state['artifacts'][0]['supporting'] = 'yes'
        code, out = gate(tmp_path, state)
        assert code == 1, out
        assert any('supporting must be a boolean' in error for error in out['errors']), out

    @pytest.mark.parametrize('value', [True, False])
    def test_boolean_supporting_on_a_bound_artifact_is_accepted(self, tmp_path, value):
        """Clean control: the type check does not reject either boolean."""
        state, _ = single_deliverable(tmp_path)
        state['artifacts'][0]['supporting'] = value
        code, out = gate(tmp_path, state)
        assert code == 0, out

    def test_supporting_true_still_exempts_an_unbound_artifact(self, tmp_path):
        """Clean control: coverage semantics are unchanged for a real boolean."""
        state, _ = single_deliverable(tmp_path)
        state['artifacts'].append({'id': 'A2', 'kind': 'external',
                                   'locator': 'https://example.invalid/x', 'revision': 'v1',
                                   'supporting': True})
        code, out = gate(tmp_path, state)
        assert code == 0, out


class TestStrictJsonInput:
    """Strict JSON: duplicate keys and non-finite numbers are refused, bytes untouched."""

    def document(self, tmp_path, snippet):
        state, _ = single_deliverable(tmp_path)
        text = json.dumps(state, ensure_ascii=False, indent=2)
        needle = '"goal": ' + json.dumps(state['goal'], ensure_ascii=False)
        assert text.count(needle) == 1, text.count(needle)
        text = text.replace(needle, needle + ', ' + snippet, 1)
        path = tmp_path / 'state.json'
        path.write_text(text, encoding='utf-8')
        return path, path.read_bytes()

    def test_duplicate_object_keys_are_refused(self, tmp_path):
        """Field preservation: the second value must not silently replace the first."""
        path, before = self.document(tmp_path, '"goal": "a second goal"')
        for command in ('validate', 'gate'):
            code, out = call(command, path)
            assert code == 1, (command, out)
            assert any('duplicate' in error for error in out.get('errors', [])), (command, out)
            assert path.read_bytes() == before

    @pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity', '1e999'])
    def test_non_finite_numbers_are_refused(self, tmp_path, literal):
        """A value that cannot be written back as JSON must not be accepted as input."""
        path, before = self.document(tmp_path, '"measured": ' + literal)
        for command in ('validate', 'gate'):
            code, out = call(command, path)
            assert code == 1, (command, out)
            assert any('not valid JSON' in error for error in out.get('errors', [])), (command, out)
            assert path.read_bytes() == before

    def test_write_state_refuses_a_non_finite_number(self, tmp_path):
        """Every persistence path serialises with allow_nan=False."""
        import checkpoint as checkpoint_module
        state, _ = single_deliverable(tmp_path)
        path = write(tmp_path, state)
        before = path.read_bytes()
        with pytest.raises(ValueError):
            checkpoint_module.write_state(path, {'note': float('inf')}, before)
        assert path.read_bytes() == before, 'a refused write must not touch the file'

    def test_a_normal_record_still_passes(self, tmp_path):
        """Clean control: strictness does not reject an ordinary document."""
        state, _ = single_deliverable(tmp_path)
        code, out = gate(tmp_path, state)
        assert code == 0, out
        assert call('validate', write(tmp_path, state))[0] == 0

    def test_a_non_zero_literal_that_underflows_is_refused(self, tmp_path):
        """N1: 1e-400 becomes 0.0 in binary64, which is a value change, not a notation."""
        path, before = self.document(tmp_path, '"measured": 1e-400')
        for command in ('validate', 'gate', 'migrate'):
            code, out = call(command, path)
            assert code == 1, (command, out)
            assert any('underflow' in error for error in out.get('errors', [])), (command, out)
            assert path.read_bytes() == before

    @pytest.mark.parametrize('literal, stored', [('1e-300', 1e-300), ('1e5', 100000.0),
                                                 ('0.0', 0.0), ('-0.0', -0.0)])
    def test_representable_numbers_are_stored_as_binary64(self, tmp_path, literal, stored):
        """Clean control: in-range numbers pass and are stored as their binary64 value."""
        state, _ = single_deliverable(tmp_path)
        text = json.dumps(state, ensure_ascii=False, indent=2)
        needle = '"goal": ' + json.dumps(state['goal'], ensure_ascii=False)
        path = tmp_path / 'state.json'
        path.write_text(text.replace(needle, needle + ', "measured": ' + literal, 1),
                        encoding='utf-8')
        code, out = call('artifact', path, '--id', 'A1', '--locator', 'report.md')
        assert code == 0, (literal, out)
        stored_value = json.loads(path.read_text())['measured']
        assert stored_value == stored, (literal, stored_value)

    def test_a_lone_surrogate_is_refused_before_any_command(self, tmp_path):
        """N2: a lone surrogate cannot be stored as UTF-8; refuse it at the boundary."""
        state, _ = single_deliverable(tmp_path)
        state['goal'] = 'synthetic \ud800 goal'
        text = json.dumps(state)          # escapes the surrogate as \ud800
        path = tmp_path / 'state.json'
        path.write_text(text, encoding='ascii')
        before = path.read_bytes()
        for command in ('validate', 'gate', 'migrate'):
            code, out = call(command, path)
            assert code == 1, (command, out)
            assert any('surrogate' in error for error in out.get('errors', [])), (command, out)
            assert path.read_bytes() == before

    def test_normal_unicode_is_accepted(self, tmp_path):
        """Clean control: real non-ASCII text and a surrogate *pair* are not surrogates."""
        state, _ = single_deliverable(tmp_path)
        state['goal'] = '日本語 — naïve café 😀'
        code, out = gate(tmp_path, state)
        assert code == 0, out
