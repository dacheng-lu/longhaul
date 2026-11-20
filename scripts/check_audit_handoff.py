#!/usr/bin/env python3
"""Check an explicitly prepared audit handoff. Does not grant task completion."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def check(root, expected_head):
    def need(ok, message):
        if not ok:
            raise ValueError(message)
    data = json.loads((root / 'handoff.json').read_text())
    need(set(data) == {'version', 'head', 'artifacts', 'unresolved', 'open_coverage', 'limitations'}, 'bad handoff fields')
    need(type(data['version']) is int and data['version'] == 1, 'unsupported handoff version')
    need(re.fullmatch(r'[0-9a-f]{40}', expected_head) and data['head'] == expected_head, 'stale source head')
    need(isinstance(data['limitations'], list) and data['limitations'] and all(isinstance(x, str) and x.strip() for x in data['limitations']), 'limitations required')
    need(isinstance(data['unresolved'], list) and isinstance(data['open_coverage'], list), 'bad unresolved lists')
    need(not data['unresolved'] and not data['open_coverage'], 'audit remains incomplete')
    need(isinstance(data['artifacts'], dict) and set(data['artifacts']) == {'validated.json', 'report.md'}, 'missing audit artifacts')
    for name, sha in data['artifacts'].items():
        path = (root / name).resolve()
        need(path.is_relative_to(root.resolve()) and path.is_file(), 'missing/escaped artifact')
        need(hashlib.sha256(path.read_bytes()).hexdigest() == sha, 'stale artifact')
    records = json.loads((root / 'validated.json').read_text())
    need(records['version'] == 1 and records['scope']['head'] == expected_head, 'snapshot scope mismatch')
    need(isinstance(records['coverage'], list) and records['coverage'] and all(u['status'] == 'checked' for u in records['coverage']), 'open coverage in snapshot')
    need(isinstance(records['findings'], list) and all(f['verdict'] in ('confirmed', 'rejected') for f in records['findings']), 'unresolved finding in snapshot')
    return data


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundle', type=Path)
    p.add_argument('--head', required=True)
    a = p.parse_args()
    try:
        check(a.bundle, a.head)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.exit(1, f'handoff refused: {exc}\n')
    print('Handoff hashes and completeness checked; no semantic acceptance recorded.')
