The worker's report does not survive an independent run: `python3 -m unittest discover -s tests` fails on tests/test_dates.py because 2026-03-04 is formatted as 04.03.2026. src/dates.py is wrong and I did not edit it, since src/ is outside this task's write scope. reports/review.md records the observation, C1 is marked failed and the task stays incomplete.

FINAL: incomplete
