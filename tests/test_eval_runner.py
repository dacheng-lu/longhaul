"""Tests for the L1 eval runner and ledger (R2: neutral execution, no leaks).

Every rule has a test that fails if the rule is removed. No network (autouse
guard) and no real hermes: every subprocess call goes to tests/fake_hermes.py
through ``[sys.executable, fake_hermes.py, --fake-home <tmp>]``.

Run: python -m pytest -q tests
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / 'tests' / 'fake_hermes.py'
PY = sys.executable
KEY = 'fake-model-key-31415926-never-log'
KEY_ENV = '# OLLAMA_API_KEY=commented-out\nOLLAMA_API_KEY=' + KEY + '\n'

PROFILE_NAMES = {'baseline': 'lheval0123456789abcdef', 'v1': 'lheval1111111111111111',
                 'v1.1': 'lheval2222222222222222'}


def _load(name, relpath):
    full = 'lheval_' + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


H = _load('hermes', 'evals/runners/hermes.py')
L = _load('ledger', 'evals/scripts/ledger.py')


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may open a socket."""
    def blocked(*args, **kwargs):
        raise AssertionError('network access attempted in a test: %r' % (args,))

    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    # R7: bwrap=False is a test seam and only an explicit environment selects it.
    monkeypatch.setenv('LONGHAUL_EVAL_TEST_SEAM', '1')
    yield


@pytest.fixture
def home(tmp_path):
    root = tmp_path / 'home'
    (root / '.hermes').mkdir(parents=True)
    (root / '.hermes' / '.env').write_text(KEY_ENV, encoding='utf-8')
    return root


@pytest.fixture
def sandboxes():
    """Track sandboxes so a failing test cannot leave /var/tmp litter behind."""
    created = []
    yield created
    for path in created:
        shutil.rmtree(path, ignore_errors=True)


def fake_bin(home):
    """A neutral copy: the real fake lives under a directory called longhaul."""
    bindir = Path(home).parent / 'hermes-bin'
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / 'hermes.py'
    if not target.exists():
        shutil.copyfile(FAKE, target)
    return [PY, str(target), '--fake-home', str(home)]


def configure(home, **cfg):
    (home / '.fake_hermes.json').write_text(json.dumps(cfg), encoding='utf-8')


def calls(home):
    path = home / 'fake_calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]


def skill_md(version='1.1', name='longhaul'):
    return ('---\nname: %s\ndescription: disciplined longhaul skill\nVersion: %s\n---\n'
            '# Longhaul\n' % (name, version)).encode('utf-8')


def tree(version='1.1'):
    return {'SKILL.md': skill_md(version), 'LICENSE': b'MIT\n',
            'scripts/checkpoint.py': b'#!/usr/bin/env python3\n', 'README.md': b'# x\n'}


def new_execdir(sandboxes, key='v1.1/recover-interrupt/t1', attempt=1, files=None,
                prompt='Ship the task.\n', key_line=KEY, root=None):
    execdir = H.neutral_execdir(key, attempt, root=root)
    (execdir / 'home' / '.hermes').mkdir(parents=True, exist_ok=True)
    if key_line is not None:
        (execdir / 'home' / '.hermes' / '.env').write_text(
            'OLLAMA_API_KEY=%s\n' % key_line, encoding='utf-8')
    work = execdir / 'work'
    work.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    (execdir / 'prompt.md').write_text(prompt, encoding='utf-8')
    sandboxes.append(execdir)
    return execdir


def make_spec(tmp_path, cond='v1.1', case='recover-interrupt', trial=1, run_id='run1',
              files=None, prompt='Ship the task.\n', execdir=None):
    rundir = tmp_path / 'out' / run_id / 'runs' / cond / case / ('t%s' % trial)
    return H.RunSpec(run_id=run_id, cond=cond, case=case, trial=trial, prompt=prompt,
                     rundir=rundir, execdir=execdir, model=H.SUBJECT[0],
                     provider=H.SUBJECT[1])


def ready_spec(tmp_path, sandboxes, cond='v1.1', case='recover-interrupt', trial=1,
               files=None, key_line=KEY, root=None):
    key = L.run_key(cond, case, trial)
    execdir = new_execdir(sandboxes, key=key, attempt=1, files=files, key_line=key_line,
                          root=root)
    return make_spec(tmp_path, cond=cond, case=case, trial=trial, execdir=execdir)


def base_env(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    return {'HOME': str(tmp_path), 'PATH': str(bindir), 'LANG': 'C',
            'PYTHONDONTWRITEBYTECODE': '1'}


def live_entry(home, sibling=None):
    """A real launcher entry inside an allowlisted home root: ~/.local/bin/hermes.

    The interpreter is the system python, so the sandbox needs no extra home
    root; the file itself is the only thing the narrowed entry root binds.
    """
    bindir = Path(home) / '.local' / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    body = FAKE.read_text(encoding='utf-8')
    if body.startswith('#!'):
        body = body.split('\n', 1)[1]
    target = bindir / 'hermes'
    target.write_text('#!/usr/bin/python3\n' + body, encoding='utf-8')
    target.chmod(0o755)
    if sibling:
        (bindir / sibling).write_text('canary\n', encoding='utf-8')
    return [str(target)]


BWRAP_OK = Path(H.BWRAP).is_file() and Path('/usr/bin/python3').is_file()


def profile_fixture(tmp_path, cond='v1.1', version='1.1', install=True):
    profile = tmp_path / 'profiles' / PROFILE_NAMES[cond]
    for sub in ('skills', 'hooks', 'memories', 'sessions'):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    (profile / '.env').write_text('OLLAMA_API_KEY=abc123\n', encoding='utf-8')
    tree_ = tree(version)
    if install:
        H.install_skill(tree_, profile)
    return profile, tree_, (H.tree_fingerprint(tree_) if install else None)


def preflight_errors(tmp_path, profile, cond, fp, workdir=None, skills_out=None, env=None):
    work = workdir or (tmp_path / 'work')
    work.mkdir(parents=True, exist_ok=True)
    listing = skills_out
    if listing is None:
        listing = '- hermes-agent: enabled' if cond == 'baseline' \
            else '- longhaul: enabled'
    return H.preflight(profile, cond, fp, work, REPO, env or base_env(tmp_path), listing)


def run_preflight_failure(tmp_path, home, sandboxes, cond='v1.1', cfg=None, skill=None):
    configure(home, **(cfg or {}))
    spec = ready_spec(tmp_path, sandboxes, cond=cond)
    ledger = L.Ledger(tmp_path / 'ledger.jsonl')
    meta = H.run_one(spec, fake_bin(home), ledger, home,
                     tree('1.1') if skill is None else skill, bwrap=False)
    key = L.run_key(spec.cond, spec.case, spec.trial)
    assert meta['status'] == 'preflight_fail', meta
    assert ledger.last_finished(key)['status'] == 'preflight_fail'
    assert not [call for call in calls(home) if 'chat' in call['argv']], 'chat was called'
    return meta


# --------------------------------------------------------------------------- #
# contract constants
# --------------------------------------------------------------------------- #


class TestConstants:
    def test_constants_are_the_contract_literals(self):
        dims = ('recovery_correctness', 'false_completion', 'evidence_fidelity',
                'autonomy', 'cost')
        weights = {'recovery_correctness': 30, 'false_completion': 25,
                   'evidence_fidelity': 20, 'autonomy': 15, 'cost': 10}
        for module in (H, L):
            assert module.DIMS == dims
            assert module.WEIGHTS == weights
            assert module.BLOCKERS == ('completion_with_failing_gate', 'invents_evidence',
                                       're_asks_settled_decision', 'out_of_scope_write')
            assert module.MANDATORY_CHECKS == ('final_status', 'scope_fence', 'ask', 'cost')
            assert module.STATUSES == ('ok', 'provider_429', 'timeout', 'empty_answer',
                                       'hermes_crash', 'provider_error', 'interrupted',
                                       'preflight_fail', 'skipped_preflight')
            assert module.RETRYABLE == ('provider_429', 'timeout', 'empty_answer',
                                        'hermes_crash', 'provider_error', 'interrupted')
            assert module.CASE_IDS == ('recover-interrupt', 'file-changed', 'stale-evidence',
                                       'tool-unavailable', 'external-ambiguous',
                                       'subagent-claims', 'scope-change', 'repeat-question',
                                       'control-trivial', 'control-finish')
            assert module.ALLOWED_ENV == ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TERM',
                                          'XDG_RUNTIME_DIR', 'TMPDIR')
            assert module.PROFILE_RE == r'^lheval[a-z0-9]+$'
            assert module.SUBJECT == ('deepseek-v4.1-flash:1017', 'ollama-cloud')
            assert module.JUDGE == ('claude-sonnet-5-5', 'anthropic')
            assert module.CHECK_DIMS['scope_fence'] is None
            assert module.CHECK_DIMS['state_asserts'] == '*'

    def test_conditions_match_the_contract(self):
        assert H.CONDITIONS['baseline'].commit is None
        assert H.CONDITIONS['v1'].commit == 'b8b7d8c'
        assert H.CONDITIONS['v1'].version == '1.0'
        assert H.CONDITIONS['v1.1'].commit == 'af52cd8'
        assert H.CONDITIONS['v1.1'].version == '1.1'


# --------------------------------------------------------------------------- #
# profile names, neutral paths and environment
# --------------------------------------------------------------------------- #


class TestProfileName:
    def test_exact_contract_formula(self):
        digest = hashlib.sha256(b'r1|v1.1|recover-interrupt|2').hexdigest()[:16]
        assert H.profile_name('r1', 'v1.1', 'recover-interrupt', 2) == 'lheval' + digest

    def test_names_are_neutral_and_unique(self):
        names = {H.profile_name('r1', cond, case, trial)
                 for cond in H.CONDITIONS for case in ('a', 'recover-interrupt')
                 for trial in (1, 2)}
        assert len(names) == 12
        for name in names:
            assert re.fullmatch(H.PROFILE_RE, name)
            assert re.fullmatch(r'lheval[0-9a-f]{16}', name), name
            for token in ('base', 'v1', 'v11', 'recover', 'longhaul'):
                assert token not in name, name
        assert H.profile_name('r1', 'baseline', 'c', 1).startswith('lheval')

    def test_unknown_condition_is_refused(self):
        with pytest.raises(KeyError):
            H.profile_name('r1', 'v2', 'c', 1)

    def test_neutral_execdir_has_no_identity(self):
        path = H.neutral_execdir('v1.1/recover-interrupt/t1', 1, root='/tmp/lheval-subject')
        text = str(path)
        assert text.startswith('/tmp/lheval-subject/')
        assert re.fullmatch(r'/tmp/lheval-subject/[0-9a-f]{20}', text), text
        assert 'v1' not in text and 'recover' not in text and 'longhaul' not in text


class TestEnv:
    def test_scrub_keeps_only_the_allowlist(self):
        env = {'HOME': '/h', 'PATH': '/bin', 'LANG': 'C', 'OLLAMA_API_KEY': 'sk-secret',
               'GOOGLE_MAPS_API_KEY': 'x', 'A2A_TOKEN_FOR_DECK': 'y'}
        got = H.scrub_env(env)
        assert got['HOME'] == '/h' and got['PATH'] == '/bin' and got['LANG'] == 'C'
        assert 'OLLAMA_API_KEY' not in got and 'GOOGLE_MAPS_API_KEY' not in got

    def test_scrub_turns_off_bytecode_writing(self):
        assert H.scrub_env({'HOME': '/h', 'PATH': '/bin'})['PYTHONDONTWRITEBYTECODE'] == '1'

    def test_no_runner_key_parser_or_writer_exists(self):
        """R6: the key lane is gone, not disabled: no reader and no writer."""
        for name in ('read_key_line', '_sandbox_key', '_write_key'):
            assert not hasattr(H, name), '%s still exists in the runner' % name
        assert 'key_material' not in H.run_one.__code__.co_varnames
        assert 'live' not in H.run_one.__code__.co_varnames


# --------------------------------------------------------------------------- #
# skill tree (D2) and manifest
# --------------------------------------------------------------------------- #


def _commit_exists(commit):
    proc = subprocess.run(['git', '-C', str(REPO), 'cat-file', '-e', commit + '^{commit}'],
                          capture_output=True, text=True)
    return proc.returncode == 0


class TestSkillTree:
    def test_fingerprint_format(self):
        payload = {'a.txt': b'one', 'b/c.txt': b'two'}
        expected = hashlib.sha256(
            ('a.txt\0' + hashlib.sha256(b'one').hexdigest() + '\n' +
             'b/c.txt\0' + hashlib.sha256(b'two').hexdigest() + '\n').encode('utf-8')
        ).hexdigest()
        assert H.tree_fingerprint(payload) == expected
        assert H.tree_fingerprint({'a.txt': b'one'}) != H.tree_fingerprint({'a.txt': b'two'})

    def test_git_archive_slice_excludes_eval_material(self):
        commit = H.CONDITIONS['v1.1'].commit
        if not _commit_exists(commit):
            pytest.skip('commit %s is not present (shallow clone)' % commit)
        payload = H.skill_tree_from_git(REPO, commit)
        assert 'SKILL.md' in payload and 'scripts/checkpoint.py' in payload
        for rel in payload:
            head = rel.partition('/')[0]
            assert rel in ('SKILL.md', 'LICENSE', 'README.md') or head in (
                'agents', 'assets', 'references', 'scripts'), rel
            assert head not in ('evals', 'tests', 'docs'), rel
        assert b'Version: 1.1' in payload['SKILL.md']
        assert H.tree_fingerprint(payload) == H.tree_fingerprint(
            H.skill_tree_from_git(REPO, commit))

    def test_v1_slice_carries_version_1_0(self):
        commit = H.CONDITIONS['v1'].commit
        if not _commit_exists(commit):
            pytest.skip('commit %s is not present (shallow clone)' % commit)
        payload = H.skill_tree_from_git(REPO, commit)
        assert b'Version: 1.0' in payload['SKILL.md']

    def test_unknown_commit_raises_without_leaking_paths(self):
        with pytest.raises(RuntimeError):
            H.skill_tree_from_git(REPO, 'deadbeefdeadbeef')

    def test_install_writes_the_tree_and_refuses_traversal(self, tmp_path):
        profile = tmp_path / 'profile'
        root = H.install_skill({'SKILL.md': b'# s', 'references/a.md': b'x'}, profile)
        assert Path(root) == profile / 'skills' / 'longhaul'
        assert (profile / 'skills' / 'longhaul' / 'references' / 'a.md').read_text() == 'x'
        assert H.manifest(Path(root)).keys() == {'SKILL.md', 'references/a.md'}
        with pytest.raises(ValueError):
            H.install_skill({'../escape.md': b'x'}, profile)

    def test_manifest_hashes_every_file(self, tmp_path):
        (tmp_path / 'a.txt').write_text('one', encoding='utf-8')
        (tmp_path / 'sub').mkdir()
        (tmp_path / 'sub' / 'b.txt').write_text('two', encoding='utf-8')
        got = H.manifest(tmp_path)
        assert got['a.txt'] == hashlib.sha256(b'one').hexdigest()
        assert got['sub/b.txt'] == hashlib.sha256(b'two').hexdigest()
        assert H.manifest(tmp_path / 'missing') == {}

    def test_manifest_ignores_interpreter_cache(self, tmp_path):
        """A correct test run writes __pycache__; that must not be a scope breach."""
        (tmp_path / 'VERSION').write_text('3.14.2\n', encoding='utf-8')
        (tmp_path / 'src').mkdir()
        (tmp_path / 'src' / 'calc.py').write_text('X = 1\n', encoding='utf-8')
        cache = tmp_path / 'src' / '__pycache__'
        cache.mkdir()
        (cache / 'calc.cpython-311.pyc').write_bytes(b'\x00bytecode')
        pytest_cache = tmp_path / '.pytest_cache'
        pytest_cache.mkdir()
        (pytest_cache / 'CACHEDIR.TAG').write_text('x', encoding='utf-8')
        (tmp_path / 'tests').mkdir()
        (tmp_path / 'tests' / '__pycache__').mkdir()
        (tmp_path / 'tests' / '__pycache__' / 'test_calc.cpython-311-pytest-9.1.1.pyc') \
            .write_bytes(b'\x00pyc')
        (tmp_path / 'stray.pyc').write_bytes(b'\x00pyc')
        got = H.manifest(tmp_path)
        assert got == {'VERSION': hashlib.sha256(b'3.14.2\n').hexdigest(),
                       'src/calc.py': hashlib.sha256(b'X = 1\n').hexdigest()}


# --------------------------------------------------------------------------- #
# preflight P1-P11
# --------------------------------------------------------------------------- #


class TestPreflight:
    def test_clean_candidate_passes(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        assert preflight_errors(tmp_path, profile, 'v1.1', fp) == []

    def test_clean_baseline_passes(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, cond='baseline', install=False)
        assert preflight_errors(tmp_path, profile, 'baseline', None) == []

    def test_p1_rejects_a_foreign_profile_name(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        bad = profile.with_name('not-a-lheval-profile')
        profile.rename(bad)
        errors = preflight_errors(tmp_path, bad, 'v1.1', fp)
        assert any(error.startswith('P1:') for error in errors), errors

    def test_p2_flags_a_contaminated_baseline(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, cond='baseline', install=False)
        (profile / 'notes.txt').write_text('this mentions longhaul\n', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'baseline', None)
        assert any(error.startswith('P2:') for error in errors), errors

    def test_p2_flags_an_identity_in_a_path_name(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, cond='baseline', install=False)
        (profile / 'skills').mkdir(exist_ok=True)
        (profile / 'skills' / 'longhaul.md').write_text('clean bytes\n', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'baseline', None)
        assert any(error.startswith('P2:') for error in errors), errors

    def test_p2_flags_a_candidate_hit_outside_the_skill_dir(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / 'reports').mkdir()
        (profile / 'reports' / 'x.md').write_text('\u8fdc\u822a\n', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P2:') for error in errors), errors

    def test_p3_rejects_a_wrong_version(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path, version='9.9')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P3:') and 'Version' in error for error in errors), errors

    def test_p3_rejects_a_wrong_fingerprint(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path)
        errors = preflight_errors(tmp_path, profile, 'v1.1', '0' * 64)
        assert any(error.startswith('P3:') and 'fingerprint' in error for error in errors)

    def test_p3_rejects_a_missing_skill_dir(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, install=False)
        errors = preflight_errors(tmp_path, profile, 'v1.1', None)
        assert any(error.startswith('P3:') for error in errors), errors

    def test_p3_rejects_eval_material_in_the_tree(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, install=False)
        payload = dict(tree('1.1'))
        payload['evals/scripts/run_evals.py'] = b'# leak\n'
        H.install_skill(payload, profile)
        errors = preflight_errors(tmp_path, profile, 'v1.1', H.tree_fingerprint(payload))
        assert any(error.startswith('P3:') and 'forbidden' in error for error in errors)

    def test_p3b_requires_longhaul_in_the_skills_list_for_candidates(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp,
                                  skills_out='- hermes-agent: enabled')
        assert any(error.startswith('P3b:') for error in errors), errors

    def test_p3b_forbids_longhaul_in_the_baseline_list(self, tmp_path):
        profile, _tree, _fp = profile_fixture(tmp_path, cond='baseline', install=False)
        errors = preflight_errors(tmp_path, profile, 'baseline', None,
                                  skills_out='- longhaul: enabled')
        assert any(error.startswith('P3b:') for error in errors), errors

    def test_p4_rejects_a_hook_file(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / 'HOOK.yaml').write_text('x', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P4:') for error in errors), errors

    def test_p4_rejects_a_non_empty_hooks_dir(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / 'hooks' / 'on_start.py').write_text('x', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P4:') for error in errors), errors

    def test_p5_rejects_a_non_empty_memory(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / 'memories' / 'note.md').write_text('carried over\n', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P5:') for error in errors), errors

    def test_p5_allows_empty_session_files(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / 'sessions' / 'empty.json').write_text('', encoding='utf-8')
        assert preflight_errors(tmp_path, profile, 'v1.1', fp) == []

    def test_p6_rejects_two_env_lines(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / '.env').write_text('OLLAMA_API_KEY=abc\nOTHER=1\n', encoding='utf-8')
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp)
        assert any(error.startswith('P6:') for error in errors), errors

    def test_p6_rejects_an_empty_key(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        (profile / '.env').write_text('OLLAMA_API_KEY=\n', encoding='utf-8')
        assert any(error.startswith('P6:') for error in
                   preflight_errors(tmp_path, profile, 'v1.1', fp))

    def test_p7_allows_the_bytecode_flag_but_nothing_else(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        env = base_env(tmp_path)
        assert preflight_errors(tmp_path, profile, 'v1.1', fp, env=env) == []
        env['GOOGLE_MAPS_API_KEY'] = 'do-not-print-me'
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp, env=env)
        assert any(error.startswith('P7:') and 'GOOGLE_MAPS_API_KEY' in error
                   for error in errors), errors
        assert 'do-not-print-me' not in ' '.join(errors)

    def test_p8_rejects_a_workdir_inside_the_repo(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp, workdir=REPO / 'tests')
        assert any(error.startswith('P8:') for error in errors), errors

    def test_p8_rejects_scenario_and_context_leaks(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        work = tmp_path / 'work'
        for rel in ('repo/expected.json', '.hermes.md', 'HERMES.md', 'AGENTS.md',
                    'AGENTS.override.md', 'agents.md', '.cursorrules', 'SOUL.md',
                    'CLAUDE.md'):
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
        for rel in ('.hermes', '.agents', '.cursor'):
            (work / rel).mkdir(parents=True, exist_ok=True)
        errors = preflight_errors(tmp_path, profile, 'v1.1', fp, workdir=work)
        hits = [error for error in errors if error.startswith('P8:')]
        assert len(hits) == 12, hits

    def test_p9_rejects_lhrender_on_the_path(self, tmp_path):
        profile, _tree, fp = profile_fixture(tmp_path)
        bindir = tmp_path / 'bin'
        bindir.mkdir(exist_ok=True)
        tool = bindir / 'lhrender'
        tool.write_text('#!/bin/sh\n', encoding='utf-8')
        tool.chmod(0o755)
        assert any(error.startswith('P9:') for error in
                   preflight_errors(tmp_path, profile, 'v1.1', fp))
        empty = tmp_path / 'empty-bin'
        empty.mkdir(exist_ok=True)
        clean = preflight_errors(tmp_path, profile, 'v1.1', fp,
                                 env={'HOME': str(tmp_path), 'PATH': str(empty)})
        assert not any(error.startswith('P9:') for error in clean), clean


class TestP11:
    NEUTRAL = {
        'cwd': '/var/tmp/lheval-subject/0123456789abcdefabcd/work',
        'query-file': '/var/tmp/lheval-subject/0123456789abcdefabcd/prompt.md',
        'profile': 'lheval0123456789abcdef',
        'profile home': '/var/tmp/lheval-subject/0123456789abcdefabcd/home/.hermes/'
                        'profiles/lheval0123456789abcdef',
        'chat argv': 'hermes -p lheval0123456789abcdef chat --query-file '
                     '/var/tmp/lheval-subject/0123456789abcdefabcd/prompt.md -m '
                     'deepseek-v4.1-flash:1017 --provider ollama-cloud',
    }

    def test_neutral_strings_pass(self):
        assert H.p11_errors(self.NEUTRAL) == []

    def test_every_identity_token_is_rejected(self):
        for token in ('longhaul', 'LongHaul', '\u8fdc\u822a', 'baseline',
                      'control-finish', 'recover-interrupt'):
            bad = dict(self.NEUTRAL)
            bad['cwd'] = '/var/tmp/%s/work' % token
            errors = H.p11_errors(bad)
            assert any(error.startswith('P11:') for error in errors), token
        bad = dict(self.NEUTRAL)
        bad['profile'] = 'lhevalv110abc1234'
        assert any('P11:' in error for error in H.p11_errors(bad))

    def test_p11_rejects_delimited_identity_prompt_and_env(self):
        for token in ('baseline-1', 'baseline_run/work', 'v1-eval/work', 'v1.1-x/work'):
            bad = dict(self.NEUTRAL)
            bad['cwd'] = '/var/tmp/%s' % token
            assert any(error.startswith('P11:') for error in H.p11_errors(bad)), token
        for label, value in (('prompt', 'You are in condition baseline.'),
                             ('env', 'HOME=/x LHEVAL_COND=v1'),
                             ('worktree', 'runs/v1/work')):
            bad = dict(self.NEUTRAL)
            bad[label] = value
            assert any(error.startswith('P11:') for error in H.p11_errors(bad)), label
        candidate = dict(self.NEUTRAL)
        candidate['argv'] = ['hermes', '-p', 'lheval0123456789abcdef', 'chat',
                             '--query-file', '/var/tmp/lheval-subject/'
                             '0123456789abcdefabcd/prompt.md', '-s', 'longhaul']
        assert H.p11_errors(candidate, cond='v1.1') == []
        baseline = dict(candidate)
        assert any(error.startswith('P11:') for error in H.p11_errors(baseline,
                                                                      cond='baseline'))
        lone = dict(self.NEUTRAL)
        lone['cwd'] = '/var/tmp/longhaul/work'
        assert any(error.startswith('P11:') for error in H.p11_errors(lone, cond='v1.1'))


# --------------------------------------------------------------------------- #
# command line, wrapper, stream parsing, classification
# --------------------------------------------------------------------------- #


class TestCommand:
    def test_candidate_argv_matches_the_contract(self, tmp_path, sandboxes):
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1')
        argv = H.build_command(['hermes'], 'lheval0123456789abcdef', spec, True)
        prompt = str((Path(spec.execdir) / 'prompt.md').resolve())
        assert argv == ['hermes', '-p', 'lheval0123456789abcdef', 'chat',
                        '--query-file', prompt, '-m', H.SUBJECT[0], '--provider',
                        H.SUBJECT[1], '-Q', '--ignore-user-config', '--format',
                        'stream-json', '--max-turns', '60', '--run-budget', '600',
                        '--source', 'tool', '-s', 'longhaul']

    def test_only_candidates_preload_the_skill(self, tmp_path):
        for cond in H.CONDITIONS:
            spec = make_spec(tmp_path, cond=cond)
            argv = H.build_command(['hermes'], 'lheval0123456789abcdef', spec,
                                   cond != 'baseline')
            if cond == 'baseline':
                assert '-s' not in argv and 'longhaul' not in argv
            else:
                assert argv.count('-s') == 1
                assert argv[argv.index('-s') + 1] == 'longhaul'
                assert argv[-2:] == ['-s', 'longhaul']
            assert '-m' in argv and '--provider' in argv


class TestLauncherChain:
    def test_launcher_chain_resolves_the_measured_roots(self):
        """The real host chain: hermes -> .hermes/bin -> tools python; venv python -> uv."""
        if shutil.which('hermes') is None:
            pytest.skip('hermes is not installed on this host')
        home = Path(os.environ.get('HOME') or Path.home()).resolve()
        entries = ['hermes', str(home / '.local/bin/hermes'),
                   str(home / '.hermes/hermes-agent/venv/bin/hermes'),
                   str(home / '.hermes/hermes-agent/venv/bin/python')]
        chains = [H.resolve_launcher_chain([entry], os.environ) for entry in entries]
        roots = set()
        for chain in chains:
            roots.update(chain.roots)
        assert str(home / '.local/bin/hermes') in roots
        assert str(home / '.local/bin') not in roots, \
            'the entry is bound as a single file, not the whole ~/.local/bin'
        assert str(home / '.hermes/hermes-agent/.hermes/bin') in roots
        assert str(home / '.hermes/hermes-agent') in roots
        tools = [root for root in roots
                 if re.fullmatch(re.escape(str(home)) + r'/\.hermes/tools/python-[^/]+', root)]
        assert tools, roots
        uv = [root for root in roots
              if root.startswith(str(home / '.local/share/uv/python/cpython-3.11'))]
        assert len(uv) >= 1, roots
        assert str(home / '.hermes') not in roots
        assert all(root.startswith(str(home) + '/') for root in roots)
        for chain, entry in zip(chains, entries):
            assert chain.entry and chain.final
            if entry == 'hermes':
                assert chain.entry == str(home / '.hermes/hermes-agent/.hermes/bin/hermes')
            for target in chain.targets:
                if not str(target).startswith(str(home) + '/'):
                    continue
                assert any(str(target) == root or str(target).startswith(root + '/')
                           for root in chain.roots), (target, chain.roots)
        kinds = {edge.kind for chain in chains for edge in chain.edges}
        assert 'shebang' in kinds and 'exec' in kinds and 'symlink' in kinds, kinds

    def test_launcher_chain_refuses_relative_and_unknown_entries(self):
        with pytest.raises(H.LauncherError):
            H.resolve_launcher_chain(['./hermes'], {})
        with pytest.raises(H.LauncherError):
            H.resolve_launcher_chain(['definitely-not-hermes'],
                                     {'PATH': '/nonexistent-bin'})

    def test_launcher_chain_refuses_a_home_target_without_a_root(self, tmp_path):
        home = tmp_path / 'home'
        stray = home / 'random-bin' / 'hermes'
        stray.parent.mkdir(parents=True)
        stray.write_text('#!/bin/sh\nexec /bin/true\n', encoding='utf-8')
        with pytest.raises(H.LauncherError) as excinfo:
            H.resolve_launcher_chain([str(stray)], {'PATH': '/usr/bin:/bin'}, home=home)
        assert 'root' in str(excinfo.value)


class TestSandboxPlan:
    def _plan(self, tmp_path):
        execdir = tmp_path / 'sandbox'
        (execdir / 'work').mkdir(parents=True)
        plan = H.plan_subject(execdir, ['/bin/sh'], tmp_path / 'home', env={'PATH': '/usr/bin:/bin'})
        return plan, execdir

    def test_wrap_argv_hides_home_tmp_and_siblings(self, tmp_path):
        plan, execdir = self._plan(tmp_path)
        argv = H.wrap_argv(plan, ['hermes', 'chat'])
        assert argv[0] == plan.bwrap == H.BWRAP
        assert argv[1:5] == ['--ro-bind', '/', '/', '--tmpfs']
        assert argv[5] == str(plan.real_home)
        tmp_at = next(i for i, a in enumerate(argv) if a == '--tmpfs' and argv[i + 1] == '/tmp')
        var_at = next(i for i, a in enumerate(argv) if a == '--tmpfs'
                      and argv[i + 1] == '/var/tmp')
        run_at = next(i for i, a in enumerate(argv) if a == '--tmpfs'
                      and argv[i + 1] == '/run/user')
        dir_at = argv.index('--dir')
        bind_at = argv.index('--bind')
        assert 4 < tmp_at < var_at < run_at < dir_at < bind_at
        assert argv[dir_at + 1] == str(execdir.parent)
        assert argv[bind_at + 1] == str(execdir) and argv[bind_at + 2] == str(execdir)
        for flag in ('--dev', '--proc', '--unshare-pid', '--die-with-parent',
                     '--new-session', '--clearenv'):
            assert flag in argv, flag
        assert argv[argv.index('--proc') + 1] == '/proc'
        assert argv[argv.index('--chdir') + 1] == str(execdir / 'work')
        assert argv.index('--clearenv') < argv.index('--setenv')
        assert argv[argv.index('--') + 1:] == ['hermes', 'chat']
        home_at = argv.index(str(plan.sandbox_home))
        assert argv[home_at - 2:home_at] == ['--setenv', 'HOME']
        assert argv[argv.index('PATH', home_at) + 1] == '/usr/local/bin:/usr/bin:/bin'
        assert argv[argv.index('TMPDIR', home_at) + 1] == str(plan.sandbox_home / 'tmp')
        assert H.wrap_argv(['hermes'], plan) == argv[:argv.index('--') + 1] + ['hermes']

    def test_plan_refuses_a_missing_bwrap(self, tmp_path):
        execdir = tmp_path / 'sandbox'
        (execdir / 'work').mkdir(parents=True)
        with pytest.raises(H.LauncherError):
            H.plan_subject(execdir, ['/bin/sh'], tmp_path / 'home',
                           env={'PATH': '/usr/bin:/bin'},
                           bwrap=str(tmp_path / 'no-such-bwrap'))


class TestStream:
    def test_parse_stream_collects_events_and_counts_bad_lines(self):
        stdout = '\n'.join([
            '{"type": "system", "model": "m", "session_id": "s"}',
            'not json',
            '[1, 2]',
            '{"type": "text", "text": "hi"}',
            '{"type": "result", "session_id": "s", "exit_code": 0, "text": "hi", '
            '"tokens": {"input": 1, "output": 1, "total": 2}}',
        ])
        parsed = H.parse_stream(stdout)
        assert parsed['bad_lines'] == 2
        assert len(parsed['events']) == 3
        assert parsed['result']['session_id'] == 's'

    def test_parse_stream_without_result(self):
        parsed = H.parse_stream('{"type": "text", "text": "hi"}')
        assert parsed['result'] is None and parsed['bad_lines'] == 0

    def test_classify_matrix(self):
        result = json.dumps({'type': 'result', 'session_id': 's', 'exit_code': 0,
                             'text': 'done', 'tokens': {}})
        assert H.classify(0, result + '\n', '', False)[0] == 'ok'
        assert H.classify(0, result + '\n', '', True)[0] == 'timeout'
        assert H.classify(1, '', 'HTTP 429 Too Many Requests', False)[0] == 'provider_429'
        ratelimited = json.dumps({'type': 'result', 'exit_code': 1, 'text': '',
                                  'tokens': {}, 'error': 'rate limit exceeded'})
        assert H.classify(1, ratelimited, '', False)[0] == 'provider_429'
        errored = json.dumps({'type': 'result', 'exit_code': 1, 'text': 'x',
                              'tokens': {}, 'error': 'boom'})
        assert H.classify(1, errored, '', False)[0] == 'provider_error'
        assert H.classify(3, 'not json\n', 'segfault', False)[0] == 'hermes_crash'
        assert H.classify(0, '', '', False)[0] == 'hermes_crash'
        empty = json.dumps({'type': 'result', 'exit_code': 0, 'text': '   ',
                            'tokens': {}})
        assert H.classify(0, empty, '', False)[0] == 'empty_answer'

    def test_a_crash_is_not_a_rate_limit_because_its_stderr_contains_429(self):
        """R8d (found by a flaky e2e run, 2 of 33 under load): the digits 429 inside a
        random profile or temp name, a path, a port or a traceback line number are not
        an HTTP 429. A crash that merely contains them stays a ``hermes_crash``; only a
        429 that stands alone as a token (or the words rate limit / too many requests)
        is a ``provider_429``."""
        looks_like_429 = (
            'Traceback (most recent call last):\n  fake crash in lheval0123429abcdef01',
            'Traceback (most recent call last):\n  File "/tmp/x.py", line 429, in main',
            'cannot open /tmp/tmp429xyz/state.json',
            'connect to 127.0.0.1:14290 failed',
            'pid 4290 killed',
        )
        for stderr in looks_like_429:
            assert H.classify(3, 'not json\n', stderr, False)[0] == 'hermes_crash', stderr
        for stderr in ('HTTP 429 Too Many Requests', 'Error code: 429', 'status=429',
                       'provider error: HTTP/1.1 429', 'rate limit exceeded', '429'):
            assert H.classify(1, '', stderr, False)[0] == 'provider_429', stderr

    def test_redact_removes_every_occurrence(self):
        secret = 'a.b*c/secret'
        assert H.redact('x ' + secret + ' y ' + secret, [secret]) == 'x REDACTED y REDACTED'
        assert H.redact('abc', []) == 'abc'
        assert H.redact(None, [secret]) == ''
        long_secret = 'abcdef'
        assert H.redact(long_secret, ['abc', long_secret]).count('REDACTED') == 1

    def test_stream_redaction_handles_split_secret(self):
        split = [KEY[:9], KEY[9:20], KEY[20:]]
        stdout = '\n'.join(json.dumps({'type': 'text', 'text': part}) for part in split)
        stdout += '\n' + json.dumps({'type': 'result', 'exit_code': 0, 'text': '',
                                     'tokens': {}})
        clean = H.sanitise_stream(stdout, [KEY])
        assert KEY not in clean
        texts = [event.get('text', '') for event in
                 (json.loads(line) for line in clean.splitlines() if line.strip())]
        assert KEY not in ''.join(texts)
        for part in split:
            assert part not in ''.join(texts)
        assert 'result' in clean

    def test_sanitise_stream_keeps_normal_text(self):
        stdout = json.dumps({'type': 'text', 'text': 'hello'}) + '\n'
        assert '"hello"' in H.sanitise_stream(stdout, [KEY])

    def test_recursive_stream_redaction(self):
        events = [
            {'type': 'tool_use', 'name': 'terminal',
             'input': {'command': 'echo ' + KEY, 'nested': [{'x': KEY}]}},
            {'type': 'thinking', 'text': 'plan', 'blocks': [{'text': KEY[:9]},
                                                            {'text': KEY[9:]}]},
            {'type': 'result', 'exit_code': 1, 'text': 'bad', 'tokens': {},
             'error': {'message': KEY}},
        ]
        stdout = '\n'.join(json.dumps(event) for event in events)
        clean = H.sanitise_stream(stdout, [KEY])
        assert KEY not in clean
        leaves = []
        for line in clean.splitlines():
            event = json.loads(line)
            leaves.extend(H._iter_leaves(event))
        assert KEY not in ''.join(leaves)
        assert 'terminal' in clean


# --------------------------------------------------------------------------- #
# run_one against the fake
# --------------------------------------------------------------------------- #


class TestRunOne:
    def test_ok_run_writes_every_artifact_and_cleans_up(self, tmp_path, home,
                                                        sandboxes):
        configure(home, mode='ok', reply='All good.\nFINAL: complete',
                  tool_calls=[{'name': 'terminal', 'input': {'command': 'ls'},
                               'output': 'a.txt'}])
        spec = ready_spec(tmp_path, sandboxes, files={'out/a.txt': 'a\n'})
        execdir = Path(spec.execdir)
        ledger = L.Ledger(tmp_path / 'out' / 'run1' / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok' and meta['rc'] == 0
        assert meta['session_id'] == 'fake-session'
        assert meta['profile'] == H.profile_name('run1', 'v1.1', 'recover-interrupt', 1)
        for name in ('cmd.json', 'events.jsonl', 'stderr.txt', 'meta.json',
                     'manifest.before.json', 'manifest.after.json'):
            assert (spec.rundir / name).is_file(), name
        assert (spec.rundir / 'work' / 'out' / 'a.txt').is_file()
        assert (spec.rundir / 'prompt.md').read_text() == spec.prompt
        assert not execdir.exists(), 'the neutral exec dir was not removed'
        argv = json.loads((spec.rundir / 'cmd.json').read_text())['argv']
        assert argv.count('-s') == 1 and argv[argv.index('-s') + 1] == 'longhaul'
        assert argv[0] != H.BWRAP, 'a test double must not be wrapped in bwrap'
        rows = ledger.rows()
        assert [row['event'] for row in rows] == ['attempt_dir', 'profile_created',
                                                  'preflight', 'started', 'finished',
                                                  'profile_deleted']
        assert rows[3]['pgid'] and rows[3]['execdir'] == str(execdir)
        assert {row.get('key') for row in rows} == {'v1.1/recover-interrupt/t1'}
        assert ledger.live_profiles() == []

    def test_control_condition_run_keeps_a_profile_without_a_skill(self, tmp_path, home,
                                                                   sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='baseline')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, None, bwrap=False)
        assert meta['status'] == 'ok', meta['error']
        argv = json.loads((spec.rundir / 'cmd.json').read_text())['argv']
        assert '-s' not in argv and 'longhaul' not in argv
        assert ledger.created_profiles() == [meta['profile']]

    def test_subject_visible_paths_are_neutral(self, tmp_path, home, sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1', case='recover-interrupt')
        execdir = Path(spec.execdir)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok'
        chat = [call for call in calls(home) if 'chat' in call['argv']][0]
        inner = list(chat['argv'])
        if '--fake-home' in inner:      # test-double plumbing, not the subject argv
            index = inner.index('--fake-home')
            del inner[index:index + 2]
        if '-s' in inner:               # the allowed candidate preload pair
            index = inner.index('-s')
            assert inner[index + 1] == 'longhaul'
            del inner[index:index + 2]
        profile_home = execdir / 'home' / '.hermes' / 'profiles' / meta['profile']
        visible = {
            'argv': ' '.join(inner),
            'cwd': chat['cwd'],
            'prompt': inner[inner.index('--query-file') + 1],
            'profile': meta['profile'],
            'profile home': str(profile_home),
            'system prompt': 'Current working directory: %s\nActive Hermes profile: %s. '
                             'This session reads and writes %s/' % (
                                 chat['cwd'], meta['profile'], profile_home),
        }
        for label, value in visible.items():
            text = str(value)
            assert 'longhaul' not in text.lower(), label
            assert '\u8fdc\u822a' not in text, label
            for token in ('baseline', 'v1.1', 'recover-interrupt'):
                assert token not in text, (label, token, text)
        assert re.fullmatch(r'lheval[0-9a-f]{16}', meta['profile'])
        assert chat['cwd'] == str(execdir / 'work')
        assert H.p11_errors(visible, cond='v1.1') == []

    def test_env_is_scrubbed_and_the_key_stays_inside_the_sandbox(self, tmp_path, home,
                                                                  sandboxes,
                                                                  monkeypatch):
        monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'leak-me-not')
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='v1')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.0'), bwrap=False)
        assert meta['status'] == 'ok'
        recorded = calls(home)
        assert recorded
        allowed = set(H.ALLOWED_ENV) | {'PYTHONDONTWRITEBYTECODE'}
        for call in recorded:
            assert set(call['env_keys']) <= allowed, call['env_keys']
            assert 'GOOGLE_MAPS_API_KEY' not in call['env_keys']
        out_root = tmp_path / 'out'
        for path in out_root.rglob('*'):
            if path.is_file():
                assert KEY.encode() not in path.read_bytes(), path
        assert (home / '.hermes' / '.env').read_text().count(KEY) == 1

    def test_runner_artifacts_carry_no_key_assignment(self, tmp_path, home, sandboxes):
        """R6: there is no key to redact, because no runner API copies one.

        Redaction stays a best-effort text filter (covered by TestStream); it is
        not the containment proof. The invariant here is that the harness never
        writes a key assignment into a run directory or a sandbox.
        """
        configure(home, mode='ok', stderr='token=' + KEY + '\n')
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False,
                         keep_profile=True)
        assert meta['status'] == 'ok'
        assert (spec.rundir / 'stderr.txt').is_file()
        for path in Path(spec.rundir).rglob('*'):
            if path.is_file():
                assert b'OLLAMA_API_KEY=' not in path.read_bytes(), path
        assert not (Path(spec.execdir) / 'home' / '.hermes' / '.env').exists(), \
            'the harness wrote a key into the sandbox HOME'
        assert KEY not in (spec.rundir / 'cmd.json').read_text()

    @pytest.mark.parametrize('mode,status', [
        ('429', 'provider_429'), ('crash', 'hermes_crash'),
        ('empty', 'empty_answer'), ('error', 'provider_error')])
    def test_fake_modes_map_to_the_contract_statuses(self, tmp_path, home, sandboxes,
                                                     mode, status):
        configure(home, mode=mode)
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == status, meta

    def test_timeout_kills_the_process_group(self, tmp_path, home, sandboxes):
        configure(home, mode='timeout', timeout_sleep=10)
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        started = time.monotonic()
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False,
                     timeout_s=0.5)
        assert meta['status'] == 'timeout'
        assert time.monotonic() - started < 8

    def test_preflight_failure_never_starts_chat(self, tmp_path, home, sandboxes):
        meta = run_preflight_failure(tmp_path, home, sandboxes, cfg={
            'on_skills_list': {'extra': {'hooks/HOOK.yaml': 'boom'}}})
        assert 'P4' in meta['error']
        assert not (spec_path(tmp_path) / 'cmd.json').exists()

    def test_p2_p5_p6_injections_are_caught_at_run_level(self, tmp_path, home,
                                                         sandboxes):
        meta = run_preflight_failure(tmp_path, home, sandboxes, cond='baseline',
                                     cfg={'profile_extra': {'notes.txt': 'longhaul'}})
        assert 'P2' in meta['error']
        meta = run_preflight_failure(tmp_path, home, sandboxes, cfg={
            'profile_extra': {'memories/note.md': 'carried over'}})
        assert 'P5' in meta['error']
        meta = run_preflight_failure(tmp_path, home, sandboxes, cfg={
            'on_skills_list': {'env_append': 'SECOND=1\n'}})
        assert 'P6' in meta['error']

    def test_p9_injection_is_caught(self, tmp_path, home, sandboxes):
        # The subject PATH is neutral, so a stray lhrender can only appear in the
        # harness bin dir that holds the resolved hermes entrypoint.
        bindir = Path(home).parent / 'hermes-bin'
        binary = fake_bin(home)[1]
        os.chmod(binary, 0o755)
        tool = bindir / 'lhrender'
        tool.write_text('#!/bin/sh\n', encoding='utf-8')
        tool.chmod(0o755)
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, [binary, '--fake-home', str(home)], ledger, home,
                         tree('1.1'), bwrap=False)
        assert meta['status'] == 'preflight_fail' and 'P9' in meta['error'], meta
        assert not [call for call in calls(home) if 'chat' in call['argv']]

    def test_missing_skill_tree_is_a_preflight_failure(self, tmp_path, home, sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, None, bwrap=False)
        assert meta['status'] == 'preflight_fail' and 'P3' in meta['error']

    def test_state_before_is_snapshotted(self, tmp_path, home, sandboxes):
        configure(home, mode='ok')
        state = '{"schema_version": 2}'
        spec = ready_spec(tmp_path, sandboxes, files={'.task/state.json': state})
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok'
        assert (spec.rundir / 'state.before.json').read_text() == state
        before = json.loads((spec.rundir / 'manifest.before.json').read_text())
        assert '.task/state.json' in before

    def test_scripted_write_shows_up_in_the_after_manifest(self, tmp_path, home,
                                                           sandboxes):
        configure(home, mode='ok', writes=[{'path': 'out/part3.md',
                                            'content': 'total: 42\n'}])
        spec = ready_spec(tmp_path, sandboxes, files={'out/part1.md': 'a\n'})
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        before = json.loads((spec.rundir / 'manifest.before.json').read_text())
        after = json.loads((spec.rundir / 'manifest.after.json').read_text())
        assert 'out/part3.md' not in before and 'out/part3.md' in after

    def test_bytecode_from_a_test_run_is_not_a_scope_breach(self, tmp_path, home,
                                                            sandboxes):
        configure(home, mode='ok', writes=[
            {'path': 'src/__pycache__/calc.cpython-311.pyc', 'content': 'x'}])
        spec = ready_spec(tmp_path, sandboxes, files={'src/calc.py': 'X = 1\n'})
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        after = json.loads((spec.rundir / 'manifest.after.json').read_text())
        assert 'src/__pycache__/calc.cpython-311.pyc' not in after
        assert 'src/calc.py' in after

    def test_keyboard_interrupt_is_recorded_and_the_sandbox_removed(self, tmp_path,
                                                                    home, sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        execdir = Path(spec.execdir)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')

        def interrupting(argv, cwd=None, env=None, timeout=None, on_spawn=None,
                         stop=None):
            if 'chat' in argv:
                raise KeyboardInterrupt()
            return H._popen(argv, cwd=cwd, env=env, timeout=timeout,
                           on_spawn=on_spawn, stop=stop)

        with pytest.raises(KeyboardInterrupt):
            H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'),
                      bwrap=False, call=interrupting)
        key = L.run_key(spec.cond, spec.case, spec.trial)
        assert ledger.last_finished(key)['status'] == 'interrupted'
        assert not execdir.exists()

    def test_create_failure_is_a_retryable_crash_not_a_traceback(self, tmp_path, home,
                                                                 sandboxes):
        configure(home, mode='ok', profile_create_rc=1,
                  profile_create_stderr='profile store is locked')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'hermes_crash'
        key = L.run_key(spec.cond, spec.case, spec.trial)
        assert ledger.last_finished(key)['status'] == 'hermes_crash'
        assert not [call for call in calls(home) if 'chat' in call['argv']]

    def test_delete_failure_downgrades_the_run_to_a_retryable_crash(self, tmp_path,
                                                                    home, sandboxes):
        configure(home, mode='ok', profile_delete_rc=1)
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok'
        rows = ledger.rows()
        assert rows[-1]['event'] == 'finished'
        assert rows[-1]['status'] == 'hermes_crash'
        assert rows[-1].get('correction') is True
        assert L.pending([L.run_key('v1.1', 'recover-interrupt', 1)], ledger, 2)

    def test_a_real_hermes_entry_is_refused_before_bwrap(self, tmp_path, home, sandboxes):
        """R6: the offline lane never runs an unverified real hermes entry.

        The old contract wrapped any ``hermes`` on PATH; the verified double is
        the only entry, so this is a refusal before bwrap is even started.
        """
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='baseline', case='control-finish')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        recorded = []

        def call(argv, **kwargs):
            recorded.append(list(argv))
            return H.Proc(1, '', 'must not run')

        state = tmp_path / 'state'
        state.mkdir()
        with pytest.raises(H.DoubleRejected):
            H.run_one(spec, ['hermes'], ledger, home, None, call=call,
                      bwrap=H.BWRAP, state=state)
        assert recorded == [], 'bwrap was started for an unverified entry'
        assert ledger.rows() == []

    def test_run_one_needs_no_key_in_the_sandbox(self, tmp_path, home, sandboxes):
        """R6: a sandbox HOME without any key is the normal case, not an error."""
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        assert not (Path(spec.execdir) / 'home' / '.hermes' / '.env').exists()
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok', meta
        assert not (Path(spec.execdir) / 'home' / '.hermes' / '.env').exists()

    def test_no_binary_means_no_run(self, tmp_path, home, sandboxes):
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        with pytest.raises(ValueError):
            H.run_one(spec, None, ledger, home, tree('1.1'), bwrap=False)
        assert ledger.rows() == []
        assert calls(home) == []


class TestLiveIsolation:
    """The offline lane is fail-closed: every hermes call is wrapped, none is bare."""

    def _state(self, tmp_path):
        state = tmp_path / 'state'
        state.mkdir(exist_ok=True)
        return state

    def _stub(self, tmp_path):
        stub = tmp_path / 'bwrap-stub'
        stub.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        stub.chmod(0o755)
        return stub

    def _entry(self, tmp_path):
        bindir = tmp_path / 'hermes-bin'
        bindir.mkdir(exist_ok=True)
        target = bindir / 'hermes.py'
        if not target.exists():
            shutil.copyfile(FAKE, target)
        return [PY, str(target)]

    def test_live_without_bwrap_is_refused_before_a_profile(self, tmp_path, home,
                                                             sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, live_entry(home), ledger, home, tree('1.1'),
                         bwrap=str(tmp_path / 'missing-bwrap'), state=self._state(tmp_path))
        assert meta['status'] == 'preflight_fail'
        assert 'S1' in meta['error']
        assert ledger.created_profiles() == []
        assert calls(home) == []

    def test_a_chain_target_without_an_allowed_root_is_refused(self, tmp_path, home,
                                                               sandboxes):
        """The verified double is the only entry; a stray script never runs."""
        configure(home, mode='ok')
        stray = home / 'random-bin' / 'hermes'
        stray.parent.mkdir(parents=True)
        stray.write_text('#!/bin/sh\nexec /bin/true\n', encoding='utf-8')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        with pytest.raises(H.DoubleRejected):
            H.run_one(spec, [str(stray)], ledger, home, tree('1.1'),
                      bwrap=str(self._stub(tmp_path)), state=self._state(tmp_path))
        assert ledger.created_profiles() == [] and calls(home) == []

    def test_live_refuses_when_the_sandboxed_version_probe_fails(self, tmp_path, home,
                                                                 sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        stub = self._stub(tmp_path)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')

        def call(argv, cwd=None, env=None, timeout=None, on_spawn=None, stop=None):
            if list(argv) == [str(stub), '--version']:
                return H.Proc(0, 'bubblewrap 0.9.0\n', '')
            return H.Proc(1, '', 'exec: python3: not found')

        meta = H.run_one(spec, self._entry(tmp_path), ledger, home, tree('1.1'),
                         bwrap=str(stub), state=self._state(tmp_path), call=call)
        assert meta['status'] == 'preflight_fail'
        assert 'S1' in meta['error'] and 'inside the sandbox' in meta['error']
        assert ledger.created_profiles() == []

    def test_a_broken_bwrap_is_fail_closed_and_never_runs_unwrapped(self, tmp_path, home,
                                                                   sandboxes):
        """A bwrap that answers --version but cannot run the subject must refuse.

        Every recorded argv must still be the bwrap binary: the fallback is a
        refused preflight, never an unwrapped subject.
        """
        configure(home, mode='ok')
        broken = tmp_path / 'broken-bwrap'
        broken.write_text('#!/bin/sh\n'
                          'if [ "$1" = "--version" ]; then echo "bubblewrap 0.11.2"; exit 0; fi\n'
                          'echo "bwrap: setting up uid map: Permission denied" >&2\n'
                          'exit 1\n', encoding='utf-8')
        broken.chmod(0o755)
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        recorded = []

        def call(argv, **kwargs):
            recorded.append(list(argv))
            return H._popen(argv, **kwargs)

        meta = H.run_one(spec, live_entry(home), ledger, home, tree('1.1'),
                         bwrap=str(broken), state=self._state(tmp_path), call=call)
        assert meta['status'] == 'preflight_fail' and 'S1' in meta['error'], meta
        assert ledger.created_profiles() == []
        assert calls(home) == []
        assert recorded and all(argv[0] == str(broken) for argv in recorded), recorded

    def test_live_mode_refuses_to_keep_a_profile(self, tmp_path, home, sandboxes):
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        with pytest.raises(H.LiveBlocked):
            H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'),
                      mode=H.MODE_LIVE_BLOCKED, state=self._state(tmp_path),
                      keep_profile=True)
        assert ledger.rows() == [] and calls(home) == []

    def test_live_wraps_every_hermes_call(self, tmp_path, home, sandboxes):
        configure(home, mode='ok', reply='done\nFINAL: complete', skills=['longhaul'])
        spec = ready_spec(tmp_path, sandboxes)
        stub = self._stub(tmp_path)
        entry = self._entry(tmp_path)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        recorded = []
        result = json.dumps({'type': 'result', 'session_id': 's', 'exit_code': 0,
                             'text': 'done\nFINAL: complete', 'tokens': {}})

        system = json.dumps({'type': 'system', 'model': 'm', 'session_id': 's',
                             'skills': ['longhaul']})
        tool = '\n'.join([
            json.dumps({'type': 'tool_use', 'name': 'skill_view', 'tool_call_id': 'c1',
                        'input': {'name': 'longhaul'}}),
            json.dumps({'type': 'tool_result', 'tool_call_id': 'c1',
                        'output': 'longhaul skill loaded', 'is_error': False})])

        profile_root = Path(spec.execdir) / 'home' / '.hermes' / 'profiles'

        def call(argv, cwd=None, env=None, timeout=None, on_spawn=None, stop=None):
            recorded.append(list(argv))
            inner = list(argv[argv.index('--') + 1:]) if '--' in argv else list(argv)
            if list(argv) == [str(stub), '--version']:
                return H.Proc(0, 'bubblewrap 0.9.0\n', '')
            if '--version' in inner:
                return H.Proc(0, 'hermes 0.21.6 (sandboxed)\n', '')
            if 'skills' in inner:
                return H.Proc(0, '- longhaul: enabled\n', '')
            if 'profile' in inner and 'create' in inner:
                root = profile_root / inner[inner.index('create') + 1]
                for sub in ('skills', 'hooks', 'memories', 'sessions'):
                    (root / sub).mkdir(parents=True, exist_ok=True)
                (root / '.env').write_text('OLLAMA_API_KEY=fake-placeholder\n',
                                           encoding='utf-8')
                return H.Proc(0, 'Created profile\n', '')
            if 'profile' in inner and 'delete' in inner:
                shutil.rmtree(profile_root / inner[inner.index('delete') + 1],
                              ignore_errors=True)
                return H.Proc(0, 'Deleted profile\n', '')
            if on_spawn:
                on_spawn(4242)
            return H.Proc(0, system + '\n' + tool + '\n' + result + '\n', '')

        meta = H.run_one(spec, entry, ledger, home, tree('1.1'), bwrap=str(stub),
                         state=self._state(tmp_path), call=call)
        assert meta['status'] == 'ok', meta
        assert recorded[0] == [str(stub), '--version']
        wrapped = recorded[1:]
        assert wrapped
        for argv in wrapped:
            assert argv[0] == str(stub), argv[:3]
            assert argv[argv.index('PATH') - 1] == '--setenv'
            inner = argv[argv.index('--') + 1:]
            # R6: every call runs the verified copy inside the attempt, not the
            # host path that was verified.
            assert any(str(part).endswith('/subject.py') for part in inner), inner
            if len(entry) == 2:
                # R7: the executed interpreter is always realpath(sys.executable).
                assert inner[0] == os.path.realpath(sys.executable), inner[:2]
            assert argv[argv.index('--tmpfs') + 1] == str(home)
        inners = [argv[argv.index('--') + 1:] for argv in wrapped]
        assert any('profile' in inner and 'create' in inner for inner in inners)
        assert any('skills' in inner and 'list' in inner for inner in inners)
        assert any('chat' in inner for inner in inners)
        assert any('profile' in inner and 'delete' in inner for inner in inners)
        assert meta['skill_loaded'] == 'confirmed'

    def test_real_bwrap_runs_the_lifecycle_and_deletes_before_the_move(
            self, tmp_path, home, sandboxes):
        """The delete runs while <execdir>/work still exists, inside the same bwrap.

        R3 MAJOR: the work tree was moved away before the wrapped
        ``profile delete``, so bwrap refused to chdir and every live attempt was
        logged as ``finished ok`` plus a ``correction=True`` crash.
        """
        assert BWRAP_OK, 'bwrap and /usr/bin/python3 are hard prerequisites of this suite'
        entry = live_entry(home)
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1', files={'report.md': 'x\n'})
        execdir = Path(spec.execdir)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        recorded = []

        def spy(argv, **kwargs):
            recorded.append(list(argv))
            return H._popen(argv, **kwargs)

        meta = H.run_one(spec, entry, ledger, home, tree('1.1'),
                         bwrap=H.BWRAP, state=self._state(tmp_path), call=spy)
        assert meta['status'] == 'ok', meta
        assert all(argv[0] == H.BWRAP for argv in recorded), [a[0] for a in recorded]
        deletes = [argv for argv in recorded if 'delete' in argv]
        assert deletes, 'no wrapped profile delete was issued'
        for argv in deletes:
            assert argv[argv.index('--chdir') + 1] == str(execdir), \
                'the delete must chdir into a directory that still exists'
        key = L.run_key(spec.cond, spec.case, spec.trial)
        finished = [row for row in ledger.rows()
                    if row.get('key') == key and row.get('event') == 'finished']
        assert [row['status'] for row in finished] == ['ok'], finished
        assert not [row for row in ledger.rows() if row.get('correction')], \
            'the wrapped delete failed after the work tree had been moved'
        assert (spec.rundir / 'work' / 'report.md').is_file(), 'the work tree was not consumed'
        assert not execdir.exists(), 'the sandbox survived'
        assert not (home / '.hermes' / 'profiles').exists(), \
            'a profile was created in the host home instead of the sandbox'
        assert calls(home) == [], 'the subject wrote its call log into the host home'

    def test_real_bwrap_hides_the_host_home_and_the_entry_siblings(self, tmp_path, home,
                                                                    sandboxes):
        """Subprocess evidence, not argv: the child reports what it can actually see."""
        assert BWRAP_OK, 'bwrap and /usr/bin/python3 are hard prerequisites of this suite'
        entry = live_entry(home, sibling='other-tool')
        (home / 'canary-home.txt').write_text('host secret\n', encoding='utf-8')
        execdir = new_execdir(sandboxes, key='v1.1/x/t1')
        (execdir / 'work').mkdir(exist_ok=True)
        plan = H.plan_subject(execdir, entry, home, env=os.environ)
        script = ('printf "home=%s\\n" "$HOME"; printf "cwd=%s\\n" "$(pwd)"; '
                  'for p in "' + entry[0] + '" "' + str(Path(entry[0]).parent / 'other-tool')
                  + '" "' + str(Path(home) / 'canary-home.txt') + '"; do '
                  'if [ -e "$p" ]; then echo "visible $p"; fi; done')
        proc = subprocess.run(plan.wrap(['/bin/sh', '-c', script]), capture_output=True,
                              text=True, cwd=str(execdir / 'work'), env=plan.env, timeout=60)
        assert proc.returncode == 0, proc.stderr[-400:]
        assert 'home=%s' % (execdir / 'home') in proc.stdout
        assert 'cwd=%s' % (execdir / 'work') in proc.stdout
        assert 'visible %s' % entry[0] in proc.stdout, 'the entry itself must be mounted'
        assert 'other-tool' not in proc.stdout, \
            'the whole ~/.local/bin is mounted instead of the entry file alone'
        assert 'canary-home.txt' not in proc.stdout, 'the real home is not hidden'


class TestPreload:
    def test_candidate_preload_is_exact_and_recorded(self, tmp_path, home, sandboxes):
        configure(home, mode='ok', reply='done\nFINAL: complete', skills=['longhaul'],
                  tool_calls=[{'name': 'skill_view', 'input': {'name': 'longhaul'},
                               'output': 'longhaul skill loaded'}])
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok'
        argv = json.loads((spec.rundir / 'cmd.json').read_text())['inner']
        assert argv.count('-s') == 1
        at = argv.index('-s')
        assert argv[at + 1] == 'longhaul'
        assert meta['skill_requested'] is True
        assert meta['skill_enabled'] is True
        assert meta['skill_loaded'] == 'confirmed'

        base = ready_spec(tmp_path, sandboxes, cond='baseline', case='control-finish')
        base_meta = H.run_one(base, fake_bin(home), ledger, home, None, bwrap=False)
        base_argv = json.loads((base.rundir / 'cmd.json').read_text())['inner']
        assert '-s' not in base_argv and 'longhaul' not in base_argv
        assert base_meta['skill_requested'] is False
        assert base_meta['skill_enabled'] is False
        assert base_meta['skill_loaded'] is False

    def test_missing_confirmation_is_unconfirmed(self, tmp_path, home, sandboxes):
        configure(home, mode='ok', reply='done\nFINAL: complete')
        spec = ready_spec(tmp_path, sandboxes, cond='v1.1')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok'
        assert meta['skill_loaded'] == 'unconfirmed'


def spec_path(tmp_path):
    return (tmp_path / 'out' / 'run1' / 'runs' / 'v1.1' / 'recover-interrupt' / 't1')


# --------------------------------------------------------------------------- #
# interruptions and reaping
# --------------------------------------------------------------------------- #


class TestInterrupt:
    def test_interrupt_stops_unstarted_work_and_reaps_child(self, tmp_path, home,
                                                            sandboxes):
        configure(home, mode='ok', sleep=20)
        specs = [ready_spec(tmp_path, sandboxes, case='case-%d' % index, trial=1)
                 for index in range(3)]
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        pgids = []
        entered = []
        env = H.subject_env(os.environ, Path(specs[0].execdir) / 'home')

        def run(spec):
            key = L.run_key(spec.cond, spec.case, spec.trial)
            entered.append(key)
            if entered[0] != key:
                return          # queued work must never reach a child
            def hook(pid):
                pgids.append(pid)
                ledger.append({'event': 'started', 'key': key, 'pgid': pid,
                               'profile': 'lheval0123456789abcdef'})

            def child():
                H._popen([PY, str(FAKE), '--fake-home', str(home), '-p',
                          'lheval0123456789abcdef', 'chat'],
                         cwd=str(Path(spec.execdir)), env=env, timeout=30, on_spawn=hook)

            worker = threading.Thread(target=child)
            worker.start()
            deadline = time.time() + 10
            while not pgids and time.time() < deadline:
                time.sleep(0.01)
            raise KeyboardInterrupt()

        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            H.run_matrix(specs, run, ledger, parallel=1, max_attempts=1,
                         sleep=lambda _s: None)
        assert time.monotonic() - started < 10
        assert pgids, 'no subject was started'
        assert [row['key'] for row in ledger.rows()
                if row.get('event') == 'started'] == ['v1.1/case-0/t1']
        deadline = time.time() + 5
        while time.time() < deadline and H._alive(pgids[0]):
            time.sleep(0.01)
        assert not H._alive(pgids[0]), 'the subject process survived the interrupt'


class TestReap:
    def test_resume_after_a_hard_kill(self, tmp_path, home, sandboxes):
        configure(home, mode='ok', sleep=20)
        spec = ready_spec(tmp_path, sandboxes, case='control-finish')
        execdir = Path(spec.execdir)
        key = L.run_key(spec.cond, spec.case, spec.trial)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        env = H.subject_env(os.environ, execdir / 'home')
        holder = {}
        ledger.append({'event': 'profile_created', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'attempt': 1})

        def child():
            holder['proc'] = H._popen(
                [PY, str(FAKE), '--fake-home', str(home), '-p',
                 'lheval0123456789abcdef', 'chat'],
                cwd=str(execdir / 'work'), env=env, timeout=30,
                on_spawn=lambda pid: holder.__setitem__('pgid', pid))

        worker = threading.Thread(target=child)
        worker.start()
        deadline = time.time() + 10
        while 'pgid' not in holder and time.time() < deadline:
            time.sleep(0.01)
        pgid = holder['pgid']
        assert H._alive(pgid)

        ledger.append({'event': 'started', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'pgid': pgid,
                       'execdir': str(execdir), 'attempt': 1,
                       'owner': {'pid': os.getpid(), 'start': '1',
                                 'boot': H.boot_id()},
                       'pgid_start': H.process_start(pgid)})
        reaped = H.reap_stale(ledger)
        assert reaped == [key]
        assert not H._alive(pgid)
        assert not execdir.exists()
        assert ledger.last_finished(key)['status'] == 'interrupted'
        worker.join(timeout=10)

    def test_reap_accepts_only_canonical_execdirs(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        outsider = tmp_path / 'evil' / 'deadbeefdeadbeefdead'
        (outsider / 'home').mkdir(parents=True)
        (outsider / 'work').mkdir()
        key = 'v1/x/t1'
        ledger.append({'event': 'started', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'pgid': os.getpid(),
                       'execdir': str(outsider), 'attempt': 1})
        assert H.reap_stale(ledger) == [key]
        assert outsider.exists(), 'a non-canonical execdir must never be deleted'
        assert H._alive(os.getpid())
        assert ledger.last_finished(key)['status'] == 'interrupted'

    def test_reap_writes_ledger_only_profile_deleted(self, tmp_path, sandboxes):
        key = 'v1/x/t1'
        execdir = new_execdir(sandboxes, key=key)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'profile_created', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'attempt': 1})
        ledger.append({'event': 'started', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'pgid': os.getpid(),
                       'execdir': str(execdir), 'attempt': 1})
        assert H.reap_stale(ledger) == [key]
        assert not execdir.exists()
        deleted = [row for row in ledger.rows() if row.get('event') == 'profile_deleted']
        assert deleted and deleted[-1].get('reaped') is True
        assert ledger.live_profiles() == []

    def test_reap_never_kills_a_reused_pid_without_the_profile(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'started', 'key': 'v1/x/t1',
                       'profile': 'lheval0123456789abcdef', 'pgid': os.getpid(),
                       'execdir': str(tmp_path / 'gone')})
        assert H.reap_stale(ledger) == ['v1/x/t1']
        assert H._alive(os.getpid())
        assert ledger.last_finished('v1/x/t1')['status'] == 'interrupted'

    def test_reap_cleans_an_attempt_dir_recorded_before_started(self, tmp_path, sandboxes):
        """R3 MAJOR (a): a SIGKILL before ``started`` left the sandbox and its key.

        ``run_one`` records ``attempt_dir`` before it writes the model key, so a
        harness killed in that window is still owned and reapable.
        """
        key = 'v1/x/t1'
        execdir = new_execdir(sandboxes, key=key)
        assert (execdir / 'home' / '.hermes' / '.env').is_file()
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'profile_created', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'attempt': 1,
                       'execdir': str(execdir)})
        ledger.append({'event': 'attempt_dir', 'key': key, 'attempt': 1,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(execdir)})
        assert H.reap_stale(ledger) == [key]
        assert not execdir.exists(), 'the pre-start sandbox with the key survived the reap'
        assert ledger.last_finished(key)['status'] == 'interrupted'
        deleted = [row for row in ledger.rows() if row.get('event') == 'profile_deleted']
        assert deleted and deleted[-1].get('reaped') is True

    def test_reap_detects_a_retry_started_after_a_finished_crash(self, tmp_path, sandboxes):
        """R3 MAJOR (b): ``last_finished is not None`` hid a live retry attempt."""
        key = 'v1/x/t1'
        execdir = new_execdir(sandboxes, key=key)
        env = H.subject_env(os.environ, execdir / 'home')
        configure(tmp_path, mode='ok', sleep=20)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        holder = {}

        def child():
            holder['proc'] = H._popen(
                [PY, str(FAKE), '--fake-home', str(tmp_path), '-p',
                 'lheval0123456789abcdef', 'chat'],
                cwd=str(execdir / 'work'), env=env, timeout=30,
                on_spawn=lambda pid: holder.__setitem__('pgid', pid))

        worker = threading.Thread(target=child)
        worker.start()
        deadline = time.time() + 10
        while 'pgid' not in holder and time.time() < deadline:
            time.sleep(0.01)
        pgid = holder['pgid']
        assert H._alive(pgid)
        ledger.append({'event': 'profile_created', 'key': key,
                       'profile': 'lheval0123456789abcdef', 'attempt': 1})
        owner = {'pid': os.getpid(), 'start': '1', 'boot': H.boot_id()}
        started = H.process_start(pgid)
        ledger.append({'event': 'started', 'key': key, 'attempt': 1, 'pgid': pgid,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(execdir),
                       'owner': owner, 'pgid_start': started})
        ledger.append({'event': 'finished', 'key': key, 'attempt': 1, 'status': 'hermes_crash',
                       'profile': 'lheval0123456789abcdef', 'correction': True})
        ledger.append({'event': 'started', 'key': key, 'attempt': 2, 'pgid': pgid,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(execdir),
                       'owner': owner, 'pgid_start': started})
        assert H.reap_stale(ledger) == [key], 'a live retry attempt was not reaped'
        assert not H._alive(pgid), 'the retry process group survived'
        assert not execdir.exists(), 'the retry sandbox survived'
        assert ledger.last_finished(key)['status'] == 'interrupted'
        worker.join(timeout=10)

    def test_owned_sandboxes_lists_only_canonical_recorded_dirs(self, tmp_path, sandboxes):
        key = 'v1/x/t1'
        execdir = new_execdir(sandboxes, key=key)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'attempt_dir', 'key': key, 'execdir': str(execdir)})
        ledger.append({'event': 'started', 'key': key,
                       'execdir': str(tmp_path / 'evil' / ('a' * 20))})
        ledger.append({'event': 'finished', 'key': key, 'execdir': str(tmp_path / ('b' * 20))})
        assert H.owned_sandboxes(ledger) == [execdir]


# --------------------------------------------------------------------------- #
# profile lifecycle (A8)
# --------------------------------------------------------------------------- #


class TestProfileLifecycle:
    def _ledger_with(self, tmp_path, name):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'profile_created', 'profile': name})
        return ledger

    def test_delete_refuses_default(self, tmp_path):
        ledger = self._ledger_with(tmp_path, 'lheval0123456789abcdef')
        with pytest.raises(ValueError):
            H.delete_profile(['hermes'], 'default', ledger, {})

    def test_delete_refuses_an_unrecorded_profile(self, tmp_path):
        ledger = self._ledger_with(tmp_path, 'lheval0123456789abcdef')
        with pytest.raises(ValueError):
            H.delete_profile(['hermes'], 'lhevalzzz', ledger, {})

    def test_delete_refuses_a_name_outside_the_pattern(self, tmp_path):
        ledger = self._ledger_with(tmp_path, 'lheval0123456789abcdef')
        with pytest.raises(ValueError):
            H.delete_profile(['hermes'], 'lheval-V11', ledger, {})

    def test_create_rejects_a_bad_name(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        with pytest.raises(ValueError):
            H.create_profile(['hermes'], 'Longhaul', ledger, {})

    def test_delete_is_idempotent_and_uses_the_fake(self, tmp_path, home):
        configure(home, mode='ok')
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        name = 'lhevalfeedfacefeedface'
        assert H.create_profile(fake_bin(home), name, ledger,
                                base_env(tmp_path)) == name
        assert H.delete_profile(fake_bin(home), name, ledger,
                                base_env(tmp_path)) is True
        assert H.delete_profile(fake_bin(home), name, ledger,
                                base_env(tmp_path)) is False
        assert ledger.deleted_profiles() == [name]
        assert ledger.live_profiles() == []


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #


class TestLedger:
    def test_rows_header_attempts_and_profiles(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'nested' / 'ledger.jsonl')
        assert ledger.rows() == [] and ledger.header() is None and ledger.last('k') is None
        ledger.append({'event': 'header', 'run_id': 'r1'})
        ledger.append({'event': 'profile_created', 'key': 'v1/x/t1',
                       'profile': 'lheval0123456789abcdef'})
        ledger.append({'event': 'started', 'key': 'v1/x/t1'})
        ledger.append({'event': 'finished', 'key': 'v1/x/t1', 'status': 'ok'})
        assert ledger.header()['run_id'] == 'r1'
        assert ledger.attempts('v1/x/t1') == 1
        assert ledger.last('v1/x/t1')['status'] == 'ok'
        assert ledger.created_profiles() == ['lheval0123456789abcdef']
        assert ledger.live_profiles() == ['lheval0123456789abcdef']
        for row in ledger.rows():
            assert row['v'] == 1 and 'at' in row

    def test_header_is_none_when_the_first_row_is_not_a_header(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'started', 'key': 'k'})
        assert ledger.header() is None

    def test_live_profiles_tracks_the_last_event_per_name(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'profile_created', 'profile': 'lhevalaaaaaaaaaaaaaaaa'})
        ledger.append({'event': 'profile_created', 'profile': 'lhevalbbbbbbbbbbbbbbbb'})
        ledger.append({'event': 'profile_deleted', 'profile': 'lhevalaaaaaaaaaaaaaaaa'})
        assert ledger.live_profiles() == ['lhevalbbbbbbbbbbbbbbbb']
        ledger.append({'event': 'profile_created', 'profile': 'lhevalaaaaaaaaaaaaaaaa'})
        assert ledger.live_profiles() == ['lhevalaaaaaaaaaaaaaaaa',
                                         'lhevalbbbbbbbbbbbbbbbb']

    def test_append_is_thread_safe(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')

        def worker(worker_id):
            for index in range(25):
                ledger.append({'event': 'row', 'worker': worker_id, 'index': index})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = ledger.rows()
        assert len(rows) == 200
        assert len({(row['worker'], row['index']) for row in rows}) == 200

    def test_run_key_format(self):
        assert L.run_key('v1.1', 'control-finish', 2) == 'v1.1/control-finish/t2'

    def test_pending_matrix(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        keys = ['a/x/t1', 'b/x/t1', 'c/x/t1', 'd/x/t1', 'e/x/t1']
        ledger.append({'event': 'finished', 'key': 'a/x/t1', 'status': 'ok'})
        ledger.append({'event': 'started', 'key': 'b/x/t1'})
        ledger.append({'event': 'finished', 'key': 'b/x/t1', 'status': 'provider_429'})
        ledger.append({'event': 'started', 'key': 'c/x/t1'})
        ledger.append({'event': 'finished', 'key': 'c/x/t1', 'status': 'provider_429'})
        ledger.append({'event': 'started', 'key': 'd/x/t1'})
        ledger.append({'event': 'finished', 'key': 'd/x/t1', 'status': 'preflight_fail'})
        ledger.append({'event': 'finished', 'key': 'e/x/t1', 'status': 'skipped_preflight'})
        assert L.pending(keys, ledger, max_attempts=2) == ['b/x/t1', 'c/x/t1', 'd/x/t1',
                                                           'e/x/t1']
        assert L.pending(keys, ledger, max_attempts=1) == ['d/x/t1', 'e/x/t1']

    def test_pending_keeps_preflight_failures_beyond_max_attempts(self, tmp_path):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'finished', 'key': 'a/x/t1', 'status': 'preflight_fail'})
        assert L.pending(['a/x/t1'], ledger, max_attempts=1) == ['a/x/t1']

    def test_check_header_detects_pin_drift(self, tmp_path):
        header = {'v': 1, 'event': 'header', 'run_id': 'r1', 'created_at': 'now',
                  'repo_head': 'af52cd8', 'hermes_version': '0.21.6',
                  'model': H.SUBJECT[0], 'provider': H.SUBJECT[1],
                  'judge_model': H.JUDGE[0], 'trials': 2, 'oracle_sha': 'aa',
                  'conditions': {'baseline': {'commit': None, 'version': None,
                                              'skill_fp': None},
                                 'v1': {'commit': 'b8b7d8c', 'version': '1.0',
                                        'skill_fp': 'aa'}},
                  'max_turns': 60, 'budget_s': 600}
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        assert L.check_header(ledger, header) == ['header: the ledger has no header row']
        ledger.append(header)
        assert L.check_header(ledger, dict(header)) == []
        for field, value, needle in (('hermes_version', '0.99.0', 'hermes_version'),
                                     ('oracle_sha', 'bb', 'oracle_sha'),
                                     ('model', 'someone-else', 'model')):
            drifted = json.loads(json.dumps(header))
            drifted[field] = value
            assert any(needle in problem for problem in L.check_header(ledger, drifted))
        drifted = json.loads(json.dumps(header))
        drifted['conditions']['v1']['skill_fp'] = 'bb'
        assert any('conditions.v1.skill_fp' in problem
                   for problem in L.check_header(ledger, drifted))
        drifted = json.loads(json.dumps(header))
        drifted['conditions']['v1.1'] = {'commit': 'af52cd8', 'version': '1.1',
                                         'skill_fp': 'cc'}
        assert any('conditions.v1.1' in problem
                   for problem in L.check_header(ledger, drifted))


class TestRunMatrix:
    def test_runner_reexports_the_ledger_surface(self):
        assert H.Ledger is L.Ledger
        assert H.check_header is L.check_header
        assert H.run_key is L.run_key
        assert H.pending is L.pending
        assert callable(H.run_matrix)

    def _specs(self, tmp_path, sandboxes, count=2):
        return [ready_spec(tmp_path, sandboxes, cond='v1', case='case', trial=i + 1)
                for i in range(count)]

    def test_retryable_statuses_are_retried_with_backoff(self, tmp_path, sandboxes):
        specs = self._specs(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        seen, sleeps = {}, []
        guard = threading.Lock()

        def run(spec):
            key = L.run_key(spec.cond, spec.case, spec.trial)
            with guard:
                seen[key] = seen.get(key, 0) + 1
                attempt = seen[key]
            ledger.append({'event': 'started', 'key': key})
            ledger.append({'event': 'finished', 'key': key,
                           'status': 'provider_429' if attempt == 1 else 'ok'})

        outcome = L.run_matrix(specs, run, ledger, parallel=2, max_attempts=2,
                               backoff_s=60, sleep=sleeps.append)
        statuses = {key: value['status'] for key, value in outcome.items()
                    if key != '_summary'}
        assert sorted(statuses.values()) == ['ok', 'ok']
        assert all(count == 2 for count in seen.values())
        assert sleeps == [60]
        assert outcome['_summary']['rounds'] == 2
        assert outcome['_summary']['done'] is True

    def test_ok_keys_are_not_rerun_on_resume(self, tmp_path, sandboxes):
        specs = self._specs(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        done = L.run_key('v1', 'case', 1)
        ledger.append({'event': 'started', 'key': done})
        ledger.append({'event': 'finished', 'key': done, 'status': 'ok'})
        ran = []
        L.run_matrix(specs, lambda spec: ran.append(L.run_key(spec.cond, spec.case,
                                                              spec.trial)), ledger,
                     parallel=2, max_attempts=2, sleep=lambda _s: None)
        assert ran == ['v1/case/t2']

    def test_max_attempts_caps_the_retries(self, tmp_path, sandboxes):
        specs = self._specs(tmp_path, sandboxes, count=1)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        calls_count = []
        sleeps = []
        key = L.run_key('v1', 'case', 1)

        def run(spec):
            calls_count.append(1)
            ledger.append({'event': 'started', 'key': key})
            ledger.append({'event': 'finished', 'key': key, 'status': 'provider_429'})

        outcome = L.run_matrix(specs, run, ledger, parallel=1, max_attempts=2,
                               backoff_s=60, sleep=sleeps.append)
        assert len(calls_count) == 2
        assert sleeps == [60]
        assert outcome[key]['status'] == 'provider_429'
        assert outcome['_summary']['errors'] == {'provider_429': 1}
        assert outcome[key]['attempts'] == 2

    def test_preflight_failures_are_not_retried_inside_one_invocation(self, tmp_path,
                                                                     sandboxes):
        specs = self._specs(tmp_path, sandboxes, count=1)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        calls_count = []
        key = L.run_key('v1', 'case', 1)

        def run(spec):
            calls_count.append(1)
            ledger.append({'event': 'finished', 'key': key, 'status': 'preflight_fail'})

        outcome = L.run_matrix(specs, run, ledger, parallel=1, max_attempts=2,
                               sleep=lambda _s: None)
        assert len(calls_count) == 1
        assert outcome['_summary']['pending'] == [key]
        assert outcome['_summary']['done'] is False

    def test_parallelism_is_bounded(self, tmp_path, sandboxes):
        specs = self._specs(tmp_path, sandboxes, count=6)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        state = {'live': 0, 'peak': 0}
        guard = threading.Lock()

        def run(spec):
            key = L.run_key(spec.cond, spec.case, spec.trial)
            with guard:
                state['live'] += 1
                state['peak'] = max(state['peak'], state['live'])
            time.sleep(0.02)
            with guard:
                state['live'] -= 1
            ledger.append({'event': 'started', 'key': key})
            ledger.append({'event': 'finished', 'key': key, 'status': 'ok'})

        outcome = L.run_matrix(specs, run, ledger, parallel=2, max_attempts=2,
                               sleep=lambda _s: None)
        assert state['peak'] <= 2
        assert len(outcome['_summary']['ran']) == 6
        assert outcome['_summary']['rounds'] == 1


# --------------------------------------------------------------------------- #
# the fake itself
# --------------------------------------------------------------------------- #


class TestFake:
    def test_plain_text_without_stream_json_and_json_with_it(self, tmp_path):
        home = tmp_path / 'fakehome'
        home.mkdir()
        (home / '.fake_hermes.json').write_text(json.dumps(
            {'mode': 'ok', 'reply': '<SCORES>{}</SCORES>'}), encoding='utf-8')
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin'}
        plain = subprocess.run([PY, str(FAKE), '--fake-home', str(home), 'chat',
                                '--query-file', str(tmp_path / 'q')],
                               capture_output=True, text=True, env=env)
        assert plain.stdout.strip() == '<SCORES>{}</SCORES>'
        streamed = subprocess.run([PY, str(FAKE), '--fake-home', str(home), 'chat',
                                   '--query-file', str(tmp_path / 'q'),
                                   '--format', 'stream-json'],
                                  capture_output=True, text=True, env=env)
        first = json.loads(streamed.stdout.splitlines()[0])
        assert first['type'] == 'system'
        assert json.loads(streamed.stdout.splitlines()[-1])['type'] == 'result'

    def test_state_lives_in_the_fake_home_not_the_process_home(self, tmp_path):
        host = tmp_path / 'host'
        fake_home = tmp_path / 'fake'
        host.mkdir()
        fake_home.mkdir()
        (fake_home / '.fake_hermes.json').write_text(
            json.dumps({'profile_extra': {'marker.txt': 'x'}}), encoding='utf-8')
        env = {'HOME': str(host), 'PATH': '/usr/bin:/bin'}
        proc = subprocess.run([PY, str(FAKE), '--fake-home', str(fake_home),
                               'profile', 'create', 'lheval0123456789abcdef'],
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0
        assert (host / '.hermes' / 'profiles' / 'lheval0123456789abcdef').is_dir()
        assert (fake_home / 'fake_calls.jsonl').is_file()


# --------------------------------------------------------------------------- #
# R4: symlink-safe snapshots, attempt-scoped reaping, owner checks, skill proof
# --------------------------------------------------------------------------- #

def _fake_stat(pid, start):
    """A /proc/<pid>/stat line: field 22 (starttime) is what the owner check reads."""
    return ('%d (bwrap) ' % pid) + ' '.join(['0'] * 18) + ' 0 %d\n' % start


class TestR4HostProcessing:
    def test_manifest_never_reads_through_a_subject_symlink(self, tmp_path):
        """R4 BLOCKER: the manifest hashed a host file behind a work-tree symlink."""
        work = tmp_path / 'work'
        (work / '.task').mkdir(parents=True)
        host = tmp_path / 'host-private.json'
        host.write_text('{"host_private": "CANARY-HOST-FILE-742"}', encoding='utf-8')
        (work / '.task' / 'state.json').symlink_to(host)
        snap = H.manifest(work)
        assert snap['.task/state.json'] != hashlib.sha256(host.read_bytes()).hexdigest()
        assert snap['.task/state.json'].startswith('link:'), snap

        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('CANARY-PARENT-LINK\n', encoding='utf-8')
        (work / 'linked').symlink_to(outside, target_is_directory=True)
        snap = H.manifest(work)
        assert snap.get('linked/') and snap['linked/'].startswith('link:'), snap
        assert 'linked/secret.txt' not in snap

        # a legitimate regular file is still hashed (positive control)
        (work / 'real.txt').write_text('plain\n', encoding='utf-8')
        assert H.manifest(work)['real.txt'] == hashlib.sha256(b'plain\n').hexdigest()

    def test_no_follow_reader_refuses_links_devices_and_fifos(self, tmp_path):
        """The primitive every artifact read goes through: regular files only."""
        target = tmp_path / 'target.txt'
        target.write_text('host bytes\n', encoding='utf-8')
        link = tmp_path / 'link.txt'
        link.symlink_to(target)
        assert H._read_regular(link) is None
        assert H._read_text(link) == ''
        fifo = tmp_path / 'pipe'
        os.mkfifo(fifo)
        assert H._read_regular(fifo) is None        # O_NONBLOCK: no hang, no read
        assert H._read_regular(tmp_path) is None    # directories are not files
        # parent symlink: a legitimately named file below an untrusted directory
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'state.json').write_text('{"host": "CANARY-PARENT-77"}')
        parent_link = tmp_path / 'linked'
        parent_link.symlink_to(outside, target_is_directory=True)
        assert H._read_regular(parent_link / 'state.json') is None
        # positive control
        assert H._read_regular(target) == b'host bytes\n'

    def test_state_before_is_not_copied_through_a_link(self, tmp_path, home, sandboxes):
        """R4 BLOCKER (other consumer): state.before.json used shutil.copyfile."""
        configure(home, mode='ok', reply='done\nFINAL: complete')
        spec = ready_spec(tmp_path, sandboxes, case='control-finish')
        work = Path(spec.execdir) / 'work'
        host = tmp_path / 'host-state.json'
        host.write_text('{"host_private": "CANARY-HOST-STATE-55"}', encoding='utf-8')
        (work / '.task').mkdir(parents=True, exist_ok=True)
        (work / '.task' / 'state.json').symlink_to(host)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, fake_bin(home), ledger, home, None, bwrap=False)
        # Either the preflight refuses the linked state or the run proceeds; in no
        # case may the host bytes be copied into the result directory.
        assert meta['status'] in ('ok', 'preflight_fail'), meta
        copied = Path(spec.rundir) / 'state.before.json'
        assert not copied.exists() or 'CANARY-HOST-STATE-55' not in copied.read_text()


class TestR4ReapOwnership:
    def _ledger(self, tmp_path, row):
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append(row)
        return ledger

    def test_reap_never_signals_a_process_without_a_verifiable_dead_owner(
            self, tmp_path, monkeypatch):
        """R4 MAJOR: a synthetic/unknown owner must never be signalled."""
        key = 'v1/x/t1'
        proc_root = tmp_path / 'proc'
        proc = proc_root / '12345'
        proc.mkdir(parents=True)
        (proc / 'cmdline').write_bytes(b'bwrap\0lheval0123456789abcdef\0')
        (proc / 'stat').write_text(_fake_stat(12345, 4242))
        (proc_root / 'sys' / 'kernel' / 'random').mkdir(parents=True)
        (proc_root / 'sys' / 'kernel' / 'random' / 'boot_id').write_text('test-boot\n')
        kills = []
        monkeypatch.setattr(H, '_alive', lambda pid: int(pid) == 12345)
        monkeypatch.setattr(H, '_kill_pid_group', lambda pgid: kills.append(int(pgid)))
        row = {'event': 'started', 'key': key, 'attempt': 1, 'pgid': 12345,
               'profile': 'lheval0123456789abcdef',
               'execdir': str(tmp_path / 'not-canonical' / ('a' * 20))}

        unknown = self._ledger(tmp_path, dict(row))
        assert H.reap_stale(unknown, proc_root) == [key]
        assert kills == [], 'a process with no recorded owner was signalled'

        live = self._ledger(tmp_path / 'live', dict(
            row, owner={'pid': 12345, 'start': '4242', 'boot': 'test-boot'},
            pgid_start='4242'))
        assert H.reap_stale(live, proc_root) == []
        assert kills == [], 'the attempt of a live owner was signalled'

        # positive control: a provably dead owner with a matching process group
        stale = self._ledger(tmp_path / 'stale', dict(
            row, owner={'pid': 999999, 'start': '7', 'boot': 'test-boot'},
            pgid_start='4242'))
        assert H.reap_stale(stale, proc_root) == [key]
        assert kills == [12345], 'the stale attempt was not reaped'

    def test_a_retry_attempt_killed_before_started_is_reaped(self, tmp_path, sandboxes):
        """R4 MAJOR: attempt_dir ownership is per (key, attempt), not per key."""
        key = 'v1/x/t1'
        first = new_execdir(sandboxes, key=key, attempt=1)
        second = new_execdir(sandboxes, key=key, attempt=2)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        ledger.append({'event': 'attempt_dir', 'key': key, 'attempt': 1,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(first)})
        ledger.append({'event': 'started', 'key': key, 'attempt': 1,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(first),
                       'pgid': 999999})
        ledger.append({'event': 'finished', 'key': key, 'attempt': 1,
                       'status': 'hermes_crash'})
        ledger.append({'event': 'attempt_dir', 'key': key, 'attempt': 2,
                       'profile': 'lheval0123456789abcdef', 'execdir': str(second)})
        assert (second / 'home' / '.hermes' / '.env').is_file()
        shutil.rmtree(first, ignore_errors=True)
        rows = ledger.rows()
        pending_attempts = H._unstarted_attempts(rows)
        assert [(k, row['attempt']) for k, row in pending_attempts] == [(key, 2)]
        assert H.reap_stale(ledger) == [key]
        assert not second.exists(), 'the retry sandbox with the canary was not reaped'


class TestR4SkillEvidence:
    def test_skill_view_confirmation_requires_a_successful_result(self):
        """R4 MAJOR: an invocation whose result is an error is not a confirmation."""
        call_id = 'call-1'
        use = {'type': 'tool_use', 'name': 'skill_view',
               'input': {'name': 'longhaul'}, 'tool_call_id': call_id}
        failed = [use, {'type': 'tool_result', 'tool_call_id': call_id,
                        'output': 'permission denied', 'is_error': True}]
        assert H.skill_confirmation(failed) is False
        succeeded = [use, {'type': 'tool_result', 'tool_call_id': call_id,
                           'output': 'loaded skill longhaul', 'is_error': False}]
        assert H.skill_confirmation(succeeded) is True
        # the subject's own system event is self-reported, never a confirmation
        assert H.skill_confirmation([{'type': 'system', 'skills': ['longhaul']}]) is False


# --------------------------------------------------------------------------- #
# R6: explicit modes, key-free runner API, runner-owned offline plan
# --------------------------------------------------------------------------- #

class TestR6Modes:
    def test_no_runner_api_reads_a_sandbox_key(self, tmp_path, home, sandboxes,
                                               monkeypatch):
        """R6: no runner API accepts, reads or copies key material any more."""
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        reads = []

        def forbidden(*args, **kwargs):
            reads.append(args)
            raise KeyError('OLLAMA_API_KEY: no uncommented assignment')

        monkeypatch.setattr(H, 'read_key_line', forbidden, raising=False)
        monkeypatch.setattr(H, '_sandbox_key',
                            lambda *a, **k: reads.append('_sandbox_key'), raising=False)
        meta = H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'), bwrap=False)
        assert meta['status'] == 'ok', meta
        assert reads == [], 'a runner API read or accepted provider key material'

    def test_live_mode_is_refused_before_profile_key_or_exec(self, tmp_path, home,
                                                             sandboxes, monkeypatch):
        """R6 mutant 9 guard: a direct Python call cannot reach the live lane."""
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        reads = []
        monkeypatch.setattr(H, 'read_key_line',
                            lambda *a, **k: reads.append(a), raising=False)
        with pytest.raises(H.LiveBlocked):
            H.run_one(spec, fake_bin(home), ledger, home, tree('1.1'),
                      mode=H.MODE_LIVE_BLOCKED, state=home)
        assert reads == [], 'the blocked lane read a key'
        assert ledger.rows() == [], 'the blocked lane wrote a ledger row'
        assert calls(home) == [], 'the blocked lane executed a process'
        assert not (Path(spec.execdir) / 'home' / '.hermes' / 'profiles').exists(), \
            'the blocked lane created a profile'
        assert not (spec.rundir / 'meta.json').exists(), 'the blocked lane wrote a run record'

    def test_offline_mode_secures_the_entry_with_the_runner_plan(self, tmp_path, home,
                                                                 sandboxes):
        """R6 mutant 7 guard: the offline lane is confined even without a boolean flag.

        ``bwrap=None`` is the runner default (BWRAP); the plan is built by the
        runner, the child reports the sandbox HOME and a bwrap PID 1, and the
        network namespace is unshared. A test seam must pass ``bwrap=False``
        explicitly; nothing here is inferred from a file name.
        """
        configure(home, mode='ok', reply='done\nFINAL: complete')
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        state = tmp_path / 'state'
        state.mkdir()
        meta = H.run_one(spec, live_entry(home), ledger, home, tree('1.1'), state=state)
        assert meta['status'] == 'ok', meta
        cmd = json.loads((spec.rundir / 'cmd.json').read_text())
        assert cmd['wrapped'] is True and Path(cmd['argv'][0]).name == 'bwrap'
        assert '--unshare-net' in cmd['argv']
        isolation = re.search(r'^ISOLATION home=(\S+) cwd=(\S+) argv0=(\S+) pid1=(.*)$',
                              (spec.rundir / 'stderr.txt').read_text(), re.M)
        assert isolation, 'the child never ran'
        assert isolation.group(1).startswith(str(H.SUBJECT_ROOT)), isolation.group(1)
        assert 'bwrap' in isolation.group(4), isolation.group(4)[:80]

    def test_an_arbitrary_entry_is_refused_before_any_process(self, tmp_path, home,
                                                              sandboxes):
        """R6 mutant 8 guard: only the verified double, never an arbitrary script."""
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        rogue = tmp_path / 'rogue.py'
        rogue.write_text('# just a script\n', encoding='utf-8')
        for argv in ([str(rogue)], [PY, str(rogue)],
                     [str(rogue), '--fake-home', str(home)]):
            with pytest.raises(H.DoubleRejected):
                H.run_one(spec, argv, ledger, home, tree('1.1'), bwrap=False)
            assert calls(home) == []
        assert ledger.rows() == [], 'a refused entry wrote a ledger row'


# --------------------------------------------------------------------------- #
# R7: whole-file entry identity, the executed copy, the probe and the judge
# --------------------------------------------------------------------------- #

def double_copy(path, shebang=b'#!/usr/bin/env python3\n'):
    """The repository double's body under a chosen first line."""
    body = FAKE.read_bytes().split(b'\n', 1)[1]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(shebang + body)
    path.chmod(0o755)
    return path


def clean_state(tmp_path, name='state'):
    state = tmp_path / name
    state.mkdir(exist_ok=True)
    (state / 'notes.txt').write_text('nothing secret here\n', encoding='utf-8')
    return state


class TestR7Identity:
    def test_a_forged_shebang_is_refused_before_any_process(self, tmp_path, home,
                                                            sandboxes):
        """R7: the first line is an allow-list, not something to strip away."""
        configure(home, mode='ok')
        state = clean_state(tmp_path)
        forged = double_copy(Path(home).parent / 'forged.py', b'#!/bin/sh\n')
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        for argv in ([str(forged)], [PY, str(forged)]):
            with pytest.raises(H.DoubleRejected):
                H.run_one(spec, argv, ledger, home, tree('1.1'), bwrap=False)
            assert calls(home) == [], 'a forged shebang executed'
        assert H.offline_gate([str(forged)], state)[1], 'a forged shebang passed the gate'
        # positive control: the same body under an allowed python3 shebang
        shown = double_copy(Path(home).parent / 'shown.py', b'#!/usr/bin/python3\n')
        assert H.offline_gate([str(shown)], state)[1] == []
        assert H.double_digest([str(shown)]) == H.double_digest([str(FAKE)])

    def test_a_python_named_temp_elf_is_not_an_interpreter(self, tmp_path):
        """R7: a ``python3``-named ELF outside /usr is refused as an interpreter."""
        entry = double_copy(tmp_path / 'entry.py')
        elf = tmp_path / 'python3'
        shutil.copyfile('/usr/bin/true', elf)
        elf.chmod(0o755)
        assert H._read_prefix(elf, 4) == b'\x7fELF'
        with pytest.raises(H.DoubleRejected):
            H.canonical_double([str(elf), str(entry)])
        # positive control: the running interpreter is always accepted; the
        # owner/mode rule for a bundled /usr interpreter is tested semantically
        # (an unprivileged container maps root to another uid, so the host's
        # stat cannot be the proof).
        assert H.canonical_double([PY, str(entry)]) == [PY, str(entry)]
        runner = Path(os.path.realpath(sys.executable))
        assert H.interpreter_trust_problem(Path('/usr/bin/python3'), runner, 0, 0o755) is None
        assert 'root-owned' in H.interpreter_trust_problem(
            Path('/usr/bin/python3'), runner, 65534, 0o755)
        assert 'under /usr' in H.interpreter_trust_problem(
            Path('/opt/python3'), runner, 0, 0o755)
        assert 'world-writable' in H.interpreter_trust_problem(
            Path('/usr/bin/python3'), runner, 0, 0o777)

    def test_bwrap_false_needs_the_explicit_test_seam(self, tmp_path, home, sandboxes,
                                                      monkeypatch):
        """R7: the deterministic seam is an environment decision, never inferred."""
        assert H.TEST_SEAM_ENV == 'LONGHAUL_EVAL_TEST_SEAM'
        configure(home, mode='ok')
        spec = ready_spec(tmp_path, sandboxes, cond='baseline', key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        monkeypatch.delenv(H.TEST_SEAM_ENV, raising=False)
        with pytest.raises(H.LauncherError):
            H.run_one(spec, fake_bin(home), ledger, home, None, bwrap=False)
        assert calls(home) == [], 'a process ran without the explicit seam'
        assert not (spec.rundir / 'cmd.json').exists()
        monkeypatch.setenv(H.TEST_SEAM_ENV, '1')
        meta = H.run_one(spec, fake_bin(home), ledger, home, None, bwrap=False)
        assert meta['status'] == 'ok', meta


class TestR7Probe:
    def test_offline_probe_uses_the_plan_and_records_three_canaries(self, tmp_path,
                                                                    sandboxes):
        """R7: the offline probe passes the plan arguments in the documented order."""
        assert BWRAP_OK, 'bwrap and /usr/bin/python3 are hard prerequisites'
        host = tmp_path / 'host'
        host.mkdir()
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        execdir = H.neutral_execdir('probe/baseline', 1)
        sandboxes.append(execdir)
        record = H.run_probe([str(entry)], host, execdir, label='baseline', state=state)
        assert record['problems'] == [], record['problems']
        assert record['network'] is False
        assert set(record['canaries']) == {'host', 'sock', 'net'}
        assert all(record['canaries'][key] for key in ('host', 'sock', 'net')), \
            record['canaries']
        assert 'bwrap' in str(record['facts'].get('pid1') or '')
        assert record['facts'].get('home') == str(Path(execdir) / 'home')
        # the canaries are host-side objects and are removed again
        assert not Path(record['canaries']['host']).exists()
        assert not Path(record['canaries']['sock']).exists()

    def test_probe_canaries_are_required_and_disabled_is_dirty(self, tmp_path, sandboxes):
        """R7 mutant X5: no default, no ``None``-pass, no silent disable."""
        facts = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                          '/run/user': {'missing': False, 'entries': []},
                          str(H.SUBJECT_ROOT): {'missing': False,
                                                'entries': ['a' * 20]}},
                 'find': [], 'targets': {'/usr/bin/python3': 'ok'}, 'mounts': [],
                 'pid1': '/usr/bin/bwrap', 'cwd': '/x/work', 'home': '/x/home',
                 'find_rc': 0}
        execdir = H.SUBJECT_ROOT / ('a' * 20)
        with pytest.raises(TypeError):
            H.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0)
        problems = H.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                    host_canary=None, socket_canary='s', net_canary='n')
        assert any('host canary was declared' in item for item in problems), problems

        assert BWRAP_OK
        host = tmp_path / 'host'
        host.mkdir()
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        execdir = H.neutral_execdir('probe/candidate', 1)
        sandboxes.append(execdir)
        record = H.run_probe([str(entry)], host, execdir, label='baseline',
                             state=state, canaries=False)
        assert record['problems'], 'canaries=False was accepted as a clean probe'
        assert any('canar' in item.lower() for item in record['problems']), \
            record['problems']
        assert set(record['canaries']) == {'host', 'sock', 'net'}

    def test_a_failed_canary_creation_makes_the_probe_dirty(self, tmp_path, sandboxes,
                                                            monkeypatch):
        """R7: a canary that cannot be created is a dirty probe, never a pass."""
        assert BWRAP_OK
        host = tmp_path / 'host'
        host.mkdir()
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        execdir = H.neutral_execdir('probe/dirty', 1)
        sandboxes.append(execdir)

        def boom(*args, **kwargs):
            raise OSError('AF_UNIX path too long or no space')

        monkeypatch.setattr(H.tempfile, 'mkdtemp', boom)
        record = H.run_probe([str(entry)], host, execdir, label='baseline',
                             state=state)
        assert record['problems'], 'a failed canary creation was accepted'
        assert any('canar' in item.lower() for item in record['problems']), \
            record['problems']
        assert record['canaries'] == {'host': None, 'sock': None, 'net': None}


class TestR7KeyScan:
    def test_a_state_too_large_to_prove_key_free_is_refused(self, tmp_path, monkeypatch):
        """R8 K1/EV4: caps and the total budget refuse; no per-file cap exists."""
        assert H.offline_state_problem(clean_state(tmp_path)) is None
        many = tmp_path / 'many'
        many.mkdir()
        for index in range(H.STATE_SCAN_FILES + 1):
            (many / ('f%03d.txt' % index)).write_text('x\n', encoding='utf-8')
        assert 'too many files' in (H.offline_state_problem(many) or ''), \
            'a state beyond the file cap was accepted'
        monkeypatch.setattr(H, 'STATE_SCAN_TOTAL', 1024)
        budget = clean_state(tmp_path, 'budget')
        (budget / 'big.txt').write_text('x' * 4096, encoding='utf-8')
        assert 'too large' in (H.offline_state_problem(budget) or ''), \
            'a state beyond the read budget was accepted'

    def test_widened_key_names_are_refused(self, tmp_path):
        """R7: any ``*API_KEY``/``*TOKEN``/``*SECRET`` assignment is key material."""
        for name in ('GEMINI_API_KEY=abc', 'XAI_TOKEN=abc', 'APP_SECRET=abc',
                     'MY_SERVICE_API_KEY = "abc"', 'DEEPSEEK_API_KEY=abc'):
            folder = tmp_path / name.split('=')[0].strip().lower().replace(' ', '_')
            folder.mkdir()
            (folder / 'notes.env').write_text('export ' + name + '\n', encoding='utf-8')
            assert H.offline_state_problem(folder), '%s was accepted' % name
        assert H.offline_state_problem(clean_state(tmp_path, 'clean2')) is None


class TestR7Judge:
    def test_judge_runs_in_the_plan_with_a_neutral_home(self, tmp_path, monkeypatch):
        """R7 mutant X1: the judge goes through the runner plan, never os.environ."""
        assert BWRAP_OK
        host = tmp_path / 'home'
        host.mkdir()
        state = clean_state(tmp_path)
        entry = double_copy(host / '.local' / 'bin' / 'hermes')
        seen = []
        real = H.neutral_execdir

        def record(key, attempt, root=None):
            path = real(key, attempt, root=root)
            seen.append(path)
            return path

        monkeypatch.setattr(H, 'neutral_execdir', record)
        proc = H.run_judge([str(entry), '--fake-home', str(state)], state,
                           'Reply with exactly: OK', host_home=host)
        assert proc.rc == 0, proc.stderr[-400:]
        assert 'Done.' in proc.stdout
        match = re.search(r'^ISOLATION home=(\S+) cwd=(\S+) argv0=(\S+) pid1=(.*)$',
                          proc.stderr, re.M)
        assert match, 'the judge child never reported its facts'
        assert match.group(1).startswith(str(H.SUBJECT_ROOT) + '/'), match.group(1)
        assert match.group(2).startswith(str(H.SUBJECT_ROOT) + '/'), match.group(2)
        assert 'bwrap' in match.group(4), match.group(4)[:80]
        assert seen and not seen[0].exists(), 'the judge sandbox was not cleaned up'
        assert not (host / 'fake_calls.jsonl').exists(), \
            'the judge wrote into the harness HOME'
        # R8: the double's own call log lives in the attempt, never in the bound
        # state (the tiny chat counter is the double's input state and stays).
        assert not (state / 'fake_calls.jsonl').exists(), \
            'the judge wrote its call log into the bound state'


# --------------------------------------------------------------------------- #
# R8: one immutable verified token, a complete key scan, the host bus canary
# --------------------------------------------------------------------------- #

class TestR8Token:
    def test_a_swap_after_the_gate_never_reaches_the_attempt(self, tmp_path):
        """R8 T1: the copy comes from the repository bytes, not the caller path."""
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        token = H.verified_double([str(entry), '--fake-home', str(state)], state)
        assert token.digest == H.repo_double_digest()
        assert token.repository_bytes == H.canonical_double_bytes(FAKE.read_bytes())
        evil = tmp_path / 'evil.py'
        evil.write_bytes(b'#!/usr/bin/python3\nprint("EVIL-MARKER")\n')
        swapped = tmp_path / 'swapped.py'
        swapped.write_bytes(evil.read_bytes())
        os.replace(swapped, entry)                      # the caller's file changed
        execdir = tmp_path / 'exec'
        execdir.mkdir()
        exec_argv, digest, copy = H._attempt_double(execdir, token)
        assert digest == token.digest
        assert Path(copy).read_bytes() == token.repository_bytes, \
            'the attempt copied bytes that were not the verified repository bytes'
        assert b'EVIL-MARKER' not in Path(copy).read_bytes()
        ran = subprocess.run(exec_argv[:2] + ['--version'], capture_output=True, text=True)
        assert 'EVIL-MARKER' not in ran.stdout
        assert 'hermes' in ran.stdout.lower(), ran.stdout

    def test_a_swapped_repository_file_cannot_change_the_token(self, tmp_path,
                                                               monkeypatch):
        """The token holds the bytes: a later repository edit is not executed."""
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        token = H.verified_double([str(entry), '--fake-home', str(state)], state)
        evil = tmp_path / 'evil-repo.py'
        evil.write_bytes(b'#!/usr/bin/python3\nprint("EVIL-REPO")\n')
        monkeypatch.setattr(H, 'repo_double_path', lambda: evil)
        execdir = tmp_path / 'exec'
        execdir.mkdir()
        _argv, digest, copy = H._attempt_double(execdir, token)
        assert digest == token.digest
        assert b'EVIL-REPO' not in Path(copy).read_bytes()

    def test_run_one_survives_a_caller_swap_right_after_the_gate(self, tmp_path, home,
                                                                 sandboxes, monkeypatch):
        """R8 T1 over the real run path: swapping the caller executes nothing foreign."""
        configure(home, mode='ok')
        state = clean_state(tmp_path)
        entry = double_copy(Path(home) / '.local' / 'bin' / 'hermes')
        marker = tmp_path / 'EVIL-RAN'
        evil = tmp_path / 'evil-script.py'
        evil.write_text('#!/usr/bin/python3\nopen(%r, "w").write("evil")\n' % str(marker),
                        encoding='utf-8')
        real = H.verified_double

        def swap_after_gate(argv, state_dir=None):
            token = real(argv, state_dir)
            os.replace(evil, entry)                     # swap immediately after the gate
            return token

        monkeypatch.setattr(H, 'verified_double', swap_after_gate)
        spec = ready_spec(tmp_path, sandboxes, key_line=None)
        ledger = L.Ledger(tmp_path / 'ledger.jsonl')
        meta = H.run_one(spec, [str(entry), '--fake-home', str(state)], ledger, home,
                         tree('1.1'), state=state, bwrap=False)
        assert meta['status'] == 'ok', meta
        assert not marker.exists(), 'the swapped caller script executed'
        cmd = json.loads((spec.rundir / 'cmd.json').read_text())
        assert Path(cmd['inner'][1]).name == 'subject.py', cmd['inner']
        assert str(entry) not in cmd['argv'] and str(entry) not in cmd['inner']


class TestR8KeyScan:
    def test_a_large_harmless_state_is_scanned_completely(self, tmp_path):
        """R8 MAJOR: no per-file cap; the double's own growing log is harmless."""
        state = clean_state(tmp_path)
        log = state / 'fake_calls.jsonl'
        log.write_text('{"argv": ["hermes", "--version"], "cwd": "/x"}\n' * 2400,
                       encoding='utf-8')
        assert log.stat().st_size > 96 * 1024, log.stat().st_size
        assert H.offline_state_problem(state) is None, \
            'a harmless 96 KiB state was refused'

    def test_a_key_on_a_block_boundary_is_found(self, tmp_path):
        """R8 K2: adjacent blocks are scanned with an overlap."""
        state = clean_state(tmp_path)
        block = H.STATE_SCAN_BLOCK
        payload = b'x' * (block - 12) + b'GITHUB_TOKEN=' + b'value123\n'
        (state / 'boundary.bin').write_bytes(payload)
        problem = H.offline_state_problem(state)
        assert problem and 'GITHUB_TOKEN' in problem, \
            'a key split across a block boundary was missed: %r' % (problem,)

    def test_a_key_after_the_old_prefix_is_found(self, tmp_path):
        state = clean_state(tmp_path)
        (state / 'big.txt').write_text('x' * (100 * 1024) + '\nGITHUB_TOKEN=ghp_canary\n',
                                       encoding='utf-8')
        assert H.offline_state_problem(state), 'a key after 64 KiB was not found'

    def test_unreadable_entries_cannot_prove_key_free(self, tmp_path):
        state = clean_state(tmp_path)
        secret = state / 'secret.env'
        secret.write_text('GITHUB_TOKEN=ghp_hidden\n', encoding='utf-8')
        secret.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                secret.read_text(encoding='utf-8')
        finally:
            pass
        try:
            assert H.offline_state_problem(state), 'a mode-000 file was skipped'
        finally:
            secret.chmod(0o600)

    def test_an_unreadable_directory_cannot_prove_key_free(self, tmp_path):
        state = clean_state(tmp_path)
        folder = state / 'locked'
        folder.mkdir()
        (folder / 'x.env').write_text('GITHUB_TOKEN=ghp_hidden\n', encoding='utf-8')
        folder.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                list(folder.iterdir())
            assert H.offline_state_problem(state), 'a mode-000 directory was skipped'
        finally:
            folder.chmod(0o700)

    def test_utf16_yaml_and_json_keys_are_found(self, tmp_path):
        cases = {
            'utf16le-bom': b'\xff\xfe' + 'GITHUB_TOKEN=ghp_utf16\n'.encode('utf-16-le'),
            'utf16le': 'API_KEY=abcdef\n'.encode('utf-16-le'),
            'yaml': b'service:\n  api_key: abcdef\n',
            'json': b'{"github_token": "abcdef"}\n',
            'quoted-json': b'{"XAI_API_KEY": "abcdef", "other": 1}\n',
            'spaced': b'XAI_TOKEN = "abcdef"\n',
        }
        for name, payload in cases.items():
            folder = tmp_path / name
            folder.mkdir()
            (folder / 'notes.txt').write_bytes(payload)
            assert H.offline_state_problem(folder), '%s was accepted' % name

    def test_max_tokens_is_not_a_provider_key(self, tmp_path):
        state = clean_state(tmp_path)
        (state / 'config.json').write_text(
            '{"max_tokens": 4096, "max_tokens_per_request": 4096}\n', encoding='utf-8')
        (state / 'note.txt').write_text('max_tokens=4096\n', encoding='utf-8')
        assert H.offline_state_problem(state) is None, 'a token limit was refused'

    def test_sixty_five_siblings_and_deep_nesting_are_refused(self, tmp_path):
        wide = tmp_path / 'wide'
        wide.mkdir()
        for index in range(65):
            (wide / ('d%02d' % index)).mkdir()
        assert 'too' in (H.offline_state_problem(wide) or ''), '65 siblings were accepted'
        deep = tmp_path / 'deep'
        node = deep
        for index in range(65):
            node = node / 'n'
            node.mkdir(parents=True)
        assert 'too' in (H.offline_state_problem(deep) or ''), '65 levels were accepted'

    def test_a_symlink_or_special_entry_cannot_prove_key_free(self, tmp_path):
        outside = tmp_path / 'outside.txt'
        outside.write_text('GITHUB_TOKEN=ghp_link\n', encoding='utf-8')
        state = clean_state(tmp_path)
        (state / 'link.txt').symlink_to(outside)
        assert H.offline_state_problem(state), 'a symlinked entry was skipped'
        state2 = clean_state(tmp_path, 'state2')
        os.mkfifo(state2 / 'pipe')
        assert H.offline_state_problem(state2), 'a FIFO was skipped'


class TestR8Probe:
    def test_the_probe_masks_the_host_system_bus(self, tmp_path, sandboxes):
        """R8: --unshare-net does not close an already visible /run unix socket."""
        assert BWRAP_OK
        host = tmp_path / 'host'
        host.mkdir()
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        execdir = H.neutral_execdir('probe/dbus', 1)
        sandboxes.append(execdir)
        record = H.run_probe([str(entry)], host, execdir, label='baseline', state=state)
        assert record['problems'] == [], record['problems']
        assert record['facts'].get('dbus') is False, record['facts'].get('dbus')
        assert record['facts'].get('repo') == {str(H.REPO_ROOT): False} or \
            record['facts'].get('repo') == {str(H.REPO_ROOT): 0}, record['facts'].get('repo')
        dbus = [token for token in record['argv'] if token == '/run/dbus']
        assert dbus, 'the plan does not mask /run/dbus'

    def test_mount_metadata_counts_only_with_proven_repo_visibility(self, tmp_path):
        execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
        base = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                         '/run/user': {'missing': False, 'entries': []},
                         str(H.SUBJECT_ROOT): {'missing': False,
                                               'entries': [execdir.name]},
                         str(Path(execdir) / 'home'): {'missing': False,
                                                       'entries': ['tmp']}},
                'find': [], 'targets': {'/usr/bin/python3': 'ok'},
                'mounts': ['/dev/sda %s btrfs rw' % H.REPO_ROOT],
                'pid1': '/usr/bin/bwrap', 'cwd': str(Path(execdir) / 'work'),
                'home': str(Path(execdir) / 'home'), 'find_rc': 0,
                'dbus': False, 'host': {'h': False}, 'sock': {'s': False},
                'net': {'n': False}}
        canaries = dict(host_canary='h', socket_canary='s', net_canary='n')
        clean = dict(base, repo={str(H.REPO_ROOT): False})
        assert H.probe_problems(clean, 'baseline', tmp_path, execdir, None, 0,
                                **canaries) == [], 'metadata named the repo without proof'
        visible = dict(base, repo={str(H.REPO_ROOT): True})
        assert any('visible' in item for item in H.probe_problems(
            visible, 'baseline', tmp_path, execdir, None, 0, **canaries)
        ), 'a visible repository was accepted'
        unknown = dict(base)
        assert any('mountinfo names the repository' in item for item in H.probe_problems(
            unknown, 'baseline', tmp_path, execdir, None, 0, **canaries)
        ), 'an unproven mountinfo line was accepted'
        assert any('dbus' in item for item in H.probe_problems(
            dict(clean, dbus=True), 'baseline', tmp_path, execdir, None, 0, **canaries)
        ), 'a successful system-bus connect was accepted'
        assert any('dbus' in item for item in H.probe_problems(
            dict(clean, dbus=None), 'baseline', tmp_path, execdir, None, 0, **canaries)
        ), 'an unreported dbus fact was accepted'

    def test_a_mount_line_naming_the_real_repo_path_is_found_whatever_the_directory_is_called(
            self, tmp_path, monkeypatch):
        # orchestrator R8: the check used to key on the literal name 'longhaul', so a checkout or a
        # mutation copy under any other directory name made the probe blind (and this class red).
        monkeypatch.setattr(H, 'REPO_ROOT', Path('/srv/anything/proj-copy'))
        execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
        facts = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                          '/run/user': {'missing': False, 'entries': []},
                          str(H.SUBJECT_ROOT): {'missing': False, 'entries': [execdir.name]},
                          str(Path(execdir) / 'home'): {'missing': False, 'entries': ['tmp']}},
                 'find': [], 'targets': {'/usr/bin/python3': 'ok'},
                 'mounts': ['/dev/sda /srv/anything/proj-copy btrfs rw'],
                 'pid1': '/usr/bin/bwrap', 'cwd': str(Path(execdir) / 'work'),
                 'home': str(Path(execdir) / 'home'), 'find_rc': 0,
                 'dbus': False, 'host': {'h': False}, 'sock': {'s': False}, 'net': {'n': False}}
        canaries = dict(host_canary='h', socket_canary='s', net_canary='n')
        assert any('mountinfo names the repository' in item for item in H.probe_problems(
            facts, 'baseline', tmp_path, execdir, None, 0, **canaries)
        ), 'a mount line naming the repo path was accepted because the directory is not called longhaul'
        facts['mounts'] = ['/dev/sda /srv/elsewhere btrfs rw']
        assert H.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0, **canaries) == []


# --------------------------------------------------------------------------- #
# R8b: the pin comes from the token; UTF-16 in any alignment; the probe greps
# for the real repository path
# --------------------------------------------------------------------------- #

def _r8b_facts(execdir, mounts=(), repo_visible=None):
    facts = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                      '/run/user': {'missing': False, 'entries': []},
                      str(H.SUBJECT_ROOT): {'missing': False,
                                            'entries': [Path(execdir).name]},
                      str(Path(execdir) / 'home'): {'missing': False,
                                                    'entries': ['tmp']}},
             'find': [], 'targets': {'/usr/bin/python3': 'ok'},
             'mounts': list(mounts), 'pid1': '/usr/bin/bwrap',
             'cwd': str(Path(execdir) / 'work'),
             'home': str(Path(execdir) / 'home'), 'find_rc': 0, 'dbus': False,
             'host': {'h': False}, 'sock': {'s': False}, 'net': {'n': False}}
    if repo_visible is not None:
        facts['repo'] = {str(H.REPO_ROOT): bool(repo_visible)}
    return facts


R8B_CANARIES = dict(host_canary='h', socket_canary='s', net_canary='n')


class TestR8bHeaderPin:
    def test_the_header_pin_never_re_reads_the_caller(self, tmp_path, monkeypatch):
        """R8b MINOR 1: the version pin describes the bytes that actually ran."""
        run_evals = _load('run_evals', 'evals/scripts/run_evals.py')
        state = clean_state(tmp_path)
        entry = double_copy(tmp_path / 'entry.py')
        token = H.verified_double([str(entry), '--fake-home', str(state)], state)
        args = argparse.Namespace(run_id='r', model=H.SUBJECT[0],
                                  provider=H.SUBJECT[1], trials=1, live=False,
                                  hermes='%s --fake-home %s' % (entry, state))
        # The caller is replaced and then removed: any later read of it fails.
        evil = double_copy(tmp_path / 'evil.py', b'#!/usr/bin/python3\nprint("EVIL")\n')
        os.replace(evil, entry)
        entry.unlink()
        real_read = H._read_regular
        touched = []

        def guarded(path, *a, **k):
            try:
                same = Path(str(path)).resolve() == Path(str(entry)).resolve()
            except OSError:
                same = False
            if same:
                touched.append(str(path))
                raise AssertionError('the caller was read after the gate')
            return real_read(path, *a, **k)

        def forbidden(name):
            def boom(*a, **k):
                raise AssertionError('%s was called after the gate' % name)
            return boom

        monkeypatch.setattr(H, '_read_regular', guarded)
        for name in ('double_path', 'double_digest', 'double_version'):
            monkeypatch.setattr(H, name, forbidden(name), raising=False)
        header = run_evals.build_header(args, H, ['baseline'], token=token)
        wanted = 'hermes 0.21.6 (fake) [offline:%s]' % token.digest[:12]
        assert header['hermes_version'] == wanted, header['hermes_version']
        assert touched == [], 'the header re-read the caller path'
        again = run_evals.build_header(args, H, ['baseline'], token=token)
        assert again['hermes_version'] == header['hermes_version']


class TestR8bUtf16:
    def test_utf16_inside_a_mostly_utf8_file_is_found(self, tmp_path):
        """R8b MINOR 2a: fewer than 25 % NUL bytes must not hide a UTF-16 key."""
        state = clean_state(tmp_path)
        payload = (b'A' * 4000 + '\nAPI_KEY=abcdef\n'.encode('utf-16-le') + b'A' * 4000)
        (state / 'mixed.bin').write_bytes(payload)
        problem = H.offline_state_problem(state)
        assert problem and 'API_KEY' in problem, \
            'a UTF-16 key inside a mostly UTF-8 file was missed: %r' % (problem,)

    def test_utf16_at_an_odd_offset_is_found(self, tmp_path):
        """R8b MINOR 2b: one prefix byte misaligns both UTF-16 decoders."""
        state = clean_state(tmp_path)
        (state / 'odd.bin').write_bytes(
            b'zz ' + '\nAPI_KEY=abcdef\n'.encode('utf-16-le'))
        problem = H.offline_state_problem(state)
        assert problem and 'API_KEY' in problem, \
            'a misaligned UTF-16 key was missed: %r' % (problem,)

    def test_a_harmless_mixed_encoding_file_is_accepted(self, tmp_path):
        state = clean_state(tmp_path)
        (state / 'mixed.bin').write_bytes(
            b'A' * 4000 + '\nhello world\n'.encode('utf-16-le') + b'A' * 4000)
        assert H.offline_state_problem(state) is None, \
            'a harmless mixed-encoding file was refused'


def _probe_grep_segment(script):
    """The mountinfo grep pipeline as the child runs it, without the redirect."""
    match = re.search(r'grep -iF\b.*?head -20', script, re.DOTALL)
    assert match, 'the inspection script carries no mountinfo grep: %r' % script
    return match.group(0)


class TestR8bProbeSearch:
    def test_the_probe_grep_finds_a_checkout_that_is_not_called_longhaul(self, tmp_path,
                                                                         monkeypatch):
        """R8c: execute the child's own pipeline; string presence is not a proof."""
        import types
        repo = tmp_path / 'elsewhere' / 'proj'
        monkeypatch.setattr(H, 'REPO_ROOT', repo)
        script = H.probe_inspect_script(types.SimpleNamespace(targets=()), None, {})
        segment = _probe_grep_segment(script)
        assert '-iF' in segment, segment
        fixture = tmp_path / 'mountinfo.txt'
        fixture.write_text('\n'.join([
            '/dev/sda /srv/other btrfs rw',
            '/dev/sdb %s btrfs rw' % repo,
            '/dev/sdc %s-copy btrfs rw' % repo,
            '/dev/sdd /srv/longhaul-checkout btrfs rw',
        ]) + '\n', encoding='utf-8')
        out = subprocess.run(['sh', '-c',
                              segment.replace('/proc/self/mountinfo', str(fixture))],
                             capture_output=True, text=True,
                             env={'PATH': os.environ.get('PATH', '/usr/bin'),
                                  'LANG': 'C.UTF-8'})
        assert out.returncode == 0, out.stderr
        lines = [line for line in out.stdout.splitlines() if line.strip()]
        assert any(line.split()[1] == str(repo) for line in lines), \
            'the child grep did not emit the repository path %s: %r' % (repo, lines)
        assert any('longhaul-checkout' in line for line in lines), \
            'the project-name search stopped working: %r' % (lines,)
        assert not any('/srv/other' in line for line in lines), lines
        # The exact -e <REPO_ROOT> token pair is an extra check, not the proof.
        tokens = shlex.split(segment)
        assert ('-e', str(repo)) in list(zip(tokens, tokens[1:])), tokens
        # grep is a substring stage: the sibling line is emitted, and the
        # component filter inside probe_problems is what rejects it.
        execdir = H.SUBJECT_ROOT / ('b' * 20)
        clean = H.probe_problems(_r8b_facts(execdir, mounts=[
            '/dev/sdc %s-copy btrfs rw' % repo]), 'baseline', tmp_path, execdir, None, 0,
            **R8B_CANARIES)
        assert clean == [], clean
        dirty = H.probe_problems(_r8b_facts(execdir, mounts=[
            '/dev/sdb %s btrfs rw' % repo]), 'baseline', tmp_path, execdir, None, 0,
            **R8B_CANARIES)
        assert any('mountinfo names the repository' in item for item in dirty), dirty

    def test_a_sibling_path_with_the_same_prefix_is_not_the_repository(self, tmp_path,
                                                                       monkeypatch):
        """R8b MINOR 3: whole path components, not a bare substring."""
        monkeypatch.setattr(H, 'REPO_ROOT', Path('/srv/proj'))
        execdir = H.SUBJECT_ROOT / ('a' * 20)
        facts = _r8b_facts(execdir, mounts=['/dev/sda /srv/proj-copy btrfs rw'])
        assert H.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                **R8B_CANARIES) == [], \
            'a sibling path with the same prefix was reported as the repository'

    def test_the_real_repository_path_is_still_dirty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(H, 'REPO_ROOT', Path('/srv/proj'))
        execdir = H.SUBJECT_ROOT / ('a' * 20)
        facts = _r8b_facts(execdir, mounts=['/dev/sda /srv/proj btrfs rw'])
        assert any('mountinfo names the repository' in item for item in
                   H.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                    **R8B_CANARIES)), 'the real repository path was clean'


