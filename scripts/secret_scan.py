#!/usr/bin/env python3
"""Credential detection for Longhaul task state.

Reports *where* a secret-shaped value sits, never the value itself.
Standard library only; no network. Tuned to be conservative: a false
positive costs one edit, a missed secret costs a leaked key.

Ported from the local context-handoff validator (MIT).
"""
import re

# Keys whose values are content hashes or revision labels by design.
EXEMPT_KEYS = frozenset({'sha256', 'revision', 'digest', 'head', 'commit',
                         'git_head', 'artifact_revisions', 'locator'})

_ASSIGN = re.compile(
    r'(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token'
    r'|auth[_-]?token|refresh[_-]?token|bearer[_-]?token|password|passwd|pwd'
    r'|private[_-]?key)\b\s*[:=]\s*["\']?([^\s"\',;]{6,})')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_PEM = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_PROVIDER = re.compile(
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
    r'|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})')
_IBAN = re.compile(r'\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b')
_CARD = re.compile(r'\b(?:\d[ -]?){15,18}\d\b')
_LONG_HEX = re.compile(r'\b[0-9a-fA-F]{40,}\b')
_LONG_B64 = re.compile(r'\b[A-Za-z0-9+/]{60,}={0,2}\b')


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


def scan_text(text):
    """Return the kinds of secret shapes found in one string (no values)."""
    kinds = []
    if _PEM.search(text):
        kinds.append('private_key_block')
        return kinds
    if _JWT.search(text):
        kinds.append('jwt')
    if _PROVIDER.search(text):
        kinds.append('provider_key')
    if _ASSIGN.search(text):
        kinds.append('credential_assignment')
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
    return list(dict.fromkeys(kinds))


def walk(node, path='$', key=None):
    """Yield (path, key, string) for every string value, skipping exempt keys."""
    if key in EXEMPT_KEYS:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f'{path}.{k}', k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f'{path}[{i}]', key)
    elif isinstance(node, str):
        yield path, key, node


def scan_state(state):
    """Return [(path, kind)] for every secret-shaped value in a state document.

    One hit per string: the caller deletes the line, so the second pattern that
    matched the same value is noise, not information.
    """
    hits = []
    for path, _key, value in walk(state):
        kinds = scan_text(value)
        if kinds:
            hits.append((path, kinds[0]))
    return hits
