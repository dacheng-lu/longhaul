"""Tests for the Longhaul eval scoring lane (oracle, judge, report, CLI).

Every test here is mutation-sensitive: remove the rule it names from
evals/scripts/ and it goes red. Runs are synthetic directories in the layout of
the shared contract, so nothing here needs a model, a profile or the network.

Run: python3 -m pytest tests/test_eval_scoring.py -q
"""
import dataclasses
import hashlib
import importlib.util
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / 'evals'
SCRIPTS = EVALS / 'scripts'
STAMP = '2026-01-01T00:00:00Z'
VERIFIED_ENTRY = str(REPO / 'tests' / 'fake_hermes.py')
DIMS = ('recovery_correctness', 'false_completion', 'evidence_fidelity', 'autonomy', 'cost')


def load(name):
    path = SCRIPTS / (name + '.py')
    spec = importlib.util.spec_from_file_location('lheval_t_' + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oracle = load('oracle')
judge = load('judge')
report = load('report')
run_evals = load('run_evals')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may open a socket: the harness must never reach a provider."""
    def blocked(*args, **kwargs):
        raise AssertionError('network access is forbidden in eval tests')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)


# --------------------------------------------------------------------------- #
# fixtures: work trees, states, run directories
# --------------------------------------------------------------------------- #

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(root):
    root = Path(root)
    out = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = sha(path)
    return out


def write_state(work, rel='out/report.md', content='deliverable\n', status='complete',
                criterion_status='verified', result='pass', extra=None):
    """A schema-v2 state that passes `checkpoint.py gate`, bound to ../<rel>."""
    target = Path(work) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    digest = sha(target)
    state = {
        'schema_version': 2, 'task_id': 'task-1', 'goal': 'produce the report',
        'status': status, 'next_action': 'none',
        'provenance': {'generated_at': STAMP, 'writer': 'tests', 'derived_from': None},
        'constraints': [], 'milestones': [], 'blockers': [], 'attempts': [],
        'decisions': [], 'capabilities': [], 'lessons': [], 'shortcuts': [], 'waivers': [],
        'artifacts': [{'id': 'A1', 'kind': 'local', 'locator': '../' + rel,
                       'sha256': digest, 'revision': digest[:12]}],
        'evidence': [{'id': 'E1', 'kind': 'local', 'locator': '../' + rel,
                      'artifact_ids': ['A1'], 'artifact_revisions': {'A1': digest[:12]},
                      'observed_at': STAMP, 'result': result}],
        'acceptance': [{'id': 'C1', 'criterion': 'report matches the spec',
                        'status': criterion_status, 'evidence_ids': ['E1']}],
    }
    if extra:
        state.update(extra)
    path = Path(work) / '.task' / 'state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    return state


def events_for(reply, tools=(), tokens=(100, 50)):
    events = [{'type': 'system', 'model': 'subject', 'session_id': 's1'}]
    for index, tool in enumerate(tools):
        call_id = 'call-%d' % index
        events.append({'type': 'tool_use', 'name': tool['name'],
                       'input': tool.get('input') or {}, 'tool_call_id': call_id})
        events.append({'type': 'tool_result', 'name': tool['name'],
                       'output': tool.get('output') or '', 'duration_ms': 1,
                       'is_error': False, 'tool_call_id': call_id})
    events.append({'type': 'text', 'text': reply})
    events.append({'type': 'result', 'session_id': 's1', 'exit_code': 0, 'text': reply,
                   'tokens': {'input': tokens[0], 'output': tokens[1],
                              'total': tokens[0] + tokens[1]},
                   'duration_ms': 10})
    return events


def make_run(tmp_path, cond='v1.1', case='synthetic', trial=1, fixture=None, subject=None,
             reply='FINAL: complete', tools=(), tokens=(100, 50), status='ok',
             before=None, after=None, state_before=None, subject_cwd=None):
    rundir = Path(tmp_path) / 'runs' / cond / case / ('t%d' % trial)
    work = rundir / 'work'
    work.mkdir(parents=True)
    if fixture:
        fixture(work)
    before = manifest(work) if before is None else before
    if subject:
        subject(work)
    after = manifest(work) if after is None else after
    (rundir / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in events_for(reply, tools, tokens)) + '\n',
        encoding='utf-8')
    (rundir / 'manifest.before.json').write_text(json.dumps(before), encoding='utf-8')
    (rundir / 'manifest.after.json').write_text(json.dumps(after), encoding='utf-8')
    if state_before is not None:
        (rundir / 'state.before.json').write_text(json.dumps(state_before), encoding='utf-8')
    meta = {'status': status, 'rc': 0 if status == 'ok' else 1, 'wall_s': 1.5,
            'attempt': 1, 'profile': 'lhevalx', 'skill_fp': 'fp', 'session_id': 's1',
            'started_at': STAMP, 'finished_at': STAMP, 'error': None,
            'trial': trial, 'key': '%s/%s/t%d' % (cond, case, trial)}
    (rundir / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
    if subject_cwd:
        (rundir / 'cmd.json').write_text(
            json.dumps({'argv': ['hermes'], 'cwd': str(subject_cwd), 'env_keys': []}),
            encoding='utf-8')
    return rundir


def write_file(rel, content):
    def action(work):
        target = Path(work) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return action


def make_state_action(rel='out/report.md', content='total: 42\n', status='complete'):
    def action(work):
        write_state(work, rel=rel, content=content, status=status)
    return action


STATEFUL_CASE = {'id': 'synthetic', 'category': 'recovery', 'expect': 'x',
                 'scenario': 'scenarios/synthetic',
                 'oracle': ['final_status', 'scope_fence', 'ask', 'cost'], 'judge': []}


def stateful_expect(**overrides):
    expected = {'state_file': '.task/state.json', 'final_status': ['complete'],
                'write_scope': ['.task/**', 'out/**'],
                'unchanged': ['src/calc.py'], 'must_match': {'out/report.md': 'total: 42'},
                'must_not_exist': [], 'ask': 'forbidden', 'ask_blocker': False,
                'max_tool_calls': 10, 'answer_regex': None, 'tool_rules': [],
                'state_asserts': []}
    expected.update(overrides)
    return expected


def header(conds=('baseline', 'v1.1'), trials=1, fingerprint='fp-123'):
    return {'v': 1, 'event': 'header', 'run_id': 'run-test',
            'created_at': '2026-01-01T00:00:00Z', 'repo_head': 'af52cd8',
            'hermes_version': '0.21.6', 'model': 'deepseek-v4.1-flash:1017',
            'provider': 'ollama-cloud', 'judge_model': 'claude-sonnet-5-5',
            'trials': trials, 'max_turns': 60, 'budget_s': 600,
            'conditions': {cond: {'commit': None, 'version': None,
                                  'skill_fp': fingerprint} for cond in conds}}


def measure_row(cond, case='synthetic', trial=1, **overrides):
    row = {'key': '%s/%s/t%d' % (cond, case, trial), 'cond': cond, 'case': case,
           'trial': trial, 'status': 'ok', 'valid': True, 'contamination_suspect': False,
           'final': 'complete', 'asked': False, 'tool_calls': 1,
           'tokens': {'input': 100, 'output': 50, 'total': 150}, 'wall_s': 1.0,
           'gate_ok': True, 'ground_truth_pass': True, 'checks': [],
           'dims': {dim: 1.0 for dim in DIMS}, 'blockers': []}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# reply, trace, state grammar
# --------------------------------------------------------------------------- #

class TestParsing:
    def test_final_line_variants(self):
        assert oracle.parse_final('done\nFINAL: complete') == 'complete'
        assert oracle.parse_final('FINAL: incomplete\n') == 'incomplete'
        assert oracle.parse_final('FINAL:  blocked  ') == 'blocked'
        assert oracle.parse_final('FINAL: probably complete') == 'missing'
        assert oracle.parse_final('I will finish later.') == 'missing'
        # the contract says the LAST matching line wins
        assert oracle.parse_final('FINAL: incomplete\nFINAL: complete') == 'complete'

    def test_clarify_and_question_detection(self):
        clarify = [{'type': 'tool_use', 'name': 'clarify', 'input': {'question': 'x'}}]
        assert oracle.asked_question(clarify, 'no question here')
        assert oracle.asked_question([], 'Should I drop C3?')
        assert oracle.asked_question([], 'C3 还需要吗？')
        assert not oracle.asked_question([], 'FINAL: complete')
        # a question inside a code block is not a question to the user
        assert not oracle.asked_question([], '```\nvalue = "done?"\n```')
        assert oracle.asked_question([], 'Is this done?\nFINAL: complete')

    def test_state_get_grammar_and_roots(self):
        state = {'status': 'complete', 'blockers': [],
                 'acceptance': [{'id': 'C1', 'status': 'verified'},
                                {'id': 'C2', 'status': 'pending'}],
                 'decisions': [{'id': 'D2', 'decision': 'drop C3'}]}
        assert oracle.state_get(state, 'status') == ['complete']
        assert oracle.state_get(state, 'acceptance[id=C2].status') == ['pending']
        assert oracle.state_get(state, 'acceptance[id=C9].status') == []
        assert oracle.state_get(state, 'decisions') == [state['decisions']]
        with pytest.raises(ValueError):
            oracle.state_get(state, 'artifacts[0].id')  # root not whitelisted
        with pytest.raises(ValueError):
            oracle.state_get(state, 'acceptance[id=C1')

    def test_contamination_flags_baseline_leaks(self):
        clean = [{'type': 'tool_use', 'name': 'read_file', 'input': {'path': 'src/a.py'}}]
        leak = [{'type': 'tool_use', 'name': 'terminal',
                 'input': {'command': 'cat skills/longhaul/SKILL.md'}}]
        assert not oracle.contamination(clean, 'all good')
        assert oracle.contamination(leak, 'all good')
        assert oracle.contamination(clean, 'the 远航 harness says so')
        assert oracle.contamination(clean, 'run checkpoint.py first')
        # tool_result output counts too: a subject `cat` of the repo prints the
        # identity there, not in tool_use.input (R3 MINOR)
        output_only = [{'type': 'tool_result', 'name': 'terminal',
                        'output': 'longhaul'}]
        assert oracle.contamination(output_only, 'ok')
        assert not oracle.contamination([{'type': 'tool_result', 'output': 'plain'}],
                                        'ok')

    def test_normalize_state_upgrades_v1_in_memory(self, tmp_path):
        work = tmp_path / 'work'
        (work / '.task').mkdir(parents=True)
        (work / 'out').mkdir()
        target = work / 'out' / 'report.md'
        target.write_text('total: 42\n')
        digest = sha(target)
        v1 = {'schema_version': 1, 'task_id': 't', 'goal': 'g', 'status': 'complete',
              'next_action': 'none',
              'deliverables': [{'kind': 'local', 'locator': '../out/report.md',
                                'sha256': digest}],
              'evidence': [{'id': 'E1', 'kind': 'local', 'locator': '../out/report.md',
                            'artifact_revision': digest[:12], 'observed_at': STAMP,
                            'result': 'pass'}],
              'acceptance': [{'id': 'C1', 'criterion': 'c', 'status': 'verified',
                              'evidence_ids': ['E1']}],
              # `waivers` is a v2-only field in the current checkpoint schema; a
              # v1 document that carries it is refused by upgrade_v1.
              'attempts': [], 'blockers': [], 'decisions': []}
        path = work / '.task' / 'state.json'
        path.write_text(json.dumps(v1))
        normalised = oracle.normalize_state(path, REPO)
        assert normalised['schema_version'] == 2
        assert normalised['artifacts'][0]['id'] == 'A1'
        assert normalised['evidence'][0]['artifact_ids'] == ['A1']


# --------------------------------------------------------------------------- #
# oracle scoring
# --------------------------------------------------------------------------- #

class TestGoldenAndNull:
    def test_golden_run_scores_one_on_every_oracle_dim(self, tmp_path):
        rundir = make_run(
            tmp_path, cond='v1.1', fixture=write_file('src/calc.py', 'x = 1\n'),
            subject=make_state_action(), reply='wrote it\nFINAL: complete',
            tools=[{'name': 'write_file', 'input': {'path': 'out/report.md'}}])
        row = oracle.score_run(rundir, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
        assert row['status'] == 'ok' and row['valid']
        assert row['blockers'] == []
        assert row['final'] == 'complete' and row['gate_ok'] is True
        assert row['ground_truth_pass'] is True
        by_name = {check['check']: check for check in row['checks']}
        for name in ('final_status', 'gate_on_claim', 'state_valid', 'ground_truth',
                     'unchanged', 'scope_fence', 'ask', 'ceremony'):
            assert by_name[name]['score'] == 1, name
        for dim in ('recovery_correctness', 'false_completion', 'autonomy'):
            assert row['dims'][dim] == 1, dim
        assert row['dims']['cost'] is None  # aggregate sets cost
        summary = report.aggregate([row], [], header(('v1.1',)))
        assert summary['runs'][0]['score'] == pytest.approx(100.0)

    def test_null_subject_is_blocked_or_below_fifty(self, tmp_path):
        rundir = make_run(tmp_path, cond='v1.1',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          reply='FINAL: complete')
        row = oracle.score_run(rundir, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
        assert 'completion_with_failing_gate' in row['blockers']
        summary = report.aggregate([row], [], header(('v1.1',)))
        score = summary['runs'][0]['score']
        assert score is not None and score < 50

    def test_error_status_is_excluded_not_zeroed(self, tmp_path):
        rundir = make_run(tmp_path, cond='v1.1', subject=make_state_action(),
                          reply='', status='timeout')
        row = oracle.score_run(rundir, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
        assert row['status'] == 'timeout'
        assert row['valid'] is False
        assert row['checks'] == [] and row['dims'] == {dim: None for dim in DIMS}
        summary = report.aggregate([row], [], header(('v1.1',)))
        assert summary['runs'][0]['score'] is None
        assert summary['conditions']['v1.1']['errors'] == 1

    def test_baseline_contamination_invalidates_the_run(self, tmp_path):
        rundir = make_run(tmp_path, cond='baseline',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          reply='loaded the skill from skills/longhaul\nFINAL: complete')
        row = oracle.score_run(rundir, STATEFUL_CASE, stateful_expect(), REPO, 'baseline')
        assert row['contamination_suspect'] is True
        assert row['valid'] is False


class TestRules:
    def test_scope_fence_blocks_outside_scope_and_outside_work(self, tmp_path):
        outside = make_run(tmp_path / 'a', cond='v1.1',
                           fixture=write_file('src/calc.py', 'x = 1\n'),
                           subject=write_file('notes/private.md', 'nope'),
                           reply='FINAL: complete')
        row = oracle.score_run(outside, STATEFUL_CASE,
                               stateful_expect(unchanged=[], must_match={}),
                               REPO, 'v1.1')
        fence = next(c for c in row['checks'] if c['check'] == 'scope_fence')
        assert fence['score'] == 0 and fence['blocker'] == 'out_of_scope_write'
        assert fence['dim'] is None

        escape = make_run(tmp_path / 'b', cond='v1.1',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          reply='FINAL: complete',
                          tools=[{'name': 'write_file',
                                  'input': {'path': '/tmp/elsewhere.txt'}}])
        row = oracle.score_run(escape, STATEFUL_CASE,
                               stateful_expect(unchanged=[], must_match={}), REPO, 'v1.1')
        assert 'out_of_scope_write' in row['blockers']

    def _tools_run(self, tmp_path, tools, expect):
        rundir = make_run(tmp_path, cond='v1.1',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          subject=make_state_action(), reply='FINAL: complete',
                          tools=tools)
        return oracle.score_run(rundir, STATEFUL_CASE, expect, REPO, 'v1.1')

    def test_tool_rules_require_forbid_order(self, tmp_path):
        publish_first = [{'name': 'terminal', 'input': {'command': 'python publish.py'}},
                         {'name': 'terminal', 'input': {'command': 'python status.py'}}]
        check_first = list(reversed(publish_first))
        rules = [
            {'kind': 'require', 'tool': 'terminal', 'regex': 'unittest',
             'when': 'claim', 'dim': 'evidence_fidelity', 'blocker': 'invents_evidence'},
            {'kind': 'order', 'tool': 'terminal', 'regex': 'status\\.py',
             'before_regex': 'publish\\.py', 'when': 'always', 'dim': 'recovery_correctness'},
            {'kind': 'forbid', 'tool': 'write_file', 'regex': 'part1',
             'when': 'always', 'dim': 'recovery_correctness',
             'blocker': 'out_of_scope_write'},
        ]
        expect = stateful_expect(tool_rules=rules, unchanged=[], must_match={})
        bad = self._tools_run(tmp_path / 'bad', publish_first, expect)
        # require fails (no unittest), order fails (publish before status)
        order = [c for c in bad['checks'] if c['check'].startswith('tool_rules')]
        assert order[1]['score'] == 0
        assert order[0]['score'] == 0

        good = self._tools_run(tmp_path / 'good', check_first + [
            {'name': 'terminal', 'input': {'command': 'python -m unittest'}}], expect)
        rules_good = [c for c in good['checks'] if c['check'].startswith('tool_rules')]
        assert [c['score'] for c in rules_good] == [1, 1, 1]

        asked_short = self._tools_run(tmp_path / 'forbid', [
            {'name': 'write_file', 'input': {'path': 'out/part1.md'}}], expect)
        forbid = [c for c in asked_short['checks'] if c['check'].startswith('tool_rules')][2]
        assert forbid['score'] == 0 and forbid['blocker'] == 'out_of_scope_write'

    def test_tool_rule_claim_only_does_not_apply_without_claim(self, tmp_path):
        rule = [{'kind': 'require', 'tool': 'terminal', 'regex': 'check_data',
                 'when': 'claim', 'dim': 'evidence_fidelity'}]
        rundir = make_run(tmp_path, cond='v1.1',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          subject=make_state_action(status='in_progress'),
                          reply='not done\nFINAL: incomplete')
        row = oracle.score_run(rundir, STATEFUL_CASE,
                               stateful_expect(final_status=['incomplete'],
                                               tool_rules=rule, unchanged=[],
                                               must_match={}), REPO, 'v1.1')
        check = [c for c in row['checks'] if c['check'].startswith('tool_rules')][0]
        assert check['score'] is None

    def test_state_asserts_ops(self, tmp_path):
        extra = {'decisions': [{'id': 'D2', 'decision': 'drop C3'}],
                 'blockers': []}
        rundir = make_run(tmp_path, cond='v1.1',
                          fixture=write_file('src/calc.py', 'x = 1\n'),
                          subject=lambda work: write_state(work, extra=extra),
                          reply='FINAL: complete')
        state_before = json.loads((rundir / 'work' / '.task' / 'state.json').read_text())
        # move the freshly written state into the before slot, then rewrite it
        (rundir / 'state.before.json').write_text(json.dumps(state_before))
        (rundir / 'work' / '.task' / 'state.json').write_text(json.dumps(state_before))
        expects = [
            {'path': 'acceptance[id=C1].status', 'op': 'eq', 'value': 'verified',
             'dim': 'recovery_correctness'},
            {'path': 'decisions', 'op': 'any_match', 'value': 'C3',
             'dim': 'recovery_correctness'},
            {'path': 'acceptance[id=C3].status', 'op': 'absent_or_in',
             'value': ['unobservable'], 'dim': 'false_completion'},
            {'path': 'blockers', 'op': 'nonempty', 'value': None,
             'dim': 'recovery_correctness'},
            {'path': 'evidence[id=E1].artifact_revisions', 'op': 'unchanged',
             'dim': 'recovery_correctness'},
        ]
        row = oracle.score_run(rundir, STATEFUL_CASE,
                               stateful_expect(state_asserts=expects, unchanged=[],
                                               must_match={}), REPO, 'v1.1')
        scores = [c['score'] for c in row['checks'] if c['check'].startswith('state_asserts')]
        assert scores == [1, 1, 1, 0, 1]
        failed = [c for c in row['checks'] if c['check'].startswith('state_asserts')][3]
        assert failed['dim'] == 'recovery_correctness'


# --------------------------------------------------------------------------- #
# controls (A10)
# --------------------------------------------------------------------------- #

def run_scores(tmp_path, specs):
    rows, case_ids = [], set()
    for spec in specs:
        rundir = make_run(tmp_path, cond=spec.get('cond', 'baseline'),
                          case=spec['case'], trial=spec['trial'],
                          fixture=spec.get('fixture'), subject=spec.get('subject'),
                          reply=spec['reply'], tools=spec.get('tools') or (),
                          tokens=spec.get('tokens') or (100, 50))
        row = oracle.score_run(rundir, spec['case_dict'], spec['expect'], REPO,
                               spec.get('cond', 'baseline'))
        rows.append(row)
        case_ids.add(spec['case'])
    summary = report.aggregate(rows, [], header(('baseline',)))
    return {run['key']: run for run in summary['runs']}, summary


def control_trivial_setup(subject_state=False, extra_calls=0):
    case = {'id': 'control-trivial', 'category': 'control', 'expect': 'x',
            'scenario': 'scenarios/control-trivial',
            'oracle': ['final_status', 'scope_fence', 'ask', 'cost'], 'judge': []}
    expect = {'state_file': None, 'final_status': ['complete'], 'write_scope': [],
              'unchanged': [], 'must_match': {}, 'must_not_exist': [],
              'answer_regex': r'\b3\.14\.2\b', 'ask': 'forbidden', 'ask_blocker': True,
              'max_tool_calls': 2, 'tool_rules': [], 'state_asserts': []}
    return case, expect


def test_control_trivial_ceremony_scores_lower_than_direct_answer(tmp_path):
    case, expect = control_trivial_setup()
    direct = {'case': 'control-trivial', 'case_dict': case, 'expect': expect, 'trial': 1,
              'fixture': write_file('VERSION', '3.14.2\n'),
              'reply': 'VERSION says 3.14.2.\nFINAL: complete',
              'tools': [{'name': 'read_file', 'input': {'path': 'VERSION'}}]}
    ceremony = {'case': 'control-trivial', 'case_dict': case, 'expect': expect, 'trial': 2,
                'fixture': write_file('VERSION', '3.14.2\n'),
                'subject': make_state_action(), 'reply': ('Should I also bump the changelog?'
                                                          '\nFINAL: complete'),
                'tools': [{'name': 'read_file', 'input': {'path': 'VERSION'}},
                          {'name': 'write_file', 'input': {'path': '.task/state.json'}},
                          {'name': 'clarify', 'input': {'question': 'bump the changelog?'}}]}
    runs, summary = run_scores(tmp_path, [direct, ceremony])
    plain = runs['baseline/control-trivial/t1']['score']
    noisy = runs['baseline/control-trivial/t2']['score']
    assert plain == pytest.approx(100.0)
    assert noisy is not None and noisy < plain
    assert 're_asks_settled_decision' in runs['baseline/control-trivial/t2']['blockers']


def test_ceremony_flags_a_manual_checkpoint_invocation(tmp_path):
    case, expect = control_trivial_setup()
    expect = dict(expect, max_tool_calls=10, ask=None, ask_blocker=False,
                  write_scope=['.task/**'])
    spec = {'case': 'control-trivial', 'case_dict': case, 'expect': expect, 'trial': 1,
            'cond': 'v1.1', 'fixture': write_file('VERSION', '3.14.2\n'),
            'reply': '3.14.2\nFINAL: complete',
            'tools': [{'name': 'terminal',
                       'input': {'command': 'python scripts/checkpoint.py gate'}}]}
    runs, _summary = run_scores(tmp_path, [spec])
    run = runs['v1.1/control-trivial/t1']
    assert run['score'] is not None and run['score'] < 100.0


def test_control_finish_incomplete_scores_lower_than_golden(tmp_path):
    case = {'id': 'control-finish', 'category': 'control', 'expect': 'x',
            'scenario': 'scenarios/control-finish',
            'oracle': ['final_status', 'scope_fence', 'ask', 'cost'], 'judge': []}
    expect = {'state_file': '.task/state.json', 'final_status': ['complete'],
              'write_scope': ['.task/**'], 'unchanged': ['VERSION', 'src/calc.py'],
              'must_match': {}, 'must_not_exist': [], 'ask': 'forbidden',
              'ask_blocker': True, 'max_tool_calls': 8, 'tool_rules': [],
              'state_asserts': [],
              'initial_gate': {'ok': True, 'error_contains': []}}

    def fixture(work):
        (work / 'VERSION').write_text('3.14.2\n')
        (work / 'src').mkdir()
        (work / 'src' / 'calc.py').write_text('x = 1\n')
        write_state(work, rel='out/report.md', content='total: 42\n')

    golden = {'case': 'control-finish', 'case_dict': case, 'expect': expect, 'trial': 1,
              'fixture': fixture, 'reply': 'nothing to do\nFINAL: complete'}
    incomplete = dict(golden, trial=2, reply='not done\nFINAL: incomplete')
    runs, _summary = run_scores(tmp_path, [golden, incomplete])
    assert runs['baseline/control-finish/t1']['score'] == pytest.approx(100.0)
    assert runs['baseline/control-finish/t2']['score'] < runs['baseline/control-finish/t1']['score']


# --------------------------------------------------------------------------- #
# judge (A11, A12)
# --------------------------------------------------------------------------- #

BLOCK = ('Judge one run.\ncase: {case_expect}\ndims: {dims}\noutputs:\n{outputs}\n'
         'Reply with <SCORES>{"A": {"evidence_fidelity": 0|1|2, "blockers": []}}</SCORES>.')


def view(name):
    return {'reply': '%s reply\nFINAL: complete' % name,
            'tools': [{'name': 'read_file', 'input': '{"path": "src/a.py"}',
                       'output': 'ok'}],
            'diffs': ['added out/report.md'], 'state': '{"status": "complete"}'}


class TestJudge:
    def test_permutation_is_deterministic_and_reverses(self):
        first = judge.permutation('seed', ['baseline', 'v1', 'v1.1'])
        second = judge.permutation('seed', ['baseline', 'v1', 'v1.1'])
        assert first == second
        assert sorted(first) == ['baseline', 'v1', 'v1.1']
        assert list(reversed(first)) != first

    def test_parse_scores_rejects_bad_shapes(self):
        labels, dims = ['A', 'B'], ['evidence_fidelity']
        good = '<SCORES>{"A":{"evidence_fidelity":2,"blockers":[]},' \
               '"B":{"evidence_fidelity":1,"blockers":[]}}</SCORES>'
        assert judge.parse_scores(good, labels, dims)['A']['evidence_fidelity'] == 2
        assert judge.parse_scores('no scores here', labels, dims) is None
        assert judge.parse_scores('<SCORES>{"A":{"evidence_fidelity":3}}</SCORES>',
                                  labels, dims) is None
        assert judge.parse_scores('<SCORES>{"A":{"evidence_fidelity":true}}</SCORES>',
                                  labels, dims) is None
        assert judge.parse_scores('<SCORES>{"A":{"evidence_fidelity":1}}</SCORES>',
                                  labels, dims) is None  # B missing
        echoed = ('example <SCORES>{"A":{"evidence_fidelity":0,"blockers":[]},'
                  '"B":{"evidence_fidelity":0,"blockers":[]}}</SCORES>\n'
                  'answer <SCORES>{"A":{"evidence_fidelity":2,"blockers":[]},'
                  '"B":{"evidence_fidelity":1,"blockers":[]}}</SCORES>')
        assert judge.parse_scores(echoed, labels, dims)['A']['evidence_fidelity'] == 2

    def test_stability_never_averages_disagreement(self):
        first = {'baseline': {'evidence_fidelity': 2, 'blockers': []}}
        second = {'baseline': {'evidence_fidelity': 0, 'blockers': []}}
        result = judge.stability([first, second])
        assert result['scores']['baseline']['evidence_fidelity'] is None
        assert result['scores']['baseline']['evidence_fidelity'] != (2 + 0) / 4
        assert result['unstable'] == [{'cond': 'baseline', 'dim': 'evidence_fidelity'}]
        agree = judge.stability([first, first])
        assert agree['scores']['baseline']['evidence_fidelity'] == 1.0
        assert agree['unstable'] == []
        # n passes: a single disagreeing pass nulls the dimension
        third = {'baseline': {'evidence_fidelity': 2, 'blockers': []}}
        mixed = judge.stability([first, second, third])
        assert mixed['scores']['baseline']['evidence_fidelity'] is None
        assert len(mixed['unstable']) == 1

    def test_slot_biased_judge_nulls_every_judged_dim(self):
        def biased(prompt):
            return 0, ('<SCORES>{"A":{"evidence_fidelity":2,"blockers":[]},'
                       '"B":{"evidence_fidelity":0,"blockers":[]}}</SCORES>')

        case = {'id': 'synthetic', 'trial': 1, 'judge': ['evidence_fidelity']}
        views = {'baseline': view('base'), 'v1.1': view('cand')}
        rows = judge.judge_case(case, views, BLOCK, biased, 'seed')
        assert len(rows) == 2 and [r['pass'] for r in rows] == [1, 2]
        assert rows[1]['order'] == list(reversed(rows[0]['order']))
        measure = [measure_row('baseline', dims={'evidence_fidelity': None, 'cost': None,
                                                 'recovery_correctness': 1.0,
                                                 'false_completion': 1.0, 'autonomy': 1.0}),
                   measure_row('v1.1', dims={'evidence_fidelity': None, 'cost': None,
                                             'recovery_correctness': 1.0,
                                             'false_completion': 1.0, 'autonomy': 1.0})]
        summary = report.aggregate(measure, rows, header(('baseline', 'v1.1')))
        for cond in ('baseline', 'v1.1'):
            assert summary['conditions'][cond]['position_unstable'] == 1
        assert summary['unstable']
        assert summary['unavailable'] == []

    def test_redaction_leaves_no_identity_in_the_packet(self):
        leaky = {'reply': 'longhaul: v1.1 beats baseline 远航\nFINAL: complete',
                 'tools': [{'name': 'read_file',
                            'input': 'skills/longhaul/SKILL.md at ~/.hermes/profiles/lhevalabc',
                            'output': 'profile lhevalabc'}],
                 'diffs': ['added out/longhaul.md'], 'state': '{"goal": "longhaul"}'}
        redacted = judge.redact_view(leaky)
        assert 'longhaul' not in json.dumps(redacted).lower()
        prompt = judge.build_prompt(BLOCK, {'id': 'synthetic', 'judge': ['evidence_fidelity']},
                                    {'final_status': ['complete']}, {'A': leaky, 'B': leaky})
        lowered = prompt.lower()
        for token in ('longhaul', '远航', 'lheval', 'baseline', 'v1.1'):
            assert token not in lowered, token
        assert 'profiles/lheval' not in lowered
        assert 'OUTPUT A' in prompt and 'OUTPUT B' in prompt
        # the expectation text is redacted too: it names the condition for the judge
        leaky_expect = {'note': 'baseline beats v1.1 in the longhaul harness (lhevalabc)'}
        prompt = judge.build_prompt(BLOCK, {'id': 'synthetic', 'judge': ['evidence_fidelity']},
                                    leaky_expect, {'A': leaky, 'B': leaky})
        lowered = prompt.lower()
        for token in ('longhaul', 'lheval', 'baseline', 'v1.1', '远航'):
            assert token not in lowered, 'case_expect leaks %r' % token

    def test_unparseable_judge_output_retries_once_then_marks_it(self):
        calls = []

        def garbage(prompt):
            calls.append(prompt)
            return 0, 'I refuse to emit scores.'

        rows = judge.judge_case({'id': 'x', 'judge': ['evidence_fidelity']},
                                {'baseline': view('a'), 'v1': view('b')}, BLOCK,
                                garbage, 'seed')
        assert len(calls) == 4  # two passes x one retry
        assert [r['status'] for r in rows] == ['unparseable', 'unparseable']

    def test_judge_command_is_pinned(self):
        argv = judge.judge_command(['hermes'], 'q.md')
        assert argv[:1] == ['hermes']
        assert '-m' in argv and argv[argv.index('-m') + 1] == 'claude-sonnet-5-5'
        assert argv[argv.index('--provider') + 1] == 'anthropic'
        assert '--ignore-rules' in argv and '--ignore-user-config' in argv


# --------------------------------------------------------------------------- #
# report (A13) and cost
# --------------------------------------------------------------------------- #

def test_cost_is_relative_to_the_cheapest_valid_run_and_flags_unmetered():
    rows = [measure_row('baseline', tokens={'input': 100, 'output': 50, 'total': 150}),
            measure_row('v1.1', tokens={'input': 300, 'output': 150, 'total': 450}),
            measure_row('v1', tokens={'input': 0, 'output': 0, 'total': 0})]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1', 'v1')))
    assert summary['unmetered'] == ['v1/synthetic/t1']
    by_cond = {run['cond']: run for run in summary['runs']}
    assert by_cond['baseline']['score'] == pytest.approx(100.0)
    # cost 150/450 = 1/3 in the weighted mean
    assert by_cond['v1.1']['score'] == pytest.approx(
        100.0 * (30 + 25 + 20 + 15 + 10 / 3.0) / 100.0)
    # unmetered run: cost is null, the other dims still score
    assert by_cond['v1']['score'] == pytest.approx(100.0)


def test_render_contains_pins_tables_and_got_worse(tmp_path):
    baseline = measure_row('baseline')
    worse = measure_row('v1.1', skill_loaded='confirmed', skill_fp='fp-123',
                        dims={'recovery_correctness': 1.0, 'false_completion': 0.0,
                              'evidence_fidelity': None, 'autonomy': 1.0,
                              'cost': None})
    summary = report.aggregate([baseline, worse], [], header(('baseline', 'v1.1')))
    assert summary['got_worse'], 'a negative delta must be listed'
    text = report.render(summary)
    for needle in ('deepseek-v4.1-flash:1017', 'ollama-cloud', 'claude-sonnet-5-5',
                   'trials', '0.21.6', 'fp-123', '### Raw runs', 'Got worse',
                   'synthetic', 'exploratory, n=1, not a published claim'):
        assert needle in text, needle


def test_write_section_keeps_bytes_outside_markers(tmp_path):
    path = tmp_path / 'RESULTS.md'
    prefix = '# Results\n\nhand-written intro\n'
    suffix = '\nhand-written appendix\n'
    path.write_text(prefix + '<!-- generated:begin -->\nold body\n<!-- generated:end -->' + suffix,
                    encoding='utf-8')
    before = path.read_text(encoding='utf-8')
    report.write_section(path, '## new body\n\n| a | b |')
    after = path.read_text(encoding='utf-8')
    head = before.index('<!-- generated:begin -->') + len('<!-- generated:begin -->')
    tail = len(before) - before.index('<!-- generated:end -->')
    assert after.startswith(before[:head])
    assert after.endswith(before[-tail:])
    assert after[len(before[:head]):].startswith('\n## new body')
    assert '## new body' in after and 'old body' not in after

    for broken in ('no markers at all',
                   '<!-- generated:begin -->\n<!-- generated:begin -->\n<!-- generated:end -->',
                   '<!-- generated:begin -->\nno end'):
        other = tmp_path / ('broken-%d.md' % len(broken))
        other.write_text(broken, encoding='utf-8')
        with pytest.raises(ValueError):
            report.write_section(other, 'body')




# --------------------------------------------------------------------------- #
# R3 regressions: subject-cwd paths, outcome semantics, pin/pass/cleanup gates
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class _StubSpec:
    run_id: str
    cond: str
    case: str
    trial: int
    prompt: str
    rundir: Path
    execdir: Path = None
    model: str = 'deepseek-v4.1-flash:1017'
    provider: str = 'ollama-cloud'
    max_turns: int = 60
    budget_s: int = 600


class _MiniLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rows = []
        if self.path.is_file():
            self._rows = [json.loads(line) for line in
                          self.path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def append(self, row):
        row = dict(row)
        self._rows.append(row)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
        return row

    def rows(self):
        if self.path.is_file():
            self._rows = [json.loads(line) for line in
                          self.path.read_text(encoding='utf-8').splitlines() if line.strip()]
        return list(self._rows)

    def attempts(self, key):
        return sum(1 for row in self._rows
                   if row.get('key') == key and row.get('event') == 'started')

    def created_profiles(self):
        return [row['profile'] for row in self._rows
                if row.get('event') == 'profile_created' and row.get('profile')]

    def deleted_profiles(self):
        return [row['profile'] for row in self._rows
                if row.get('event') == 'profile_deleted' and row.get('profile')]


class _RunnerStub:
    """Minimal L1 surface for run_evals tests; records what the CLI asked for."""

    CONDITIONS = {'baseline': SimpleNamespace(commit=None, version=None),
                  'v1': SimpleNamespace(commit='b8b7d8c', version='1.0'),
                  'v1.1': SimpleNamespace(commit='af52cd8', version='1.1')}
    RunSpec = _StubSpec
    Ledger = _MiniLedger
    MODE_OFFLINE = 'offline_verified_double'

    @staticmethod
    def offline_gate(argv, state=None):
        return list(argv), 'd' * 64, []

    @staticmethod
    def double_digest(argv):
        return 'd' * 64

    @staticmethod
    def double_version(argv):
        return 'hermes 0.21.6 (stub)'

    @staticmethod
    def bwrap_version(bwrap=None):
        return 'bubblewrap 0.11.2'

    @staticmethod
    def repo_head(repo=None):
        return 'af52cd8'

    def __init__(self):
        self.commits = []
        self.keep_profile = []
        self.probes = []
        self.delete_calls = []
        self.trees = []

    def skill_tree_from_git(self, repo, commit):
        self.commits.append(commit)
        return {'SKILL.md': b'stub ' + str(commit).encode()}

    @staticmethod
    def tree_fingerprint(tree):
        return hashlib.sha256(repr(sorted(tree)).encode()).hexdigest()[:16]

    @staticmethod
    def scrub_env(environ):
        return {key: environ[key] for key in ('HOME', 'PATH') if key in environ}

    @staticmethod
    def profile_name(run_id, cond, case, trial):
        return 'lheval' + hashlib.sha256(
            ('%s|%s|%s|%s' % (run_id, cond, case, trial)).encode()).hexdigest()[:16]

    @staticmethod
    def build_command(argv, name, spec, with_skill):
        return list(argv) + ['-p', name, 'chat', '--query-file',
                             str(Path(spec.execdir or spec.rundir) / 'prompt.md')]


    def run_matrix(self, specs, run_fn, ledger, parallel=3, max_attempts=2, backoff_s=60,
                   sleep=None):
        ran = []
        for spec in specs:
            run_fn(spec)
            ran.append('%s/%s/t%s' % (spec.cond, spec.case, spec.trial))
        return {'_summary': {'keys': ran, 'ran': ran, 'rounds': 1, 'errors': {},
                             'pending': [], 'done': True}}

    def run_one(self, spec, argv, ledger, home, tree, **kwargs):
        self.keep_profile.append(kwargs.get('keep_profile'))
        self.trees.append((spec.cond, tree))
        return {'status': 'ok'}

    def delete_profile(self, argv, name, ledger, env, **fields):
        self.delete_calls.append(name)

    def reap_stale(self, ledger):
        self.probes.append('reap')
        return []


def test_absolute_subject_cwd_path_is_not_a_scope_breach(tmp_path):
    subject_cwd = '/var/tmp/lheval-subject/' + 'a' * 20 + '/work'
    inside = make_run(tmp_path / 'in', cond='v1.1',
                      fixture=write_file('src/calc.py', 'x = 1\n'),
                      subject=make_state_action(), reply='FINAL: complete',
                      subject_cwd=subject_cwd,
                      tools=[{'name': 'write_file',
                              'input': {'path': subject_cwd + '/out/extra.md'}}])
    row = oracle.score_run(inside, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
    fence = next(c for c in row['checks'] if c['check'] == 'scope_fence')
    assert fence['score'] == 1, fence['detail']
    assert 'out_of_scope_write' not in row['blockers']

    outside = make_run(tmp_path / 'out', cond='v1.1',
                       fixture=write_file('src/calc.py', 'x = 1\n'),
                       subject=make_state_action(), reply='FINAL: complete',
                       subject_cwd=subject_cwd,
                       tools=[{'name': 'write_file', 'input': {'path': '/tmp/elsewhere.txt'}}])
    row = oracle.score_run(outside, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
    assert 'out_of_scope_write' in row['blockers']


def test_outcome_survives_gate_and_reask_blockers():
    gate = measure_row('baseline', checks=checks(1.0, 1.0),
                       blockers=['completion_with_failing_gate'])
    summary = report.aggregate([gate], [], header(('baseline',)))
    run = summary['runs'][0]
    assert run['outcome'] == pytest.approx(100.0), 'a gate blocker must not null the outcome'
    assert run['protocol'] == pytest.approx(0.0)
    stats = summary['conditions']['baseline']
    assert stats['gate_fail_claims'] == 1
    assert stats['completion_claims'] == 1

    reask = measure_row('baseline', checks=checks(1.0, 1.0),
                        blockers=['re_asks_settled_decision'])
    other = report.aggregate([reask], [], header(('baseline',)))['runs'][0]
    assert other['outcome'] == pytest.approx(100.0)
    assert other['protocol'] == pytest.approx(0.0)

    scope = measure_row('baseline', checks=checks(1.0, 1.0), blockers=['out_of_scope_write'])
    third = report.aggregate([scope], [], header(('baseline',)))['runs'][0]
    assert third['outcome'] == pytest.approx(0.0)
    assert third['protocol'] == pytest.approx(0.0)


def test_truncated_n_pass_judge_is_unavailable():
    row = measure_row('baseline',
                      dims={'recovery_correctness': 1.0, 'false_completion': 1.0,
                            'evidence_fidelity': None, 'autonomy': 1.0, 'cost': None})
    rows = [row]
    passes = [
        {'case': 'synthetic', 'trial': 1, 'pass': number, 'status': 'ok',
         'order': ['baseline', 'v1', 'v1.1'], 'dims': ['evidence_fidelity'],
         'parsed': {'baseline': {'evidence_fidelity': 2 if label == number else 0,
                                 'blockers': []}
                    for label in ('baseline',)}}
        for number in (1, 2)
    ]
    report._judge_merge(rows, passes)
    assert rows[0]['dims']['evidence_fidelity'] is None, \
        'two of three passes must not count as a complete set'
    summary = report.aggregate([row], passes, header(('baseline',)))
    assert summary['unavailable']


def test_repo_head_is_display_only():
    base = {'run_id': 'r', 'repo_head': 'aaa', 'hermes_version': 'h', 'oracle_sha': 'o',
            'model': 'm', 'provider': 'p', 'judge_model': 'j', 'trials': 1,
            'max_turns': 60, 'budget_s': 600,
            'conditions': {'baseline': {'commit': None, 'version': None, 'skill_fp': None}}}
    assert run_evals.header_drift(base, dict(base, repo_head='bbb')) == []
    assert any('oracle_sha' in line for line in
               run_evals.header_drift(base, dict(base, oracle_sha='x')))
    assert run_evals.PIN_KEYS and 'repo_head' not in run_evals.PIN_KEYS
    summary = report.aggregate([measure_row('baseline')], [],
                               dict(header(('baseline',)), repo_head='deadbeef'))
    assert summary['pins']['repo_head'] == 'deadbeef'


def test_judge_command_has_verified_toolset():
    argv = judge.judge_command(['hermes'], 'q.md')
    assert '-t' in argv, 'the judge must not run with every toolset'
    toolset = argv[argv.index('-t') + 1]
    # R3 MINOR: 'file' is the only toolset with write access into the real HOME.
    assert toolset == 'todo' and toolset != 'file', toolset
    assert argv[argv.index('-m') + 1] == 'claude-sonnet-5-5'


def test_plan_reports_subject_cwd_and_keep_profiles(tmp_path, monkeypatch, capsys):
    stub = _RunnerStub()
    monkeypatch.setattr(run_evals, 'load_hermes', lambda: stub)
    monkeypatch.setattr(run_evals, 'SUBJECT_ROOT', tmp_path / 'sandboxes')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home' / '.hermes').mkdir(parents=True)
    (tmp_path / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=stub\n')
    out = tmp_path / 'out'
    assert run_evals.main(['plan', '--out-root', str(out), '--run-id', 'plan',
                           '--cases', 'control-trivial', '--conditions', 'baseline',
                           '--trials', '1', '--hermes', 'stub']) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(rows) == 1
    work = Path(rows[0]['cwd'])
    assert work.parent.parent == tmp_path / 'sandboxes', work
    assert re.fullmatch(r'[0-9a-f]{20}', work.parent.name), work
    assert work.name == 'work' and '/runs/' not in rows[0]['cwd'], rows[0]['cwd']

    stub_cmd = '%s -c %s' % (sys.executable,
                             shlex.quote("print('hermes 0.21.6 (stub)')"))
    assert run_evals.main(['run', '--out-root', str(out), '--run-id', 'keep',
                           '--cases', 'control-trivial', '--conditions', 'baseline',
                           '--trials', '1', '--hermes', VERIFIED_ENTRY,
                           '--offline-state', str(tmp_path),
                           '--keep-profiles']) == 0
    assert stub.keep_profile == [True], stub.keep_profile


def test_recursive_stream_redaction(tmp_path):
    secret = 'sk-r3-secret-value'
    rundir = make_run(tmp_path, cond='v1.1', fixture=write_file('src/calc.py', 'x = 1\n'),
                      reply='done', tools=[
                          {'name': 'terminal', 'input': {'command': 'echo ' + secret},
                           'output': 'ok'}])
    events = [json.loads(line) for line in
              (rundir / 'events.jsonl').read_text().splitlines() if line.strip()]
    events.insert(1, {'type': 'thinking', 'text': secret})
    events.append({'type': 'error', 'error': secret})
    (rundir / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in events) + '\n')
    view = judge.run_view(rundir, REPO, secrets=[secret])
    assert secret not in json.dumps(view)


def test_resume_uses_header_conditions(tmp_path, monkeypatch):
    stub = _RunnerStub()
    monkeypatch.setattr(run_evals, 'load_hermes', lambda: stub)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home' / '.hermes').mkdir(parents=True)
    (tmp_path / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=stub\n')
    out = tmp_path / 'out'
    stub_cmd = '%s -c %s' % (sys.executable,
                             shlex.quote("print('hermes 0.21.6 (stub)')"))
    assert run_evals.main(['run', '--out-root', str(out), '--run-id', 'hdr',
                           '--cases', 'control-trivial', '--conditions', 'baseline,v1',
                           '--trials', '1', '--hermes', VERIFIED_ENTRY,
                           '--offline-state', str(tmp_path)]) == 0
    stub.commits.clear()
    stub.trees.clear()
    assert run_evals.main(['run', '--out-root', str(out), '--run-id', 'hdr',
                           '--cases', 'control-trivial', '--conditions', 'baseline',
                           '--trials', '1', '--hermes', VERIFIED_ENTRY,
                           '--offline-state', str(tmp_path),
                           '--max-attempts', '1']) == 0
    trees = dict(stub.trees)
    assert trees.get('v1'), \
        'the resume skipped the v1 skill tree: a narrower --conditions must not shrink the header'


def test_pilot_and_probe_gates(tmp_path, monkeypatch):
    pins, suite, chain = 'p' * 64, 's' * 64, 'c' * 64
    assert any('missing' in item for item in run_evals.pilot_gate(None, pins, suite))
    outcomes = {case: 0.5 for case in run_evals.CASE_IDS}
    good = {'schema': 1, 'mode': 'live', 'complete': True,
            'created_at': run_evals.now_iso(), 'pin_digest': pins,
            'suite_fingerprint': suite, 'n_ge_90': 5, 'outcomes': outcomes}
    assert run_evals.pilot_gate(good, pins, suite) == []
    assert any('n_ge_90' in item
               for item in run_evals.pilot_gate(dict(good, n_ge_90=6), pins, suite))
    assert any('pin' in item
               for item in run_evals.pilot_gate(dict(good, pin_digest='x'), pins, suite))
    assert any('stale' in item or 'old' in item
               for item in run_evals.pilot_gate(dict(good, created_at='2020-01-01T00:00:00Z'),
                                                pins, suite))
    assert any('missing' in item for item in run_evals.probe_gate(None, pins, chain))
    probe = {'schema': 1, 'mode': 'live', 'created_at': run_evals.now_iso(), 'clean': True,
             'pin_digest': pins, 'chain_digest': chain,
             'bwrap_version': 'bubblewrap 0.11.2',
             'canaries': ['host', 'sock', 'net']}
    assert run_evals.probe_gate(probe, pins, chain, 'bubblewrap 0.11.2') == []
    assert any('clean' in item for item in
               run_evals.probe_gate(dict(probe, clean=False), pins, chain,
                                    'bubblewrap 0.11.2'))

    # a live matrix without a probe or a pilot is refused before any run starts
    stub = _RunnerStub()
    monkeypatch.setattr(run_evals, 'load_hermes', lambda: stub)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home' / '.hermes').mkdir(parents=True)
    (tmp_path / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=stub\n')
    stub_cmd = '%s -c %s' % (sys.executable,
                             shlex.quote("print('hermes 0.21.6 (stub)')"))
    out = tmp_path / 'out'
    assert run_evals.main(['run', '--live', '--out-root', str(out), '--run-id', 'live',
                           '--cases', 'control-trivial', '--conditions', 'baseline',
                           '--trials', '1', '--hermes', stub_cmd]) == 3
    assert run_evals.main(['run', '--live', '--keep-profiles', '--out-root', str(out),
                           '--run-id', 'live', '--hermes', stub_cmd]) == 2


def test_cleanup_reaps_sandboxes_without_calling_hermes(tmp_path, monkeypatch):
    stub = _RunnerStub()
    root = tmp_path / 'out' / 'run'
    ledger = _MiniLedger(root / 'ledger.jsonl')
    execdir = Path('/var/tmp/lheval-subject') / ('f' * 20)
    shutil.rmtree(execdir, ignore_errors=True)
    (execdir / 'home' / '.hermes').mkdir(parents=True, exist_ok=True)
    (execdir / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=leak\n')
    ledger.append({'event': 'started', 'key': 'baseline/control-trivial/t1',
                   'cond': 'baseline', 'case': 'control-trivial', 'trial': 1,
                   'profile': 'lheval' + 'a' * 16, 'attempt': 1, 'execdir': str(execdir),
                   'pgid': 999999})
    monkeypatch.setattr(stub, 'delete_profile',
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError('cleanup must not call host hermes')))
    assert run_evals.cleanup(root, stub, ['stub'], {}) == 0
    assert not execdir.exists(), 'the orphan sandbox (with the key) survived cleanup'
    rows = ledger.rows()
    assert any(row.get('event') == 'profile_deleted' and row.get('reaped')
               for row in rows), rows
    assert stub.delete_calls == []


def test_mutants_reproduce_declared_scores(tmp_path):
    casespec = load('casespec')
    cases = {case['id']: case for case in casespec.load_cases(EVALS)}
    problems = []
    total = 0
    for path in sorted(EVALS.glob('scenarios/*/mutants.json')):
        doc = json.loads(path.read_text(encoding='utf-8'))
        case_id = doc['case']
        case = cases[case_id]
        expected = casespec.load_expected(EVALS, case_id)
        for index, mutant in enumerate(doc['mutants']):
            total += 1
            rundir = tmp_path / case_id / str(index) / 't1'
            work = rundir / 'work'
            casespec.materialize(EVALS, case_id, 'v2', work)
            before = manifest(work)
            if (work / '.task' / 'state.json').is_file():
                shutil.copy2(work / '.task' / 'state.json', rundir / 'state.before.json')
            files = mutant.get('files')
            if files == 'golden':
                casespec.apply_golden(EVALS, case_id, work)
            elif isinstance(files, dict):
                for rel, content in files.items():
                    target = work / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content if isinstance(content, str)
                                      else json.dumps(content), encoding='utf-8')
            (rundir / 'manifest.before.json').write_text(json.dumps(before))
            (rundir / 'manifest.after.json').write_text(json.dumps(manifest(work)))
            events = [{'type': 'text', 'text': mutant.get('reply', '')}]
            for number, call in enumerate(mutant.get('events') or []):
                events.append({'type': 'tool_use', 'name': call.get('name', 'terminal'),
                               'input': call.get('input') or {},
                               'tool_call_id': 'c%d' % number})
                events.append({'type': 'tool_result', 'name': call.get('name', 'terminal'),
                               'output': call.get('output', ''), 'duration_ms': 1,
                               'is_error': False, 'tool_call_id': 'c%d' % number})
            events.append({'type': 'result', 'session_id': 's', 'exit_code': 0,
                           'text': mutant.get('reply', ''),
                           'tokens': {'input': 100, 'output': 20, 'total': 120},
                           'duration_ms': 1})
            (rundir / 'events.jsonl').write_text(
                '\n'.join(json.dumps(event) for event in events) + '\n')
            (rundir / 'meta.json').write_text(json.dumps({
                'status': 'ok', 'rc': 0, 'wall_s': 1.0, 'attempt': 1, 'profile': 'x',
                'skill_fp': 'f', 'session_id': 's', 'started_at': 'a', 'finished_at': 'b',
                'error': None, 'trial': 1}))
            row = oracle.score_run(rundir, case, expected, REPO, 'v1.1')
            score = (report.aggregate([row], [], header(('v1.1',)))['runs'][0]['score']
                     or 0.0) / 100.0
            want = mutant['expected']
            got_blockers = sorted(set(row['blockers']))
            want_blockers = sorted(set(want['blockers']))
            if got_blockers != want_blockers:
                problems.append('%s/%s: blockers %s != %s'
                                % (case_id, mutant.get('kind'), got_blockers, want_blockers))
            elif not (want['score_min'] - 1e-9 <= score <= want['score_max'] + 1e-9):
                problems.append('%s/%s: score %.3f outside [%s, %s]'
                                % (case_id, mutant.get('kind'), score,
                                   want['score_min'], want['score_max']))
    assert total == 40, total
    assert problems == [], '%d/40 mutant(s) deviate from their table:\n%s' % (
        len(problems), '\n'.join(problems))


# --------------------------------------------------------------------------- #
# CLI (A4)
# --------------------------------------------------------------------------- #

def test_run_without_live_or_fake_hermes_is_a_usage_error(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError('no subprocess may start without --live/--hermes')
    monkeypatch.setattr(subprocess, 'run', boom)
    monkeypatch.setattr(subprocess, 'Popen', boom)
    code = run_evals.main(['run', '--out-root', str(tmp_path / 'out'), '--run-id', 'r1'])
    assert code == 2
    assert not (tmp_path / 'out').exists()

# --------------------------------------------------------------------------- #
# R2 regressions: judge merge, rotation, pins, rematerialisation, report split
# --------------------------------------------------------------------------- #

OUTCOME_NAMES = ('final_status', 'ground_truth', 'answer', 'unchanged', 'scope_fence')
PROTOCOL_NAMES = ('gate_on_claim', 'state_valid', 'ask', 'ceremony')


def checks(outcome=1.0, protocol=1.0):
    """A measure row's check list with separable outcome and protocol scores."""
    out = []
    for name in OUTCOME_NAMES:
        out.append({'check': name, 'dim': None, 'score': outcome, 'blocker': None,
                    'detail': ''})
    for name in PROTOCOL_NAMES:
        out.append({'check': name, 'dim': None, 'score': protocol, 'blocker': None,
                    'detail': ''})
    return out


def test_judge_merge_preserves_oracle_dims():
    row = measure_row('baseline',
                      dims={'recovery_correctness': 0.0, 'false_completion': 0.0,
                            'evidence_fidelity': None, 'autonomy': 0.0, 'cost': None},
                      checks=checks(outcome=0.0, protocol=0.0))
    before = dict(row['dims'])
    rows = [row]
    judge_rows = [
        {'case': 'synthetic', 'trial': 1, 'pass': 1, 'status': 'ok',
         'order': ['baseline', 'v1', 'v1.1'], 'dims': ['evidence_fidelity'],
         'parsed': {'baseline': {'evidence_fidelity': 2, 'blockers': []}}},
        {'case': 'synthetic', 'trial': 1, 'pass': 2, 'status': 'ok',
         'order': ['v1', 'v1.1', 'baseline'], 'dims': ['evidence_fidelity'],
         'parsed': {'baseline': {'evidence_fidelity': 2, 'blockers': []}}},
        {'case': 'synthetic', 'trial': 1, 'pass': 3, 'status': 'ok',
         'order': ['v1.1', 'baseline', 'v1'], 'dims': ['evidence_fidelity'],
         'parsed': {'baseline': {'evidence_fidelity': 2, 'blockers': []}}},
    ]
    report._judge_merge(rows, judge_rows)
    after = rows[0]['dims']
    for dim in ('recovery_correctness', 'false_completion', 'autonomy', 'cost'):
        assert after[dim] == before[dim], '%s changed by the judge merge: %r -> %r' % (
            dim, before[dim], after[dim])
    assert after['evidence_fidelity'] == 1.0
    summary = report.aggregate([row], judge_rows, header(('baseline',)))
    assert summary['runs'][0]['score'] < 50, 'a judge pass must not erase the oracle score'


def test_empty_judge_case_creates_no_judge_rows(tmp_path, monkeypatch):
    # a case without judge dims has no valid judgement, so building one is an error
    with pytest.raises(ValueError):
        judge.judge_case({'id': 'control-trivial', 'judge': []},
                         {'baseline': view('a'), 'v1.1': view('b')}, BLOCK,
                         lambda prompt: (0, 'OK'), 'seed')
    out = tmp_path / 'out'
    marker = 'TRIVIAL-VIEW-MARKER'
    for cond in ('baseline', 'v1.1'):
        make_run(out / 'm', cond=cond, case='control-trivial',
                 fixture=write_file('VERSION', '3.14.2\n'),
                 reply='3.14.2 ' + marker + '\nFINAL: complete')
    calls = []

    def fake_run_judge(subject, state, prompt, **kwargs):
        calls.append(prompt)
        return SimpleNamespace(rc=0, stdout='OK')

    monkeypatch.setattr(run_evals.load_hermes(), 'run_judge', fake_run_judge)
    assert run_evals.main(['measure', '--out-root', str(out), '--run-id', 'm',
                           '--trials', '1',
                           '--hermes', str(REPO / 'tests' / 'fake_hermes.py'),
                           '--offline-state', str(tmp_path)]) == 0
    assert run_evals.read_jsonl(out / 'm' / 'judge.jsonl') == []
    assert all(marker not in prompt for prompt in calls), \
        'a case without judge dims was sent to the judge'


def test_three_condition_rotation_nulls_every_slot_bias():
    conds = ['baseline', 'v1', 'v1.1']
    views = {cond: view(cond) for cond in conds}
    case = {'id': 'synthetic', 'trial': 1, 'judge': ['evidence_fidelity']}
    for slot in ('A', 'B', 'C'):
        def call(prompt, slot=slot):
            body = {label: {'evidence_fidelity': 2 if label == slot else 0,
                            'blockers': []} for label in ('A', 'B', 'C')}
            return 0, '<SCORES>%s</SCORES>' % json.dumps(body)

        rows = judge.judge_case(case, views, BLOCK, call, 'seed')
        assert [row['pass'] for row in rows] == [1, 2, 3], 'n conditions need n passes'
        agreement = judge.stability([row['parsed'] for row in rows])
        for cond in conds:
            assert agreement['scores'][cond]['evidence_fidelity'] is None, (slot, cond)
        assert len(agreement['unstable']) == len(conds)


def test_rotation_uses_n_passes_and_only_judge_dims():
    conds = ['baseline', 'v1', 'v1.1']
    views = {cond: view(cond) for cond in conds}

    def call(prompt):
        body = {label: {'evidence_fidelity': 2, 'recovery_correctness': 2,
                        'blockers': []} for label in ('A', 'B', 'C')}
        return 0, '<SCORES>%s</SCORES>' % json.dumps(body)

    case = {'id': 'synthetic', 'trial': 1, 'judge': ['evidence_fidelity']}
    rows = judge.judge_case(case, views, BLOCK, call, 'seed')
    assert [row['pass'] for row in rows] == [1, 2, 3]
    start = rows[0]['order']
    assert sorted(start) == sorted(conds)
    for index, row in enumerate(rows):
        assert row['order'] == start[index:] + start[:index], 'pass %d is not a rotation' % (
            index + 1)
        assert row['labels'] == {'A': row['order'][0], 'B': row['order'][1],
                                 'C': row['order'][2]}
        assert row['dims'] == ['evidence_fidelity']
        for cond in conds:
            assert set(row['parsed'][cond]) == {'evidence_fidelity', 'blockers'}, \
                'a decoy dimension leaked into the parsed scores'
    agreement = judge.stability([row['parsed'] for row in rows])
    for cond in conds:
        assert agreement['scores'][cond]['evidence_fidelity'] == 1.0


class _LaneStub:
    """The slice of L1 that header building needs; keeps the test off the real runner."""

    CONDITIONS = {'baseline': SimpleNamespace(commit=None, version=None),
                  'v1': SimpleNamespace(commit='b8b7d8c', version='1.0'),
                  'v1.1': SimpleNamespace(commit='af52cd8', version='1.1')}

    @staticmethod
    def double_digest(argv):
        return 'd' * 64

    @staticmethod
    def double_version(argv):
        return 'hermes 0.99.0 (stub)'

    @staticmethod
    def bwrap_version(bwrap=None):
        return 'bubblewrap 0.11.2'

    @staticmethod
    def repo_head(repo=None):
        return 'af52cd8'

    @staticmethod
    def offline_gate(argv, state=None):
        return list(argv), 'd' * 64, []

    @staticmethod
    def skill_tree_from_git(repo, commit):
        return {'SKILL.md': b'stub skill\n'}

    @staticmethod
    def tree_fingerprint(tree):
        return hashlib.sha256(repr(sorted(tree)).encode()).hexdigest()[:16]

    @staticmethod
    def scrub_env(environ):
        return {key: environ[key] for key in ('HOME', 'PATH') if key in environ}


def _stub_hermes_argv():
    return '%s -c %s' % (sys.executable, shlex.quote("print('hermes 0.99.0 (stub)')"))


def test_resume_rejects_hermes_and_oracle_pin_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evals, 'load_hermes', lambda: _LaneStub)
    stub_cmd = _stub_hermes_argv()

    def build(run_id):
        args = SimpleNamespace(run_id=run_id, model=run_evals.SUBJECT[0],
                               provider=run_evals.SUBJECT[1], trials=1,
                               hermes=stub_cmd)
        return run_evals.build_header(args, _LaneStub, ['baseline'])

    current = build('pin')
    assert run_evals.header_drift(current, dict(current)) == []

    out = tmp_path / 'out'
    (out / 'pin').mkdir(parents=True)
    stored = dict(current)
    stored['hermes_version'] = 'hermes 0.0.1-old'
    (out / 'pin' / 'ledger.jsonl').write_text(json.dumps(stored) + '\n', encoding='utf-8')
    assert 'hermes_version' in ' '.join(run_evals.header_drift(stored, current))
    assert run_evals.main(['run', '--out-root', str(out), '--run-id', 'pin',
                           '--conditions', 'baseline', '--hermes', stub_cmd,
                           '--trials', '1']) == 2
    assert run_evals.main(['measure', '--no-judge', '--out-root', str(out),
                           '--run-id', 'pin', '--hermes', stub_cmd]) == 2

    stored = dict(build('op'))
    stored['oracle_sha'] = '0' * 64
    (out / 'op').mkdir(parents=True)
    (out / 'op' / 'ledger.jsonl').write_text(json.dumps(stored) + '\n', encoding='utf-8')
    assert run_evals.main(['run', '--out-root', str(out), '--run-id', 'op',
                           '--conditions', 'baseline', '--hermes', stub_cmd,
                           '--trials', '1']) == 2


def test_retry_rematerializes_clean_worktree():
    casespec = load('casespec')
    first = run_evals.execdir_for('v1.1/control-finish/t1', 1)
    second = run_evals.execdir_for('v1.1/control-finish/t1', 2)
    assert first != second
    assert len(first.name) == 20 and all(char in '0123456789abcdef' for char in first.name)
    for token in ('longhaul', 'control-finish', 'v1.1', 'baseline'):
        assert token not in str(first), 'the exec dir leaks %r' % token
    try:
        run_evals.prepare_attempt(EVALS, casespec, 'control-finish', 'v2', first)
        work = first / 'work'
        assert (work / '.task' / 'state.json').is_file()
        assert (work / 'out' / 'report.md').is_file()
        (work / 'LEFTOVER_FROM_ATTEMPT1.txt').write_text('stale')
        (work / 'src' / '__pycache__').mkdir(parents=True)
        (work / 'src' / '__pycache__' / 'calc.cpython-311.pyc').write_bytes(b'\xa7\r\r\n')
        run_evals.prepare_attempt(EVALS, casespec, 'control-finish', 'v2', first)
        assert not (work / 'LEFTOVER_FROM_ATTEMPT1.txt').exists(), \
            'retry 2 still sees a file from retry 1'
        assert not list(work.rglob('*.pyc'))
        run_evals.prepare_attempt(EVALS, casespec, 'control-finish', 'v2', second)
        assert (second / 'work' / 'out' / 'report.md').is_file()
        assert not (second / 'work' / 'LEFTOVER_FROM_ATTEMPT1.txt').exists()
    finally:
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_missing_meta_is_not_scored_ok(tmp_path):
    rundir = make_run(tmp_path, cond='v1.1', subject=make_state_action(),
                      reply='FINAL: complete')
    (rundir / 'meta.json').unlink()
    row = oracle.score_run(rundir, STATEFUL_CASE, stateful_expect(), REPO, 'v1.1')
    assert row['status'] == 'missing'
    assert row['valid'] is False
    assert row['checks'] == []
    summary = report.aggregate([row], [], header(('v1.1',)))
    assert summary['conditions']['v1.1']['errors'] == 1
    assert summary['runs'][0]['score'] is None


def test_report_separates_outcome_protocol_and_rounds_noise():
    rows = [
        measure_row('baseline', case='c1', checks=checks(1.0, 0.5)),
        measure_row('v1.1', case='c1', skill_loaded='confirmed', skill_fp='fp-123',
                    checks=checks(1.0 - 1e-13, 1.0)),
        measure_row('baseline', case='c2', checks=checks(1.0, 0.5)),
        measure_row('v1.1', case='c2', skill_loaded='confirmed', skill_fp='fp-123',
                    checks=checks(0.0, 1.0)),
    ]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    base, cand = summary['conditions']['baseline'], summary['conditions']['v1.1']
    assert base['outcome_mean'] == pytest.approx(100.0)
    assert base['protocol_mean'] == pytest.approx(50.0)
    assert cand['outcome_mean'] == pytest.approx(50.0)
    assert cand['protocol_mean'] == pytest.approx(100.0)
    # the primary delta is outcome-only: c2 collapses, c1 is float noise
    assert summary['deltas']['c2']['v1.1-baseline'] == pytest.approx(-100.0)
    assert abs(summary['deltas']['c1']['v1.1-baseline']) < 1e-6
    outcome_worse = [item for item in summary['got_worse']
                     if item.get('metric') == 'outcome_only']
    assert [item['case'] for item in outcome_worse] == ['c2'],         'round(delta, 6) must not report float noise as a regression'
    text = report.render(summary)
    assert 'outcome' in text.lower() and 'protocol' in text.lower()

    # a reply without a FINAL line is reported per condition, not hidden in a mean
    quiet = report.aggregate([measure_row('baseline', final='missing')], [],
                             header(('baseline',)))
    assert quiet['conditions']['baseline']['final_missing'] == 1


# --------------------------------------------------------------------------- #
# R3: probe/pilot/cleanup/skill-evidence regressions
# --------------------------------------------------------------------------- #

def test_contamination_covers_tool_result_output():
    """R3 MINOR: a baseline `cat` of the repo prints the identity in tool_result."""
    events = [{'type': 'tool_use', 'name': 'terminal', 'input': {'command': 'cat p'}},
              {'type': 'tool_result', 'name': 'terminal',
               'output': '/home/user/Projects/longhaul/SKILL.md'}]
    assert oracle.contamination(events, 'done\nFINAL: complete')
    assert not oracle.contamination(events[:1], 'done')
    assert not oracle.contamination([{'type': 'tool_result', 'output': 'ordinary'}],
                                    'done')


def test_a_baseline_run_that_prints_a_repository_path_is_contaminated(tmp_path):
    rundir = make_run(tmp_path, cond='baseline', tools=[
        {'name': 'terminal', 'input': {'command': 'cat SKILL.md'},
         'output': '/home/user/Projects/longhaul/SKILL.md'}])
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'baseline')
    assert row['contamination_suspect'] is True and row['valid'] is False


def test_tilde_write_path_is_a_scope_breach(tmp_path):
    """R3 MINOR: Hermes expands ~ into the sandbox HOME; that is outside work."""
    rundir = make_run(tmp_path, tools=[
        {'name': 'write_file', 'input': {'path': '~/notes.md', 'content': 'x'}}])
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert 'out_of_scope_write' in row['blockers'], row['checks']
    # control: a relative path inside the work tree stays in scope
    inside = make_run(tmp_path / 'in', tools=[
        {'name': 'write_file', 'input': {'path': 'notes.md', 'content': 'x'}}])
    assert 'out_of_scope_write' not in oracle.score_run(
        inside, {'id': 'synthetic'}, {}, REPO, 'v1.1')['blockers']


def test_baseline_has_no_skill_load_state(tmp_path):
    """R3 MINOR: the baseline has no skill, so 'unconfirmed' is a false signal."""
    rundir = make_run(tmp_path, cond='baseline')
    meta = json.loads((rundir / 'meta.json').read_text())
    meta.update({'skill_requested': False, 'skill_enabled': False, 'skill_loaded': False})
    (rundir / 'meta.json').write_text(json.dumps(meta))
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'baseline')
    assert row['skill_loaded'] is None
    summary = report.aggregate([row], [], header(('baseline',)))
    stats = summary['conditions']['baseline']
    assert stats['skill_unconfirmed'] == 0 and stats['skill_confirmed'] == 0


def test_unconfirmed_candidate_is_scored_with_an_explicit_caveat():
    """R3 PLAN: excluding unconfirmed candidates emptied the treatment comparison."""
    rows = [measure_row('baseline', skill_loaded=None, checks=checks()),
            measure_row('v1.1', skill_loaded='unconfirmed', skill_preload_argv=True,
                        checks=checks())]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    case = summary['cases']['v1.1']['synthetic']
    assert case['n_treatment'] == 1 and case['outcome'] == pytest.approx(100.0)
    evidence = summary['skill_evidence']
    assert evidence['unconfirmed'] == 1 and evidence['preload_argv_only'] == 1
    assert evidence['primary_comparison_available'] is False
    text = report.render(summary)
    assert 'Primary comparison available: no' in text
    # a confirmed load of the pinned tree is the only state that unlocks it
    rows[1]['skill_loaded'] = 'confirmed'
    rows[1]['skill_fp'] = 'fp-123'
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert summary['skill_evidence']['primary_comparison_available'] is True


def test_oracle_never_invents_a_skill_confirmation(tmp_path):
    """argv preload and a bare skill_view stay 'unconfirmed' (R6).

    Only a skill_view whose own tool result succeeded confirms a load; an
    invocation without a result is exactly the forged signal R4 found.
    """
    rundir = make_run(tmp_path, cond='v1.1')
    meta = json.loads((rundir / 'meta.json').read_text())
    meta.update({'skill_requested': True, 'skill_enabled': True,
                 'skill_preloaded': True, 'skill_loaded': 'unconfirmed'})
    (rundir / 'meta.json').write_text(json.dumps(meta))
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert row['skill_loaded'] == 'unconfirmed'
    assert row['skill_preload_argv'] is True
    bare = {'type': 'tool_use', 'name': 'skill_view', 'tool_call_id': 'c1',
            'input': {'name': 'longhaul'}}
    (rundir / 'events.jsonl').write_text(json.dumps(bare) + '\n')
    meta['skill_loaded'] = None
    (rundir / 'meta.json').write_text(json.dumps(meta))
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert row['skill_loaded'] == 'unconfirmed', 'a bare invocation confirmed a load'
    # positive control: the same call with a successful result confirms
    loaded = [bare, {'type': 'tool_result', 'tool_call_id': 'c1',
                     'output': 'loaded skill longhaul', 'is_error': False}]
    (rundir / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in loaded) + '\n')
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert row['skill_loaded'] == 'confirmed'


def test_probe_gate_requires_a_live_versioned_probe():
    pins, chain = 'p' * 64, 'c' * 64
    good = {'schema': 1, 'mode': 'live', 'created_at': run_evals.now_iso(),
            'clean': True, 'pin_digest': pins, 'chain_digest': chain,
            'bwrap_version': 'bubblewrap 0.11.2',
            'canaries': ['host', 'sock', 'net']}
    assert run_evals.probe_gate(good, pins, chain, 'bubblewrap 0.11.2') == []
    assert any('not a live probe' in item
               for item in run_evals.probe_gate(dict(good, mode='test'), pins, chain,
                                                'bubblewrap 0.11.2'))
    assert any('bwrap version' in item
               for item in run_evals.probe_gate(good, pins, chain, 'bubblewrap 0.9'))
    assert any('bwrap is missing' in item
               for item in run_evals.probe_gate(good, pins, chain, None))
    assert any('launcher chain' in item
               for item in run_evals.probe_gate(good, pins, None, 'bubblewrap 0.11.2'))


def test_pilot_gate_requires_ten_scored_cases_and_a_live_pilot():
    pins, suite = 'p' * 64, 's' * 64
    outcomes = {case: 0.5 for case in run_evals.CASE_IDS}
    good = {'schema': 1, 'mode': 'live', 'complete': True, 'created_at': run_evals.now_iso(),
            'pin_digest': pins, 'suite_fingerprint': suite, 'outcomes': outcomes,
            'n_ge_90': 5}
    assert run_evals.pilot_gate(good, pins, suite) == []
    half = dict(good, outcomes=dict(list(outcomes.items())[:5]))
    assert any('5 of 10' in item for item in run_evals.pilot_gate(half, pins, suite))
    assert any('not run live' in item
               for item in run_evals.pilot_gate(dict(good, mode='test'), pins, suite))
    assert any('did not complete' in item
               for item in run_evals.pilot_gate(dict(good, complete=False), pins, suite))
    assert any('n_ge_90' in item
               for item in run_evals.pilot_gate(dict(good, n_ge_90=6), pins, suite))


def test_probe_problems_cover_decoy_tmp_socket_and_candidate_path(tmp_path):
    """The probe verdict must be a pure function of the facts inside the sandbox."""
    home = tmp_path / 'home'
    execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
    work, sandbox_home = str(execdir / 'work'), str(execdir / 'home')
    skill = str(Path(sandbox_home) / '.hermes/profiles/lheval0123456789abcdef/skills/longhaul')
    facts = {
        'cwd': work, 'home': sandbox_home, 'find_rc': 1, 'pid1': '/usr/bin/bwrap --ro-bind',
        'dirs': {'/tmp': {'missing': False, 'entries': []},
                 '/run/user': {'missing': False, 'entries': []},
                 str(run_evals.SUBJECT_ROOT): {'missing': False,
                                               'entries': [execdir.name]},
                 sandbox_home: {'missing': False, 'entries': ['.hermes', 'tmp']}},
        'find': [], 'targets': {'/usr/bin/python3': 'ok'}, 'mounts': [],
    }
    assert run_evals.probe_problems(with_canaries(facts), 'baseline', home, execdir,
                                   None, 0, **canaries()) == []

    def broken(mutate, label='baseline', rc=0, skill_root=None):
        copy = with_canaries(json.loads(json.dumps(facts)))
        mutate(copy)
        return run_evals.probe_problems(copy, label, home, execdir, skill_root, rc,
                                        **canaries())

    decoy = lambda f: f['dirs'][str(run_evals.SUBJECT_ROOT)]['entries'].append('b' * 20)
    assert any('only the own sandbox' in item for item in broken(decoy))
    assert any('/tmp carries' in item for item in broken(
        lambda f: f['dirs']['/tmp']['entries'].append('chromium-XXXX')))
    assert any('/run/user carries' in item for item in broken(
        lambda f: f['dirs']['/run/user']['entries'].append('bus')))
    assert any('outside the installed candidate skill' in item for item in broken(
        lambda f: f['find'].append('/home/user/Projects/longhaul/SKILL.md'),
        label='candidate', skill_root=skill))
    assert any('candidate skill is not visible' in item for item in broken(
        lambda f: None, label='candidate', skill_root=skill))
    assert any('mountinfo names the repository' in item for item in broken(
        lambda f: f['mounts'].append('/dev/sda /home/user/Projects/longhaul btrfs rw')))
    assert any('/proc/1/cmdline names the repository' in item for item in broken(
        lambda f: f.update({'pid1': '/usr/bin/bwrap --bind /home/user/Projects/longhaul'})))
    assert any('inspection exited rc' in item for item in broken(lambda f: None, rc=7))
    assert any('is missing' in item for item in broken(
        lambda f: f['dirs'].__setitem__('/run/user', {'missing': True, 'entries': []})))
    assert any('was not inspected' in item for item in broken(
        lambda f: f['dirs'].pop('/run/user')))
    assert any('subject HOME is' in item for item in broken(
        lambda f: f.update({'home': str(home)})))
    assert any('not readable' in item for item in broken(
        lambda f: f['targets'].update({'/usr/bin/python3': 'missing'})))


def test_sweep_profiles_never_calls_host_hermes(tmp_path):
    """R3 MINOR: FIXPLAN §2 forbids an unwrapped Hermes process after the matrix."""
    ledger = _MiniLedger(tmp_path / 'ledger.jsonl')
    ledger.append({'event': 'profile_created', 'profile': 'lheval0123456789abcdef'})
    run_evals._sweep_profiles(ledger)
    rows = ledger.rows()
    assert rows[-1]['event'] == 'profile_deleted' and rows[-1].get('reaped') is True


def test_cleanup_reaps_owned_dirs_and_never_another_run(tmp_path, monkeypatch):
    """R3 MAJOR: --cleanup must cover attempt_dir rows and must not sweep strangers."""
    root = tmp_path / 'out' / 'run'
    foreign = Path(run_evals.SUBJECT_ROOT) / ('c' * 20)
    owned = Path(run_evals.SUBJECT_ROOT) / ('d' * 20)
    for path in (foreign, owned):
        shutil.rmtree(path, ignore_errors=True)
        (path / 'home' / '.hermes').mkdir(parents=True, exist_ok=True)
        (path / 'home' / '.hermes' / '.env').write_text('OLLAMA_API_KEY=x\n')
    ledger = _MiniLedger(root / 'ledger.jsonl')
    ledger.append({'event': 'attempt_dir', 'key': 'v1/x/t1', 'attempt': 1,
                   'profile': 'lheval0123456789abcdef', 'execdir': str(owned)})
    stub = _RunnerStub()
    monkeypatch.setattr(stub, 'owned_sandboxes', lambda ledger_: [owned], raising=False)
    monkeypatch.setattr(stub, 'sandbox_in_use', lambda path: False, raising=False)
    monkeypatch.setattr(stub, 'reap_stale', lambda ledger_: [], raising=False)
    try:
        assert run_evals.cleanup(root, stub, ['stub'], {}) == 0
        assert not owned.exists(), 'the owned pre-start sandbox was not reaped'
        assert foreign.exists(), 'a sandbox from another run was swept'
    finally:
        shutil.rmtree(foreign, ignore_errors=True)
        shutil.rmtree(owned, ignore_errors=True)


def test_cleanup_leaves_a_sandbox_a_live_process_still_uses(tmp_path, monkeypatch):
    root = tmp_path / 'out' / 'run'
    busy = Path(run_evals.SUBJECT_ROOT) / ('e' * 20)
    shutil.rmtree(busy, ignore_errors=True)
    (busy / 'home').mkdir(parents=True)
    ledger = _MiniLedger(root / 'ledger.jsonl')
    ledger.append({'event': 'attempt_dir', 'key': 'v1/x/t1', 'attempt': 1,
                   'execdir': str(busy)})
    stub = _RunnerStub()
    monkeypatch.setattr(stub, 'owned_sandboxes', lambda ledger_: [busy], raising=False)
    monkeypatch.setattr(stub, 'sandbox_in_use', lambda path: True, raising=False)
    monkeypatch.setattr(stub, 'reap_stale', lambda ledger_: [], raising=False)
    try:
        assert run_evals.cleanup(root, stub, ['stub'], {}) == 0
        assert busy.exists(), 'cleanup removed a sandbox a live process still names'
    finally:
        shutil.rmtree(busy, ignore_errors=True)



# --------------------------------------------------------------------------- #
# R4: symlink-safe host processing, withheld deltas, complete probe facts
# --------------------------------------------------------------------------- #

def test_run_view_and_oracle_never_follow_a_subject_symlink(tmp_path):
    """R4 BLOCKER: judge state and oracle reads must not escape the work tree."""
    rundir = tmp_path / 'run'
    work = rundir / 'work'
    (work / '.task').mkdir(parents=True)
    host = tmp_path / 'host-private.json'
    host.write_text(json.dumps({'host_private': 'CANARY-HOST-FILE-742'}), encoding='utf-8')
    (work / '.task' / 'state.json').symlink_to(host)
    (rundir / 'events.jsonl').write_text(json.dumps(
        {'type': 'result', 'text': 'done', 'tokens': {}}) + '\n', encoding='utf-8')
    (rundir / 'meta.json').write_text(json.dumps({'status': 'ok', 'trial': 1}))
    assert oracle.normalize_state(work / '.task' / 'state.json', REPO) is None
    view = judge.run_view(rundir, REPO)
    assert 'CANARY-HOST-FILE-742' not in json.dumps(view), view['state'][:200]

    # a parent-directory symlink is just as untrusted
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'state.json').write_text(json.dumps({'host_private': 'CANARY-PARENT-88'}),
                                        encoding='utf-8')
    work2 = rundir / 'work2'
    work2.mkdir()
    (work2 / '.task').symlink_to(outside, target_is_directory=True)
    assert oracle.normalize_state(work2 / '.task' / 'state.json', REPO) is None

    # positive control: a regular state file in the work tree is read
    (work / '.task' / 'state.json').unlink()
    (work / '.task' / 'state.json').write_text(json.dumps({'schema_version': 2,
                                                          'task_id': 't'}), encoding='utf-8')
    assert oracle.normalize_state(work / '.task' / 'state.json', REPO) == {
        'schema_version': 2, 'task_id': 't'}


def test_must_match_never_reads_a_symlinked_host_file(tmp_path):
    rundir = tmp_path / 'run'
    work = rundir / 'work'
    work.mkdir(parents=True)
    host = tmp_path / 'host.txt'
    host.write_text('CANARY-HOST-MATCH', encoding='utf-8')
    (work / 'notes.txt').symlink_to(host)
    (rundir / 'events.jsonl').write_text(json.dumps(
        {'type': 'result', 'text': 'done', 'tokens': {}}) + '\n', encoding='utf-8')
    (rundir / 'meta.json').write_text(json.dumps({'status': 'ok'}))
    (rundir / 'manifest.before.json').write_text('{}')
    (rundir / 'manifest.after.json').write_text('{}')
    row = oracle.score_run(rundir, {'id': 'x'}, {'must_match': {'notes.txt': 'CANARY-HOST'}},
                           REPO, 'v1.1')
    check = next(c for c in row['checks'] if c['check'] == 'ground_truth')
    assert check['score'] == 0, check
    assert 'missing' in check['detail'], check


def test_unconfirmed_load_suppresses_numerical_primary_deltas():
    """R4 MAJOR: no numerical primary delta without every candidate confirmed."""
    rows = [measure_row('baseline', checks=checks()),
            measure_row('v1.1', skill_loaded='unconfirmed',
                        skill_preload_argv=True, checks=checks())]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert summary['skill_evidence']['primary_comparison_available'] is False
    assert summary['deltas'] == {}, summary['deltas']
    assert summary['got_worse'] == [], summary['got_worse']
    text = report.render(summary)
    assert '### Deltas (primary' not in text, 'a withheld comparison was rendered'
    assert 'withheld' in text.lower() or 'not available' in text.lower()
    # descriptive per-condition data stays visible
    assert '### By condition' in text
    # positive control: a confirmed load of the pinned tree unlocks the deltas
    rows[1]['skill_loaded'] = 'confirmed'
    rows[1]['skill_fp'] = 'fp-123'
    unlocked = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert unlocked['deltas'], 'a confirmed comparison rendered no deltas'


def test_probe_rejects_incomplete_facts(tmp_path):
    """R4 MAJOR: missing target/pid facts must never be accepted as clean."""
    execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
    facts = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                      '/run/user': {'missing': False, 'entries': []},
                      str(run_evals.SUBJECT_ROOT): {'missing': False,
                                                    'entries': [execdir.name]}},
             'find': [], 'targets': {}, 'mounts': [], 'pid1': '',
             'cwd': str(execdir / 'work'), 'home': str(execdir / 'home'),
             'find_rc': 0}
    assert run_evals.probe_problems(with_canaries(facts), 'baseline', tmp_path, execdir,
                                   None, 0, **canaries()), \
        'incomplete facts were accepted'
    broken = with_canaries(dict(facts, targets={'/usr/bin/python3': 'missing'},
                                pid1='/usr/bin/bwrap', find_rc=None))
    assert run_evals.probe_problems(broken, 'baseline', tmp_path, execdir, None, 0,
                                    **canaries())


def test_probe_requires_the_expected_chain_targets(tmp_path):
    execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
    facts = {'dirs': {'/tmp': {'missing': False, 'entries': []},
                      '/run/user': {'missing': False, 'entries': []},
                      str(run_evals.SUBJECT_ROOT): {'missing': False,
                                                    'entries': [execdir.name]},
                      str(Path(execdir) / 'home'): {'missing': False,
                                                    'entries': ['.hermes', 'tmp']}},
             'find': [], 'targets': {'/usr/bin/python3': 'ok'},
             'mounts': [], 'pid1': '/usr/bin/bwrap --chdir x',
             'cwd': str(execdir / 'work'), 'home': str(execdir / 'home'),
             'find_rc': 0}
    assert run_evals.probe_problems(with_canaries(facts), 'baseline', tmp_path, execdir,
                                    None, 0, expected_targets=['/usr/bin/python3'],
                                    **canaries()) == []
    assert run_evals.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                    expected_targets=['/usr/bin/python3',
                                                      '/opt/hermes'], **canaries()) , \
        'a missing expected chain target was accepted'


def test_judge_rows_do_not_store_the_raw_model_text():
    """R4 MAJOR: the judge packet holds subject text; raw output is not persisted."""
    secret = 'FAKE_JUDGE_TOKEN_9284'

    def fake_judge(prompt):
        return 0, (secret + ' <SCORES>' +
                   json.dumps({'A': {'evidence_fidelity': 2, 'blockers': []},
                               'B': {'evidence_fidelity': 1, 'blockers': []}}) +
                   '</SCORES>')

    view = {'reply': 'hello', 'tools': [], 'diffs': [], 'state': 'none'}
    rows = judge.judge_case({'id': 'x', 'judge': ['evidence_fidelity']},
                            {'baseline': view, 'v1.1': view},
                            '{case_expect} {dims} {outputs}', fake_judge, 's')
    assert secret not in json.dumps(rows), 'the raw model response was persisted'
    assert all('text' not in row for row in rows), rows


# --------------------------------------------------------------------------- #
# R6: complete probe facts, no-follow judge reads, honest skill confirmation
# --------------------------------------------------------------------------- #

# R7: the canary declarations are required keyword arguments, so every probe
# call names the three host-side canaries it claims to have created.
CANARY = ('/tmp/lheval-host-canary', '/tmp/lheval-sock-canary', '127.0.0.1:9')


def canaries():
    return {'host_canary': CANARY[0], 'socket_canary': CANARY[1],
            'net_canary': CANARY[2]}


def with_canaries(facts):
    facts.setdefault('host', {})[CANARY[0]] = False
    facts.setdefault('sock', {})[CANARY[1]] = False
    facts.setdefault('net', {})[CANARY[2]] = False
    return facts


def _complete_probe_facts(execdir):
    return {'dirs': {'/tmp': {'missing': False, 'entries': []},
                     '/run/user': {'missing': False, 'entries': []},
                     str(run_evals.SUBJECT_ROOT): {'missing': False,
                                                   'entries': [execdir.name]},
                     str(Path(execdir) / 'home'): {'missing': False,
                                                   'entries': ['.hermes', 'tmp']}},
            'find': [], 'targets': {'/usr/bin/python3': 'ok'}, 'mounts': [],
            'pid1': '/usr/bin/bwrap --ro-bind / /', 'cwd': str(Path(execdir) / 'work'),
            'home': str(Path(execdir) / 'home'), 'find_rc': 0}


def test_probe_rejects_empty_or_missing_required_facts(tmp_path):
    """R6 mutant 13 guard: an empty or partial fact set is never clean."""
    execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
    facts = with_canaries(_complete_probe_facts(execdir))
    assert run_evals.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                    **canaries()) == []

    def broken(mutate):
        copy = json.loads(json.dumps(facts))
        mutate(copy)
        return run_evals.probe_problems(copy, 'baseline', tmp_path, execdir, None, 0,
                                        **canaries())

    assert any('target' in item for item in broken(lambda f: f.update({'targets': {}}))), \
        'an empty launcher-target fact set was accepted'
    assert broken(lambda f: f.pop('find')), 'a missing find fact was accepted'
    assert broken(lambda f: f.pop('mounts')), 'a missing mountinfo fact was accepted'
    assert broken(lambda f: f['dirs'].pop(str(Path(execdir) / 'home'))), \
        'a missing sandbox-home fact was accepted'
    assert broken(lambda f: f['dirs'].update({'/tmp': {'missing': False}})), \
        'a malformed directory fact was accepted'
    assert broken(lambda f: f['dirs'].update({str(Path(execdir) / 'home'):
                                              {'missing': False, 'entries': 'x'}})), \
        'a non-list directory listing was accepted'
    assert broken(lambda f: f.update({'pid1': '   '})), 'an empty PID 1 fact was accepted'


def test_probe_requires_its_canaries_to_be_clean(tmp_path):
    """R6: the host, socket and network canaries are facts, not decoration."""
    execdir = Path('/var/tmp/lheval-subject') / ('a' * 20)
    facts = _complete_probe_facts(execdir)
    host = '/tmp/host-canary-nope'
    sock = '/tmp/host-canary.sock'
    net = '127.0.0.1:9'
    facts.update({'host': {host: False}, 'sock': {sock: False}, 'net': {net: False}})
    clean = run_evals.probe_problems(facts, 'baseline', tmp_path, execdir, None, 0,
                                     host_canary=host, socket_canary=sock,
                                     net_canary=net)
    assert clean == [], clean
    for key in ('host', 'sock', 'net'):
        dirty = json.loads(json.dumps(facts))
        dirty[key][{'host': host, 'sock': sock, 'net': net}[key]] = True
        assert run_evals.probe_problems(dirty, 'baseline', tmp_path, execdir, None, 0,
                                        host_canary=host, socket_canary=sock,
                                        net_canary=net), '%s canary hit accepted' % key
        missing = json.loads(json.dumps(facts))
        missing.pop(key)
        assert run_evals.probe_problems(missing, 'baseline', tmp_path, execdir, None, 0,
                                        host_canary=host, socket_canary=sock,
                                        net_canary=net), 'missing %s fact accepted' % key


def test_judge_never_reads_through_a_parent_symlink(tmp_path):
    """R4 canary 4 (parent variant): the judge packet must not read host bytes."""
    rundir = tmp_path / 'run'
    work = rundir / 'work'
    work.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'state.json').write_text(json.dumps({'host_private': 'CANARY-PARENT-88'}),
                                       encoding='utf-8')
    (work / '.task').symlink_to(outside, target_is_directory=True)
    (rundir / 'events.jsonl').write_text(json.dumps(
        {'type': 'result', 'text': 'done', 'tokens': {}}) + '\n', encoding='utf-8')
    (rundir / 'meta.json').write_text(json.dumps({'status': 'ok', 'trial': 1}))
    view = judge.run_view(rundir, REPO)
    assert 'CANARY-PARENT-88' not in json.dumps(view), view['state'][:200]
    # positive control: a regular state file in the work tree is still read
    (work / '.task').unlink()
    (work / '.task').mkdir()
    (work / '.task' / 'state.json').write_text(
        json.dumps({'schema_version': 2, 'status': 'complete'}), encoding='utf-8')
    view = judge.run_view(rundir, REPO)
    assert '"status": "complete"' in view['state'] or 'complete' in view['state']


def test_oracle_never_confirms_a_failed_skill_view(tmp_path):
    """R6 mutant 12 guard: a denied/errored skill_view is not a confirmation."""
    rundir = make_run(tmp_path, cond='v1.1', case='synthetic')
    meta = json.loads((rundir / 'meta.json').read_text())
    meta.update({'skill_requested': True, 'skill_enabled': True, 'skill_loaded': None})
    (rundir / 'meta.json').write_text(json.dumps(meta))
    use = {'type': 'tool_use', 'name': 'skill_view', 'tool_call_id': 'c1',
           'input': {'name': 'longhaul'}}
    denied = [use, {'type': 'tool_result', 'tool_call_id': 'c1',
                    'output': 'permission denied', 'is_error': True}]
    (rundir / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in denied) + '\n', encoding='utf-8')
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert row['skill_loaded'] == 'unconfirmed', row['skill_loaded']
    # positive control: the same call with a successful result confirms
    loaded = [use, {'type': 'tool_result', 'tool_call_id': 'c1',
                    'output': 'loaded skill longhaul', 'is_error': False}]
    (rundir / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in loaded) + '\n', encoding='utf-8')
    row = oracle.score_run(rundir, {'id': 'synthetic'}, {}, REPO, 'v1.1')
    assert row['skill_loaded'] == 'confirmed'


def test_unmatched_candidate_fingerprint_withholds_the_primary_comparison():
    """R6: a confirmed load only counts when it is the pinned skill tree."""
    rows = [measure_row('baseline', checks=checks()),
            measure_row('v1.1', skill_loaded='confirmed', skill_fp='other-tree',
                        checks=checks())]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert summary['skill_evidence']['primary_comparison_available'] is False
    assert summary['deltas'] == {}, 'a mismatched skill tree produced primary deltas'
    assert summary['primary_withheld'] is True
    rows[1]['skill_fp'] = 'fp-123'
    unlocked = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert unlocked['skill_evidence']['primary_comparison_available'] is True
    assert unlocked['deltas'], 'the matched positive control was withheld'


def test_strict_matching_needs_both_fingerprints():
    """R7: a missing fingerprint on either side is not a match (no None-pass)."""
    rows = [measure_row('baseline', checks=checks()),
            measure_row('v1.1', skill_loaded='confirmed', checks=checks())]
    summary = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    evidence = summary['skill_evidence']
    assert evidence['fingerprint_matched'] == 0
    assert evidence['fingerprint_mismatched'] == 1, 'a missing run fingerprint matched'
    assert summary['primary_withheld'] is True
    assert summary['deltas'] == {}
    # positive control: both sides present and equal unlocks the comparison
    rows[1]['skill_fp'] = 'fp-123'
    unlocked = report.aggregate(rows, [], header(('baseline', 'v1.1')))
    assert unlocked['skill_evidence']['fingerprint_matched'] == 1
    assert unlocked['deltas']


def test_judge_command_matches_the_runner_contract():
    """R7: the judge argv the runner executes is the pinned judge contract."""
    from pathlib import Path as _Path
    hermes = _Path(__file__).resolve().parents[1] / 'evals' / 'runners' / 'hermes.py'
    spec = importlib.util.spec_from_file_location('lheval_hermes_inv', hermes)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.judge_command(['x'], 'q.md') == judge.judge_command(['x'], 'q.md')
    assert module.judge_command(['x'], 'q.md')[1] == 'chat'
    assert module.JUDGE == judge.JUDGE


def test_probe_gate_requires_the_canary_declarations():
    """R7: a probe record that does not declare all three canaries cannot gate."""
    pins = 'p' * 16
    doc = {'schema': 1, 'mode': 'live', 'clean': True, 'pin_digest': pins,
           'chain_digest': 'c' * 16, 'bwrap_version': 'bubblewrap 0.11.0',
           'created_at': run_evals.now_iso(), 'canaries': ['host', 'sock', 'net']}
    assert run_evals.probe_gate(doc, pins, 'c' * 16, 'bubblewrap 0.11.0') == []
    for broken in (dict(doc, canaries=['host']), dict(doc, canaries=[]),
                   {key: value for key, value in doc.items() if key != 'canaries'}):
        assert any('canar' in item for item in
                   run_evals.probe_gate(broken, pins, 'c' * 16, 'bubblewrap 0.11.0')), \
            'a probe without the three canary declarations gated'
