# Stop states

`blockers` says in prose what stands in the way. `report` says it in a word a machine can
sort, so a tick, a panel or another agent can separate "a person must act" from "the work is
still going". The block is optional: a record without it validates exactly as in 1.2.

## The six states

| state | means | ends the round? |
|---|---|---|
| `progress` | work is running | no |
| `waiting` | the turn ended and nothing is outstanding | no |
| `ready` | the artifact exists and wants review | yes, a person reviews |
| `blocked` | cannot continue without a person | yes |
| `failed` | an attempt ended badly | yes |
| `dropped` | there was nothing to do | yes, and it may be complete |

Derived stage, first match wins: `ready` → `person/review`, `blocked` → `person/blocked`,
`failed` → `person/failed`, `progress` → `working`, `waiting` → `idle`. Only the three
`person/*` stages may page anyone; everything else is machine work that stays quiet.

## Writing it

```bash
python3 scripts/checkpoint.py report ../longhaul-state/task.json --state blocked \
    --reason "no SSH key on 100.88.235.211" --next "add the key, then rerun the probe"
```

The command prints the block it replaced, so a later report can be compared with the one it
superseded. `since` defaults to now and must be ISO-8601 with an offset.

## Rules the gate keeps

- `state` must be one of the six; a sentence where the state belongs is refused.
- `reason` must be non-empty and at most 300 characters.
- `next` is one step, like `next_action`.
- `status: complete` may not stand over `blocked` or `failed`. `dropped` may: work that
  needed no action can be a completed end.

A passed gate still says nothing about correctness — the state is bookkeeping, and the
reviewer's judgement remains the reviewer's.
