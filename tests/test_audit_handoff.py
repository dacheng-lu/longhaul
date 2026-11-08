import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('audit_handoff', Path(__file__).resolve().parents[1] / 'scripts/check_audit_handoff.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def bundle(root):
    records = {'version': 1, 'scope': {'head': 'b' * 40}, 'coverage': [{'status': 'checked'}], 'findings': []}
    (root / 'validated.json').write_text(json.dumps(records))
    (root / 'report.md').write_text('record checks only')
    data = {'version': 1, 'head': 'b' * 40, 'artifacts': {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in ('validated.json', 'report.md')}, 'unresolved': [], 'open_coverage': [], 'limitations': ['Does not authenticate reviewers or establish vulnerability truth.']}
    (root / 'handoff.json').write_text(json.dumps(data))
    return data


def test_valid(tmp_path):
    bundle(tmp_path)
    module.check(tmp_path, 'b' * 40)
    assert not (tmp_path / 'task.json').exists()


@pytest.mark.parametrize('case', ['head', 'artifact', 'unresolved', 'coverage', 'hidden-unresolved', 'version', 'limitations'])
def test_refuse(tmp_path, case):
    data = bundle(tmp_path)
    if case == 'head':
        data['head'] = 'c' * 40
    elif case == 'artifact':
        (tmp_path / 'report.md').write_text('changed')
    elif case == 'unresolved':
        data['unresolved'] = ['F1']
    elif case == 'coverage':
        data['open_coverage'] = ['boundary']
    elif case == 'version':
        data['version'] = 2
    elif case == 'limitations':
        data['limitations'] = []
    else:
        path = tmp_path / 'validated.json'
        records = json.loads(path.read_text())
        records['findings'] = [{'verdict': 'needs_validation'}]
        path.write_text(json.dumps(records))
        data['artifacts']['validated.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / 'handoff.json').write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.check(tmp_path, 'b' * 40)
