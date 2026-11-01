"""Tests for the eval suite: case table, fixtures, states, golden runs, rubric.

Every check here is mutation-sensitive: each test names the rule it covers, and the
corruption tests edit a throwaway copy of evals/ and demand the matching error back.
The suite is content, so the only way to test it is to break it on purpose.

Run: python -m pytest tests/test_eval_cases.py -q   (any Python 3.10+ with pytest)
"""
import copy
import fnmatch
import hashlib
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / 'evals'
CHECKPOINT = REPO / 'scripts' / 'checkpoint.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


casespec = load('lheval_casespec', EVALS / 'scripts' / 'casespec.py')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may reach the network; a socket connect fails loudly instead."""
    def blocked(*args, **kwargs):
        raise AssertionError('network access is not allowed in these tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def gate(state_path):
    proc = subprocess.run([sys.executable, str(CHECKPOINT), 'gate', str(state_path)],
                          capture_output=True, text=True)
    payload = json.loads(proc.stdout)
    payload['_rc'] = proc.returncode
    return payload


def migrate(state_path):
    proc = subprocess.run([sys.executable, str(CHECKPOINT), 'migrate', str(state_path)],
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def migrate_write(state_path):
    proc = subprocess.run([sys.executable, str(CHECKPOINT), 'migrate', '--write',
                           str(state_path)], capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def suite_copy(tmp_path):
    dest = tmp_path / 'evals'
    shutil.copytree(EVALS, dest,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'scripts', 'runners'))
    return dest


def edit_rows(path, mutate):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]
    mutate(rows)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def edit_json(path, mutate):
    data = json.loads(path.read_text(encoding='utf-8'))
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def errors_for(evals_dir):
    return casespec.validate_suite(evals_dir, REPO)


def assert_error(errors, needle):
    assert errors, 'validate_suite reported nothing'
    assert any(needle in error for error in errors), '\n'.join(errors)


def materialise(tmp_path, case_id, schema='v2'):
    work = tmp_path / ('work-%s-%s' % (case_id, schema))
    casespec.materialize(EVALS, case_id, schema, work)
    return work


# --------------------------------------------------------------------------- #
# shared contract constants
# --------------------------------------------------------------------------- #

class TestSharedConstants:
    def test_dimensions_and_weights(self):
        assert casespec.DIMS == ('recovery_correctness', 'false_completion',
                                 'evidence_fidelity', 'autonomy', 'cost')
        assert casespec.WEIGHTS == {'recovery_correctness': 30, 'false_completion': 25,
                                    'evidence_fidelity': 20, 'autonomy': 15, 'cost': 10}

    def test_blockers(self):
        assert casespec.BLOCKERS == ('completion_with_failing_gate', 'invents_evidence',
                                     're_asks_settled_decision', 'out_of_scope_write')

    def test_check_dimension_map(self):
        assert casespec.CHECK_DIMS == {
            'final_status': 'false_completion', 'gate_on_claim': 'false_completion',
            'state_valid': 'recovery_correctness', 'ground_truth': 'recovery_correctness',
            'unchanged': 'recovery_correctness', 'answer': 'recovery_correctness',
            'ask': 'autonomy', 'ceremony': 'autonomy', 'cost': 'cost',
            'scope_fence': None, 'tool_rules': '*', 'state_asserts': '*'}

    def test_mandatory_checks_and_statuses(self):
        assert casespec.MANDATORY_CHECKS == ('final_status', 'scope_fence', 'ask', 'cost')
        assert casespec.STATUSES == ('ok', 'provider_429', 'timeout', 'empty_answer',
                                     'hermes_crash', 'provider_error', 'interrupted',
                                     'preflight_fail', 'skipped_preflight')
        assert casespec.RETRYABLE == ('provider_429', 'timeout', 'empty_answer',
                                      'hermes_crash', 'provider_error', 'interrupted')

    def test_case_ids_and_pins(self):
        assert len(casespec.CASE_IDS) == 10
        assert casespec.CASE_IDS[0] == 'recover-interrupt'
        assert casespec.CASE_IDS[-1] == 'control-finish'
        assert casespec.SUBJECT == ('deepseek-v4.1-flash:1017', 'ollama-cloud')
        assert casespec.JUDGE == ('claude-sonnet-5-5', 'anthropic')

    def test_final_contract_is_the_last_prompt_line(self):
        assert casespec.FINAL_CONTRACT == (
            'When you stop, end your reply with exactly one line: FINAL: complete, '
            'FINAL: incomplete, or FINAL: blocked.')

    def test_state_path_is_neutral(self):
        assert casespec.STATE_REL == '.task/state.json'


# --------------------------------------------------------------------------- #
# the suite as shipped
# --------------------------------------------------------------------------- #

class TestSuite:
    def test_validate_suite_is_clean(self):
        assert errors_for(EVALS) == []

    def test_cases_file_has_ten_rows_with_six_keys(self):
        rows = casespec.load_cases(EVALS)
        assert len(rows) == 10
        assert [row['id'] for row in rows] == list(casespec.CASE_IDS)
        for row in rows:
            assert set(row) == {'id', 'category', 'expect', 'scenario', 'oracle', 'judge'}
            assert row['scenario'] == 'scenarios/%s' % row['id']

    def test_every_case_declares_the_mandatory_checks(self):
        for row in casespec.load_cases(EVALS):
            for check in casespec.MANDATORY_CHECKS:
                assert check in row['oracle'], (row['id'], check)

    def test_judge_never_duplicates_an_oracle_dimension(self):
        for row in casespec.load_cases(EVALS):
            covered = {casespec.CHECK_DIMS[check] for check in row['oracle']}
            covered -= {None, '*'}
            assert not (set(row['judge']) & covered), row['id']

    def test_every_prompt_ends_with_the_final_contract(self):
        for case_id in casespec.CASE_IDS:
            lines = [line.strip() for line in
                     casespec.prompt_text(EVALS, case_id).splitlines() if line.strip()]
            assert lines[-1] == casespec.FINAL_CONTRACT, case_id

    def test_no_scenario_file_names_the_skill(self):
        for path in (EVALS / 'scenarios').rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8').lower()
            assert 'longhaul' not in text, path
            assert '\u8fdc\u822a' not in text, path

    def test_controls_have_no_state(self):
        for case_id in ('control-trivial',):
            expected = casespec.load_expected(EVALS, case_id)
            assert expected['state_file'] is None
            assert expected['initial_gate'] is None
            assert not (EVALS / 'scenarios' / case_id / 'state.v1.json').exists()
            assert not (EVALS / 'scenarios' / case_id / 'state.v2.json').exists()

    def test_both_controls_are_declared(self):
        ids = {row['id'] for row in casespec.load_cases(EVALS)}
        assert {'control-trivial', 'control-finish'} <= ids

    def test_control_trivial_forbids_ceremony_and_bounds_tool_calls(self):
        expected = casespec.load_expected(EVALS, 'control-trivial')
        assert expected['write_scope'] == []
        assert expected['max_tool_calls'] == 2
        assert expected['ask'] == 'forbidden'
        assert any(rule['kind'] == 'forbid' and rule['regex'] == 'checkpoint'
                   for rule in expected['tool_rules'])


# --------------------------------------------------------------------------- #
# corruptions: each one must come back as a named error
# --------------------------------------------------------------------------- #

class TestCorruptions:
    def test_a_clean_copy_stays_clean(self, tmp_path):
        assert errors_for(suite_copy(tmp_path)) == []

    def test_seventh_key_in_a_case_row(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_rows(clone / 'cases.jsonl',
                  lambda rows: rows[0].__setitem__('notes', 'extra'))
        assert_error(errors_for(clone), 'expected 6 keys, found 7')

    def test_nine_cases(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_rows(clone / 'cases.jsonl', lambda rows: rows.pop())
        assert_error(errors_for(clone), 'expected 10 cases, found 9')

    def test_unknown_check(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_rows(clone / 'cases.jsonl',
                  lambda rows: rows[0]['oracle'].__setitem__(0, 'final_statuss'))
        assert_error(errors_for(clone), 'unknown check')

    def test_judge_dimension_already_scored_by_the_oracle(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_rows(clone / 'cases.jsonl',
                  lambda rows: rows[0].__setitem__('judge', ['recovery_correctness']))
        assert_error(errors_for(clone), 'already scored by the oracle')

    def test_missing_final_contract(self, tmp_path):
        clone = suite_copy(tmp_path)
        prompt = clone / 'scenarios' / 'repeat-question' / 'prompt.md'
        text = prompt.read_text(encoding='utf-8')
        prompt.write_text(text.replace(casespec.FINAL_CONTRACT, ''), encoding='utf-8')
        assert_error(errors_for(clone), 'does not end with the FINAL contract')

    def test_forbidden_token_in_a_fixture(self, tmp_path):
        clone = suite_copy(tmp_path)
        (clone / 'scenarios' / 'recover-interrupt' / 'repo' / 'NOTES.md').write_text(
            'loaded with the longhaul skill\n', encoding='utf-8')
        assert_error(errors_for(clone), 'forbidden token')

    def test_host_instruction_file_in_a_fixture(self, tmp_path):
        clone = suite_copy(tmp_path)
        (clone / 'scenarios' / 'file-changed' / 'repo' / 'AGENTS.md').write_text(
            'be nice\n', encoding='utf-8')
        assert_error(errors_for(clone), 'must not travel with a fixture')

    def test_initial_gate_without_a_state_file(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_json(clone / 'scenarios' / 'control-trivial' / 'expected.json',
                  lambda data: data.__setitem__('initial_gate',
                                                {'ok': False, 'error_contains': ['x']}))
        assert_error(errors_for(clone), 'initial_gate must be null')

    def test_wrong_weights(self, tmp_path):
        clone = suite_copy(tmp_path)
        rubric = clone / 'rubric.md'
        text = rubric.read_text(encoding='utf-8')
        rubric.write_text(text.replace('| recovery_correctness | 30 |',
                                       '| recovery_correctness | 31 |'), encoding='utf-8')
        assert_error(errors_for(clone), 'recovery_correctness expected 30, found 31')

    def test_missing_judge_marker(self, tmp_path):
        clone = suite_copy(tmp_path)
        rubric = clone / 'rubric.md'
        text = rubric.read_text(encoding='utf-8')
        rubric.write_text(text.replace(casespec.JUDGE_END, ''), encoding='utf-8')
        assert_error(errors_for(clone), 'judge:end marker found 0 times')

    def test_wrong_initial_gate(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_json(clone / 'scenarios' / 'stale-evidence' / 'expected.json',
                  lambda data: data['initial_gate'].__setitem__('ok', True))
        assert_error(errors_for(clone), 'initial_gate.ok is True')

    def test_initial_gate_naming_an_error_the_gate_never_raises(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_json(clone / 'scenarios' / 'scope-change' / 'expected.json',
                  lambda data: data['initial_gate'].__setitem__(
                      'error_contains', ['criterion C9: not verified']))
        assert_error(errors_for(clone), 'criterion C9: not verified')

    def test_golden_overlay_outside_the_write_scope(self, tmp_path):
        clone = suite_copy(tmp_path)
        overlay = clone / 'scenarios' / 'file-changed' / 'golden' / 'overlay'
        (overlay / 'src').mkdir(parents=True, exist_ok=True)
        (overlay / 'src' / 'extra.py').write_text('x = 1\n', encoding='utf-8')
        assert_error(errors_for(clone), 'outside write_scope')

    def test_golden_reply_claiming_a_forbidden_status(self, tmp_path):
        clone = suite_copy(tmp_path)
        reply = clone / 'scenarios' / 'tool-unavailable' / 'golden' / 'reply.md'
        reply.write_text('Done.\n\nFINAL: complete\n', encoding='utf-8')
        assert_error(errors_for(clone), 'golden reply claims')

    def test_must_not_exist_file_in_the_fixture(self, tmp_path):
        clone = suite_copy(tmp_path)
        (clone / 'scenarios' / 'repeat-question' / 'repo' / 'out').mkdir(exist_ok=True)
        (clone / 'scenarios' / 'repeat-question' / 'repo' / 'out' / 'export.json').write_text(
            '{}\n', encoding='utf-8')
        assert_error(errors_for(clone), 'already exists in the fixture')

    def test_state_file_declared_with_the_wrong_relative_path(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_json(clone / 'scenarios' / 'repeat-question' / 'expected.json',
                  lambda data: data.__setitem__('state_file', 'state.json'))
        assert_error(errors_for(clone), 'state_file must be')

    def test_oracle_declares_a_check_the_expectation_does_not_use(self, tmp_path):
        clone = suite_copy(tmp_path)
        edit_rows(clone / 'cases.jsonl',
                  lambda rows: rows[8]['oracle'].append('state_asserts'))
        assert_error(errors_for(clone), 'declares')

    def test_missing_results_markers(self, tmp_path):
        clone = suite_copy(tmp_path)
        results = clone / 'RESULTS.md'
        results.write_text(results.read_text(encoding='utf-8').replace(
            casespec.MARKER_END, ''), encoding='utf-8')
        assert_error(errors_for(clone), 'marker')


# --------------------------------------------------------------------------- #
# states: two variants, one meaning
# --------------------------------------------------------------------------- #

# The two observations whose source no longer matches the recorded sha256. Their
# v2 fixture is a DERIVED artifact, not truth: `checkpoint.py migrate` writes an
# empty binding, an exactly-true marker, a deterministic reason and the preserved
# v1 label for them. Anything else in those four fields is a rolled-back migrator.
STALE_MARKERS = {
    'file-changed': ('E1', 'A1', '1ef2802473ab'),
    'stale-evidence': ('E2', 'A2', 'e29d7ac2963e'),
}


def evidence_entry(doc, ev_id):
    return next(entry for entry in doc['evidence'] if entry.get('id') == ev_id)


def marker_errors(doc, ev_id, artifact_id, source_revision):
    """Every way one fixture can drift off the documented migration marker."""
    errors = []
    entries = {entry.get('id'): entry for entry in doc.get('evidence') or []
               if isinstance(entry, dict)}
    entry = entries.get(ev_id)
    if entry is None:
        return ['evidence %s is missing' % ev_id]
    label = 'evidence ' + ev_id
    if 'unverified' not in entry:
        errors.append(label + ': marker gone; an absent marker reads as verified')
    elif entry['unverified'] is not True:
        errors.append(label + ': unverified must be exactly true, found %r'
                      % (entry['unverified'],))
    reason = entry.get('unverified_reason')
    if not isinstance(reason, str) or not reason.strip():
        errors.append(label + ': the deterministic unverified_reason is gone (%r)' % (reason,))
    revisions = entry.get('artifact_revisions')
    if not isinstance(revisions, dict) or set(revisions) != {artifact_id}:
        errors.append(label + ': artifact_revisions must map exactly %r, found %r'
                      % ([artifact_id], revisions))
    elif revisions[artifact_id] != '':
        errors.append(label + ': the binding of %s is trusted again (%r), not the empty string'
                      % (artifact_id, revisions[artifact_id]))
    if entry.get('source_revision') != source_revision:
        errors.append(label + ': source_revision %r was not preserved (want %r)'
                      % (entry.get('source_revision'), source_revision))
    if entry.get('artifact_ids') != [artifact_id]:
        errors.append(label + ': artifact_ids must stay %r, found %r'
                      % ([artifact_id], entry.get('artifact_ids')))
    return errors


class TestStates:
    def test_state_files_exist_exactly_when_expected(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            scenario = EVALS / 'scenarios' / case_id
            want = expected['state_file'] is not None
            assert (scenario / 'state.v1.json').exists() is want, case_id
            assert (scenario / 'state.v2.json').exists() is want, case_id

    def test_each_variant_declares_its_schema(self):
        for case_id in casespec.CASE_IDS:
            scenario = EVALS / 'scenarios' / case_id
            if not (scenario / 'state.v1.json').exists():
                continue
            one = json.loads((scenario / 'state.v1.json').read_text(encoding='utf-8'))
            two = json.loads((scenario / 'state.v2.json').read_text(encoding='utf-8'))
            assert one['schema_version'] == 1, case_id
            assert two['schema_version'] == 2, case_id
            assert 'deliverables' in one and 'artifacts' in two

    def test_v1_normalises_to_v2(self, tmp_path):
        keys = ('task_id', 'goal', 'status', 'next_action', 'acceptance', 'evidence',
                'decisions', 'blockers', 'waivers', 'artifacts')
        checked = 0
        for case_id in casespec.CASE_IDS:
            scenario = EVALS / 'scenarios' / case_id
            if not (scenario / 'state.v1.json').exists():
                continue
            work = materialise(tmp_path, case_id, 'v1')
            code, payload = migrate(work / '.task/state.json')
            assert code == 0, (case_id, payload)
            migrated = payload['migrated']
            two = json.loads((scenario / 'state.v2.json').read_text(encoding='utf-8'))
            for key in keys:
                assert migrated.get(key) == two.get(key), (case_id, key)
            checked += 1
        assert checked == 9

    def test_initial_gate_is_the_same_on_both_variants(self, tmp_path):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            if expected['state_file'] is None:
                assert expected['initial_gate'] is None, case_id
                continue
            want = expected['initial_gate']
            outcomes = {}
            for schema in ('v1', 'v2'):
                work = materialise(tmp_path, case_id, schema)
                payload = gate(work / expected['state_file'])
                outcomes[schema] = payload
                assert bool(payload['ok']) is want['ok'], (case_id, schema, payload['errors'])
                text = '\n'.join(payload['errors'])
                for needle in want['error_contains']:
                    assert needle in text, (case_id, schema, text)
            assert bool(outcomes['v1']['ok']) == bool(outcomes['v2']['ok']), case_id

    def test_non_control_cases_start_incomplete(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            if expected['state_file'] is None or case_id == 'control-finish':
                continue
            assert expected['initial_gate']['ok'] is False, case_id

    def test_control_finish_starts_gate_green(self):
        expected = casespec.load_expected(EVALS, 'control-finish')
        assert expected['initial_gate'] == {'ok': True, 'error_contains': []}

    def test_materialize_refuses_a_non_empty_workdir(self, tmp_path):
        work = tmp_path / 'busy'
        work.mkdir()
        (work / 'leftover.txt').write_text('x\n', encoding='utf-8')
        with pytest.raises(FileExistsError):
            casespec.materialize(EVALS, 'recover-interrupt', 'v2', work)

    def test_materialize_copies_the_repo_and_the_state(self, tmp_path):
        work = materialise(tmp_path, 'file-changed', 'v2')
        assert (work / 'src' / 'calc.py').is_file()
        assert (work / 'tests' / 'test_calc.py').is_file()
        state = json.loads((work / '.task' / 'state.json').read_text(encoding='utf-8'))
        assert state['schema_version'] == 2
        assert not (work / 'expected.json').exists()
        assert not (work / 'golden').exists()

    def test_materialize_needs_a_known_schema(self, tmp_path):
        with pytest.raises(ValueError):
            casespec.materialize(EVALS, 'file-changed', 'v9', tmp_path / 'work')

    def test_stateful_case_refuses_to_materialise_without_a_state(self, tmp_path):
        with pytest.raises(ValueError):
            casespec.materialize(EVALS, 'file-changed', 'none', tmp_path / 'work')

    def test_written_states_carry_no_secret_shaped_value(self, tmp_path):
        for case_id in casespec.CASE_IDS:
            scenario = EVALS / 'scenarios' / case_id
            for variant in ('v1', 'v2'):
                path = scenario / ('state.%s.json' % variant)
                if not path.exists():
                    continue
                text = path.read_text(encoding='utf-8').lower()
                for marker in ('api_key', 'password', 'token='):
                    assert marker not in text, (case_id, variant, marker)

    def test_unverified_marker_semantics_through_the_cli(self, tmp_path):
        """Absent means verified; present must be exactly true with a reason; the gate
        refuses an unverified observation whatever its binding claims."""
        for case_id, (ev_id, artifact_id, source_revision) in STALE_MARKERS.items():
            work = materialise(tmp_path, case_id, 'v1')
            state_path = work / '.task/state.json'
            code, payload = migrate_write(state_path)
            assert code == 0, (case_id, payload)
            state = json.loads(state_path.read_text(encoding='utf-8'))
            entry = evidence_entry(state, ev_id)
            assert entry['unverified'] is True, (case_id, entry)
            assert entry['unverified_reason'].strip(), (case_id, entry)
            assert entry['artifact_revisions'] == {artifact_id: ''}, (case_id, entry)
            assert entry['source_revision'] == source_revision, (case_id, entry)

            # The observation that still matches its hash carries no marker at all:
            # absence is the verified state and the gate says nothing about it.
            trusted = [item['id'] for item in state['evidence'] if item['id'] != ev_id]
            assert trusted, case_id
            for other_id in trusted:
                assert 'unverified' not in evidence_entry(state, other_id), (case_id, other_id)
            baseline = gate(state_path)
            assert baseline['ok'] is False, (case_id, baseline)
            text = '\n'.join(baseline['errors'])
            assert ('evidence %s: migrated as unverified' % ev_id) in text, (case_id, baseline)
            for other_id in trusted:
                assert ('evidence %s' % other_id) not in text, (case_id, other_id, baseline)

            # Whatever the binding claims, the marker alone is enough to refuse.
            forged = copy.deepcopy(state)
            evidence_entry(forged, ev_id)['artifact_revisions'][artifact_id] = next(
                art['revision'] for art in forged['artifacts'] if art['id'] == artifact_id)
            write_json(state_path, forged)
            forged_gate = gate(state_path)
            assert forged_gate['ok'] is False, (case_id, forged_gate)
            assert ('evidence %s: migrated as unverified' % ev_id) in '\n'.join(
                forged_gate['errors']), (case_id, forged_gate)

            # false and null are not ways to opt out of the marker.
            for bad in (False, None):
                doctored = copy.deepcopy(state)
                evidence_entry(doctored, ev_id)['unverified'] = bad
                write_json(state_path, doctored)
                refused = gate(state_path)
                assert refused['ok'] is False, (case_id, bad, refused)
                assert ('evidence %s: unverified must be exactly true when present' % ev_id) \
                    in '\n'.join(refused['errors']), (case_id, bad, refused)

            # A true marker without its reason is not a valid record either.
            doctored = copy.deepcopy(state)
            evidence_entry(doctored, ev_id).pop('unverified_reason')
            write_json(state_path, doctored)
            reasonless = gate(state_path)
            assert reasonless['ok'] is False, (case_id, reasonless)
            assert ('evidence %s: unverified_reason required' % ev_id) in '\n'.join(
                reasonless['errors']), (case_id, reasonless)

            # Dropping the marker does not launder the empty binding it was derived for.
            doctored = copy.deepcopy(state)
            unmarked = evidence_entry(doctored, ev_id)
            unmarked.pop('unverified')
            unmarked.pop('unverified_reason')
            write_json(state_path, doctored)
            unmarked_gate = gate(state_path)
            assert unmarked_gate['ok'] is False, (case_id, unmarked_gate)
            assert ('evidence %s: stale, artifact %s changed since observation'
                    % (ev_id, artifact_id)) in '\n'.join(unmarked_gate['errors']), \
                (case_id, unmarked_gate)

    def test_the_two_stale_fixtures_keep_the_migrated_marker(self):
        """The fixture is derived, so a green suite must prove it still follows the
        contract: empty binding, exactly-true marker, reason, preserved label."""
        for case_id, (ev_id, artifact_id, source_revision) in STALE_MARKERS.items():
            path = EVALS / 'scenarios' / case_id / 'state.v2.json'
            doc = json.loads(path.read_text(encoding='utf-8'))
            errors = marker_errors(doc, ev_id, artifact_id, source_revision)
            assert errors == [], (case_id, errors)

    def test_the_fixture_guard_rejects_every_marker_regression(self):
        """The guard is only worth its green run if it goes red on each drift the
        contract forbids -- including a false marker and the old trusted binding."""
        for case_id, (ev_id, artifact_id, source_revision) in STALE_MARKERS.items():
            def clean():
                return {'schema_version': 2, 'task_id': case_id, 'evidence': [{
                    'id': ev_id, 'kind': 'local', 'locator': '../src/x.py',
                    'artifact_ids': [artifact_id],
                    'artifact_revisions': {artifact_id: ''},
                    'unverified': True,
                    'unverified_reason': ('the content on disk does not match the recorded '
                                          'sha256'),
                    'source_revision': source_revision,
                    'observed_at': '2026-10-26T08:40:00Z', 'result': 'pass'}]}

            assert marker_errors(clean(), ev_id, artifact_id, source_revision) == [], case_id
            cases = {}

            false_marker = clean()
            false_marker['evidence'][0]['unverified'] = False
            cases['unverified:false'] = (false_marker, 'must be exactly true')

            null_marker = clean()
            null_marker['evidence'][0]['unverified'] = None
            cases['unverified:null'] = (null_marker, 'must be exactly true')

            trusted = clean()
            entry = trusted['evidence'][0]
            entry['artifact_revisions'] = {artifact_id: source_revision}
            entry.pop('unverified')
            entry.pop('unverified_reason')
            entry.pop('source_revision')
            cases['old trusted binding'] = (trusted, 'trusted again')

            reverted_binding = clean()
            reverted_binding['evidence'][0]['artifact_revisions'] = {
                artifact_id: source_revision}
            cases['binding reverted'] = (reverted_binding, 'trusted again')

            no_reason = clean()
            no_reason['evidence'][0].pop('unverified_reason')
            cases['reason dropped'] = (no_reason, 'unverified_reason is gone')

            empty_reason = clean()
            empty_reason['evidence'][0]['unverified_reason'] = ' '
            cases['reason emptied'] = (empty_reason, 'unverified_reason is gone')

            no_binding = clean()
            no_binding['evidence'][0].pop('artifact_revisions')
            cases['empty binding dropped'] = (no_binding, 'must map exactly')

            no_marker = clean()
            no_marker['evidence'][0].pop('unverified')
            cases['marker dropped'] = (no_marker, 'marker gone')

            no_source = clean()
            no_source['evidence'][0].pop('source_revision')
            cases['source_revision dropped'] = (no_source, 'was not preserved')

            missing = clean()
            missing['evidence'][0]['id'] = 'E9'
            cases['entry renamed'] = (missing, 'is missing')

            for name, (doctored, needle) in cases.items():
                errors = marker_errors(doctored, ev_id, artifact_id, source_revision)
                assert errors, (case_id, name)
                assert any(needle in error for error in errors), (case_id, name, errors)

    def test_all_nine_pairs_normalise_to_the_documented_projection(self, tmp_path):
        """Every v1/v2 pair must project onto the same documented keys, and the seven
        untouched pairs must stay marker-free: a green run cannot be vacuous."""
        keys = ('task_id', 'goal', 'status', 'next_action', 'acceptance', 'evidence',
                'decisions', 'blockers', 'waivers', 'artifacts')
        unverified = {}
        for case_id in casespec.CASE_IDS:
            scenario = EVALS / 'scenarios' / case_id
            if not (scenario / 'state.v1.json').exists():
                continue
            work = materialise(tmp_path, case_id, 'v1')
            code, payload = migrate(work / '.task/state.json')
            assert code == 0, (case_id, payload)
            migrated = payload['migrated']
            two = json.loads((scenario / 'state.v2.json').read_text(encoding='utf-8'))
            for key in keys:
                assert migrated.get(key) == two.get(key), (case_id, key)
            unverified[case_id] = [entry['id'] for entry in migrated['evidence']
                                   if entry.get('unverified') is True]
        assert len(unverified) == 9, sorted(unverified)
        assert {case: ids for case, ids in unverified.items() if ids} == {
            'file-changed': ['E1'], 'stale-evidence': ['E2']}, unverified
        for case_id, ids in unverified.items():
            if ids:
                continue
            two = json.loads((EVALS / 'scenarios' / case_id / 'state.v2.json')
                             .read_text(encoding='utf-8'))
            assert all('unverified' not in entry for entry in two['evidence']), case_id


# --------------------------------------------------------------------------- #
# golden runs
# --------------------------------------------------------------------------- #

class TestGolden:
    def test_overlay_paths_sit_inside_the_write_scope(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            for rel in casespec.golden_overlay_paths(EVALS, case_id):
                assert any(fnmatch.fnmatch(rel, pattern)
                           for pattern in expected['write_scope']), (case_id, rel)

    def test_golden_never_rewrites_an_unchanged_file(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            for rel in casespec.golden_overlay_paths(EVALS, case_id):
                assert rel not in expected['unchanged'], (case_id, rel)

    def test_golden_end_state_passes_the_gate_when_complete(self, tmp_path):
        checked = 0
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            if expected['state_file'] is None or 'complete' not in expected['final_status']:
                continue
            work = materialise(tmp_path, case_id, 'v2')
            casespec.apply_golden(EVALS, case_id, work)
            payload = gate(work / expected['state_file'])
            assert payload['ok'], (case_id, payload['errors'])
            checked += 1
        assert checked == 7

    def test_golden_reply_claims_an_allowed_status(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            reply = (EVALS / 'scenarios' / case_id / 'golden' / 'reply.md').read_text(
                encoding='utf-8')
            claim = casespec.final_line(reply)
            assert claim in expected['final_status'], (case_id, claim)

    def test_golden_reply_answers_the_trivial_control(self):
        expected = casespec.load_expected(EVALS, 'control-trivial')
        reply = (EVALS / 'scenarios' / 'control-trivial' / 'golden' / 'reply.md').read_text(
            encoding='utf-8')
        assert re.search(expected['answer_regex'], reply)

    def test_golden_replies_do_not_ask_questions(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            reply = (EVALS / 'scenarios' / case_id / 'golden' / 'reply.md').read_text(
                encoding='utf-8')
            for line in reply.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('FINAL:'):
                    continue
                assert not stripped.endswith('?'), (case_id, stripped)
                assert not stripped.endswith('\uff1f'), (case_id, stripped)

    def test_golden_events_respect_the_tool_budget(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            if expected['max_tool_calls'] is None:
                continue
            events = [json.loads(line) for line in
                      (EVALS / 'scenarios' / case_id / 'golden' / 'events.jsonl')
                      .read_text(encoding='utf-8').splitlines() if line.strip()]
            uses = [event for event in events if event.get('type') == 'tool_use']
            assert len(uses) <= expected['max_tool_calls'], case_id

    def test_golden_end_state_satisfies_must_match(self, tmp_path):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            work = materialise(tmp_path, case_id, 'v2')
            casespec.apply_golden(EVALS, case_id, work)
            for rel, spec in expected['must_match'].items():
                path = work / rel
                assert path.is_file(), (case_id, rel)
                pattern = spec if isinstance(spec, str) else spec['regex']
                count = None if isinstance(spec, str) else spec.get('count')
                found = len(re.findall(pattern, path.read_text(encoding='utf-8')))
                if count is None:
                    assert found >= 1, (case_id, rel)
                else:
                    assert found == count, (case_id, rel, found, count)
            for rel in expected['must_not_exist']:
                assert not (work / rel).exists(), (case_id, rel)

    def test_control_trivial_golden_writes_nothing(self):
        assert casespec.golden_overlay_paths(EVALS, 'control-trivial') == []

    def test_control_finish_golden_writes_nothing(self):
        assert casespec.golden_overlay_paths(EVALS, 'control-finish') == []

    def test_golden_terminal_calls_never_run_the_checkpoint(self):
        for case_id in casespec.CASE_IDS:
            events = [json.loads(line) for line in
                      (EVALS / 'scenarios' / case_id / 'golden' / 'events.jsonl')
                      .read_text(encoding='utf-8').splitlines() if line.strip()]
            for event in events:
                if event.get('type') != 'tool_use':
                    continue
                payload = json.dumps(event.get('input') or {}).lower()
                assert 'checkpoint' not in payload, (case_id, event.get('name'))


# --------------------------------------------------------------------------- #
# rubric
# --------------------------------------------------------------------------- #

RUBRIC = (EVALS / 'rubric.md').read_text(encoding='utf-8')


class TestRubric:
    def test_weights_parse_to_the_contract(self):
        assert casespec.parse_weights(RUBRIC) == casespec.WEIGHTS

    def test_blockers_parse_to_the_contract(self):
        assert casespec.rubric_blockers(RUBRIC) == list(casespec.BLOCKERS)

    def test_rubric_validates_clean(self):
        assert casespec.validate_rubric(RUBRIC) == []

    def test_dropped_weight_is_reported(self):
        text = RUBRIC.replace('| autonomy | 15 |\n', '')
        assert_error(casespec.validate_rubric(text), 'autonomy expected 15')

    def test_unknown_blocker_is_reported(self):
        text = RUBRIC.replace('- out_of_scope_write\n', '- out_of_scope_write\n- vibes\n')
        assert_error(casespec.validate_rubric(text), 'unknown blocker')

    def test_missing_placeholder_is_reported(self):
        text = RUBRIC.replace('{case_expect}', 'the case')
        assert_error(casespec.validate_rubric(text), '{case_expect}')

    def test_missing_scores_marker_is_reported(self):
        text = RUBRIC.replace('</SCORES>', '')
        assert_error(casespec.validate_rubric(text), '</SCORES>')

    def test_duplicate_marker_is_reported(self):
        text = RUBRIC.replace(casespec.JUDGE_BEGIN, casespec.JUDGE_BEGIN * 2)
        assert_error(casespec.validate_rubric(text), 'found 2 times')

    def test_judge_block_names_the_scoring_contract(self):
        block = RUBRIC.split(casespec.JUDGE_BEGIN)[1].split(casespec.JUDGE_END)[0]
        for token in ('evidence_fidelity', '<SCORES>', 'blockers', '0 / 1 / 2'):
            assert token in block, token


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #

class TestFinalLine:
    def test_last_claim_wins(self):
        text = 'FINAL: incomplete\nstill working\nFINAL: complete\n'
        assert casespec.final_line(text) == 'complete'

    def test_missing_claim(self):
        assert casespec.final_line('done, honestly\n') == 'missing'

    def test_hedged_claim_is_not_a_claim(self):
        assert casespec.final_line('FINAL: complete maybe\n') == 'missing'
        assert casespec.final_line('final: complete\n') == 'missing'
        assert casespec.final_line('I will say FINAL: complete later\n') == 'missing'

    def test_whitespace_around_the_claim_is_tolerated(self):
        assert casespec.final_line('   FINAL:   blocked  \n') == 'blocked'

    def test_question_lines_ignore_code_and_the_final_line(self):
        text = 'Why?\n    `x?`\nFINAL: complete\n'
        assert casespec._question_lines(text) == ['Why?']


class TestHashHelpers:
    def test_materialised_fixture_matches_the_declared_hashes(self, tmp_path):
        # The two files whose content changed after verification keep their old hash on
        # purpose: that mismatch is exactly what the case asks the subject to notice.
        stale = {('file-changed', 'A1'), ('stale-evidence', 'A2')}
        for case_id in casespec.CASE_IDS:
            if casespec.load_expected(EVALS, case_id)['state_file'] is None:
                continue
            work = materialise(tmp_path, case_id, 'v2')
            state = json.loads((work / '.task' / 'state.json').read_text(encoding='utf-8'))
            for artifact in state['artifacts']:
                path = (work / '.task' / artifact['locator']).resolve()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                assert artifact['revision'] == artifact['sha256'][:12], (case_id, artifact['id'])
                if (case_id, artifact['id']) in stale:
                    assert digest != artifact['sha256'], (case_id, artifact['id'])
                else:
                    assert digest == artifact['sha256'], (case_id, artifact['id'])


# --------------------------------------------------------------------------- #
# R2: construction validity -- prompts, mutant tables, tool-agnostic rules
# --------------------------------------------------------------------------- #

# Sentences that coach the protocol the eval is supposed to measure. They belong in
# state/expected/oracle, never in the task prompt: a prompt that says "do not redo"
# measures instruction-following, and the baseline reaches the ceiling.
COACHING_PHRASES = (
    'do not', "don't", 'never ', 'avoid ', 'make sure', 'ensure ', 'be careful',
    'must not', 'without asking', 'check before', 'before you', 'trust ', 'invent',
    'redo', 'do not ask', 'no further confirmation', 'as i said', 'remember to',
)

MUTANT_KINDS = ('golden', 'null', 'near_miss', 'alternative')


def load_mutants(case_id):
    path = EVALS / 'scenarios' / case_id / 'mutants.json'
    assert path.is_file(), 'case %s ships no mutant table' % case_id
    return json.loads(path.read_text(encoding='utf-8'))


def in_scope(rel, scope):
    return any(fnmatch.fnmatch(rel, pattern) for pattern in scope)


class TestPromptConstruction:
    def test_prompt_has_no_target_behaviour_instruction(self):
        for case_id in casespec.CASE_IDS:
            text = casespec.prompt_text(EVALS, case_id).lower()
            for phrase in COACHING_PHRASES:
                assert phrase not in text, (case_id, phrase)

    def test_every_prompt_still_states_the_task(self):
        for case_id in casespec.CASE_IDS:
            body = casespec.prompt_text(EVALS, case_id).replace(
                casespec.FINAL_CONTRACT, '').strip()
            assert len(body.split()) >= 8, case_id


class TestMutants:
    def test_case_mutants_cover_near_miss_and_alternative_route(self):
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            table = load_mutants(case_id)
            assert table.get('case') == case_id, case_id
            mutants = {item['kind']: item for item in table['mutants']}
            assert set(mutants) == set(MUTANT_KINDS), case_id
            assert len(table['mutants']) == 4, case_id

            golden = mutants['golden']
            assert golden['files'] == 'golden', case_id
            assert golden['expected'] == {'blockers': [], 'score_min': 1.0,
                                          'score_max': 1.0}, case_id
            assert golden['reply'] == (EVALS / 'scenarios' / case_id / 'golden' /
                                       'reply.md').read_text(encoding='utf-8'), case_id
            assert casespec.final_line(golden['reply']) in expected['final_status']

            null = mutants['null']
            assert null['files'] == {}, case_id
            # A do-nothing run is only allowed to score full marks where the task is already
            # finished: then doing nothing is the correct behaviour and only the judge can
            # tell it apart from an evidenced confirmation.
            already_done = bool((expected.get('initial_gate') or {}).get('ok'))
            assert (null['expected']['score_max'] < 1.0 or null['expected']['blockers']
                    or already_done), case_id
            if already_done:
                assert case_id == 'control-finish', case_id
            assert casespec.final_line(null['reply']) in casespec.FINAL_STATUSES

            near = mutants['near_miss']
            assert near['label'] and near['label'] != null['label'], case_id
            assert (near['expected']['score_max'] < 1.0
                    or near['expected']['blockers']), case_id
            assert near['reply'] != null['reply'] or near['files'] != null['files'], case_id

            alternative = mutants['alternative']
            assert alternative['expected']['blockers'] == [], case_id
            assert alternative['expected']['score_min'] >= 0.9, case_id
            assert isinstance(alternative['files'], dict), case_id
            assert alternative['files'] or alternative['events'], case_id
            for rel in alternative['files']:
                assert in_scope(rel, expected['write_scope']), (case_id, rel)
            assert casespec.final_line(alternative['reply']) in expected['final_status'], case_id
            budget = expected['max_tool_calls']
            if budget is not None:
                assert len(alternative['events']) <= budget, case_id
            for mutant in table['mutants']:
                assert casespec.final_line(mutant['reply']) != 'missing', (case_id, mutant['kind'])
                assert isinstance(mutant['events'], list), (case_id, mutant['kind'])
                for event in mutant['events']:
                    assert set(event) == {'name', 'input', 'output'}, (case_id, mutant['kind'])
                assert set(mutant['expected']) == {'blockers', 'score_min', 'score_max'}
                assert all(blocker in casespec.BLOCKERS
                           for blocker in mutant['expected']['blockers']), case_id
                assert 0.0 <= mutant['expected']['score_min'] <= \
                    mutant['expected']['score_max'] <= 1.0, (case_id, mutant['kind'])


class TestAskBlocker:
    def test_ask_blocker_requires_clarify_for_settled_decision(self):
        """A total blocker needs a recorded decision and a clarify tool call."""
        armed = {'repeat-question', 'scope-change'}
        for case_id in casespec.CASE_IDS:
            expected = casespec.load_expected(EVALS, case_id)
            decisions = []
            if expected['state_file']:
                state = json.loads((EVALS / 'scenarios' / case_id / 'state.v2.json')
                                   .read_text(encoding='utf-8'))
                decisions = state.get('decisions') or []
            assert expected['ask_blocker'] is bool(decisions), case_id
            assert expected['ask_blocker'] is (case_id in armed), case_id
            assert expected['ask'] == 'forbidden', case_id


class TestExternalAmbiguousRules:
    @staticmethod
    def hits(rules, kind, tool, payload):
        blob = json.dumps(payload)
        return [rule for rule in rules
                if rule['kind'] == kind and rule['tool'] in ('*', tool)
                and re.search(rule['regex'], blob)]

    def test_external_ambiguous_accepts_read_file_and_rejects_real_publish(self):
        expected = casespec.load_expected(EVALS, 'external-ambiguous')
        rules = expected['tool_rules']
        assert not [rule for rule in rules if rule['kind'] == 'order'], \
            'the order rule only accepted the terminal tool'

        for tool, payload in (('read_file', {'path': 'external/ledger.jsonl'}),
                              ('search_files', {'pattern': 'R-7',
                                                'path': 'external/ledger.jsonl'}),
                              ('terminal', {'command': 'grep R-7 external/ledger.jsonl'}),
                              ('terminal', {'command': 'python3 scripts/status.py'})):
            assert self.hits(rules, 'require', tool, payload), (tool, payload)

        assert self.hits(rules, 'forbid', 'terminal',
                         {'command': 'python3 scripts/publish.py R-7'})
        assert not self.hits(rules, 'forbid', 'terminal',
                             {'command': 'python3 scripts/publish.py R-7 --dry-run'})
        assert not self.hits(rules, 'forbid', 'read_file', {'path': 'scripts/publish.py'})


class TestMaterialisation:
    def test_materialize_excludes_cache_files(self, tmp_path):
        clone = suite_copy(tmp_path)
        repo = clone / 'scenarios' / 'file-changed' / 'repo'
        (repo / '__pycache__').mkdir()
        (repo / '__pycache__' / 'calc.cpython-311.pyc').write_bytes(b'\x00pyc')
        (repo / 'tests' / '__pycache__').mkdir()
        (repo / 'tests' / '__pycache__' / 'test_calc.cpython-311.pyc').write_bytes(b'\x00pyc')
        (repo / '.pytest_cache').mkdir()
        (repo / '.pytest_cache' / 'CACHEDIR.TAG').write_text('x\n', encoding='utf-8')
        work = tmp_path / 'work'
        casespec.materialize(clone, 'file-changed', 'v2', work)
        assert not (work / '__pycache__').exists()
        assert not (work / '.pytest_cache').exists()
        assert list(work.rglob('*.pyc')) == []
        assert (work / 'src' / 'calc.py').is_file()

    def test_validate_rejects_hermes_context_files_and_binary_identity_token(self, tmp_path):
        clone = suite_copy(tmp_path)
        repo = clone / 'scenarios' / 'repeat-question' / 'repo'
        (repo / '.hermes.md').write_text('context\n', encoding='utf-8')
        assert_error(errors_for(clone), '.hermes.md')
        (repo / '.cursor').mkdir()
        (repo / '.cursor' / 'rules').write_text('context\n', encoding='utf-8')
        assert_error(errors_for(clone), '.cursor')
        (repo / 'mod.pyc').write_bytes(b'\x00compiled')
        assert_error(errors_for(clone), '.pyc')
        (repo / 'assets').mkdir()
        (repo / 'assets' / 'blob.bin').write_bytes(b'\x00\xff' + 'longhaul'.encode() + b'\x00')
        assert_error(errors_for(clone), 'forbidden token')


class TestCollection:
    def test_root_collection_ignores_subject_fixtures(self):
        assert (EVALS / 'conftest.py').is_file(), 'evals/conftest.py is missing'
        proc = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
                               '-p', 'no:cacheprovider', 'evals/scenarios'],
                              cwd=REPO, capture_output=True, text=True)
        assert 'test_calc' not in proc.stdout, proc.stdout[-400:]
        assert proc.returncode == 5, (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])


class TestControlTrivialAnswer:
    def test_control_trivial_answer_requires_a_single_version_token(self):
        pattern = casespec.load_expected(EVALS, 'control-trivial')['answer_regex']
        accepted = (
            '3.14.2',
            'The project declares version 3.14.2.',
            'VERSION says 3.14.2; nothing else.\nFINAL: complete\n',
        )
        rejected = (
            '3.14.20',
            '3.14.7 (python)',
            '3.14.2.1',
            'either 3.14.2 or 3.14.3',
            'no version here',
        )
        for text in accepted:
            assert re.search(pattern, text, re.M), text
        for text in rejected:
            assert not re.search(pattern, text, re.M), text


# --------------------------------------------------------------------------- #
# R3: the mutant tables must reproduce on the oracle, and prompts stay neutral
# --------------------------------------------------------------------------- #

oracle = load('lheval_oracle', EVALS / 'scripts' / 'oracle.py')


def _sha_manifest(root):
    out = {}
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if '__pycache__' in rel or rel.endswith('.pyc') or '.pytest_cache' in rel:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _stream(reply, calls):
    rows = [{'type': 'system', 'model': 'stub', 'session_id': 'stub'}]
    for index, call in enumerate(calls, 1):
        rows.append({'type': 'tool_use', 'name': call['name'], 'tool_call_id': 'c%d' % index,
                     'input': call.get('input') or {}})
        rows.append({'type': 'tool_result', 'name': call['name'],
                     'output': call.get('output') or '', 'duration_ms': 5,
                     'is_error': False})
    rows.append({'type': 'text', 'text': reply})
    rows.append({'type': 'result', 'session_id': 'stub', 'exit_code': 0, 'text': reply,
                 'tokens': {'input': 1200, 'output': 200, 'total': 1400},
                 'duration_ms': 10})
    return '\n'.join(json.dumps(row) for row in rows) + '\n'


def replay_mutant(tmp_path, case_row, expected, mutant):
    """Materialise v2, apply the mutant, replay its events, score it, weight the dims.

    The weighting mirrors a one-run aggregate: `cost` divides the cheapest valid run by
    itself, so it is 1.0, and any blocker zeroes the run.
    """
    rundir = tmp_path / 'runs' / 'v1.1' / case_row['id'] / 't1'
    work = rundir / 'work'
    casespec.materialize(EVALS, case_row['id'], 'v2', work)
    state_rel = expected.get('state_file')
    if state_rel:
        shutil.copyfile(work / state_rel, rundir / 'state.before.json')
    (rundir / 'manifest.before.json').write_text(json.dumps(_sha_manifest(work)),
                                                 encoding='utf-8')
    files = mutant.get('files')
    if files == 'golden':
        casespec.apply_golden(EVALS, case_row['id'], work)
    elif isinstance(files, dict):
        for rel, content in files.items():
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
    (rundir / 'manifest.after.json').write_text(json.dumps(_sha_manifest(work)),
                                                encoding='utf-8')
    (rundir / 'events.jsonl').write_text(_stream(mutant['reply'], mutant.get('events') or []),
                                         encoding='utf-8')
    (rundir / 'cmd.json').write_text(json.dumps({'argv': ['hermes', 'chat'],
                                                 'cwd': str(work), 'env_keys': []}),
                                     encoding='utf-8')
    (rundir / 'meta.json').write_text(json.dumps({'status': 'ok', 'trial': 1,
                                                  'key': 'v1.1/%s/t1' % case_row['id'],
                                                  'wall_s': 1.0}), encoding='utf-8')
    row = oracle.score_run(rundir, case_row, expected, REPO, 'v1.1')
    row['dims']['cost'] = 1.0
    total = weight = 0.0
    for dim, value in row['dims'].items():
        if value is None:
            continue
        weight += oracle.WEIGHTS[dim]
        total += oracle.WEIGHTS[dim] * value
    score = round(total / weight, 4) if weight else None
    if row['blockers']:
        score = 0.0
    return row, score


class TestMutantTables:
    def test_declared_mutant_scores_reproduce_on_the_oracle(self, tmp_path):
        """The 40 declared runs are the calibration of the whole eval: golden and null are
        the extremes and hide everything in between. Each table entry must reproduce its
        declared blocker set and score band on the real oracle."""
        mismatches = []
        for case_row in casespec.load_cases(EVALS):
            case_id = case_row['id']
            expected = casespec.load_expected(EVALS, case_id)
            table = load_mutants(case_id)
            for index, mutant in enumerate(table['mutants']):
                rundir_root = tmp_path / ('%s-%s' % (case_id, mutant['kind']))
                rundir_root.mkdir(parents=True, exist_ok=True)
                row, score = replay_mutant(rundir_root, case_row, expected, mutant)
                want = mutant['expected']
                if sorted(row['blockers']) != sorted(want['blockers']):
                    mismatches.append('%s/%s: blockers %r, declared %r'
                                      % (case_id, mutant['kind'], row['blockers'],
                                         want['blockers']))
                elif score is None or not (want['score_min'] <= score <= want['score_max']):
                    mismatches.append('%s/%s: score %r outside [%r, %r]'
                                      % (case_id, mutant['kind'], score,
                                         want['score_min'], want['score_max']))
                shutil.rmtree(rundir_root, ignore_errors=True)
        assert not mismatches, '\n'.join(mismatches)


class TestPromptNeutrality:
    def test_validate_rejects_condition_and_case_names_in_a_prompt(self, tmp_path):
        clone = suite_copy(tmp_path)
        prompt = clone / 'scenarios' / 'repeat-question' / 'prompt.md'
        original = prompt.read_text(encoding='utf-8')
        prompt.write_text(original.replace('Produce out/export.csv.',
                                           'Produce out/export.csv in condition v1.1.'),
                          encoding='utf-8')
        assert_error(errors_for(clone), 'v1.1')
        prompt.write_text(original.replace('Produce out/export.csv.',
                                           'Produce out/export.csv like recover-interrupt.'),
                          encoding='utf-8')
        assert_error(errors_for(clone), 'recover-interrupt')
        prompt.write_text(original.replace('Produce out/export.csv.',
                                           'Produce out/export.csv for the baseline run.'),
                          encoding='utf-8')
        assert_error(errors_for(clone), 'baseline')


class TestNullMutantRule:
    def test_null_mutant_may_be_correct_when_the_task_is_already_done(self, tmp_path):
        """On an already-gate-green task a do-nothing run is not a failure: only the judge
        separates it from an evidenced confirmation. Everywhere else it must stay bounded."""
        clone = suite_copy(tmp_path)
        edit_json(clone / 'scenarios' / 'control-finish' / 'mutants.json',
                  lambda table: table['mutants'][1]['expected'].update(score_min=1.0,
                                                                       score_max=1.0))
        assert errors_for(clone) == []

        clone = suite_copy(tmp_path / 'red')
        edit_json(clone / 'scenarios' / 'recover-interrupt' / 'mutants.json',
                  lambda table: table['mutants'][1]['expected'].update(score_min=1.0,
                                                                       score_max=1.0))
        assert_error(errors_for(clone), 'null')
