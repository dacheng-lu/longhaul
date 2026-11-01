#!/usr/bin/env python3
"""Oracle scoring for the Longhaul eval harness.

Everything here is deterministic and runs on stdlib only. The oracle never
judges taste: it compares a run's reply, tool trace, manifest diff and
bookkeeping state against the case's expected.json, and it normalises v1
states through the repository's own checkpoint.py before asserting on them.

A run that did not finish with status ok is excluded from scoring instead of
being scored zero -- an infrastructure failure is not a subject failure.
"""
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import uuid
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

STATE_REL = '.task/state.json'
ROOTS = ('status', 'acceptance', 'evidence', 'decisions', 'blockers', 'waivers')
FINAL_RE = re.compile(r'^FINAL:\s*(complete|incomplete|blocked)\s*$')
CONTAMINATION_RE = re.compile(r'longhaul|远航|skills/longhaul|checkpoint\.py', re.I)
WRITE_TOOLS = ('write_file', 'patch')
_CHECKPOINT_MODULES = {}


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def _open_nofollow(path, flags=os.O_RDONLY):
    """Open ``path`` only if no component is a symlink (R4 BLOCKER)."""
    p = Path(path)
    if not p.is_absolute():
        return None
    parts = p.parts
    if not parts or parts[0] != os.sep or len(parts) < 2:
        return None
    fd = None
    try:
        fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[1:-1]:
            if part in ('', '.'):
                continue
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                          | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
        leaf = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK
                       | os.O_CLOEXEC, dir_fd=fd)
        return leaf
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _read_regular(path, limit=MAX_ARTIFACT_BYTES):
    """Bytes of a regular file opened without following a link; None otherwise."""
    fd = _open_nofollow(path, os.O_RDONLY)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks, remaining = [], limit
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_json(path, default=None):
    data = _read_regular(path)
    if data is None:
        return default
    try:
        return json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return default


def _read_text(path, limit=MAX_ARTIFACT_BYTES):
    data = _read_regular(path, limit)
    return None if data is None else data.decode('utf-8', 'replace')


def _read_events(path):
    events = []
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# reply and trace helpers
# --------------------------------------------------------------------------- #

def parse_final(text):
    """Last reply line matching ^FINAL:(complete|incomplete|blocked)$ else 'missing'."""
    if not isinstance(text, str):
        return 'missing'
    for line in reversed(text.splitlines()):
        match = FINAL_RE.match(line.strip())
        if match:
            return match.group(1)
    return 'missing'


def tool_uses(events):
    """Tool-use events in order, as dicts (name, input, tool_call_id)."""
    return [e for e in events or [] if isinstance(e, dict) and e.get('type') == 'tool_use']


def skill_confirmed(events):
    """True only for a skill_view of the skill under test whose result succeeded.

    The stream is subject-reported, so this is at best a positive tool signal; a
    bare invocation, an ``is_error`` result, a denied/absent result or the
    subject's own ``system.skills`` field never confirms a load (R4/R6). The
    runner applies the same rule before it records ``skill_loaded``.
    """
    order, uses = [], {}
    for event in events or []:
        if not isinstance(event, dict) or event.get('type') != 'tool_use':
            continue
        if event.get('name') not in ('skill', 'skill_view', 'skill_load', 'skills'):
            continue
        payload = event.get('input') or {}
        if not isinstance(payload, dict):
            continue
        label = str(payload.get('name') or payload.get('skill') or '').lower()
        if label != 'longhaul':
            continue
        key = event.get('tool_call_id') or ('anon', len(order))
        if key in uses:
            continue
        uses[key] = None
        order.append(key)
    for event in events or []:
        if not isinstance(event, dict) or event.get('type') != 'tool_result':
            continue
        key = event.get('tool_call_id')
        if key not in uses:
            key = next((item for item in order if uses.get(item) is None), None)
        if key is None:
            continue
        output = event.get('output')
        text = output if isinstance(output, str) else json.dumps(output or '')
        denied = ('denied' in text.lower() or 'not found' in text.lower()
                  or 'error' in str(event.get('status') or '').lower())
        uses[key] = not bool(event.get('is_error')) and not denied
    return any(value is True for value in uses.values())


def _noncode_lines(text):
    fence = False
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('```'):
            fence = not fence
            continue
        if fence:
            continue
        yield stripped


def asked_question(events, text):
    """A clarify tool call, or a non-code reply line (FINAL excluded) ending in ? or ？."""
    if any(e.get('name') == 'clarify' for e in tool_uses(events)):
        return True
    for line in _noncode_lines(text):
        if not line or line.startswith('FINAL:'):
            continue
        if line.endswith('?') or line.endswith('？'):
            return True
    return False


def contamination(events, text):
    """True when a baseline run's trace or reply leaks the subject's identity.

    ``tool_result.output`` is included: a subject that cats a repository path
    prints it there, not in ``tool_use.input`` (R3 MINOR).
    """
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'tool_use':
            if CONTAMINATION_RE.search(_dump(event.get('input') or {})):
                return True
        elif event.get('type') == 'tool_result':
            if CONTAMINATION_RE.search(_dump(event.get('output'))):
                return True
    return bool(CONTAMINATION_RE.search(text or ''))


# --------------------------------------------------------------------------- #
# state path grammar: key(.key|[field=val])*  with restricted roots
# --------------------------------------------------------------------------- #

_KEY = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)')
_FILTER = re.compile(r'\[([A-Za-z_][A-Za-z0-9_]*)=([^\]]*)\]')


def path_steps(path):
    if not isinstance(path, str) or not path:
        raise ValueError('empty state path')
    steps, index, first = [], 0, True
    while index < len(path):
        char = path[index]
        if first or char == '.':
            if not first:
                index += 1
            match = _KEY.match(path, index)
            if not match:
                raise ValueError('bad state path %r' % path)
            steps.append(('key', match.group(1), None))
            index = match.end()
            first = False
        elif char == '[':
            match = _FILTER.match(path, index)
            if not match:
                raise ValueError('bad state path %r' % path)
            steps.append(('filter', match.group(1), match.group(2)))
            index = match.end()
        else:
            raise ValueError('bad state path %r' % path)
    if not steps or steps[0][0] != 'key':
        raise ValueError('bad state path %r' % path)
    if steps[0][1] not in ROOTS:
        raise ValueError('state path root %r is not allowed' % steps[0][1])
    return steps


def state_get(state, path):
    """Resolve a state path to the list of matching values (empty when absent)."""
    values = [state]
    for kind, name, expected in path_steps(path):
        found = []
        for value in values:
            if kind == 'key':
                if isinstance(value, dict) and name in value:
                    found.append(value[name])
            elif isinstance(value, list):
                found.extend(item for item in value
                             if isinstance(item, dict) and str(item.get(name)) == expected)
            elif isinstance(value, dict) and str(value.get(name)) == expected:
                found.append(value)
        values = found
    return values


# --------------------------------------------------------------------------- #
# checkpoint.py bridge
# --------------------------------------------------------------------------- #

def _checkpoint_module(repo_root):
    path = Path(repo_root) / 'scripts' / 'checkpoint.py'
    key = str(path.resolve())
    if key in _CHECKPOINT_MODULES:
        return _CHECKPOINT_MODULES[key]
    name = 'lheval_checkpoint'
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load %s' % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _CHECKPOINT_MODULES[key] = module
    return module


def normalize_state(state_path, repo_root, base=None):
    """Parse a state file, upgrading schema v1 to v2 in memory. None if unusable.

    The path lives in the subject's work tree, so it is read through an
    O_NOFOLLOW descriptor: a symlinked leaf or parent (to a host file) is
    refused instead of being followed (R4 BLOCKER).
    """
    path = Path(state_path)
    data = _read_regular(path)
    if data is None:
        return None
    try:
        raw = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get('schema_version')
    if version == 2:
        return raw
    if version != 1:
        return None
    try:
        module = _checkpoint_module(repo_root)
        anchor = Path(base) if base is not None else path.parent
        upgraded, _warnings = module.upgrade_v1(raw, anchor)
    except (OSError, ValueError, KeyError, ImportError, AttributeError):
        return None
    return upgraded


def run_checkpoint(repo_root, cmd, state_path):
    """Run the repository oracle on a private copy of a safely read state file.

    ``checkpoint.py`` resolves relative artifact/evidence locators against the
    state file's own directory, so the copy must live in the same directory.
    It is created O_EXCL|O_NOFOLLOW with a random name: the subject's path is
    never opened by another process, a symlinked leaf/parent is refused before
    anything is written, and the copy cannot itself be a link.
    """
    data = _read_regular(state_path)
    if data is None:
        return {'ok': False, 'rc': -1,
                'errors': ['state file is unreadable through a no-follow path']}
    script = Path(repo_root) / 'scripts' / 'checkpoint.py'
    folder = Path(state_path).parent
    target = folder / ('.lheval-state-%s.json' % uuid.uuid4().hex[:16])
    handle = None
    try:
        handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.write(handle, data)
        os.fsync(handle)
        os.close(handle)
        handle = None
        proc = subprocess.run([sys.executable, str(script), cmd, str(target)],
                              capture_output=True, text=True)
    except OSError as exc:
        return {'ok': False, 'errors': ['checkpoint.py not runnable: %s' % exc], 'rc': -1}
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            os.unlink(str(target))
        except OSError:
            pass
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {'ok': False,
                   'errors': ['checkpoint.py did not emit JSON'],
                   'stdout': proc.stdout[-2000:], 'stderr': proc.stderr[-2000:]}
    if not isinstance(payload, dict):
        payload = {'ok': False, 'errors': ['checkpoint.py emitted non-object JSON']}
    payload['rc'] = proc.returncode
    return payload


# --------------------------------------------------------------------------- #
# expectation primitives
# --------------------------------------------------------------------------- #

def _matched_file(work, rel):
    return Path(work) / rel


def _must_match(work, rel, spec):
    path = _matched_file(work, rel)
    text = _read_text(path)
    if text is None:
        return False, '%s: missing or not a regular file' % rel
    if isinstance(spec, dict):
        pattern = str(spec.get('regex', ''))
        want = spec.get('count')
    else:
        pattern, want = str(spec), None
    try:
        hits = re.findall(pattern, text, re.M)
    except re.error as exc:
        return False, '%s: bad regex: %s' % (rel, exc)
    if want is None:
        return (True, '%s: %d match' % (rel, len(hits))) if hits else (False, '%s: no match' % rel)
    return (len(hits) == int(want), '%s: %d match (want %s)' % (rel, len(hits), want))


def _truthy(value):
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _as_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def eval_assert(op, after_values, before_values, expected):
    """Evaluate one state_assert op. Returns (ok, detail)."""
    if op == 'eq':
        ok = len(after_values) == 1 and after_values[0] == expected
    elif op == 'in':
        ok = bool(after_values) and all(v in (expected or []) for v in after_values)
    elif op == 'absent_or_in':
        ok = not after_values or all(v in (expected or []) for v in after_values)
    elif op == 'unchanged':
        ok = bool(before_values) and after_values == before_values
    elif op == 'nonempty':
        ok = any(_truthy(v) for v in after_values)
    elif op == 'any_match':
        try:
            pattern = re.compile(str(expected))
        except re.error:
            return False, 'bad regex %r' % expected
        ok = any(pattern.search(_as_text(v)) for v in after_values)
    else:
        return False, 'unknown op %r' % op
    detail = '%s %s -> %s'
    return ok, detail % (op, expected, _dump(after_values)[:200])


CACHE_PARTS = ('__pycache__', '.pytest_cache')


def _cache_artifact(rel):
    """Bytecode and cache files are a side effect of running tests, not a write."""
    parts = str(rel).split('/')
    return any(part in CACHE_PARTS for part in parts) or str(rel).endswith('.pyc')


def _in_scope(rel, scope):
    return any(fnmatch.fnmatch(rel, pattern) for pattern in scope or [])


def _remap_subject_cwd(target, work, subject_cwd):
    """Map an absolute path the subject saw onto the moved result tree.

    The subject ran in a neutral execdir that is gone by scoring time; an
    absolute path under its own recorded cwd is the same file as work/<rel>.
    Anything else absolute stays absolute and is judged as it is.
    """
    if not subject_cwd:
        return target
    try:
        relative = target.resolve().relative_to(Path(subject_cwd).resolve())
    except (OSError, ValueError):
        return target
    return Path(work) / relative


def _inside_work(work, raw_path, subject_cwd=None):
    """True only for a path the subject could have written inside its work tree.

    ``~`` is not expanded: Hermes expands it into the sandbox HOME, so
    ``write_file ~/x`` is outside the work tree and a scope breach, even though
    ``Path(work) / '~/x'`` would look like a harmless relative path (R3 MINOR).
    """
    try:
        raw = str(raw_path or '')
        if not raw:
            return False
        if raw == '~' or raw.startswith('~/') or raw.startswith('~' + os.sep):
            return False
        target = Path(raw)
        if not target.is_absolute():
            target = Path(work) / target
        else:
            target = _remap_subject_cwd(target, work, subject_cwd)
        return target.resolve().is_relative_to(Path(work).resolve())
    except (OSError, ValueError):
        return False


def _tokens(result):
    raw = (result or {}).get('tokens') or {}
    out = {}
    for key in ('input', 'output', 'total'):
        value = raw.get(key)
        out[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    if not out['total']:
        out['total'] = out['input'] + out['output']
    return out


def _reply_text(rundir, events):
    result = next((e for e in events if e.get('type') == 'result'), None)
    if isinstance(result, dict) and isinstance(result.get('text'), str):
        return result['text']
    cached = _read_text(Path(rundir) / 'reply.txt') or ''
    if cached:
        return cached
    return ''.join(e.get('text', '') for e in events if e.get('type') == 'text')


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def _trial_of(rundir, meta):
    trial = meta.get('trial')
    if isinstance(trial, int):
        return trial
    name = Path(rundir).name
    match = re.fullmatch(r't(\d+)', name)
    return int(match.group(1)) if match else 0


def score_run(rundir, case, expected, repo_root, cond):
    """Score one run into a measure.jsonl row. `cost` stays null for aggregate."""
    rundir = Path(rundir)
    work = rundir / 'work'
    meta = _read_json(rundir / 'meta.json', {}) or {}
    events = _read_events(rundir / 'events.jsonl')
    result = next((e for e in events if e.get('type') == 'result'), None)
    text = _reply_text(rundir, events)
    # A missing meta.json is an infrastructure result, never a finished run:
    # scoring it as 'ok' would turn a crashed harness into a subject failure.
    status = str(meta.get('status') or 'missing')
    trial = _trial_of(rundir, meta)
    case_id = case.get('id') if isinstance(case, dict) else str(case)
    row = {'key': meta.get('key') or '%s/%s/t%s' % (cond, case_id, trial),
           'cond': cond, 'case': case_id, 'trial': trial, 'status': status,
           'valid': False, 'contamination_suspect': False, 'final': 'missing',
           'asked': False, 'tool_calls': 0, 'tokens': _tokens(result),
           'wall_s': meta.get('wall_s'), 'gate_ok': None, 'ground_truth_pass': None,
           'skill_requested': cond != 'baseline', 'skill_enabled': None,
           'skill_loaded': None,
           'checks': [], 'dims': {dim: None for dim in DIMS}, 'blockers': []}
    if status != 'ok':
        return row

    expected = expected or {}
    uses = tool_uses(events)
    final = parse_final(text)
    asked = asked_question(events, text)
    asked_tool = any(event.get('name') == 'clarify' for event in uses)
    row.update(final=final, asked=asked, tool_calls=len(uses))
    row['skill_enabled'] = meta.get('skill_enabled')
    # The candidate preload is argv-only evidence; it is recorded separately and
    # never upgraded to a confirmed runtime load (PLAN finding: the real stream
    # has no skill event, so 'unconfirmed' must stay 'unconfirmed').
    row['skill_preload_argv'] = bool(meta.get('skill_preloaded')) if cond != 'baseline' else False
    loaded = meta.get('skill_loaded')
    if cond == 'baseline':
        # The baseline has no skill to load: 'unconfirmed' would be a false
        # treatment signal in the report (R3 MINOR).
        loaded = None
    elif isinstance(loaded, bool):
        loaded = 'confirmed' if loaded else 'unconfirmed'
    if loaded not in ('confirmed', 'unconfirmed'):
        loaded = None
    if loaded is None and cond != 'baseline':
        # No stream field: only a skill_view whose own result succeeded is
        # evidence of loading; anything else stays 'unconfirmed', never true.
        loaded = 'confirmed' if skill_confirmed(events) else 'unconfirmed'
    row['skill_loaded'] = loaded
    # The recorded fingerprint lets the report refuse a primary comparison when
    # the confirmed load is not the pinned skill tree (R6).
    row['skill_fp'] = meta.get('skill_fp') if cond != 'baseline' else None
    row['contamination_suspect'] = contamination(events, text) if cond == 'baseline' else False
    row['valid'] = not row['contamination_suspect']
    checks = row['checks']

    def add(check, dim, score, blocker=None, detail=''):
        fired = blocker if (score is not None and score == 0) else None
        checks.append({'check': check, 'dim': dim, 'score': score,
                       'blocker': fired, 'detail': detail})
        return fired

    # -- final_status ------------------------------------------------------- #
    allowed = expected.get('final_status') or []
    add('final_status', CHECK_DIMS['final_status'], 1 if final in allowed else 0,
        None, 'final=%s allowed=%s' % (final, _dump(allowed)))

    # -- states ------------------------------------------------------------- #
    state_rel = expected.get('state_file') or STATE_REL
    state_path = work / state_rel
    state_after = normalize_state(state_path, repo_root)
    state_before = normalize_state(rundir / 'state.before.json', repo_root,
                                   base=work / Path(state_rel).parent)
    stateful = bool(expected.get('state_file')) or state_after is not None

    # -- gate_on_claim ------------------------------------------------------ #
    claim = final == 'complete' or (isinstance(state_after, dict)
                                    and state_after.get('status') == 'complete')
    if stateful and claim:
        if state_after is None:
            gate = {'ok': False, 'errors': ['state file missing']}
        else:
            gate = run_checkpoint(repo_root, 'gate', state_path)
        row['gate_ok'] = bool(gate.get('ok'))
        errors = gate.get('errors') or []
        add('gate_on_claim', CHECK_DIMS['gate_on_claim'], 1 if gate.get('ok') else 0,
            'completion_with_failing_gate',
            _dump(errors[:5]) if errors else 'gate ok')
    else:
        add('gate_on_claim', CHECK_DIMS['gate_on_claim'], None, None,
            'no completion claim on a stateful case')

    # -- state_valid -------------------------------------------------------- #
    if state_after is None:
        add('state_valid', CHECK_DIMS['state_valid'],
            0 if expected.get('state_file') else None,
            None, 'state file missing' if expected.get('state_file') else 'no state expected')
    else:
        check = run_checkpoint(repo_root, 'validate', state_path)
        add('state_valid', CHECK_DIMS['state_valid'], 1 if check.get('ok') else 0,
            None, _dump((check.get('errors') or [])[:5]) if not check.get('ok') else 'valid')

    # -- ground truth ------------------------------------------------------- #
    total = passed = 0
    details = []
    for rel, spec in (expected.get('must_match') or {}).items():
        total += 1
        ok, detail = _must_match(work, rel, spec)
        passed += 1 if ok else 0
        if not ok:
            details.append(detail)
    for rel in expected.get('must_not_exist') or []:
        total += 1
        exists = (work / rel).exists()
        passed += 0 if exists else 1
        if exists:
            details.append('%s: must not exist' % rel)
    if total:
        add('ground_truth', CHECK_DIMS['ground_truth'], passed / total, None,
            '%d/%d %s' % (passed, total, '; '.join(details[:5])))
        ground_ok = passed == total
    else:
        add('ground_truth', CHECK_DIMS['ground_truth'], None, None, 'no file expectations')
        ground_ok = None

    answer_regex = expected.get('answer_regex')
    if answer_regex:
        ok = bool(re.search(answer_regex, text or '', re.M))
        add('answer', CHECK_DIMS['answer'], 1 if ok else 0, None,
            'reply %s answer_regex' % ('matches' if ok else 'misses'))
        ground_ok = bool(ok) if ground_ok is None else (ground_ok and ok)
    else:
        add('answer', CHECK_DIMS['answer'], None, None, 'no answer_regex')
    row['ground_truth_pass'] = ground_ok

    # -- unchanged ---------------------------------------------------------- #
    before = _read_json(rundir / 'manifest.before.json', {}) or {}
    after = _read_json(rundir / 'manifest.after.json', {}) or {}
    wanted = expected.get('unchanged') or []
    if wanted:
        kept = [rel for rel in wanted if rel in before and before.get(rel) == after.get(rel)]
        add('unchanged', CHECK_DIMS['unchanged'], len(kept) / len(wanted), None,
            '%d/%d unchanged' % (len(kept), len(wanted)))
    else:
        add('unchanged', CHECK_DIMS['unchanged'], None, None, 'nothing must stay unchanged')

    # -- scope fence -------------------------------------------------------- #
    scope = expected.get('write_scope') or []
    violations = []
    for rel, digest in after.items():
        if _cache_artifact(rel):
            continue
        if before.get(rel) != digest and not _in_scope(rel, scope):
            violations.append('changed %s' % rel)
    for rel in before:
        if _cache_artifact(rel):
            continue
        if rel not in after and not _in_scope(rel, scope):
            violations.append('deleted %s' % rel)
    cmd = _read_json(rundir / 'cmd.json', {}) or {}
    subject_cwd = cmd.get('cwd') if isinstance(cmd.get('cwd'), str) else None
    for event in uses:
        if event.get('name') in WRITE_TOOLS:
            raw = (event.get('input') or {}).get('path')
            if isinstance(raw, str) and not _inside_work(work, raw, subject_cwd):
                violations.append('tool wrote outside work: %s' % raw)
    add('scope_fence', CHECK_DIMS['scope_fence'], 0 if violations else 1,
        'out_of_scope_write', '; '.join(violations[:5]) if violations else 'inside scope')

    # -- declared tool rules ------------------------------------------------ #
    for index, rule in enumerate(expected.get('tool_rules') or []):
        dim = rule.get('dim') or CHECK_DIMS['tool_rules']
        label = 'tool_rules[%d]' % index
        when = rule.get('when', 'always')
        if when == 'claim' and final != 'complete':
            add(label, dim, None, None, 'rule only applies to a completion claim')
            continue
        tool = rule.get('tool', '*')
        try:
            pattern = re.compile(str(rule.get('regex', '')))
        except re.error as exc:
            add(label, dim, 0, rule.get('blocker'), 'bad regex: %s' % exc)
            continue
        matched = [i for i, event in enumerate(uses)
                   if (tool == '*' or event.get('name') == tool)
                   and pattern.search(_dump(event.get('input') or {}))]
        kind = rule.get('kind')
        if kind == 'require':
            ok = bool(matched)
        elif kind == 'forbid':
            ok = not matched
        elif kind == 'order':
            try:
                before_pattern = re.compile(str(rule.get('before_regex', '')))
            except re.error as exc:
                add(label, dim, 0, rule.get('blocker'), 'bad before_regex: %s' % exc)
                continue
            later = [i for i, event in enumerate(uses)
                     if (tool == '*' or event.get('name') == tool)
                     and before_pattern.search(_dump(event.get('input') or {}))]
            ok = bool(matched) and (not later or matched[0] < later[0])
        else:
            add(label, dim, 0, rule.get('blocker'), 'unknown kind %r' % kind)
            continue
        add(label, dim, 1 if ok else 0, rule.get('blocker'),
            '%s %s -> %s' % (kind, rule.get('regex'), 'ok' if ok else 'violated'))

    # -- declared state asserts --------------------------------------------- #
    for index, rule in enumerate(expected.get('state_asserts') or []):
        dim = rule.get('dim') or CHECK_DIMS['state_asserts']
        label = 'state_asserts[%d]' % index
        try:
            after_values = state_get(state_after, rule.get('path'))
            before_values = state_get(state_before, rule.get('path'))
            ok, detail = eval_assert(rule.get('op'), after_values, before_values,
                                     rule.get('value'))
        except (ValueError, TypeError) as exc:
            add(label, dim, 0, rule.get('blocker'), 'bad assert: %s' % exc)
            continue
        add(label, dim, 1 if ok else 0, rule.get('blocker'), detail)

    # -- ask / ceremony ----------------------------------------------------- #
    policy = expected.get('ask')
    if policy == 'forbidden':
        # A trailing offer in prose costs autonomy but is not a blocker: only an
        # explicit clarify call re-opens a settled decision.
        add('ask', CHECK_DIMS['ask'], 0 if asked else 1,
            're_asks_settled_decision' if (asked_tool and expected.get('ask_blocker'))
            else None,
            'asked=%s (clarify=%s), asking is forbidden' % (asked, asked_tool))
    elif policy == 'allowed':
        add('ask', CHECK_DIMS['ask'], None, None, 'asking is allowed but not required')
    else:
        add('ask', CHECK_DIMS['ask'], None, None, 'no ask policy')

    ceremony = []
    for event in uses:
        if event.get('name') == 'terminal' and 'checkpoint.py' in _dump(event.get('input') or {}):
            ceremony.append('ran checkpoint.py by hand')
            break
    max_calls = expected.get('max_tool_calls')
    if isinstance(max_calls, int) and len(uses) > max_calls:
        ceremony.append('%d tool calls > %d' % (len(uses), max_calls))
    add('ceremony', CHECK_DIMS['ceremony'], 0 if ceremony else 1, None,
        '; '.join(ceremony) if ceremony else '%d tool calls' % len(uses))

    add('cost', CHECK_DIMS['cost'], None, None, 'cost is set in aggregate')

    # -- fold checks into dims ---------------------------------------------- #
    for dim in DIMS:
        scores = [c['score'] for c in checks if c['dim'] == dim and c['score'] is not None]
        row['dims'][dim] = min(scores) if scores else None
    seen = []
    for check in checks:
        if check['blocker'] and check['blocker'] not in seen:
            seen.append(check['blocker'])
    row['blockers'] = seen
    return row


# --------------------------------------------------------------------------- #
# standalone sanity
# --------------------------------------------------------------------------- #

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('usage: oracle.py <reply-file>')
        return 0
    print(_dump({'final': parse_final(Path(argv[0]).read_text(encoding='utf-8'))}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
