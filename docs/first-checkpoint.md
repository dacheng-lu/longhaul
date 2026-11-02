# First checkpoint: detect stale evidence

This standalone helper example needs Linux and Python 3; run from the repository
root. It uses a temporary directory, so copy the task state and artifact to host
durable storage for a real cross-session handoff. It does not run an agent or
simulate a process interruption. The check below verifies one exact line only.

```bash
workdir=$(mktemp -d)
state="$workdir/task.json"
printf 'ready\n' > "$workdir/report.txt"
python3 scripts/checkpoint.py init "$state" --task-id demo --goal 'Verify a report'
python3 scripts/checkpoint.py artifact "$state" --id A1 --locator report.txt
test "$(cat "$workdir/report.txt")" = ready
python3 scripts/checkpoint.py evidence "$state" --id E1 --artifact A1 --result pass
python3 - "$state" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
state = json.loads(path.read_text())
state['acceptance'] = [{'id': 'C1', 'criterion': 'Report says ready',
                        'status': 'verified', 'evidence_ids': ['E1']}]
path.write_text(json.dumps(state))
PY
python3 scripts/checkpoint.py gate "$state"

# The report changes; re-register it and confirm the old evidence is rejected.
printf 'ready\nupdated\n' > "$workdir/report.txt"
python3 scripts/checkpoint.py artifact "$state" --id A1 --locator report.txt
if python3 scripts/checkpoint.py gate "$state"; then
  printf 'ERROR: stale evidence unexpectedly passed\n' >&2
  exit 1
else
  printf 'Expected: the gate rejected stale evidence.\n'
fi

# Re-check the new content before recording a new observation.
test "$(cat "$workdir/report.txt")" = "$(printf 'ready\nupdated')"
python3 scripts/checkpoint.py evidence "$state" --id E1 --artifact A1 --result pass
python3 scripts/checkpoint.py gate "$state"
printf 'Example files: %s\n' "$workdir"
```

The `pass` observation is supplied by the operator after the shown check; the
helper cannot prove the operator performed it or determine semantic correctness.
For production, preserve the state with the host's durable storage and use the
safe-edit procedure in the [checkpoint contract](../references/state.md).
