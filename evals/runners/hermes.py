#!/usr/bin/env python3
"""L1 subject runner: one fail-closed sandbox around every Hermes process.

There are exactly two explicit modes. ``MODE_LIVE_BLOCKED`` is refused by
``run_one`` itself, before a profile, a key or a process: the provider
credential cannot be proven unreachable from an untrusted subject, so no runner
API reads, copies, inherits or accepts key material at all. ``MODE_OFFLINE``
accepts only the byte-identical verified test double (direct entry or
``python <verified-double>``), binds one key-free state directory and runs every
Hermes call inside the same bwrap plan with ``--unshare-net``.

The launcher chain is resolved on the host (which/realpath/shebang/``exec``) and
only the individual home prefixes it actually needs are mounted read-only inside
the sandbox; the real HOME, /tmp, /var/tmp and /run/user are tmpfs, and only the
attempt's own ``execdir`` and the state directory are bound back after that. A
missing bwrap, an unresolvable chain, a home target without an allowed root or a
failed in-sandbox ``--version`` probe is refused before a profile exists. When
``$HOME`` does not name the invoking user's passwd home, both are masked:
overriding ``$HOME`` must not leave the real home readable through the root bind.

Deterministic tests pass ``bwrap=False`` explicitly; nothing here is inferred
from a file name.
"""
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# shared constants (contract section 1 -- copied verbatim into every lane module)
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
FINAL_CONTRACT = ('When you stop, end your reply with exactly one line: '
                  'FINAL: complete, FINAL: incomplete, or FINAL: blocked.')
ALLOWED_ENV = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TERM', 'XDG_RUNTIME_DIR', 'TMPDIR')
PROFILE_RE = r'^lheval[a-z0-9]+$'
SUBJECT = ('deepseek-v4.1-flash:1017', 'ollama-cloud')
JUDGE = ('claude-sonnet-5-5', 'anthropic')

STATE_REL = '.task/state.json'
KEEP_PROFILES = False
BWRAP = '/usr/bin/bwrap'
SUBJECT_ROOT = Path('/var/tmp/lheval-subject')
HARNESS_ENV = ('PYTHONDONTWRITEBYTECODE',)
CACHE_DIRS = ('__pycache__', '.pytest_cache')
CACHE_SUFFIXES = ('.pyc',)
SKILL_NAME = 'longhaul'
PROFILE_NEUTRAL_RE = re.compile(r'lheval[0-9a-f]{16}')
IDENTITY_RE = re.compile(r'longhaul|\u8fdc\u822a', re.IGNORECASE)
CONDITION_RE = re.compile(r'baseline|v1\.1|(?<![A-Za-z0-9])v1(?![0-9])', re.IGNORECASE)
CASE_RE = re.compile('|'.join(re.escape(case) for case in CASE_IDS), re.IGNORECASE)
SHELLS = ('sh', 'bash', 'dash', 'ksh', 'zsh')
MAX_CHAIN_NODES = 64

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- #
# explicit execution modes (R6: no boolean live/key_material combination)
# --------------------------------------------------------------------------- #
# There is exactly one mode that may execute and one that may not. A live run is
# refused by ``run_one`` itself, before a profile, a key or a process, so a
# direct Python call is as fail-closed as the CLI.
MODE_OFFLINE = 'offline_verified_double'
MODE_LIVE_BLOCKED = 'live_blocked'
MODES = (MODE_OFFLINE, MODE_LIVE_BLOCKED)


class LiveBlocked(RuntimeError):
    """A live subject run is refused: the credential boundary is unsolved."""


class DoubleRejected(ValueError):
    """The offline entry is not the verified, canonical test double."""


# --------------------------------------------------------------------------- #
# conditions and run specification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cond:
    """One eval condition. ``slots`` is a scheduling hint; it never scores."""

    slug: str
    commit: str | None = None
    version: str | None = None
    slots: int = 1


CONDITIONS = {
    'baseline': Cond('base', None, None, 2),
    'v1': Cond('v1', 'b8b7d8c', '1.0', 1),
    'v1.1': Cond('v11', 'af52cd8', '1.1', 2),
}


@dataclass
class RunSpec:
    """Everything one subject run needs, except the binary and the host home."""

    run_id: str
    cond: str
    case: str
    trial: int
    prompt: str
    rundir: Path
    execdir: Path = None
    model: str = SUBJECT[0]
    provider: str = SUBJECT[1]
    max_turns: int = 60
    budget_s: int = 600


def profile_name(run_id, cond, case, trial):
    """``lheval<16 hex>``; no condition slug, no case id (P11)."""
    CONDITIONS[cond]
    raw = '%s|%s|%s|%s' % (run_id, cond, case, trial)
    return 'lheval' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def neutral_execdir(key, attempt, root=None):
    """A path derived from nothing the subject could learn (run key, attempt, random)."""
    base = Path(root) if root is not None else SUBJECT_ROOT
    seed = '%s|%s|%s' % (key, attempt, os.urandom(16).hex())
    return base / hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]


def scrub_env(environ):
    """Allowlist only. Unrelated host secrets never reach a child process."""
    env = {key: str(value) for key, value in dict(environ).items()
           if key in ALLOWED_ENV and value is not None}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def subject_env(environ, sandbox_home, extra_path=None):
    """The only environment a subject ever sees: neutral HOME/TMPDIR/PATH."""
    env = scrub_env(environ)
    env.pop('XDG_RUNTIME_DIR', None)
    env['HOME'] = str(sandbox_home)
    env['TMPDIR'] = str(Path(sandbox_home) / 'tmp')
    bits = ['/usr/local/bin', '/usr/bin', '/bin']
    if extra_path:
        bits.insert(0, str(extra_path))
    env['PATH'] = ':'.join(bits)
    env.setdefault('LANG', 'C.UTF-8')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


# --------------------------------------------------------------------------- #
# skill tree (D2) and manifest
# --------------------------------------------------------------------------- #

KEEP_FILES = ('SKILL.md', 'LICENSE', 'README.md')
KEEP_DIRS = ('agents', 'assets', 'references', 'scripts')


def _member_path(name):
    rel = name.replace('\\', '/')
    while rel.startswith('./'):
        rel = rel[2:]
    return rel.strip('/')


def _keep_member(rel):
    if rel in KEEP_FILES:
        return True
    head, _, tail = rel.partition('/')
    return bool(tail) and head in KEEP_DIRS


def skill_tree_from_git(repo, commit):
    """Return ``{relpath: bytes}`` for the skill-only slice of ``commit``."""
    repo = Path(repo)
    proc = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', str(commit)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', 'replace').strip()[:300]
        raise RuntimeError('git archive %s failed: %s' % (commit, detail or 'no stderr'))
    tree = {}
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode='r:') as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = _member_path(member.name)
            if not rel or '..' in rel.split('/') or not _keep_member(rel):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            tree[rel] = handle.read()
    return tree


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode('utf-8')


def _fingerprint_hashes(hashes):
    lines = ''.join('%s\0%s\n' % (rel, hashes[rel]) for rel in sorted(hashes))
    return hashlib.sha256(lines.encode('utf-8')).hexdigest()


def tree_fingerprint(tree):
    """sha256 over sorted ``relpath\\0sha256(file)\\n`` (D2)."""
    return _fingerprint_hashes({rel: hashlib.sha256(_as_bytes(data)).hexdigest()
                                for rel, data in tree.items()})


def install_skill(tree, profile_dir):
    """Write the tree under ``<profile>/skills/longhaul`` and return that path."""
    root = Path(profile_dir) / 'skills' / 'longhaul'
    for rel in sorted(tree):
        parts = [part for part in str(rel).replace('\\', '/').split('/')
                 if part not in ('', '.')]
        if not parts or any(part == '..' for part in parts):
            raise ValueError('unsafe skill path: %r' % rel)
        target = root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_as_bytes(tree[rel]))
    return str(root)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# no-follow file access
# --------------------------------------------------------------------------- #
# Every artifact below a subject-controlled directory is untrusted: it may be a
# symlink (leaf or parent), a FIFO, a device or a socket. Opening the path and
# trusting ``resolve()`` is not enough (that is the R4 symlink BLOCKER), so the
# harness walks the components with O_NOFOLLOW and opens only regular files.

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def _open_nofollow(path, flags=os.O_RDONLY):
    """Open ``path`` only if no component is a symlink; None on any refusal."""
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
    """Bytes of a regular file opened without following any symlink; None else."""
    fd = _open_nofollow(path, os.O_RDONLY)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks = []
        remaining = limit
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


def _sha256_regular(path):
    fd = _open_nofollow(path, os.O_RDONLY)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def _link_marker(path):
    try:
        target = os.readlink(str(path))
    except OSError:
        return 'link:unreadable'
    return 'link:' + hashlib.sha256(target.encode('utf-8', 'replace')).hexdigest()


def manifest(root):
    """``{relposix: digest}`` for every source file below ``root``.

    The tree belongs to the subject, so nothing here follows a link or opens a
    non-regular file: symlinks are recorded as ``link:<sha256(target)>`` (the
    link text only, never the target's bytes), FIFOs/devices/sockets as
    ``special``, and a regular file is hashed through an O_NOFOLLOW descriptor.
    Interpreter caches are not the subject's work: a correct test run writes
    ``__pycache__`` and that must never become an out_of_scope_write.
    """
    root = Path(root)
    out = {}
    try:
        info = os.lstat(root)
    except OSError:
        return out
    if not stat.S_ISDIR(info.st_mode):
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            path = base / name
            try:
                entry = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                out[(path.relative_to(root)).as_posix() + '/'] = _link_marker(path)
                continue
            if name in CACHE_DIRS or not stat.S_ISDIR(entry.st_mode):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            try:
                entry = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                out[rel] = _link_marker(path)
                continue
            if not stat.S_ISREG(entry.st_mode):
                out[rel] = 'special'
                continue
            if name.endswith(CACHE_SUFFIXES):
                continue
            digest = _sha256_regular(path)
            out[rel] = digest if digest else 'unreadable'
    return out


def _read_prefix(path, limit):
    """At most ``limit`` bytes of a regular file opened without following links."""
    fd = _open_nofollow(path, os.O_RDONLY)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        return os.read(fd, limit)
    except OSError:
        return None
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# the verified test double (R6: identity by content, canonical argv only)
# --------------------------------------------------------------------------- #
# The offline lane exists only to exercise the harness with the double that
# ships in this repository. It is selected by content, never by a name, a flag
# or a caller's promise: the entry must be byte-identical to
# ``tests/fake_hermes.py`` (shebang aside, so it can be installed with a
# different interpreter), it must be reachable without following a symlink, and
# only two argv shapes are canonical. Everything else is refused before exec.

DOUBLE_REL = 'tests/fake_hermes.py'
PY_NAME_RE = re.compile(r'^python(3(\.\d+)?)?$')
# R8: quoted/unquoted ``NAME = value`` and ``NAME: value`` (YAML/JSON), the name
# must *end* in API_KEY/TOKEN/SECRET (so ``max_tokens`` is not a key), and the
# value stops at a real terminator (whitespace, quote, comma, brace, newline).
KEY_ASSIGN_RE = re.compile(
    r'(?im)(?:^|[;{,][ \t]*|[ \t])["\']?(?:export[ \t]+)?'
    r'([A-Za-z0-9_.\-]*(?:API_KEY|TOKEN|SECRET))["\']?[ \t]*[:=][ \t]*["\']?'
    r'([^\s"\',;}\]\r\n]{1,256})')
KEY_SHAPE_RE = re.compile(r'sk-(?:ant-)?[A-Za-z0-9_-]{16,}')
STATE_SCAN_FILES = 400
STATE_SCAN_DIRS = 64
STATE_SCAN_WIDTH = 64
STATE_SCAN_DEPTH = 64
# Total read budget: the state is scanned *completely*, so this must be generous
# enough for a real key-free state (the double's call log no longer lives here).
STATE_SCAN_TOTAL = 16 * 1024 * 1024
STATE_SCAN_BLOCK = 64 * 1024
STATE_SCAN_OVERLAP = 4096
# R7: entry identity is the WHOLE file. Line 1 must match this allow-list of
# python3 launchers and is then replaced by the one fixed canonical shebang, so
# the direct entry, the historical ``python <double>`` launcher and the attempt
# copy hash identically -- and an edited body, an extra line or a shell first
# line never do.
_DOUBLE_SHEBANG_RE = re.compile(
    rb'^#!/usr/bin/(?:env[ \t]+python3|python3(?:\.\d+)*)[ \t]*\r?\n')
DOUBLE_SHEBANG = b'#!/usr/bin/python3\n'
# ``run_one(..., bwrap=False)`` is a deterministic test seam. Only this explicit
# environment decision selects it; it is never inferred from a name or a flag.
TEST_SEAM_ENV = 'LONGHAUL_EVAL_TEST_SEAM'
# The judge is the same verified double under a fixed, minimal contract.
JUDGE = ('claude-sonnet-5-5', 'anthropic')
JUDGE_TOOLSET = 'todo'


def canonical_double_bytes(data):
    """The canonical bytes of a verified double, or None when it is not one.

    Line 1 is an allow-list, never stripped blindly: ``#!/bin/sh`` plus the
    repository body must not hash like the double. Everything after line 1 is
    kept byte for byte.
    """
    match = _DOUBLE_SHEBANG_RE.match(data)
    if not match:
        return None
    return DOUBLE_SHEBANG + data[match.end():]


def _double_digest_of(data):
    canonical = canonical_double_bytes(data)
    return hashlib.sha256(canonical).hexdigest() if canonical is not None else None


def repo_double_path():
    return Path(REPO_ROOT) / DOUBLE_REL


def _double_digest(path):
    data = _read_regular(path, limit=MAX_ARTIFACT_BYTES)
    if data is None:
        return None
    return _double_digest_of(data)


def repo_double_digest():
    return _double_digest(repo_double_path())


def _canonical_double_file(path):
    """Return the body of a verified double; refuse links, specials and edits."""
    data = _read_regular(path, limit=MAX_ARTIFACT_BYTES)
    if data is None:
        raise DoubleRejected('%s is not a regular file reachable without a symlink' % path)
    want = repo_double_digest()
    if want is None:
        raise DoubleRejected('the repository test double %s is missing' % DOUBLE_REL)
    if canonical_double_bytes(data) is None:
        raise DoubleRejected('%s does not start with an allowed python3 shebang' % path)
    if _double_digest_of(data) != want:
        raise DoubleRejected('%s is not the confinement-verified test double' % path)
    return data


def _canonical_interpreter(path):
    """A python3 ELF executable, never a shell, a script or a wrapper.

    R7: a bundled interpreter must be the running interpreter (``realpath(
    sys.executable)``) or root-owned under ``/usr`` and not group/world-writable.
    A ``python3``-named ELF copied into a temp directory is always refused; the
    input form is only validated, never executed -- the attempt always runs
    ``realpath(sys.executable)``.
    """
    if not os.path.isabs(str(path)) or not PY_NAME_RE.match(Path(path).name):
        raise DoubleRejected('interpreter %r is not an absolute python3' % (path,))
    try:
        real = Path(os.path.realpath(str(path)))
    except OSError:
        raise DoubleRejected('interpreter %r is unresolvable' % (path,)) from None
    if not PY_NAME_RE.match(real.name):
        raise DoubleRejected('interpreter %r resolves to %r' % (path, real.name))
    if _read_prefix(real, 4) != b'\x7fELF':
        raise DoubleRejected('interpreter %r is not an ELF executable' % (path,))
    if not os.access(str(real), os.X_OK):
        raise DoubleRejected('interpreter %r is not executable' % (path,))
    runner = Path(os.path.realpath(sys.executable))
    if real == runner:
        return
    try:
        info = os.stat(str(real))
    except OSError:
        raise DoubleRejected('interpreter %r is not statable' % (path,)) from None
    problem = interpreter_trust_problem(real, runner, info.st_uid, info.st_mode)
    if problem:
        raise DoubleRejected('interpreter %r %s' % (real, problem))


def interpreter_trust_problem(real, runner, uid, mode):
    """The owner/mode rule for a bundled interpreter, as a pure function.

    A file that is not the running interpreter must be root-owned, under
    ``/usr`` and not group/world-writable. Kept pure so the rule can be tested
    without depending on the host's uid mapping (an unprivileged container maps
    root to another uid; the semantic test does not).
    """
    if uid != 0:
        return 'is neither %r nor root-owned' % (runner,)
    if not str(real).startswith('/usr/'):
        return 'is not under /usr'
    if int(mode) & 0o022:
        return 'is group- or world-writable'
    return None


def _canonical_shape(argv, state=None):
    """``(items, target, fake_home)`` for a canonical input form, no file reads.

    The shape gate only: ``[double]`` / ``[python3, double]`` plus one optional
    ``--fake-home <state>`` pair. Identity (bytes) is checked separately, once.
    """
    items = [str(part) for part in (argv or [])]
    if not items:
        raise DoubleRejected('empty offline argv')
    fake_home = None
    target_items = list(items)
    if '--fake-home' in items:
        index = items.index('--fake-home')
        if index != len(items) - 2 or items.count('--fake-home') != 1:
            raise DoubleRejected('--fake-home must be the last argv pair')
        fake_home = items[index + 1]
        target_items = items[:index]
        if not os.path.isabs(fake_home):
            raise DoubleRejected('--fake-home must be absolute: %r' % (fake_home,))
    if not target_items:
        raise DoubleRejected('empty offline argv')
    if len(target_items) > 2:
        raise DoubleRejected('the verified double takes no extra arguments: %r'
                             % (target_items[2:],))
    if len(target_items) == 2:
        _canonical_interpreter(target_items[0])
        target = target_items[1]
    else:
        target = target_items[0]
    if state is not None and fake_home is not None:
        try:
            same = Path(fake_home).resolve() == Path(str(state)).resolve()
        except OSError:
            same = False
        if not same:
            raise DoubleRejected('--fake-home %s is not the bound state %s'
                                 % (fake_home, state))
    return items, target, fake_home


def canonical_double(argv, state=None):
    """Return the argv the runner accepts, or raise ``DoubleRejected``.

    Identity only (no execution): the canonical shapes, the interpreter rule and
    a byte-identical, no-follow double file. ``verified_double`` wraps this into
    the immutable token the runner executes from.
    """
    items, target, _fake_home = _canonical_shape(argv, state)
    _canonical_double_file(target)
    return items


@dataclass(frozen=True)
class VerifiedDouble:
    """The one immutable authorisation for the offline lane.

    ``repository_bytes`` are the canonical bytes of *this repository's* double,
    read once when the gate ran. Nothing downstream may read the caller's file
    again: the attempt copy is written from these bytes, so a swap after the
    gate (or an edit of the repository double) can never change what executes.
    """

    argv: tuple
    source: str
    tail: tuple
    digest: str
    repository_bytes: bytes


def _read_repository_double():
    data = _read_regular(repo_double_path(), limit=MAX_ARTIFACT_BYTES)
    if data is None:
        raise DoubleRejected('the repository test double %s is missing' % DOUBLE_REL)
    canonical = canonical_double_bytes(data)
    if canonical is None:
        raise DoubleRejected('the repository test double has no allowed shebang')
    return canonical


def verified_double(argv, state=None):
    """Check the caller's file once (no-follow) and return the immutable token.

    The caller file is only an *identity witness*: its canonical digest must
    equal the repository double's canonical digest. The token carries the
    repository bytes, and they are the only bytes the attempt will ever copy.
    """
    items, source, _fake_home = _canonical_shape(argv, state)
    caller = _read_regular(source, limit=MAX_ARTIFACT_BYTES)
    if caller is None:
        raise DoubleRejected('%s is not readable without following a link' % source)
    if canonical_double_bytes(caller) is None:
        raise DoubleRejected('%s does not start with an allowed python3 shebang'
                             % source)
    repository = _read_repository_double()
    digest = hashlib.sha256(repository).hexdigest()
    if _double_digest_of(caller) != digest:
        raise DoubleRejected('%s is not the confinement-verified test double' % source)
    index = items.index('--fake-home') if '--fake-home' in items else len(items)
    return VerifiedDouble(argv=tuple(items), source=str(source),
                          tail=tuple(items[index:]), digest=digest,
                          repository_bytes=repository)


def double_path(argv):
    """The verified double file named by an argv, or None when it is not canonical."""
    try:
        canonical_double(argv)
    except DoubleRejected:
        return None
    items = [str(part) for part in (argv or [])]
    if '--fake-home' in items:
        items = items[:items.index('--fake-home')]
    return items[-1] if items else None


def double_digest(argv):
    """Body digest of the verified double named by ``argv``, or None."""
    path = double_path(argv)
    return _double_digest(path) if path else None


def double_version_from_bytes(data):
    """The ``DEFAULT_VERSION`` declared by a double's bytes, or None.

    R8b: the offline pin reads the version from ``VerifiedDouble.repository_bytes``
    -- never from the caller's path again.
    """
    match = re.search(r"^DEFAULT_VERSION\s*=\s*'([^']*)'",
                      (data or b'').decode('utf-8', 'replace'), re.MULTILINE)
    return match.group(1) if match else None


def double_version(argv):
    """The ``DEFAULT_VERSION`` the double at ``argv`` declares, or None.

    Identity surface only (header stubs, diagnostics); the offline run/measure
    pin uses ``double_version_from_bytes`` on the token.
    """
    path = double_path(argv)
    if not path:
        return None
    data = _read_regular(path, limit=MAX_ARTIFACT_BYTES)
    if data is None:
        return None
    return double_version_from_bytes(data)


def _harness_home():
    """The invoking user's home from the passwd db, never from ``$HOME``."""
    try:
        import pwd
        path = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return None
    return path if path.is_absolute() and str(path) != '/' else None


def _text_views(data):
    """UTF-8 plus UTF-16 (with or without BOM) views of one window."""
    views = []
    if data.startswith(b'\xff\xfe'):
        views.append(data[2:].decode('utf-16-le', 'replace'))
    elif data.startswith(b'\xfe\xff'):
        views.append(data[2:].decode('utf-16-be', 'replace'))
    else:
        views.append(data.decode('utf-8', 'replace'))
        zeros = data.count(b'\x00')
        if zeros:
            # R8b: ASCII-range UTF-16 text mixed into a UTF-8 file, or shifted by
            # an odd offset, is recovered by dropping the NUL bytes. Non-ASCII
            # UTF-16 is still only found in the aligned, predominantly UTF-16
            # window below (Results names that limit).
            views.append(data.replace(b'\x00', b'').decode('utf-8', 'replace'))
        if zeros and zeros * 4 >= len(data):
            views.append(data.decode('utf-16-le', 'replace'))
            views.append(data.decode('utf-16-be', 'replace'))
    return views


def _key_problem(data):
    """The first provider-key signal in one window, or None."""
    for text in _text_views(data):
        match = KEY_ASSIGN_RE.search(text)
        if match:
            return 'carries a provider-key assignment (%s)' % match.group(1)
        if KEY_SHAPE_RE.search(text):
            return 'carries a provider-key shaped value'
    return None


def _state_files(root):
    """Complete no-follow walk: ``(paths, problems)``.

    R8: no per-file cap (the double's own growing call log used to trip it).
    Every regular file is returned and read completely by the caller. A cap
    (files, directories, width, depth), a walk error, a symlink or a special
    entry means the state cannot be proven key-free -- a refusal, never a silent
    "nothing found".
    """
    root = Path(root)
    paths = []
    problems = []
    files = 0
    dirs = 0

    def onerror(exc):
        problems.append('cannot be walked (%s)' % exc)

    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False,
                                                onerror=onerror):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        dirs += 1
        if dirs > STATE_SCAN_DIRS:
            return paths, ['has too many directories (>%d)' % STATE_SCAN_DIRS]
        if depth > STATE_SCAN_DEPTH:
            return paths, ['is too deep (>%d levels)' % STATE_SCAN_DEPTH]
        keep = []
        for name in sorted(dirnames):
            path = Path(dirpath) / name
            try:
                info = os.lstat(str(path))
            except OSError as exc:
                return paths, ['cannot be inspected (%s)' % exc]
            if stat.S_ISLNK(info.st_mode):
                return paths, ['contains a symlinked directory (%s)' % path]
            if not stat.S_ISDIR(info.st_mode):
                return paths, ['contains a special entry (%s)' % path]
            keep.append(name)
        if len(keep) > STATE_SCAN_WIDTH:
            return paths, ['has too many directories in one level (>%d)'
                           % STATE_SCAN_WIDTH]
        dirnames[:] = keep
        for name in sorted(filenames):
            files += 1
            if files > STATE_SCAN_FILES:
                return paths, ['has too many files (>%d)' % STATE_SCAN_FILES]
            path = Path(dirpath) / name
            try:
                info = os.lstat(str(path))
            except OSError as exc:
                return paths, ['cannot be inspected (%s)' % exc]
            if stat.S_ISLNK(info.st_mode):
                return paths, ['contains a symlink (%s)' % path]
            if not stat.S_ISREG(info.st_mode):
                return paths, ['contains a special entry (%s)' % path]
            paths.append(path)
        if problems:
            return paths, problems
    return paths, problems


def _scan_file(path, want, budget):
    """Read one regular file completely (no-follow, overlapping blocks).

    Returns ``(problem, remaining_budget)``. The whole file is read, so a key
    after the old 64 KiB prefix, or one that straddles a block boundary, is
    found. An unreadable file is a refusal, not a skip.
    """
    try:
        handle = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return 'cannot be read to prove key-free (%s)' % exc, budget
    try:
        window = b''
        first = True
        while True:
            block = os.read(handle, STATE_SCAN_BLOCK)
            if not block:
                break
            budget -= len(block)
            if budget < 0:
                return 'is too large to prove key-free', budget
            if first and len(block) < STATE_SCAN_BLOCK and want is not None \
                    and _double_digest_of(block) == want:
                return None, budget          # this repository's double source
            first = False
            window += block
            problem = _key_problem(window)
            if problem:
                return problem, budget
            window = window[-STATE_SCAN_OVERLAP:]
        return None, budget
    except OSError as exc:
        return 'cannot be read to prove key-free (%s)' % exc, budget
    finally:
        os.close(handle)


def offline_state_problem(state):
    """Why ``state`` may not be bound read-write into an offline sandbox, or None.

    Besides the path checks, a state that holds ``.hermes/.env`` or a
    provider-key shaped value is refused before any process starts: the state
    directory is mounted into the subject, so it must be key-free by
    construction, not by redaction. The verified double's own source is not key
    material and is skipped by content; everything else is read completely.
    """
    path = Path(str(state))
    if not path.is_absolute():
        return '--offline-state must be absolute: %r' % (state,)
    try:
        resolved = path.resolve()
    except OSError:
        return '--offline-state cannot be resolved: %r' % (state,)
    real = _harness_home()
    if real is not None:
        try:
            real = real.resolve()
        except OSError:
            real = None
    if real is not None and (resolved == real or real.is_relative_to(resolved)):
        return ('--offline-state %s would expose the harness home (%s) to the subject'
                % (resolved, real))
    repo = REPO_ROOT.resolve()
    if resolved == repo or repo.is_relative_to(resolved):
        return '--offline-state %s contains the repository' % resolved
    if resolved == Path('/') or resolved.parent == Path('/'):
        return '--offline-state %s is a top-level system directory' % resolved
    if not resolved.is_dir():
        return '--offline-state %s is not a directory' % resolved
    if not os.access(str(resolved), os.W_OK | os.X_OK):
        return '--offline-state %s is not writable' % resolved
    if os.path.lexists(str(resolved / '.hermes' / '.env')):
        return '--offline-state %s holds a .hermes/.env' % resolved
    files, problems = _state_files(resolved)
    if problems:
        return '--offline-state %s %s' % (resolved, problems[0])
    want = repo_double_digest()
    budget = STATE_SCAN_TOTAL
    for path in files:
        problem, budget = _scan_file(path, want, budget)
        if problem:
            if problem.startswith('carries'):
                return '--offline-state %s %s' % (resolved, problem)
            return '--offline-state %s %s' % (resolved, problem)
    return None


def offline_gate(argv, state=None):
    """``(token, errors)`` for the offline lane; fail-closed.

    The token is the one immutable authorisation: the caller file is checked
    once, compared with the repository's canonical bytes, and only those bytes
    travel further. Every consumer (run, probe, pilot, measure and the judge)
    takes the same token.
    """
    try:
        token = verified_double(argv, state)
    except DoubleRejected as exc:
        return None, [str(exc)]
    if not state:
        return None, ['--offline-state (or HOME) is needed for the test double']
    problem = offline_state_problem(state)
    if problem:
        return None, [problem]
    return token, []


# --------------------------------------------------------------------------- #
# launcher chain (resolve the real first process without executing it)
# --------------------------------------------------------------------------- #


class LauncherError(RuntimeError):
    """The launcher chain cannot be resolved to an executable we may sandbox."""


@dataclass(frozen=True)
class LauncherEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class LauncherChain:
    argv: tuple
    entry: str
    final: str | None
    edges: tuple
    targets: tuple
    roots: tuple

    def as_dict(self):
        return {'argv': list(self.argv), 'entry': self.entry, 'final': self.final,
                'edges': [{'source': edge.source, 'target': edge.target,
                           'kind': edge.kind} for edge in self.edges],
                'targets': list(self.targets), 'roots': list(self.roots)}


# Only these home subtrees may be remounted; ``~/.hermes`` as a whole never is.
HOME_ROOT_PATTERNS = (
    # The launcher entry is a single file: the rest of ~/.local/bin (claude,
    # codex, airi, cyberpunkt-*, ...) has no business inside the sandbox.
    ('entry-bin', re.compile(r'^\.local/bin/hermes$')),
    ('agent-bin', re.compile(r'^\.hermes/hermes-agent/\.hermes/bin$')),
    ('agent', re.compile(r'^\.hermes/hermes-agent$')),
    ('tools-python', re.compile(r'^\.hermes/tools/python-[^/]+$')),
    ('uv-python', re.compile(r'^\.local/share/uv/python/cpython-3\.11[^/]*$')),
)


def _read_bytes(path, limit=4):
    try:
        with Path(path).open('rb') as stream:
            return stream.read(limit)
    except OSError:
        return b''


def _entry_path(first, env):
    first = str(first)
    if not first:
        raise LauncherError('empty launcher entry')
    if os.sep in first:
        if not os.path.isabs(first):
            raise LauncherError('relative launcher path: %r' % first)
        return Path(first)
    found = shutil.which(first, path=(env or {}).get('PATH') or os.defpath)
    if not found:
        raise LauncherError('launcher %r is not on PATH' % first)
    return Path(found)


def _shebang(path):
    try:
        first = Path(path).open('rb').readline().decode('utf-8', 'replace').strip()
    except OSError:
        return ''
    if not first.startswith('#!'):
        return ''
    return first[2:].strip()


def _exec_targets(path):
    """Absolute targets of ``exec`` lines in a shell launcher."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    targets = []
    for match in re.finditer(r'(?m)^\s*exec\s+([^\s;]+)', text):
        token = match.group(1).strip().strip('"\'')
        if not token:
            continue
        if not os.path.isabs(token):
            raise LauncherError('relative exec target in %s: %r' % (path, token))
        targets.append(token)
    return targets


def _chain_roots(targets, home):
    """Read-only home prefixes the resolved chain actually needs under ``home``.

    ``[]`` when no target lives under ``home``; a target under ``home`` without a
    matching allowlist entry is refused (fail-closed).
    """
    home = Path(home).resolve()
    roots = []
    for target in targets:
        path = Path(target)
        try:
            path.relative_to(home)
        except ValueError:
            continue
        found = False
        for candidate in [path] + list(path.parents):
            if candidate == home:
                break
            try:
                rel = candidate.relative_to(home).as_posix()
            except ValueError:
                break
            if any(pattern.match(rel) for _name, pattern in HOME_ROOT_PATTERNS):
                text = str(candidate)
                if text not in roots:
                    roots.append(text)
                found = True
        if not found:
            raise LauncherError('home target %s has no allowed read-only root' % path)
    return roots


def resolve_launcher_chain(argv, env=None, home=None, max_nodes=MAX_CHAIN_NODES):
    """Resolve entry -> symlinks/shebangs/exec targets until an ELF executable.

    Nothing is executed. Relative or unreadable targets are refused, and every
    target under the real home must fall under one of the allowed roots.
    """
    env = dict(env or os.environ)
    home = Path(home) if home is not None else Path(env.get('HOME') or Path.home())
    home = home.resolve()
    argv = [str(part) for part in (argv or [])]
    if not argv:
        raise LauncherError('empty argv')
    entry = _entry_path(argv[0], env)
    queue = [entry]
    seen = set()
    edges = []
    targets = []
    final = None
    nodes = 0
    while queue:
        path = queue.pop(0)
        nodes += 1
        if nodes > max_nodes:
            raise LauncherError('launcher chain is cyclic or too deep')
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        targets.append(key)
        if not path.exists():
            raise LauncherError('chain target does not exist: %s' % path)
        if path.is_dir():
            raise LauncherError('chain target is a directory: %s' % path)
        if path.is_symlink():
            link = os.readlink(str(path))
            target = Path(link)
            if not target.is_absolute():
                target = path.parent / target
            edges.append(LauncherEdge(str(path), str(target), 'symlink'))
            queue.append(target)
            continue
        head = _read_bytes(path, 4)
        if head == b'\x7fELF':
            final = str(path)
            continue
        if head[:2] == b'#!':
            line = _shebang(path)
            parts = line.split()
            if not parts:
                raise LauncherError('empty shebang in %s' % path)
            interpreter = parts[0]
            if not interpreter.startswith('/'):
                raise LauncherError('relative shebang interpreter in %s: %r'
                                    % (path, interpreter))
            edges.append(LauncherEdge(str(path), interpreter, 'shebang'))
            queue.append(Path(interpreter))
            if os.path.basename(interpreter) == 'env' and len(parts) > 1:
                resolved = shutil.which(parts[1], path=env.get('PATH') or os.defpath)
                if not resolved:
                    raise LauncherError('shebang program %r is not on PATH' % parts[1])
                edges.append(LauncherEdge(str(path), resolved, 'shebang'))
                queue.append(Path(resolved))
            if os.path.basename(interpreter) in SHELLS:
                for target in _exec_targets(path):
                    edges.append(LauncherEdge(str(path), target, 'exec'))
                    queue.append(Path(target))
            continue
        raise LauncherError('unclear chain target (neither script nor ELF): %s' % path)
    roots = _chain_roots(targets, home)
    forbidden = str(home / '.hermes')
    if forbidden in roots:
        raise LauncherError('refusing to mount ~/.hermes as a whole')
    if not any(str(target).startswith(str(home)) for target in targets) and roots:
        raise LauncherError('roots without a home target')
    return LauncherChain(argv=tuple(argv), entry=str(entry), final=final,
                         edges=tuple(edges), targets=tuple(targets),
                         roots=tuple(roots))


# --------------------------------------------------------------------------- #
# sandbox plan and bwrap argv
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SandboxPlan:
    """Everything one bwrap invocation needs; the only way a live subject runs."""

    execdir: Path
    sandbox_home: Path
    work: Path
    real_home: Path
    bwrap: str
    chain: LauncherChain
    env: dict
    network: bool = True
    extra_binds: tuple = ()
    # Extra --setenv pairs the plan owns (the offline double's attempt-owned log
    # directory). They are part of the plan, never taken from the caller.
    extra_env: tuple = ()
    # Files inside the attempt that must stay read-only for the subject: the
    # verified double copy is re-bound read-only after the writable execdir bind.
    protected: tuple = ()

    def wrap(self, inner, chdir=None):
        return wrap_argv(self, inner, chdir=chdir)

    def as_dict(self):
        return {'execdir': str(self.execdir), 'sandbox_home': str(self.sandbox_home),
                'work': str(self.work), 'real_home': str(self.real_home),
                'bwrap': self.bwrap, 'chain': self.chain.as_dict(),
                'roots': list(self.chain.roots), 'network': self.network,
                'extra_binds': [str(path) for path in self.extra_binds],
                'extra_env': [list(pair) for pair in self.extra_env],
                'protected': [str(path) for path in self.protected]}


def plan_subject(execdir, hermes, host_home, env=None, bwrap=BWRAP, chain=None,
                 sandbox_home=None, network=True, extra_binds=(), protected=(),
                 extra_env=()):
    """Build the fail-closed plan: chain, bwrap, neutral env.

    ``sandbox_home`` names the directory that becomes the subject's HOME (the
    default is ``<execdir>/home``); it is masked first and re-bound only if it is
    also listed in ``extra_binds``. ``extra_binds`` are bound read-write at their
    own path after every mask -- the offline double's state directory is the only
    caller. ``network=False`` adds ``--unshare-net``: the offline double never
    needs the network.
    """
    execdir = Path(execdir)
    host_home = Path(host_home).resolve()
    bwrap_path = str(bwrap or BWRAP)
    if not Path(bwrap_path).is_file() or not os.access(bwrap_path, os.X_OK):
        raise LauncherError('bwrap is missing or not executable: %s' % bwrap_path)
    chain = chain or resolve_launcher_chain(hermes, env or os.environ, home=host_home)
    # Every masked home must re-bind the chain targets that live under it,
    # otherwise an overridden $HOME hides the interpreter the entry needs
    # (R6: the ``python <verified-double>`` launcher must work, and a target
    # under a masked home without an allowed root is refused, never exposed).
    roots = list(chain.roots)
    attempt_root = execdir.resolve()
    for other in _home_masks(host_home)[1:]:
        for target in chain.targets:
            try:
                Path(target).relative_to(Path(other).resolve())
            except ValueError:
                continue
            if str(target) == str(attempt_root) \
                    or str(target).startswith(str(attempt_root) + '/') \
                    or str(target).startswith(str(execdir) + '/'):
                continue          # the attempt (and its verified copy) is bound anyway
            if any(str(target) == root or str(target).startswith(root + '/')
                   for root in roots):
                continue          # already re-bound through the declared home
            for root in _chain_roots([target], other):
                if root not in roots:
                    roots.append(root)
    if tuple(roots) != chain.roots:
        chain = dataclasses.replace(chain, roots=tuple(roots))
    home = Path(sandbox_home) if sandbox_home is not None else execdir / 'home'
    binds = []
    for path in extra_binds or ():
        resolved = Path(path)
        if not resolved.is_absolute():
            raise LauncherError('extra bind must be absolute: %s' % path)
        if resolved not in binds:
            binds.append(resolved)
    base_env = subject_env(env or os.environ, home,
                           extra_path=Path(chain.entry).parent)
    guarded = []
    for path in protected or ():
        resolved = Path(path)
        if not resolved.is_absolute():
            raise LauncherError('protected file must be absolute: %s' % path)
        if resolved not in guarded:
            guarded.append(resolved)
    for pair in extra_env or ():
        key, value = pair
        if not str(key) or not str(value):
            raise LauncherError('extra env pair must be non-empty: %r' % (pair,))
    return SandboxPlan(execdir=execdir, sandbox_home=home,
                       work=execdir / 'work', real_home=host_home,
                       bwrap=bwrap_path, chain=chain, env=base_env,
                       network=bool(network), extra_binds=tuple(binds),
                       extra_env=tuple((str(k), str(v)) for k, v in (extra_env or ())),
                       protected=tuple(guarded))


def _wrap_path(plan):
    minimal = ['/usr/local/bin', '/usr/bin', '/bin']
    parent = str(Path(plan.chain.entry).parent)
    if parent in minimal:
        return ':'.join(minimal)
    return ':'.join([parent] + minimal)


def _user_home():
    """The invoking user's real home from the passwd db, never from ``$HOME``."""
    return _harness_home()


def _home_masks(real_home):
    """The tmpfs targets that hide home data inside the sandbox.

    Always the home the harness was told to use. When ``$HOME`` was overridden
    (tests, a wrapper, a service), the user's real home would otherwise stay
    visible through the read-only root bind, so it is masked as well -- strictly
    more isolation, never less. The individual launcher roots are re-bound after
    every mask.
    """
    home = Path(real_home)
    masks = [home]
    other = _user_home()
    if other is None:
        return masks
    try:
        if other.resolve() == home.resolve():
            return masks
    except OSError:
        if str(other) == str(home):
            return masks
    masks.append(other)
    return masks


def wrap_argv(first, second=None, *args, chdir=None, **legacy):
    """The bwrap argv for one subject call.

    Accepts ``wrap_argv(plan, inner)`` or ``wrap_argv(inner, plan)``. The legacy
    piecewise form (execdir=..., sandbox_home=..., roots=...) stays available
    for callers that have no chain, but every live path uses the plan.

    ``chdir`` overrides the working directory bwrap enters; profile create and
    delete pass the execdir itself because ``<execdir>/work`` may be gone by
    the time a delete runs.
    """
    if isinstance(first, SandboxPlan):
        plan, inner = first, second
    elif isinstance(second, SandboxPlan):
        plan, inner = second, first
    else:
        plan, inner = None, first
    if plan is None:
        if args:
            # legacy positional form: wrap_argv(inner, execdir, sandbox_home[, real_home])
            real_home = args[1] if len(args) > 1 else None
            if real_home is None:
                raise LauncherError('legacy wrap_argv needs real_home')
            return _wrap_pieces(inner, execdir=second, sandbox_home=args[0],
                                real_home=real_home, chdir=chdir, **legacy)
        return _wrap_pieces(inner, chdir=chdir, **legacy)
    inner = [str(part) for part in (inner or [])]
    execdir = Path(plan.execdir)
    home_at = str(plan.real_home)
    work_at = str(chdir) if chdir is not None else str(execdir / 'work')
    argv = [plan.bwrap, '--ro-bind', '/', '/']
    for mask in _home_masks(plan.real_home):
        argv += ['--tmpfs', str(mask)]
    for root in plan.chain.roots:
        argv += ['--ro-bind', str(root), str(root)]
    argv += ['--tmpfs', '/tmp', '--tmpfs', '/var/tmp', '--tmpfs', '/run/user',
             '--tmpfs', '/run/dbus', '--tmpfs', '/run/systemd',
             '--dir', str(execdir.parent),
             '--bind', str(execdir), str(execdir)]
    for extra in plan.extra_binds:
        argv += ['--bind', str(extra), str(extra)]
    for path in plan.protected:
        argv += ['--ro-bind', str(path), str(path)]
    argv += ['--dev', '/dev', '--proc', '/proc',
             '--unshare-pid']
    if not plan.network:
        argv += ['--unshare-net']
    argv += ['--die-with-parent', '--new-session',
             '--clearenv',
             '--setenv', 'HOME', str(plan.sandbox_home),
             '--setenv', 'TMPDIR', str(plan.sandbox_home / 'tmp'),
             '--setenv', 'PATH', _wrap_path(plan),
             '--setenv', 'LANG', plan.env.get('LANG', 'C.UTF-8')]
    for key, value in plan.extra_env:
        argv += ['--setenv', key, value]
    argv += ['--chdir', work_at, '--']
    return argv + inner


def _wrap_pieces(inner, execdir=None, sandbox_home=None, real_home=None, roots=(),
                 bwrap=None, path_extra=None, chdir=None):
    """Legacy/test form without a resolved chain."""
    if execdir is None or sandbox_home is None or real_home is None:
        raise LauncherError('wrap_argv needs a SandboxPlan or execdir/sandbox_home/'
                            'real_home')
    execdir = Path(execdir)
    sandbox_home = Path(sandbox_home)
    bits = [str(path_extra)] if path_extra else []
    bits += ['/usr/local/bin', '/usr/bin', '/bin']
    work_at = str(chdir) if chdir is not None else str(execdir / 'work')
    argv = [str(bwrap or BWRAP), '--ro-bind', '/', '/']
    for mask in _home_masks(real_home):
        argv += ['--tmpfs', str(mask)]
    for root in roots or ():
        argv += ['--ro-bind', str(root), str(root)]
    argv += ['--tmpfs', '/tmp', '--tmpfs', '/var/tmp', '--tmpfs', '/run/user',
             '--tmpfs', '/run/dbus', '--tmpfs', '/run/systemd',
             '--dir', str(execdir.parent),
             '--bind', str(execdir), str(execdir),
             '--dev', '/dev', '--proc', '/proc',
             '--unshare-pid', '--die-with-parent', '--new-session',
             '--clearenv',
             '--setenv', 'HOME', str(sandbox_home),
             '--setenv', 'TMPDIR', str(sandbox_home / 'tmp'),
             '--setenv', 'PATH', ':'.join(bits),
             '--setenv', 'LANG', 'C.UTF-8',
             '--chdir', work_at,
             '--']
    return argv + [str(part) for part in (inner or [])]


def install_roots(hermes, env):
    """Compatibility shim: the resolved chain owns the roots now."""
    try:
        return list(resolve_launcher_chain(hermes, env or os.environ).roots)
    except LauncherError:
        return []


def _attempt_double(execdir, token):
    """Write the token's repository bytes into the attempt; return the argv.

    R8: the only byte source is ``token.repository_bytes`` -- the canonical
    repository double read once by the gate. The caller's file is never read
    again, so a swap after the gate (TOCTOU) cannot reach the attempt. The copy
    is executed as ``[realpath(sys.executable), <attempt>/subject.py, ...]``,
    re-read, re-hashed and re-bound read-only inside the sandbox.
    """
    if not isinstance(token, VerifiedDouble):
        raise DoubleRejected('the attempt needs a verified double token')
    data = token.repository_bytes
    want = token.digest
    target = Path(execdir) / 'subject.py'
    try:
        if target.exists() or target.is_symlink():
            target.unlink()
        handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o400)
        try:
            os.write(handle, data)
            os.fsync(handle)
        finally:
            os.close(handle)
    except OSError as exc:
        raise DoubleRejected('cannot write the attempt copy: %s' % exc) from None
    copied = _read_regular(target, limit=MAX_ARTIFACT_BYTES)
    if copied is None:
        raise DoubleRejected('the attempt copy cannot be read safely; check the '
                             'sandbox root and its parent directories for symlinks, '
                             'non-regular files or permissions')
    if hashlib.sha256(copied).hexdigest() != want:
        raise DoubleRejected('the attempt copy of the double does not match the '
                             'verified content')
    exec_argv = [str(Path(os.path.realpath(sys.executable))), str(target)] + list(token.tail)
    return exec_argv, want, str(target)


def offline_plan(execdir, token, host_home, state, call=None, bwrap=None,
                 timeout=120, sandbox_home=None):
    """The one offline plan: verified copy + key-free state + bwrap + no network.

    ``token`` is a :class:`VerifiedDouble` (an argv is accepted for tests and is
    turned into a token once, right here). Returns ``(plan, exec_argv, errors)``.
    Nothing starts until the state is key-free, the token bytes are in the
    attempt, bwrap answers ``--version`` and the copy starts inside the sandbox;
    every later Hermes call (create, list, chat, delete) and the judge use the
    same plan and the same read-only copy.
    """
    if call is None:
        call = _popen
    execdir = Path(execdir)
    if not isinstance(token, VerifiedDouble):
        try:
            token = verified_double(token, state)
        except DoubleRejected as exc:
            return None, None, ['S0: %s' % exc]
    if not state:
        return None, None, ['S0: the offline lane needs a key-free state directory']
    problem = offline_state_problem(state)
    if problem:
        return None, None, ['S0: %s' % problem]
    try:
        exec_argv, _digest, copy_path = _attempt_double(execdir, token)
    except DoubleRejected as exc:
        return None, None, ['S0: %s' % exc]
    try:
        plan = plan_subject(execdir, exec_argv, host_home, env=os.environ, bwrap=bwrap,
                            sandbox_home=sandbox_home, network=False,
                            extra_binds=(Path(str(state)),),
                            protected=(Path(copy_path),),
                            extra_env=(('LONGHAUL_FAKE_LOG',
                                        str(execdir / 'fake-log')),))
    except LauncherError as exc:
        return None, None, ['S1: %s' % exc]
    version = call([plan.bwrap, '--version'], cwd=None, env=scrub_env(os.environ),
                   timeout=timeout)
    if version.rc != 0 or not (version.stdout or '').strip():
        return None, None, ['S1: bwrap --version failed (rc=%s)' % version.rc]
    probe = call(plan.wrap(exec_argv + ['--version']), cwd=str(execdir / 'work'),
                 env=plan.env, timeout=timeout)
    if probe.rc != 0 or not (probe.stdout or '').strip():
        return None, None, ['S1: the test double does not start inside the sandbox '
                            '(version probe rc=%s)' % probe.rc]
    return plan, exec_argv, []


# --------------------------------------------------------------------------- #
# judge: one runner-owned route, never a host subprocess
# --------------------------------------------------------------------------- #

def judge_command(hermes, qfile):
    """The pinned judge argv: same verified copy, minimal toolset, no stream.

    Only a fixed flag sequence after the entry; ``-t todo`` is the smallest
    harmless toolset (``file`` would expose read/write into the real HOME). This
    builder must stay byte-identical to ``evals/scripts/judge.py``'s contract.
    """
    return list(hermes or ['hermes']) + [
        'chat', '--query-file', str(qfile), '-m', JUDGE[0], '--provider', JUDGE[1],
        '-t', JUDGE_TOOLSET, '-Q', '--ignore-user-config', '--ignore-rules',
        '--source', 'tool', '--max-turns', '1']


def run_judge(verified_subject, state, prompt, host_home=None, bwrap=None, call=None,
              timeout=300):
    """Run one judge call through the same offline plan as the subject.

    ``verified_subject`` is the same :class:`VerifiedDouble` the run used (an
    argv is turned into a token once, here). The judge gets the same boundary:
    a fresh neutral attempt directory, the verified copy, ``--unshare-net``, a
    scrubbed environment and a neutral HOME, and the attempt directory is
    removed in ``finally``. Returns a ``Proc``; rc 2 with the setup errors when
    the plan refuses (fail-closed, never an unwrapped retry).
    """
    if call is None:
        call = _popen
    home = Path(host_home) if host_home else (_harness_home() or Path.home())
    execdir = neutral_execdir('judge/%s' % os.urandom(8).hex(), 1)
    try:
        (execdir / 'work').mkdir(parents=True, exist_ok=True)
        plan, exec_argv, errors = offline_plan(execdir, verified_subject, home, state,
                                              call=call, bwrap=bwrap, timeout=timeout,
                                              sandbox_home=execdir / 'home')
        if errors or plan is None:
            return Proc(rc=2, stdout='', stderr='; '.join(errors or ['no plan']))
        qfile = execdir / 'q.md'
        qfile.write_text(prompt, encoding='utf-8')
        argv = plan.wrap(judge_command(exec_argv, qfile))
        return call(argv, cwd=str(execdir / 'work'), env=plan.env, timeout=timeout)
    finally:
        shutil.rmtree(str(execdir), ignore_errors=True)


# --------------------------------------------------------------------------- #
# fixed maintenance probes (classified non-subject spawns)
# --------------------------------------------------------------------------- #

def bwrap_version():
    """The host bwrap's version string, or None when it is missing/broken."""
    if not Path(str(BWRAP)).is_file():
        return None
    try:
        proc = subprocess.run([str(BWRAP), '--version'], capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or '').strip().splitlines()
    return text[0] if text else None


def repo_head():
    """The repository HEAD, display metadata only (never a run pin)."""
    try:
        proc = subprocess.run(['git', '-C', str(REPO_ROOT), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True)
        return proc.stdout.strip() or None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# preflight P1-P11
# --------------------------------------------------------------------------- #

CONTAINS_RE = re.compile(r'longhaul|\u8fdc\u822a', re.IGNORECASE)
# A 429 counts only as a token of its own: the digits inside a hex profile name, a temp
# path, a port, a pid or a traceback ``line 429`` are not an HTTP status.
RATE_RE = re.compile(r'(?i)(?<!line )\b429\b|rate.?limit|too many requests')
UNSAFE_FILES = ('expected.json', 'AGENTS.md', 'CLAUDE.md', '.cursorrules', 'SOUL.md',
                '.hermes.md', 'HERMES.md', 'AGENTS.override.md', 'agents.md')
UNSAFE_DIRS = ('.hermes', '.agents', '.cursor')


def _read_text(path):
    data = _read_regular(path, limit=4 * 1024 * 1024)
    return data.decode('utf-8', 'replace') if data is not None else ''


def _walk_text(root):
    """``(rel, text)`` for regular files below ``root``; links are never read."""
    root = Path(root)
    out = []
    try:
        info = os.lstat(root)
    except OSError:
        return out
    if not stat.S_ISDIR(info.st_mode):
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            path = base / name
            try:
                entry = os.lstat(path)
            except OSError:
                continue
            if name in CACHE_DIRS or not stat.S_ISDIR(entry.st_mode):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name.endswith(CACHE_SUFFIXES):
                continue
            path = base / name
            try:
                entry = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_size > 4 * 1024 * 1024:
                continue
            data = _read_regular(path, limit=4 * 1024 * 1024)
            if data is None:
                continue
            out.append((path.relative_to(root).as_posix(), data.decode('utf-8',
                                                                      'replace')))
    return out


def _skill_meta(text):
    name = version = None
    for line in text.splitlines():
        match = re.match(r'\s*(name|version)\s*:\s*(.+?)\s*$', line, re.IGNORECASE)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip().strip('"\'')
        if key == 'name' and name is None:
            name = value
        elif key == 'version' and version is None:
            version = value
    return name, version


def preflight(profile_dir, cond, expected_fp, workdir, repo_root, child_env, skills_list_out):
    """P1-P9 on the sandbox profile. Returns ``[]`` when it is clean."""
    profile = Path(profile_dir)
    meta = CONDITIONS.get(cond)
    if meta is None:
        return ['P1: unknown condition %r' % cond]
    candidate = meta.commit is not None
    errors = []

    if not re.fullmatch(PROFILE_RE, profile.name):
        errors.append('P1: profile name %r does not match %s' % (profile.name, PROFILE_RE))

    for rel, text in _walk_text(profile):
        allowed = candidate and (rel == 'skills/longhaul'
                                 or rel.startswith('skills/longhaul/'))
        if allowed:
            continue
        if CONTAINS_RE.search(rel):
            errors.append('P2: path %r mentions the subject skill' % rel)
        if CONTAINS_RE.search(text):
            errors.append('P2: %r mentions the subject skill in its bytes' % rel)

    skill_root = profile / 'skills' / 'longhaul'
    if candidate:
        found = []
        for rel, text in _walk_text(profile):
            if rel.rsplit('/', 1)[-1] != 'SKILL.md':
                continue
            if _skill_meta(text)[0] == 'longhaul':
                found.append(rel)
        if len(found) != 1:
            errors.append('P3: expected exactly one SKILL.md named longhaul, found %d'
                          % len(found))
        else:
            if found[0] != 'skills/longhaul/SKILL.md':
                errors.append('P3: the longhaul SKILL.md sits at %r, not '
                              'skills/longhaul/SKILL.md' % found[0])
            version = _skill_meta(_read_text(profile / found[0]))[1]
            if version != meta.version:
                errors.append('P3: SKILL.md Version is %r, expected %r'
                              % (version, meta.version))
        if skill_root.is_dir():
            installed = manifest(skill_root)
            actual_fp = _fingerprint_hashes(installed)
            if expected_fp and actual_fp != expected_fp:
                errors.append('P3: installed skill fingerprint %s != expected %s'
                              % (actual_fp[:12], str(expected_fp)[:12]))
            forbidden = [rel for rel in installed
                         if 'evals' in rel.split('/') or 'tests' in rel.split('/')]
            if forbidden:
                errors.append('P3: installed skill carries %d forbidden path(s), e.g. %r'
                              % (len(forbidden), forbidden[0]))
        else:
            errors.append('P3: no skills/longhaul directory in the profile')

    listed = bool(re.search(r'(?<![A-Za-z0-9_])longhaul(?![A-Za-z0-9_])',
                            skills_list_out or '', re.IGNORECASE))
    if candidate and not listed:
        errors.append('P3b: skills list does not offer longhaul')
    if not candidate and listed:
        errors.append('P3b: the baseline skills list offers longhaul')

    for rel, _text in _walk_text(profile):
        if rel.rsplit('/', 1)[-1] == 'HOOK.yaml':
            errors.append('P4: hook file %r' % rel)
    hooks = profile / 'hooks'
    if hooks.is_dir():
        entries = [Path(dirpath) / name for dirpath, _dirs, names in os.walk(hooks)
                   for name in names]
        if entries:
            errors.append('P4: hooks/ is not empty (%d file(s))' % len(entries))

    for sub in ('memories', 'sessions'):
        directory = profile / sub
        for rel, _text in _walk_text(directory):
            path = directory / rel
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > 0:
                errors.append('P5: %s/%s is not empty' % (sub, rel))

    env_path = profile / '.env'
    lines = [line for line in _read_text(env_path).splitlines() if line.strip()]
    if len(lines) != 1:
        errors.append('P6: .env must hold exactly one line, found %d' % len(lines))
    elif not re.fullmatch(r'OLLAMA_API_KEY=\S.*', lines[0].strip()):
        errors.append('P6: .env line is not OLLAMA_API_KEY=<nonempty>')

    unexpected = sorted(set(child_env) - set(ALLOWED_ENV) - set(HARNESS_ENV))
    if unexpected:
        errors.append('P7: child env carries %d non-allowlisted key(s): %s'
                      % (len(unexpected), ', '.join(unexpected)))

    work = Path(workdir).resolve()
    repo = Path(repo_root).resolve()
    if work == repo or repo in work.parents:
        errors.append('P8: workdir %s is inside the repo' % work)
    if work.is_dir():
        hits = []
        for dirpath, dirnames, filenames in os.walk(work):
            base = Path(dirpath)
            for name in dirnames:
                if name in UNSAFE_DIRS:
                    hits.append((base / name).relative_to(work).as_posix() + '/')
            for name in filenames:
                if name in UNSAFE_FILES:
                    hits.append((base / name).relative_to(work).as_posix())
        for rel in sorted(set(hits)):
            errors.append('P8: workdir carries %r' % rel)

    found_render = shutil.which('lhrender', path=child_env.get('PATH') or os.defpath)
    if found_render:
        errors.append('P9: lhrender is on PATH (%s); this case needs it absent' % found_render)

    return errors


def _visible_parts(visible, label):
    value = visible[label]
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return [str(value)]


def p11_errors(visible, cond=None):
    """The subject must not be able to tell condition, case or skill apart.

    The only tolerated identity token is the adjacent candidate argv pair
    ``-s longhaul``; everything else is checked textually in cwd, query path,
    profile name/home, argv, prompt, env values and worktree names.
    """
    errors = []
    candidate = cond is not None and cond != 'baseline'
    for label in sorted(visible):
        parts = _visible_parts(visible, label)
        if label == 'chat argv' or label == 'argv':
            kept = []
            index = 0
            while index < len(parts):
                if (candidate and parts[index] == '-s'
                        and index + 1 < len(parts) and parts[index + 1] == SKILL_NAME):
                    index += 2
                    continue
                kept.append(parts[index])
                index += 1
            texts = [' '.join(kept)]
        else:
            texts = parts
        for text in texts:
            if IDENTITY_RE.search(text):
                errors.append('P11: %s exposes the skill identity' % label)
            if CONDITION_RE.search(text):
                errors.append('P11: %s exposes a condition name' % label)
            if CASE_RE.search(text):
                errors.append('P11: %s exposes a case id' % label)
    profile = visible.get('profile')
    if profile is not None and not PROFILE_NEUTRAL_RE.fullmatch(str(profile)):
        errors.append('P11: the profile name is not neutral')
    return errors


# --------------------------------------------------------------------------- #
# command line, stream parsing, classification
# --------------------------------------------------------------------------- #


def build_command(hermes, profile, spec, with_skill):
    """The subject argv; candidates are preloaded with ``-s longhaul`` exactly."""
    rundir = Path(spec.execdir) if spec.execdir else Path(spec.rundir)
    argv = list(hermes) + [
        '-p', profile, 'chat',
        '--query-file', str((rundir / 'prompt.md').resolve()),
        '-m', spec.model, '--provider', spec.provider,
        '-Q', '--ignore-user-config', '--format', 'stream-json',
        '--max-turns', str(spec.max_turns), '--run-budget', str(spec.budget_s),
        '--source', 'tool',
    ]
    if with_skill:
        argv += ['-s', SKILL_NAME]
    return argv


def _event_kind(event):
    for key in ('type', 'event', 'kind'):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ''


def parse_stream(stdout):
    """Parse stream-json lines; the last result event wins."""
    events = []
    result = None
    bad = 0
    for line in (stdout or '').splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            bad += 1
            continue
        if not isinstance(event, dict):
            bad += 1
            continue
        events.append(event)
        if _event_kind(event) == 'result' or (
                'exit_code' in event and 'tokens' in event and 'text' in event):
            result = event
    return {'events': events, 'result': result, 'bad_lines': bad}


def classify(rc, stdout, stderr, timed_out):
    """Map one process outcome onto STATUSES (contract section 5)."""
    parsed = parse_stream(stdout)
    if timed_out:
        return 'timeout', parsed
    result = parsed['result']
    if rc != 0:
        haystack = (stderr or '') + '\n' + str((result or {}).get('error') or '')
        if RATE_RE.search(haystack):
            return 'provider_429', parsed
        if result is not None:
            return 'provider_error', parsed
        return 'hermes_crash', parsed
    if result is None:
        return 'hermes_crash', parsed
    text = result.get('text')
    if not isinstance(text, str) or not text.strip():
        return 'empty_answer', parsed
    return 'ok', parsed


def skill_confirmation(events):
    """True only for a skill tool call whose own result succeeded.

    The stream is emitted by the subject, so this is at best a subject-reported
    positive result; the harness never treats a bare invocation, an error
    result, or the subject's own ``system.skills`` field as a loaded skill
    (R4 MAJOR: a denied ``skill_view`` was counted as confirmation).
    """
    order, uses = [], {}
    for event in events or []:
        if not isinstance(event, dict) or _event_kind(event) != 'tool_use':
            continue
        name = str(event.get('name') or '')
        if name not in ('skill', 'skill_view', 'skill_load', 'skills'):
            continue
        payload = event.get('input') or {}
        if not isinstance(payload, dict):
            continue
        label = str(payload.get('name') or payload.get('skill') or '').lower()
        if label != SKILL_NAME:
            continue
        key = event.get('tool_call_id') or ('anon', len(order))
        if key in uses:
            continue
        uses[key] = None
        order.append(key)
    for event in events or []:
        if not isinstance(event, dict) or _event_kind(event) != 'tool_result':
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


def redact(text, secrets):
    out = '' if text is None else str(text)
    for secret in sorted({item for item in (secrets or []) if item}, key=len, reverse=True):
        out = out.replace(secret, 'REDACTED')
    return out


def _payload_leaves(value):
    """String leaves in document order, skipping event metadata keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _payload_leaves(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ('type', 'event', 'kind'):
                continue
            yield from _payload_leaves(item)


def _iter_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_leaves(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_leaves(item)


def _redact_leaves(value, secrets):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [_redact_leaves(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_leaves(item, secrets) for key, item in value.items()}
    return value


def _wipe_leaves(value):
    if isinstance(value, str):
        return 'REDACTED' if value else value
    if isinstance(value, list):
        return [_wipe_leaves(item) for item in value]
    if isinstance(value, dict):
        return {key: _wipe_leaves(item) for key, item in value.items()}
    return value


def _redact_fragments(value, secrets):
    """Blank every leaf that is itself a fragment of a secret."""
    if isinstance(value, str):
        if value and any(value in secret for secret in secrets if secret):
            return 'REDACTED'
        return value
    if isinstance(value, list):
        return [_redact_fragments(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_fragments(item, secrets) for key, item in value.items()}
    return value


def sanitise_stream(stdout, secrets):
    """Redact every string leaf of a stream-json log, then verify the join.

    ``tool_use.input``, ``error``, ``thinking`` and nested lists/dicts are all
    covered. If the reconstruction of all leaves still yields a secret (the
    fragments join up across events), every leaf is replaced wholesale.
    """
    items = []
    for raw in (stdout or '').splitlines():
        text = raw.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            items.append(redact(text, secrets))
            continue
        if not isinstance(event, dict):
            items.append(redact(text, secrets))
            continue
        items.append(_redact_leaves(event, secrets))
    joined = ''.join(leaf for item in items if isinstance(item, dict)
                     for leaf in _payload_leaves(item))
    if any(secret in joined for secret in (secrets or []) if secret):
        items = [_redact_fragments(item, secrets) if isinstance(item, dict)
                 else redact(item, secrets) for item in items]
        joined = ''.join(leaf for item in items if isinstance(item, dict)
                         for leaf in _payload_leaves(item))
        if any(secret in joined for secret in (secrets or []) if secret):
            items = [_wipe_leaves(item) if isinstance(item, dict) else 'REDACTED'
                     for item in items]
    lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
             for item in items]
    return '\n'.join(lines) + ('\n' if lines else '')


# --------------------------------------------------------------------------- #
# the real probe: the same runner-owned plan, and child facts instead of argv
# --------------------------------------------------------------------------- #
# The probe starts no model and reads no key. It builds the production plan for
# the entry, then runs one inspection shell inside it and reads only LH1-prefixed
# fact lines. A fact that is missing, empty or malformed is a failure: the
# verdict is computed from what the child could see, never from `wrapped`.

PROBE_MARK = 'LH1'
PROBE_DIRS = ('/tmp', '/run/user')
PROBE_MIN_TARGETS = 1
# A successful connect to the host system bus means the sandbox still sees a
# host unix socket (--unshare-net does not close one). R8 masks /run/dbus and
# /run/systemd in the plan; this fact proves it from inside.
DBUS_SOCKET = '/run/dbus/system_bus_socket'
_DBUS_CODE = ('import socket, sys\n'
              'handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
              'handle.settimeout(1.0)\n'
              'try:\n'
              '    handle.connect(sys.argv[1])\n'
              'except OSError:\n'
              '    sys.exit(1)\n')


def probe_inspect_script(chain, skill_root=None, canaries=None):
    """The in-sandbox inspection: only ``LH1``-prefixed facts on stdout."""
    quote = shlex.quote
    canaries = dict(canaries or {})
    lines = [
        'set -u',
        'emit() { printf "LH1 %s\\n" "$*"; }',
        'list_dir() {',
        '  if [ -d "$1" ]; then',
        '    n=$(ls -A "$1" 2>/dev/null | wc -l | tr -d " ")',
        '    emit "dir $1 $n $(ls -A "$1" 2>/dev/null | head -20 | tr "\\n" ",")"',
        '  else',
        '    emit "dir $1 missing 0"',
        '  fi',
        '}',
        'emit "cwd $(pwd)"',
        'emit "home $HOME"',
        ('if [ -e %s ]; then emit "repo %s 1"; else emit "repo %s 0"; fi'
         % (quote(str(Path(REPO_ROOT) / 'evals' / 'runners' / 'hermes.py')),
            REPO_ROOT, REPO_ROOT)),
        ('if command -v python3 >/dev/null 2>&1; then '
         'if python3 -c %s %s >/dev/null 2>&1; then emit "dbus 1"; '
         'else emit "dbus 0"; fi; else emit "dbus skip"; fi'
         % (quote(_DBUS_CODE), DBUS_SOCKET)),
    ]
    for path in PROBE_DIRS:
        lines.append('list_dir %s' % quote(path))
    lines += [
        'list_dir %s' % quote(str(SUBJECT_ROOT)),
        'list_dir "$HOME"',
        'find / -iname "*longhaul*" -print 2>/dev/null > "$TMPDIR/probe-find.txt"',
        'emit "findrc $?"',
        'while IFS= read -r p; do emit "find $p"; done < "$TMPDIR/probe-find.txt"',
        ('grep -iF -e longhaul -e %s /proc/self/mountinfo 2>/dev/null | head -20 '
         '> "$TMPDIR/probe-mount.txt" || true' % quote(str(REPO_ROOT))),
        'while IFS= read -r l; do emit "mount $l"; done < "$TMPDIR/probe-mount.txt"',
        'emit "pid1 $(tr "\\0" " " < /proc/1/cmdline 2>/dev/null)"',
    ]
    for label, path in (('host', canaries.get('host')), ('sock', canaries.get('sock'))):
        if path:
            lines.append('if [ -e %s ]; then emit "%s %s 1"; else emit "%s %s 0"; fi'
                         % (quote(str(path)), label, path, label, path))
    net = canaries.get('net')
    if net:
        host, _, port = str(net).rpartition(':')
        lines.append('if (exec 3<>/dev/tcp/%s/%s) 2>/dev/null; then emit "net %s 1"; '
                     'else emit "net %s 0"; fi' % (host, port, net, net))
    for target in getattr(chain, 'targets', ()) or ():
        lines.append('if [ -e %s ] && [ -r %s ]; then emit "target %s ok"; '
                     'else emit "target %s missing"; fi'
                     % (quote(str(target)), quote(str(target)), target, target))
    return '\n'.join(lines) + '\n'


def parse_probe(stdout):
    """Parse the ``LH1`` facts of one in-sandbox inspection."""
    facts = {'dirs': {}, 'find': [], 'targets': {}, 'mounts': [], 'pid1': '',
             'cwd': None, 'home': None, 'find_rc': None, 'repo': {},
             'dbus': None, 'host': {}, 'sock': {}, 'net': {}}
    for line in (stdout or '').splitlines():
        if not line.startswith(PROBE_MARK + ' '):
            continue
        kind, _, rest = line[len(PROBE_MARK) + 1:].partition(' ')
        if kind == 'dir':
            parts = rest.split(' ', 2)
            path = parts[0]
            state = parts[1] if len(parts) > 1 else ''
            raw = parts[2] if len(parts) > 2 else ''
            facts['dirs'][path] = {'missing': state == 'missing',
                                   'entries': [item for item in raw.split(',') if item]}
        elif kind == 'find':
            facts['find'].append(rest)
        elif kind == 'findrc':
            facts['find_rc'] = int(rest.strip()) if rest.strip().isdigit() else None
        elif kind == 'target':
            path, _, state = rest.rpartition(' ')
            facts['targets'][path] = state
        elif kind == 'mount':
            facts['mounts'].append(rest)
        elif kind == 'pid1':
            facts['pid1'] = rest
        elif kind == 'cwd':
            facts['cwd'] = rest
        elif kind == 'home':
            facts['home'] = rest
        elif kind == 'repo':
            path, _, state = rest.rpartition(' ')
            facts['repo'][path] = state == '1'
        elif kind == 'dbus':
            state = rest.strip()
            facts['dbus'] = None if state == 'skip' else state == '1'
        elif kind in ('host', 'sock', 'net'):
            path, _, state = rest.rpartition(' ')
            facts[kind][path] = state == '1'
    return facts


def _dir_fact(facts, path, label, problems):
    """Return the validated entry of one required directory fact, or None."""
    dirs = facts.get('dirs')
    info = dirs.get(path) if isinstance(dirs, dict) else None
    if not isinstance(info, dict):
        problems.append('%s: %s was not inspected' % (label, path))
        return None
    missing = info.get('missing')
    entries = info.get('entries')
    if not isinstance(missing, bool) or not isinstance(entries, list) \
            or any(not isinstance(item, str) for item in entries):
        problems.append('%s: the %s fact is malformed' % (label, path))
        return None
    return info


def _canary_problem(facts, kind, path, label, problems):
    if not path:
        # R7: an undeclared canary is a dirty probe, not an absent requirement.
        problems.append('%s: no %s canary was declared' % (label, kind))
        return
    table = facts.get(kind)
    if not isinstance(table, dict) or path not in table:
        problems.append('%s: the %s canary %s was not reported' % (label, kind, path))
        return
    if table[path] is not False:
        problems.append('%s: the child reached the %s canary %s' % (label, kind, path))


def probe_problems(facts, label, home, execdir, skill_root=None, inspect_rc=None,
                   expected_targets=None, *, host_canary, socket_canary,
                   net_canary):
    """Every cross-run and identity check the probe must enforce.

    Missing, empty or malformed facts are a failure, not a pass: a probe that
    emits nothing (or only a subset) must never be recorded as clean. A missing
    expected chain target is as fatal as one reported as unreadable.

    R7: the three canary declarations are required keyword arguments with no
    default -- a caller cannot silently skip them, and ``None`` means "not
    created", which is itself a problem.
    """
    if not isinstance(facts, dict):
        return ['%s: the probe emitted no facts' % label]
    problems = []
    sandbox_home = str(Path(execdir) / 'home')
    work = str(Path(execdir) / 'work')
    dirs = facts.get('dirs')
    targets = facts.get('targets')
    if not isinstance(dirs, dict):
        problems.append('%s: the probe reported no directory facts' % label)
        dirs = {}
    if not isinstance(targets, dict) or any(not isinstance(value, str)
                                            for value in targets.values()):
        problems.append('%s: the probe reported no launcher-target facts' % label)
        targets = {}
    elif len(targets) < PROBE_MIN_TARGETS:
        problems.append('%s: the probe reported an empty launcher-target fact set'
                        % label)
    if not isinstance(facts.get('find'), list) \
            or any(not isinstance(item, str) for item in facts['find']):
        problems.append('%s: the probe reported no name-scan fact' % label)
    if not isinstance(facts.get('mounts'), list) \
            or any(not isinstance(item, str) for item in facts['mounts']):
        problems.append('%s: the probe reported no mountinfo fact' % label)
    if not str(facts.get('cwd') or ''):
        problems.append('%s: the probe reported no cwd' % label)
    if not str(facts.get('home') or ''):
        problems.append('%s: the probe reported no HOME' % label)
    if facts.get('find_rc') is None:
        problems.append('%s: the probe reported no find result' % label)
    elif facts.get('find_rc') not in (0, 1):
        problems.append('%s: find exited rc=%r' % (label, facts.get('find_rc')))
    pid1 = str(facts.get('pid1') or '').strip()
    if not pid1:
        problems.append('%s: the probe reported no PID 1 command line' % label)
    elif 'bwrap' not in pid1:
        problems.append('%s: PID 1 is not bwrap: %r' % (label, pid1[:80]))
    if inspect_rc not in (0, 1):
        problems.append('%s: the in-sandbox inspection exited rc=%r' % (label, inspect_rc))
    if facts.get('cwd') != work:
        problems.append('%s: subject cwd is %r, not the neutral work dir'
                        % (label, facts.get('cwd')))
    if facts.get('home') != sandbox_home:
        problems.append('%s: subject HOME is %r, not the sandbox home'
                        % (label, facts.get('home')))
    for path in PROBE_DIRS:
        info = _dir_fact(facts, path, label, problems)
        if info is None:
            continue
        if info['missing']:
            problems.append('%s: %s is missing; the tmpfs was not mounted' % (label, path))
        elif info['entries']:
            problems.append('%s: %s carries %d host entry/entries (%s)'
                            % (label, path, len(info['entries']),
                               ', '.join(info['entries'][:3])))
    info = _dir_fact(facts, sandbox_home, label, problems)
    if info is not None and info['missing']:
        problems.append('%s: the sandbox HOME is missing' % label)
    subject = _dir_fact(facts, str(SUBJECT_ROOT), label, problems)
    if subject is not None:
        if subject['missing']:
            problems.append('%s: %s is not mounted' % (label, SUBJECT_ROOT))
        elif subject['entries'] != [Path(execdir).name]:
            problems.append('%s: %s lists %r; only the own sandbox may be visible'
                            % (label, SUBJECT_ROOT, subject['entries']))
    hits = list(facts.get('find') or [])
    if label == 'baseline':
        if hits:
            problems.append('baseline: the find probe hit %r' % (hits[0],))
    else:
        root = str(skill_root) if skill_root else ''
        outside = [hit for hit in hits
                   if not root or not (hit == root or hit.startswith(root + '/'))]
        if outside:
            problems.append('%s: the find probe hit a path outside the installed '
                            'candidate skill: %r' % (label, outside[0]))
        if not hits and root:
            problems.append('%s: the installed candidate skill is not visible' % label)
    for path, state in sorted(targets.items()):
        if state != 'ok':
            problems.append('%s: launcher target %s is not readable inside the sandbox'
                            % (label, path))
    if expected_targets is not None:
        for target in expected_targets:
            key = str(target)
            if key not in targets:
                problems.append('%s: launcher target %s was not reported by the '
                                'inspection' % (label, key))
    # R8: mount/pid metadata only counts as a leak when the repository is either
    # provably visible (child fact) or not provably invisible (no fact at all).
    # A mount line that merely *names* a path the child cannot traverse is not a
    # leak; the auditor's read-only sandbox exposed the repo path in mountinfo
    # while the child could not reach it.
    repo = facts.get('repo')
    repo_visible = None
    if isinstance(repo, dict) and repo:
        repo_visible = any(bool(value) for value in repo.values())
    if repo_visible:
        problems.append('%s: the repository is visible inside the sandbox' % label)
    if 'dbus' in facts:
        if facts.get('dbus') is None:
            problems.append('%s: the /run/dbus system-bus canary was not reported'
                            % label)
        elif facts.get('dbus'):
            problems.append('%s: the child connected to the host system bus '
                            '(/run/dbus system_bus_socket)' % label)
    def names_repo(text):
        # the real checkout path counts whatever the directory is called; the project
        # name stays as a heuristic for paths the plan did not compute. The path is
        # compared as whole components, so /x/proj-copy does not match /x/proj.
        text = str(text)
        if 'longhaul' in text.lower() or '\u8fdc\u822a' in text:
            return True
        return re.search(re.escape(str(REPO_ROOT)) + r'(?=$|[/,\s])', text) is not None

    if repo_visible is not False:
        for mount in facts.get('mounts') or []:
            if names_repo(mount):
                problems.append('%s: /proc/self/mountinfo names the repository: %r'
                                % (label, str(mount)[:120]))
                break
        if names_repo(pid1):
            problems.append('%s: /proc/1/cmdline names the repository' % label)
    _canary_problem(facts, 'host', host_canary, label, problems)
    _canary_problem(facts, 'sock', socket_canary, label, problems)
    _canary_problem(facts, 'net', net_canary, label, problems)
    return problems


def _probe_canaries(host_home=None):
    """A host file, a host unix socket and a host TCP listener the child must miss.

    R7: the canaries live in one fresh private directory under /tmp. That path is
    never a bind source of the plan (the offline state may itself be the harness
    HOME -- binding it must not expose a canary), it is short enough for
    AF_UNIX, and the plan masks /tmp with a tmpfs, so the child can neither see
    nor connect to them. Every creation failure is a *dirty* probe with an
    explicit problem, never ``None``-passed as if the check had succeeded.
    """
    try:
        folder = tempfile.mkdtemp(prefix='lhcan-', dir='/tmp')
    except OSError as exc:
        empty = {'host': None, 'sock': None, 'net': None, '_dir': None}
        return empty, [], ['the canary directory cannot be created: %s' % exc]
    token = os.urandom(8).hex()
    paths = {'host': str(Path(folder) / ('canary-%s.txt' % token)),
             'sock': str(Path(folder) / 'c.sock'), 'net': None, '_dir': folder}
    problems = []
    try:
        Path(paths['host']).write_text('CANARY-PROBE-HOST-%s\n' % token, encoding='utf-8')
    except OSError as exc:
        paths['host'] = None
        problems.append('the host canary cannot be created: %s' % exc)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(paths['sock'])
        listener.listen(1)
    except OSError as exc:
        listener = None
        paths['sock'] = None
        problems.append('the unix-socket canary cannot be created: %s' % exc)
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(('127.0.0.1', 0))
        tcp.listen(1)
        paths['net'] = '127.0.0.1:%d' % tcp.getsockname()[1]
    except OSError as exc:
        tcp = None
        problems.append('the TCP canary cannot be created: %s' % exc)
    return paths, [handle for handle in (listener, tcp) if handle is not None], problems


def _close_canaries(paths, handles):
    for handle in handles:
        try:
            handle.close()
        except OSError:
            pass
    for key in ('sock', 'host'):
        if paths.get(key):
            try:
                os.unlink(paths[key])
            except OSError:
                pass
    folder = paths.get('_dir')
    if folder:
        try:
            os.rmdir(folder)
        except OSError:
            pass


def run_probe(subject, host_home, execdir, label='baseline', skill_tree=None,
              profile_name=None, state=None, network=False, bwrap=None, call=None,
              timeout=120, inspect_timeout=300, canaries=True):
    """Run the real probe through the same plan ``run_one`` uses; return the record.

    The child reports its HOME, cwd, PID 1, the contents of /tmp, /run/user and
    the sandbox root, every launcher chain target, the candidate skill hit and
    the host/socket/network canaries. ``state`` selects the offline lane
    (verified double + key-free state); without it the probe uses the live
    launcher chain and still starts no model, reads no key and sends nothing.
    """
    if call is None:
        call = _popen
    execdir = Path(execdir)
    work = execdir / 'work'
    work.mkdir(parents=True, exist_ok=True)
    (execdir / 'home' / 'tmp').mkdir(parents=True, exist_ok=True)
    record = {'argv': None, 'inspect_argv': None, 'rc': None, 'inspect_rc': None,
              'stdout': '', 'stderr': '', 'version': '', 'inspect': '', 'facts': {},
              'skill_root': None, 'roots': [], 'network': bool(network),
              'label': label, 'canaries': {'host': None, 'sock': None, 'net': None},
              'problems': []}
    if skill_tree is not None:
        if not profile_name:
            record['problems'].append('%s: a skill tree needs a profile name' % label)
            return record
        record['skill_root'] = install_skill(
            skill_tree, execdir / 'home' / '.hermes' / 'profiles' / profile_name)
    token = subject if isinstance(subject, VerifiedDouble) else None
    live_argv = list(subject.argv) if token is not None else [str(part) for part in subject]
    if token is not None or state is not None:
        # R8: the plan takes the token (or an argv, turned into a token once);
        # the documented order is (execdir, token, host_home, state).
        plan, exec_argv, errors = offline_plan(execdir, token or subject, host_home,
                                               state, call=call, bwrap=bwrap,
                                               timeout=timeout,
                                               sandbox_home=execdir / 'home')
    else:
        exec_argv = live_argv
        plan, errors = _live_plan(live_argv, host_home, execdir, call, work,
                                  bwrap=bwrap, timeout=timeout,
                                  sandbox_home=execdir / 'home', network=network)
    if errors or plan is None:
        record['problems'].extend('%s: %s' % (label, item) for item in errors)
        record['stderr'] = '; '.join(errors)[:2000]
        return record
    record['roots'] = list(plan.chain.roots)
    record['network'] = bool(plan.network)
    version_argv = plan.wrap(exec_argv + ['--version'])
    record['argv'] = version_argv
    probe = call(version_argv, cwd=str(work), env=plan.env, timeout=timeout)
    record['rc'] = probe.rc
    record['stdout'] = (probe.stdout or '')[-4000:]
    record['stderr'] = (probe.stderr or '')[-2000:]
    record['version'] = ((probe.stdout or '').strip().splitlines() or [''])[0]
    if probe.rc != 0:
        record['problems'].append('%s: the entry --version exited rc=%s' % (label, probe.rc))
    if not record['version']:
        record['problems'].append('%s: empty version' % label)
        return record
    # R7: the canaries exist on the host BEFORE the subject starts, and a
    # disabled or uncreatable canary is a dirty probe (mutant X5).
    if not canaries:
        record['problems'].append('%s: canaries were disabled for this probe' % label)
        paths, handles = {'host': None, 'sock': None, 'net': None}, []
    else:
        paths, handles, canary_problems = _probe_canaries()
        record['problems'].extend('%s: %s' % (label, item) for item in canary_problems)
    record['canaries'] = {key: paths.get(key) for key in ('host', 'sock', 'net')}
    shell = '/bin/bash' if Path('/bin/bash').is_file() else '/bin/sh'
    try:
        inner = [shell, '-c', probe_inspect_script(plan.chain, record['skill_root'], paths)]
        inspect_argv = plan.wrap(inner)
        record['inspect_argv'] = inspect_argv
        seen = call(inspect_argv, cwd=str(work), env=plan.env, timeout=inspect_timeout)
    finally:
        _close_canaries(paths, handles)
    record['inspect_rc'] = seen.rc
    record['inspect'] = (seen.stdout or '')[-20000:]
    record['facts'] = parse_probe(seen.stdout or '')
    if 'dbus' not in record['facts']:
        record['problems'].append('%s: the /run/dbus system-bus canary was not reported'
                                  % label)
    if not record['facts'].get('repo'):
        record['problems'].append('%s: the repository visibility was not reported'
                                  % label)
    record['problems'].extend(probe_problems(
        record['facts'], label, host_home, execdir, record['skill_root'],
        inspect_rc=seen.rc, expected_targets=list(plan.chain.targets),
        host_canary=paths.get('host'), socket_canary=paths.get('sock'),
        net_canary=paths.get('net')))
    return record


# --------------------------------------------------------------------------- #
# subprocess plumbing
# --------------------------------------------------------------------------- #


class Proc:
    """The slice of subprocess.CompletedProcess this module needs."""

    __slots__ = ('rc', 'stdout', 'stderr', 'timed_out', 'stopped', 'pgid')

    def __init__(self, rc, stdout, stderr, timed_out=False, stopped=False, pgid=None):
        self.rc = rc
        self.stdout = stdout or ''
        self.stderr = stderr or ''
        self.timed_out = bool(timed_out)
        self.stopped = bool(stopped)
        self.pgid = pgid


def _kill_group(process):
    if os.name == 'posix':
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _drain(process):
    try:
        return process.communicate(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        return '', ''


def _popen(argv, cwd=None, env=None, timeout=None, on_spawn=None, stop=None):
    """Run argv; a timeout or the stop event kills the whole process group."""
    if stop is None:
        stop = getattr(_ledger, 'ACTIVE_STOP', None)
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True,
              'encoding': 'utf-8', 'errors': 'replace', 'cwd': cwd, 'env': env}
    if os.name == 'posix':
        kwargs['start_new_session'] = True
    process = subprocess.Popen([str(part) for part in argv], **kwargs)
    pgid = process.pid if os.name == 'posix' else None
    if on_spawn is not None:
        try:
            on_spawn(process.pid)
        except Exception:
            pass
    deadline = time.monotonic() + float(timeout or 600)
    timed_out = stopped = False
    out = err = ''
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_group(process)
            out, err = _drain(process)
            break
        try:
            out, err = process.communicate(timeout=min(0.25, remaining))
            break
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                stopped = True
                _kill_group(process)
                out, err = _drain(process)
                break
    return Proc(process.returncode, out, err, timed_out, stopped, pgid)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _tail(text, limit=400):
    return (text or '').strip()[-limit:]


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                          encoding='utf-8')


def _check_profile_name(name):
    if not isinstance(name, str) or not re.fullmatch(PROFILE_RE, name):
        raise ValueError('refusing profile name %r: it does not match %s' % (name, PROFILE_RE))


def _move_path(src, dst):
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    try:
        os.replace(str(src), str(dst))
    except OSError:
        shutil.move(str(src), str(dst))


# --------------------------------------------------------------------------- #
# profile lifecycle
# --------------------------------------------------------------------------- #


def _profile_call_wrap(plan, execdir):
    """Profile create/delete enter the execdir itself, which always exists.

    ``<execdir>/work`` may already be gone (a consumed run) or not yet created;
    bwrap refuses to start when ``--chdir`` names a missing directory.
    """
    if plan is None:
        return None
    return lambda inner: plan.wrap(inner, chdir=str(execdir))


def create_profile(hermes, name, ledger, env, call=_popen, wrap=None, **fields):
    """Create one empty profile; recorded in the ledger before it can leak."""
    _check_profile_name(name)
    argv = list(hermes) + ['profile', 'create', name, '--no-skills', '--no-alias']
    if wrap is not None:
        argv = wrap(argv)
    proc = call(argv, cwd=None, env=env, timeout=180)
    if proc.timed_out:
        raise RuntimeError('profile create %s timed out' % name)
    if proc.rc != 0:
        raise RuntimeError('profile create %s failed (rc=%s): %s'
                           % (name, proc.rc, _tail(proc.stderr)))
    ledger.append(dict({'event': 'profile_created', 'profile': name}, **fields))
    return name


def delete_profile(hermes, name, ledger, env, call=_popen, wrap=None, **fields):
    """Delete a live profile this ledger created. Refuses everything else (A8)."""
    _check_profile_name(name)
    if name not in ledger.created_profiles():
        raise ValueError('refusing to delete %r: the ledger does not record creating it' % name)
    if name not in ledger.live_profiles():
        return False
    argv = list(hermes) + ['profile', 'delete', name, '--yes']
    if wrap is not None:
        argv = wrap(argv)
    proc = call(argv, cwd=None, env=env, timeout=180)
    if proc.timed_out:
        raise RuntimeError('profile delete %s timed out' % name)
    if proc.rc != 0:
        raise RuntimeError('profile delete %s failed (rc=%s): %s'
                           % (name, proc.rc, _tail(proc.stderr)))
    ledger.append(dict({'event': 'profile_deleted', 'profile': name}, **fields))
    return True


# --------------------------------------------------------------------------- #
# interruption and reaping
# --------------------------------------------------------------------------- #


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def process_start(pid, proc_root=Path('/proc')):
    """Field 22 (starttime) of /proc/<pid>/stat, or None when unreadable."""
    try:
        raw = (Path(proc_root) / str(pid) / 'stat').read_text(encoding='utf-8',
                                                             errors='replace')
    except OSError:
        return None
    tail = raw.rpartition(')')[2].split()
    if len(tail) < 20:
        return None
    return tail[19]


def boot_id(proc_root=Path('/proc')):
    try:
        return (Path(proc_root) / 'sys' / 'kernel' / 'random' / 'boot_id').read_text(
            encoding='utf-8', errors='replace').strip() or None
    except OSError:
        return None


def harness_identity(proc_root=Path('/proc')):
    """The current harness process, verifiable across PID reuse and reboots."""
    pid = os.getpid()
    return {'pid': pid, 'start': process_start(pid, proc_root), 'boot': boot_id(proc_root)}


def owner_state(owner, proc_root=Path('/proc')):
    """``dead`` | ``live`` | ``unknown`` for a recorded harness identity.

    Unknown is the default: a row without a complete, verifiable owner never
    authorises a signal. A different boot id means the owner cannot be alive; a
    matching pid with a different starttime is a reused pid (the owner is gone).
    """
    if not isinstance(owner, dict):
        return 'unknown'
    pid, start, boot = owner.get('pid'), owner.get('start'), owner.get('boot')
    if not isinstance(pid, int) or not start or not boot:
        return 'unknown'
    current_boot = boot_id(proc_root)
    if current_boot is None:
        return 'unknown'
    if boot != current_boot:
        return 'dead'
    if not _alive(pid):
        return 'dead'
    current_start = process_start(pid, proc_root)
    if current_start is None:
        return 'unknown'
    return 'live' if current_start == start else 'dead'


def _cmdline_matches(pid, profile, proc_root=Path('/proc'), expected_start=None):
    if not profile:
        return False
    try:
        raw = (Path(proc_root) / str(pid) / 'cmdline').read_bytes()
    except OSError:
        return False
    text = raw.replace(b'\0', b' ').decode('utf-8', 'replace')
    if profile not in text:
        return False
    if expected_start:
        current = process_start(pid, proc_root)
        if current is None or str(current) != str(expected_start):
            return False
    return True


def _kill_pid_group(pgid):
    try:
        if os.name == 'posix':
            os.killpg(os.getpgid(int(pgid)), signal.SIGKILL)
        else:  # pragma: no cover - Windows
            os.kill(int(pgid), signal.SIGTERM)
    except OSError:
        pass


def _canonical_sandbox(path_value):
    """Only ``<SUBJECT_ROOT>/<20 hex>`` is ever reaped."""
    try:
        path = Path(path_value)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute():
        return None
    try:
        if path.resolve().parent != SUBJECT_ROOT.resolve():
            return None
    except OSError:
        return None
    if not re.fullmatch(r'[0-9a-f]{20}', path.name):
        return None
    return path


def _stale_keys(rows):
    """Keys whose last ``started``/``finished`` row is a ``started``.

    The old predicate (``last_finished(key) is not None``) hid a retry attempt
    that started after a finished crash: that process stayed alive and its
    sandbox (with the model key) stayed on disk (R3 MAJOR).
    """
    last = {}
    for row in rows:
        key = row.get('key')
        event = row.get('event')
        if not key or event not in ('started', 'finished'):
            continue
        last[key] = (event, row)
    return [key for key, (event, _row) in last.items() if event == 'started']


def _unstarted_attempts(rows):
    """``attempt_dir`` rows whose own ``(key, attempt)`` never started or finished."""
    done = {(row.get('key'), row.get('attempt')) for row in rows
            if row.get('event') in ('started', 'finished')}
    seen, out = set(), []
    for row in rows:
        if row.get('event') != 'attempt_dir':
            continue
        key = row.get('key')
        attempt = row.get('attempt')
        if not key or (key, attempt) in done or (key, attempt) in seen:
            continue
        seen.add((key, attempt))
        out.append((key, row))
    return out


def _live_reference(path, proc_root=Path('/proc'), exclude_pgids=()):
    """True when a live process other than the harness still names ``path``."""
    text = str(path)
    if not text:
        return False
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                'utf-8', 'replace')
        except OSError:
            continue
        if text not in cmdline:
            continue
        try:
            pgid = os.getpgid(int(entry.name))
        except OSError:
            pgid = None
        if pgid is not None and pgid in exclude_pgids:
            continue
        return True
    return False


def sandbox_in_use(path, proc_root=Path('/proc')):
    """Public guard: never delete a sandbox another live process still uses."""
    return _live_reference(path, proc_root)


def owned_sandboxes(ledger):
    """Canonical sandboxes this ledger recorded, before or during a run."""
    out = []
    for row in ledger.rows():
        if row.get('event') not in ('started', 'attempt_dir'):
            continue
        sandbox = _canonical_sandbox(row.get('execdir'))
        if sandbox is not None and sandbox not in out:
            out.append(sandbox)
    return out


def _reap_sandbox(sandbox, killed_pgids=(), proc_root=Path('/proc')):
    if sandbox is None or _live_reference(sandbox, proc_root, killed_pgids):
        return False
    shutil.rmtree(str(sandbox), ignore_errors=True)
    return not sandbox.exists()


def reap_stale(ledger, proc_root=Path('/proc')):
    """Kill and clear attempts whose harness died between ``started`` and ``finished``.

    Three windows are covered: an attempt killed after ``started``, a retry
    started after an earlier finished crash, and a harness killed after
    ``attempt_dir`` but before ``started``. Only a canonical sandbox with an
    immediate ``SUBJECT_ROOT`` parent is touched, only a process whose cmdline
    still carries the ledger profile name is killed, and a directory another
    live process still references is left alone. A reaped profile gets a
    ledger-only ``profile_deleted`` event; host Hermes is never invoked.
    """
    rows = ledger.rows()
    reaped = []
    for key in _stale_keys(rows):
        last = ledger.last(key) or {}
        if last.get('event') != 'started':
            continue
        pgid = last.get('pgid')
        profile = str(last.get('profile') or '')
        killed = set()
        state = owner_state(last.get('owner'), proc_root)
        if state == 'live':
            # A verifiably live harness still owns this attempt: never signal,
            # delete or re-label it from another invocation.
            continue
        # A signal needs a verified-dead harness owner *and* positive proof that
        # the process group is our subject: the ledger profile in its cmdline
        # (and, when recorded, a matching starttime, so PID reuse is a refusal).
        if (state == 'dead' and pgid and _alive(pgid)
                and _cmdline_matches(pgid, profile, proc_root, last.get('pgid_start'))):
            _kill_pid_group(pgid)
            killed.add(int(pgid))
        sandbox = _canonical_sandbox(last.get('execdir'))
        removed = _reap_sandbox(sandbox, killed, proc_root)
        errors = ['reaped after an interrupted harness']
        if not killed and state != 'dead':
            errors.append('no verified dead owner; no signal was sent')
        ledger.append({'event': 'finished', 'key': key, 'cond': last.get('cond'),
                       'case': last.get('case'), 'trial': last.get('trial'),
                       'profile': profile or None, 'attempt': last.get('attempt'),
                       'status': 'interrupted', 'rc': None, 'wall_s': None,
                       'errors': errors})
        if removed and profile and profile in ledger.live_profiles():
            ledger.append({'event': 'profile_deleted', 'profile': profile,
                           'key': key, 'reaped': True})
        reaped.append(key)

    for key, row in _unstarted_attempts(rows):
        profile = str(row.get('profile') or '')
        sandbox = _canonical_sandbox(row.get('execdir'))
        removed = _reap_sandbox(sandbox, (), proc_root)
        ledger.append({'event': 'finished', 'key': key, 'cond': row.get('cond'),
                       'case': row.get('case'), 'trial': row.get('trial'),
                       'profile': profile or None, 'attempt': row.get('attempt'),
                       'status': 'interrupted', 'rc': None, 'wall_s': None,
                       'errors': ['reaped before the subject started']})
        if removed and profile and profile in ledger.live_profiles():
            ledger.append({'event': 'profile_deleted', 'profile': profile,
                           'key': key, 'reaped': True})
        reaped.append(key)
    return reaped


# --------------------------------------------------------------------------- #
# one run
# --------------------------------------------------------------------------- #


def _run_key(spec):
    return '%s/%s/t%s' % (spec.cond, spec.case, spec.trial)


def _live_plan(hermes, real_home, execdir, call, workdir, bwrap=None, timeout=120,
               sandbox_home=None, network=True, extra_binds=()):
    """Resolve the chain and prove it inside bwrap; ``[]`` errors means go."""
    try:
        plan = plan_subject(execdir, hermes, real_home, env=os.environ, bwrap=bwrap,
                            sandbox_home=sandbox_home, network=network,
                            extra_binds=extra_binds)
    except LauncherError as exc:
        return None, ['S1: %s' % exc]
    version = call([plan.bwrap, '--version'], cwd=None, env=scrub_env(os.environ),
                   timeout=timeout)
    if version.rc != 0 or not (version.stdout or '').strip():
        return None, ['S1: bwrap --version failed (rc=%s)' % version.rc]
    probe = call(plan.wrap(list(hermes) + ['--version']), cwd=str(workdir),
                 env=plan.env, timeout=timeout)
    if probe.rc != 0 or not (probe.stdout or '').strip():
        return None, ['S1: hermes does not start inside the sandbox '
                      '(version probe rc=%s)' % probe.rc]
    return plan, []


def _attempt_recorded(ledger, key, execdir):
    """True when the caller already recorded this exact sandbox (run_evals does)."""
    try:
        rows = ledger.rows()
    except (OSError, ValueError):
        return False
    for row in reversed(rows):
        if row.get('event') == 'attempt_dir' and row.get('key') == key:
            return str(row.get('execdir')) == str(execdir)
    return False


def run_one(spec, hermes, ledger, home, skill_tree, *, mode=MODE_OFFLINE, bwrap=None,
            state=None, token=None, call=_popen, timeout_s=None, keep_profile=None,
            stop=None, sandbox_home=None):
    """Run one subject and return its meta record.

    ``mode`` is one of ``MODE_OFFLINE`` / ``MODE_LIVE_BLOCKED``. The live lane is
    refused here, first, before a profile, a key or a process -- a direct Python
    call is as fail-closed as the CLI, and no runner API reads, copies or accepts
    key material at all. The offline lane accepts only the canonical verified
    double (direct entry or ``python <double>``), binds one key-free state
    directory, and runs every Hermes call (create, skills list, chat, delete)
    inside one runner-owned SandboxPlan with ``--unshare-net``. ``bwrap=False``
    is the explicit deterministic test seam; it is never inferred and never
    selected by a file name.
    """
    if mode == MODE_LIVE_BLOCKED:
        raise LiveBlocked('a live subject run is disabled: the model credential cannot '
                          'be proven unreachable from an untrusted subject yet')
    if mode not in MODES:
        raise ValueError('run_one needs an explicit mode, got %r' % (mode,))
    if bwrap is False and os.environ.get(TEST_SEAM_ENV) != '1':
        # R7: the deterministic seam is an explicit environment decision. A
        # caller (including a direct Python call) cannot switch the sandbox off
        # by passing False, and nothing is inferred from a file name or a flag.
        raise LauncherError('bwrap=False is a test seam; set %s=1 to enable it'
                            % TEST_SEAM_ENV)
    if token is None and not hermes:
        raise ValueError('run_one needs an explicit hermes argv; there is no default binary')
    if token is None:
        token = verified_double(hermes, state)
    elif not isinstance(token, VerifiedDouble):
        raise ValueError('run_one token must be a VerifiedDouble')
    if bwrap is True or bwrap is None:
        bwrap = BWRAP
    host_home = Path(home) if home else Path(os.environ.get('HOME') or Path.home())
    rundir = Path(spec.rundir)
    key = _run_key(spec)
    attempt = ledger.attempts(key) + 1
    execdir = Path(spec.execdir) if spec.execdir else neutral_execdir(key, attempt)
    sandbox_home = Path(sandbox_home) if sandbox_home is not None else execdir / 'home'
    real_home = host_home
    if real_home.resolve() == sandbox_home.resolve():
        real_home = Path(os.environ.get('HOME') or Path.home())
    workdir = execdir / 'work'
    prompt_path = execdir / 'prompt.md'
    name = profile_name(spec.run_id, spec.cond, spec.case, spec.trial)
    profile_dir = sandbox_home / '.hermes' / 'profiles' / name
    with_skill = spec.cond != 'baseline'
    keep = KEEP_PROFILES if keep_profile is None else bool(keep_profile)

    # The sandbox is recorded before anything is written into it: a SIGKILL
    # between here and the first `started` row must still leave an owned,
    # reapable directory (R3 MAJOR, pre-start window).
    if not _attempt_recorded(ledger, key, execdir):
        ledger.append({'event': 'attempt_dir', 'key': key, 'cond': spec.cond,
                       'case': spec.case, 'trial': spec.trial, 'profile': name,
                       'attempt': attempt, 'execdir': str(execdir),
                       'owner': harness_identity()})
    execdir.mkdir(parents=True, exist_ok=True)
    sandbox_home.mkdir(parents=True, exist_ok=True)
    (sandbox_home / 'tmp').mkdir(parents=True, exist_ok=True)
    rundir.mkdir(parents=True, exist_ok=True)
    if not workdir.exists():
        legacy = rundir / 'work'
        if legacy.is_dir():
            _move_path(legacy, workdir)
        else:
            workdir.mkdir(parents=True, exist_ok=True)
    if not prompt_path.exists() and spec.prompt:
        prompt_path.write_text(spec.prompt, encoding='utf-8')

    # No credential exists in this lane: there is nothing to read, copy into a
    # profile, inherit or redact (R6).
    secrets = []
    entry_parent = Path(token.source).parent if os.path.isabs(token.source) else None
    child_env = subject_env(os.environ, sandbox_home, extra_path=entry_parent)
    fp = tree_fingerprint(skill_tree) if skill_tree else None

    started_at = _now()
    t0 = time.monotonic()
    record = {'status': None, 'rc': None, 'wall_s': 0.0, 'attempt': attempt,
              'profile': name, 'skill_fp': fp, 'session_id': None,
              'started_at': started_at, 'finished_at': None, 'error': None,
              'mode': mode, 'skill_requested': with_skill, 'skill_enabled': False,
              'skill_preloaded': False, 'skill_loaded': False}
    row = {'key': key, 'cond': spec.cond, 'case': spec.case, 'trial': spec.trial,
           'profile': name, 'attempt': attempt}
    _write_json(rundir / 'manifest.before.json', manifest(workdir))
    state_before = workdir / STATE_REL
    # A subject-planted symlink or FIFO at .task/state.json must not be followed
    # (and must not block); only a verified regular file is copied out.
    before_bytes = _read_regular(state_before)
    if before_bytes is not None:
        (rundir / 'state.before.json').write_bytes(before_bytes)

    stdout = stderr = ''
    plan = None
    exec_argv = None
    try:
        setup_errors = []
        if bwrap is False:
            # The deterministic seam still executes the token bytes, never the
            # caller's path: the copy is the only executable in both lanes.
            try:
                exec_argv, _digest, _copy = _attempt_double(execdir, token)
            except DoubleRejected as exc:
                setup_errors = ['S0: %s' % exc]
        else:
            plan, exec_argv, setup_errors = offline_plan(
                execdir, token, real_home, state, call=call, bwrap=bwrap,
                sandbox_home=sandbox_home)
        if setup_errors:
            record.update(status='preflight_fail', error='; '.join(setup_errors)[:2000])
            ledger.append(dict({'event': 'preflight', 'status': 'preflight_fail',
                                'errors': setup_errors}, **row))
        else:
            created = False
            try:
                create_profile(exec_argv, name, ledger, child_env, call=call,
                               wrap=_profile_call_wrap(plan, execdir), **row)
                created = True
            except Exception as exc:
                record.update(status='hermes_crash',
                              error='profile create failed: %s' % _tail(str(exc)))
            if created:
                run_env = plan.env if plan else child_env
                preflight_errors = []
                if with_skill:
                    if skill_tree is None:
                        preflight_errors.append(
                            'P3: condition %s needs a skill tree; none supplied' % spec.cond)
                    else:
                        install_skill(skill_tree, profile_dir)

                skills_out = ''
                if not preflight_errors:
                    listed_argv = exec_argv + ['-p', name, 'skills', 'list', '--enabled-only']
                    if plan:
                        listed_argv = plan.wrap(listed_argv)
                    listed = call(listed_argv, cwd=str(workdir), env=run_env,
                                  timeout=120)
                    skills_out = listed.stdout or ''
                    if listed.timed_out:
                        preflight_errors.append('P3b: skills list timed out')
                    elif listed.rc != 0:
                        preflight_errors.append('P3b: skills list exited rc=%s'
                                                % listed.rc)
                    else:
                        record['skill_enabled'] = bool(re.search(
                            r'(?<![A-Za-z0-9_])longhaul(?![A-Za-z0-9_])',
                            skills_out, re.IGNORECASE))
                        # Preload by argv is a separate, weaker signal than a
                        # runtime confirmation; it never becomes 'confirmed'.
                        record['skill_preloaded'] = bool(with_skill
                                                         and record['skill_enabled'])

                if not preflight_errors:
                    preflight_errors.extend(preflight(profile_dir, spec.cond, fp, workdir,
                                                      REPO_ROOT, run_env, skills_out))
                    if not re.fullmatch(PROFILE_RE, name) \
                            or name not in ledger.created_profiles():
                        preflight_errors.insert(
                            0, 'P1: %s is not a ledger-created lheval profile' % name)

                argv = build_command(exec_argv, name, spec, with_skill)
                if '-m' not in argv or '--provider' not in argv:
                    preflight_errors.append('P10: argv lacks -m or --provider')
                preflight_errors.extend(p11_errors({
                    'cwd': str(workdir), 'query-file': str(prompt_path),
                    'profile': name, 'profile home': str(profile_dir),
                    'chat argv': argv,
                    'prompt': spec.prompt or '',
                    'env': ' '.join(str(value) for value in run_env.values()),
                    'worktree': ' '.join(sorted(manifest(workdir)))},
                    cond=spec.cond))

                ledger.append(dict({'event': 'preflight',
                                    'status': 'preflight_fail' if preflight_errors
                                    else 'ok',
                                    'errors': preflight_errors}, **row))
                if preflight_errors:
                    record.update(status='preflight_fail',
                                  error='; '.join(preflight_errors)[:2000])
                else:
                    command = plan.wrap(argv) if plan else argv
                    _write_json(rundir / 'cmd.json',
                                {'argv': command, 'inner': argv, 'cwd': str(workdir),
                                 'env_keys': sorted(run_env), 'mode': mode,
                                 'network': False if plan else None,
                                 'double_digest': token.digest,
                                 'wrapped': bool(plan)})

                    def on_spawn(pid):
                        ledger.append(dict(row, event='started', pgid=pid,
                                           execdir=str(execdir),
                                           owner=harness_identity(),
                                           pgid_start=process_start(pid)))

                    try:
                        proc = call(command, cwd=str(workdir), env=run_env,
                                    timeout=timeout_s or (spec.budget_s + 60),
                                    on_spawn=on_spawn, stop=stop)
                    except KeyboardInterrupt:
                        record.update(status='interrupted', error='interrupted')
                        raise
                    stdout = proc.stdout or ''
                    stderr = proc.stderr or ''
                    status, parsed = classify(proc.rc, stdout, stderr, proc.timed_out)
                    if getattr(proc, 'stopped', False):
                        status = 'interrupted'
                    result = parsed['result'] or {}
                    record.update(status=status, rc=proc.rc,
                                  session_id=result.get('session_id'))
                    if with_skill and status == 'ok':
                        record['skill_loaded'] = (
                            'confirmed' if skill_confirmation(parsed['events'])
                            else 'unconfirmed')
                    if result.get('error'):
                        record['error'] = str(result['error'])
                    elif status != 'ok':
                        record['error'] = (_tail(stderr)
                                           or 'bad stream lines: %d' % parsed['bad_lines'])
    finally:
        record['wall_s'] = round(time.monotonic() - t0, 3)
        record['finished_at'] = _now()
        if record['error']:
            record['error'] = redact(record['error'], secrets)
        _write_json(rundir / 'manifest.after.json', manifest(workdir))
        if record['status'] is not None:
            (rundir / 'events.jsonl').write_text(sanitise_stream(stdout, secrets),
                                                 encoding='utf-8')
            (rundir / 'stderr.txt').write_text(redact(stderr, secrets), encoding='utf-8')
            _write_json(rundir / 'meta.json', record)
            ledger.append(dict({'event': 'finished', 'status': record['status'],
                                'rc': record['rc'], 'wall_s': record['wall_s'],
                                'errors': ([record['error']]
                                           if record['status'] == 'preflight_fail'
                                           and record['error'] else [])}, **row))
        # The delete runs before the work tree is consumed: a wrapped delete
        # must still find the directory its --chdir names (R3 MAJOR).
        delete_error = None
        if profile_dir.exists() and not keep and name in ledger.live_profiles():
            try:
                delete_profile(exec_argv, name, ledger, child_env, call=call,
                               wrap=_profile_call_wrap(plan, execdir), **row)
            except Exception as exc:
                delete_error = redact(str(exc), secrets)
        if delete_error:
            ledger.append(dict(row, event='finished', status='hermes_crash',
                               rc=record['rc'], wall_s=record['wall_s'],
                               errors=['profile delete failed: %s' % delete_error],
                               correction=True))
        if record['status'] is not None:
            if workdir.resolve() != (rundir / 'work').resolve():
                _move_path(workdir, rundir / 'work')
            if prompt_path.exists() and prompt_path.resolve() != \
                    (rundir / 'prompt.md').resolve():
                shutil.copyfile(str(prompt_path), str(rundir / 'prompt.md'))
        # The double's own call log lives in the attempt (never in the bound
        # state); keep a copy in the run directory for the record.
        log = execdir / 'fake-log' / 'fake_calls.jsonl'
        if log.is_file():
            try:
                shutil.copyfile(str(log), str(rundir / 'fake-log.jsonl'))
            except OSError:
                pass
        if not keep:
            shutil.rmtree(execdir, ignore_errors=True)
    return record


# --------------------------------------------------------------------------- #
# ledger compatibility surface
# --------------------------------------------------------------------------- #
# The runner and the ledger are one lane, but run_evals imports the runner
# lazily as a single module. Re-exporting the ledger names keeps that call
# surface in one place without a second import path.


def _load_sibling(name):
    full = 'lheval_' + name
    module = sys.modules.get(full)
    if module is not None:
        return module
    path = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(full, path)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load %s' % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_ledger = _load_sibling('ledger')
Ledger = _ledger.Ledger
run_key = _ledger.run_key
pending = _ledger.pending
check_header = _ledger.check_header


def run_matrix(specs, run_fn, ledger, parallel=3, max_attempts=2, backoff_s=60,
               sleep=time.sleep, stop=None):
    """Reap stale attempts first, then run the pending specs (ledger.run_matrix)."""
    reap_stale(ledger)
    return _ledger.run_matrix(specs, run_fn, ledger, parallel=parallel,
                              max_attempts=max_attempts, backoff_s=backoff_s,
                              sleep=sleep, stop=stop)
