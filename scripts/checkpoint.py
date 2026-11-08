#!/usr/bin/env python3
"""Validate Longhaul bookkeeping; never claim to verify substantive truth.

Schema v2. Evidence is bound to artifacts by content-addressed revision, so a
changed file invalidates exactly the criteria that depend on it. Nothing here
can judge whether the work is correct -- only whether the record is coherent.

Revision contract (v1 -> v2, ``migrate``) -- three sources, no precedence::

    G = state.artifact_revision      the record's global revision
    E = evidence.artifact_revision   the revision the observation names
    A = deliverable.revision         only external deliverables carry one

An external observation is trusted exactly when G, E and A are non-empty and
G == E == A. A v1 external deliverable may omit its own ``revision``: v1 has one
global revision, so A falls back to G -- never to a placeholder -- and only a
nonempty G with E == G is trusted. An explicit ``revision`` stays authoritative,
``revision: ""`` is an error, and a deliverable that has neither its own
revision nor a nonempty G is refused with the source bytes unchanged.

Every difference between present values is a CONFLICT: *all* of that
observation's bindings are written as ``""`` (never a label that could collide
with a revision), even the ones that happened to match, together with
``unverified: true``, a deterministic non-empty ``unverified_reason``, the v1
label preserved as ``source_revision`` when E exists, and a warning. A CONFLICT
can never pass the gate and is never migrated silently.

A local observation is trusted exactly when G and E are non-empty, equal, and
the content read through the same no-follow descriptor still matches the v1
``sha256``; the new local revision is that hash's 12-hex prefix. Malformed v1
shapes, v2-reserved nested fields, ambiguous duplicate locators and locators
that are not plain files (symlink component, special file) are refused with
JSON and the source file stays byte for byte unchanged.

Representation: ``unverified`` is optional and its absence means the
observation is verified. When present it must be exactly ``true`` and requires
``unverified_reason``; ``false`` is invalid. The gate refuses an unverified
observation whatever its binding says, and ``source_revision`` is only valid
together with ``unverified: true`` -- it records a label that was *not*
verified.

Field contract: every unknown top-level, deliverable and evidence field keeps
its exact key, type and value (including falsey values and a boolean
``supporting``). Only these mappings may change: v1-local ``sha256 -> sha256``
plus the computed ``revision``; v1-external ``revision -> revision`` (or
``A := G`` when the deliverable has none); v1-evidence
``artifact_revision -> source_revision``; binding fields are re-derived. A
non-boolean or otherwise semantically colliding value is refused before any
write, with the source bytes unchanged.

Input: the document is read as strict JSON. A repeated object key, ``NaN``,
``Infinity``/``-Infinity``, a number that does not fit a finite float, a non-zero
literal that underflows to ``0.0`` in binary64 (``1e-400``), and a string that
contains a lone surrogate (which could never be written back as UTF-8) are refused
before anything is written; every persistence path serialises with
``allow_nan=False``. Other numbers are stored as the nearest IEEE-754 binary64
value. A refusal leaves the source bytes untouched.

Paths: every path this tool reads or writes is walked one component at a time
through a directory descriptor with O_NOFOLLOW on each component, the file
opened O_NONBLOCK and then required to be a regular file (fstat/S_ISREG). A
symlink component, a symlinked final file, a FIFO or any other non-regular file
is refused as JSON within the same call -- never followed, never blocking. A
platform without descriptor-anchored traversal refuses every path operation;
there is no realpath() fallback, which is why v1.2 supports Linux only.

Output: every CLI response goes through one boundary that renames detected
secret-shaped dict keys and masks every detected shape in free text, while
keeping content-hash and revision fields readable.

Writes: atomic whole-file replacement through an exclusive temporary file in the
verified directory. The write is refused if the target changed up to a second
byte and ``(st_dev, st_ino)`` comparison made immediately before ``os.replace``.
The temporary file keeps its held descriptor, so the original mode and
ownership are applied with ``os.fchmod``/``os.fchown`` rather than by name, and
its inode is confirmed through the verified directory's descriptor. That is the
documented NO-LOCK limitation: there is no serialization against a writer that
acts after that second check, and no cross-process lock or CAS protocol --
nothing here promises otherwise.

Two optional additions carry a stop and a grant, both outside the v2 schema so
every existing record keeps validating unchanged::

    report      why the unit stopped, in a word a machine can sort
                (progress | waiting | ready | blocked | failed | dropped)
    <stem>.mandate.json   the immutable authorisation: who asked, on which
                channel, under which policy version, with which scope

A mandate lives in its own file because it never changes while the record is
rewritten on every step; a wider scope is a new mandate under a new id, and
``revalidate`` answers whether the one on record still covers the work. Neither
addition is ever inferred from prose, and neither can be relaxed by prompt text.
"""
import argparse
import errno
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_scan

SCHEMA = 2
STATUS = ('in_progress', 'blocked', 'complete')
CRITERION = ('pending', 'verified', 'failed', 'unobservable')
RESULTS = ('pass', 'fail', 'unknown')
KINDS = ('local', 'external')
# Optional run binding. It records which case/model/input produced an observation
# so a criterion cannot be completed with a byte-identical result from another run.
RUN_FIELDS = frozenset({'case_id', 'model', 'input_sha256'})
CLASSES = ('callable', 'installed_unavailable', 'discovered', 'unavailable')
REPORT_STATES = ('progress', 'waiting', 'ready', 'blocked', 'failed', 'dropped')
REPORT_NEEDS_PERSON = ('ready', 'blocked', 'failed')
MANDATE_STRENGTHS = ('explicit', 'inferred')
MANDATE_REQUIRED = ('id', 'source', 'actor', 'channel', 'policy_version', 'scope')
LISTS = ('constraints', 'milestones', 'acceptance', 'artifacts', 'evidence',
         'blockers', 'attempts', 'decisions', 'capabilities', 'lessons',
         'shortcuts', 'waivers')
SKEW = timedelta(minutes=5)
SCOPE = ('bookkeeping and local hashes only; verify substantive and external '
         'claims separately')
V1_FIELDS = frozenset({'schema_version', 'task_id', 'goal', 'status', 'next_action',
                       'artifact_revision', 'deliverables', 'evidence', 'constraints',
                       'milestones', 'acceptance', 'blockers', 'attempts', 'decisions',
                       'capabilities', 'lessons', 'evaluation_date'})
V2_ONLY = (frozenset(LISTS) | {'provenance'}) - V1_FIELDS
# Fields a v1 record must not already carry: migration would overwrite them or
# reinterpret user metadata with v2 semantics.
EVIDENCE_RESERVED = ('artifact_ids', 'artifact_revisions', 'unverified',
                     'unverified_reason', 'source_revision')
# Only a local deliverable's `revision` is an explicit conflict: the computed
# content revision cannot also be user data. `supporting` is data when boolean.
DELIVERABLE_CONFLICT = ('revision',)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
# Descriptor-anchored traversal needs openat-style calls and a no-follow flag.
# When a platform lacks them there is no safe fallback: every path operation
# refuses (see _walk_parent); realpath() trust is exactly what this replaces.
_CAN_WALK = bool(_NOFOLLOW and _DIRECTORY and _NONBLOCK) and all(
    call in os.supports_dir_fd
    for call in (os.open, os.stat, os.unlink, os.mkdir, os.rename))


class RecordError(Exception):
    """A record this command cannot safely consume; errors are JSON-ready strings."""

    def __init__(self, errors):
        self.errors = [str(error) for error in errors]
        super().__init__('; '.join(self.errors))


class PathIdentityError(Exception):
    """The locator cannot be mapped to a plain file without following a link."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def now():
    return datetime.now(timezone.utc)


def sha256_fd(fd):
    h = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)


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
# strict JSON input: no duplicate keys, no non-finite numbers
# --------------------------------------------------------------------------- #

def _no_duplicate_keys(pairs):
    """object_pairs_hook: a repeated key would silently drop the first value."""
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError('the document repeats an object key; duplicate keys are refused')
        seen.add(key)
    return dict(pairs)


def _reject_constant(token):
    """parse_constant: NaN/Infinity are not JSON and could not be written back."""
    raise ValueError('%s is not valid JSON; non-finite numbers are refused' % token)


def _finite_float(text):
    """parse_float: reject inf and any non-zero literal that underflows to 0.0.

    Numbers are stored as IEEE-754 binary64, so a literal such as ``1e-400`` would
    become 0.0 and silently change the recorded value. A literal whose mantissa is
    all zeros (``0.0``, ``-0.0``, ``0.0e-400``) is a real zero and stays allowed.
    """
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('%s is not a finite number' % text)
    mantissa = text.split('e')[0].split('E')[0]
    if value == 0.0 and any(digit in '123456789' for digit in mantissa):
        raise ValueError('%s underflows to 0.0 in IEEE-754 binary64' % text)
    return value


def _has_lone_surrogate(text):
    """True for a UTF-16 surrogate code point that has no partner in *text*.

    json decodes a valid ``\\ud83d\\ude00`` pair into one astral character, so any
    surviving surrogate is lone. Such a string cannot be encoded as UTF-8, which
    means it could be read but never written back.
    """
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def _contains_surrogate(node):
    """True when any string value or object key carries a lone surrogate."""
    if isinstance(node, str):
        return _has_lone_surrogate(node)
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and _has_lone_surrogate(key):
                return True
            if _contains_surrogate(value):
                return True
        return False
    if isinstance(node, list):
        return any(_contains_surrogate(item) for item in node)
    return False


# --------------------------------------------------------------------------- #
# descriptor-anchored paths: every component is opened without following a link
# --------------------------------------------------------------------------- #

def _reason(exc, part):
    if exc.errno == errno.ELOOP:
        return 'symlink component %r' % part
    if exc.errno == errno.ENOENT:
        return 'missing component %r' % part
    if exc.errno == errno.ENOTDIR:
        return 'not a directory or a symlink: %r' % part
    return '%r: %s' % (part, exc.strerror or exc)


def _file_kind(mode):
    for label, test in (('a directory', stat.S_ISDIR), ('a fifo', stat.S_ISFIFO),
                        ('a socket', stat.S_ISSOCK), ('a character device', stat.S_ISCHR),
                        ('a block device', stat.S_ISBLK)):
        if test(mode):
            return label
    return 'an unknown special file'


def _absolute(raw):
    """One absolute path without resolving links; a relative path joins the cwd."""
    text = os.fspath(raw)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if not isinstance(text, str) or '\x00' in text:
        raise PathIdentityError('not a usable path')
    return text if os.path.isabs(text) else os.path.join(os.getcwd(), text)


def _components(raw):
    parts = [part for part in _absolute(raw).split(os.sep) if part not in ('', '.')]
    if not parts or parts[-1] == '..':
        raise PathIdentityError('not a file path')
    return parts[:-1], parts[-1]


def _walk_parent(raw, create=False):
    """Return (dir_fd, leaf) for the parent of *raw*; no component is followed.

    Every component is opened O_NOFOLLOW|O_DIRECTORY from the previous
    descriptor, so a symlink, a non-directory or (without ``create``) a missing
    step is a refusal instead of a redirect. A platform without descriptor
    anchoring refuses here rather than falling back to path trust.
    """
    if not _CAN_WALK:
        raise PathIdentityError(
            'descriptor-anchored path traversal is unavailable on this platform; '
            'refusing to touch a path it cannot verify')
    components, leaf = _components(raw)
    fd = os.open(os.sep, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for part in components:
            try:
                nfd = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                              dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT and create:
                    try:
                        os.mkdir(part, 0o777, dir_fd=fd)
                        nfd = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                                      dir_fd=fd)
                    except OSError as making:
                        raise PathIdentityError(_reason(making, part))
                elif exc.errno == errno.ENOENT:
                    raise
                else:
                    raise PathIdentityError(_reason(exc, part))
            os.close(fd)
            fd = nfd
        return fd, leaf
    except BaseException:
        os.close(fd)
        raise


def _open_leaf(dir_fd, leaf, flags):
    """Open *leaf* relative to *dir_fd* without following it; require S_ISREG."""
    try:
        fd = os.open(leaf, flags | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise
        raise PathIdentityError(_reason(exc, leaf))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # O_NONBLOCK is what keeps a FIFO from blocking here: it opens at
            # once and is then refused by its file type, never read.
            raise PathIdentityError('not a regular file (%s)' % _file_kind(info.st_mode))
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def open_regular(raw):
    """(fd, stat) for a plain regular file reached without following a link."""
    dir_fd, leaf = _walk_parent(raw)
    try:
        return _open_leaf(dir_fd, leaf, os.O_RDONLY | _NONBLOCK)
    finally:
        os.close(dir_fd)


def _read_all(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


def sha256_path(raw):
    """sha256 of one plain file, read through the descriptor walk."""
    fd, _info = open_regular(raw)
    try:
        return sha256_fd(fd)
    finally:
        os.close(fd)


def sha256_file(path):
    """Compatibility wrapper: hashing never follows a link and never blocks."""
    return sha256_path(path)


def local_identity(base, locator):
    """Return ((kind, ...), canonical_path, digest_or_None) for a local locator.

    Identity is the inode opened through the descriptor walk, or the lexical
    absolute path when the file does not exist (a missing file is a stable
    identity but never a proof of content). A symlink anywhere in the path, a
    special file, or a platform without anchored traversal raises
    PathIdentityError: neither lexical normalisation nor realpath() is identity.
    """
    raw = str(resolve(base, locator))
    canonical = os.path.normpath(_absolute(raw))
    try:
        fd, info = open_regular(raw)
    except FileNotFoundError:
        return ('path', canonical), canonical, None
    try:
        return ('inode', info.st_dev, info.st_ino), canonical, sha256_fd(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# schema upgrade
# --------------------------------------------------------------------------- #

def _local_trust_reason(observation, record_revision, on_disk, expected):
    """Deterministic, echo-free reason why a local observation is not trusted."""
    if observation is None:
        return 'the observation names no source revision'
    if not nonempty(record_revision):
        return 'the record names no global revision'
    if observation != record_revision:
        return 'the observation names a revision the record does not'
    if on_disk is None:
        return 'the artifact file is missing'
    if on_disk != expected:
        return 'the content on disk does not match the recorded sha256'
    return 'the revision and the content could not be verified against this artifact'


def _external_trust_reason(observation, record_revision, deliverable_revision):
    """Deterministic, echo-free reason for the three-way external contract."""
    if observation is None:
        return 'the observation names no source revision'
    if not nonempty(record_revision):
        return 'the record names no global revision'
    if not (observation == record_revision == deliverable_revision):
        return ('the record revision, the observation revision and the deliverable '
                'revision do not all agree')
    return 'the external revision could not be verified against this artifact'


def upgrade_v1(state, base):
    """Translate a v1 record to v2 without guessing (see the module docstring)."""
    errors = []
    old_rev = state.get('artifact_revision')
    if 'artifact_revision' in state and not nonempty(old_rev):
        errors.append('artifact_revision must be a nonempty string when present')

    for key in sorted(V2_ONLY):
        if key in state:
            errors.append("field '%s' already exists in schema v%d; rename or remove it "
                          "before migrating" % (key, SCHEMA))

    for key in ('constraints', 'milestones', 'acceptance', 'blockers', 'attempts',
                'decisions', 'capabilities', 'lessons'):
        if key in state and not isinstance(state[key], list):
            errors.append('%s must be an array in a v1 record' % key)

    deliverables = state.get('deliverables', [])
    if not isinstance(deliverables, list):
        errors.append('deliverables must be an array')
        deliverables = []

    artifacts, used_ids = [], set()
    identities, digests = {}, {}
    for index, item in enumerate(deliverables):
        label = 'deliverables[%d]' % index
        if not isinstance(item, dict):
            errors.append(label + ' must be an object')
            continue
        kind = item.get('kind', 'local')
        if kind not in KINDS:
            errors.append(label + ': kind must be local or external')
            continue
        locator = item.get('locator')
        if not nonempty(locator):
            errors.append(label + ': locator must be a nonempty string')
            continue
        art_id = item.get('id', 'A%d' % (index + 1))
        if not nonempty(art_id):
            errors.append(label + ': id must be a nonempty string when present')
            continue
        if art_id in used_ids:
            errors.append(label + ': duplicate artifact id %r' % art_id)
            continue
        used_ids.add(art_id)
        art = {'id': art_id, 'kind': kind, 'locator': locator}
        supporting = item.get('supporting')
        if 'supporting' in item and not isinstance(supporting, bool):
            errors.append(label + ': supporting must be a boolean (true or false) when present')
            continue
        if kind == 'local':
            if 'revision' in item:
                errors.append(label + ": the field 'revision' is reserved by schema v%d; "
                                     "rename it before migrating" % SCHEMA)
                continue
            digest = item.get('sha256')
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                errors.append(label + ': sha256 must be 64 lowercase hex digits')
                continue
            try:
                identity, _canonical, on_disk = local_identity(base, locator)
            except PathIdentityError as exc:
                errors.append(label + ': locator is not a plain file (%s); migration refused'
                              % exc.reason)
                continue
            identities[art_id] = identity
            digests[art_id] = on_disk
            art['sha256'] = digest
            art['revision'] = digest[:12]
        else:
            # R-P1: a v1 record carries one global revision, so an external
            # deliverable without its own label means A := G. The fallback is
            # never a placeholder, and without G there is nothing to fall back to.
            if 'revision' in item:
                revision = item.get('revision')
                if not nonempty(revision):
                    errors.append(label + ': external revision must be a nonempty label when '
                                         'present')
                    continue
            else:
                if not nonempty(old_rev):
                    errors.append(label + ': external deliverable without its own revision '
                                         'needs a nonempty global revision on the record; '
                                         'migration refused')
                    continue
                revision = old_rev
            art['revision'] = revision
        for extra_key, value in item.items():
            if extra_key in ('id', 'kind', 'locator', 'revision', 'supporting'):
                continue
            if extra_key == 'sha256' and kind == 'local':
                continue
            art[extra_key] = value
        if 'supporting' in item:
            art['supporting'] = supporting
        artifacts.append(art)

    source_evidence = state.get('evidence', [])
    if not isinstance(source_evidence, list):
        errors.append('evidence must be an array')
        source_evidence = []

    entries = []
    for index, item in enumerate(source_evidence):
        label = 'evidence[%d]' % index
        if not isinstance(item, dict):
            errors.append(label + ' must be an object')
            continue
        for reserved in EVIDENCE_RESERVED:
            if reserved in item:
                errors.append(label + ": the field '%s' belongs to schema v%d binding; "
                                     "migration would overwrite it, so it is refused"
                              % (reserved, SCHEMA))
        if any(reserved in item for reserved in EVIDENCE_RESERVED):
            continue
        if not nonempty(item.get('id')):
            errors.append(label + ': id must be a nonempty string')
            continue
        if item.get('kind') not in KINDS:
            errors.append(label + ': kind must be local or external')
            continue
        if not nonempty(item.get('locator')):
            errors.append(label + ': locator must be a nonempty string')
            continue
        if 'artifact_revision' in item and not nonempty(item.get('artifact_revision')):
            errors.append(label + ': artifact_revision must be a nonempty string when present')
            continue
        entries.append((index, dict(item)))

    if errors:
        raise RecordError(errors)

    # The same actual file listed twice must agree on what it contained, or the
    # record is ambiguous and migration stops instead of picking a content.
    groups = {}
    for art in artifacts:
        if art['kind'] == 'local':
            groups.setdefault(identities[art['id']], []).append(art)
    for identity, group in groups.items():
        if len(group) > 1 and len({art['sha256'] for art in group}) > 1:
            errors.append('deliverables %s are the same local file with conflicting sha256 '
                          'values; the v1 record is ambiguous'
                          % ', '.join(art['id'] for art in group))
    if errors:
        raise RecordError(errors)

    evidence_identities = {}
    for index, ev in entries:
        if ev['kind'] == 'local':
            try:
                identity, _canonical, _digest = local_identity(base, ev['locator'])
            except PathIdentityError as exc:
                errors.append('evidence[%d]: locator is not a plain file (%s); migration '
                              'refused' % (index, exc.reason))
                continue
            evidence_identities[ev['id']] = identity
    if errors:
        raise RecordError(errors)

    local_index, external_index = {}, {}
    for art in artifacts:
        if art['kind'] == 'local':
            local_index.setdefault(identities[art['id']], []).append(art)
        else:
            external_index.setdefault(art['locator'], []).append(art)

    evidence, warnings = [], []
    for index, ev in entries:
        kind = ev['kind']
        ev_rev = ev.get('artifact_revision') if nonempty(ev.get('artifact_revision')) else None
        if kind == 'local':
            hits = local_index.get(evidence_identities[ev['id']], [])
        else:
            hits = external_index.get(ev['locator'], [])
        revisions, unverified, conflict = {}, False, None
        for art in hits:
            if art['kind'] == 'local':
                proven = (ev_rev is not None and nonempty(old_rev) and ev_rev == old_rev
                          and digests.get(art['id']) is not None
                          and digests[art['id']] == art['sha256'])
                reason = _local_trust_reason(ev_rev, old_rev, digests.get(art['id']),
                                             art['sha256'])
            else:
                # The three-way contract: G, E and A must all be present and
                # equal. No source wins over another, so every difference is a
                # conflict and none of the bindings may be trusted.
                proven = (ev_rev is not None and nonempty(old_rev)
                          and ev_rev == old_rev == art['revision'])
                reason = _external_trust_reason(ev_rev, old_rev, art['revision'])
            revisions[art['id']] = art['revision'] if proven else ''
            if not proven:
                unverified = True
                conflict = conflict or reason
        if unverified:
            # C6: an unverified observation keeps no partial proof -- every
            # binding is empty, including one that happened to match.
            revisions = {artifact_id: '' for artifact_id in revisions}
        ev['artifact_ids'] = [art['id'] for art in hits]
        ev['artifact_revisions'] = revisions
        ev.pop('artifact_revision', None)
        if not hits:
            ev['unverified'] = True
            ev['unverified_reason'] = 'no deliverable of its kind matches its locator'
            if ev_rev is not None:
                ev['source_revision'] = ev_rev
            warnings.append('evidence %s: unverified, no matching deliverable; re-verify '
                            'before it supports a criterion' % ev['id'])
        elif unverified:
            ev['unverified'] = True
            ev['unverified_reason'] = conflict
            if ev_rev is not None:
                ev['source_revision'] = ev_rev
            warnings.append('evidence %s: unverified (%s); re-verify before it can support a '
                            'criterion' % (ev['id'], conflict))
        evidence.append(ev)

    if any(isinstance(a, dict) and 'blocker_key' not in a
           for a in (state.get('attempts') if isinstance(state.get('attempts'), list) else [])):
        warnings.append('attempts[]: no blocker_key; the retry budget is not enforced until '
                        'attempts are rewritten in v2 form')

    new = {key: value for key, value in state.items() if key not in V1_FIELDS}
    derived = {'schema_version': 1}
    if nonempty(old_rev):
        derived['artifact_revision'] = old_rev
    new.update(schema_version=SCHEMA, task_id=state.get('task_id'), goal=state.get('goal'),
               status=state.get('status'), next_action=state.get('next_action'),
               artifacts=artifacts, evidence=evidence, shortcuts=[], waivers=[],
               provenance={'generated_at': iso(now()), 'writer': 'checkpoint.py migrate',
                           'derived_from': derived})
    for key in ('constraints', 'milestones', 'acceptance', 'blockers', 'attempts',
                'decisions', 'capabilities', 'lessons'):
        new[key] = state.get(key, [])
    if 'evaluation_date' in state:
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
                      % (kind, secret_scan.redact_path(path)))


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
            errors.append('artifact must be an object with a nonempty string id')
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
        if 'supporting' in art and not isinstance(art['supporting'], bool):
            # Checked before the kind branch: an external artifact used to skip it.
            errors.append(label + ': supporting must be a boolean')
        if art['kind'] == 'external':
            continue
        expected = art.get('sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            errors.append(label + ': sha256 required for local artifacts')
            continue
        if art.get('revision') != expected[:12]:
            errors.append(label + ': revision must be the first 12 hex of sha256')
        if gate:
            # Same descriptor walk as every other reader: a symlink, a FIFO or
            # any special file is a refusal, never a hash of a redirected read.
            try:
                actual = sha256_path(str(resolve(base, art['locator'])))
                if actual != expected:
                    errors.append(label + ': file missing or hash mismatch')
            except FileNotFoundError:
                errors.append(label + ': file missing or hash mismatch')
            except PathIdentityError as exc:
                errors.append(label + ': locator cannot be read safely (%s)' % exc.reason)
            except OSError as exc:
                errors.append(label + ': unreadable file: %s' % exc)


def check_evidence(state, base, errors, gate):
    arts = {a['id']: a for a in state['artifacts']
            if isinstance(a, dict) and nonempty(a.get('id'))}
    seen = set()
    for ev in state['evidence']:
        if not isinstance(ev, dict) or not nonempty(ev.get('id')):
            errors.append('evidence must be an object with a nonempty string id')
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
        if 'run' in ev:
            run = ev['run']
            if (not isinstance(run, dict) or set(run) != RUN_FIELDS
                    or any(not nonempty(run.get(k)) for k in RUN_FIELDS)):
                errors.append(label + ': run must map exactly case_id, model and input_sha256 '
                                       'to nonempty strings')
        if 'unverified' in ev:
            # Representation contract: absent means verified; present must be
            # exactly true and needs a reason. False is not a way to opt out.
            if ev['unverified'] is not True:
                errors.append(label + ': unverified must be exactly true when present; '
                                       'omit it for a verified observation')
            elif not nonempty(ev.get('unverified_reason')):
                errors.append(label + ': unverified_reason required when unverified is true')
        if 'unverified_reason' in ev and not nonempty(ev.get('unverified_reason')):
            errors.append(label + ': unverified_reason must be a nonempty string when present')
        if 'source_revision' in ev:
            # The v1 label only describes an observation that was NOT verified;
            # without the marker it is a hand edit trying to look verified.
            if ev.get('unverified') is not True:
                errors.append(label + ': source_revision is only valid together with '
                                       'unverified: true; it records a label that was not '
                                       'verified')
            elif not nonempty(ev.get('source_revision')):
                errors.append(label + ': source_revision must be a nonempty string when present')
        if gate:
            if ev.get('unverified') is True:
                errors.append(label + ': migrated as unverified; re-observe before it can '
                                       'support a criterion')
            for aid in ids:
                art = arts.get(aid)
                if art and revisions.get(aid) != art.get('revision'):
                    errors.append(label + ': stale, artifact %s changed since observation' % aid)


def check_acceptance(state, errors, gate):
    evidence = {e['id']: e for e in state['evidence']
                if isinstance(e, dict) and nonempty(e.get('id'))}
    waivers = {w['criterion_id'] for w in state['waivers']
               if isinstance(w, dict) and nonempty(w.get('criterion_id'))}
    seen = set()
    for item in state['acceptance']:
        if not isinstance(item, dict) or not nonempty(item.get('id')):
            errors.append('acceptance must be an object with a nonempty string id')
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
        criterion_run = item.get('run')
        if 'run' in item:
            if (not isinstance(criterion_run, dict) or set(criterion_run) != RUN_FIELDS
                    or any(not nonempty(criterion_run.get(k)) for k in RUN_FIELDS)):
                errors.append(label + ': run must map exactly case_id, model and input_sha256 '
                                       'to nonempty strings')
                criterion_run = None
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
                if criterion_run is not None and ev.get('run') != criterion_run:
                    errors.append(label + ': evidence %s is not from the criterion\'s run' % ref)


def check_waivers(state, errors):
    for item in state['waivers']:
        if not isinstance(item, dict) or not nonempty(item.get('criterion_id')):
            errors.append('waiver must be an object with a nonempty criterion_id')
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
            # A change needs a nonempty value on both sides; a missing 'before' is
            # not evidence that the hypothesis moved.
            changed = [bool(nonempty(i.get('hypothesis_before'))
                            and nonempty(i.get('hypothesis_after'))
                            and i.get('hypothesis_after') != i.get('hypothesis_before'))
                       for i in items]
            for start in range(len(items) - 2):
                if not any(changed[start:start + 3]):
                    errors.append(label + ': 3 attempts without a hypothesis change; escalate '
                                        'or switch approach instead of retrying')
                    break


def check_shortcuts(state, errors):
    for index, item in enumerate(state['shortcuts']):
        label = 'shortcuts[%d]' % index
        if not isinstance(item, dict) or not nonempty(item.get('what')):
            errors.append(label + ' must be an object with a nonempty what')
            continue
        if not nonempty(item.get('ceiling')):
            errors.append(label + ': ceiling required (what limit was accepted)')
        if not nonempty(item.get('upgrade_trigger')):
            errors.append(label + ': no-trigger; state when this shortcut must be revisited')
        if 'at' in item:
            parse_ts(item.get('at'), label + ': at', errors)


def check_capabilities(state, errors, warnings):
    for index, item in enumerate(state['capabilities']):
        label = 'capabilities[%d]' % index
        if isinstance(item, str):
            warnings.append(label + ' is unstructured; record class, source and last_success')
            continue
        if not isinstance(item, dict) or not nonempty(item.get('name')):
            errors.append(label + ' must be an object with a nonempty name')
            continue
        if 'class' not in item:
            warnings.append(label + ' has no class; cannot distinguish discovered from '
                                   'authorized')
            continue
        if item['class'] not in CLASSES:
            errors.append(label + ': invalid class')
        if item['class'] == 'callable' and not item.get('untested'):
            if not nonempty(item.get('last_success')):
                errors.append(label + ': callable needs last_success or untested: true')


def check_coverage(state, errors):
    if not state['acceptance']:
        errors.append('acceptance must be nonempty before delivery')
    if not state['artifacts']:
        errors.append('artifacts must be nonempty before delivery')
    bound = set()
    for ev in state['evidence']:
        if isinstance(ev, dict) and isinstance(ev.get('artifact_ids'), list):
            bound.update(aid for aid in ev['artifact_ids'] if isinstance(aid, str))
    for art in state['artifacts']:
        if (isinstance(art, dict) and nonempty(art.get('id'))
                and art['id'] not in bound and not art.get('supporting')):
            errors.append('artifact %s: no evidence binds it; mark supporting: true if it is '
                          'input rather than output' % art['id'])


def check_blockers(state, errors, gate):
    if not gate:
        return
    if state['blockers']:
        errors.append('unresolved blockers')
    if state.get('status') == 'blocked':
        errors.append('status is blocked')


def check_report(state, errors):
    """The optional ``report`` block: why the unit stopped, in a word a machine can sort.

    ``blockers`` holds prose a human reads; ``report.state`` is what a tick, a panel or a
    reviewer can count. The block is optional so every v1.2 record still validates, and a
    failure may not be filed as complete. ``dropped`` is exempt from that rule on purpose:
    it ends work that needed no action, which can be a completed end.
    """
    report = state.get('report')
    if report is None:
        return
    if not isinstance(report, dict):
        errors.append('report must be an object with state, since and reason')
        return
    if report.get('state') not in REPORT_STATES:
        errors.append('report.state must be one of %s; a sentence is not a state'
                      % ', '.join(REPORT_STATES))
    if not nonempty(report.get('reason')):
        errors.append('report.reason must be nonempty: say why it stopped')
    elif len(str(report['reason'])) > 300:
        errors.append('report.reason must be at most 300 characters')
    if 'since' in report:
        parse_ts(report.get('since'), 'report.since', errors)
    if 'next' in report and not nonempty(report.get('next')):
        errors.append('report.next must be nonempty when present')
    if state.get('status') == 'complete' and report.get('state') in ('blocked', 'failed'):
        errors.append('status is complete but report.state is %r: work that stopped on a block '
                      'or a failure is not complete' % report.get('state'))


def mandate_path(state_path):
    """The mandate sits next to the record, under the record's own stem.

    A separate file because a mandate is immutable and the record is rewritten on every
    step: a new mandate is a new file, and nothing in the state schema moves.
    """
    p = Path(os.path.normpath(_absolute(str(state_path))))
    return p.with_name(p.stem + '.mandate.json')


def mandate_errors(doc):
    errors = []
    if not isinstance(doc, dict):
        return ['mandate must be an object']
    if doc.get('schema_version') != 1:
        errors.append('mandate.schema_version must be 1')
    for key in MANDATE_REQUIRED:
        if not nonempty(doc.get(key)):
            errors.append('mandate.%s must be nonempty' % key)
    if doc.get('strength') not in MANDATE_STRENGTHS:
        errors.append('mandate.strength must be explicit or inferred')
    if 'granted_at' in doc:
        parse_ts(doc.get('granted_at'), 'mandate.granted_at', errors)
    rules = doc.get('rules', [])
    if not isinstance(rules, list) or any(not nonempty(r) for r in rules):
        errors.append('mandate.rules must be a list of non-empty strings')
    for path, kind in secret_scan.scan_state(doc):
        errors.append('mandate: secret-shaped value (%s) at %s; name who and what, never a '
                      'credential' % (kind, secret_scan.redact_path(path)))
    return errors


def check_mandate(state_path, errors):
    """A mandate file is optional; when it exists it must be readable and complete."""
    path = mandate_path(state_path)
    if not os.path.lexists(str(path)):
        return
    try:
        _raw, doc = load_document(str(path))
    except RecordError as exc:
        errors.extend(exc.errors)
        return
    errors.extend(mandate_errors(doc))


def inspect(state, base, gate=False):
    errors, warnings = [], []
    if not check_top(state, errors):
        return errors, warnings
    check_secrets(state, errors)
    check_report(state, errors)
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

def read_regular(path):
    """(bytes, stat) for a plain regular file reached without following a link.

    The walk is descriptor-anchored and the file is opened O_NONBLOCK, so a FIFO
    or a device cannot block the read and a symlink component is refused instead
    of followed.
    """
    try:
        return read_regular_bytes(path)
    except FileNotFoundError as exc:
        raise RecordError(['%s: missing component %r' % (path, exc.filename)])
    except PathIdentityError as exc:
        raise RecordError(['%s: cannot be read safely (%s)' % (path, exc.reason)])
    except OSError as exc:
        raise RecordError(['%s: cannot be read safely (%s)' % (path, exc.strerror or exc)])


def read_regular_bytes(path):
    fd, info = open_regular(path)
    try:
        return _read_all(fd), info
    finally:
        os.close(fd)


def load_document(path):
    data, _info = read_regular(path)
    try:
        document = json.loads(data.decode('utf-8'),
                              object_pairs_hook=_no_duplicate_keys,
                              parse_constant=_reject_constant,
                              parse_float=_finite_float)
    except ValueError as exc:
        raise RecordError(['%s: not valid JSON (%s)' % (path, exc)])
    if _contains_surrogate(document):
        raise RecordError(['%s: not valid JSON (a string contains a lone surrogate code '
                           'point, which cannot be stored as UTF-8)' % path])
    return data, document


def load(path):
    return load_document(path)[1]


def require_object(state):
    """A command that reads or writes a record needs a JSON object, not an array."""
    if not isinstance(state, dict):
        raise SystemExit('state must be an object')
    return state


def require_schema_v2(state, command):
    """Mutating a record only makes sense on the schema and the lists it writes."""
    if state.get('schema_version') != SCHEMA:
        raise SystemExit('%s needs schema v%d, found %r; run migrate first'
                         % (command, SCHEMA, state.get('schema_version')))
    for key in ('artifacts', 'evidence'):
        if not isinstance(state.get(key), list):
            raise SystemExit('%s: %s must be an array in the record; run validate'
                             % (command, key))
    return state


def _entries(state, key):
    value = state.get(key)
    return value if isinstance(value, list) else []


def structure_errors(state):
    """Nested shapes the commands index or hash directly.

    Content rules (timestamps, secrets, criterion status) stay in ``inspect``; this
    only answers "can this document be read without raising?".
    """
    errors = []
    for index, art in enumerate(_entries(state, 'artifacts')):
        label = 'artifacts[%d]' % index
        if not isinstance(art, dict):
            errors.append(label + ' must be an object')
        elif not nonempty(art.get('id')):
            errors.append(label + ': id must be a nonempty string')
        elif art.get('kind') not in KINDS:
            errors.append(label + ': kind must be local or external')
    for index, ev in enumerate(_entries(state, 'evidence')):
        label = 'evidence[%d]' % index
        if not isinstance(ev, dict):
            errors.append(label + ' must be an object')
            continue
        if not nonempty(ev.get('id')):
            errors.append(label + ': id must be a nonempty string')
        ids = ev.get('artifact_ids')
        if ids is not None and (not isinstance(ids, list)
                                or any(not nonempty(i) for i in ids)):
            errors.append(label + ': artifact_ids must be a list of ids')
    for index, item in enumerate(_entries(state, 'acceptance')):
        if not isinstance(item, dict) or not nonempty(item.get('id')):
            errors.append('acceptance[%d] must be an object with a nonempty id' % index)
        elif not isinstance(item.get('evidence_ids', []), list):
            errors.append('acceptance[%d]: evidence_ids must be an array' % index)
    for index, item in enumerate(_entries(state, 'waivers')):
        if not isinstance(item, dict) or not nonempty(item.get('criterion_id')):
            errors.append('waivers[%d] must be an object with a nonempty criterion_id' % index)
    for index, item in enumerate(_entries(state, 'attempts')):
        if not isinstance(item, dict):
            errors.append('attempts[%d] must be an object' % index)
    for index, item in enumerate(_entries(state, 'capabilities')):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict) or not nonempty(item.get('name')):
            errors.append('capabilities[%d] must be an object with a nonempty name' % index)
    for index, item in enumerate(_entries(state, 'shortcuts')):
        if not isinstance(item, dict) or not nonempty(item.get('what')):
            errors.append('shortcuts[%d] must be an object with a nonempty what' % index)
    return errors


def ensure_structure(state, command):
    errors = structure_errors(state)
    if errors:
        raise RecordError(['%s refused: %s' % (command, error) for error in errors])
    return state


def ensure_valid(state, base, command):
    """A record a command is about to persist must pass the structural checks."""
    errors, _warnings = inspect(state, base, gate=False)
    if errors:
        raise RecordError(['%s refused: %s' % (command, error) for error in errors])
    return state


def new_state(task_id, goal):
    state = {'schema_version': SCHEMA, 'task_id': task_id, 'goal': goal,
             'status': 'in_progress',
             'next_action': 'Bind artifacts and define acceptance criteria',
             'provenance': {'generated_at': iso(now()), 'writer': 'checkpoint.py init',
                            'derived_from': None}}
    for key in LISTS:
        state[key] = []
    return state


def write_state(path, state, expected):
    """Atomic whole-file replacement inside the descriptor-walked directory.

    Promise, exactly: the replacement is atomic (one ``os.replace`` of a complete,
    fsynced file); the write is refused if the target changed up to the second
    comparison -- bytes and ``(st_dev, st_ino)`` -- that runs immediately before
    ``os.replace``. There is NO serialization against a writer that acts after
    that second check and no cross-process lock or CAS protocol; the target is
    walked component by component with O_NOFOLLOW, so a symlink or a special file
    never becomes the destination, and the temporary file is created with O_EXCL
    in the verified directory (its inode is confirmed through that directory's
    descriptor), keeps the original mode and ownership on the held descriptor,
    and is fsynced. The cleanup removes only a file this call created: by leaf
    through the anchored directory descriptor when it was confirmed there,
    otherwise by ``(st_dev, st_ino)`` identity at the path ``mkstemp`` returned.
    One known exception: in the anchored case, if the unlink by leaf itself
    fails, a last fallback unlinks by name at that path with no identity check
    (see ``evals/RESULTS.md``, known residual risks).
    """
    data = (json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
            + '\n').encode('utf-8')
    try:
        dir_fd, leaf = _walk_parent(path)
    except FileNotFoundError as exc:
        raise RecordError(['%s: missing component %r' % (path, exc.filename)])
    except PathIdentityError as exc:
        raise RecordError(['%s: cannot be written safely (%s)' % (path, exc.reason)])
    tmp_name = None
    tmp_leaf = None
    fd = None
    held = None
    temp_is_anchored = False
    try:
        try:
            fd, first = _open_leaf(dir_fd, leaf, os.O_RDONLY | _NONBLOCK)
        except FileNotFoundError as exc:
            raise RecordError(['%s: missing component %r' % (path, exc.filename)])
        except PathIdentityError as exc:
            raise RecordError(['%s: cannot be written safely (%s)' % (path, exc.reason)])
        try:
            current = _read_all(fd)
        finally:
            os.close(fd)
        if current != expected:
            raise RecordError(['%s changed on disk since it was read; refusing to overwrite it'
                               % path])
        mode = stat.S_IMODE(first.st_mode)
        directory = os.path.dirname(os.path.normpath(_absolute(path)))
        fd, tmp_name = tempfile.mkstemp(prefix='.' + leaf + '.', suffix='.tmp',
                                        dir=directory)
        tmp_leaf = os.path.basename(tmp_name)
        # The temp file must be the one we hold, seen through the anchored
        # directory: if the parent path was redirected, refuse instead of
        # renaming an unrelated file into the target.
        try:
            seen = os.stat(tmp_leaf, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            seen = None
        held = os.fstat(fd)
        if seen is None or (seen.st_dev, seen.st_ino) != (held.st_dev, held.st_ino):
            raise RecordError(['%s: the temporary file is not in the verified directory; '
                               'refusing to replace the target' % path])
        # Only now is the temp file proven to be the one named in the directory the
        # walk anchored, so only now may the cleanup unlink it by leaf there.
        temp_is_anchored = True
        # The held descriptor, not the name: a swapped temp path cannot redirect
        # the mode or owner change onto an unrelated file.
        try:
            os.fchmod(fd, mode)
            try:
                os.fchown(fd, first.st_uid, first.st_gid)
            except OSError:
                pass
            stream = os.fdopen(fd, 'wb')
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Second comparison, immediately before the replace. A writer that got
        # in up to here wins: its bytes stay and this call refuses.
        try:
            second_fd, second = _open_leaf(dir_fd, leaf, os.O_RDONLY | _NONBLOCK)
        except FileNotFoundError:
            raise RecordError(['%s disappeared while it was being written; refusing to '
                               'recreate it' % path])
        except PathIdentityError as exc:
            raise RecordError(['%s changed into something unsafe while it was being written '
                               '(%s); refusing to overwrite it' % (path, exc.reason)])
        try:
            still = _read_all(second_fd)
        finally:
            os.close(second_fd)
        if (second.st_dev, second.st_ino) != (first.st_dev, first.st_ino) or still != expected:
            raise RecordError(['%s changed on disk while it was being written; refusing to '
                               'overwrite it' % path])
        os.replace(tmp_leaf, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = None
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_name is not None and tmp_leaf is not None:
            if temp_is_anchored:
                try:
                    os.unlink(tmp_leaf, dir_fd=dir_fd)
                except OSError:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            else:
                # The temp was created after the parent had already been redirected,
                # so ``tmp_leaf`` names a foreign file in the anchored directory.
                # Drop the mkstemp descriptor and remove the file this call created
                # by identity at the path mkstemp returned -- never by leaf.
                try:
                    if fd is not None:
                        os.close(fd)
                except OSError:
                    pass
                current = None
                if held is not None:
                    try:
                        current = os.stat(tmp_name, follow_symlinks=False)
                    except OSError:
                        current = None
                    if current is not None and (current.st_dev, current.st_ino) == \
                            (held.st_dev, held.st_ino):
                        try:
                            os.unlink(tmp_name)
                        except OSError:
                            pass
        os.close(dir_fd)


def emit(payload):
    """Single output boundary: values and keys the detector flags are hidden first."""
    print(json.dumps(secret_scan.redact(payload), ensure_ascii=False))


def emit_text(text):
    print(secret_scan.scrub(text))


class SecretParser(argparse.ArgumentParser):
    """ArgumentParser that scrubs the message before argparse writes it to stderr."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, '%s: error: %s\n' % (self.prog, secret_scan.scrub(message)))


def cmd_init(args):
    if not nonempty(args.task_id) or not nonempty(args.goal):
        raise SystemExit('init requires --task-id and --goal')
    path = Path(args.path)
    payload = (json.dumps(new_state(args.task_id, args.goal), ensure_ascii=False,
                          indent=2, allow_nan=False) + '\n').encode('utf-8')
    try:
        dir_fd, leaf = _walk_parent(path, create=True)
    except FileNotFoundError as exc:
        raise RecordError(['%s: missing component %r' % (path, exc.filename)])
    except PathIdentityError as exc:
        raise RecordError(['%s: cannot be created safely (%s)' % (path, exc.reason)])
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                     0o600, dir_fd=dir_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)
    emit_text('Created %s (schema v%d). No completion or durable-storage claim.'
              % (str(path), SCHEMA))
    return 0


def resolve_local(base, locator):
    """(stored_locator, digest) for a local locator, verified through the walk.

    The state-relative form wins; the caller's working directory is only a
    fallback when that path does not exist. A symlink or a special file is a
    refusal either way -- it is never silently canonicalised or followed.
    """
    target = str(resolve(base, locator))
    try:
        return locator, sha256_path(target)
    except FileNotFoundError:
        pass
    if not os.path.isabs(locator):
        alternative = _absolute(locator)
        try:
            return os.path.normpath(alternative), sha256_path(alternative)
        except FileNotFoundError:
            pass
    raise FileNotFoundError(locator)


def cmd_hash(args):
    path = Path(args.path)
    try:
        digest = sha256_path(path)
    except FileNotFoundError:
        raise SystemExit('%s: not a file' % str(path))
    except PathIdentityError as exc:
        raise RecordError(['%s: %s' % (str(path), exc.reason)])
    emit({'path': str(path), 'sha256': digest, 'revision': digest[:12]})
    return 0


def cmd_artifact(args):
    data, state = load_document(args.path)
    require_schema_v2(require_object(state), 'artifact')
    base = Path(os.path.dirname(os.path.normpath(_absolute(args.path))))
    ensure_structure(state, 'artifact')
    kind = args.kind or 'local'
    locator = args.locator
    if kind == 'local':
        try:
            locator, digest = resolve_local(base, locator)
        except FileNotFoundError:
            raise SystemExit('%s: no such file (looked at %s, relative to the state file '
                             'directory)' % (str(locator), str(resolve(base, locator))))
        revision = digest[:12]
    else:
        if not nonempty(args.revision):
            raise SystemExit('external artifacts need --revision (a label you can point at)')
        digest, revision = None, args.revision
    entry = next((a for a in state['artifacts']
                  if isinstance(a, dict) and a.get('id') == args.id), None)
    if entry is None:
        entry = {'id': args.id}
        state['artifacts'].append(entry)
    entry.update(kind=kind, locator=locator, revision=revision)
    if digest:
        entry['sha256'] = digest
    if args.supporting:
        entry['supporting'] = True
    ensure_structure(state, 'artifact')
    write_state(args.path, state, data)
    emit({'artifact': entry,
          'note': 'revision is content-derived; evidence bound to the previous '
                  'revision is now stale'})
    return 0


def cmd_evidence(args):
    data, state = load_document(args.path)
    require_schema_v2(require_object(state), 'evidence')
    ensure_structure(state, 'evidence')
    ids = args.artifact or []
    arts = {a['id']: a for a in state['artifacts']
            if isinstance(a, dict) and nonempty(a.get('id'))}
    missing = [i for i in ids if i not in arts]
    if missing:
        raise RecordError(['unknown artifact id(s): %s' % ', '.join(str(i) for i in missing)])
    kinds = {arts[i].get('kind') for i in ids}
    if len(kinds) != 1:
        raise SystemExit('bound artifacts must share one kind')
    entry = {'id': args.id, 'kind': kinds.pop(), 'locator': arts[ids[0]].get('locator'),
             'artifact_ids': ids,
             'artifact_revisions': {i: arts[i].get('revision') for i in ids},
             'observed_at': args.observed_at or iso(now()), 'result': args.result}
    run_flags = (args.run_case, args.run_model, args.run_input)
    if any(run_flags):
        if not all(nonempty(x) for x in run_flags):
            raise SystemExit('--run-case, --run-model and --run-input must be given together')
        entry['run'] = {'case_id': args.run_case, 'model': args.run_model,
                        'input_sha256': args.run_input}
    if args.summary:
        entry['summary'] = args.summary
    if args.result == 'unknown':
        if not nonempty(args.unknown_reason):
            raise SystemExit('--result unknown needs --unknown-reason')
        entry['unknown_reason'] = args.unknown_reason
    state['evidence'] = [e for e in state['evidence']
                         if isinstance(e, dict) and e.get('id') != args.id] + [entry]
    ensure_structure(state, 'evidence')
    write_state(args.path, state, data)
    emit({'evidence': entry})
    return 0


def write_mandate(path, doc):
    """Create the mandate file exclusively: O_EXCL refuses an existing file and a symlink alike."""
    raw = _absolute(str(path))
    try:
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o644)
    except FileExistsError:
        raise RecordError(['%s already exists: a mandate is immutable, write a new one under a '
                           'new id (a wider scope is a new mandate)' % raw])
    except OSError as exc:
        raise RecordError(['%s: cannot be created (%s)' % (raw, exc.strerror or exc)])
    try:
        os.write(fd, (json.dumps(doc, indent=1, allow_nan=False) + '\n').encode('utf-8'))
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_report(args):
    data, state = load_document(args.path)
    require_schema_v2(require_object(state), 'report')
    ensure_structure(state, 'report')
    report = {'state': args.state, 'since': args.since or iso(now()), 'reason': args.reason}
    if args.next:
        report['next'] = args.next
    previous = state.get('report')
    state['report'] = report
    errors = []
    check_report(state, errors)
    if errors:
        raise RecordError(errors)
    write_state(args.path, state, data)
    emit({'report': report, 'previous': previous})
    return 0


def cmd_mandate(args):
    doc = {'schema_version': 1, 'id': args.id, 'source': args.source, 'actor': args.actor,
           'channel': args.channel, 'strength': args.strength,
           'policy_version': args.policy_version, 'scope': args.scope,
           'granted_at': args.granted_at or iso(now())}
    if args.rule:
        doc['rules'] = list(args.rule)
    errors = mandate_errors(doc)
    if errors:
        raise RecordError(errors)
    path = mandate_path(args.path)
    write_mandate(path, doc)
    emit({'ok': True, 'mandate': doc, 'written': str(path)})
    return 0


def cmd_revalidate(args):
    """Does the mandate on record still cover the work? 0 yes, 1 no, 2 there is none."""
    _data, state = load_document(args.path)
    require_schema_v2(require_object(state), 'revalidate')
    path = mandate_path(args.path)
    if not os.path.lexists(str(path)):
        emit({'ok': True, 'holds': False, 'reason': 'no mandate on record', 'mandate': str(path)})
        return 2
    _raw, doc = load_document(str(path))
    errors = mandate_errors(doc)
    if errors:
        emit({'ok': False, 'holds': False, 'errors': errors})
        return 1
    reasons = []
    if args.policy_version and doc['policy_version'] != args.policy_version:
        reasons.append('granted under policy %r, the current policy is %r'
                       % (doc['policy_version'], args.policy_version))
    if args.actor and doc['actor'] != args.actor:
        reasons.append('granted for actor %r, the work is now asked for by %r'
                       % (doc['actor'], args.actor))
    emit({'ok': True, 'holds': not reasons, 'mandate': doc['id'],
          'policy_version': doc['policy_version'], 'reasons': reasons})
    return 0 if not reasons else 1


def state_base(path):
    """The record's directory as a lexical path: the walk verified it, no resolve()."""
    return Path(os.path.dirname(os.path.normpath(_absolute(path))))


def cmd_migrate(args):
    data, state = load_document(args.path)
    require_object(state)
    version = state.get('schema_version')
    if version == SCHEMA:
        emit({'ok': True, 'note': 'already schema v%d' % SCHEMA})
        return 0
    if version != 1:
        raise SystemExit('unsupported schema_version: %r' % (version,))
    base = state_base(args.path)
    new, warnings = upgrade_v1(state, base)
    ensure_valid(new, base, 'migrate')
    if args.write:
        write_state(args.path, new, data)
        emit({'ok': True, 'wrote': str(args.path), 'warnings': warnings,
              'next': 'run gate; unverified evidence must be re-observed'})
    else:
        emit({'ok': True, 'warnings': warnings, 'migrated': new})
    return 0


def cmd_check(args, gate):
    raw = load(args.path)
    base = state_base(args.path)
    warnings = []
    if not isinstance(raw, dict):
        # Same shape as every other check outcome: a recovery script parses stdout.
        emit({'ok': False, 'errors': ['state must be an object'], 'warnings': [],
              'scope': SCOPE})
        return 1
    if raw.get('schema_version') != SCHEMA:
        if raw.get('schema_version') == 1:
            try:
                raw, warnings = upgrade_v1(raw, base)
            except RecordError as exc:
                emit({'ok': False, 'errors': exc.errors, 'warnings': [], 'scope': SCOPE})
                return 1
            warnings.insert(0, 'schema v1 read as v2 in memory; run migrate to persist')
        else:
            emit({'ok': False, 'errors': ['unsupported schema_version'], 'warnings': [],
                  'scope': SCOPE})
            return 1
    errors, more = inspect(raw, base, gate)
    check_mandate(args.path, errors)
    emit({'ok': not errors, 'errors': errors, 'warnings': warnings + more, 'scope': SCOPE})
    return 1 if errors else 0


def main():
    parser = SecretParser(description=__doc__)
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
    p.add_argument('--run-case', help='run binding: the case this observation belongs to')
    p.add_argument('--run-model', help='run binding: the model or runner identity')
    p.add_argument('--run-input', help='run binding: sha256 of the exact input observed')
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser(
        'migrate', help='v1 -> v2 under the written three-way revision contract',
        description=(
            'Migrate a v1 record. An external observation is trusted only when '
            'state.artifact_revision (G), evidence.artifact_revision (E) and the '
            'deliverable revision (A) are non-empty and equal. A v1 external '
            'deliverable without its own revision uses A := G (v1 has one global '
            'revision); without any global revision the record is refused, and an '
            'explicit revision: "" stays an error. Any difference empties all '
            'bindings, marks the observation unverified with a deterministic '
            'reason, keeps the v1 label as source_revision and prints a warning. '
            'A local observation is trusted only when G == E and the file still '
            'matches its recorded sha256, read without following any link. Unknown '
            'fields are preserved unchanged; only sha256->sha256 + revision (local), '
            'revision->revision or A := G (external) and '
            'artifact_revision->source_revision are mapped. A repeated object key or '
            'a non-finite number is refused. Nothing is written unless the '
            'converted record passes validate.'))
    p.add_argument('path')
    p.add_argument('--write', action='store_true')
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser('report', help='record why the unit stopped, in a sortable state')
    p.add_argument('path')
    p.add_argument('--state', choices=REPORT_STATES, required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('--since')
    p.add_argument('--next')
    p.set_defaults(func=cmd_report)

    p = sub.add_parser('mandate', help='write the immutable authorisation next to the record '
                                       '(refuses overwrite)')
    p.add_argument('path')
    p.add_argument('--id', required=True)
    p.add_argument('--source', required=True)
    p.add_argument('--actor', required=True)
    p.add_argument('--channel', required=True)
    p.add_argument('--strength', choices=MANDATE_STRENGTHS, required=True)
    p.add_argument('--policy-version', required=True)
    p.add_argument('--scope', required=True)
    p.add_argument('--rule', action='append')
    p.add_argument('--granted-at')
    p.set_defaults(func=cmd_mandate)

    p = sub.add_parser('revalidate', help='does the mandate still hold? 0 yes, 1 no, 2 no mandate')
    p.add_argument('path')
    p.add_argument('--policy-version')
    p.add_argument('--actor')
    p.set_defaults(func=cmd_revalidate)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RecordError as exc:
        emit({'ok': False, 'errors': exc.errors})
        return 1
    except SystemExit as exc:
        emit({'ok': False, 'error': str(exc) or 'aborted'})
        return 1
    except PathIdentityError as exc:
        emit({'ok': False, 'error': str(exc)})
        return 1
    except (OSError, ValueError) as exc:
        emit({'ok': False, 'error': str(exc)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
