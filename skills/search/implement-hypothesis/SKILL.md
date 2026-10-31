---
name: implement-hypothesis
description: "Use when a vibesys optimize brief gives you one hypothesis to implement in your own git worktree. Makes the smallest change that yields a measurement, commits it, measures it with vibesys:measure, and hands back the claim, the commit, the verdict, what was learned and the next step. Never edits .vibesys/ or the task's checker or benchmark."
---

# Implement a hypothesis

You own one hypothesis in one worktree. Your brief names the claim, the pass criteria, the
parent commit and the scope. Details of the steps and the handoff are in
[handoff.md](references/handoff.md).

## Start

1. Run the first command of your brief and confirm the worktree and branch. Do not go on if they
   differ. Everything you do happens in that worktree.
2. Read `OBJECTIVE.md` in the task directory. Its rules bind you, whatever a measurement says.
3. Load `vibesys:measure` and the domain skills the brief names.

## Work

1. **Start from the first measurable step**: the smallest change to the claim that you can
   finish and measure in this turn. If you cannot finish a step, do not stop on partial work:
   name the concrete blocker (the file, function, check or error) and the narrower step that
   would get past it.
2. Build, then run the cheapest check that covers what you changed. Once it builds and one local
   check passes, commit and measure; do not polish first. After a failure, change the code
   before you run the check again. When output is long, write it to a file and read the file.
3. Commit in your worktree. If your brief grants no measurement slot, return the SHA as
   `ready for measurement`; the lead measures it before judging it. Otherwise measure that commit with `vibesys:measure`: the correctness
   check, then at least 5 benchmark samples, recorded in the run directory. Compare with the
   base: `compare --commit <sha>`. Add `--base <sha>` when you built on a candidate, to see the
   gain over your parent.
4. With the verdict in hand, build the next step on it, or stop.


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

## Never

- Edit anything under `.vibesys/`, the task's checker or its benchmark.
- Add a stub, a special case for the benchmark's inputs, a skipped check or any shortcut that
  raises a number without the program doing the work. Obey every rule in `OBJECTIVE.md`.
- Report a number that is not in the measurement record.

## Hand back

Claim, commit SHA, the verdict (the `compare` output), what you learned, and the step you would
take next. If you made no candidate: the blocker and the narrower step.
