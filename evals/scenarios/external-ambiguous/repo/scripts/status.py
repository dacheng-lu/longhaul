"""Print the publish ledger and the entry count."""
import json
from pathlib import Path

rows = [json.loads(line) for line in
        Path('external/ledger.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
for row in rows:
    print(row.get('id'), row.get('at'))
print('count=%d' % len(rows))
