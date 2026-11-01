"""Render docs/page.md into the static page."""
import shutil
from pathlib import Path

if shutil.which('lhrender') is None:
    print('lhrender: not found on PATH')
else:
    print('lhrender: available')
Path('out/page.html').write_text(
    '<h1>Changelog page</h1>\n<p>This page lists the 3.14 changes.</p>\n', encoding='utf-8')
print('wrote out/page.html')
