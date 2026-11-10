package alloc_test

import (
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// A job's end that reports processes left in its allocation ends the allocation, external, and
// asks for its release, before the job's JobFinished could start the next job beside them: the job
// that reported them is done and not lost, and the job queued behind it does not start there. A
// pending or ended allocation is unchanged. Before, a failed kill was only logged and the next job
// ran beside the processes.
func TestProcessesLeftInAnAllocationEndItBeforeTheNextJobStarts(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 10*time.Hour)
	w.submit("j1", time.Hour, id)
	w.do(alloc.JobStarted{Job: coreJob("j1")})
	w.submit("j2", time.Hour, id)
	actions := w.do(alloc.ProcessesLeft{ID: id, Job: coreJob("j1")})
	if end := w.ended(id); end.Reason != alloc.EndExternal || end.Released {
		t.Fatalf("the allocation ended %+v, want external with its release asked", end)
	}
	if !has(actions, alloc.Release{ID: id, Handle: w.get(id).Handle}) {
		t.Fatalf("the step asked %v, want the release of %s", kinds(actions), id)
	}
	for _, a := range actions {
		switch a := a.(type) {
		case alloc.StartJob:
			t.Fatalf("%s started beside the processes left in %s", a.Job, id)
		case alloc.LoseJob:
			t.Fatalf("the job that reported the processes was lost: %+v", a)
		}
	}
	if !slices.ContainsFunc(actions, func(a alloc.Action) bool { d, ok := a.(alloc.DropJob); return ok && d.Job == coreJob("j2") }) {
		t.Fatalf("the step asked %v, want the queued j2 dropped", kinds(actions))
	}
	if actions := w.do(alloc.JobFinished{Job: coreJob("j1")}); len(actions) != 0 {
		t.Fatalf("the job's end after the allocation's asked %v", kinds(actions))
	}

	pending := w.ask(gpu, time.Hour)
	before := w.get(pending)
	w.do(alloc.ProcessesLeft{ID: pending, Job: coreJob("j3")})
	if got := w.get(pending); got.Stage() != before.Stage() {
		t.Fatalf("a pending allocation is %s after processes were reported left in it", got.Stage())
	}
}
