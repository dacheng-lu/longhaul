#!/usr/bin/env python3
"""Validate Longhaul bookkeeping; never claim to verify substantive truth."""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inspect(state, base, gate=False):
    errors = []
    if not isinstance(state, dict):
        return ['state must be an object']
    if state.get('schema_version') != 1:
        errors.append('schema_version must be 1')
    for key in ('task_id', 'goal', 'artifact_revision', 'next_action'):
        if not nonempty(state.get(key)):
            errors.append(key + ' must be nonempty')
    if state.get('status') not in ('in_progress', 'blocked', 'complete'):
        errors.append('invalid status')
    arrays = ('constraints', 'milestones', 'acceptance', 'deliverables', 'evidence',
              'blockers', 'attempts', 'decisions', 'capabilities', 'lessons')
    for key in arrays:
        if not isinstance(state.get(key), list):
            errors.append(key + ' must be an array')
    if errors:
        return errors

    def location(item, label):
        kind = item.get('kind')
        if kind not in ('local', 'external') or not nonempty(item.get('locator')):
            errors.append(label + ': invalid kind/locator')
            return
        if kind == 'local':
            expected = item.get('sha256', '')
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                errors.append(label + ': sha256 required')
            elif gate:
                path = Path(item['locator'])
                path = path if path.is_absolute() else base / path
                try:
                    if not path.is_file() or digest(path) != expected:
                        errors.append(label + ': file missing or hash mismatch')
                except OSError as exc:
                    errors.append(label + ': unreadable file: ' + str(exc))

    evidence = {}
    for item in state['evidence']:
        if not isinstance(item, dict) or not nonempty(item.get('id')):
            errors.append('evidence must be an object with id')
            continue
        label = 'evidence ' + item['id']
        if item['id'] in evidence:
            errors.append(label + ': duplicate id')
        evidence[item['id']] = item
        location(item, label)
        if item.get('result') not in ('pass', 'fail'):
            errors.append(label + ': invalid result')
        if not nonempty(item.get('artifact_revision')):
            errors.append(label + ': revision required')
        try:
            stamp = datetime.fromisoformat(item.get('observed_at', '').replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('timezone required')
        except (AttributeError, TypeError, ValueError):
            errors.append(label + ': timezone-aware observed_at required')
        if item.get('kind') == 'external' and not nonempty(item.get('summary')):
            errors.append(label + ': external observation summary required')

    seen = set()
    for item in state['acceptance']:
        if not isinstance(item, dict) or not nonempty(item.get('id')):
            errors.append('acceptance must be an object with id')
            continue
        label = 'criterion ' + item['id']
        if item['id'] in seen:
            errors.append(label + ': duplicate id')
        seen.add(item['id'])
        if not nonempty(item.get('criterion')):
            errors.append(label + ': criterion text required')
        if item.get('status') not in ('pending', 'verified', 'failed'):
            errors.append(label + ': invalid status')
        refs = item.get('evidence_ids')
        if not isinstance(refs, list) or any(not nonempty(ref) for ref in refs):
            errors.append(label + ': evidence_ids must be strings')
            continue
        for ref in refs:
            if ref not in evidence:
                errors.append(label + ': unknown evidence ' + ref)
        if gate:
            if item.get('status') != 'verified' or not refs:
                errors.append(label + ': not verified with evidence')
            for ref in refs:
                ev = evidence.get(ref, {})
                if ev.get('result') != 'pass' or ev.get('artifact_revision') != state['artifact_revision']:
                    errors.append(label + ': failed or stale evidence ' + ref)

    for item in state['deliverables']:
        if not isinstance(item, dict):
            errors.append('deliverable must be an object')
        else:
            location(item, 'deliverable')
    if gate:
        if not state['acceptance'] or not state['deliverables']:
            errors.append('acceptance and deliverables must be nonempty')
        if state['blockers'] or state['status'] == 'blocked':
            errors.append('unresolved blockers')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'validate', 'gate'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--task-id')
    parser.add_argument('--goal')
    args = parser.parse_args()
    try:
        if args.command == 'init':
            if not nonempty(args.task_id) or not nonempty(args.goal):
                parser.error('init requires --task-id and --goal')
            state = dict(schema_version=1, task_id=args.task_id, goal=args.goal,
                         status='in_progress', artifact_revision='initial',
                         next_action='Define acceptance criteria and inspect inputs')
            for key in ('constraints', 'milestones', 'acceptance', 'deliverables', 'evidence',
                        'blockers', 'attempts', 'decisions', 'capabilities', 'lessons'):
                state[key] = []
            args.path.parent.mkdir(parents=True, exist_ok=True)
            with args.path.open('x', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            print('Created template; no completion or durable-storage claim.')
            return 0
        state = json.loads(args.path.read_text(encoding='utf-8'))
        errors = inspect(state, args.path.resolve().parent, args.command == 'gate')
        print(json.dumps({'ok': not errors, 'errors': errors,
                          'scope': 'bookkeeping/local hashes only; verify substantive and external claims separately'},
                         ensure_ascii=False))
        return 1 if errors else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
