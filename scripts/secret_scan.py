#!/usr/bin/env python3
"""Credential detection for Longhaul task state.

Reports *where* a secret-shaped value sits, never the value itself. Four
independent signals are combined:

* value shapes that are credentials wherever they appear (PEM block, JWT,
  provider key, generic bearer token, userinfo inside a URL, IBAN, Luhn-valid
  card run, long hex, long base64);
* a value stored under a key that names a credential, compared after collapsing
  case and separators, so ``clientSecret``/``client_secret``/``CLIENT-SECRET``
  agree, unless the value is a *syntactic* pointer such as ``${DB_PASSWORD}``,
  ``env:DB_PASSWORD`` or ``op://prod/db/password``;
* the legacy assignment form (``password = ...``) inside free text;
* dict *keys* that are themselves secret-shaped (reported by path, renamed on
  output with a unique placeholder so no entry is dropped).

Fields that hold content hashes, revision labels or locators by design keep the
shape-only signals: a 64-hex sha256 or a long build directory must stay silent,
while a token pasted into a locator or a revision label is still reported.

``scrub()`` is the substring filter used for every outgoing string: in label
context (hash/revision/locator fields) it keeps hash-like shapes, everywhere else
it masks every shape this module detects. ``redact()`` is the structural boundary
for JSON payloads (values and keys). Both are output-only; the state is never
rewritten by them.

Standard library only; no network. This is a heuristic backstop, not a guarantee:
it reads string values, does not decode encoded secrets, does not scan dict keys
for detection (it only renames a key when a hit is found elsewhere), and cannot
recognise a bare password that no rule describes.
"""
import re

# Keys whose values are content hashes, revision labels or locators by design.
LABEL_KEYS = frozenset({'sha256', 'revision', 'digest', 'head', 'commit',
                        'git_head', 'artifact_revisions', 'locator',
                        'source_revision', 'input_sha256'})

# Keys that name a credential, stored with case and separators collapsed.
CREDENTIAL_KEYS = frozenset({
    'apikey', 'secretkey', 'clientsecret', 'accesstoken', 'authtoken',
    'refreshtoken', 'bearertoken', 'password', 'passwd', 'pwd', 'privatekey',
    'token', 'authorization', 'apiintegrationcode', 'secret',
})

# Fields that carry the value of a wrapper which names the credential in a
# sibling ``name`` field, as n8n stores HTTP header credentials:
# ``{"name": "Secret", "value": "..."}``. The value is the secret even though its
# own key is only ``value``.
VALUE_KEYS = frozenset({'value', 'valuestring', 'valuetext'})

# Values that point at a credential instead of being one. Only syntax that is
# verifiably a pointer is exempt; a phrase such as "see the vault entry" is not.
_REFERENCE = re.compile(
    r'''(?ix)^(?:
          «[^»]{1,60}» | <[^>]{1,60}> | \[[^\]]{1,60}\] | \*{2,}
        | \$\{?[A-Za-z_][A-Za-z0-9_]*\}?
        | env[:/][A-Za-z_][A-Za-z0-9_]*
        | (?:op|vault|sops)://\S+
        | see\s+(?:op|vault|sops)://\S+
        | (?:redacted|removed|omitted|hidden|none|null|n/?a|na|unset|unknown|tbd|todo
           |required)
    )$''')

_ASSIGN = re.compile(
    r'(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token'
    r'|auth[_-]?token|refresh[_-]?token|bearer[_-]?token|token|password|passwd|pwd'
    r'|private[_-]?key)\b\s*[:=]\s*["\']?([^\s"\',;]{6,})')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_PEM_BLOCK = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?'
                        r'(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)')
_PROVIDER = re.compile(
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
    r'|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})')
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*')
_URL_USERINFO = re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]{1,80}:[^/\s@]{1,200}@')
_IBAN = re.compile(r'\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b')
_CARD = re.compile(r'\b(?:\d[ -]?){15,18}\d\b')
_LONG_HEX = re.compile(r'\b[0-9a-fA-F]{40,}\b')
_LONG_B64 = re.compile(r'\b[A-Za-z0-9+/]{60,}={0,2}\b')

REDACTED = '«redacted»'


def luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _credential_key(key):
    if not isinstance(key, str):
        return False
    return re.sub(r'[^a-z0-9]', '', key.strip().lower()) in CREDENTIAL_KEYS


def _value_key(key):
    if not isinstance(key, str):
        return False
    return re.sub(r'[^a-z0-9]', '', key.strip().lower()) in VALUE_KEYS


def scan_text(text, key=None, label=False):
    """Return the kinds of secret shapes found in one string (never the value).

    ``key`` is the field name the string was stored under; ``label`` marks a value
    inside a hash, revision or locator field, where hash and numeric shapes are
    expected and only credential shapes are reported.
    """
    kinds = []
    if _PEM.search(text):
        return ['private_key_block']
    if _JWT.search(text):
        kinds.append('jwt')
    if _PROVIDER.search(text):
        kinds.append('provider_key')
    if _BEARER.search(text):
        kinds.append('bearer_token')
    if _URL_USERINFO.search(text):
        kinds.append('url_credentials')
    if _ASSIGN.search(text):
        kinds.append('credential_assignment')
    if not label:
        if _IBAN.search(text):
            kinds.append('iban')
        for match in _CARD.finditer(text):
            digits = re.sub(r'[ -]', '', match.group(0))
            if 16 <= len(digits) <= 19 and luhn(digits):
                kinds.append('card_number')
                break
        if _LONG_HEX.search(text):
            kinds.append('long_hex')
        if _LONG_B64.search(text):
            kinds.append('long_base64')
    if _credential_key(key) and not _REFERENCE.match(text.strip()):
        kinds.append('credential_key')
    return list(dict.fromkeys(kinds))


def walk(node, path='$', key=None, label=False):
    """Yield (path, key, value, label) for every string value in the document."""
    if isinstance(node, dict):
        # A wrapper that names the credential next to its value, as n8n stores HTTP
        # header credentials ({"name": "Secret", "value": "..."}): the value is the
        # secret even though its own key is only "value".
        named = node.get('name')
        named = named if isinstance(named, str) and _credential_key(named) else None
        for k, v in node.items():
            field = named if named and _value_key(k) else k
            yield from walk(v, f'{path}.{k}', field, label or k in LABEL_KEYS)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f'{path}[{i}]', key, label)
    elif isinstance(node, str):
        yield path, key, node, label


def scan_state(state):
    """Return [(path, kind)] for every secret-shaped value in a state document.

    One hit per string: the caller deletes the line, so the second pattern that
    matched the same value is noise, not information.
    """
    hits = []
    for path, key, value, label in walk(state):
        kinds = scan_text(value, key, label)
        if kinds:
            hits.append((path, kinds[0]))
    return hits


def _mask_card(match):
    digits = re.sub(r'[ -]', '', match.group(0))
    return REDACTED if 16 <= len(digits) <= 19 and luhn(digits) else match.group(0)


def scrub(text, label=False):
    """Mask every detected shape in one outgoing string.

    ``label`` keeps content-hash, revision-label and locator context readable: a
    64-hex sha256 or a long build directory is legitimate there. Everywhere else
    the hash, base64, IBAN and card shapes are masked too.
    """
    if not isinstance(text, str):
        return text
    text = _PEM_BLOCK.sub(REDACTED, text)
    for pattern in (_JWT, _PROVIDER, _BEARER, _URL_USERINFO):
        text = pattern.sub(REDACTED, text)
    text = _ASSIGN.sub(lambda match: match.group(0)[:match.start(1) - match.start(0)]
                       + REDACTED, text)
    if not label:
        text = _IBAN.sub(REDACTED, text)
        text = _CARD.sub(_mask_card, text)
        text = _LONG_HEX.sub(REDACTED, text)
        text = _LONG_B64.sub(REDACTED, text)
    return text


def redact(node, key=None, label=False):
    """Return an output-only copy with detected values hidden and detected keys renamed.

    Values under a credential-named key are replaced whole; every other string is
    scrubbed in place, so a diagnostic keeps its wording. Renamed keys get a unique
    suffix instead of colliding, so no entry is dropped. The input is not mutated.
    """
    if isinstance(node, dict):
        out = {}
        remaining = set(node)
        for k, v in node.items():
            remaining.discard(k)
            if isinstance(k, str) and scan_text(k):
                name, index = REDACTED, 2
                while name in out or name in remaining:
                    name = '%s#%d' % (REDACTED, index)
                    index += 1
            else:
                name, index = k, 2
                while name in out:
                    name = '%s#%d' % (k, index)
                    index += 1
            out[name] = redact(v, k, label or k in LABEL_KEYS)
        return out
    if isinstance(node, list):
        return [redact(v, key, label) for v in node]
    if isinstance(node, str):
        if _credential_key(key) and not _REFERENCE.match(node.strip()):
            return REDACTED
        return scrub(node, label or (isinstance(key, str) and key in LABEL_KEYS))
    return node


def redact_path(path):
    """Hide a credential-shaped dict key so a location string never carries the value."""
    return '.'.join(REDACTED if scan_text(part) else part for part in path.split('.'))


def redact_locator(text):
    """Hide a credential inside a path or URL; a hash inside a path is expected, not a hit."""
    return REDACTED if scan_text(text, label=True) else text
