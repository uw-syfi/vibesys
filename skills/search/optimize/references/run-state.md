# Run state

Each run keeps its state in `.vibesys-optimize/<run-id>/` at the repository root. The directory
is excluded from git, so nothing in it is ever committed.

## Create a run

```sh
RUN_ID="$(date +%Y%m%d-%H%M%S)-<task>"
RUN="$(git rev-parse --show-toplevel)/.vibesys-optimize/$RUN_ID"
mkdir -p "$RUN/notes" "$RUN/worktrees"
echo '.vibesys-optimize/' >> "$(git rev-parse --git-path info/exclude)"
BASE="$(git rev-parse HEAD)"
git branch "vibesys/$RUN_ID" "$BASE"
git worktree add "$RUN/worktrees/main" "vibesys/$RUN_ID"
# Parse the requested duration to seconds (90m = 5400; 1h30m = 5400), then:
python3 - "$RUN" "<task>" "<seconds>" "<fanout>" "$BASE" "$RUN_ID" <<'PYTHON'
import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
run, task, seconds, fanout, base, run_id = sys.argv[1:]
started = datetime.now(timezone.utc)
state = dict(task=task, deadline=(started + timedelta(seconds=int(seconds))).isoformat(),
             fanout=int(fanout), started_at=started.isoformat(), base_commit=base,
             main_branch=f"vibesys/{run_id}")
Path(run, "run.json").write_text(json.dumps(state) + "\n")
Path(run).parent.joinpath("active").write_text(run_id + "\n")
PYTHON
```

Reject an existing `active` run, invalid task names (use letters, digits, hyphens or
underscores), nonpositive durations and fanout values before creating state. Add the exclude
line only if it is not there yet. Use absolute paths in all briefs and measurement commands.

Branch names: main is `vibesys/<run-id>`. A work item's branch is
`vibesys-work/<run-id>/<work-id>`, because git cannot hold `vibesys/<run-id>` and
`vibesys/<run-id>/x` at once. Work item worktrees live in `$RUN/worktrees/<work-id>`; the user's
own checkout stays untouched.

## Files

`run.json`: one JSON object.

| key | meaning |
| --- | --- |
| `task` | the task name, a folder in `.vibesys/tasks/` |
| `deadline` | ISO 8601 with a timezone offset; the stop hook reads this |
| `fanout` | the most subagents running at once |
| `started_at` | ISO 8601 with a timezone offset |
| `base_commit` | full SHA of the commit the run started from |
| `main_branch` | `vibesys/<run-id>` |

`active` (in `.vibesys-optimize/`, not in the run directory): the run id, one line. Present
while the run is live. The lead removes it at the end of wrap-up.

`measurements.jsonl`: written only by `vibesys:measure`'s script; never edit it.

`ledger.md`: a Markdown table, one row per piece of work, updated when its state changes.
Columns: `id`, `kind` (implement, profile, combine, merge), `hypothesis`, `worktree / branch`,
`started`, `ended`, `outcome` (running, win, no gain, failed, parked, abandoned), `best value`.

`decisions.md`: numbered entries, each with what you decided, why, and the alternatives you
turned down. Add one for every park, abandon, merge and change of plan.

`notes/<work-id>.md`: written when you dispatch the item. What you expect it to show, what you
will do on each outcome. After a compaction these are how you know what you meant.

`wakeups.json`: the scheduling tool IDs for this run, canceled at wrap-up.

`report.md`: written at wrap-up.

## Time

```sh
python3 - "$RUN/run.json" <<'PY'
import json, sys
from datetime import datetime, timezone
run = json.load(open(sys.argv[1]))
left = (datetime.fromisoformat(run["deadline"]) - datetime.now(timezone.utc)).total_seconds() / 60
print(f"{left:.0f} minutes left")
PY
```

Wrap-up starts `min(20 minutes, 25% of the time limit)` before the deadline (7.5 minutes for a
30-minute run). Check time after every completion. Give each worker an earlier return time,
leaving room for judging and measurement. Bound each command by the smaller of its declared
timeout and the remaining worker budget. Do not start work that cannot finish in it.
