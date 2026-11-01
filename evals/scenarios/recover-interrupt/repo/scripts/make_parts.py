"""Write out/part3.md with the batch total."""
import csv
from pathlib import Path

rows = list(csv.DictReader(Path('data/raw.csv').open(encoding='utf-8')))
total = sum(int(row['amount']) for row in rows)
Path('out').mkdir(exist_ok=True)
Path('out/part3.md').write_text('total: %d\n' % total, encoding='utf-8')
print('wrote out/part3.md total=%d' % total)
