#!/usr/bin/env python3
"""Blind LLM judging for the Longhaul eval harness.

The judge never sees a condition label: a deterministic permutation renames the
runs A/B/C, the second pass reverses the order, and a score is only used when
both passes agree. A slot-biased judge therefore yields null, never a mean.
"""
import hashlib
import importlib.util
import json
import re
import sys
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

BEGIN_MARKER = '<!-- judge:begin -->'
END_MARKER = '<!-- judge:end -->'
SCORES_RE = re.compile(r'<SCORES>\s*(.*?)\s*</SCORES>', re.S)
STATE_REL = '.task/state.json'
STATE_VIEW_LIMIT = 6000
FIELD_LIMIT = 300
ORACLE_MODULE = None


def _load_oracle():
    global ORACLE_MODULE
    if ORACLE_MODULE is not None:
        return ORACLE_MODULE
    path = Path(__file__).resolve().parent / 'oracle.py'
    spec = importlib.util.spec_from_file_location('lheval_oracle', path)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load %s' % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['lheval_oracle'] = module
    spec.loader.exec_module(module)
    ORACLE_MODULE = module
    return module


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #

def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


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
# view building
# --------------------------------------------------------------------------- #

def redact_secrets(text, secrets):
    out = '' if text is None else str(text)
    for secret in sorted({item for item in (secrets or []) if item}, key=len, reverse=True):
        out = out.replace(secret, 'REDACTED')
    return out


def _redact_leaves(value, secrets):
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, list):
        return [_redact_leaves(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_leaves(item, secrets) for key, item in value.items()}
    return value


def run_view(rundir, repo_root=None, secrets=()):
    """Reply + truncated tool trace + diff paths + normalised state for judging."""
    rundir = Path(rundir)
    events = _read_events(rundir / 'events.jsonl')
    meta = _read_json(rundir / 'meta.json', {}) or {}
    result = next((e for e in events if e.get('type') == 'result'), None)
    if isinstance(result, dict) and isinstance(result.get('text'), str):
        reply = result['text']
    else:
        reply = ''.join(e.get('text', '') for e in events if e.get('type') == 'text')

    tools, by_id = [], {}
    for event in events:
        kind = event.get('type')
        if kind == 'tool_use':
            entry = {'name': str(event.get('name') or ''),
                     'input': _dump(event.get('input') or {})[:FIELD_LIMIT],
                     'output': ''}
            tools.append(entry)
            if event.get('tool_call_id'):
                by_id[event['tool_call_id']] = entry
        elif kind == 'tool_result':
            output = event.get('output')
            text = output if isinstance(output, str) else _dump(output or '')
            entry = by_id.get(event.get('tool_call_id'))
            if entry is None:
                entry = next((t for t in reversed(tools) if not t['output']), None)
            if entry is not None:
                entry['output'] = text[:FIELD_LIMIT]

    before = _read_json(rundir / 'manifest.before.json', {}) or {}
    after = _read_json(rundir / 'manifest.after.json', {}) or {}
    diffs = []
    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            diffs.append('deleted ' + rel)
        elif rel not in before:
            diffs.append('added ' + rel)
        elif before[rel] != after[rel]:
            diffs.append('changed ' + rel)

    root = repo_root or meta.get('repo_root')
    state_path = rundir / 'work' / STATE_REL
    state_text = 'none'
    try:
        oracle = _load_oracle()
        state = oracle.normalize_state(state_path, root) if root else None
        if state is None:
            # The raw state is subject-controlled too: read it through the same
            # no-follow reader, never through a leaf or parent symlink (R4/R6).
            raw = oracle._read_regular(state_path)
            if raw is not None:
                try:
                    state = json.loads(raw.decode('utf-8'))
                except (UnicodeDecodeError, ValueError):
                    state = None
        if isinstance(state, dict):
            # provenance names the writer (e.g. the skill's own tooling) and would
            # tell the judge which condition it is reading.
            state = {key: value for key, value in state.items() if key != 'provenance'}
        if state is not None:
            state_text = _dump(state)[:STATE_VIEW_LIMIT]
        elif state_path.is_symlink():
            # A subject-planted link to a host file is not evidence: never follow it.
            state_text = 'unreadable (link outside the work tree)'
    except (OSError, ImportError, ValueError):
        state_text = 'unreadable'

    view = {'reply': reply, 'tools': tools, 'diffs': diffs, 'state': state_text}
    # The events file is already sanitised by the runner, but the judge packet is
    # a second egress path: redact every string leaf with the same secret list.
    return _redact_leaves(view, secrets) if secrets else view


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #

_REDACTIONS = (
    (re.compile(r'[^\s"\']*\.hermes/profiles/[^\s"\']*', re.I), '<profile>'),
    (re.compile(r'skills/longhaul', re.I), 'skills/<skill>'),
    (re.compile(r'lheval[a-z0-9]*', re.I), '<profile>'),
    (re.compile(r'longhaul|远航', re.I), '<subject>'),
    (re.compile(r'v1\.1', re.I), '<cond>'),
    (re.compile(r'\bv1\b', re.I), '<cond>'),
    (re.compile(r'\bbaseline\b', re.I), '<cond>'),
)


def redact_text(text):
    if not isinstance(text, str):
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_view(view):
    """Strip every condition, profile and subject identity from a run view."""
    out = {}
    for key, value in (view or {}).items():
        if key == 'tools':
            out[key] = [{k: redact_text(v) if isinstance(v, str) else v for k, v in tool.items()}
                        for tool in value or []]
        elif key == 'diffs':
            out[key] = [redact_text(item) for item in value or []]
        elif isinstance(value, str):
            out[key] = redact_text(value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# ordering and prompts
# --------------------------------------------------------------------------- #

def permutation(seed, conds):
    """Deterministic order: sha256(str(seed)|cond), so reruns are reproducible."""
    conds = list(conds)
    return sorted(conds, key=lambda cond: hashlib.sha256(
        ('%s|%s' % (seed, cond)).encode('utf-8')).hexdigest())


def labels_for(count):
    return [chr(ord('A') + index) for index in range(count)]


def rotations(order):
    """Every cyclic rotation of the start order: ABC, BCA, CAB."""
    order = list(order or [])
    return [order[index:] + order[:index] for index in range(len(order))]


def dims_of(case):
    """The judged dimensions a case declares; an empty list is a suite bug."""
    dims = [dim for dim in (case.get('judge') or []) if isinstance(dim, str)]
    if not dims:
        raise ValueError('case %r declares no judge dimension' % (case.get('id'),))
    return dims


def extract_judge_block(rubric_text):
    text = rubric_text or ''
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError('rubric needs exactly one judge:begin and one judge:end marker')
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise ValueError('judge:end appears before judge:begin')
    return text[start:end].strip('\n')


def _pairs(labelled):
    if isinstance(labelled, dict):
        return list(labelled.items())
    return [(label, view) for label, view in labelled]


def _render_view(view):
    lines = ['reply:', view.get('reply', ''), 'tool calls:']
    for tool in view.get('tools') or []:
        lines.append('- %s input=%s output=%s' % (tool.get('name', ''),
                                                  tool.get('input', ''), tool.get('output', '')))
    if not view.get('tools'):
        lines.append('- none')
    lines.append('changed paths: ' + (', '.join(view.get('diffs') or []) or 'none'))
    lines.append('state:')
    lines.append(str(view.get('state') or 'none'))
    return '\n'.join(lines)


def build_prompt(block, case, expect, labelled):
    """Fill {case_expect}, {dims} and {outputs}; every placeholder must exist."""
    for placeholder in ('{case_expect}', '{dims}', '{outputs}'):
        if placeholder not in block:
            raise ValueError('judge block is missing %s' % placeholder)
    dims = dims_of(case)
    case_expect = redact_text(_dump({'case': case.get('id'),
                                     'category': case.get('category'),
                                     'expect': expect}))
    chunks = []
    for label, view in _pairs(labelled):
        chunks.append('### OUTPUT %s\n%s' % (label, _render_view(redact_view(view))))
    return (block.replace('{case_expect}', case_expect)
                 .replace('{dims}', _dump(dims))
                 .replace('{outputs}', '\n\n'.join(chunks)))


# --------------------------------------------------------------------------- #
# parsing and stability
# --------------------------------------------------------------------------- #

def parse_scores(text, labels, dims):
    """Parse <SCORES>{label: {dim: 0|1|2, blockers: []}}</SCORES>; None if malformed.

    The last block wins: models that echo the instruction before answering must
    not be scored on the echoed example.
    """
    matches = list(SCORES_RE.finditer(text or ''))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].group(1))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for label in labels:
        entry = data.get(label)
        if not isinstance(entry, dict):
            return None
        row = {}
        for dim in dims:
            value = entry.get(dim)
            if isinstance(value, bool) or value not in (0, 1, 2):
                return None
            row[dim] = int(value)
        blockers = entry.get('blockers') or []
        if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers):
            return None
        row['blockers'] = blockers
        out[label] = row
    return out


def stability(passes):
    """Agreement only, over n passes: all equal -> s/2; any disagreement -> null.

    A dimension is only evaluated when every pass reports it; a pass that omits
    a dimension cannot establish agreement on it, so the dimension is dropped
    rather than guessed.
    """
    passes = [entry or {} for entry in (passes or [])]
    result = {'scores': {}, 'unstable': [], 'blockers': {}}
    if not passes:
        return result
    conds = sorted(set().union(*[set(entry) for entry in passes]))
    for cond in conds:
        entries = [(entry.get(cond) or {}) for entry in passes]
        common = set.intersection(*[set(entry) - {'blockers'} for entry in entries])
        scores = {}
        for dim in sorted(common):
            values = [entry.get(dim) for entry in entries]
            if any(value is None for value in values):
                scores[dim] = None
            elif len(set(values)) == 1:
                scores[dim] = values[0] / 2
            else:
                scores[dim] = None
                result['unstable'].append({'cond': cond, 'dim': dim})
        result['scores'][cond] = scores
        both = set.intersection(*[set(entry.get('blockers') or []) for entry in entries])
        result['blockers'][cond] = sorted(b for b in both if b == 'invents_evidence')
    return result


# --------------------------------------------------------------------------- #
# judge invocation
# --------------------------------------------------------------------------- #

# A judge only reads the packet. ``file`` is the *only* toolset with write
# access (read_file/write_file/patch/search_files into the real HOME); ``todo``
# exposes todo_list alone and is the smallest harmless set.
JUDGE_TOOLSET = 'todo'


def judge_command(hermes, qfile):
    argv = list(hermes or ['hermes'])
    return argv + ['chat', '--query-file', str(qfile), '-m', JUDGE[0], '--provider', JUDGE[1],
                   '-t', JUDGE_TOOLSET,
                   '-Q', '--ignore-user-config', '--ignore-rules', '--source', 'tool',
                   '--max-turns', '1']


def _split_call(result):
    """Accept (rc, text), [rc, text], {'rc','text'} or a bare text string."""
    if isinstance(result, str):
        return 0, result
    if isinstance(result, dict):
        return int(result.get('rc', 0)), str(result.get('text') or '')
    if isinstance(result, (tuple, list)) and len(result) == 2:
        return int(result[0]), str(result[1] or '')
    return 0, str(result or '')


def judge_case(case, views, block, call, seed):
    """One pass per condition: a shuffled start order and all its rotations."""
    case = dict(case or {})
    dims = dims_of(case)
    conds = list(views)
    if not conds:
        raise ValueError('judge_case needs at least one run view')
    rows = []
    for pass_no, sequence in enumerate(rotations(permutation(seed, conds)), 1):
        labels = dict(zip(labels_for(len(sequence)), sequence))
        labelled = [(label, views[cond]) for label, cond in labels.items()]
        prompt = build_prompt(block, case, case.get('expect'), labelled)
        status, parsed, raw = 'ok', None, ''
        for attempt in (1, 2):
            try:
                rc, raw = _split_call(call(prompt))
            except Exception as exc:  # a broken call is an infrastructure error
                status, raw = 'error', 'call failed: %s' % exc
                break
            if rc != 0:
                status = 'error'
                break
            parsed = parse_scores(raw, list(labels), dims)
            if parsed is not None:
                status = 'ok'
                break
            status = 'unparseable'
        resolved = None
        if parsed is not None:
            resolved = {}
            for label, cond in labels.items():
                resolved[cond] = dict(parsed[label])
        rows.append({'case': case.get('id'), 'trial': int(case.get('trial', 1)),
                     'pass': pass_no, 'order': sequence, 'labels': labels,
                     'status': status, 'dims': dims, 'parsed': resolved,
                     # The raw response may quote the packet (and whatever the
                     # subject wrote). Only its digest is kept: storing the text
                     # would make the judge log a second egress path (R4 MAJOR).
                     'text_sha256': hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest(),
                     'chars': len(raw)})
    return rows


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('usage: judge.py <rubric-file>')
        return 0
    print(extract_judge_block(Path(argv[0]).read_text(encoding='utf-8')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
