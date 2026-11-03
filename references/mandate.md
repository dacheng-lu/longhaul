# Mandate

A mandate records why a piece of work may exist at all: who asked, on which channel, under
which policy version, with which scope and strength. It is not permission to run a command —
the host owns that, and Longhaul never executes anything. It is the answer to "why is this
being done, and how far may it go", which a resumed task otherwise inherits from nobody.

It lives in its own file, `<record-stem>.mandate.json`, next to the record. Not a field in
the record, because the record is rewritten on every step while the mandate never changes
after it is granted: a new mandate is a new file under a new id, and no schema migration is
needed for any existing task.

## Shape

```json
{"schema_version": 1, "id": "M1", "source": "github", "actor": "dacheng-lu",
 "channel": "issue#42", "strength": "explicit", "policy_version": "2026-11-02.3",
 "scope": "repo dacheng-lu/makeextreme, branch feat/report-contract, paths docs/**",
 "rules": ["docs-only"], "granted_at": "2026-11-02T09:12:00Z"}
```

`strength` is `explicit` (someone with standing asked) or `inferred` (a written rule or a
bot suggests it). An inferred mandate is the weaker basis and must be re-checked before the
work resumes.

## Rules

- **Immutable.** `mandate` refuses to overwrite an existing file; a wider scope is a new
  mandate under a new id. The old one stays as history.
- **No credentials.** Name who and what, never a token. A secret-shaped value is refused and
  never echoed.
- **`policy_version` is required.** A mandate without one cannot be revalidated, so the gate
  refuses the record.
- **`scope` is a claim, not an ACL.** One line a human can hold against the actual work.

## Revalidating before a resume

```bash
python3 scripts/checkpoint.py revalidate ../longhaul-state/task.json \
    --policy-version 2026-11-02.3 --actor dacheng-lu
```

Exit `0` the mandate still holds, `1` it does not (the reasons are named), `2` there is no
mandate on record. A failed revalidation leaves the record untouched: the work needs a new
mandate, not a repaired old one. Only a person's stop withdraws a mandate outright.

## Why an agent may not infer one

"ok", "mach", "passt", "ja" is not a mandate — it is prose. A mandate is a record with an
actor, a channel, a policy version and a scope, written once and checkable later. Anything
that can be relaxed by adding a sentence to a prompt is not authorisation.
