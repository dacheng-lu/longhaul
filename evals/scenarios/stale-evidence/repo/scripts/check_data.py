import csv
from pathlib import Path

rows = list(csv.DictReader(Path('data/data.csv').open(encoding='utf-8')))
print('rows=%d total=%d' % (len(rows), sum(int(row['amount']) for row in rows)))
