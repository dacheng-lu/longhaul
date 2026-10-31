#!/usr/bin/env python3
"""Validate Longhaul bookkeeping; never claim to verify substantive truth.

Schema v2. Evidence is bound to artifacts by content-addressed revision, so a
changed file invalidates exactly the criteria that depend on it. Nothing here
can judge whether the work is correct -- only whether the record is coherent.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_scan

SCHEMA = 2
STATUS = ('in_progress', 'blocked', 'complete')
CRITERION = ('pending', 'verified', 'failed', 'unobservable')
RESULTS = ('pass', 'fail', 'unknown')
KINDS = ('local', 'external')
CLASSES = ('callable', 'installed_unavailable', 'discovered', 'unavailable')
LISTS = ('constraints', 'milestones', 'acceptance', 'artifacts', 'evidence',
         'blockers', 'attempts', 'decisions', 'capabilities', 'lessons',
         'shortcuts', 'waivers')
SKEW = timedelta(minutes=5)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def now():
    return datetime.now(timezone.utc)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_ts(value, label, errors):
    """Parse an ISO-8601 timestamp with an offset; reject naive and future stamps."""
    if not nonempty(value):
        errors.append(label + ': timestamp required')
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        errors.append(label + ': not ISO-8601')
        return None
    if stamp.tzinfo is None:
        errors.append(label + ': timezone required')
        return None
    if stamp > now() + SKEW:
        errors.append(label + ': timestamp is in the future')
        return None
    return stamp


def resolve(base, locator):
    path = Path(locator)
    return path if path.is_absolute() else base / path


# --------------------------------------------------------------------------- #
# schema upgrade
# --------------------------------------------------------------------------- #

def upgrade_v1(state, base):
    """Translate a v1 record to v2. Re-binds evidence only while the content hash
    still matches, so migration cannot launder evidence for changed files."""
    warnings = []
    old_rev = state.get('artifact_revision')
    artifacts, by_locator = [], {}
    for index, item in enumerate(state.get('deliverables') or []):
        if not isinstance(item, dict):
            continue
        art = {'id': 'A%d' % (index + 1), 'kind': item.get('kind', 'local'),
               'locator': item.get('locator', '')}
        if art['kind'] == 'local':
            art['sha256'] = item.get('sha256', '')
            art['revision'] = (art['sha256'] or '')[:12]
        else:
            art['revision'] = old_rev or 'unversioned'
        artifacts.append(art)
        by_locator.setdefault(str(art['locator']), []).append(art)

    evidence = []
    for item in state.get('evidence') or []:
        if not isinstance(item, dict):
            continue
        ev = dict(item)
        locator = str(ev.get('locator', ''))
        hits = by_locator.get(locator, [])
        if not hits:
            for art in artifacts:
                if resolve(base, art['locator']) == resolve(base, locator):
                    hits = [art]
                    break
        ev['artifact_ids'] = [art['id'] for art in hits]
        if hits and hits[0]['kind'] == 'local':
            art = hits[0]
            on_disk_ok = False
            try:
                path = resolve(base, art['locator'])
                on_disk_ok = path.is_file() and sha256_file(path) == art['sha256']
            except OSError:
                on_disk_ok = False
            if on_disk_ok and ev.get('artifact_revision') == old_rev:
                ev['artifact_revisions'] = {art['id']: art['revision']}
            else:
                ev['artifact_revisions'] = {art['id']: str(ev.get('artifact_revision', ''))}
                warnings.append('evidence %s: content changed since v1 or revision was bumped; '
                                're-verify instead of trusting the migrated record' % ev.get('id'))
        elif hits:
            ev['artifact_revisions'] = {
                hits[0]['id']: str(ev.get('artifact_revision') or hits[0]['revision'])}
        else:
            ev['artifact_revisions'] = {}
            warnings.append('evidence %s: no deliverable matches its locator; bind it to an '
                            'artifact before gate' % ev.get('id'))
        ev.pop('artifact_revision', None)
        ev.pop('sha256', None)
        evidence.append(ev)

    if any(isinstance(a, dict) and 'blocker_key' not in a for a in state.get('attempts') or []):
        warnings.append('attempts[]: no blocker_key; the retry budget is not enforced until '
                        'attempts are rewritten in v2 form')

    new = {key: state[key] for key in LISTS if key in state}
    new.update(schema_version=SCHEMA, task_id=state.get('task_id'), goal=state.get('goal'),
               status=state.get('status'), next_action=state.get('next_action'),
               artifacts=artifacts, evidence=evidence,
               shortcuts=state.get('shortcuts') or [], waivers=state.get('waivers') or [],
               provenance={'generated_at': iso(now()), 'writer': 'checkpoint.py migrate',
                           'derived_from': None})
    if state.get('evaluation_date'):
        new['evaluation_date'] = state['evaluation_date']
    for key in LISTS:
        new.setdefault(key, [])
    return new, warnings


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def check_top(state, errors):
    if not isinstance(state, dict):
        errors.append('state must be an object')
        return False
    if state.get('schema_version') != SCHEMA:
        errors.append('schema_version must be %d' % SCHEMA)
    for key in ('task_id', 'goal', 'next_action'):
        if not nonempty(state.get(key)):
            errors.append(key + ' must be nonempty')
    if state.get('status') not in STATUS:
        errors.append('invalid status')
    for key in LISTS:
        if not isinstance(state.get(key), list):
            errors.append(key + ' must be an array')
    provenance = state.get('provenance')
    if not isinstance(provenance, dict):
        errors.append('provenance must be an object')
    else:
        parse_ts(provenance.get('generated_at'), 'provenance.generated_at', errors)
        if not nonempty(provenance.get('writer')):
            errors.append('provenance.writer must be nonempty')
    return not errors


def check_secrets(state, errors):
    for path, kind in secret_scan.scan_state(state):
        errors.append('secret-shaped value (%s) at %s; reference the location, not the value'
                      % (kind, path))


def check_next_action(state, errors, warnings):
    action = state.get('next_action')
    if not nonempty(action):
        return
    lowered = action.lower()
    if re.search(r'\bthen\b|\bafter that\b', lowered) or ';' in action \
            or re.search(r'^\s*\d+[.)]\s', action) or '\n' in action.strip():
        errors.append('next_action must be one step; move the rest into milestones')
    for marker in ('然后', '接着', 'dann '):
        if marker in action:
            warnings.append('next_action may bundle steps (%r); judged by a human, not blocked'
                            % marker.strip())


def check_artifacts(state, base, errors, gate):
    seen = set()
    for art in state['artifacts']:
        if not isinstance(art, dict) or not nonempty(art.get('id')):
            errors.append('artifact must be an object with id')
            continue
        label = 'artifact ' + art['id']
        if art['id'] in seen:
            errors.append(label + ': duplicate id')
        seen.add(art['id'])
        if art.get('kind') not in KINDS:
            errors.append(label + ': invalid kind')
            continue
        if not nonempty(art.get('locator')):
            errors.append(label + ': locator required')
            continue
        if not nonempty(art.get('revision')):
            errors.append(label + ': revision required')
        if art['kind'] == 'external':
            continue
        expected = art.get('sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            errors.append(label + ': sha256 required for local artifacts')
            continue
        if art.get('revision') != expected[:12]:
            errors.append(label + ': revision must be the first 12 hex of sha256')
        if gate:
            try:
                path = resolve(base, art['locator'])
                if not path.is_file() or sha256_file(path) != expected:
                    errors.append(label + ': file missing or hash mismatch')
            except OSError as exc:
                errors.append(label + ': unreadable file: %s' % exc)


def check_evidence(state, base, errors, gate):
    arts = {a['id']: a for a in state['artifacts'] if isinstance(a, dict) and 'id' in a}
    seen = set()
    for ev in state['evidence']:
        if not isinstance(ev, dict) or not nonempty(ev.get('id')):
            errors.append('evidence must be an object with id')
            continue
        label = 'evidence ' + ev['id']
        if ev['id'] in seen:
            errors.append(label + ': duplicate id')
        seen.add(ev['id'])
        if ev.get('kind') not in KINDS:
            errors.append(label + ': invalid kind')
        if not nonempty(ev.get('locator')):
            errors.append(label + ': locator required')
        ids = ev.get('artifact_ids')
        if not isinstance(ids, list) or not ids or any(not nonempty(i) for i in ids):
            errors.append(label + ': artifact_ids must be a nonempty list of ids')
            ids = []
        for aid in ids:
            if aid not in arts:
                errors.append(label + ': unknown artifact ' + aid)
        revisions = ev.get('artifact_revisions')
        if not isinstance(revisions, dict) or set(revisions) != set(ids):
            errors.append(label + ': artifact_revisions must map exactly the bound artifacts')
            revisions = {}
        parse_ts(ev.get('observed_at'), label + ': observed_at', errors)
        if ev.get('result') not in RESULTS:
            errors.append(label + ': invalid result')
        if ev.get('result') == 'unknown' and not nonempty(ev.get('unknown_reason')):
            errors.append(label + ': unknown_reason required when result is unknown')
        if ev.get('kind') == 'external' and not nonempty(ev.get('summary')):
            errors.append(label + ': external observation needs a summary')
        if gate:
            for aid in ids:
                art = arts.get(aid)
                if art and revisions.get(aid) != art.get('revision'):
                    errors.append(label + ': stale, artifact %s changed since observation' % aid)


def check_acceptance(state, errors, gate):
    evidence = {e['id']: e for e in state['evidence'] if isinstance(e, dict) and 'id' in e}
    waivers = {w.get('criterion_id') for w in state['waivers'] if isinstance(w, dict)}
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
        if item.get('status') not in CRITERION:
            errors.append(label + ': invalid status')
        ids = item.get('evidence_ids')
        if not isinstance(ids, list) or any(not nonempty(i) for i in ids):
            errors.append(label + ': evidence_ids must be a list of strings')
            ids = []
        for ref in ids:
            if ref not in evidence:
                errors.append(label + ': unknown evidence ' + ref)
        if not gate:
            continue
        if item.get('status') == 'unobservable':
            if item['id'] not in waivers:
                errors.append(label + ': unobservable needs a recorded waiver')
        elif item.get('status') != 'verified':
            errors.append(label + ': not verified')
        elif not ids:
            errors.append(label + ': verified without evidence')
        else:
            for ref in ids:
                ev = evidence.get(ref, {})
                if ev.get('result') != 'pass':
                    errors.append(label + ': failed or unknown evidence ' + ref)


def check_waivers(state, errors):
    for item in state['waivers']:
        if not isinstance(item, dict) or not nonempty(item.get('criterion_id')):
            errors.append('waiver must be an object with criterion_id')
            continue
        label = 'waiver %s' % item['criterion_id']
        if not nonempty(item.get('reason')):
            errors.append(label + ': reason required')
        if not nonempty(item.get('approved_by')):
            errors.append(label + ': approved_by required; silence is not consent')
        parse_ts(item.get('approved_at'), label + ': approved_at', errors)


def check_attempts(state, errors, warnings):
    groups = {}
    for item in state['attempts']:
        if not isinstance(item, dict):
            errors.append('attempt must be an object')
            continue
        if not nonempty(item.get('blocker_key')):
            warnings.append('attempt without blocker_key; the retry budget is not enforced for it')
            continue
        if 'at' in item:
            parse_ts(item.get('at'), 'attempt %s: at' % item['blocker_key'], errors)
        groups.setdefault(item['blocker_key'], []).append(item)
    for key, items in groups.items():
        label = 'attempts[%s]' % key
        for first, second in zip(items, items[1:]):
            if first.get('action') == second.get('action') \
                    and first.get('error') == second.get('error'):
                errors.append(label + ': unchanged retry (same action and error); change the '
                                    'hypothesis or check whether the effect already landed')
                break
        if len(items) >= 3:
            changed = any(nonempty(i.get('hypothesis_after'))
                          and i.get('hypothesis_after') != i.get('hypothesis_before')
                          for i in items[:3])
            if not changed:
                errors.append(label + ': 3 attempts without a hypothesis change; escalate or '
                                    'switch approach instead of retrying')


def check_shortcuts(state, errors):
    for item in state['shortcuts']:
        if not isinstance(item, dict) or not nonempty(item.get('what')):
            errors.append('shortcut must be an object with what')
            continue
        label = 'shortcut %r' % item['what'][:40]
        if not nonempty(item.get('ceiling')):
            errors.append(label + ': ceiling required (what limit was accepted)')
        if not nonempty(item.get('upgrade_trigger')):
            errors.append(label + ': no-trigger; state when this shortcut must be revisited')
        if 'at' in item:
            parse_ts(item.get('at'), label + ': at', errors)


def check_capabilities(state, errors, warnings):
    for item in state['capabilities']:
        if isinstance(item, str):
            warnings.append('capability %r is unstructured; record class, source and last_success'
                            % item[:40])
            continue
        if not isinstance(item, dict) or not nonempty(item.get('name')):
            errors.append('capability must be an object with name')
            continue
        if 'class' not in item:
            warnings.append('capability %s has no class; cannot distinguish discovered from '
                            'authorized' % item['name'])
            continue
        if item['class'] not in CLASSES:
            errors.append('capability %s: invalid class' % item['name'])
        if item['class'] == 'callable' and not item.get('untested'):
            if not nonempty(item.get('last_success')):
                errors.append('capability %s: callable needs last_success or untested: true'
                              % item['name'])


def check_coverage(state, errors):
    if not state['acceptance']:
        errors.append('acceptance must be nonempty before delivery')
    if not state['artifacts']:
        errors.append('artifacts must be nonempty before delivery')
    bound = set()
    for ev in state['evidence']:
        if isinstance(ev, dict):
            bound.update(ev.get('artifact_ids') or [])
    for art in state['artifacts']:
        if isinstance(art, dict) and art.get('id') not in bound and not art.get('supporting'):
            errors.append('artifact %s: no evidence binds it; mark supporting: true if it is '
                          'input rather than output' % art.get('id'))


def check_blockers(state, errors, gate):
    if not gate:
        return
    if state['blockers']:
        errors.append('unresolved blockers')
    if state.get('status') == 'blocked':
        errors.append('status is blocked')


def inspect(state, base, gate=False):
    errors, warnings = [], []
    if not check_top(state, errors):
        return errors, warnings
    check_secrets(state, errors)
    check_next_action(state, errors, warnings)
    check_artifacts(state, base, errors, gate)
    check_evidence(state, base, errors, gate)
    check_acceptance(state, errors, gate)
    check_waivers(state, errors)
    check_attempts(state, errors, warnings)
    check_shortcuts(state, errors)
    check_capabilities(state, errors, warnings)
    if gate:
        check_coverage(state, errors)
    check_blockers(state, errors, gate)
    return errors, warnings


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def new_state(task_id, goal):
    state = {'schema_version': SCHEMA, 'task_id': task_id, 'goal': goal,
             'status': 'in_progress',
             'next_action': 'Bind artifacts and define acceptance criteria',
             'provenance': {'generated_at': iso(now()), 'writer': 'checkpoint.py init',
                            'derived_from': None}}
    for key in LISTS:
        state[key] = []
    return state


def write_state(path, state):
    target = Path(path)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(target)


def cmd_init(args):
    if not nonempty(args.task_id) or not nonempty(args.goal):
        raise SystemExit('init requires --task-id and --goal')
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(new_state(args.task_id, args.goal), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print('Created %s (schema v%d). No completion or durable-storage claim.' % (path, SCHEMA))
    return 0


def cmd_hash(args):
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit('%s: not a file' % path)
    digest = sha256_file(path)
    print(json.dumps({'path': str(path), 'sha256': digest, 'revision': digest[:12]}))
    return 0


def cmd_artifact(args):
    state = load(args.path)
    base = Path(args.path).resolve().parent
    kind = args.kind or 'local'
    locator = args.locator
    if kind == 'local':
        target = resolve(base, locator)
        if not target.is_file():
            # Be forgiving about the caller's working directory, but store an
            # unambiguous absolute path when the state-relative form does not exist.
            alternative = Path(locator)
            if alternative.is_file():
                target = alternative.resolve()
                locator = str(target)
            else:
                raise SystemExit('%s: no such file (looked at %s, relative to the state file '
                                 'directory)' % (locator, target.resolve()))
        digest = sha256_file(target)
        revision = digest[:12]
    else:
        if not nonempty(args.revision):
            raise SystemExit('external artifacts need --revision (a label you can point at)')
        digest, revision = None, args.revision
    entry = next((a for a in state['artifacts'] if a.get('id') == args.id), None)
    if entry is None:
        entry = {'id': args.id}
        state['artifacts'].append(entry)
    entry.update(kind=kind, locator=locator, revision=revision)
    if digest:
        entry['sha256'] = digest
    if args.supporting:
        entry['supporting'] = True
    write_state(args.path, state)
    print(json.dumps({'artifact': entry,
                      'note': 'revision is content-derived; evidence bound to the previous '
                              'revision is now stale'}))
    return 0


def cmd_evidence(args):
    state = load(args.path)
    ids = args.artifact or []
    arts = {a['id']: a for a in state['artifacts'] if isinstance(a, dict)}
    missing = [i for i in ids if i not in arts]
    if missing:
        raise SystemExit('unknown artifact(s): %s' % ', '.join(missing))
    kinds = {arts[i].get('kind') for i in ids}
    if len(kinds) != 1:
        raise SystemExit('bound artifacts must share one kind')
    entry = {'id': args.id, 'kind': kinds.pop(), 'locator': arts[ids[0]].get('locator'),
             'artifact_ids': ids,
             'artifact_revisions': {i: arts[i].get('revision') for i in ids},
             'observed_at': args.observed_at or iso(now()), 'result': args.result}
    if args.summary:
        entry['summary'] = args.summary
    if args.result == 'unknown':
        if not nonempty(args.unknown_reason):
            raise SystemExit('--result unknown needs --unknown-reason')
        entry['unknown_reason'] = args.unknown_reason
    state['evidence'] = [e for e in state['evidence'] if e.get('id') != args.id] + [entry]
    write_state(args.path, state)
    print(json.dumps({'evidence': entry}))
    return 0


def cmd_migrate(args):
    state = load(args.path)
    if state.get('schema_version') == SCHEMA:
        print(json.dumps({'ok': True, 'note': 'already schema v%d' % SCHEMA}))
        return 0
    base = Path(args.path).resolve().parent
    new, warnings = upgrade_v1(state, base)
    if args.write:
        write_state(args.path, new)
        print(json.dumps({'ok': True, 'wrote': str(args.path), 'warnings': warnings,
                          'next': 'run gate; unbound or re-bound evidence must be re-verified'}))
    else:
        print(json.dumps({'ok': True, 'warnings': warnings, 'migrated': new},
                         ensure_ascii=False))
    return 0


def cmd_check(args, gate):
    raw = load(args.path)
    base = Path(args.path).resolve().parent
    warnings = []
    if raw.get('schema_version') != SCHEMA:
        if raw.get('schema_version') == 1:
            raw, warnings = upgrade_v1(raw, base)
            warnings.insert(0, 'schema v1 read as v2 in memory; run migrate to persist')
        else:
            print(json.dumps({'ok': False, 'errors': ['unsupported schema_version']}))
            return 1
    errors, more = inspect(raw, base, gate)
    print(json.dumps({'ok': not errors, 'errors': errors, 'warnings': warnings + more,
                      'scope': 'bookkeeping and local hashes only; verify substantive and '
                               'external claims separately'}, ensure_ascii=False))
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('init', help='create a v2 template (refuses to overwrite)')
    p.add_argument('path')
    p.add_argument('--task-id', required=True)
    p.add_argument('--goal', required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('validate', help='structure only')
    p.add_argument('path')
    p.set_defaults(func=lambda a: cmd_check(a, False))

    p = sub.add_parser('gate', help='pre-delivery: every criterion verified or waived')
    p.add_argument('path')
    p.set_defaults(func=lambda a: cmd_check(a, True))

    p = sub.add_parser('hash', help='print sha256 and the 12-hex revision')
    p.add_argument('path')
    p.set_defaults(func=cmd_hash)

    p = sub.add_parser('artifact', help='register or refresh an artifact')
    p.add_argument('path')
    p.add_argument('--id', required=True)
    p.add_argument('--locator', required=True)
    p.add_argument('--kind', choices=KINDS)
    p.add_argument('--revision')
    p.add_argument('--supporting', action='store_true')
    p.set_defaults(func=cmd_artifact)

    p = sub.add_parser('evidence', help='append evidence with current revisions')
    p.add_argument('path')
    p.add_argument('--id', required=True)
    p.add_argument('--artifact', action='append', required=True)
    p.add_argument('--result', choices=RESULTS, required=True)
    p.add_argument('--summary')
    p.add_argument('--unknown-reason')
    p.add_argument('--observed-at')
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser('migrate', help='v1 -> v2')
    p.add_argument('path')
    p.add_argument('--write', action='store_true')
    p.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    try:
        return args.func(args)
    except SystemExit as exc:
        print(json.dumps({'ok': False, 'error': str(exc) or 'aborted'}))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
