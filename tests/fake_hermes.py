#!/usr/bin/env python3
"""Scriptable stand-in for the hermes CLI, used only by tests.

Run as ``[sys.executable, tests/fake_hermes.py, --fake-home <dir>, <argv...>]``
so it works on Windows. The *state* used by the CLI (profiles, .env, sandbox
HOME) is always ``$HOME``; ``--fake-home`` holds this double's config, and the
call log and chat counter live in ``$LONGHAUL_FAKE_LOG`` when the runner sets it
(R8: the attempt owns the log, so a growing log never fills the bound state);
otherwise they fall back to the ``--fake-home`` directory. The subject's HOME is
always a throw-away sandbox.

It never touches the network and never sees a real profile store.

Inputs
------
``<fake-home>/.fake_hermes.json`` -- behaviour. Keys, all optional:
  mode             ok | 429 | timeout | crash | empty | error   (default ok)
  reply            answer text (default "Done.\\nFINAL: complete")
  events           extra stream-json events emitted before the reply
  result           overrides merged into the result event
  tokens           token dict for the result event
  rc               exit code override
  stderr           text written to stderr
  sleep            seconds to sleep before answering
  writes           [{"path": <rel or abs>, "content": <str>}] applied to cwd
  tool_calls       [{"name", "input", "output", "is_error"}]
  skills_list      exact stdout of `skills list`
  skills_list_rc   exit code of `skills list`
  on_skills_list   {"extra": {rel: text}, "env_append": str, "delete": [rel]}
  profile_extra    {rel: text} written when the profile is created
  profile_create_rc / profile_create_stderr
  profile_delete_rc / profile_delete_stderr
  script           [overrides] consumed one entry per chat call
  version          `--version` output
  model            model name echoed in the system event

Output protocol: ``chat`` writes stream-json events only when ``--format
stream-json`` is present; otherwise it writes the plain reply, which is what the
judge path needs.

``<fake-home>/fake_calls.jsonl`` -- one row per invocation: argv, env keys, cwd.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

HOME = Path(os.environ['HOME']) if os.environ.get('HOME') else Path.home()
STATE_HOME = None
CONFIG = None
CALLS = None
COUNTER = None
LOG_HOME = None
DEFAULT_VERSION = 'hermes 0.21.6 (fake)'
DEFAULT_REPLY = 'Done.\nFINAL: complete'
DEFAULT_TOKENS = {'input': 1200, 'output': 80, 'total': 1280, 'cache_read': 0,
                  'cache_write': 0}


def flag(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def configure_paths(argv):
    global STATE_HOME, CONFIG, CALLS, COUNTER, LOG_HOME
    fake_home = flag(argv, '--fake-home')
    STATE_HOME = Path(fake_home) if fake_home else HOME
    # The runner sets LONGHAUL_FAKE_LOG to an attempt-owned, writable directory
    # that is not the bound state. Only the config is read from --fake-home.
    log_home = os.environ.get('LONGHAUL_FAKE_LOG')
    LOG_HOME = Path(log_home) if log_home else STATE_HOME
    CONFIG = STATE_HOME / '.fake_hermes.json'
    CALLS = LOG_HOME / 'fake_calls.jsonl'
    # The chat counter is the double's *input* state (it scripts successive
    # replies), not a log: it stays tiny and lives with the config in
    # --fake-home so a whole run scripts coherently across attempts.
    COUNTER = STATE_HOME / '.fake_hermes_counter.json'


def config():
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_log_home():
    try:
        LOG_HOME.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def log(argv):
    _ensure_log_home()
    try:
        with CALLS.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'argv': list(argv), 'env_keys': sorted(os.environ),
                                     'cwd': os.getcwd()}) + '\n')
    except OSError:
        # A live sandbox keeps its own HOME; a harness that died closes the pipe.
        pass


def chat_index():
    _ensure_log_home()
    try:
        value = int(json.loads(COUNTER.read_text(encoding='utf-8')).get('chat', 0))
    except (OSError, ValueError, TypeError, AttributeError):
        value = 0
    COUNTER.write_text(json.dumps({'chat': value + 1}), encoding='utf-8')
    return value


def write_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def profile_root(name):
    return HOME / '.hermes' / 'profiles' / name


def create_profile(name, cfg):
    if not name:
        print('profile create needs a name', file=sys.stderr)
        return 2
    root = profile_root(name)
    if root.exists():
        print('profile %s already exists' % name, file=sys.stderr)
        return 1
    for sub in ('skills', 'hooks', 'memories', 'sessions'):
        (root / sub).mkdir(parents=True)
    write_file(root / 'config.yaml', 'provider: anthropic\nskills: auto\n')
    write_file(root / 'SOUL.md', 'fake profile\n')
    write_file(root / '.env', 'OLLAMA_API_KEY=fake-placeholder\n')
    os.chmod(root / '.env', 0o600)
    write_file(root / 'skills' / 'hermes-agent' / 'SKILL.md',
               '---\nname: hermes-agent\ndescription: bundled skill\n---\n# bundled\n')
    for rel, content in (cfg.get('profile_extra') or {}).items():
        write_file(root / rel, content)
    rc = int(cfg.get('profile_create_rc') or 0)
    if rc:
        shutil.rmtree(root, ignore_errors=True)
        print(cfg.get('profile_create_stderr') or 'profile create failed', file=sys.stderr)
        return rc
    print('Created profile %s' % name)
    return 0


def delete_profile(name, cfg):
    if not name:
        print('profile delete needs a name', file=sys.stderr)
        return 2
    rc = int(cfg.get('profile_delete_rc') or 0)
    if rc:
        print(cfg.get('profile_delete_stderr') or 'profile delete failed', file=sys.stderr)
        return rc
    root = profile_root(name)
    if not root.exists():
        print('no such profile: %s' % name, file=sys.stderr)
        return 1
    shutil.rmtree(root)
    print('Deleted profile %s' % name)
    return 0


def skills_list(name, cfg):
    root = profile_root(name)
    if not root.is_dir():
        print('no such profile: %s' % name, file=sys.stderr)
        return 1
    hook = cfg.get('on_skills_list') or {}
    for rel, content in (hook.get('extra') or {}).items():
        write_file(root / rel, content)
    if hook.get('env_append'):
        with (root / '.env').open('a', encoding='utf-8') as stream:
            stream.write(hook['env_append'])
    for rel in hook.get('delete') or []:
        target = root / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    rc = int(cfg.get('skills_list_rc') or 0)
    if rc:
        print('skills list failed', file=sys.stderr)
        return rc
    if 'skills_list' in cfg:
        sys.stdout.write(cfg['skills_list'])
        sys.stdout.write('\n')
        return 0
    lines = []
    skills_dir = root / 'skills'
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            skill_md = entry / 'SKILL.md'
            if not skill_md.is_file():
                continue
            label = entry.name
            for line in skill_md.read_text(encoding='utf-8').splitlines():
                if line.lower().startswith('name:'):
                    label = line.split(':', 1)[1].strip()
                    break
            lines.append('- %s: enabled' % label)
    print('\n'.join(lines) if lines else 'no skills')
    return 0


def chat(cfg, stream):
    index = chat_index()
    script = cfg.get('script') or []
    if isinstance(script, list) and index < len(script) and isinstance(script[index], dict):
        merged = dict(cfg)
        merged.update(script[index])
        cfg = merged
    for item in cfg.get('writes') or []:
        target = Path(item['path'])
        if not target.is_absolute():
            target = Path.cwd() / target
        write_file(target, item.get('content', ''))
    if cfg.get('sleep'):
        time.sleep(float(cfg['sleep']))
    mode = cfg.get('mode') or 'ok'
    if mode == 'timeout':
        time.sleep(float(cfg.get('timeout_sleep') or 10))
        mode = 'ok'
    session = 'fake-session'
    reply = cfg.get('reply', DEFAULT_REPLY)
    tokens = cfg.get('tokens') or dict(DEFAULT_TOKENS)

    if not stream:
        # The judge path has no --format stream-json: real plain text.
        if mode == 'crash':
            print('Traceback (most recent call last):\n  fake crash', file=sys.stderr)
            return int(cfg.get('rc') or 3)
        if mode == '429':
            print(cfg.get('stderr') or 'HTTP 429 Too Many Requests', file=sys.stderr)
            return int(cfg.get('rc') or 1)
        if mode == 'error':
            print(cfg.get('stderr') or 'provider failed', file=sys.stderr)
            return int(cfg.get('rc') or 1)
        if mode == 'empty':
            sys.stdout.write('   \n')
            return int(cfg.get('rc') or 0)
        print(reply)
        if cfg.get('stderr'):
            print(cfg['stderr'], file=sys.stderr)
        return int(cfg.get('rc') or 0)

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    if mode == 'crash':
        print('not-json: the fake crashed before emitting a result')
        print('Traceback (most recent call last):\n  fake crash', file=sys.stderr)
        return int(cfg.get('rc') or 3)
    if mode == '429':
        if cfg.get('with_result'):
            emit({'type': 'result', 'session_id': session, 'exit_code': 1, 'text': '',
                  'tokens': tokens, 'duration_ms': 3, 'error': 'HTTP 429 rate limit'})
        print(cfg.get('stderr') or 'provider error: HTTP 429 Too Many Requests',
              file=sys.stderr)
        return int(cfg.get('rc') or 1)

    system = {'type': 'system', 'model': cfg.get('model') or 'fake-model',
              'session_id': session}
    if 'skills' in cfg:
        system['skills'] = [str(name) for name in (cfg.get('skills') or [])]
    emit(system)
    for event in cfg.get('events') or []:
        emit(event)
    for call in cfg.get('tool_calls') or []:
        name = call.get('name', 'terminal')
        emit({'type': 'tool_use', 'name': name, 'tool_call_id': 'call-1',
              'input': call.get('input') or {}})
        emit({'type': 'tool_result', 'name': name, 'output': call.get('output', ''),
              'duration_ms': 4, 'is_error': bool(call.get('is_error'))})
    if mode == 'empty':
        text = '   \n'
        emit({'type': 'text', 'text': text})
        emit({'type': 'result', 'session_id': session, 'exit_code': 0, 'text': text,
              'tokens': tokens, 'duration_ms': 5})
        return int(cfg.get('rc') or 0)
    if mode == 'error':
        emit({'type': 'result', 'session_id': session, 'exit_code': 1, 'text': reply,
              'tokens': tokens, 'duration_ms': 9,
              'error': cfg.get('error') or 'upstream exploded'})
        print(cfg.get('stderr') or 'provider failed', file=sys.stderr)
        return int(cfg.get('rc') or 1)

    emit({'type': 'text', 'text': reply})
    result = {'type': 'result', 'session_id': session, 'exit_code': int(cfg.get('rc') or 0),
              'text': reply, 'tokens': tokens, 'duration_ms': 12}
    result.update(cfg.get('result') or {})
    emit(result)
    if cfg.get('stderr'):
        print(cfg['stderr'], file=sys.stderr)
    return int(cfg.get('rc') or 0)


def isolation_report():
    """Print what the child can actually see; the harness captures it on stderr.

    This is subprocess-level evidence, not argv inspection: only a process that
    really runs inside the sandbox reports the sandbox HOME, the neutral cwd and
    a bwrap PID 1.
    """
    try:
        pid1 = Path('/proc/1/cmdline').read_bytes().replace(b'\0', b' ').decode(
            'utf-8', 'replace')[:300]
    except OSError:
        pid1 = ''
    try:
        sys.stderr.write('ISOLATION home=%s cwd=%s argv0=%s pid1=%s\n'
                         % (HOME, os.getcwd(), sys.argv[0], pid1))
        sys.stderr.flush()
    except (OSError, ValueError):
        # The harness died: a closed stderr pipe must not kill the subject.
        pass


def probe_environment(cfg):
    """Report what the child can really reach; the harness captures stderr.

    ``visibility``: absolute paths whose existence is reported. ``connect``:
    [host, port] tried with a one-second timeout. Only a process that really
    runs inside the confinement reports 0/failed.
    """
    for path in cfg.get('visibility') or []:
        try:
            exists = os.path.exists(path)
        except OSError:
            exists = False
        sys.stderr.write('VISIBLE %s %d\n' % (path, 1 if exists else 0))
    target = cfg.get('connect')
    if isinstance(target, (list, tuple)) and len(target) == 2:
        import socket
        try:
            handle = socket.create_connection((str(target[0]), int(target[1])), timeout=1)
            handle.close()
            reached = 1
        except OSError:
            reached = 0
        sys.stderr.write('CONNECT %s:%s %d\n' % (target[0], target[1], reached))
    sys.stderr.flush()


def main(argv):
    configure_paths(argv)
    isolation_report()
    cfg = config()
    probe_environment(cfg)
    log(argv)
    if '--version' in argv:
        print(cfg.get('version', DEFAULT_VERSION))
        return 0
    if 'profile' in argv:
        index = argv.index('profile')
        action = argv[index + 1] if len(argv) > index + 1 else ''
        name = argv[index + 2] if len(argv) > index + 2 else ''
        if action == 'create':
            return create_profile(name, cfg)
        if action == 'delete':
            return delete_profile(name, cfg)
        print('fake: unsupported profile action %r' % action, file=sys.stderr)
        return 2
    if 'skills' in argv and 'list' in argv:
        return skills_list(flag(argv, '-p'), cfg)
    if 'chat' in argv:
        stream = flag(argv, '--format') == 'stream-json'
        return chat(cfg, stream)
    print('fake: unsupported argv %r' % (argv,), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
