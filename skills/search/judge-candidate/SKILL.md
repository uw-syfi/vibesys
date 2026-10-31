---
name: judge-candidate
description: "Use when a vibesys optimize brief asks you to independently review a candidate commit that claims a gain, before the lead treats it as a win. Read-only: compares the diff with its parent against the claim, checks that the change is on the measured path, looks for gaming of the check or benchmark and for broken rules in OBJECTIVE.md, and returns pass, fail or unmeasured with reasons."
---

# Judge a candidate

You decide whether a candidate's claim is supported. You did not write it and you edit nothing.
The checklist and the verdict format are in [review.md](references/review.md).

## Start

Run the first command of your brief and confirm the worktree. It is a checkout of the candidate
commit. Read `OBJECTIVE.md` in the task directory: its rules decide pass or fail as much as any
measurement does. Load `vibesys:measure`.

## Decide

Start from the diff against the parent (`git diff <parent> <candidate>`) and the claim. Answer:

1. **Correct?** Does the code do what the claim says, and does it still do everything it did?
   Run the task's declared correctness command yourself, in your own candidate worktree,
   using your own allocated port. Worker evidence is not a substitute. Coordinate the slot
   with the lead. Return `unmeasured` if the budget cannot cover the check.
   Start and clean up services with the saved-PID recipe below.
2. **Active?** Is the change on the path the benchmark runs? A change that the benchmark never
   reaches cannot explain a gain.
3. **Honest?** Does it special-case the benchmark's inputs, cache on its seed, skip or weaken a
   check, stub work, or touch `.vibesys/`, the checker or the benchmark?
4. **Within the rules?** Does it break any rule or constraint that `OBJECTIVE.md` states?
5. **Real?** Is the gain beyond noise? The recorded verdict decides this:
   Run `python3 "${CLAUDE_SKILL_DIR}/../measure/scripts/measurements.py" compare
   --run <dir> --commit <candidate> --base <parent> --json` yourself and read its output.
   Confirm the full commit SHA and recorded samples support the claimed gain; worker text
   is not measurement evidence. Fail a claim
   about speed only when a recorded result shows it is not there. If no comparable verdict
   exists, return `unmeasured` and ask the lead to schedule measurement. Never run a long
   measurement in the judge; the lead records it before requesting a new judgment.
   A correctness-only pass is `unmeasured`, never an unconditional `pass`.

Source reading decides correctness and rule violations. Only a recorded measurement decides a
claim about speed. Do not run other benchmarks. The required correctness check is the exception to the short-command budget.

Passing the check is not enough: reject a candidate that violates a rule in `OBJECTIVE.md`.


Start a required service with `&`, save `$!` immediately, and kill only that saved PID:

```sh
./server --port "$P" &
service_pid=$!
trap 'kill "$service_pid" 2>/dev/null || true; wait "$service_pid" 2>/dev/null || true' EXIT
ready=false
for i in $(seq 50); do
  if redis-cli -p "$P" PING; then ready=true; break; fi
  sleep 0.1
done
[ "$ready" = true ] || exit 1
```

Adapt the start command and readiness probe to the task. Bounded readiness waits are fine;
fixed startup delays and open-ended sleeps are not. Never use `pkill`, `killall`, or a PID
found through port lookup. Ownership comes from `$!`, not a process name or listening port.

## Return

`pass`, `fail` or `unmeasured`, and the reasons, each tied to a file and line or a recorded
number.
