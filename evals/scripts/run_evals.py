#!/usr/bin/env python3
"""Longhaul eval harness CLI: validate | plan | run | measure | report.

The CLI is deliberately thin. Cases and the subject runner live in other
modules and are imported lazily, so `measure` and `report` work on a finished
run directory without importing the runner at all.

Exit codes: 0 ok, 1 errors, 2 usage/pin drift, 3 a preflight failed.
"""
import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

DIMS = ('recovery_correctness', 'false_completion', 'evidence_fidelity', 'autonomy', 'cost')
WEIGHTS = {'recovery_correctness': 30, 'false_completion': 25, 'evidence_fidelity': 20,
           'autonomy': 15, 'cost': 10}
BLOCKERS = ('completion_with_failing_gate', 'invents_evidence', 're_asks_settled_decision',
            'out_of_scope_write')
CHECK_DIMS = {'final_status': 'false_completion', 'gate_on_claim': 'false_completion',
              'state_valid': 'recovery_correctness', 'ground_truth': 'recovery_correctness',
              'unchanged': 'recovery_correctness', 'answer': 'recovery_correctness',
              'ask': 'autonomy', 'ceremony': 'autonomy', 'cost': 'cost',
              'scope_fence': None, 'tool_rules': '*', 'state_asserts': '*'}
MANDATORY_CHECKS = ('final_status', 'scope_fence', 'ask', 'cost')
STATUSES = ('ok', 'provider_429', 'timeout', 'empty_answer', 'hermes_crash',
            'provider_error', 'interrupted', 'preflight_fail', 'skipped_preflight')
RETRYABLE = ('provider_429', 'timeout', 'empty_answer', 'hermes_crash',
             'provider_error', 'interrupted')
CASE_IDS = ('recover-interrupt', 'file-changed', 'stale-evidence', 'tool-unavailable',
            'external-ambiguous', 'subagent-claims', 'scope-change', 'repeat-question',
            'control-trivial', 'control-finish')
FINAL_CONTRACT = ('When you stop, end your reply with exactly one line: FINAL: complete, '
                  'FINAL: incomplete, or FINAL: blocked.')
ALLOWED_ENV = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TERM', 'XDG_RUNTIME_DIR', 'TMPDIR')
PROFILE_RE = r'^lheval[a-z0-9]+$'
SUBJECT = ('deepseek-v4.1-flash:1017', 'ollama-cloud')
JUDGE = ('claude-sonnet-5-5', 'anthropic')

EVALS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EVALS_DIR.parent
DEFAULT_CONDITIONS = ('baseline', 'v1', 'v1.1')
SCRIPTS = EVALS_DIR / 'scripts'
RUNNERS = EVALS_DIR / 'runners'
ORACLE_REL = 'scripts/checkpoint.py'
SUBJECT_ROOT = Path('/var/tmp/lheval-subject')
# Every fact line of the in-sandbox probe inspection starts with this marker.
# repo_head is report metadata only: a foreign commit must not invalidate a run
# whose conditions, fingerprints and oracle hash still match.
PIN_KEYS = ('run_id', 'hermes_version', 'oracle_sha', 'model', 'provider',
            'judge_model', 'trials', 'max_turns', 'budget_s')
CONDITION_PIN_KEYS = ('commit', 'version', 'skill_fp')
_LOADED = {}


# --------------------------------------------------------------------------- #
# lazy module loading (D8: never a bare package import)
# --------------------------------------------------------------------------- #

def load_module(name, path):
    key = (name, str(path))
    if key in _LOADED:
        return _LOADED[key]
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError('%s not found' % path)
    spec = importlib.util.spec_from_file_location('lheval_' + name, path)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load %s' % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['lheval_' + name] = module
    spec.loader.exec_module(module)
    _LOADED[key] = module
    return module


def load_casespec():
    return load_module('casespec', SCRIPTS / 'casespec.py')


def load_hermes():
    return load_module('hermes', RUNNERS / 'hermes.py')


def load_oracle():
    return load_module('oracle', SCRIPTS / 'oracle.py')


def load_judge():
    return load_module('judge', SCRIPTS / 'judge.py')


def load_report():
    return load_module('report', SCRIPTS / 'report.py')


# --------------------------------------------------------------------------- #
# plain JSON readers (measure/report must not need the runner)
# --------------------------------------------------------------------------- #

def read_jsonl(path):
    rows = []
    path = Path(path)
    if not path.is_file():
        return rows
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def read_cases(evals_dir):
    cases = {}
    for row in read_jsonl(Path(evals_dir) / 'cases.jsonl'):
        if isinstance(row.get('id'), str):
            cases[row['id']] = row
    return cases


def read_expected(evals_dir, case):
    path = Path(evals_dir) / str(case.get('scenario') or '') / 'expected.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def read_header(root):
    rows = read_jsonl(Path(root) / 'ledger.jsonl')
    header = next((row for row in rows if row.get('event') == 'header'), None)
    return header, rows


def default_out_root():
    # Never 'longhaul' in a path: the subject sees its cwd in the system prompt.
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return str(Path(base) / 'lheval-runs')


def oracle_sha():
    """Content hash of the oracle the scores came from (scripts/checkpoint.py)."""
    try:
        return hashlib.sha256((REPO_ROOT / ORACLE_REL).read_bytes()).hexdigest()
    except OSError:
        return None


def new_run_id():
    return time.strftime('run-%Y%m%d-%H%M%S')


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def _common(parser):
    parser.add_argument('--out-root', default=None)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--conditions', default=None,
                        help='comma-separated subset of baseline,v1,v1.1')
    parser.add_argument('--cases', default=None, help='comma-separated case ids')
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--parallel', type=int, default=3)
    parser.add_argument('--max-attempts', type=int, default=2)
    parser.add_argument('--hermes', default=None,
                        help='hermes argv as a shell string; tests pass a fake')
    parser.add_argument('--offline-state', default=None,
                        help='state dir bound read-write for the test double (default HOME)')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--keep-profiles', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--model', default=SUBJECT[0])
    parser.add_argument('--provider', default=SUBJECT[1])


def build_parser():
    parser = argparse.ArgumentParser(prog='run_evals.py', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'plan', 'run', 'measure', 'report'):
        child = sub.add_parser(name)
        _common(child)
        if name == 'measure':
            child.add_argument('--no-judge', action='store_true')
        if name == 'run':
            child.add_argument('--probe', action='store_true',
                               help='prove the sandbox and write probe.json, then exit')
            child.add_argument('--pilot', action='store_true',
                               help='run the baseline pilot and write pilot.json, then exit')
    return parser


def selected_conditions(args, hermes=None):
    known = list(hermes.CONDITIONS) if hermes is not None else list(DEFAULT_CONDITIONS)
    if not args.conditions:
        return known
    wanted = [item.strip() for item in args.conditions.split(',') if item.strip()]
    unknown = [item for item in wanted if item not in known]
    if unknown:
        raise ValueError('unknown condition(s): %s' % ', '.join(unknown))
    return wanted


def selected_cases(args, cases):
    ids = list(cases)
    if args.cases:
        wanted = [item.strip() for item in args.cases.split(',') if item.strip()]
        unknown = [item for item in wanted if item not in cases]
        if unknown:
            raise ValueError('unknown case(s): %s' % ', '.join(unknown))
        return [cid for cid in ids if cid in set(wanted)]
    return ids


def unpinned(args):
    return args.model != SUBJECT[0] or args.provider != SUBJECT[1]


def hermes_argv(args):
    return (shlex.split(args.hermes, posix=(os.name != 'nt')) if args.hermes else ['hermes'])


# --------------------------------------------------------------------------- #
# offline double: the runner owns identity, state and plan; the CLI only asks
# --------------------------------------------------------------------------- #
# The non-live path exists only to exercise the harness with the test double that
# ships in this repository. The runner (runners/hermes.py) owns the whole
# boundary: content identity (direct entry or ``python <verified-double>``),
# a key-free state directory, bwrap and --unshare-net. This module no longer
# carries an alternative subject-subprocess route or its own plan.wrap().

def offline_gate(args):
    """Fail-closed gate for every non-live execution.

    Returns ``(token, errors)``. The token is the runner's immutable
    ``VerifiedDouble``: the caller file is checked once, compared with the
    repository's canonical bytes, and only those bytes are executed later. No
    consumer of this gate may touch the caller path again.
    """
    hermes = load_hermes()
    state = getattr(args, 'offline_state', None) or os.environ.get('HOME')
    return _gate_result(hermes.offline_gate(hermes_argv(args), state))


def _gate_result(result):
    """``(token, errors)``; accept the legacy ``(argv, digest, errors)`` surface."""
    if isinstance(result, tuple) and len(result) == 3:
        subject, _digest, errors = result
        return (None if errors else subject), list(errors or [])
    token, errors = result
    return token, list(errors or [])


def token_argv(token):
    """The canonical input argv of a token (or a legacy stub's argv list)."""
    argv = getattr(token, 'argv', token)
    return [str(part) for part in (argv or [])]


def header_drift(stored, current):
    """Pin drift between the ledger header and the current pins (created_at aside)."""
    if not isinstance(stored, dict):
        return ['header: the ledger has no header row']
    if not isinstance(current, dict):
        return ['header: no current pins were supplied']
    problems = []
    for key in PIN_KEYS:
        if stored.get(key) != current.get(key):
            problems.append('%s: ledger %r != current %r'
                            % (key, stored.get(key), current.get(key)))
    stored_conds = stored.get('conditions') or {}
    current_conds = current.get('conditions') or {}
    for name in sorted(set(stored_conds) | set(current_conds)):
        if name not in stored_conds:
            problems.append('conditions.%s: missing from the ledger header' % name)
            continue
        if name not in current_conds:
            problems.append('conditions.%s: missing from the current pins' % name)
            continue
        for field in CONDITION_PIN_KEYS:
            if stored_conds[name].get(field) != current_conds[name].get(field):
                problems.append('conditions.%s.%s: ledger %r != current %r'
                                % (name, field, stored_conds[name].get(field),
                                   current_conds[name].get(field)))
    return problems


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

def _supports_execdir(hermes):
    return 'execdir' in getattr(getattr(hermes, 'RunSpec', None), '__dataclass_fields__', {})


def run_spec(hermes, *, run_id, cond, case, trial, prompt, rundir, execdir,
             model, provider, max_turns=60, budget_s=600):
    """Build a RunSpec, carrying execdir once L1's dataclass provides it."""
    fields = dict(run_id=run_id, cond=cond, case=case, trial=trial, prompt=prompt,
                  rundir=Path(rundir), model=model, provider=provider,
                  max_turns=max_turns, budget_s=budget_s)
    if _supports_execdir(hermes):
        fields['execdir'] = Path(execdir)
    return hermes.RunSpec(**fields)


def execdir_for(run_key, attempt):
    """Neutral per-attempt directory: no run id, condition, case or 'longhaul'."""
    seed = '%s|%s|%s' % (run_key, attempt, os.urandom(8).hex())
    token = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]
    return SUBJECT_ROOT / token


def prepare_attempt(evals_dir, casespec, case_id, schema, execdir, workdir=None):
    """A clean, fully materialised work tree; retry 2 never sees retry 1."""
    execdir = Path(execdir)
    if execdir.exists():
        shutil.rmtree(execdir)
    work = Path(workdir) if workdir is not None else execdir / 'work'
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    casespec.materialize(evals_dir, case_id, schema, work)
    (execdir / 'home' / '.hermes').mkdir(parents=True, exist_ok=True)
    return work


def _with_execdir(hermes, spec, execdir):
    if not _supports_execdir(hermes):
        return spec
    try:
        return dataclasses.replace(spec, execdir=Path(execdir))
    except (TypeError, ValueError):
        return spec


def _consume_execdir(execdir, rundir, keep_home=False):
    """Move the finished work tree into the result dir, then drop the sandbox.

    ``keep_home`` (only reachable without ``--live``) keeps the sandbox HOME,
    so ``--keep-profiles`` is actually effective; ``run --cleanup`` reaps the
    directory later.
    """
    execdir, rundir = Path(execdir), Path(rundir)
    work, target = execdir / 'work', rundir / 'work'
    if work.is_dir() and not target.exists():
        rundir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(target))
    prompt = execdir / 'prompt.md'
    if prompt.is_file() and not (rundir / 'prompt.md').exists():
        rundir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prompt, rundir / 'prompt.md')
    if keep_home and (execdir / 'home').is_dir():
        for child in list(execdir.iterdir()):
            if child.name == 'home':
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        return
    shutil.rmtree(execdir, ignore_errors=True)


def live_profiles(ledger):
    """Profiles created more often than deleted: a re-created one is still alive."""
    created, deleted = {}, {}
    for row in ledger.rows():
        name = row.get('profile')
        if not name:
            continue
        if row.get('event') == 'profile_created':
            created[name] = created.get(name, 0) + 1
        elif row.get('event') == 'profile_deleted':
            deleted[name] = deleted.get(name, 0) + 1
    return sorted(name for name, count in created.items() if count > deleted.get(name, 0))


def build_specs(args, evals_dir, root, hermes, casespec, case_ids, conditions=None):
    """case -> trial -> condition: time of day cannot drift with the condition."""
    conditions = list(conditions) if conditions is not None else selected_conditions(args, hermes)
    argv = hermes_argv(args)
    specs = []
    for case_id in case_ids:
        prompt = casespec.prompt_text(evals_dir, case_id)
        for trial in range(1, max(1, args.trials) + 1):
            for cond in conditions:
                rundir = Path(root) / 'runs' / cond / case_id / ('t%d' % trial)
                key = '%s/%s/t%s' % (cond, case_id, trial)
                execdir = execdir_for(key, 1)
                spec = run_spec(hermes, run_id=args.run_id, cond=cond, case=case_id,
                                trial=trial, prompt=prompt, rundir=rundir,
                                execdir=execdir, model=args.model, provider=args.provider)
                name = hermes.profile_name(args.run_id, cond, case_id, trial)
                command = hermes.build_command(argv, name, spec, with_skill=(cond != 'baseline'))
                specs.append({'spec': spec, 'name': name, 'key': key, 'cond': cond,
                              'case': case_id, 'trial': trial, 'argv': command,
                              'cwd': str(Path(execdir) / 'work'),
                              'env_keys': sorted(hermes.scrub_env(os.environ))})
    return specs


def cmd_plan(args, evals_dir, root):
    hermes = load_hermes()
    casespec = load_casespec()
    cases = casespec.load_cases(evals_dir)
    by_id = {case['id']: case for case in cases}
    case_ids = selected_cases(args, by_id)
    if unpinned(args):
        print('UNPINNED: model=%s provider=%s' % (args.model, args.provider))
    specs = build_specs(args, evals_dir, root, hermes, casespec, case_ids)
    for item in specs:
        print(json.dumps({'name': item['name'], 'cond': item['cond'],
                          'case': item['case'], 'trial': item['trial'],
                          'argv': item['argv'], 'cwd': item['cwd'],
                          'env_keys': item['env_keys']}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #

def cmd_validate(args, evals_dir, root):
    casespec = load_casespec()
    errors = list(casespec.validate_suite(evals_dir, REPO_ROOT))
    print(json.dumps({'ok': not errors, 'errors': errors}, ensure_ascii=False))
    return 0 if not errors else 1


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #

def _hermes_version(hermes, argv, env, live=False, token=None):
    """The subject's version pin.

    R8b: with a ``VerifiedDouble`` token the pin is parsed from the token's
    repository bytes and the digest is the token's -- the caller's file is never
    read after the gate (its bytes may already be gone or swapped).
    """
    """The subject's version pin.

    Live mode never starts Hermes on the host (FIXPLAN §2): the resolved
    launcher chain is the pin, so probe, matrix and measure agree without an
    unsandboxed Hermes process. The non-live mode has no host execution either:
    its pin is the double's declared version plus the digest of its body, so a
    changed double is pin drift and nothing is run to find that out.
    """
    if token is not None and getattr(token, 'repository_bytes', None) is not None:
        version = hermes.double_version_from_bytes(token.repository_bytes) or 'unversioned'
        return '%s [offline:%s]' % (version, token.digest[:12])
    if live:
        _chain, digest = chain_digest(hermes, argv, env)
        return ('chain:' + digest[:16]) if digest else 'unresolved'
    # Identity/stub surface only; every offline CLI path passes its token.
    digest = hermes.double_digest(argv)
    if digest is None:
        return 'unresolved'
    return '%s [offline:%s]' % (hermes.double_version(argv) or 'unversioned', digest[:12])


def _bwrap_version(hermes):
    """The host bwrap's version string (owned by the runner, no CLI spawn)."""
    return hermes.bwrap_version()


def _repo_head(hermes=None):
    """The repository HEAD (display metadata, owned by the runner)."""
    return (hermes or load_hermes()).repo_head()


def build_header(args, hermes, conditions, token=None):
    """The ledger header. With a token, the version pin comes from its bytes."""
    env = hermes.scrub_env(os.environ)
    pins = {}
    for cond in conditions:
        meta = hermes.CONDITIONS[cond]
        tree = {}
        if meta.commit:
            tree = hermes.skill_tree_from_git(REPO_ROOT, meta.commit)
        fingerprints = hermes.tree_fingerprint(tree) if tree else None
        pins[cond] = {'commit': meta.commit, 'version': meta.version,
                      'skill_fp': fingerprints}
    return {'v': 1, 'event': 'header', 'run_id': args.run_id,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'repo_head': _repo_head(hermes),
            'hermes_version': _hermes_version(hermes, hermes_argv(args), env,
                                              live=bool(getattr(args, 'live', False)),
                                              token=token),
            'oracle_sha': oracle_sha(),
            'model': args.model, 'provider': args.provider, 'judge_model': JUDGE[0],
            'trials': args.trials, 'conditions': pins, 'max_turns': 60, 'budget_s': 600}


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _age_seconds(created_at, now=None):
    try:
        import calendar
        stamp = calendar.timegm(time.strptime(str(created_at), '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time() if now is None else now) - stamp)


def suite_fingerprint(evals_dir):
    """Identity of the case suite (cases + expected tables), not of the code."""
    digest = hashlib.sha256()
    cases = Path(evals_dir) / 'cases.jsonl'
    if cases.is_file():
        digest.update(cases.read_bytes())
    for path in sorted((Path(evals_dir) / 'scenarios').glob('*/expected.json')):
        digest.update(path.parent.name.encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def pin_digest(header):
    payload = {key: (header or {}).get(key) for key in PIN_KEYS}
    payload['conditions'] = (header or {}).get('conditions')
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def chain_digest(hermes, argv, env):
    """Digest of the resolved launcher chain, or None when it cannot be resolved."""
    resolver = getattr(hermes, 'resolve_launcher_chain', None)
    if not callable(resolver):
        return None, None
    try:
        chain = resolver(argv, env)
    except Exception:
        return None, None
    if chain is None:
        return None, None
    if hasattr(chain, 'to_dict'):
        chain = chain.to_dict()
    elif dataclasses.is_dataclass(chain):
        chain = dataclasses.asdict(chain)
    try:
        digest = hashlib.sha256(json.dumps(chain, sort_keys=True, default=str)
                                .encode('utf-8')).hexdigest()
    except (TypeError, ValueError):
        return chain, None
    return chain, digest


def probe_gate(doc, pins, chain, bwrap_version=None, now=None, max_age=3600):
    """A live matrix needs a fresh, clean, live probe of the same pins and chain."""
    if not isinstance(doc, dict):
        return ['probe: missing (run --live --probe first)']
    problems = []
    # R7: a probe record without the three canary declarations proves nothing.
    declared = doc.get('canaries')
    if isinstance(declared, dict):
        missing = [key for key in ('host', 'sock', 'net') if not declared.get(key)]
    elif isinstance(declared, (list, tuple, set)):
        # Legacy records carry a list; it must still name three truthy values.
        missing = [] if len([item for item in declared if item]) == 3 else ['three']
    else:
        missing = ['three']
    if missing:
        problems.append('probe: the host, socket and network canaries were not all '
                        'declared (%r missing: %r)' % (declared, missing))
    if doc.get('schema') != 1:
        problems.append('probe: unknown schema %r' % (doc.get('schema'),))
    if doc.get('mode') != 'live':
        problems.append('probe: the probe was not a live probe')
    if doc.get('clean') is not True:
        problems.append('probe: clean is not true')
    if doc.get('pin_digest') != pins:
        problems.append('probe: pin digest does not match this matrix')
    if chain is None:
        problems.append('probe: the launcher chain cannot be resolved')
    elif doc.get('chain_digest') != chain:
        problems.append('probe: launcher chain changed since the probe')
    if not bwrap_version:
        problems.append('probe: bwrap is missing or broken')
    elif doc.get('bwrap_version') != bwrap_version:
        problems.append('probe: bwrap version %r changed since the probe (now %r)'
                        % (doc.get('bwrap_version'), bwrap_version))
    age = _age_seconds(doc.get('created_at'), now)
    if age is None:
        problems.append('probe: created_at is unreadable')
    elif age > max_age:
        problems.append('probe: stale (%.0f s old)' % age)
    return problems


def pilot_gate(doc, pins, suite, now=None, max_age=3600):
    """A full matrix needs a complete live baseline pilot that is not saturated."""
    if not isinstance(doc, dict):
        return ['pilot: missing (run --live --pilot first)']
    problems = []
    if doc.get('schema') != 1:
        problems.append('pilot: unknown schema %r' % (doc.get('schema'),))
    if doc.get('mode') != 'live':
        problems.append('pilot: the pilot was not run live')
    if doc.get('complete') is not True:
        problems.append('pilot: the pilot did not complete every case')
    if doc.get('pin_digest') != pins:
        problems.append('pilot: pin digest does not match this matrix')
    if doc.get('suite_fingerprint') != suite:
        problems.append('pilot: case suite changed since the pilot')
    outcomes = doc.get('outcomes')
    if not isinstance(outcomes, dict) or len(outcomes) != len(CASE_IDS):
        problems.append('pilot: %s of %d case outcomes were scored'
                        % (len(outcomes) if isinstance(outcomes, dict) else 0,
                           len(CASE_IDS)))
    count = doc.get('n_ge_90')
    if not isinstance(count, int):
        problems.append('pilot: n_ge_90 missing')
    elif count >= 6:
        problems.append('pilot: n_ge_90 is %d, the baseline is above 90 on six cases '
                        '(the prompts teach the behaviour)' % count)
    age = _age_seconds(doc.get('created_at'), now)
    if age is None:
        problems.append('pilot: created_at is unreadable')
    elif age > max_age:
        problems.append('pilot: stale (%.0f s old)' % age)
    return problems


def _sandbox_path(execdir):
    if not execdir:
        return None
    try:
        path = Path(execdir).resolve()
    except (OSError, ValueError):
        return None
    return path if path.parent == SUBJECT_ROOT.resolve() else None


def cleanup(root, hermes, argv, env):
    """Reap orphan subjects and sandboxes; never call host hermes for a sandbox profile.

    Every ledger this run owns is cleaned: the matrix ledger, the pilot ledger
    and the probe ledger (R4 MAJOR: probe and pilot sandboxes used to leak).
    Only canonical directories recorded in one of those ledgers are touched --
    never a wildcard sweep, never another run's directory, never a sandbox a
    live process still references.
    """
    root = Path(root)
    for path in (root / 'ledger.jsonl', root / 'pilot' / 'ledger.jsonl',
                 root / 'probe' / 'ledger.jsonl'):
        _cleanup_ledger(path, hermes)
    return 0


def _cleanup_ledger(path, hermes):
    path = Path(path)
    if not path.is_file():
        return
    ledger = hermes.Ledger(path)
    reaper = getattr(hermes, 'reap_stale', None)
    if callable(reaper):
        try:
            reaper(ledger)
        except Exception as exc:  # reaping must not turn cleanup into a traceback
            print('reap: %s' % exc, file=sys.stderr)
    owned = getattr(hermes, 'owned_sandboxes', None)
    sandboxes = list(owned(ledger)) if callable(owned) else []
    if not sandboxes:  # a ledger from an older harness has only `started` rows
        for row in ledger.rows():
            if row.get('event') == 'started':
                sandbox = _sandbox_path(row.get('execdir'))
                if sandbox is not None and sandbox not in sandboxes:
                    sandboxes.append(sandbox)
    in_use = getattr(hermes, 'sandbox_in_use', None)
    for sandbox in sandboxes:
        if callable(in_use) and in_use(sandbox):
            # Another live run still owns this directory; its ledger will reap it.
            print('cleanup: %s is referenced by a live process, not removed' % sandbox,
                  file=sys.stderr)
            continue
        shutil.rmtree(str(sandbox), ignore_errors=True)
    seen = set()
    for row in ledger.rows():
        if row.get('event') != 'started':
            continue
        profile = row.get('profile')
        if profile and profile not in seen and profile not in ledger.deleted_profiles():
            seen.add(profile)
            ledger.append({'event': 'profile_deleted', 'profile': profile,
                           'key': row.get('key'), 'cond': row.get('cond'),
                           'case': row.get('case'), 'trial': row.get('trial'),
                           'attempt': row.get('attempt'), 'reaped': True})


def _sweep_profiles(ledger):
    """Record the sweep of live profiles in the ledger only.

    The profiles were created inside sandboxes that are already gone; there is
    no host profile to delete, and FIXPLAN §2 forbids an unwrapped Hermes call.
    """
    for name in live_profiles(ledger):
        ledger.append({'event': 'profile_deleted', 'profile': name, 'reaped': True})


def probe_problems(facts, label, home, execdir, skill_root=None, inspect_rc=None,
                   expected_targets=None, **canaries):
    """Every probe fact check, owned by the runner (fail-closed)."""
    return load_hermes().probe_problems(facts, label, home, execdir, skill_root,
                                        inspect_rc, expected_targets, **canaries)


def _write_probe(root, doc):
    doc['clean'] = not doc['problems']
    (Path(root) / 'probe.json').write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for line in doc['problems']:
        print('probe: ' + line, file=sys.stderr)
    return 0 if doc['clean'] else 3


def cmd_probe(args, evals_dir, root):
    """Run --probe: prove the sandbox with the production plan, write probe.json."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    hermes = load_hermes()
    casespec = load_casespec()
    argv = hermes_argv(args)
    env = hermes.scrub_env(os.environ)
    home = Path(os.environ.get('HOME') or Path.home())
    doc = {'schema': 1, 'created_at': now_iso(), 'run_id': args.run_id,
           'mode': 'live' if args.live else 'test', 'pin_digest': None,
           'chain': None, 'chain_digest': None, 'network': False,
           'bwrap': str(getattr(hermes, 'BWRAP', '') or ''), 'bwrap_version': None,
           'baseline': {}, 'candidate': {}, 'clean': False, 'problems': []}
    problems = doc['problems']
    state = None
    token = None
    if not args.live:
        token, offline_errors = offline_gate(args)
        if offline_errors:
            problems.extend('offline: ' + line for line in offline_errors)
            return _write_probe(root, doc)
        argv = token_argv(token)
        raw_state = getattr(args, 'offline_state', None) or os.environ.get('HOME')
        state = Path(raw_state) if raw_state else None
    try:
        doc['pin_digest'] = pin_digest(build_header(args, hermes,
                                                    sorted(hermes.CONDITIONS),
                                                    token=token))
    except (OSError, ValueError, RuntimeError) as exc:
        problems.append('the pins cannot be built: %s' % exc)
        return _write_probe(root, doc)
    chain = None
    if args.live:
        chain, doc['chain_digest'] = chain_digest(hermes, argv, env)
        doc['chain'] = chain
        doc['bwrap_version'] = _bwrap_version(hermes)
        if not doc['bwrap_version']:
            problems.append('bwrap is missing or broken')
        if chain is None:
            problems.append('launcher chain is unresolvable')
    candidate_cond = sorted(hermes.CONDITIONS)[-1]
    tree = {}
    try:
        commit = hermes.CONDITIONS[candidate_cond].commit
        if commit:
            tree = hermes.skill_tree_from_git(REPO_ROOT, commit)
        if not tree:
            problems.append('the candidate skill tree is empty')
    except (OSError, ValueError, RuntimeError) as exc:
        problems.append('the candidate skill tree cannot be materialised: %s' % exc)

    for label, with_skill in (('baseline', False), ('candidate', True)):
        execdir = execdir_for('probe/%s' % label, 1)
        record = {'argv': None, 'rc': None, 'inspect_rc': None, 'stdout': '',
                  'stderr': '', 'version': '', 'inspect': '', 'facts': {},
                  'skill_root': None, 'network': False, 'problems': [],
                  'roots': list(getattr(chain, 'roots', ()) or ())}
        try:
            prepare_attempt(EVALS_DIR, casespec, 'control-trivial', 'v2', execdir)
            neutral = hermes.profile_name(args.run_id, candidate_cond,
                                          'control-trivial', 1) if with_skill else None
            # The runner owns the token, the plan, the child and the facts.
            record = hermes.run_probe(token if token is not None else argv, home,
                                      execdir, label=label,
                                      skill_tree=tree if with_skill else None,
                                      profile_name=neutral, state=state,
                                      network=False)
            problems.extend(record['problems'])
        except (OSError, ValueError, RuntimeError) as exc:
            problems.append('%s: probe failed: %s' % (label, exc))
            record['problems'] = [str(exc)]
        finally:
            doc[label] = record
            doc['network'] = bool((record or {}).get('network'))
            shutil.rmtree(str(execdir), ignore_errors=True)
    # R8 C1: the gate needs three truthy, actually declared canary values, not
    # the dict keys. A record whose values are all None declares nothing.
    declared = {}
    for key in ('host', 'sock', 'net'):
        values = [(doc.get(label) or {}).get('canaries') or {} for label
                  in ('baseline', 'candidate')]
        declared[key] = values[0].get(key) if all(
            (item or {}).get(key) for item in values) else None
    doc['canaries'] = declared
    return _write_probe(root, doc)


def cmd_pilot(args, evals_dir, root):
    """Run --pilot: baseline x all cases x 1 trial, oracle only, write pilot.json.

    The pilot owns ``<root>/pilot/`` (ledger and run trees) so it can never pin
    ``conditions: {baseline}`` into the full matrix's ledger and silently
    suppress v1/v1.1 (R3 MAJOR). It only writes ``pilot.json`` when all ten
    cases produced a clean subject run.
    """
    root = Path(root)
    if args.live:
        print('blocked: a live pilot is disabled: the model credential cannot be '
              'proven unreachable from an untrusted subject yet (R4 BLOCKER). '
              '--probe stays available; the offline double needs --hermes '
              'and --offline-state.', file=sys.stderr)
        return 3
    token, offline_errors = offline_gate(args)
    if offline_errors:
        for line in offline_errors:
            print('offline: ' + line, file=sys.stderr)
        return 2
    state = getattr(args, 'offline_state', None) or os.environ.get('HOME')
    pilot_root = root / 'pilot'
    hermes = load_hermes()
    casespec = load_casespec()
    oracle = load_oracle()
    report = load_report()
    argv = list(token.argv)
    env = hermes.scrub_env(os.environ)
    home = Path(os.environ.get('HOME') or Path.home())
    conditions = ['baseline']
    try:
        pins = build_header(args, hermes, sorted(hermes.CONDITIONS), token=token)
    except (OSError, ValueError, RuntimeError) as exc:
        print('pilot: the pins cannot be built: %s' % exc, file=sys.stderr)
        return 3
    pilot_root.mkdir(parents=True, exist_ok=True)
    ledger = hermes.Ledger(pilot_root / 'ledger.jsonl')
    # Model/tree/suite drift invalidates the old outcomes; never relabel them as
    # the new pin's (R4 MAJOR).
    current_digest = pin_digest(pins)
    previous = _read_json_file(root / 'pilot.json')
    current_suite = suite_fingerprint(evals_dir)
    if isinstance(previous, dict) and previous.get('pin_digest') != current_digest:
        print('pilot: pin drift: pilot.json belongs to a different model/provider/'
              'tree/suite; refusing to reuse or relabel it', file=sys.stderr)
        return 3
    if isinstance(previous, dict) and previous.get('suite_fingerprint') != current_suite:
        print('pilot: suite drift: pilot.json was scored against a different case '
              'suite; refusing to reuse or relabel it', file=sys.stderr)
        return 3
    header = ledger.header()
    if header is None:
        ledger.append(dict(pins, trials=1, pin_digest=current_digest,
                           conditions={'baseline': pins['conditions']['baseline']}))
    elif header.get('pin_digest') and header['pin_digest'] != current_digest:
        print('pilot: pin drift: the pilot ledger belongs to a different pin; '
              'refusing to relabel it', file=sys.stderr)
        return 3
    pilot_args = argparse.Namespace(**vars(args))
    pilot_args.trials = 1  # the pilot is one trial per case, whatever the matrix uses
    specs = build_specs(pilot_args, evals_dir, pilot_root, hermes, casespec,
                        list(CASE_IDS), conditions=conditions)

    def run_fn(spec):
        return _run_attempt(hermes, casespec, spec, token, ledger, {}, env,
                            keep_profile=False, mode=hermes.MODE_OFFLINE,
                            state=state)

    hermes.run_matrix([item['spec'] for item in specs], run_fn, ledger,
                      parallel=args.parallel, max_attempts=args.max_attempts)
    statuses = {item['key']: (ledger.last_finished(item['key']) or {}).get('status')
                for item in specs}
    incomplete = {key: status for key, status in statuses.items() if status != 'ok'}
    cases = read_cases(evals_dir)
    rows = []
    for item in specs:
        case = cases.get(item['case'])
        if case is None:
            continue
        rows.append(oracle.score_run(item['spec'].rundir, case,
                                     read_expected(evals_dir, case), REPO_ROOT, 'baseline'))
    suite_header = {'v': 1, 'event': 'header', 'run_id': args.run_id, 'trials': 1,
                    'conditions': {'baseline': {'commit': None, 'version': None,
                                                'skill_fp': None}}}
    outcomes = {}
    if rows:
        summary = report.aggregate(rows, [], suite_header)
        for case_id in sorted({row['case'] for row in rows}):
            value = summary['cases'].get('baseline', {}).get(case_id, {}).get('outcome')
            if value is not None:
                outcomes[case_id] = round(value / 100.0, 4)
    n_ge_90 = sum(1 for value in outcomes.values() if value >= 0.9)
    complete = not incomplete and len(outcomes) == len(CASE_IDS)
    doc = {'schema': 1, 'created_at': now_iso(), 'run_id': args.run_id,
           'mode': 'live' if args.live else 'test',
           'pin_digest': pin_digest(pins),
           'suite_fingerprint': suite_fingerprint(evals_dir),
           'outcomes': outcomes, 'n_ge_90': n_ge_90, 'complete': complete,
           'trials': 1, 'conditions': conditions}
    target = root / 'pilot.json'
    if not complete:
        # A crashed or partially scored pilot must never leave an old pass behind.
        try:
            target.unlink()
        except OSError:
            pass
        for key, status in sorted(incomplete.items()):
            print('pilot: %s ended %r' % (key, status), file=sys.stderr)
        if len(outcomes) != len(CASE_IDS):
            print('pilot: %d of %d cases were scored; refusing to write pilot.json'
                  % (len(outcomes), len(CASE_IDS)), file=sys.stderr)
        return 3
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n',
                      encoding='utf-8')
    print(json.dumps({'pilot': str(target), 'n_ge_90': n_ge_90,
                      'outcomes': outcomes}, ensure_ascii=False))
    return 0


def _harness_identity(hermes):
    """The optional ownership stamp a runner provides; ``{}`` when it cannot."""
    provider = getattr(hermes, 'harness_identity', None)
    if not callable(provider):
        return {}
    try:
        return provider()
    except (OSError, ValueError):
        return {}


def _run_attempt(hermes, casespec, spec, token, ledger, skill_tree, env,
                 keep_profile=None, mode=None, state=None):
    """One subject attempt: clean sandbox, fresh work tree, consumed afterwards.

    The runner owns the mode, the plan and every subject process; this function
    only materialises and consumes the work tree around one ``run_one`` call.
    """
    key = '%s/%s/t%s' % (spec.cond, spec.case, spec.trial)
    attempt = ledger.attempts(key) + 1
    execdir = execdir_for(key, attempt)
    name = hermes.profile_name(spec.run_id, spec.cond, spec.case, spec.trial)
    # Recorded before the work tree exists: a SIGKILL in this window must still
    # leave an owned directory that --cleanup can prove and reap.
    ledger.append({'event': 'attempt_dir', 'key': key, 'cond': spec.cond,
                   'case': spec.case, 'trial': spec.trial, 'profile': name,
                   'attempt': attempt, 'execdir': str(execdir),
                   'owner': _harness_identity(hermes)})
    schema = 'v1' if spec.cond == 'v1' else 'v2'
    workdir = None if _supports_execdir(hermes) else Path(spec.rundir) / 'work'
    try:
        prepare_attempt(EVALS_DIR, casespec, spec.case, schema, execdir, workdir=workdir)
        attempt_spec = _with_execdir(hermes, spec, execdir)
        return hermes.run_one(attempt_spec, None, ledger, execdir / 'home', skill_tree,
                              mode=mode or hermes.MODE_OFFLINE,
                              state=Path(state) if state else None,
                              token=token, keep_profile=keep_profile)
    finally:
        _consume_execdir(execdir, spec.rundir, keep_home=bool(keep_profile))


def _mark_skipped(ledger, specs):
    """Keys that never started are skipped once a preflight failure aborts a condition."""
    finished = {row.get('key') for row in ledger.rows() if row.get('event') == 'finished'}
    for item in specs:
        if item['key'] in finished:
            continue
        ledger.append({'event': 'finished', 'key': item['key'], 'cond': item['cond'],
                       'case': item['case'], 'trial': item['trial'],
                       'profile': item['name'], 'status': 'skipped_preflight',
                       'rc': None, 'wall_s': 0.0,
                       'errors': ['preflight failed for another key in this invocation']})


def cmd_run(args, evals_dir, root):
    if getattr(args, 'probe', False):
        return cmd_probe(args, evals_dir, root)
    if getattr(args, 'pilot', False):
        return cmd_pilot(args, evals_dir, root)
    if not args.live and not args.hermes:
        print('run needs --live or --hermes (no model call is implied by default)',
              file=sys.stderr)
        return 2
    if args.live:
        # Planner safety design: the key-copy architecture cannot prove that an
        # untrusted subject tool cannot read or exfiltrate the provider
        # credential, so a live matrix is refused BEFORE any key is read.
        if args.keep_profiles:
            print('--keep-profiles is refused with a confined run: a kept sandbox '
                  'HOME would hold key material', file=sys.stderr)
            return 2
        print('blocked: a live subject run is disabled: the model credential cannot '
              'be proven unreachable from an untrusted subject yet (R4 BLOCKER). '
              'Run --probe (no model, no key) to prove the sandbox; offline runs '
              'need --hermes <verified test double> and --offline-state.', file=sys.stderr)
        return 3
    hermes = load_hermes()
    casespec = load_casespec()
    argv = hermes_argv(args)
    env = hermes.scrub_env(os.environ)

    if args.cleanup:
        return cleanup(root, hermes, argv, env)

    token, offline_errors = offline_gate(args)
    if offline_errors:
        for line in offline_errors:
            print('offline: ' + line, file=sys.stderr)
        return 2
    argv = token_argv(token)
    state = getattr(args, 'offline_state', None) or os.environ.get('HOME')

    stored, _rows = read_header(root)
    conditions = (sorted((stored or {}).get('conditions') or {})
                  or selected_conditions(args, hermes))
    try:
        current = build_header(args, hermes, conditions, token=token)
    except (OSError, ValueError, RuntimeError) as exc:
        print('run: the pins cannot be built: %s' % exc, file=sys.stderr)
        return 3 if args.live else 1
    if stored is not None:
        drift = header_drift(stored, current)
        if drift:
            for line in drift:
                print('pin drift: ' + str(line), file=sys.stderr)
            return 2
    if args.live:
        matrix = build_header(args, hermes, sorted(hermes.CONDITIONS))
        pins = pin_digest(matrix)
        gates = probe_gate(_read_json_file(Path(root) / 'probe.json'), pins,
                           chain_digest(hermes, argv, env)[1], _bwrap_version(hermes))
        gates += pilot_gate(_read_json_file(Path(root) / 'pilot.json'), pins,
                            suite_fingerprint(evals_dir))
        if gates:
            for line in gates:
                print('gate: ' + line, file=sys.stderr)
            return 3
    ledger = hermes.Ledger(Path(root) / 'ledger.jsonl')
    if stored is None:
        ledger.append(current)

    case_ids = selected_cases(args, {cid: {} for cid in CASE_IDS})
    specs = build_specs(args, evals_dir, root, hermes, casespec, case_ids,
                        conditions=conditions)
    trees = {}
    for cond in conditions:
        meta = hermes.CONDITIONS[cond]
        trees[cond] = hermes.skill_tree_from_git(REPO_ROOT, meta.commit) if meta.commit else {}

    def run_fn(spec):
        return _run_attempt(hermes, casespec, spec, token, ledger,
                            trees.get(spec.cond), env,
                            keep_profile=args.keep_profiles,
                            mode=hermes.MODE_OFFLINE, state=state)

    results = hermes.run_matrix([item['spec'] for item in specs], run_fn, ledger,
                                parallel=args.parallel, max_attempts=args.max_attempts)
    if not args.keep_profiles:
        _sweep_profiles(ledger)
    statuses = []
    if isinstance(results, dict):
        for value in results.values():
            if isinstance(value, dict) and value.get('status'):
                statuses.append(value['status'])
            elif isinstance(value, str):
                statuses.append(value)
    failed_preflight = any(status == 'preflight_fail' for status in statuses)
    if failed_preflight:
        _mark_skipped(ledger, specs)
        return 3
    pending = ((results or {}).get('_summary') or {}).get('pending') or []
    if pending:
        print('%d key(s) still pending after the matrix: %s'
              % (len(pending), ', '.join(map(str, pending[:5]))), file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# measure
# --------------------------------------------------------------------------- #

def _current_header(args, header, token=None):
    """Rebuild the pins for this invocation; None when the run has no ledger yet."""
    hermes = load_hermes()
    conditions = sorted((header or {}).get('conditions') or {}) \
        or selected_conditions(args, hermes)
    return build_header(args, hermes, conditions, token=token)


def cmd_measure(args, evals_dir, root):
    oracle = load_oracle()
    if args.live:
        print('blocked: live judging is disabled with the live subject path: no key '
              'may be read or sent until containment is proven (R4 BLOCKER).',
              file=sys.stderr)
        return 3
    if not args.no_judge and not args.hermes:
        print('measure judging needs --hermes; pass --no-judge for oracle only',
              file=sys.stderr)
        return 2
    subject, state = None, None
    if args.hermes:
        # R7 (auditor mutant X3): every non-live --hermes route reaches the
        # central gate before anything is read, judged or spawned -- including
        # `measure --no-judge`, which used to skip the gate entirely.
        subject, offline_errors = offline_gate(args)
        if offline_errors:
            for line in offline_errors:
                print('offline: ' + line, file=sys.stderr)
            return 2
        state = getattr(args, 'offline_state', None) or os.environ.get('HOME')
        if not state:
            print('offline: --offline-state (or HOME) is needed for the judge',
                  file=sys.stderr)
            return 2
    header, _rows = read_header(root)
    if header is not None:
        drift = header_drift(header, _current_header(args, header, token=subject))
        if drift:
            for line in drift:
                print('pin drift: ' + str(line), file=sys.stderr)
            return 2
    cases = read_cases(evals_dir)
    rows = []
    for rundir in sorted((Path(root) / 'runs').glob('*/*/t*')):
        if not rundir.is_dir():
            continue
        cond = rundir.parts[-3]
        case_id = rundir.parts[-2]
        case = cases.get(case_id)
        if case is None:
            print('run dir %s has no case in cases.jsonl' % rundir, file=sys.stderr)
            return 1
        rows.append(oracle.score_run(rundir, case, read_expected(evals_dir, case),
                                     REPO_ROOT, cond))
    out = Path(root) / 'measure.jsonl'
    with out.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    # Cases without judge dimensions are never sent to the judge.
    judged = {case_id: case for case_id, case in cases.items() if case.get('judge')}
    if args.no_judge or not judged:
        return 0
    judge = load_judge()
    # No provider credential is read (or needed) on the offline path: the judge
    # runs the identity-verified double through the same runner plan (R7) and the
    # sandbox HOME never held a key.
    secrets = []
    hermes = load_hermes()
    home = Path(os.environ.get('HOME') or Path.home())

    last_error = {'text': ''}

    def call(prompt):
        # judge._split_call accepts (rc, text); the runner returns a Proc.
        proc = hermes.run_judge(subject, state, prompt, host_home=home)
        last_error['text'] = (proc.stderr or '').strip()
        return proc.rc, proc.stdout
    judge_error = ''
    setup_refused = False
    try:
        probe = call('Reply with exactly: OK')
        available = probe[0] == 0 and 'OK' in (probe[1] or '')
        if not available:
            judge_error = 'judge: unavailable (rc=%s): %s' % (
                probe[0], (last_error['text'] or (probe[1] or '')).strip()[:400])
            setup_refused = probe[0] == 2
    except Exception as exc:                 # a broken call is infrastructure
        available = False
        judge_error = 'judge: unavailable: %s' % exc
    if not available:
        print(judge_error, file=sys.stderr)
        if setup_refused:
            # R8 J1: the sandbox refused the judge (setup error). That is an
            # infrastructure failure, not a measurement: exit 2, never silent
            # 'unavailable' rows.
            return 2
    try:
        block = judge.extract_judge_block(
            (Path(evals_dir) / 'rubric.md').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('rubric: %s' % exc, file=sys.stderr)
        return 1
    judge_rows = []
    for case_id in sorted(judged):
        case = judged[case_id]
        dims = list(case.get('judge') or [])
        for trial in range(1, max(1, args.trials) + 1):
            views = {}
            for cond in selected_conditions(args):
                rundir = Path(root) / 'runs' / cond / case_id / ('t%d' % trial)
                if (rundir / 'events.jsonl').is_file():
                    views[cond] = judge.run_view(rundir, REPO_ROOT, secrets=secrets)
            if len(views) < 2:
                continue
            payload = dict(case, trial=trial, expect=read_expected(evals_dir, case))
            seed = '%s|%s|%d' % (args.run_id, case_id, trial)
            if not available:
                order = list(views)
                for pass_no, sequence in enumerate(judge.rotations(order), 1):
                    judge_rows.append({'case': case_id, 'trial': trial, 'pass': pass_no,
                                       'order': sequence,
                                       'labels': dict(zip(judge.labels_for(len(sequence)),
                                                          sequence)),
                                       'status': 'unavailable', 'dims': dims,
                                       'parsed': None, 'text_sha256': None, 'chars': 0})
                continue
            judge_rows.extend(judge.judge_case(payload, views, block, call, seed))
    if not judge_rows:
        return 0
    with (Path(root) / 'judge.jsonl').open('w', encoding='utf-8') as stream:
        for row in judge_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    return 0


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def cmd_report(args, evals_dir, root):
    report = load_report()
    header, _rows = read_header(root)
    if header is None:
        print('no ledger header under %s' % root, file=sys.stderr)
        return 1
    summary = report.aggregate(read_jsonl(Path(root) / 'measure.jsonl'),
                               read_jsonl(Path(root) / 'judge.jsonl'), header)
    body = report.render(summary)
    (Path(root) / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    results = Path(evals_dir) / 'RESULTS.md'
    if not results.is_file():
        print('missing %s (hand-written template with generated markers)' % results,
              file=sys.stderr)
        return 1
    try:
        report.write_section(results, body)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    root = Path(args.out_root or default_out_root()) / (args.run_id or new_run_id())
    args.run_id = args.run_id or root.name
    evals_dir = EVALS_DIR
    handlers = {'validate': cmd_validate, 'plan': cmd_plan, 'run': cmd_run,
                'measure': cmd_measure, 'report': cmd_report}
    try:
        return handlers[args.command](args, evals_dir, root)
    except (FileNotFoundError, ValueError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == '__main__':
    sys.exit(main())
