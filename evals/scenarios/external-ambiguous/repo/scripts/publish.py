"""Append one entry to the publish ledger. It does not check for duplicates."""
import json
import sys
from pathlib import Path

ident = sys.argv[1]
dry = '--dry-run' in sys.argv
path = Path('external/ledger.jsonl')
if dry:
    print('dry-run: would append %s' % ident)
else:
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'id': ident, 'at': '2026-10-30T12:00:00Z',
                                 'channel': 'stable'}) + '\n')
    print('appended %s' % ident)
