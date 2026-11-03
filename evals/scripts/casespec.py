#!/usr/bin/env python3
"""Load and validate the eval suite: cases.jsonl, scenarios/<id>/, rubric.md.

Every defect is reported as a named error string. `validate_suite` never raises
for bad content, so `run_evals.py validate` can print all of them and exit 1.

The suite has two halves that must agree: the case table (what is expected) and
the fixtures (what a subject actually receives). The checks here are the only
thing standing between a plausible-looking fixture and a measurable one, so each
rule is narrow and each message names the case and the offending value.

Stdlib only (Python 3.10+). No network, no hermes, no user files.
"""
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# shared contract constants (identical in every lane; INT asserts equality)
# --------------------------------------------------------------------------- #

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

STATUSES = ('ok', 'provider_429', 'timeout', 'empty_answer', 'hermes_crash', 'provider_error',
            'interrupted', 'preflight_fail', 'skipped_preflight')

RETRYABLE = ('provider_429', 'timeout', 'empty_answer', 'hermes_crash', 'provider_error',
             'interrupted')

CASE_IDS = ('recover-interrupt', 'file-changed', 'stale-evidence', 'tool-unavailable',
            'external-ambiguous', 'subagent-claims', 'scope-change', 'repeat-question',
            'control-trivial', 'control-finish')

# git cannot store an empty directory. The two controls, whose golden end state is the unchanged repo,
# therefore may have no golden/overlay at all; every other case must have one.
EMPTY_OVERLAY_IDS = ('control-trivial', 'control-finish')

FINAL_CONTRACT = ('When you stop, end your reply with exactly one line: FINAL: complete, '
                  'FINAL: incomplete, or FINAL: blocked.')

ALLOWED_ENV = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TERM', 'XDG_RUNTIME_DIR', 'TMPDIR')

PROFILE_RE = r'^lheval[a-z0-9]+$'

SUBJECT = ('deepseek-v4.1-flash:1017', 'ollama-cloud')

JUDGE = ('claude-sonnet-5-5', 'anthropic')

# --------------------------------------------------------------------------- #
# suite-specific constants
# --------------------------------------------------------------------------- #

STATE_REL = '.task/state.json'

# P11: condition names, tolerant of the neighbours that a word boundary would hide.
CONDITION_RE = re.compile(r'baseline|v1\.1|(?<![A-Za-z0-9])v1(?![0-9])', re.IGNORECASE)

MUTANT_KINDS = ('golden', 'null', 'near_miss', 'alternative')

CASE_KEYS = ('id', 'category', 'expect', 'scenario', 'oracle', 'judge')

EXPECT_KEYS = ('state_file', 'initial_gate', 'final_status', 'write_scope', 'unchanged',
               'must_match', 'must_not_exist', 'tool_rules', 'state_asserts', 'ask',
               'ask_blocker', 'max_tool_calls', 'answer_regex')

RULE_KINDS = ('require', 'forbid', 'order')

RULE_WHEN = ('always', 'claim')

ASSERT_OPS = ('eq', 'in', 'absent_or_in', 'unchanged', 'nonempty', 'any_match')

ASSERT_ROOTS = ('status', 'acceptance', 'evidence', 'decisions', 'blockers', 'waivers')

FINAL_STATUSES = ('complete', 'incomplete', 'blocked')

ASK_VALUES = ('forbidden', 'allowed')

# D3: the fixtures never name the skill that is being measured.
FORBIDDEN_TOKENS = ('longhaul', '远航')

# P8: no host instruction file and no injected skill tree may travel with a fixture.
# Hermes itself reads .hermes.md, AGENTS.md and the .cursor/ tree, so a fixture that carries
# one of them hands the subject a second instruction source outside the condition.
FORBIDDEN_FIXTURE_NAMES = ('AGENTS.md', 'AGENTS.override.md', 'agents.md', 'CLAUDE.md',
                           '.cursorrules', '.cursor', 'SOUL.md', 'HERMES.md', '.hermes.md',
                           '.hermes', '.agents')

# Derived files a test run leaves behind. They are not work: copying them into a workdir
# invents an out-of-scope write before the subject starts.
CACHE_DIR_NAMES = ('__pycache__', '.pytest_cache')

CACHE_SUFFIXES = ('.pyc', '.pyo')

MARKER_BEGIN = '<!-- generated:begin -->'

MARKER_END = '<!-- generated:end -->'

JUDGE_BEGIN = '<!-- judge:begin -->'

JUDGE_END = '<!-- judge:end -->'

JUDGE_PLACEHOLDERS = ('{case_expect}', '{dims}', '{outputs}')

ASSERT_PATH_RE = re.compile(
    r'^(?:status|acceptance|evidence|decisions|blockers|waivers)'
    r'(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[A-Za-z_][A-Za-z0-9_]*=[^\]\s]+\])*$')

FINAL_LINE_RE = re.compile(r'^FINAL:\s*(complete|incomplete|blocked)\s*$')

WEIGHT_ROW_RE = re.compile(r'^\|\s*([a-z][a-z0-9_]*)\s*\|\s*(-?\d+)\s*\|\s*$')


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _scenario_dir(evals_dir, case_id):
    return Path(evals_dir) / 'scenarios' / case_id


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _relative_ok(rel):
    """A work-relative path: no absolute part, no parent escapes, no traversal."""
    if not isinstance(rel, str) or not rel.strip():
        return False
    if Path(rel).is_absolute() or rel.startswith('/') or rel.startswith('~'):
        return False
    return '..' not in Path(rel).parts


def _match_scope(rel, globs):
    return any(fnmatch.fnmatch(rel, pattern) for pattern in globs)


def final_line(text):
    """The claimed status: the last line matching the FINAL contract, else 'missing'."""
    claim = 'missing'
    for line in str(text or '').splitlines():
        hit = FINAL_LINE_RE.match(line.strip())
        if hit:
            claim = hit.group(1)
    return claim


def _question_lines(text):
    out = []
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if not stripped or FINAL_LINE_RE.match(stripped):
            continue
        if stripped.startswith(('`', '    ')):
            continue
        if stripped.endswith('?') or stripped.endswith('？'):
            out.append(stripped)
    return out


def _load_events(path):
    events = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def _tool_uses(events):
    return [event for event in events if event.get('type') == 'tool_use']


def _iter_files(root):
    root = Path(root)
    for path in sorted(root.rglob('*')):
        if path.is_file():
            yield path


def _text_of(path):
    try:
        return Path(path).read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return ''


# --------------------------------------------------------------------------- #
# public loaders
# --------------------------------------------------------------------------- #

def _parse_cases(evals_dir, errors):
    path = Path(evals_dir) / 'cases.jsonl'
    if not path.is_file():
        errors.append('cases.jsonl: missing at %s' % path)
        return None
    rows = []
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append('cases.jsonl:%d: not JSON (%s)' % (lineno, exc))
            continue
        if not isinstance(row, dict):
            errors.append('cases.jsonl:%d: not an object' % lineno)
            continue
        rows.append(row)
    return rows


def load_cases(evals_dir):
    """All case rows. Raises ValueError naming the first structural problem."""
    errors = []
    rows = _parse_cases(evals_dir, errors)
    if errors:
        raise ValueError('; '.join(errors))
    if rows is None:
        raise ValueError('cases.jsonl: missing')
    return rows


def load_expected(evals_dir, case_id):
    return _read_json(_scenario_dir(evals_dir, case_id) / 'expected.json')


def prompt_text(evals_dir, case_id):
    return (_scenario_dir(evals_dir, case_id) / 'prompt.md').read_text(encoding='utf-8')


def materialize(evals_dir, case_id, state_schema, workdir):
    """Copy the fixture repo into `workdir` and drop the requested state variant in.

    Refuses a non-empty workdir: materialising twice would mix two states, and a
    leftover file would silently become part of the run's before-manifest.
    """
    if state_schema not in ('v1', 'v2', 'none'):
        raise ValueError('state_schema must be v1, v2 or none, not %r' % (state_schema,))
    scenario = _scenario_dir(evals_dir, case_id)
    repo = scenario / 'repo'
    if not repo.is_dir():
        raise FileNotFoundError('fixture repo missing: %s' % repo)
    expected = _read_json(scenario / 'expected.json')
    state_rel = expected.get('state_file')
    workdir = Path(workdir)
    if workdir.exists():
        if not workdir.is_dir():
            raise NotADirectoryError('workdir is not a directory: %s' % workdir)
        if any(workdir.iterdir()):
            raise FileExistsError('workdir is not empty: %s' % workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo, workdir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*(CACHE_DIR_NAMES + ('*.pyc', '*.pyo'))))
    if state_rel:
        if state_schema == 'none':
            raise ValueError('case %s ships a state file; state_schema=none is invalid'
                             % case_id)
        source = scenario / ('state.%s.json' % state_schema)
        if not source.is_file():
            raise FileNotFoundError('state variant missing: %s' % source)
        target = workdir / state_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return workdir


def apply_golden(evals_dir, case_id, workdir):
    """Copy golden/overlay/** onto a materialised workdir; returns the written paths."""
    overlay = _scenario_dir(evals_dir, case_id) / 'golden' / 'overlay'
    written = []
    if not overlay.is_dir():
        return written
    workdir = Path(workdir)
    for path in _iter_files(overlay):
        rel = path.relative_to(overlay)
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        written.append(rel.as_posix())
    return written


def golden_overlay_paths(evals_dir, case_id):
    overlay = _scenario_dir(evals_dir, case_id) / 'golden' / 'overlay'
    if not overlay.is_dir():
        return []
    return [path.relative_to(overlay).as_posix() for path in _iter_files(overlay)]


# --------------------------------------------------------------------------- #
# rubric
# --------------------------------------------------------------------------- #

def parse_weights(text):
    """Map dimension -> weight from `| <dim> | <int> |` rows (last row wins)."""
    weights = {}
    for line in str(text or '').splitlines():
        hit = WEIGHT_ROW_RE.match(line.strip())
        if hit:
            weights[hit.group(1)] = int(hit.group(2))
    return weights


def rubric_blockers(text):
    """The `- <name>` entries of the `## Blockers` section, in file order."""
    names = []
    inside = False
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            heading = stripped.lstrip('#').strip().lower()
            inside = heading == 'blockers'
            continue
        if not inside:
            continue
        if stripped.startswith('- '):
            token = stripped[2:].strip()
            token = re.split(r'[\s:(—]', token, maxsplit=1)[0]
            token = token.strip('`*.,;')
            if token:
                names.append(token)
    return names


def validate_rubric(text):
    """Named errors for a rubric that a judge packet cannot be built from."""
    errors = []
    text = str(text or '')
    for marker, label in ((JUDGE_BEGIN, 'judge:begin'), (JUDGE_END, 'judge:end')):
        count = text.count(marker)
        if count != 1:
            errors.append('rubric: %s marker found %d times (need exactly 1)' % (label, count))
    block = ''
    if not errors:
        start = text.index(JUDGE_BEGIN) + len(JUDGE_BEGIN)
        stop = text.index(JUDGE_END)
        if stop <= start:
            errors.append('rubric: judge:end appears before judge:begin')
        else:
            block = text[start:stop]
            if not block.strip():
                errors.append('rubric: judge block is empty')
            for placeholder in JUDGE_PLACEHOLDERS:
                if placeholder not in block:
                    errors.append('rubric judge block: placeholder %s missing' % placeholder)
            for marker in ('<SCORES>', '</SCORES>'):
                if marker not in block:
                    errors.append('rubric judge block: %s missing' % marker)
            if 'blockers' not in block:
                errors.append('rubric judge block: no "blockers" field described')
    found = parse_weights(text)
    for dim in DIMS:
        want = WEIGHTS[dim]
        got = found.get(dim)
        if got != want:
            errors.append('rubric weights: %s expected %d, found %s'
                          % (dim, want, 'missing' if got is None else got))
    for dim in sorted(set(found) - set(DIMS)):
        errors.append('rubric weights: unknown dimension %r' % dim)
    names = rubric_blockers(text)
    for name in BLOCKERS:
        if name not in names:
            errors.append('rubric blockers: %r missing from the "## Blockers" list' % name)
    for name in names:
        if name not in BLOCKERS:
            errors.append('rubric blockers: unknown blocker %r' % name)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    for name in duplicates:
        errors.append('rubric blockers: %r listed more than once' % name)
    return errors


# --------------------------------------------------------------------------- #
# suite validation
# --------------------------------------------------------------------------- #

def _check_case_shape(row, errors):
    case_id = row.get('id')
    label = 'case %s' % (case_id if isinstance(case_id, str) and case_id else '<no id>')
    extra = sorted(set(row) - set(CASE_KEYS))
    missing = sorted(set(CASE_KEYS) - set(row))
    if extra or missing:
        errors.append('%s: expected 6 keys, found %d (extra=%s missing=%s)'
                      % (label, len(row), extra, missing))
    for key in ('id', 'category', 'expect'):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append('%s: %s must be a non-empty string' % (label, key))
    want_scenario = 'scenarios/%s' % case_id
    if row.get('scenario') != want_scenario:
        errors.append('%s: scenario must be %r, found %r'
                      % (label, want_scenario, row.get('scenario')))
    oracle = row.get('oracle')
    if not isinstance(oracle, list) or not oracle:
        errors.append('%s: oracle must be a non-empty list' % label)
        oracle = []
    seen = set()
    for check in oracle:
        if not isinstance(check, str) or check not in CHECK_DIMS:
            errors.append('%s: unknown check %r' % (label, check))
            continue
        if check in seen:
            errors.append('%s: duplicate check %r' % (label, check))
        seen.add(check)
    for check in MANDATORY_CHECKS:
        if check not in oracle:
            errors.append('%s: mandatory check %r missing from oracle' % (label, check))
    covered = {}
    for check in oracle:
        dim = CHECK_DIMS.get(check)
        if dim in (None, '*'):
            continue
        covered.setdefault(dim, check)
    judge = row.get('judge')
    if not isinstance(judge, list):
        errors.append('%s: judge must be a list' % label)
        judge = []
    for dim in judge:
        if not isinstance(dim, str) or dim not in DIMS:
            errors.append('%s: unknown judge dimension %r' % (label, dim))
            continue
        if judge.count(dim) > 1:
            errors.append('%s: duplicate judge dimension %r' % (label, dim))
        if dim in covered:
            errors.append('%s: judge dimension %r is already scored by the oracle check %s'
                          % (label, dim, covered[dim]))
    return oracle, judge


def _check_prompt(scenario, row, errors):
    case_id = row.get('id')
    path = scenario / 'prompt.md'
    if not path.is_file():
        errors.append('case %s: prompt.md missing' % case_id)
        return
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        errors.append('case %s: prompt.md is empty' % case_id)
        return
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != FINAL_CONTRACT:
        errors.append('case %s: prompt.md does not end with the FINAL contract line' % case_id)
    # P11 applies to the prompt text as well: a subject that reads "condition v1.1" or a case
    # id in its own task is told which cell of the matrix it is in.
    for match in CONDITION_RE.finditer(text):
        errors.append('case %s: prompt.md names the condition %r' % (case_id, match.group(0)))
    lowered = text.lower()
    for other in CASE_IDS:
        if other.lower() in lowered:
            errors.append('case %s: prompt.md names the case id %r' % (case_id, other))


def _check_fixture_tree(scenario, row, errors):
    case_id = row.get('id')
    if not (scenario / 'repo').is_dir():
        errors.append('case %s: repo/ missing' % case_id)
    for path in scenario.rglob('*'):
        rel = path.relative_to(scenario).as_posix()
        if path.name in FORBIDDEN_FIXTURE_NAMES:
            errors.append('case %s: %r must not travel with a fixture (found %s)'
                          % (case_id, path.name, rel))
        if path.name in CACHE_DIR_NAMES or path.suffix.lower() in CACHE_SUFFIXES:
            errors.append('case %s: cache file %s must not travel with a fixture '
                          '(.pyc / __pycache__ / .pytest_cache)' % (case_id, rel))


def _check_tokens(scenario, row, errors):
    """Scan every fixture byte, not every decodable text: a token in a .pyc or a PNG is
    still a token the subject can read, and `read_text` would have skipped it."""
    case_id = row.get('id')
    needles = [(token, token.lower().encode('utf-8')) for token in FORBIDDEN_TOKENS]
    for path in _iter_files(scenario):
        rel = path.relative_to(scenario).as_posix()
        try:
            data = path.read_bytes().lower()
        except OSError:
            continue
        haystacks = (rel.lower().encode('utf-8'), data)
        for token, needle in needles:
            if any(needle in hay for hay in haystacks):
                errors.append('case %s: forbidden token %r in %s' % (case_id, token, rel))
                break


def _check_expected_shape(expected, row, errors):
    case_id = row.get('id')
    label = 'case %s: expected.json' % case_id
    if not isinstance(expected, dict):
        errors.append('%s: not an object' % label)
        return
    extra = sorted(set(expected) - set(EXPECT_KEYS))
    missing = sorted(set(EXPECT_KEYS) - set(expected))
    if extra or missing:
        errors.append('%s: expected %d keys, found %d (extra=%s missing=%s)'
                      % (label, len(EXPECT_KEYS), len(expected), extra, missing))
    state_file = expected.get('state_file')
    if state_file is None:
        if expected.get('initial_gate') is not None:
            errors.append('%s: initial_gate must be null when there is no state file' % label)
    else:
        if state_file != STATE_REL:
            errors.append('%s: state_file must be %r, found %r' % (label, STATE_REL, state_file))
        gate = expected.get('initial_gate')
        if not isinstance(gate, dict) or set(gate) != {'ok', 'error_contains'}:
            errors.append('%s: initial_gate must be null or {ok, error_contains}' % label)
        else:
            if not isinstance(gate.get('ok'), bool):
                errors.append('%s: initial_gate.ok must be a bool' % label)
            if not isinstance(gate.get('error_contains'), list) or \
                    any(not isinstance(item, str) or not item for item in gate['error_contains']):
                errors.append('%s: initial_gate.error_contains must be a list of strings' % label)
    statuses = expected.get('final_status')
    if not isinstance(statuses, list) or not statuses:
        errors.append('%s: final_status must be a non-empty list' % label)
    else:
        for status in statuses:
            if status not in FINAL_STATUSES:
                errors.append('%s: invalid final status %r' % (label, status))
    for key in ('write_scope', 'unchanged', 'must_not_exist'):
        value = expected.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append('%s: %s must be a list of strings' % (label, key))
            continue
        for rel in value:
            if not _relative_ok(rel):
                errors.append('%s: %s entry %r is not a work-relative path' % (label, key, rel))
    must_match = expected.get('must_match')
    if not isinstance(must_match, dict):
        errors.append('%s: must_match must be an object' % label)
    else:
        for rel, spec in must_match.items():
            if not _relative_ok(rel):
                errors.append('%s: must_match key %r is not a work-relative path' % (label, rel))
                continue
            if isinstance(spec, str):
                pattern = spec
            elif isinstance(spec, dict) and isinstance(spec.get('regex'), str):
                pattern = spec['regex']
                if 'count' in spec and not isinstance(spec['count'], int):
                    errors.append('%s: must_match[%r].count must be an integer' % (label, rel))
            else:
                errors.append('%s: must_match[%r] must be a regex or {regex, count}' % (label, rel))
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append('%s: must_match[%r] regex does not compile (%s)' % (label, rel, exc))
    rules = expected.get('tool_rules')
    if not isinstance(rules, list):
        errors.append('%s: tool_rules must be a list' % label)
        rules = []
    for index, rule in enumerate(rules):
        spot = '%s: tool_rules[%d]' % (label, index)
        if not isinstance(rule, dict):
            errors.append('%s must be an object' % spot)
            continue
        if rule.get('kind') not in RULE_KINDS:
            errors.append('%s: invalid kind %r' % (spot, rule.get('kind')))
        if not isinstance(rule.get('tool'), str) or not rule.get('tool'):
            errors.append('%s: tool must be a non-empty string' % spot)
        pattern = rule.get('regex')
        if not isinstance(pattern, str):
            errors.append('%s: regex must be a string' % spot)
        else:
            try:
                re.compile(pattern)
            except re.error:
                errors.append('%s: regex does not compile' % spot)
        if rule.get('kind') == 'order':
            before = rule.get('before_regex')
            if not isinstance(before, str):
                errors.append('%s: order rule needs a before_regex' % spot)
            else:
                try:
                    re.compile(before)
                except re.error:
                    errors.append('%s: before_regex does not compile' % spot)
        if rule.get('when') not in RULE_WHEN:
            errors.append('%s: when must be one of %s' % (spot, '|'.join(RULE_WHEN)))
        if rule.get('dim') not in DIMS:
            errors.append('%s: dim %r is not a dimension' % (spot, rule.get('dim')))
        if rule.get('blocker') is not None and rule.get('blocker') not in BLOCKERS:
            errors.append('%s: unknown blocker %r' % (spot, rule.get('blocker')))
    asserts = expected.get('state_asserts')
    if not isinstance(asserts, list):
        errors.append('%s: state_asserts must be a list' % label)
        asserts = []
    for index, item in enumerate(asserts):
        spot = '%s: state_asserts[%d]' % (label, index)
        if not isinstance(item, dict):
            errors.append('%s must be an object' % spot)
            continue
        path = item.get('path')
        if not isinstance(path, str) or not ASSERT_PATH_RE.match(path):
            errors.append('%s: invalid path %r' % (spot, path))
        elif path.split('.')[0].split('[')[0] not in ASSERT_ROOTS:
            errors.append('%s: path root of %r is not one of %s'
                          % (spot, path, ', '.join(ASSERT_ROOTS)))
        op = item.get('op')
        if op not in ASSERT_OPS:
            errors.append('%s: invalid op %r' % (spot, op))
        if op in ('eq', 'in', 'absent_or_in', 'any_match') and 'value' not in item:
            errors.append('%s: op %r needs a value' % (spot, op))
        if op in ('in', 'absent_or_in') and not isinstance(item.get('value'), list):
            errors.append('%s: op %r needs a list value' % (spot, op))
        if op == 'any_match':
            try:
                re.compile(str(item.get('value')))
            except re.error:
                errors.append('%s: any_match value must be a regex' % spot)
        if item.get('dim') not in DIMS:
            errors.append('%s: dim %r is not a dimension' % (spot, item.get('dim')))
        if item.get('blocker') is not None and item.get('blocker') not in BLOCKERS:
            errors.append('%s: unknown blocker %r' % (spot, item.get('blocker')))
    if expected.get('ask') not in ASK_VALUES:
        errors.append('%s: ask must be forbidden or allowed' % label)
    if not isinstance(expected.get('ask_blocker'), bool):
        errors.append('%s: ask_blocker must be a bool' % label)
    elif expected.get('ask') == 'allowed' and expected.get('ask_blocker'):
        errors.append('%s: ask_blocker cannot be true when asking is allowed' % label)
    budget = expected.get('max_tool_calls')
    if budget is not None and (not isinstance(budget, int) or budget < 0):
        errors.append('%s: max_tool_calls must be null or a non-negative integer' % label)
    regex = expected.get('answer_regex')
    if regex is not None:
        if not isinstance(regex, str):
            errors.append('%s: answer_regex must be null or a string' % label)
        else:
            try:
                re.compile(regex)
            except re.error:
                errors.append('%s: answer_regex does not compile' % label)
    return rules, asserts


def _check_consistency(expected, oracle, row, errors):
    case_id = row.get('id')
    label = 'case %s' % case_id
    has_state = expected.get('state_file') is not None
    pairs = (('tool_rules', bool(expected.get('tool_rules'))),
             ('state_asserts', bool(expected.get('state_asserts'))),
             ('unchanged', bool(expected.get('unchanged'))),
             ('answer', expected.get('answer_regex') is not None),
             ('gate_on_claim', has_state),
             ('state_valid', has_state))
    for check, needed in pairs:
        if needed and check not in oracle:
            errors.append('%s: oracle must declare %r (the expectation uses it)' % (label, check))
        if not needed and check in oracle:
            errors.append('%s: oracle declares %r but the expectation is empty for it'
                          % (label, check))


def _check_state_files(scenario, expected, row, errors):
    case_id = row.get('id')
    state_rel = expected.get('state_file')
    variants = ('v1', 'v2')
    if not state_rel:
        for variant in variants:
            path = scenario / ('state.%s.json' % variant)
            if path.exists():
                errors.append('case %s: state.%s.json present but state_file is null'
                              % (case_id, variant))
        return
    for variant, schema in zip(variants, (1, 2)):
        path = scenario / ('state.%s.json' % variant)
        if not path.is_file():
            errors.append('case %s: state.%s.json missing' % (case_id, variant))
            continue
        try:
            state = _read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append('case %s: state.%s.json not readable JSON (%s)' % (case_id, variant, exc))
            continue
        if state.get('schema_version') != schema:
            errors.append('case %s: state.%s.json has schema_version %r (want %d)'
                          % (case_id, variant, state.get('schema_version'), schema))
        if state.get('status') not in ('in_progress', 'blocked', 'complete'):
            errors.append('case %s: state.%s.json has invalid status %r'
                          % (case_id, variant, state.get('status')))


def _run_gate(repo_root, state_path):
    script = Path(repo_root) / 'scripts' / 'checkpoint.py'
    if not script.is_file():
        return None
    proc = subprocess.run([sys.executable, str(script), 'gate', str(state_path)],
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {'ok': False,
                   'errors': ['checkpoint gate produced no JSON: %r'
                              % (proc.stdout.strip() or proc.stderr.strip())[:200]]}
    if not isinstance(payload, dict):
        payload = {'ok': False, 'errors': ['checkpoint gate produced %r' % type(payload).__name__]}
    payload.setdefault('errors', [])
    return payload


def _materialised(evals_dir, case_id, schema):
    tmpdir = tempfile.mkdtemp(prefix='lheval-case-')
    work = Path(tmpdir) / 'work'
    materialize(evals_dir, case_id, schema, work)
    return tmpdir, work


def _check_initial_gate(evals_dir, scenario, expected, row, repo_root, errors):
    case_id = row.get('id')
    state_rel = expected.get('state_file')
    if not state_rel or repo_root is None:
        return
    want = expected.get('initial_gate')
    if not isinstance(want, dict):
        return
    for schema in ('v1', 'v2'):
        try:
            tmpdir, work = _materialised(evals_dir, case_id, schema)
        except (OSError, ValueError) as exc:
            errors.append('case %s: cannot materialise state.%s (%s)' % (case_id, schema, exc))
            continue
        try:
            payload = _run_gate(repo_root, work / state_rel)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if payload is None:
            continue
        ok = bool(payload.get('ok'))
        if ok != bool(want.get('ok')):
            errors.append('case %s: initial_gate.ok is %r but checkpoint gate on state.%s '
                          'returned %r' % (case_id, want.get('ok'), schema, ok))
        text = '\n'.join(str(item) for item in payload.get('errors') or [])
        for needle in want.get('error_contains') or []:
            if needle not in text:
                errors.append('case %s: initial_gate expects the gate error %r on state.%s; '
                              'got %r' % (case_id, needle, schema, text[:240]))


def _check_golden(evals_dir, scenario, expected, row, oracle, repo_root, errors):
    case_id = row.get('id')
    golden = scenario / 'golden'
    overlay = golden / 'overlay'
    reply_path = golden / 'reply.md'
    events_path = golden / 'events.jsonl'
    needed = ((golden, reply_path, events_path) if case_id in EMPTY_OVERLAY_IDS
              else (golden, overlay, reply_path, events_path))
    for path in needed:
        if not path.exists():
            label = '' if path == golden else '/' + path.relative_to(golden).as_posix()
            errors.append('case %s: golden%s missing' % (case_id, label))
    # Empty controls may omit the directory; a dangling link, a link to a
    # directory, or a regular file is not an omitted directory. Never follow it.
    if overlay.is_symlink() or (overlay.exists() and not overlay.is_dir()):
        errors.append('case %s: golden/overlay must be a directory, not a symlink or file' % case_id)
        return
    if not reply_path.is_file() or not events_path.is_file():
        return
    reply = reply_path.read_text(encoding='utf-8')
    if not reply.strip():
        errors.append('case %s: golden/reply.md is empty' % case_id)
    try:
        events = _load_events(events_path)
    except (json.JSONDecodeError, OSError) as exc:
        errors.append('case %s: golden/events.jsonl not readable (%s)' % (case_id, exc))
        return
    scope = expected.get('write_scope') or []
    for rel in golden_overlay_paths(evals_dir, case_id):
        if not _match_scope(rel, scope):
            errors.append('case %s: golden overlay path %r is outside write_scope' % (case_id, rel))
        if rel in (expected.get('unchanged') or []):
            errors.append('case %s: golden overlay rewrites the unchanged file %r'
                          % (case_id, rel))
    claim = final_line(reply)
    if claim == 'missing':
        errors.append('case %s: golden reply has no FINAL line' % case_id)
    elif claim not in (expected.get('final_status') or []):
        errors.append('case %s: golden reply claims %r, not one of %r'
                      % (case_id, claim, expected.get('final_status')))
    regex = expected.get('answer_regex')
    if regex and not re.search(regex, reply):
        errors.append('case %s: golden reply does not match answer_regex' % case_id)
    if expected.get('ask') == 'forbidden':
        questions = _question_lines(reply)
        if questions:
            errors.append('case %s: golden reply asks a question (%r)' % (case_id, questions[0]))
        for event in _tool_uses(events):
            if event.get('name') in ('clarify', 'ask_user'):
                errors.append('case %s: golden events call %r' % (case_id, event.get('name')))
    budget = expected.get('max_tool_calls')
    if budget is not None and len(_tool_uses(events)) > budget:
        errors.append('case %s: golden events use %d tool calls, budget is %d'
                      % (case_id, len(_tool_uses(events)), budget))
    if expected.get('state_file') and repo_root is not None and \
            'complete' in (expected.get('final_status') or []):
        tmpdir, work = _materialised(evals_dir, case_id, 'v2')
        try:
            apply_golden(evals_dir, case_id, work)
            payload = _run_gate(repo_root, work / expected['state_file'])
            truth_errors = []
            _check_golden_truth(scenario, work, expected, oracle, row, truth_errors)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        errors.extend(truth_errors)
        if payload is None:
            return
        if not payload.get('ok'):
            errors.append('case %s: golden end state does not pass the gate (%r)'
                          % (case_id, (payload.get('errors') or [])[:3]))


def _check_golden_truth(scenario, work, expected, oracle, row, errors):
    """must_match / must_not_exist must hold on the golden end state."""
    case_id = row.get('id')
    if 'ground_truth' not in oracle:
        return
    for rel, spec in (expected.get('must_match') or {}).items():
        path = Path(work) / rel
        if not path.is_file():
            errors.append('case %s: golden state lost must_match file %r' % (case_id, rel))
            continue
        text = _text_of(path)
        pattern = spec if isinstance(spec, str) else spec['regex']
        count = None if isinstance(spec, str) else spec.get('count')
        found = len(re.findall(pattern, text))
        if count is not None:
            if found != count:
                errors.append('case %s: golden %r matches %r %d times, want %d'
                              % (case_id, rel, pattern, found, count))
        elif not found:
            errors.append('case %s: golden %r does not match %r' % (case_id, rel, pattern))
    for rel in expected.get('must_not_exist') or []:
        if (Path(work) / rel).exists():
            errors.append('case %s: golden state creates must_not_exist %r' % (case_id, rel))


def _check_fixture_expectations(scenario, expected, row, errors):
    case_id = row.get('id')
    repo = scenario / 'repo'
    if not repo.is_dir():
        return
    for rel in expected.get('unchanged') or []:
        if not (repo / rel).is_file():
            errors.append('case %s: unchanged file %r is not part of the fixture' % (case_id, rel))
    for rel in expected.get('must_not_exist') or []:
        if (repo / rel).exists():
            errors.append('case %s: must_not_exist path %r already exists in the fixture'
                          % (case_id, rel))
    for rel in expected.get('write_scope') or []:
        if not _relative_ok(rel):
            errors.append('case %s: write_scope entry %r is not a work-relative path'
                          % (case_id, rel))


def _check_mutants(evals_dir, scenario, expected, row, errors):
    """Every case ships four calibration runs: golden, null, a near miss and one correct
    alternative route. Golden and null are the two extremes and hide the failure modes in
    between, which is how the order-rule and evidence-handling defects survived R1.

    Format of scenarios/<id>/mutants.json:
        {"case": "<id>", "mutants": [{"kind", "label", "files", "reply", "events",
                                      "expected": {"blockers", "score_min", "score_max"}}]}
    `files` is "golden" (inherit golden/overlay) or {work-relative path: text}; `events` is a
    list of {name, input, output} tool calls. The scores are the values the oracle must
    reproduce, so a mutant that scores outside its band is an oracle defect, not a fixture one.
    """
    case_id = row.get('id')
    path = scenario / 'mutants.json'
    if not path.is_file():
        errors.append('case %s: mutants.json missing (golden, null, near_miss, alternative)'
                      % case_id)
        return
    try:
        table = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append('case %s: mutants.json not readable (%s)' % (case_id, exc))
        return
    if not isinstance(table, dict) or table.get('case') != case_id:
        errors.append('case %s: mutants.json must carry "case": %r' % (case_id, case_id))
        return
    entries = table.get('mutants')
    if not isinstance(entries, list) or len(entries) != len(MUTANT_KINDS):
        errors.append('case %s: mutants must list %d entries, found %s'
                      % (case_id, len(MUTANT_KINDS),
                         len(entries) if isinstance(entries, list) else 'none'))
        return
    by_kind = {}
    for item in entries:
        if not isinstance(item, dict) or item.get('kind') not in MUTANT_KINDS:
            errors.append('case %s: mutant kind must be one of %s'
                          % (case_id, ', '.join(MUTANT_KINDS)))
            continue
        if item['kind'] in by_kind:
            errors.append('case %s: duplicate mutant kind %r' % (case_id, item['kind']))
        by_kind[item['kind']] = item
    missing = [kind for kind in MUTANT_KINDS if kind not in by_kind]
    if missing:
        errors.append('case %s: mutant kinds missing %s' % (case_id, ', '.join(missing)))
    scope = expected.get('write_scope') or []
    for kind in MUTANT_KINDS:
        item = by_kind.get(kind)
        if item is None:
            continue
        spot = 'case %s: mutant %r' % (case_id, kind)
        label = item.get('label')
        if not isinstance(label, str) or not label.strip():
            errors.append(spot + ': label required')
        files = item.get('files', {})
        if files == 'golden':
            if kind != 'golden':
                errors.append(spot + ': only the golden mutant may inherit golden/overlay')
        elif isinstance(files, dict):
            for rel, content in files.items():
                if not _relative_ok(rel):
                    errors.append(spot + ': file %r is not a work-relative path' % rel)
                elif not isinstance(content, str):
                    errors.append(spot + ': content of %r must be text' % rel)
                elif kind == 'alternative' and not _match_scope(rel, scope):
                    errors.append(spot + ': the correct alternative writes %r outside '
                                         'write_scope' % rel)
        else:
            errors.append(spot + ': files must be "golden" or an object of relpath -> text')
        if kind == 'null' and item.get('files', {}) != {}:
            errors.append(spot + ': the null run writes nothing')
        reply = item.get('reply')
        if not isinstance(reply, str) or final_line(reply) == 'missing':
            errors.append(spot + ': reply must end with a FINAL line')
        elif kind in ('golden', 'alternative') and \
                final_line(reply) not in (expected.get('final_status') or []):
            errors.append(spot + ': reply claims %r, not one of %r'
                          % (final_line(reply), expected.get('final_status')))
        events = item.get('events')
        if not isinstance(events, list):
            errors.append(spot + ': events must be a list')
        else:
            for event in events:
                if not isinstance(event, dict) or set(event) != {'name', 'input', 'output'}:
                    errors.append(spot + ': every event needs exactly name, input, output')
                    break
            budget = expected.get('max_tool_calls')
            if budget is not None and len(events) > budget:
                errors.append(spot + ': %d tool calls exceed max_tool_calls %d'
                              % (len(events), budget))
        want = item.get('expected')
        if not isinstance(want, dict) or set(want) != {'blockers', 'score_min', 'score_max'}:
            errors.append(spot + ': expected must be {blockers, score_min, score_max}')
            continue
        blockers = want.get('blockers')
        if not isinstance(blockers, list) or any(name not in BLOCKERS for name in blockers):
            errors.append(spot + ': blockers must be a list of known blockers')
            continue
        low, high = want.get('score_min'), want.get('score_max')
        if isinstance(low, bool) or isinstance(high, bool) \
                or not isinstance(low, (int, float)) or not isinstance(high, (int, float)) \
                or not 0 <= low <= high <= 1:
            errors.append(spot + ': scores must satisfy 0 <= score_min <= score_max <= 1')
            continue
        if kind == 'golden' and (float(low), float(high)) != (1.0, 1.0):
            errors.append(spot + ': the golden run scores exactly 1.0')
        gate = expected.get('initial_gate') or {}
        already_done = bool(gate.get('ok')) and not blockers
        if kind in ('null', 'near_miss') and high >= 1.0 and not already_done:
            errors.append(spot + ': a failing mutant needs score_max < 1 or a blocker '
                                 '(only an already gate-green task may have a do-nothing null)')
        if kind == 'alternative' and (blockers or low < 0.9):
            errors.append(spot + ': a correct alternative needs no blocker and score_min >= 0.9')
    golden = by_kind.get('golden')
    if golden is not None and golden.get('files') == 'golden':
        reply_path = scenario / 'golden' / 'reply.md'
        if reply_path.is_file() and golden.get('reply') != reply_path.read_text(encoding='utf-8'):
            errors.append('case %s: the golden mutant reply differs from golden/reply.md'
                          % case_id)
        events_path = scenario / 'golden' / 'events.jsonl'
        if events_path.is_file():
            try:
                tool_calls = [{'name': event.get('name'), 'input': event.get('input') or {},
                               'output': ''} for event in _load_events(events_path)
                              if event.get('type') == 'tool_use']
            except (OSError, json.JSONDecodeError):
                tool_calls = None
            if tool_calls is not None:
                claimed = [{'name': event.get('name'), 'input': event.get('input') or {}}
                           for event in golden.get('events') or []]
                expected_calls = [{'name': call['name'], 'input': call['input']}
                                  for call in tool_calls]
                if claimed != expected_calls:
                    errors.append('case %s: the golden mutant must replay golden/events.jsonl'
                                  % case_id)


def validate_suite(evals_dir, repo_root):
    """All named defects in a suite. Empty list = the suite is measurable."""
    evals_dir = Path(evals_dir)
    repo_root = Path(repo_root) if repo_root is not None else None
    errors = []
    rows = _parse_cases(evals_dir, errors)
    if rows is None:
        return errors
    if len(rows) != len(CASE_IDS):
        errors.append('cases.jsonl: expected %d cases, found %d' % (len(CASE_IDS), len(rows)))
    ids = [row.get('id') for row in rows]
    for case_id in sorted({item for item in ids if isinstance(item, str)}):
        if ids.count(case_id) > 1:
            errors.append('cases.jsonl: duplicate case id %r' % case_id)
    missing = [item for item in CASE_IDS if item not in ids]
    unknown = [item for item in ids if item not in CASE_IDS]
    if missing:
        errors.append('cases.jsonl: missing case ids %s' % ', '.join(map(str, missing)))
    if unknown:
        errors.append('cases.jsonl: unknown case ids %s' % ', '.join(map(str, unknown)))

    rubric_path = evals_dir / 'rubric.md'
    if not rubric_path.is_file():
        errors.append('rubric.md: missing')
    else:
        errors.extend(validate_rubric(rubric_path.read_text(encoding='utf-8')))

    results_path = evals_dir / 'RESULTS.md'
    if not results_path.is_file():
        errors.append('RESULTS.md: missing')
    else:
        text = results_path.read_text(encoding='utf-8')
        for marker in (MARKER_BEGIN, MARKER_END):
            count = text.count(marker)
            if count != 1:
                errors.append('RESULTS.md: marker %r found %d times (need exactly 1)'
                              % (marker, count))

    for row in rows:
        case_id = row.get('id')
        oracle, _ = _check_case_shape(row, errors)
        if not isinstance(case_id, str):
            continue
        scenario = _scenario_dir(evals_dir, case_id)
        if not scenario.is_dir():
            errors.append('case %s: scenario directory missing' % case_id)
            continue
        _check_prompt(scenario, row, errors)
        _check_fixture_tree(scenario, row, errors)
        _check_tokens(scenario, row, errors)
        expected_path = scenario / 'expected.json'
        if not expected_path.is_file():
            errors.append('case %s: expected.json missing' % case_id)
            continue
        try:
            expected = _read_json(expected_path)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append('case %s: expected.json not readable (%s)' % (case_id, exc))
            continue
        _check_expected_shape(expected, row, errors)
        if isinstance(expected, dict):
            _check_consistency(expected, oracle, row, errors)
            _check_state_files(scenario, expected, row, errors)
            _check_fixture_expectations(scenario, expected, row, errors)
            _check_initial_gate(evals_dir, scenario, expected, row, repo_root, errors)
            _check_golden(evals_dir, scenario, expected, row, oracle, repo_root, errors)
            _check_mutants(evals_dir, scenario, expected, row, errors)
    return errors
