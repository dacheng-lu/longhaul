"""A stop is a state, a grant is a file: the two optional additions (D1, 2026-11-02).

Both are optional, so a v1.2 record keeps validating exactly as before, and both are
strict when present. The report is what a tick or a panel can count; the mandate is why
the work may exist at all and how far it may go. Neither is inferred from prose.
"""
import json

from test_checkpoint import call, now


def fresh(tmp_path, name='task.json'):
    path = tmp_path / name
    rc, out = call('init', path, '--task-id', 't1', '--goal', 'prove the stop contract')
    assert rc == 0, out
    return path


def doc_of(path):
    return json.loads(path.read_text())


def edit(path, **fields):
    doc = doc_of(path)
    doc.update(fields)
    path.write_text(json.dumps(doc, indent=1))


class TestReport:
    def test_a_report_is_written_and_the_record_still_validates(self, tmp_path):
        path = fresh(tmp_path)
        rc, out = call('report', path, '--state', 'blocked', '--reason', 'no SSH key on the target')
        assert rc == 0, out
        report = doc_of(path)['report']
        assert report['state'] == 'blocked'
        assert report['reason'] == 'no SSH key on the target'
        assert report['since'].endswith('Z') or '+' in report['since']
        rc, out = call('validate', path)
        assert rc == 0, out

    def test_a_sentence_where_the_state_belongs_is_refused(self, tmp_path):
        path = fresh(tmp_path)
        edit(path, report={'state': 'waiting for Bob to answer', 'reason': 'x'})
        rc, out = call('validate', path)
        assert rc == 1
        assert any('a sentence is not a state' in e for e in out['errors']), out

    def test_a_report_needs_a_reason(self, tmp_path):
        path = fresh(tmp_path)
        edit(path, report={'state': 'blocked', 'reason': '   '})
        rc, out = call('validate', path)
        assert rc == 1
        assert any('report.reason' in e for e in out['errors']), out

    def test_complete_may_not_be_filed_over_a_block_or_a_failure(self, tmp_path):
        for state in ('blocked', 'failed'):
            path = fresh(tmp_path, 'task-%s.json' % state)
            edit(path, status='complete', report={'state': state, 'reason': 'still stuck'})
            rc, out = call('validate', path)
            assert rc == 1, (state, out)
            assert any('is not complete' in e for e in out['errors']), out

    def test_dropped_may_end_a_task_that_needed_no_action(self, tmp_path):
        path = fresh(tmp_path)
        edit(path, status='complete', report={'state': 'dropped', 'reason': 'request was superseded'})
        rc, out = call('validate', path)
        assert rc == 0, out

    def test_a_record_without_a_report_is_unchanged(self, tmp_path):
        path = fresh(tmp_path)
        assert 'report' not in doc_of(path)
        rc, out = call('validate', path)
        assert rc == 0, out


class TestMandate:
    def write(self, path, **over):
        args = dict(id='M1', source='github', actor='dacheng-lu', channel='issue#42',
                    strength='explicit', policy_version='p3', scope='repo X, paths docs/**')
        args.update(over)
        return call('mandate', path, '--id', args['id'], '--source', args['source'],
                    '--actor', args['actor'], '--channel', args['channel'],
                    '--strength', args['strength'], '--policy-version', args['policy_version'],
                    '--scope', args['scope'])

    def test_the_mandate_is_a_sibling_file_and_an_immutable_one(self, tmp_path):
        path = fresh(tmp_path)
        rc, out = self.write(path)
        assert rc == 0, out
        mandate = tmp_path / 'task.mandate.json'
        assert mandate.exists()
        assert doc_of(mandate)['policy_version'] == 'p3'
        rc, out = self.write(path, id='M2')
        assert rc == 1, out
        assert any('immutable' in e for e in out['errors']), out
        assert doc_of(mandate)['id'] == 'M1', 'the refused second write changed the mandate'

    def test_a_mandate_without_a_policy_version_makes_the_record_invalid(self, tmp_path):
        path = fresh(tmp_path)
        (tmp_path / 'task.mandate.json').write_text(json.dumps(
            {'schema_version': 1, 'id': 'M1', 'source': 'github', 'actor': 'a',
             'channel': 'c', 'strength': 'explicit', 'scope': 's'}))
        rc, out = call('validate', path)
        assert rc == 1, out
        assert any('mandate.policy_version' in e for e in out['errors']), out

    def test_a_mandate_carrying_a_secret_is_refused_without_echoing_it(self, tmp_path):
        path = fresh(tmp_path)
        rc, out = self.write(path, scope='use token sk-abcdefghijklmnopqrstuvwx')
        assert rc == 1, out
        assert any('secret-shaped' in e for e in out['errors']), out
        assert 'sk-abcdefghijklmnopqrstuvwx' not in json.dumps(out), 'the value was echoed'
        assert not (tmp_path / 'task.mandate.json').exists()

    def test_revalidate_answers_yes_no_and_none(self, tmp_path):
        path = fresh(tmp_path)
        rc, out = call('revalidate', path)
        assert rc == 2, out
        assert out['holds'] is False
        self.write(path)
        rc, out = call('revalidate', path, '--policy-version', 'p3', '--actor', 'dacheng-lu')
        assert (rc, out['holds']) == (0, True), out
        rc, out = call('revalidate', path, '--policy-version', 'p4')
        assert (rc, out['holds']) == (1, False), out
        assert any('p3' in r and 'p4' in r for r in out['reasons']), out
        rc, out = call('revalidate', path, '--actor', 'someone-else')
        assert (rc, out['holds']) == (1, False), out

    def test_a_record_without_a_mandate_is_unchanged(self, tmp_path):
        path = fresh(tmp_path)
        rc, out = call('validate', path)
        assert rc == 0, out
        assert not (tmp_path / 'task.mandate.json').exists()

    def test_the_mandate_does_not_touch_the_state_file(self, tmp_path):
        path = fresh(tmp_path)
        before = path.read_bytes()
        self.write(path)
        assert path.read_bytes() == before, 'writing a mandate rewrote the record'
