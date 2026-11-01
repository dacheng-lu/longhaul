"""End-to-end integration tests for the Longhaul eval harness (PLAN §8 INT).

Everything here runs against the real evals/ content and L1's fake hermes:
no network, no real profiles, no ~/.hermes/.env. HOME is a tmp directory, so
the only profile store the harness ever sees is disposable. The fake keeps its
own state under ``--fake-home`` because the subject runs with a sandbox HOME.

The flow proves the wiring (constants -> validate -> plan -> run -> resume ->
measure -> report) plus the R2 invariants: neutral subject paths, reaping after
a hard kill, judge rows only for judged cases, and a plain-text judge call.

Run: python3 -m pytest tests/test_eval_e2e.py -q
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / 'evals'
SCRIPTS = EVALS / 'scripts'
RUNNERS = EVALS / 'runners'
FAKE = REPO / 'tests' / 'fake_hermes.py'
KEY = '«redacted:sk-…»'
PROFILE_RE = re.compile(r'^lheval[a-z0-9]+$')
BEGIN = '<!-- generated:begin -->'
END = '<!-- generated:end -->'
JUDGE_STATUSES = ('ok', 'unparseable', 'error', 'unavailable')
CONSTANTS = ('DIMS', 'WEIGHTS', 'BLOCKERS', 'CHECK_DIMS', 'MANDATORY_CHECKS', 'STATUSES',
             'RETRYABLE', 'CASE_IDS', 'FINAL_CONTRACT', 'ALLOWED_ENV', 'PROFILE_RE',
             'SUBJECT', 'JUDGE')
LANE_MODULES = (
    ('hermes', RUNNERS / 'hermes.py'),
    ('ledger', SCRIPTS / 'ledger.py'),
    ('casespec', SCRIPTS / 'casespec.py'),
    ('oracle', SCRIPTS / 'oracle.py'),
    ('judge', SCRIPTS / 'judge.py'),
    ('report', SCRIPTS / 'report.py'),
    ('run_evals', SCRIPTS / 'run_evals.py'),
)
JUDGE_REPLY = ('OK. <SCORES>'
               '{"A": {"evidence_fidelity": 2, "blockers": []}, '
               '"B": {"evidence_fidelity": 1, "blockers": []}, '
               '"C": {"evidence_fidelity": 0, "blockers": []}}'
               '</SCORES>')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The harness must never reach a provider: no socket may be opened."""
    def blocked(*args, **kwargs):
        raise AssertionError('network access is forbidden in the eval harness tests')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)


def load(name, path=None):
    """D8: sibling modules are loaded as 'lheval_<name>', never imported."""
    path = Path(path) if path else SCRIPTS / (name + '.py')
    spec = importlib.util.spec_from_file_location('lheval_' + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['lheval_' + name] = module
    spec.loader.exec_module(module)
    return module


def fake_argv(home):
    """The double's entry inside an allowlisted home root: <home>/.local/bin/hermes.

    The interpreter is the system python and the state stays in ``home`` (which
    the confined offline run binds as the subject HOME).
    """
    home = Path(home)
    bindir = home / '.local' / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / 'hermes'
    if not target.exists():
        body = FAKE.read_text(encoding='utf-8')
        if body.startswith('#!'):
            body = body.split('\n', 1)[1]
        target.write_text('#!/usr/bin/python3\n' + body, encoding='utf-8')
        target.chmod(0o755)
    return '%s --fake-home %s' % (target, home)


def prepare_home(tmp_path):
    home = Path(tmp_path) / 'home'
    home.mkdir(parents=True, exist_ok=True)
    return home


def configure(home, **values):
    (Path(home) / '.fake_hermes.json').write_text(json.dumps(values), encoding='utf-8')


def ledger_rows(out, run_id):
    path = Path(out) / run_id / 'ledger.jsonl'
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]


def manifest(root):
    root = Path(root)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()}


def offline(home):
    """The confined offline harness: the double's state directory is its HOME."""
    return ['--offline-state', str(home)]


def run_cli(mod, home, out, run_id, cases, conditions, extra=()):
    return mod.main(['run', '--out-root', str(out), '--run-id', run_id, '--trials', '1',
                     '--cases', cases, '--conditions', conditions,
                     '--hermes', fake_argv(home)] + offline(home) + list(extra))


def profiles_on_disk(home):
    profiles = Path(home) / '.hermes' / 'profiles'
    if not profiles.is_dir():
        return []
    return sorted(path.name for path in profiles.iterdir() if path.name.startswith('lheval'))


def live_profiles(rows):
    """The R2 invariant: the last event per profile decides whether it is live."""
    state = {}
    for row in rows:
        name = row.get('profile')
        if not name:
            continue
        if row.get('event') == 'profile_created':
            state[name] = True
        elif row.get('event') == 'profile_deleted':
            state[name] = False
    return sorted(name for name, live in state.items() if live)


def surviving_subjects():
    """Processes that still run the fake with a harness profile name."""
    hits = []
    proc_root = Path('/proc')
    if not proc_root.is_dir():
        return hits
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                'utf-8', 'replace')
        except OSError:
            continue
        if '-p lheval' in text and '--fake-home' in text:
            hits.append(text)
    return hits


# --------------------------------------------------------------------------- #
# (a) shared constants and (d) source hygiene
# --------------------------------------------------------------------------- #

class TestLaneContract:
    def test_constants_are_identical_across_every_lane(self):
        modules = {name: load(name, path) for name, path in LANE_MODULES}
        assert set(modules) == {name for name, _ in LANE_MODULES}
        for constant in CONSTANTS:
            values = {name: getattr(module, constant) for name, module in modules.items()}
            unique = {repr(value) for value in values.values()}
            assert len(unique) == 1, '%s differs across lanes: %r' % (constant, values)

    def test_eval_sources_are_py310_and_stdlib_only(self):
        # Scenario repos are subject fixtures, not harness code: they model a user
        # project and may import their own local modules (e.g. `src`). Every other
        # .py under evals/ is harness code and must be stdlib-only.
        files = sorted(path for path in EVALS.rglob('*.py')
                       if '__pycache__' not in path.parts and 'scenarios' not in path.parts)
        assert files, 'no eval sources found'
        assert any('runners' in path.parts for path in files)
        assert any('scripts' in path.parts for path in files)
        stdlib = set(sys.stdlib_module_names)
        for path in files:
            source = path.read_text(encoding='utf-8')
            ast.parse(source, feature_version=(3, 10))
            for node in ast.walk(ast.parse(source)):
                roots = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split('.')[0]]
                for root in roots:
                    assert root in stdlib or root.startswith('lheval_'), \
                        '%s imports %r' % (path, root)


# --------------------------------------------------------------------------- #
# (b) + (e) the deterministic flow
# --------------------------------------------------------------------------- #

def test_end_to_end_validate_plan_run_resume_measure_report(tmp_path, monkeypatch, capsys):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    out = tmp_path / 'out'
    run_id = 'e2e'
    cases = ['control-finish', 'control-trivial']
    common = ['--out-root', str(out), '--run-id', run_id, '--trials', '1',
              '--cases', ','.join(cases), '--hermes', fake_argv(home)]
    mod = load('run_evals')
    configure(home, mode='ok', reply='Done.\nFINAL: complete', skills=['longhaul'])

    # -- validate ----------------------------------------------------------- #
    assert mod.main(['validate'] + common) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {'ok': True, 'errors': []}

    # -- plan: 2 cases x 3 conditions x 1 trial, and no model call ---------- #
    assert mod.main(['plan'] + common) == 0
    planned = [json.loads(line) for line in capsys.readouterr().out.splitlines()
               if line.strip()]
    assert len(planned) == 6
    names = [row['name'] for row in planned]
    assert len(set(names)) == 6 and all(PROFILE_RE.match(name) for name in names)
    assert all(re.fullmatch(r'lheval[0-9a-f]{16}', name) for name in names), names
    for row in planned:
        if row['cond'] == 'baseline':
            assert '-s' not in row['argv'] and 'longhaul' not in row['argv']
        else:
            at = row['argv'].index('-s')
            assert row['argv'][at + 1] == 'longhaul' and row['argv'].count('-s') == 1
        assert row['cwd'].startswith('/var/tmp/lheval-subject/'), row['cwd']
        for token in ('baseline', 'v1.1', 'control-trivial', 'control-finish'):
            assert token not in row['cwd'], (token, row['cwd'])
    assert all(row['argv'][row['argv'].index('-m') + 1] == 'deepseek-v4.1-flash:1017'
               for row in planned)
    assert all(row['argv'][row['argv'].index('--provider') + 1] == 'ollama-cloud'
               for row in planned)
    assert not (home / 'fake_calls.jsonl').exists(), 'plan must not call the subject'

    # -- run 1: the subject dies after the first finished key --------------- #
    configure(home, mode='ok', reply='Done.\nFINAL: complete',
              script=[{}] + [{'mode': 'crash'}] * 5)
    assert run_cli(mod, home, out, run_id, ','.join(cases), 'baseline,v1,v1.1',
                   ['--parallel', '1', '--max-attempts', '1']) == 0
    first = [row for row in ledger_rows(out, run_id) if row.get('event') == 'finished']
    assert len(first) == 6
    ok_first = [row['key'] for row in first if row.get('status') == 'ok']
    assert ok_first == ['baseline/control-trivial/t1']
    assert all(row.get('status') == 'hermes_crash'
               for row in first if row['key'] != ok_first[0])

    # -- resume: only the unfinished keys run again ------------------------- #
    # The double counts chats across invocations: the first run consumed six,
    # so indices 6..10 are the resumed candidate/baseline calls (index 8 is the
    # remaining baseline run and must not mention the skill).
    skill_call = [{'name': 'skill_view', 'input': {'name': 'longhaul'},
                   'output': 'longhaul skill loaded'}]
    configure(home, mode='ok', reply='Done.\nFINAL: complete',
              script=[{}] * 6 + [{'tool_calls': skill_call},
                                 {'tool_calls': skill_call}, {},
                                 {'tool_calls': skill_call},
                                 {'tool_calls': skill_call}])
    assert run_cli(mod, home, out, run_id, ','.join(cases), 'baseline,v1,v1.1',
                   ['--parallel', '1', '--max-attempts', '2']) == 0
    rows = ledger_rows(out, run_id)
    ok_rows = [row for row in rows
               if row.get('event') == 'finished' and row.get('status') == 'ok']
    per_key = Counter(row['key'] for row in ok_rows)
    assert set(per_key) == {'%s/%s/t1' % (cond, case)
                            for cond in ('baseline', 'v1', 'v1.1') for case in cases}
    assert all(count == 1 for count in per_key.values()), per_key
    started = Counter(row['key'] for row in rows if row.get('event') == 'started')
    assert started['baseline/control-trivial/t1'] == 1, 'an ok key was re-run'
    assert all(started[key] == 2 for key in started
               if key != 'baseline/control-trivial/t1')
    created = Counter(row['profile'] for row in rows if row.get('event') == 'profile_created')
    deleted = Counter(row['profile'] for row in rows if row.get('event') == 'profile_deleted')
    assert created == deleted, 'profile lifecycle: %r vs %r' % (created, deleted)
    assert live_profiles(rows) == []
    assert profiles_on_disk(home) == []

    for case in cases:
        for cond in ('baseline', 'v1', 'v1.1'):
            rundir = out / run_id / 'runs' / cond / case / 't1'
            meta = json.loads((rundir / 'meta.json').read_text(encoding='utf-8'))
            assert meta['status'] == 'ok'
            assert (rundir / 'events.jsonl').is_file()
            assert (rundir / 'manifest.after.json').is_file()
            assert (rundir / 'work').is_dir()
            if cond == 'baseline':
                assert meta['skill_requested'] is False
                assert meta['skill_loaded'] is False
            else:
                assert meta['skill_requested'] is True
                assert meta['skill_loaded'] == 'confirmed', 'the fake stream confirms the skill'

    # -- measure with a fake judge ------------------------------------------ #
    configure(home, mode='ok', reply=JUDGE_REPLY)
    assert mod.main(['measure'] + common) == 0
    measured = read_jsonl(out / run_id / 'measure.jsonl')
    assert len(measured) == 6
    assert all(row['status'] == 'ok' and row['valid'] for row in measured)
    judged = read_jsonl(out / run_id / 'judge.jsonl')
    # Only control-finish declares judge dims; three conditions -> three rotated passes.
    assert len(judged) == 3, judged
    assert [row['case'] for row in judged] == ['control-finish'] * 3
    assert [row['pass'] for row in judged] == [1, 2, 3]
    assert len({tuple(row['order']) for row in judged}) == 3, 'the passes must rotate'
    for row in judged:
        assert sorted(row['labels'].values()) == ['baseline', 'v1', 'v1.1']
        assert row['status'] in JUDGE_STATUSES

    # -- report into a COPY of RESULTS.md ----------------------------------- #
    copy = tmp_path / 'results-copy'
    copy.mkdir()
    shutil.copy2(EVALS / 'RESULTS.md', copy / 'RESULTS.md')
    before = (copy / 'RESULTS.md').read_text(encoding='utf-8')
    real_before = (EVALS / 'RESULTS.md').read_text(encoding='utf-8')
    monkeypatch.setattr(mod, 'EVALS_DIR', copy)
    assert mod.main(['report', '--out-root', str(out), '--run-id', run_id]) == 0
    after = (copy / 'RESULTS.md').read_text(encoding='utf-8')

    head = before.index(BEGIN) + len(BEGIN)
    end = before.index(END)
    assert after[:head] == before[:head], 'bytes before the section changed'
    assert after[after.index(END):] == before[end:], 'bytes after the section changed'
    section = after[head:after.index(END)]
    header = next(row for row in ledger_rows(out, run_id) if row.get('event') == 'header')
    for needle in ('## Eval results', 'deepseek-v4.1-flash:1017', 'ollama-cloud',
                   'claude-sonnet-5-5', '0.21.6', '### Raw runs', '### Got worse',
                   header['conditions']['v1']['skill_fp'], 'exploratory, n=1'):
        assert needle in section, needle
    summary = json.loads((out / run_id / 'summary.json').read_text(encoding='utf-8'))
    assert summary['trials'] == 1 and len(summary['runs']) == 6
    assert (EVALS / 'RESULTS.md').read_text(encoding='utf-8') == real_before, \
        'the test wrote the repository RESULTS.md instead of the copy'

    # -- (e) A6: no key ever reaches out_root ------------------------------ #
    markers = (KEY, 'OLLAMA_API_KEY')
    hits = []
    for path in out.rglob('*'):
        if path.is_file():
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if any(marker in text for marker in markers):
                hits.append(str(path))
    assert hits == [], 'a model key leaked into %r' % hits
    # The offline path has no credential at all: nothing wrote one.
    assert not (home / '.hermes' / '.env').exists(), 'an offline run wrote a key'
    assert not (home / '.hermes').exists(), home / '.hermes'


# --------------------------------------------------------------------------- #
# (c) A9 against the real scenarios
# --------------------------------------------------------------------------- #

def build_golden(evals_dir, casespec, case, rundir):
    work = Path(rundir) / 'work'
    casespec.materialize(evals_dir, case['id'], 'v2', work)
    before = manifest(work)
    if (work / '.task' / 'state.json').is_file():
        shutil.copy2(work / '.task' / 'state.json', Path(rundir) / 'state.before.json')
    casespec.apply_golden(evals_dir, case['id'], work)
    after = manifest(work)
    (Path(rundir) / 'events.jsonl').write_text(
        (evals_dir / case['scenario'] / 'golden' / 'events.jsonl').read_text(encoding='utf-8'),
        encoding='utf-8')
    (Path(rundir) / 'manifest.before.json').write_text(json.dumps(before), encoding='utf-8')
    (Path(rundir) / 'manifest.after.json').write_text(json.dumps(after), encoding='utf-8')
    (Path(rundir) / 'meta.json').write_text(json.dumps({
        'status': 'ok', 'rc': 0, 'wall_s': 1.0, 'attempt': 1, 'profile': 'lhevalx',
        'skill_fp': 'fp', 'session_id': 's', 'started_at': '2026-01-01T00:00:00Z',
        'finished_at': '2026-01-01T00:00:01Z', 'error': None, 'trial': 1,
        'key': 'v1.1/%s/t1' % case['id']}), encoding='utf-8')


def build_null(evals_dir, casespec, case, rundir):
    work = Path(rundir) / 'work'
    casespec.materialize(evals_dir, case['id'], 'v2', work)
    files = manifest(work)
    reply = 'FINAL: complete'
    events = [{'type': 'text', 'text': reply},
              {'type': 'result', 'session_id': 's', 'exit_code': 0, 'text': reply,
               'tokens': {'input': 10, 'output': 5, 'total': 15}, 'duration_ms': 1}]
    (Path(rundir) / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in events) + '\n', encoding='utf-8')
    (Path(rundir) / 'manifest.before.json').write_text(json.dumps(files), encoding='utf-8')
    (Path(rundir) / 'manifest.after.json').write_text(json.dumps(files), encoding='utf-8')
    (Path(rundir) / 'meta.json').write_text(json.dumps({
        'status': 'ok', 'rc': 0, 'wall_s': 1.0, 'attempt': 1, 'profile': 'lhevalx',
        'skill_fp': 'fp', 'session_id': 's', 'started_at': '2026-01-01T00:00:00Z',
        'finished_at': '2026-01-01T00:00:01Z', 'error': None, 'trial': 1,
        'key': 'v1.1/%s/t1' % case['id']}), encoding='utf-8')


def test_golden_overlay_scores_one_and_null_subject_fails(tmp_path):
    casespec = load('casespec')
    oracle = load('oracle')
    report = load('report')
    cases = casespec.load_cases(EVALS)
    assert len(cases) == 10
    header = {'v': 1, 'event': 'header', 'run_id': 'a9', 'created_at': '2026-01-01T00:00:00Z',
              'repo_head': 'af52cd8', 'hermes_version': '0.21.6',
              'model': 'deepseek-v4.1-flash:1017', 'provider': 'ollama-cloud',
              'judge_model': 'claude-sonnet-5-5', 'trials': 1, 'max_turns': 60,
              'budget_s': 600, 'conditions': {'v1.1': {'commit': 'af52cd8',
                                                       'version': '1.1', 'skill_fp': 'fp'}}}

    golden, null = [], []
    for case in cases:
        case_id = case['id']
        expected = casespec.load_expected(EVALS, case_id)
        golden_dir = tmp_path / 'golden' / case_id / 't1'
        golden_dir.mkdir(parents=True)
        build_golden(EVALS, casespec, case, golden_dir)
        golden.append(oracle.score_run(golden_dir, case, expected, REPO, 'v1.1'))

        null_dir = tmp_path / 'null' / case_id / 't1'
        null_dir.mkdir(parents=True)
        build_null(EVALS, casespec, case, null_dir)
        null.append(oracle.score_run(null_dir, case, expected, REPO, 'v1.1'))

    for row in golden:
        assert row['status'] == 'ok' and row['valid'], row['key']
        assert row['blockers'] == [], '%s: %r' % (row['key'], row['blockers'])
        scored = {dim: value for dim, value in row['dims'].items() if value is not None}
        assert scored, '%s has no scored dimension' % row['key']
        assert all(value == 1 for value in scored.values()), '%s: %r' % (row['key'], row['dims'])
        assert row['ground_truth_pass'] is not False

    null_scores = {run['key']: run['score']
                   for run in report.aggregate(null, [], header)['runs']}
    controls = {'control-trivial', 'control-finish'}
    for row in null:
        if row['case'] in controls:
            continue
        score = null_scores[row['key']]
        assert row['blockers'] or (score is not None and score < 50), \
            '%s scored %r without a blocker' % (row['key'], score)


# --------------------------------------------------------------------------- #
# R2 invariants (the lanes now own the fixes)
# --------------------------------------------------------------------------- #

def test_run_materialises_the_scenario_fixture(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete', skills=['longhaul'])
    mod = load('run_evals')
    out = tmp_path / 'out'
    assert run_cli(mod, home, out, 'mat', 'control-trivial,control-finish', 'baseline',
                   ['--parallel', '1', '--max-attempts', '1']) == 0
    work = out / 'mat' / 'runs' / 'baseline' / 'control-trivial' / 't1' / 'work'
    assert work.is_dir()
    assert (work / 'VERSION').is_file(), 'the work dir is empty: materialize() was never called'
    finish = out / 'mat' / 'runs' / 'baseline' / 'control-finish' / 't1' / 'work'
    assert (finish / '.task' / 'state.json').is_file(), 'control-finish ships a state'


def test_resume_does_not_leak_a_recreated_profile(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    out = tmp_path / 'out'

    configure(home, mode='ok', reply='Done.\nFINAL: complete', script=[{'mode': 'crash'}])
    assert run_cli(mod, home, out, 'leak', 'control-trivial', 'baseline',
                   ['--parallel', '1', '--max-attempts', '1']) == 0
    configure(home, mode='ok', reply='Done.\nFINAL: complete', skills=['longhaul'])
    assert run_cli(mod, home, out, 'leak', 'control-trivial', 'baseline',
                   ['--parallel', '1', '--max-attempts', '2']) == 0

    rows = ledger_rows(out, 'leak')
    created = Counter(row['profile'] for row in rows if row.get('event') == 'profile_created')
    deleted = Counter(row['profile'] for row in rows if row.get('event') == 'profile_deleted')
    assert created == deleted, 'ledger: %r vs %r' % (created, deleted)
    assert live_profiles(rows) == []
    assert profiles_on_disk(home) == []


def test_resume_after_a_hard_kill(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete', sleep=8)
    out = tmp_path / 'out'
    run_id = 'kill'
    argv = [sys.executable, str(SCRIPTS / 'run_evals.py'), 'run',
            '--out-root', str(out), '--run-id', run_id, '--trials', '1',
            '--cases', 'control-finish', '--conditions', 'baseline,v1',
            '--hermes', fake_argv(home), '--parallel', '1', '--max-attempts', '1']
    env = dict(os.environ)
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 25
        started_rows = []
        while time.time() < deadline:
            started_rows = [row for row in ledger_rows(out, run_id)
                            if row.get('event') == 'started']
            if any(row.get('pgid') for row in started_rows):
                break
            time.sleep(0.02)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    finally:
        proc.wait(timeout=15)

    assert started_rows, 'the kill happened before any subject started'
    # Whether the bwrap child survives the harness kill is a race in bwrap 0.11.2
    # (PDEATHSIG vs. exec); both outcomes must be handled. The owned sandbox
    # directory is the invariant, and the resume must leave no subject behind.
    assert live_profiles(ledger_rows(out, run_id)), 'no live profile to recover from'
    assert all(Path(row['execdir']).is_dir() for row in started_rows
               if row.get('execdir')), 'the sandbox directory did not survive the kill'

    configure(home, mode='ok', reply='Done.\nFINAL: complete', skills=['longhaul'])
    resume = list(argv)
    resume[resume.index('--max-attempts') + 1] = '2'
    done = subprocess.run(resume, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]
    deadline = time.time() + 15
    while time.time() < deadline and surviving_subjects():
        time.sleep(0.05)
    assert surviving_subjects() == [], ('the resumed harness left a subject behind: %r'
                                        % (surviving_subjects(),))
    assert live_profiles(ledger_rows(out, run_id)) == []
    assert profiles_on_disk(home) == []


def test_sigkill_cleanup_is_idempotent(tmp_path, monkeypatch):
    """A hard kill leaves a sandbox; --cleanup must reap it, twice, without a traceback."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete', sleep=8)
    out = tmp_path / 'out'
    run_id = 'cleanup'
    argv = [sys.executable, str(SCRIPTS / 'run_evals.py'), 'run',
            '--out-root', str(out), '--run-id', run_id, '--trials', '1',
            '--cases', 'control-finish', '--conditions', 'baseline',
            '--hermes', fake_argv(home), '--parallel', '1', '--max-attempts', '1']
    env = dict(os.environ)
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 25
        started = []
        while time.time() < deadline:
            started = [row for row in ledger_rows(out, run_id)
                       if row.get('event') == 'started']
            if any(row.get('pgid') for row in started):
                break
            time.sleep(0.02)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    finally:
        proc.wait(timeout=15)

    assert started, 'the kill happened before any subject started'
    execdirs = [Path(row['execdir']) for row in started if row.get('execdir')]
    assert execdirs and all(path.exists() for path in execdirs)
    assert all((path / 'home' / '.hermes').is_dir() for path in execdirs), \
        'the sandbox HOME was gone before cleanup'
    # A SIGKILL may leave the bwrap child running or not (PDEATHSIG race); both
    # outcomes are legal, but --cleanup must end with no subject and no directory.
    for attempt in range(2):
        done = subprocess.run(list(argv) + ['--cleanup'], env=env, capture_output=True,
                              text=True)
        assert done.returncode == 0, (attempt, done.stderr[-800:])
        assert 'Traceback' not in done.stderr, done.stderr[-800:]
        deadline = time.time() + 5
        while time.time() < deadline and surviving_subjects():
            time.sleep(0.02)
        assert surviving_subjects() == [], '--cleanup left the subject running'
        assert all(not path.exists() for path in execdirs)
        assert live_profiles(ledger_rows(out, run_id)) == []

    resume = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert resume.returncode in (0, 1), resume.stderr[-800:]
    assert 'Traceback' not in resume.stderr, resume.stderr[-800:]


def test_measure_with_a_fake_judge_parses_scores(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete', skills=['longhaul'])
    mod = load('run_evals')
    out = tmp_path / 'out'
    assert run_cli(mod, home, out, 'judge', 'control-finish', 'baseline,v1',
                   ['--parallel', '1', '--max-attempts', '1']) == 0
    configure(home, mode='ok', reply=JUDGE_REPLY)
    assert mod.main(['measure', '--out-root', str(out), '--run-id', 'judge',
                     '--trials', '1', '--hermes', fake_argv(home)]) == 0
    rows = read_jsonl(out / 'judge' / 'judge.jsonl')
    assert rows
    assert [row['status'] for row in rows] == ['ok', 'ok'], \
        'judge rows are %r' % [row['status'] for row in rows]
    assert all(row['parsed'] for row in rows)


# --------------------------------------------------------------------------- #
# R3: live probe -> pilot -> matrix with the real bwrap and the fake entry
# --------------------------------------------------------------------------- #

BWRAP = Path('/usr/bin/bwrap')
LIVE_OK = BWRAP.is_file() and Path('/usr/bin/python3').is_file()
ISOLATION = re.compile(r'^ISOLATION home=(\S+) cwd=(\S+) argv0=(\S+) pid1=(.*)$', re.M)
TOKEN = re.compile(r'^[0-9a-f]{20}$')


def live_home(tmp_path):
    """A throw-away home whose launcher chain is the fake, inside an allowed root."""
    home = Path(tmp_path) / 'subject-home'
    (home / '.hermes').mkdir(parents=True)
    (home / '.hermes' / '.env').write_text('OLLAMA_API_KEY=%s\n' % KEY, encoding='utf-8')
    bindir = home / '.local' / 'bin'
    bindir.mkdir(parents=True)
    body = FAKE.read_text(encoding='utf-8').split('\n', 1)[1]
    (bindir / 'hermes').write_text('#!/usr/bin/python3\n' + body, encoding='utf-8')
    (bindir / 'hermes').chmod(0o755)
    (home / 'host-canary.txt').write_text('host secret\n', encoding='utf-8')
    return home, str(bindir / 'hermes')


def test_live_probe_pilot_and_matrix_isolate_every_subprocess(tmp_path, monkeypatch):
    """The production plan end to end: the live probe runs in real bwrap.

    Subprocess-level evidence: the fake reports (on stderr, from inside) the
    sandbox HOME, the neutral cwd and a bwrap PID 1; the host profile store and
    the fake call log stay empty; every recorded argv is bwrap's. The live pilot
    and matrix are fail-closed (R4 BLOCKER): they must refuse before a key is
    read and start nothing, so the only live artefact is probe.json.
    """
    require_bwrap()
    home, entry = live_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    run_id = 'live'
    common = ['--out-root', str(out), '--run-id', run_id, '--trials', '1',
              '--hermes', entry, '--parallel', '1', '--max-attempts', '1']

    # -- probe ------------------------------------------------------------- #
    assert mod.main(['run', '--live', '--probe'] + common) == 0, 'the probe refused'
    probe = json.loads((out / run_id / 'probe.json').read_text())
    assert probe['clean'] is True and probe['problems'] == []
    assert probe['mode'] == 'live'
    assert probe['bwrap_version'] == mod._bwrap_version(mod.load_hermes())
    for label in ('baseline', 'candidate'):
        record = probe[label]
        assert record['rc'] == 0 and record['inspect_rc'] in (0, 1), record
        assert record['argv'][0] == str(BWRAP)
        facts = record['facts']
        assert facts['dirs']['/tmp']['entries'] == []
        assert facts['dirs']['/run/user']['entries'] == []
        sandboxes = facts['dirs'][str(mod.SUBJECT_ROOT)]['entries']
        assert len(sandboxes) == 1 and TOKEN.match(sandboxes[0]), sandboxes
        if label == 'baseline':
            assert facts['find'] == []
        else:
            root = record['skill_root']
            assert root.endswith('/skills/longhaul') and str(mod.SUBJECT_ROOT) in root
            assert facts['find'] and all(hit.startswith(root) for hit in facts['find'])

    # -- live pilot and matrix: fail-closed, nothing starts ----------------- #
    for argv in (['run', '--live', '--pilot'] + common,
                 ['run', '--live', '--cases', 'control-trivial,control-finish'] + common):
        assert mod.main(argv) == 3, argv
    assert not (out / run_id / 'pilot' / 'ledger.jsonl').exists(), \
        'the blocked pilot started attempts'
    assert not (out / run_id / 'ledger.jsonl').exists(), \
        'the blocked matrix created a ledger'
    assert not (home / '.hermes' / 'profiles').exists(), \
        'a profile was created in the host home'
    assert not (home / 'fake_calls.jsonl').exists(), \
        'the subject ran in the host home'


def test_live_probe_cannot_see_a_decoy_sibling(tmp_path, monkeypatch):
    """A sibling sandbox on the host must be invisible inside the probe sandbox.

    The probe lists ``/var/tmp/lheval-subject`` from inside; the tmpfs must leave
    exactly the own token, and the decoy (no `longhaul` in its name) must survive.
    """
    require_bwrap()
    home, entry = live_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    decoy = Path('/var/tmp/lheval-subject') / ('b' * 20)
    shutil.rmtree(decoy, ignore_errors=True)
    (decoy / 'work').mkdir(parents=True)
    (decoy / 'work' / 'secret_notes.txt').write_text('sibling\n', encoding='utf-8')
    mod = load('run_evals')
    out = tmp_path / 'out'
    try:
        code = mod.main(['run', '--live', '--probe', '--out-root', str(out),
                         '--run-id', 'decoy', '--trials', '1', '--hermes', entry])
        assert code == 0, code
        doc = json.loads((out / 'decoy' / 'probe.json').read_text())
        assert doc['clean'] is True, doc['problems']
        for label in ('baseline', 'candidate'):
            entries = doc[label]['facts']['dirs'][str(mod.SUBJECT_ROOT)]['entries']
            assert len(entries) == 1 and entries[0] != decoy.name, entries
        assert decoy.exists(), 'the probe deleted a foreign sandbox'
        assert (decoy / 'work' / 'secret_notes.txt').is_file()
    finally:
        shutil.rmtree(decoy, ignore_errors=True)


def test_pilot_rejects_a_crashed_case_and_writes_no_pilot_json(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete',
              script=[{}, {'mode': 'crash'}] + [{'mode': 'crash'}] * 9)
    mod = load('run_evals')
    out = tmp_path / 'out'
    code = mod.main(['run', '--pilot', '--out-root', str(out), '--run-id', 'crash',
                     '--hermes', fake_argv(home), '--parallel', '1', '--max-attempts', '1'])
    assert code == 3, 'a crashed pilot must be refused'
    assert not (out / 'crash' / 'pilot.json').exists(), 'a crashed pilot wrote pilot.json'


def test_pilot_does_not_pin_the_matrix_header(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete')
    mod = load('run_evals')
    out = tmp_path / 'out'
    common = ['--out-root', str(out), '--run-id', 'ph', '--trials', '1',
              '--hermes', fake_argv(home), '--parallel', '1', '--max-attempts', '1']
    assert mod.main(['run', '--pilot'] + common) == 0
    assert not (out / 'ph' / 'ledger.jsonl').exists(), \
        'the pilot wrote into the matrix ledger'
    assert (out / 'ph' / 'pilot.json').is_file()
    assert mod.main(['run', '--cases', 'control-trivial,control-finish'] + common) == 0
    rows = ledger_rows(out, 'ph')
    header = next(row for row in rows if row.get('event') == 'header')
    assert set(header['conditions']) == {'baseline', 'v1', 'v1.1'}, header['conditions']
    keys = {row['key'] for row in rows
            if row.get('event') == 'finished' and row.get('status') == 'ok'}
    assert any(key.startswith('v1.1/') for key in keys), keys
    assert any(key.startswith('v1/') for key in keys), keys


def test_live_pilot_requires_a_probe_and_starts_nothing(tmp_path, monkeypatch):
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete')
    mod = load('run_evals')
    out = tmp_path / 'out'
    code = mod.main(['run', '--live', '--pilot', '--out-root', str(out),
                     '--run-id', 'noprobe', '--trials', '1', '--hermes', fake_argv(home)])
    assert code == 3
    assert not (out / 'noprobe' / 'pilot' / 'ledger.jsonl').exists(), \
        'the pilot started attempts before the probe gate passed'
    assert not (home / 'fake_calls.jsonl').exists()


def test_live_probe_with_a_missing_binary_is_a_preflight_failure(tmp_path, monkeypatch):
    """R3 MINOR: a missing --hermes must be rc 3, not an uncaught rc 1."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    out = tmp_path / 'out'
    code = mod.main(['run', '--live', '--probe', '--out-root', str(out),
                     '--run-id', 'nobin', '--hermes', '/nonexistent/hermes'])
    assert code == 3
    doc = json.loads((out / 'nobin' / 'probe.json').read_text())
    assert doc['clean'] is False
    assert any('chain' in line for line in doc['problems']), doc['problems']


def test_keep_profiles_keeps_the_sandbox_home_and_cleanup_reaps_it(tmp_path, monkeypatch):
    """R3 MINOR: --keep-profiles used to be inert because the execdir was dropped."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete')
    mod = load('run_evals')
    out = tmp_path / 'out'
    sandbox_root = tmp_path / 'sandboxes'
    monkeypatch.setattr(mod, 'SUBJECT_ROOT', sandbox_root)
    monkeypatch.setattr(mod.load_hermes(), 'SUBJECT_ROOT', sandbox_root)
    assert run_cli(mod, home, out, 'kp', 'control-trivial', 'baseline',
                   ['--parallel', '1', '--max-attempts', '1', '--keep-profiles']) == 0
    rows = ledger_rows(out, 'kp')
    execdirs = [Path(row['execdir']) for row in rows if row.get('event') == 'attempt_dir']
    assert execdirs and all(path.is_dir() for path in execdirs)
    profiles = execdirs[0] / 'home' / '.hermes' / 'profiles'
    assert profiles.is_dir() and list(profiles.iterdir()), 'the profile was not kept'
    assert live_profiles(rows), 'the kept profile is still live in the ledger'
    assert run_cli(mod, home, out, 'kp', 'control-trivial', 'baseline', ['--cleanup']) == 0
    assert not execdirs[0].exists(), '--cleanup did not reap the kept sandbox'
    assert live_profiles(ledger_rows(out, 'kp')) == []


# --------------------------------------------------------------------------- #
# R4: the four audit canaries as regressions (canary-only, no real secrets)
# --------------------------------------------------------------------------- #

def _entry(home):
    return str(Path(home) / '.local' / 'bin' / 'hermes')


def test_cli_refuses_a_subject_without_a_confined_offline_home(tmp_path, monkeypatch):
    """R4 BLOCKER: `run --hermes` must never execute an arbitrary binary."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    out = tmp_path / 'out'
    hostfile = tmp_path / 'outside-write.txt'
    configure(home, writes=[{'path': str(hostfile), 'content': 'host-write'},
                            {'path': 'subject-canary.txt', 'content': 'inside-work'}])
    entry = fake_argv(home)
    base = ['run', '--out-root', str(out), '--run-id', 'r4', '--conditions', 'baseline',
            '--cases', 'control-trivial', '--trials', '1', '--max-attempts', '1']

    def refused(argv):
        assert mod.main(argv) == 2, argv
        assert not (out / 'r4').exists(), 'a refused subject created a run dir'
        assert not hostfile.exists(), 'a refused subject wrote to the host'
        assert not (home / 'fake_calls.jsonl').exists(), 'a refused subject ran'

    # 1. a double with one edited byte is not the verified double: refused
    rogue = tmp_path / 'hermes-rogue'
    rogue.write_text((Path(entry.split()[0]).read_text(encoding='utf-8')
                      + '\n# edited\n'), encoding='utf-8')
    rogue.chmod(0o755)
    refused(base + ['--hermes', str(rogue)] + offline(home))
    # 2. an arbitrary host binary is refused too, even with a state dir
    refused(base + ['--hermes', '/bin/sh'] + offline(home))
    # 3. a state dir outside the sanctioned scratch is refused, not "confined-ish"
    refused(base + ['--hermes', entry, '--offline-state', '/home'])

    # 4. positive control: the verified double runs inside bwrap, no host write, no net
    assert mod.main(base + ['--hermes', entry] + offline(home)) == 0
    rundir = out / 'r4' / 'runs' / 'baseline' / 'control-trivial' / 't1'
    cmd = json.loads((rundir / 'cmd.json').read_text())
    assert cmd['wrapped'] is True and Path(cmd['argv'][0]).name == 'bwrap'
    assert '--unshare-net' in cmd['argv']
    assert not hostfile.exists(), 'the subject wrote outside its sandbox'
    assert (rundir / 'work' / 'subject-canary.txt').read_text() == 'inside-work'
    log = rundir / 'fake-log.jsonl'
    assert log.is_file() and log.read_text().strip(), \
        'the double never ran: the test would pass vacuously'


def test_offline_subject_cannot_reach_host_files_or_sockets(tmp_path, monkeypatch):
    """R4 BLOCKER (continued): confinement is proven by the child, not by argv."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    out = tmp_path / 'out'
    host_canary = tmp_path / 'host-private.txt'
    host_canary.write_text('CANARY-HOST-FILE-1357\n', encoding='utf-8')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    configure(home, visibility=[str(host_canary), str(REPO)],
              connect=['127.0.0.1', port], writes=[{'path': 'out.txt', 'content': 'ok'}])
    try:
        assert mod.main(['run', '--out-root', str(out), '--run-id', 'conf',
                         '--conditions', 'baseline', '--cases', 'control-trivial',
                         '--trials', '1', '--max-attempts', '1', '--parallel', '1',
                         '--hermes', fake_argv(home)] + offline(home)) == 0
    finally:
        listener.close()
    rundir = out / 'conf' / 'runs' / 'baseline' / 'control-trivial' / 't1'
    stderr = (rundir / 'stderr.txt').read_text()
    assert 'VISIBLE %s 0' % host_canary in stderr, stderr[-800:]
    assert 'VISIBLE %s 0' % REPO in stderr, stderr[-800:]
    assert 'CONNECT 127.0.0.1:%d 0' % port in stderr, stderr[-800:]
    # positive control: the harness-owned work file is visible and writable, and
    # the subject HOME is the neutral sandbox home, not the harness $HOME
    assert (rundir / 'work' / 'out.txt').read_text() == 'ok'
    isolation = ISOLATION.search(stderr)
    assert isolation, stderr[-800:]
    reported_home = isolation.group(1)
    assert reported_home.startswith(str(mod.SUBJECT_ROOT) + '/'), reported_home
    assert reported_home != str(home), 'the subject ran with the harness HOME'


def test_live_matrix_pilot_and_measure_are_blocked_before_any_key_or_exec(tmp_path,
                                                                         monkeypatch, capsys):
    """Planner: no real credential may enter a subject until containment is proven."""
    home = prepare_home(tmp_path)
    (home / '.hermes').mkdir()
    (home / '.hermes' / '.env').write_text('OLLAMA_API_KEY=R4-REAL-LOOKING-CANARY\n')
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    out = tmp_path / 'out'
    entry = fake_argv(home)

    def forbidden(*args, **kwargs):
        raise AssertionError('a blocked live run must not even load the runner')

    monkeypatch.setattr(mod, 'load_hermes', forbidden)
    for argv in (['run', '--live', '--out-root', str(out), '--run-id', 'live',
                  '--conditions', 'baseline', '--cases', 'control-trivial',
                  '--trials', '1', '--hermes', entry],
                 ['run', '--live', '--pilot', '--out-root', str(out), '--run-id', 'live',
                  '--trials', '1', '--hermes', entry],
                 ['measure', '--live', '--out-root', str(out), '--run-id', 'live',
                  '--trials', '1', '--hermes', entry]):
        assert mod.main(argv) == 3, argv
        assert 'blocked' in capsys.readouterr().err.lower()
    assert not (out / 'live').exists(), 'a blocked live run created a run directory'
    assert not (home / 'fake_calls.jsonl').exists(), 'a blocked live run started the subject'


def test_pilot_refuses_pin_drift_and_keeps_the_old_outcomes(tmp_path, monkeypatch):
    """R4 MAJOR: model A's outcomes must not be relabelled as model B's."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete')
    mod = load('run_evals')
    out = tmp_path / 'out'
    common = ['run', '--pilot', '--out-root', str(out), '--run-id', 'p',
              '--trials', '1', '--parallel', '1', '--max-attempts', '1',
              '--hermes', fake_argv(home)] + offline(home)
    assert mod.main(common + ['--model', 'model-A']) == 0
    first = json.loads((out / 'p' / 'pilot.json').read_text())
    # the pilot really ran under bwrap, with no network and the neutral home
    for cond_dir in (out / 'p' / 'pilot' / 'runs' / 'baseline').iterdir():
        cmd = json.loads((cond_dir / 't1' / 'cmd.json').read_text())
        assert cmd['wrapped'] is True and Path(cmd['argv'][0]).name == 'bwrap', cmd
        assert '--unshare-net' in cmd['argv']
    chats = json.loads((home / '.fake_hermes_counter.json').read_text())['chat']
    assert mod.main(common + ['--model', 'model-B']) == 3
    assert json.loads((home / '.fake_hermes_counter.json').read_text())['chat'] == chats
    second = json.loads((out / 'p' / 'pilot.json').read_text())
    assert second['pin_digest'] == first['pin_digest'], 'model B relabelled model A'
    assert second['outcomes'] == first['outcomes']
    header = read_jsonl(out / 'p' / 'pilot' / 'ledger.jsonl')[0]
    assert header['model'] == 'model-A'
    # positive control: the same pins resume without a single new subject call
    assert mod.main(common + ['--model', 'model-A']) == 0
    assert json.loads((home / '.fake_hermes_counter.json').read_text())['chat'] == chats


def test_a_broken_or_missing_bwrap_fails_closed_for_the_offline_double(tmp_path, monkeypatch):
    """R4: no bwrap means no execution — never an unwrapped fallback."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    runner = mod.load_hermes()
    out = tmp_path / 'out'
    configure(home, writes=[{'path': str(tmp_path / 'outside.txt'), 'content': 'x'}])

    def attempt(run_id, bwrap):
        monkeypatch.setattr(runner, 'BWRAP', str(bwrap))
        return mod.main(['run', '--out-root', str(out), '--run-id', run_id,
                         '--conditions', 'baseline', '--cases', 'control-trivial',
                         '--trials', '1', '--max-attempts', '1', '--parallel', '1',
                         '--hermes', fake_argv(home)] + offline(home))

    # a) missing binary
    assert attempt('missing', tmp_path / 'no-such-bwrap') == 3
    # b) a broken binary that exits non-zero
    broken = tmp_path / 'broken-bwrap'
    broken.write_text('#!/bin/sh\necho "bwrap: not allowed" >&2\nexit 1\n')
    broken.chmod(0o755)
    assert attempt('broken', broken) == 3
    for run_id in ('missing', 'broken'):
        rundir = out / run_id / 'runs' / 'baseline' / 'control-trivial' / 't1'
        meta = json.loads((rundir / 'meta.json').read_text())
        assert meta['status'] == 'preflight_fail', meta
        assert not (rundir / 'cmd.json').exists(), 'the subject ran without bwrap'
        events = rundir / 'events.jsonl'
        assert not events.exists() or not events.read_text().strip(), \
            'a subject stream exists without bwrap'
    assert not (tmp_path / 'outside.txt').exists()
    assert not (home / 'fake_calls.jsonl').exists(), 'a subject started anywhere'


def test_cleanup_reaps_pilot_and_probe_sandboxes(tmp_path, monkeypatch):
    """R4 MAJOR: probe and pilot sandboxes are owned and cleaned by --cleanup."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    mod = load('run_evals')
    sandbox_root = tmp_path / 'sandboxes'
    sandbox_root.mkdir()
    monkeypatch.setattr(mod, 'SUBJECT_ROOT', sandbox_root)
    monkeypatch.setattr(mod.load_hermes(), 'SUBJECT_ROOT', sandbox_root)
    out = tmp_path / 'out'
    root = out / 'c'
    pilot_dir = sandbox_root / ('a' * 20)
    probe_dir = sandbox_root / ('b' * 20)
    for path in (pilot_dir, probe_dir):
        (path / 'home' / '.hermes').mkdir(parents=True)
        (path / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=CANARY-LEFTOVER\n')
    for ledger_path, execdir in ((root / 'pilot' / 'ledger.jsonl', pilot_dir),
                                 (root / 'probe' / 'ledger.jsonl', probe_dir)):
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = mod.load_hermes().Ledger(ledger_path)
        ledger.append({'event': 'header', 'run_id': 'c', 'trials': 1,
                       'conditions': {'baseline': {}}})
        ledger.append({'event': 'attempt_dir', 'key': 'baseline/x/t1', 'attempt': 1,
                       'profile': 'lheval0123456789abcdef',
                       'execdir': str(execdir)})
    assert mod.main(['run', '--cleanup', '--out-root', str(out), '--run-id', 'c',
                     '--hermes', fake_argv(home)]) == 0
    assert not pilot_dir.exists() and not probe_dir.exists()
    assert mod.main(['run', '--cleanup', '--out-root', str(out), '--run-id', 'c',
                     '--hermes', fake_argv(home)]) == 0, 'cleanup is not idempotent'


# --------------------------------------------------------------------------- #
# R6: one owned offline boundary, ported from the four R4 eval canaries
# --------------------------------------------------------------------------- #

def require_bwrap():
    """bwrap is a hard prerequisite of this harness, never a silent skip."""
    assert BWRAP.is_file(), 'bwrap is missing: the real-sandbox E2E cannot be skipped'
    assert Path('/usr/bin/python3').is_file(), '/usr/bin/python3 is required'


def ensure_double(home):
    """Create the byte-identical double copy under an allowlisted home root."""
    fake_argv(home)
    return _entry(home)


def offline_args(out, run_id, entry, state, cases='control-trivial', cond='baseline'):
    return ['run', '--out-root', str(out), '--run-id', run_id, '--conditions', cond,
            '--cases', cases, '--trials', '1', '--max-attempts', '1', '--parallel', '1',
            '--hermes', entry, '--offline-state', str(state)]


def test_offline_adapter_refuses_every_non_canonical_entry(tmp_path, monkeypatch):
    """R4 canary 1: only a byte-identical verified double may execute offline.

    A refused entry must leave no run directory, no subject process and no host
    write behind; the two canonical forms (direct entry and the historical
    ``python <verified-double>`` launcher) must both run inside real bwrap with
    ``--unshare-net`` and a child that reports the sandbox HOME and a bwrap PID 1.
    """
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    entry = ensure_double(home)
    hostfile = tmp_path / 'outside-write.txt'
    configure(home, writes=[{'path': str(hostfile), 'content': 'host-write'}])

    rogue = tmp_path / 'rogue.py'
    rogue.write_text(Path(entry).read_text(encoding='utf-8') + '\n# edited\n',
                     encoding='utf-8')
    rogue.chmod(0o755)
    body = FAKE.read_bytes().split(b'\n', 1)[1]
    forged = tmp_path / 'forged.py'
    forged.write_bytes(b'#!/bin/sh\n' + body)
    forged.chmod(0o755)
    elf = tmp_path / 'python3'
    shutil.copyfile('/usr/bin/true', elf)
    elf.chmod(0o755)
    link = tmp_path / 'link.py'
    link.symlink_to(entry)
    keyed = tmp_path / 'keyed-state'
    (keyed / '.hermes').mkdir(parents=True)
    (keyed / '.hermes' / '.env').write_text('OLLAMA_API_KEY=CANARY-R6-KEY-4242\n',
                                           encoding='utf-8')

    def refused(entry_argv, state_dir=state):
        assert mod.main(offline_args(out, 'refused', entry_argv, state_dir)) == 2, entry_argv
        assert not (out / 'refused').exists(), 'a refused entry created a run directory'
        assert not hostfile.exists(), 'a refused entry wrote to the host'
        assert not (home / 'fake_calls.jsonl').exists(), 'a refused entry executed'

    refused(str(rogue))                                     # one edited byte
    refused('/bin/sh')                                      # arbitrary binary
    refused('%s %s' % (sys.executable, str(rogue)))          # python + unverified script
    refused('%s --rogue-flag' % entry)                      # argv smuggled onto the double
    refused(str(forged))                                    # a shell shebang, same body
    refused('%s %s' % (sys.executable, str(forged)))         # ... also via python
    refused('%s %s' % (str(elf), entry))                     # a python3-named temp ELF
    refused(str(link))                                      # a symlinked entry
    refused(entry, state_dir='/home')                       # a top-level system dir
    refused(entry, state_dir=keyed)                         # a state that holds a key

    # positive controls: both canonical forms run, confined by the runner plan
    assert mod.main(offline_args(out, 'direct', entry, home)) == 0
    assert mod.main(offline_args(out, 'launcher',
                                 '%s %s' % (sys.executable, entry), home)) == 0
    for run_id in ('direct', 'launcher'):
        rundir = out / run_id / 'runs' / 'baseline' / 'control-trivial' / 't1'
        cmd = json.loads((rundir / 'cmd.json').read_text())
        assert cmd['wrapped'] is True and Path(cmd['argv'][0]).name == 'bwrap', cmd['argv'][:2]
        assert '--unshare-net' in cmd['argv'], 'the offline plan did not unshare the network'
        # R7: the executed entry is the verified attempt copy, never the input path
        inner = cmd['inner']
        assert inner[0] == os.path.realpath(sys.executable), inner[0]
        assert Path(inner[1]).name == 'subject.py' and inner[1] != entry, inner
        assert not Path(inner[1]).exists(), 'the attempt copy outlived the run'
        assert cmd['double_digest'] == mod.load_hermes().repo_double_digest()
        meta = json.loads((rundir / 'meta.json').read_text())
        assert meta['status'] == 'ok', meta
        isolation = ISOLATION.search((rundir / 'stderr.txt').read_text())
        assert isolation, 'the double never reported what its child could see'
        assert isolation.group(1).startswith(str(mod.SUBJECT_ROOT) + '/'), isolation.group(1)
        assert isolation.group(1) != str(home), 'the subject ran with the harness HOME'
        assert 'bwrap' in isolation.group(4), isolation.group(4)[:80]
    assert not hostfile.exists(), 'the confined subject wrote outside its sandbox'


def test_offline_state_must_be_key_free(tmp_path, monkeypatch):
    """R6: a state with ``.hermes/.env`` or a provider-key pattern never starts a process."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    entry = ensure_double(home)
    configure(home, mode='ok', writes=[{'path': 'out.txt', 'content': 'ok'}])

    for name, body in (('provider.env', 'OPENAI_API_KEY=sk-live-9f8e7d6c5b4a39281706\n'),
                       ('notes.txt', 'export ANTHROPIC_API_KEY="sk-ant-api03-AAAABBBBCCCC"\n'),
                       ('paste.txt', 'leaked sk-ant-api03-ZZZZYYYYXXXXWWWWVVVV\n')):
        dirty = tmp_path / ('dirty-' + name.split('.')[0])
        dirty.mkdir()
        (dirty / name).write_text(body, encoding='utf-8')
        assert mod.main(offline_args(out, 'dirty', entry, dirty)) == 2, name
        assert not (home / 'fake_calls.jsonl').exists(), 'a keyed state executed'

    # positive control: a clean state with the double's configured write runs
    assert mod.main(offline_args(out, 'clean', fake_argv(home), home)) == 0
    rundir = out / 'clean' / 'runs' / 'baseline' / 'control-trivial' / 't1'
    assert (rundir / 'work' / 'out.txt').read_text() == 'ok', 'the positive control did not run'


def test_offline_attempt_never_holds_a_key(tmp_path, monkeypatch):
    """R6: the harness writes no key into a sandbox, not even a kept one."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    sandbox_root = tmp_path / 'sandboxes'
    sandbox_root.mkdir()
    monkeypatch.setattr(mod, 'SUBJECT_ROOT', sandbox_root)
    monkeypatch.setattr(mod.load_hermes(), 'SUBJECT_ROOT', sandbox_root)
    out = tmp_path / 'out'
    entry = ensure_double(home)
    configure(home, mode='ok', reply='done\nFINAL: complete')
    execdirs = []
    try:
        assert mod.main(offline_args(out, 'kp', entry, home) + ['--keep-profiles']) == 0
        execdirs = [Path(row['execdir']) for row in ledger_rows(out, 'kp')
                    if row.get('event') == 'attempt_dir']
        assert execdirs and all(path.is_dir() for path in execdirs), execdirs
        kept = execdirs[0] / 'home'
        assert not (kept / '.hermes' / '.env').exists(), \
            'the harness wrote a key into the sandbox'
        for path in kept.rglob('*'):
            if path.is_file():
                assert 'CANARY-R6-KEY' not in path.read_text(encoding='utf-8',
                                                             errors='replace')
    finally:
        # R7: this test owns its sandboxes; the kept profile is reaped through
        # the runner's own cleanup, never by a wildcard sweep.
        assert mod.main(['run', '--cleanup', '--out-root', str(out), '--run-id', 'kp',
                         '--hermes', entry]) == 0
    assert execdirs, 'the test did not record its attempt'
    for path in execdirs:
        assert not path.exists(), 'the test leaked its own sandbox: %s' % path


def test_pilot_rejects_suite_drift_before_reuse(tmp_path, monkeypatch):
    """R6: a changed case suite must not be relabelled with the old outcomes."""
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    configure(home, mode='ok', reply='Done.\nFINAL: complete')
    mod = load('run_evals')
    evals_copy = tmp_path / 'evals'
    shutil.copytree(EVALS, evals_copy, ignore=shutil.ignore_patterns('__pycache__'))
    monkeypatch.setattr(mod, 'EVALS_DIR', evals_copy)
    out = tmp_path / 'out'
    common = ['run', '--pilot', '--out-root', str(out), '--run-id', 'p',
              '--trials', '1', '--parallel', '1', '--max-attempts', '1',
              '--hermes', fake_argv(home), '--offline-state', str(home)]
    assert mod.main(list(common)) == 0
    first = json.loads((out / 'p' / 'pilot.json').read_text())
    with (evals_copy / 'cases.jsonl').open('a', encoding='utf-8') as stream:
        stream.write('\n')
    assert mod.main(list(common)) == 3, 'a changed suite was accepted'
    second = json.loads((out / 'p' / 'pilot.json').read_text())
    assert second['suite_fingerprint'] == first['suite_fingerprint'], 'the suite pin was relabelled'
    assert second['outcomes'] == first['outcomes']


def test_docs_state_the_boundary_and_its_remaining_limits():
    """The written limits are part of the boundary: live stays off, and why."""
    results = (EVALS / 'RESULTS.md').read_text(encoding='utf-8').lower()
    readme = (REPO / 'README.md').read_text(encoding='utf-8').lower()
    changelog = (REPO / 'CHANGELOG.md').read_text(encoding='utf-8').lower()
    for needle in ('no runner api reads', 'unshare-net', '/proc', 'abstract socket',
                   'pdeathsig', 'not a containment proof', 'credential broker',
                   'control-trivial', 'control-finish', 'legacy ledger',
                   # R7
                   'longhaul_eval_test_seam', '--live --probe', 'measure',
                   'base64', 'too large', 'canary',
                   # R8: token, attempt-owned log, dbus mask, honest limits
                   'verifieddouble', 'attempt-owned', '/run/dbus', 'max_tokens',
                   'mountinfo', 'traverse', 'no host socket is reachable'):
        assert needle in results, 'RESULTS.md does not name %r' % needle
    for needle in ('live', 'blocked', 'credential'):
        assert needle in readme, 'README.md does not name %r' % needle
    for needle in ('live evaluation', 'blocked', 'no key'):
        assert needle in changelog, 'CHANGELOG.md does not name %r' % needle


# --------------------------------------------------------------------------- #
# R7: the judge is a sandboxed subject route; the CLI never spawns
# --------------------------------------------------------------------------- #

def judge_args(out, run_id, entry, state, extra=()):
    return ['measure', '--out-root', str(out), '--run-id', run_id, '--trials', '1',
            '--hermes', entry, '--offline-state', str(state)] + list(extra)


def test_measure_refuses_a_non_double_and_sandboxes_its_judge(tmp_path, monkeypatch):
    """R7 auditor mutants X1/X3: ``measure`` is a subject route with the same gate.

    The judge runs through the runner-owned plan: the child reports the sandbox
    HOME and a bwrap PID 1, the harness HOME stays empty, and a non-double entry
    is refused before any process -- with and without ``--no-judge``.
    """
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    hermes = mod.load_hermes()
    sandbox_root = tmp_path / 'sandboxes'
    sandbox_root.mkdir()
    monkeypatch.setattr(mod, 'SUBJECT_ROOT', sandbox_root)
    monkeypatch.setattr(hermes, 'SUBJECT_ROOT', sandbox_root)
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    configure(state, mode='ok', reply='OK. 3.14.2\nFINAL: complete')
    entry = '%s --fake-home %s' % (ensure_double(home), state)

    assert mod.main(offline_args(out, 'm', entry, state)) == 0, 'the matrix did not run'
    assert mod.main(judge_args(out, 'm', entry, state)) == 0, 'measure refused'
    # R8: the judge's call log is attempt-owned; neither the harness HOME nor the
    # bound state may collect it.
    assert not (home / 'fake_calls.jsonl').exists(), 'the judge wrote into the harness HOME'
    assert not (home / '.fake_hermes_counter.json').exists(), \
        'the judge incremented a counter in the harness HOME'
    assert not (home / '.hermes' / 'profiles').exists(), 'a profile appeared in the HOME'
    assert not (state / 'fake_calls.jsonl').exists(), \
        'the judge wrote its call log into the bound state'
    assert list(sandbox_root.iterdir()) == [], 'the judge sandbox was not cleaned up'

    rogue = tmp_path / 'rogue.py'
    rogue.write_text('print("rogue")\n', encoding='utf-8')
    body = FAKE.read_bytes().split(b'\n', 1)[1]
    forged = tmp_path / 'forged.py'
    forged.write_bytes(b'#!/bin/sh\n' + body)
    forged.chmod(0o755)
    elf = tmp_path / 'python3'
    shutil.copyfile('/usr/bin/true', elf)
    elf.chmod(0o755)
    for name, entry_argv in (('rogue', str(rogue)), ('forged', str(forged)),
                             ('forged-python', '%s %s' % (sys.executable, forged)),
                             ('elf', '%s %s' % (str(elf), ensure_double(home)))):
        for extra in ((), ('--no-judge',)):
            before = len(read_jsonl(state / 'fake_calls.jsonl'))
            assert mod.main(judge_args(out, 'rogue', entry_argv, state, extra)) == 2, \
                (name, extra)
            assert not (out / 'rogue').exists(), 'a refused measure created a run directory'
            assert len(read_jsonl(state / 'fake_calls.jsonl')) == before, \
                'a refused measure (%s) started a process' % name


def test_weird_entry_paths_cannot_inject_a_command(tmp_path, monkeypatch):
    """R7: spaces, unicode and ``;$(...)`` in the entry path stay literal."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    configure(state, mode='ok', reply='3.14.2\nFINAL: complete')
    marker = 'PWNED'
    bindir = home / '.local' / 'bin'
    bindir.mkdir(parents=True)
    body = FAKE.read_bytes().split(b'\n', 1)[1]
    for name in ('with space.py', 'ünïcode-\u5165\u53e3.py', 'semi;$(touch %s).py' % marker):
        target = bindir / name
        target.write_bytes(b'#!/usr/bin/env python3\n' + body)
        target.chmod(0o755)
        quoted = shlex.join([str(target), '--fake-home', str(state)])
        assert mod.main(offline_args(out, 'w', quoted, state)) == 0, name
        cmd = json.loads((out / 'w' / 'runs' / 'baseline' / 'control-trivial'
                          / 't1' / 'cmd.json').read_text())
        assert Path(cmd['inner'][1]).name == 'subject.py', cmd['inner']
        assert mod.main(judge_args(out, 'w', quoted, state)) == 0, name
    files = [path for path in tmp_path.rglob(marker) if path.is_file()]
    assert not files, 'an entry path was interpreted by a shell: %r' % (files,)
    assert (bindir / ('semi;$(touch %s).py' % marker)).is_file(), \
        'the literal path was rewritten'


def test_probe_records_three_canary_declarations(tmp_path, monkeypatch):
    """R7/R8: probe.json carries the three canaries as values, and the gate needs them."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    entry = ensure_double(home)
    assert mod.main(['run', '--probe', '--out-root', str(out), '--run-id', 'pr',
                     '--hermes', entry, '--offline-state', str(home)]) == 0
    doc = json.loads((out / 'pr' / 'probe.json').read_text())
    assert doc['clean'] is True, doc['problems']
    assert set(doc['canaries']) == {'host', 'sock', 'net'}, doc['canaries']
    assert all(doc['canaries'][key] for key in ('host', 'sock', 'net')), doc['canaries']
    for label in ('baseline', 'candidate'):
        record = doc[label]
        assert set(record['canaries']) == {'host', 'sock', 'net'}, record['canaries']
        assert all(record['canaries'][key] for key in ('host', 'sock', 'net'))
        assert record['facts']['home'].startswith(str(mod.SUBJECT_ROOT) + '/')
    # R8c: a clean hand-built record must gate (positive control), and every
    # broken canary form must fail *on the canaries* -- not on an unrelated pin.
    clean = {'schema': 1, 'mode': 'live', 'clean': True, 'pin_digest': 'p',
             'chain_digest': 'c', 'bwrap_version': 'bwrap 0.9.0',
             'created_at': mod.now_iso(),
             'canaries': {'host': 'h', 'sock': 's', 'net': 'n'}}
    assert mod.probe_gate(clean, 'p', 'c', 'bwrap 0.9.0') == [], \
        'a clean probe record did not gate'
    broken = {
        'none-values': {'host': None, 'sock': None, 'net': None},
        'empty-dict': {},
        'one-missing': {'host': 'h', 'sock': 's'},
        'falsy-values': {'host': '', 'sock': False, 'net': 0},
    }
    for label, canaries in broken.items():
        problems = mod.probe_gate(dict(clean, canaries=canaries), 'p', 'c', 'bwrap 0.9.0')
        assert any('canar' in item.lower() for item in problems), (label, problems)
    # The real doc is an offline probe: it must fail on its mode, and it must
    # fail on the canaries too, but the canaries are what this test owns.
    assert doc['canaries'] and all(doc['canaries'].values())


def test_run_and_measure_build_the_header_from_the_gate_token(tmp_path, monkeypatch):
    """R8b MINOR 1: the CLI pins the token's bytes, never the caller path again."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    hermes = mod.load_hermes()
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    configure(state, mode='ok', reply='OK. 3.14.2\nFINAL: complete')
    entry = '%s --fake-home %s' % (ensure_double(home), state)
    seen = []
    real = mod.build_header

    def spy(args, module, conditions, token=None):
        seen.append(token)
        extra = {'token': token} if token is not None else {}
        return real(args, module, conditions, **extra)

    monkeypatch.setattr(mod, 'build_header', spy)
    assert mod.main(offline_args(out, 'pin', entry, state)) == 0
    assert mod.main(judge_args(out, 'pin', entry, state)) == 0
    assert seen, 'the header was never built'
    assert all(isinstance(item, hermes.VerifiedDouble) for item in seen), \
        'a header was built without the gate token: %r' % (seen,)


def test_two_full_offline_runs_on_one_fake_home_state(tmp_path, monkeypatch):
    """R8 MAJOR: the double's growing log must not poison the bound state.

    Two complete matrices (all cases, all conditions, two trials, three workers)
    on the same ``--fake-home`` state must both succeed; the call log lives in
    the attempt, not in the bound state.
    """
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    configure(state, mode='ok', reply='Done.\nFINAL: complete')
    entry = '%s --fake-home %s' % (ensure_double(home), state)
    base = ['run', '--trials', '2', '--parallel', '3', '--hermes', entry,
            '--offline-state', str(state)]
    first = mod.main(base + ['--out-root', str(out), '--run-id', 'a'])
    assert first == 0, 'the first full offline run failed'
    second = mod.main(base + ['--out-root', str(out), '--run-id', 'b'])
    assert second == 0, 'the second full offline run failed'
    assert not (state / 'fake_calls.jsonl').exists(), \
        'the double wrote its log into the bound state'
    log = out / 'b' / 'runs' / 'baseline' / 'control-trivial' / 't1' / 'fake-log.jsonl'
    assert log.is_file(), 'the attempt did not keep its own call log'


def test_measure_reports_a_judge_setup_refusal(tmp_path, monkeypatch, capsys):
    """R8 J1: a sandbox-refused judge is rc 2 with a reason, never silent rows."""
    require_bwrap()
    home = prepare_home(tmp_path)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    mod = load('run_evals')
    hermes = mod.load_hermes()
    out = tmp_path / 'out'
    state = tmp_path / 'state'
    state.mkdir()
    configure(state, mode='ok', reply='3.14.2\nFINAL: complete')
    entry = '%s --fake-home %s' % (ensure_double(home), state)
    assert mod.main(offline_args(out, 'j', entry, state)) == 0
    monkeypatch.setattr(hermes, 'BWRAP', str(tmp_path / 'no-such-bwrap'))
    capsys.readouterr()
    assert mod.main(judge_args(out, 'j', entry, state)) == 2, 'a refused judge exited 0'
    captured = capsys.readouterr()
    assert 'judge' in captured.err.lower() and 'bwrap' in captured.err.lower(), \
        captured.err[-400:]


